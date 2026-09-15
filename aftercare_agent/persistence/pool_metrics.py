"""Pool health as bounded metrics, and the sampler that publishes it.

The pool is where a process's remaining capacity becomes visible before a
caller notices it: slots out with a borrower, borrowers waiting for one, units
of work that gave up.  These numbers are diagnostics.  They never authorize
work, they carry no tenant or case, and a sampler that cannot read them stops
instead of failing the process it observes.
"""

import os
from collections.abc import Mapping
from datetime import timedelta
from threading import Event, Lock, Thread
from time import monotonic
from typing import Protocol

from aftercare_agent.observability import MetricRecord, Metrics

from .db import PoolStats

POOL_METRIC_PREFIX = "aftercare.db.pool"
DEFAULT_SAMPLER_INTERVAL_SECONDS = 10.0


class PoolStatsSource(Protocol):
    """Anything that can snapshot a pool; ``Database`` is the real one."""

    def stats(self) -> PoolStats | None: ...


def report_pool_stats(metrics: Metrics, stats: PoolStats, *, component: str) -> None:
    """Publish one snapshot under a bounded label set.

    The only attribute is which process reported the numbers.  A pool gauge
    labelled with a tenant or a case would be a cardinality explosion with no
    diagnostic value: the pool is shared by everything in the process, so the
    label could not explain which of them was waiting.
    """
    gauges = {
        "connections.available": stats.available,
        "connections.in_use": stats.in_use,
        "connections.max": stats.max_size,
        "connections.min": stats.min_size,
        "connections.size": stats.size,
        "requests.waiting": stats.waiting,
    }
    counters = {
        "connections.errors": stats.connection_errors,
        "connections.lost": stats.connections_lost,
        "connections.opened": stats.connections,
        "requests.errors": stats.request_errors,
        "requests.queued": stats.queued,
        "requests.total": stats.requests,
        "requests.wait_ms": stats.wait_ms,
    }
    for suffix, value in gauges.items():
        metrics.record(
            MetricRecord(
                f"{POOL_METRIC_PREFIX}.{suffix}", float(value), "gauge", {"component": component}
            )
        )
    for suffix, value in counters.items():
        metrics.record(
            MetricRecord(
                f"{POOL_METRIC_PREFIX}.{suffix}", float(value), "counter", {"component": component}
            )
        )


class PoolStatsSampler:
    """Publish a pool snapshot every interval until stopped.

    This is deliberately the opposite of the lease heartbeat.  A heartbeat
    that cannot reach the database must fail the work that depends on it; a
    sampler that cannot read the pool only loses a diagnostic, so it records
    the failure and stops rather than failing callers over telemetry.  The
    first sample is taken immediately so that a short-lived process -- a
    migration job, a single Worker slice -- still reports what its pool did.
    """

    def __init__(
        self,
        source: PoolStatsSource,
        metrics: Metrics,
        *,
        component: str,
        interval: timedelta | None = None,
    ) -> None:
        if interval is None:
            seconds = DEFAULT_SAMPLER_INTERVAL_SECONDS
        else:
            seconds = interval.total_seconds()
        if not seconds > 0:
            raise ValueError("sampler interval must be positive")
        self.component = component
        self._source = source
        self._metrics = metrics
        self._interval = seconds
        self._stop = Event()
        self._lock = Lock()
        self._failure: Exception | None = None
        self._thread: Thread | None = None
        self._samples = 0

    @property
    def failure(self) -> Exception | None:
        with self._lock:
            return self._failure

    @property
    def samples(self) -> int:
        with self._lock:
            return self._samples

    @property
    def interval(self) -> timedelta:
        return timedelta(seconds=self._interval)

    def start(self) -> None:
        if self._thread is not None or self._stop.is_set():
            raise RuntimeError("pool stats sampler already started or stopped")
        self._thread = Thread(target=self._run, name="aftercare-pool-metrics", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop sampling and wait for the thread; safe to call twice."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join()

    def sample_once(self) -> bool:
        """Publish one snapshot; ``False`` when there is no pool to report."""
        stats = self._source.stats()
        if stats is None:
            return False
        report_pool_stats(self._metrics, stats, component=self.component)
        with self._lock:
            self._samples += 1
        return True

    def _run(self) -> None:
        deadline = monotonic()
        while True:
            if self._stop.wait(max(deadline - monotonic(), 0.0)):
                return
            try:
                self.sample_once()
            except Exception as exc:
                with self._lock:
                    self._failure = exc
                self._stop.set()
                return
            # Schedule from where this tick started: a slow read must not push
            # every later sample further behind the interval it was asked for.
            deadline += self._interval


def sampler_from_environment(
    source: PoolStatsSource,
    metrics: Metrics,
    *,
    component: str,
    environ: Mapping[str, str] | None = None,
) -> PoolStatsSampler | None:
    """Build the sampler this process should run, or ``None`` when disabled.

    Sampling is on by default because a pool nobody can see is how a capacity
    problem is discovered by customers; ``AFTERCARE_POOL_METRICS=0`` turns it
    off, and ``AFTERCARE_POOL_METRICS_INTERVAL_SECONDS`` changes the interval.
    """
    values = os.environ if environ is None else environ
    enabled = values.get("AFTERCARE_POOL_METRICS", "1")
    if enabled not in {"0", "1"}:
        raise RuntimeError("AFTERCARE_POOL_METRICS must be 0 or 1")
    if enabled == "0":
        return None
    raw = values.get("AFTERCARE_POOL_METRICS_INTERVAL_SECONDS", "")
    interval: timedelta | None = None
    if raw:
        try:
            seconds = float(raw)
        except ValueError as exc:
            raise RuntimeError("AFTERCARE_POOL_METRICS_INTERVAL_SECONDS must be a number") from exc
        if not seconds > 0:
            raise RuntimeError("AFTERCARE_POOL_METRICS_INTERVAL_SECONDS must be positive")
        interval = timedelta(seconds=seconds)
    return PoolStatsSampler(source, metrics, component=component, interval=interval)
