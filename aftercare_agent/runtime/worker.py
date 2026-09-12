"""Durable Worker slices, lease heartbeats and a bounded polling loop.

The model/tool call is deliberately outside a database transaction.  A
separate short-lived connection renews the Run lease while that call is in
flight; the final checkpoint write is still fenced, so a lost heartbeat can
never turn into an unprotected commit.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Event, Lock, Thread

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.protocol import Checkpoint
from aftercare_agent.domain.runtime import ExecutionClaim
from aftercare_agent.persistence import (
    AdmissionRepository,
    CheckpointRepository,
    Database,
    RunRepository,
    SlotReservation,
)

from .harness import HarnessResult
from .harness import run_fake_harness as run_fake_harness

__all__ = [
    "LeaseHeartbeat",
    "WorkerLoopResult",
    "WorkerResult",
    "run_daemon",
    "run_fake_harness",
    "run_next",
    "run_once",
]


@dataclass(frozen=True)
class WorkerResult:
    """Outcome of one bounded lease-held execution slice."""

    tenant_id: str
    case_id: str
    run_id: str
    owner: str
    fencing_token: int
    checkpoint: Checkpoint
    completed: bool


@dataclass(frozen=True)
class WorkerLoopResult:
    """Counters returned when a polling Worker is explicitly stopped."""

    iterations: int
    claimed: int
    completed: int
    idle_polls: int
    failures: int


class LeaseHeartbeat:
    """Renew one claim on a separate connection until the slice finishes.

    Heartbeats do not grant execution rights and do not hide a lost lease.  A
    database error or fencing rejection stops the heartbeat; the caller then
    refuses to save the slice and lets the normal fenced write report the
    authoritative error.  A fresh connection per tick avoids sharing a
    psycopg connection across threads.
    """

    def __init__(
        self,
        database: Database,
        claim: ExecutionClaim,
        lease: timedelta,
        *,
        interval: timedelta | None = None,
    ) -> None:
        _validate_duration("lease", lease)
        selected = interval or lease / 3
        _validate_duration("heartbeat interval", selected)
        _validate_heartbeat_interval(lease, selected)
        self._database = database
        self._claim = claim
        self._lease = lease
        self._interval = selected
        self._stop = Event()
        self._lock = Lock()
        self._failure: Exception | None = None
        self._thread: Thread | None = None

    @property
    def failure(self) -> Exception | None:
        with self._lock:
            return self._failure

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("lease heartbeat already started")
        self._thread = Thread(target=self._run, name="aftercare-lease-heartbeat", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            # Do not allow a renewal to race the final fenced checkpoint.
            # A stuck database call must delay this slice, not leave a daemon
            # thread mutating the lease after the slice has returned.
            thread.join()

    def _run(self) -> None:
        seconds = self._interval.total_seconds()
        while not self._stop.wait(seconds):
            try:
                with self._database.transaction() as connection:
                    RunRepository().renew(connection, self._claim, self._lease)
                    AdmissionRepository().renew_slot(connection, self._claim, self._lease)
            except Exception as exc:
                with self._lock:
                    self._failure = exc
                self._stop.set()
                return


def _validate_duration(name: str, value: timedelta) -> None:
    if value.total_seconds() <= 0:
        raise ContractViolation(ErrorCode.INVALID_INPUT, f"{name} must be positive")


def _validate_heartbeat_interval(lease: timedelta, interval: timedelta | None) -> None:
    if interval is None:
        return
    _validate_duration("heartbeat interval", interval)
    if interval >= lease:
        raise ContractViolation(
            ErrorCode.INVALID_INPUT, "heartbeat interval must be shorter than lease"
        )


def _validate_steps(max_steps: int) -> None:
    if type(max_steps) is not int or max_steps < 1:
        raise ContractViolation(ErrorCode.INVALID_INPUT, "max_steps must be positive")


def _execute_claim(
    database: Database,
    *,
    claim: ExecutionClaim,
    previous: Checkpoint | None,
    execution_time: datetime,
    max_steps: int,
    lease: timedelta,
    heartbeat_interval: timedelta | None = None,
    slot: SlotReservation | None = None,
) -> WorkerResult:
    _validate_steps(max_steps)
    heartbeat = LeaseHeartbeat(
        database,
        claim,
        lease,
        interval=heartbeat_interval,
    )
    heartbeat.start()
    try:
        result: HarnessResult = run_fake_harness(
            tenant_id=claim.tenant_id,
            case_id=claim.case_id,
            run_id=claim.run_id,
            checkpoint=previous,
            now=execution_time,
            max_steps=max_steps,
        )
    finally:
        heartbeat.stop()
    if heartbeat.failure is not None:
        raise ContractViolation(ErrorCode.LEASE_LOST, "lease heartbeat failed")
    checkpoint = result.checkpoint.model_copy(update={"saved_fencing_token": claim.fencing_token})
    runs = RunRepository()
    checkpoints = CheckpointRepository()
    with database.transaction() as connection:
        checkpoints.save(connection, checkpoint, claim)
        # A bounded slice gives the execution right back so another Worker
        # can resume it without waiting for lease expiry.
        runs.transition(connection, claim, "COMPLETED" if result.completed else "READY")
        if slot is not None:
            AdmissionRepository().release_slot(connection, claim)
    return WorkerResult(
        tenant_id=claim.tenant_id,
        case_id=claim.case_id,
        run_id=claim.run_id,
        owner=claim.owner,
        fencing_token=claim.fencing_token,
        checkpoint=checkpoint,
        completed=result.completed,
    )


def run_once(
    database: Database,
    *,
    tenant_id: str,
    run_id: str,
    owner: str,
    now: datetime | None = None,
    lease: timedelta = timedelta(seconds=30),
    max_steps: int = 8,
    heartbeat_interval: timedelta | None = None,
) -> WorkerResult:
    """Claim one Run, execute a bounded Fake Harness slice, and persist it.

    The model/tool loop runs outside database transactions. A short final
    transaction checks the same fencing token before writing the checkpoint;
    only then is a completed Run transitioned to ``COMPLETED``.
    """
    _validate_duration("lease", lease)
    _validate_heartbeat_interval(lease, heartbeat_interval)
    _validate_steps(max_steps)
    execution_time = now or datetime.now(UTC)
    runs = RunRepository()
    checkpoints = CheckpointRepository()
    with database.transaction() as connection:
        claim = runs.claim(connection, tenant_id, run_id, owner, lease)
        slot = AdmissionRepository().acquire_slot(connection, claim, lease)
        previous = checkpoints.get_latest(connection, tenant_id, run_id)

    return _execute_claim(
        database,
        claim=claim,
        previous=previous,
        execution_time=execution_time,
        max_steps=max_steps,
        lease=lease,
        heartbeat_interval=heartbeat_interval,
        slot=slot,
    )


def run_next(
    database: Database,
    *,
    tenant_id: str | None,
    owner: str,
    now: datetime | None = None,
    lease: timedelta = timedelta(seconds=30),
    max_steps: int = 8,
    heartbeat_interval: timedelta | None = None,
) -> WorkerResult | None:
    """Claim and execute one runnable Run, or return ``None`` when idle.

    Passing a tenant dispatches only that tenant.  Passing ``None`` enables
    the PostgreSQL-backed fair queue to share one Worker pool across tenants.
    """
    _validate_duration("lease", lease)
    _validate_heartbeat_interval(lease, heartbeat_interval)
    _validate_steps(max_steps)
    execution_time = now or datetime.now(UTC)
    runs = RunRepository()
    checkpoints = CheckpointRepository()
    with database.transaction() as connection:
        claim = runs.claim_next(connection, tenant_id, owner, lease)
        if claim is None:
            return None
        slot = AdmissionRepository().acquire_slot(connection, claim, lease)
        previous = checkpoints.get_latest(connection, claim.tenant_id, claim.run_id)
    return _execute_claim(
        database,
        claim=claim,
        previous=previous,
        execution_time=execution_time,
        max_steps=max_steps,
        lease=lease,
        heartbeat_interval=heartbeat_interval,
        slot=slot,
    )


def run_daemon(
    database: Database,
    *,
    tenant_id: str | None,
    owner: str,
    stop_event: Event | None = None,
    idle_sleep: timedelta = timedelta(seconds=1),
    lease: timedelta = timedelta(seconds=30),
    heartbeat_interval: timedelta | None = None,
    max_steps: int = 8,
    max_iterations: int | None = None,
    on_error: Callable[[Exception], None] | None = None,
) -> WorkerLoopResult:
    """Poll runnable Runs until stopped, with bounded idle backoff.

    ``max_iterations`` exists for deterministic tests and one-shot batch
    jobs.  Production callers normally provide a process-owned ``Event`` and
    stop it on SIGTERM; no in-memory counter is used for scheduling authority.
    Errors are counted and the loop continues after the same idle backoff so a
    transient database failure does not crash every worker process.
    ``tenant_id=None`` uses the durable cross-tenant fairness cursor; a value
    keeps the daemon pinned to one tenant for isolation or dedicated pools.
    """
    _validate_duration("lease", lease)
    _validate_duration("idle sleep", idle_sleep)
    _validate_heartbeat_interval(lease, heartbeat_interval)
    _validate_steps(max_steps)
    if max_iterations is not None and (type(max_iterations) is not int or max_iterations < 1):
        raise ContractViolation(ErrorCode.INVALID_INPUT, "max_iterations must be positive")
    event = stop_event or Event()
    iterations = claimed = completed = idle_polls = failures = 0
    while not event.is_set() and (max_iterations is None or iterations < max_iterations):
        iterations += 1
        try:
            result = run_next(
                database,
                tenant_id=tenant_id,
                owner=owner,
                lease=lease,
                max_steps=max_steps,
                heartbeat_interval=heartbeat_interval,
            )
            if result is None:
                idle_polls += 1
                event.wait(idle_sleep.total_seconds())
                continue
            claimed += 1
            completed += int(result.completed)
        except Exception as exc:
            failures += 1
            if on_error is not None:
                on_error(exc)
            event.wait(idle_sleep.total_seconds())
    return WorkerLoopResult(iterations, claimed, completed, idle_polls, failures)
