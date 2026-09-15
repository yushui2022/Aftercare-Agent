"""Measure what one process's pool carries, and size it from that.

``aftercare-capacity`` runs three pool-level workloads against a real
PostgreSQL, sweeps ``max_size`` over a list of sizes, and reports success and
failure counts, latency percentiles, pool high-water marks and connection
counts for every run.  The last line is the smallest size whose runs met the
caller's latency budget with no failures.

The unit of work is one short transaction, so the numbers answer "how many
concurrent units of work can this process carry before borrowers start paying
queue time".  This is a pool model, not an end-to-end business load test: it
runs no Agent, calls no model and touches no supplier.  A run is evidence for
sizing a process, never a service level agreement -- publishing a capacity
number means naming its version, workload, failure rate, latency and cost,
which is what the JSON report is for.

``--service-time-ms`` says how long one unit holds its slot, and ``--min-size``
says how much of each swept pool is pre-warmed.  Together they are the
difference between "the pool is fine" and "the pool is the bottleneck": the
same ceiling is enough for sub-millisecond units and far too narrow for 10 ms
units at the same concurrency.
"""

import argparse
import json
import math
import os
import platform
import sys
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Barrier, Event, Thread
from time import monotonic, sleep
from typing import Any

import psycopg

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.persistence import Database, PoolStats

WORKLOAD_SHAPES = ("steady", "burst", "wake")
DEFAULT_MAX_SIZES = (1, 2, 4, 8, 16)
DEFAULT_CONCURRENCY = 8
DEFAULT_ROUNDS = 25
DEFAULT_WAKE_CADENCE_SECONDS = 0.05
DEFAULT_P95_BUDGET_MS = 25.0
DEFAULT_ACQUIRE_TIMEOUT_SECONDS = 1.0
# Warming the pool is not a borrow, so a sweep of very short borrow budgets
# still gets a usable pool instead of failing its own start.
STARTUP_TIMEOUT_SECONDS = 5.0
POOL_SAMPLE_SECONDS = 0.005

NOTE = (
    "Pool-level workload model over short transactions: it sizes a process, "
    "it is not an SLA and it does not include model, connector or sandbox time."
)


@dataclass(frozen=True)
class Workload:
    """One concurrency shape, described in the units the pool actually sees.

    ``steady`` keeps every worker busy without synchronising them, ``burst``
    makes all of them borrow at the same instant once per round, and ``wake``
    models concentrated wake-ups: every worker becomes runnable on a shared
    tick, applies one unit of work, and sleeps until the next tick.
    """

    name: str
    concurrency: int
    rounds: int
    cadence_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.name not in WORKLOAD_SHAPES:
            raise ValueError(f"unknown workload shape {self.name!r}")
        if self.concurrency < 1 or self.rounds < 1:
            raise ValueError("concurrency and rounds must be positive")
        if self.name == "wake":
            if self.cadence_seconds is None or self.cadence_seconds <= 0:
                raise ValueError("the wake workload needs a positive cadence")
        elif self.cadence_seconds is not None:
            raise ValueError("only the wake workload takes a cadence")

    @property
    def units(self) -> int:
        return self.concurrency * self.rounds

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "concurrency": self.concurrency,
            "rounds": self.rounds,
            "cadence_seconds": self.cadence_seconds,
        }


@dataclass(frozen=True)
class RunResult:
    """What one workload did at one ``max_size``.

    Latencies cover completed units only.  A refused borrow is reported as a
    failure instead: it ended at the acquire timeout, and folding that wait
    into the latency percentiles would describe a queue, not a service time.
    """

    workload: str
    max_size: int
    units: int
    failures: int
    errors: int
    latency_ms: tuple[float, ...]
    in_use_high_water: int
    waiting_high_water: int
    # The peaks can only be as sharp as the sampling: a unit of work shorter
    # than the sample interval can be over before the pool is read, so the
    # sample count is reported next to the peak instead of hidden.
    watcher_samples: int
    wait_ms: int
    connections_opened: int
    connection_errors: int
    wall_seconds: float

    @property
    def p50_ms(self) -> float:
        return percentile(self.latency_ms, 0.50)

    @property
    def p95_ms(self) -> float:
        return percentile(self.latency_ms, 0.95)

    @property
    def max_ms(self) -> float:
        return max(self.latency_ms, default=0.0)

    @property
    def met_budget(self) -> bool:
        return self.failures == 0 and self.errors == 0

    def to_json(self) -> dict[str, Any]:
        return {
            "workload": self.workload,
            "max_size": self.max_size,
            "units": self.units,
            "failures": self.failures,
            "errors": self.errors,
            "p50_ms": round(self.p50_ms, 3),
            "p95_ms": round(self.p95_ms, 3),
            "max_ms": round(self.max_ms, 3),
            "in_use_high_water": self.in_use_high_water,
            "waiting_high_water": self.waiting_high_water,
            "watcher_samples": self.watcher_samples,
            "wait_ms": self.wait_ms,
            "connections_opened": self.connections_opened,
            "connection_errors": self.connection_errors,
            "wall_seconds": round(self.wall_seconds, 3),
        }


@dataclass(frozen=True)
class Sizing:
    """The sweep's conclusion, or why it has none."""

    max_size: int | None
    reason: str

    def to_json(self) -> dict[str, Any]:
        return {"max_size": self.max_size, "reason": self.reason}


@dataclass(frozen=True)
class ProbeReport:
    generated_at: str
    python_version: str
    platform_name: str
    postgres_version: str
    acquire_timeout_seconds: float
    service_time_seconds: float
    min_size: int
    p95_budget_ms: float
    workloads: tuple[Workload, ...]
    results: tuple[RunResult, ...]
    sizing: Sizing

    def to_json(self) -> dict[str, Any]:
        return {
            "note": NOTE,
            "generated_at": self.generated_at,
            "python_version": self.python_version,
            "platform": self.platform_name,
            "postgres_version": self.postgres_version,
            "acquire_timeout_seconds": self.acquire_timeout_seconds,
            "service_time_seconds": self.service_time_seconds,
            "min_size": self.min_size,
            "p95_budget_ms": self.p95_budget_ms,
            "workloads": [workload.to_json() for workload in self.workloads],
            "results": [result.to_json() for result in self.results],
            "sizing": self.sizing.to_json(),
        }

    def table(self) -> str:
        header = (
            f"{'workload':<9}{'max':>5}{'units':>7}{'fail':>6}{'err':>5}"
            f"{'p50ms':>9}{'p95ms':>9}{'maxms':>9}{'in_use':>8}{'wait':>6}"
            f"{'opened':>8}{'waitms':>8}{'wall':>8}"
        )
        rows = [header]
        for result in self.results:
            rows.append(
                f"{result.workload:<9}{result.max_size:>5}{result.units:>7}{result.failures:>6}"
                f"{result.errors:>5}{result.p50_ms:>9.2f}{result.p95_ms:>9.2f}{result.max_ms:>9.2f}"
                f"{result.in_use_high_water:>8}{result.waiting_high_water:>6}"
                f"{result.connections_opened:>8}{result.wait_ms:>8}{result.wall_seconds:>8.2f}"
            )
        chosen = "none" if self.sizing.max_size is None else str(self.sizing.max_size)
        rows.append(f"sizing: max_size={chosen} ({self.sizing.reason})")
        return "\n".join(rows)


def percentile(values: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile: the smallest value at or above *fraction*."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def recommend_size(results: Sequence[RunResult], *, p95_budget_ms: float) -> Sizing:
    """The smallest swept size that met the budget on every workload."""
    by_size: dict[int, list[RunResult]] = {}
    for result in results:
        by_size.setdefault(result.max_size, []).append(result)
    for size in sorted(by_size):
        runs = by_size[size]
        unmet = [run for run in runs if run.failures or run.errors or run.p95_ms > p95_budget_ms]
        if not unmet:
            return Sizing(
                size,
                f"smallest size with no failures and p95 <= {p95_budget_ms:g} ms",
            )
    if not by_size:
        return Sizing(None, "no results")
    worst = max(
        by_size[max(by_size)],
        key=lambda run: (run.failures + run.errors, run.p95_ms),
    )
    return Sizing(
        None,
        f"no swept size met p95 <= {p95_budget_ms:g} ms with no failures; at "
        f"max_size={worst.max_size} the {worst.workload} workload had "
        f"{worst.failures + worst.errors} failed units and p95 {worst.p95_ms:.2f} ms",
    )


class _PoolWatcher:
    """Sample the pool while a workload runs and keep only the peaks.

    The watcher is a single writer, so the peaks need no lock, and it stops
    itself when the pool disappears instead of reporting a stale peak.
    """

    def __init__(self, source: Database, *, interval: float = POOL_SAMPLE_SECONDS) -> None:
        self._source = source
        self._interval = interval
        self._stop = Event()
        self._thread: Thread | None = None
        self.in_use_peak = 0
        self.waiting_peak = 0
        self.samples = 0

    def start(self) -> None:
        self._thread = Thread(target=self._run, name="aftercare-capacity-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join()

    def _run(self) -> None:
        # Sample before the first wait: a run shorter than one interval still
        # deserves a reading, and the peaks are sampled, not guaranteed.
        while True:
            stats = self._source.stats()
            if stats is None:
                return
            self.samples += 1
            self.in_use_peak = max(self.in_use_peak, stats.in_use)
            self.waiting_peak = max(self.waiting_peak, stats.waiting)
            if self._stop.wait(self._interval):
                return


def _pool_stats(database: Database) -> PoolStats:
    stats = database.stats()
    if stats is None:
        raise RuntimeError("capacity runs need a pooled Database")
    return stats


def run_workload(
    database: Database, workload: Workload, *, service_time_seconds: float = 0.0
) -> RunResult:
    """Run one workload against one pool and describe what happened.

    A unit of work is one short transaction.  *service_time_seconds* is how
    long that transaction holds its slot, so a sweep can model work heavier
    than a round trip; at zero the unit is a bare ``SELECT 1`` and the numbers
    are the pool's own overhead.
    """
    before = _pool_stats(database)
    watcher = _PoolWatcher(database)
    round_barrier = Barrier(workload.concurrency)
    start_barrier = Barrier(workload.concurrency)
    tick_origin = monotonic()

    def worker() -> tuple[list[float], int, int]:
        latencies: list[float] = []
        failures = 0
        errors = 0

        def unit() -> None:
            nonlocal failures, errors
            began = monotonic()
            try:
                with database.transaction() as connection:
                    if service_time_seconds > 0:
                        connection.execute("SELECT pg_sleep(%s)", (service_time_seconds,))
                    else:
                        connection.execute("SELECT 1")
            except ContractViolation as exc:
                # A refused borrow is the pool telling the caller to retry.
                if exc.code is ErrorCode.RETRYABLE:
                    failures += 1
                else:
                    errors += 1
            except psycopg.Error:
                errors += 1
            else:
                latencies.append((monotonic() - began) * 1000.0)

        if workload.name == "steady":
            start_barrier.wait()
            for _ in range(workload.rounds):
                unit()
        elif workload.name == "burst":
            for _ in range(workload.rounds):
                round_barrier.wait()
                unit()
        else:
            assert workload.cadence_seconds is not None
            for index in range(workload.rounds):
                deadline = tick_origin + workload.cadence_seconds * (index + 1)
                sleep(max(deadline - monotonic(), 0.0))
                unit()
        return latencies, failures, errors

    started = monotonic()
    watcher.start()
    latencies: list[float] = []
    failures = 0
    errors = 0
    try:
        with ThreadPoolExecutor(
            max_workers=workload.concurrency, thread_name_prefix="aftercare-capacity"
        ) as pool:
            for future in [pool.submit(worker) for _ in range(workload.concurrency)]:
                worker_latencies, worker_failures, worker_errors = future.result()
                latencies.extend(worker_latencies)
                failures += worker_failures
                errors += worker_errors
    finally:
        watcher.stop()
    wall_seconds = monotonic() - started
    after = _pool_stats(database)
    return RunResult(
        workload=workload.name,
        max_size=after.max_size,
        units=len(latencies),
        failures=failures,
        errors=errors,
        latency_ms=tuple(latencies),
        in_use_high_water=watcher.in_use_peak,
        waiting_high_water=watcher.waiting_peak,
        watcher_samples=watcher.samples,
        wait_ms=max(after.wait_ms - before.wait_ms, 0),
        connections_opened=max(after.connections - before.connections, 0),
        connection_errors=max(after.connection_errors - before.connection_errors, 0),
        wall_seconds=wall_seconds,
    )


def run_probe(
    dsn: str,
    *,
    workloads: Sequence[Workload],
    max_sizes: Sequence[int],
    p95_budget_ms: float = DEFAULT_P95_BUDGET_MS,
    acquire_timeout_seconds: float = DEFAULT_ACQUIRE_TIMEOUT_SECONDS,
    service_time_seconds: float = 0.0,
    min_size: int = 1,
) -> ProbeReport:
    """Sweep ``max_size`` and report every workload at every size.

    Each size gets its own pool: a reused pool would carry the previous run's
    counters into the next one and make the connection counts meaningless.
    ``min_size`` is how much of the pool is pre-warmed: a cold pool grows one
    handshake at a time, which a burst meets before it meets ``max_size``.
    """
    if not max_sizes or min(max_sizes) < 1:
        raise ValueError("max_sizes must be positive")
    if min_size < 0 or min_size > min(max_sizes):
        raise ValueError("min_size must be between 0 and the smallest max_size")
    results: list[RunResult] = []
    for size in max_sizes:
        database = Database(
            dsn,
            min_size=min_size,
            max_size=size,
            acquire_timeout=acquire_timeout_seconds,
        )
        try:
            database.startup(timeout=STARTUP_TIMEOUT_SECONDS)
            for workload in workloads:
                results.append(
                    run_workload(database, workload, service_time_seconds=service_time_seconds)
                )
        finally:
            database.close()
    return ProbeReport(
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        python_version=platform.python_version(),
        platform_name=f"{platform.system()} {platform.release()}",
        postgres_version=server_version(dsn),
        acquire_timeout_seconds=acquire_timeout_seconds,
        service_time_seconds=service_time_seconds,
        min_size=min_size,
        p95_budget_ms=p95_budget_ms,
        workloads=tuple(workloads),
        results=tuple(results),
        sizing=recommend_size(results, p95_budget_ms=p95_budget_ms),
    )


def server_version(dsn: str) -> str:
    """The server's version string, so a published number names its database."""
    with psycopg.connect(dsn) as connection:
        row = connection.execute("SHOW server_version").fetchone()
    return "unknown" if row is None else str(row[0])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aftercare-capacity",
        description=(
            "Sweep the process pool's max_size against steady, burst and wake-up "
            "workloads on a real PostgreSQL. Runs no model, connector or sandbox."
        ),
    )
    parser.add_argument("--dsn", default=None, help="PostgreSQL DSN; defaults to DATABASE_URL")
    parser.add_argument(
        "--max-sizes",
        default=",".join(str(size) for size in DEFAULT_MAX_SIZES),
        help="comma-separated pool ceilings to sweep",
    )
    parser.add_argument(
        "--workloads",
        default=",".join(WORKLOAD_SHAPES),
        help=f"comma-separated shapes from {', '.join(WORKLOAD_SHAPES)}",
    )
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS)
    parser.add_argument(
        "--min-size",
        type=int,
        default=1,
        help="connections pre-warmed in every swept pool; full prewarm is min-size=max-size",
    )
    parser.add_argument("--wake-cadence", type=float, default=DEFAULT_WAKE_CADENCE_SECONDS)
    parser.add_argument("--p95-budget-ms", type=float, default=DEFAULT_P95_BUDGET_MS)
    parser.add_argument("--acquire-timeout", type=float, default=DEFAULT_ACQUIRE_TIMEOUT_SECONDS)
    parser.add_argument(
        "--service-time-ms",
        type=float,
        default=0.0,
        help="how long one unit of work holds its slot; 0 measures pool overhead alone",
    )
    parser.add_argument("--json", default=None, help="write the report to this path")
    return parser


def _parse_sizes(raw: str) -> tuple[int, ...]:
    sizes = []
    for part in raw.split(","):
        text = part.strip()
        if not text:
            continue
        try:
            sizes.append(int(text))
        except ValueError as exc:
            raise SystemExit("--max-sizes takes comma-separated integers") from exc
    if not sizes or min(sizes) < 1:
        raise SystemExit("--max-sizes needs positive integers")
    return tuple(sizes)


def _parse_workloads(
    raw: str, *, concurrency: int, rounds: int, cadence: float
) -> tuple[Workload, ...]:
    selected = []
    for part in raw.split(","):
        name = part.strip()
        if not name:
            continue
        if name not in WORKLOAD_SHAPES:
            raise SystemExit(f"--workloads takes shapes from {', '.join(WORKLOAD_SHAPES)}")
        try:
            selected.append(
                Workload(
                    name=name,
                    concurrency=concurrency,
                    rounds=rounds,
                    cadence_seconds=cadence if name == "wake" else None,
                )
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    if not selected:
        raise SystemExit("--workloads needs at least one shape")
    return tuple(selected)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dsn = args.dsn or os.environ.get("DATABASE_URL", "")
    if not dsn:
        print("DATABASE_URL or --dsn is required", file=sys.stderr)
        return 2
    if args.concurrency < 1 or args.rounds < 1:
        raise SystemExit("--concurrency and --rounds must be positive")
    if not args.p95_budget_ms > 0 or not args.acquire_timeout > 0:
        raise SystemExit("--p95-budget-ms and --acquire-timeout must be positive")
    if args.service_time_ms < 0:
        raise SystemExit("--service-time-ms cannot be negative")
    if args.min_size < 0:
        raise SystemExit("--min-size cannot be negative")
    workloads = _parse_workloads(
        args.workloads,
        concurrency=args.concurrency,
        rounds=args.rounds,
        cadence=args.wake_cadence,
    )
    sizes = _parse_sizes(args.max_sizes)
    if args.min_size > min(sizes):
        raise SystemExit("--min-size cannot exceed the smallest swept size")
    report = run_probe(
        dsn,
        workloads=workloads,
        max_sizes=sizes,
        p95_budget_ms=args.p95_budget_ms,
        acquire_timeout_seconds=args.acquire_timeout,
        service_time_seconds=args.service_time_ms / 1000.0,
        min_size=args.min_size,
    )
    print(report.table())
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(report.to_json(), handle, indent=2, sort_keys=True)
            handle.write("\n")
    return 0 if report.sizing.max_size is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
