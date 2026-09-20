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
from time import monotonic
from typing import Any, Literal

import psycopg

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.investigation import parse_investigation_proposal
from aftercare_agent.domain.protocol import Checkpoint
from aftercare_agent.domain.runtime import ExecutionClaim
from aftercare_agent.persistence import (
    AdmissionRepository,
    CaseRepository,
    CheckpointRepository,
    Database,
    InvestigationAssessmentRepository,
    RunRepository,
    SlotReservation,
    set_transaction_context,
)

from .harness import HarnessResult
from .harness import run_fake_harness as run_fake_harness
from .judgment import JudgmentGate

# One bounded execution slice.  The default is the deterministic fake Harness;
# a caller that owns a provider adapter, a tool executor and a budget injects
# ``functools.partial`` of another one instead.  The Worker builds neither, so
# it never holds credentials or business connectors itself.
type Harness = Callable[..., HarnessResult]
# Kept as a compatibility alias for integrations written against the original
# name.  New provider adapters should depend on the provider-neutral gate.
type AssessmentEvaluator = JudgmentGate

__all__ = [
    "LeaseHeartbeat",
    "WorkerLoopResult",
    "WorkerResult",
    "Harness",
    "JudgmentGate",
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
    """Renew one claim on the heartbeat's own connection until the slice ends.

    Heartbeats do not grant execution rights and do not hide a lost lease.  A
    database error or fencing rejection stops the heartbeat; the caller then
    refuses to save the slice and lets the normal fenced write report the
    authoritative error.

    The renewing thread takes one connection for its whole life and releases it
    before returning, so the connection is never shared across threads while a
    renewal still costs one round trip instead of a fresh TCP and
    authentication handshake.  That matters because the renewal has to land
    inside a window derived from the lease: a handshake timed from the same
    budget made short leases unrenewable on hosts where connecting is slow.
    The heartbeat holds one pool slot for the length of the slice, so the pool
    it borrows from has to be sized with that room.
    A connection that fails is not retried for the same reason a lost lease
    is not: the slice must not keep going on a liveness signal nobody can
    still prove.
    """

    def __init__(
        self,
        database: Database,
        claim: ExecutionClaim,
        lease: timedelta,
        *,
        slot: SlotReservation | None = None,
        interval: timedelta | None = None,
    ) -> None:
        _validate_duration("lease", lease)
        selected = interval or lease / 3
        _validate_duration("heartbeat interval", selected)
        _validate_heartbeat_interval(lease, selected)
        self._database = database
        self._claim = claim
        self._lease = lease
        # ``None`` means no admission limit was configured when this slice
        # was claimed.  When a reservation exists, losing it is a capacity
        # safety failure and must stop the slice rather than silently letting
        # the Run lease continue without a slot.
        self._slot = slot
        self._interval = selected
        self._stop = Event()
        self._lock = Lock()
        self._failure: Exception | None = None
        self._thread: Thread | None = None
        self._connection: psycopg.Connection[Any] | None = None

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
        try:
            self._renew_until_stopped()
        finally:
            self._close()

    def _renew_until_stopped(self) -> None:
        seconds = self._interval.total_seconds()
        # Renew once before the first wait.  The lease the claim already
        # holds is the tightest window this thread ever has to hit, and it is
        # the only one that has to absorb opening the connection; sleeping
        # through an interval first would hand a third of that window to a
        # timer for nothing.
        deadline = monotonic()
        while True:
            if self._stop.wait(max(deadline - monotonic(), 0.0)):
                return
            try:
                self._renew()
            except Exception as exc:
                with self._lock:
                    self._failure = exc
                self._stop.set()
                return
            # Schedule from where this tick started, not from where it ended,
            # so a slow round trip cannot push every later renewal further
            # behind the lease it exists to keep alive.
            deadline += seconds

    def _renew(self) -> None:
        connection = self._connection
        if connection is None:
            connection = self._connection = self._database.open()
        with connection.transaction():
            # Lightweight unit-test doubles model only the transaction
            # boundary; real psycopg connections always expose ``execute``.
            # Keep the production RLS binding while allowing those doubles to
            # exercise lease semantics without pretending to be SQL drivers.
            if hasattr(connection, "execute"):
                set_transaction_context(
                    connection,
                    tenant_id=self._claim.tenant_id,
                    subject_id=self._claim.owner,
                )
            RunRepository().renew(connection, self._claim, self._lease)
            if self._slot is not None:
                AdmissionRepository().renew_slot(connection, self._claim, self._lease)

    def _close(self) -> None:
        connection, self._connection = self._connection, None
        if connection is None:
            return
        try:
            # Back to the pool, not destroyed: the next slice can reuse the
            # handshake this one already paid for.
            self._database.release(connection)
        except Exception:
            # The slice's own fenced write decides the outcome, so a failure
            # while giving back a connection that is about to disappear must
            # not be reported as a lost lease.
            pass


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


# A slice's last phase, mapped to the Run state that makes it actionable.
# ``complete`` and the two routes are terminal for a slice; any other phase
# means the slice hit ``max_steps`` and gives its execution right back.
_RUN_STATE_FOR_STEP: dict[str, str] = {
    "complete": "COMPLETED",
    "review": "REVIEW",
    "retry": "RETRY_AT",
}


def _run_state_for(checkpoint: Checkpoint) -> str:
    """Return the Run state to record for the checkpoint a slice just produced."""
    target = _RUN_STATE_FOR_STEP.get(checkpoint.next_step, "READY")
    if target == "RETRY_AT" and checkpoint.available_at is None:
        # The checkpoint invariant already refuses this combination.  The guard
        # is here so the slice fails before saving a checkpoint that no
        # transition could accept.
        raise ContractViolation(ErrorCode.INVALID_INPUT, "retry route lost its available_at")
    return target


def _execute_claim(
    database: Database,
    *,
    claim: ExecutionClaim,
    previous: Checkpoint | None,
    execution_time: datetime,
    max_steps: int,
    lease: timedelta,
    harness: Harness | None = None,
    assessment_evaluator: AssessmentEvaluator | None = None,
    heartbeat_interval: timedelta | None = None,
    slot: SlotReservation | None = None,
    resume_next_step: Literal["model", "tool", "evaluate"] | None = None,
) -> WorkerResult:
    _validate_steps(max_steps)
    # Resolved here rather than as a default argument so that replacing the
    # module-level name still takes effect, as tests and embedders expect.
    selected_harness = harness or run_fake_harness
    heartbeat = LeaseHeartbeat(
        database,
        claim,
        lease,
        slot=slot,
        interval=heartbeat_interval,
    )
    heartbeat.start()
    try:
        # A wait is settled by the trusted Inbox/Approval path before the Run
        # becomes READY again.  The checkpoint intentionally keeps the wait
        # identity for audit/recovery; once a fresh claim is acquired, clear
        # that execution marker before handing control back to the Harness.
        # This makes WAITING_INPUT/WAITING_APPROVAL a durable boundary rather
        # than an in-memory branch in the worker loop.
        if previous is not None and previous.next_step == "wait":
            persisted_resume_step = previous.resume_next_step
            if (
                persisted_resume_step is not None
                and resume_next_step is not None
                and persisted_resume_step != resume_next_step
            ):
                raise ContractViolation(
                    ErrorCode.CONFLICT,
                    "resume phase disagrees with the wait checkpoint",
                )
            selected_resume_step = resume_next_step or persisted_resume_step
            if selected_resume_step is None:
                raise ContractViolation(
                    ErrorCode.CONFLICT, "wait checkpoint needs an explicit resume step"
                )
            previous = previous.model_copy(
                update={
                    "checkpoint_version": previous.checkpoint_version + 1,
                    # The wait is registered after a bounded step.  Resume
                    # the exact pending phase; do not spend a second model
                    # budget just because the Run was parked.
                    "next_step": selected_resume_step,
                    "resume_next_step": None,
                    "wait_id": None,
                    "wait_generation": None,
                }
            )
        result: HarnessResult = selected_harness(
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
    with database.transaction(tenant_id=claim.tenant_id, subject_id=claim.owner) as connection:
        # Keep the repository-wide Case→Run lock order.  Without this, saving
        # a checkpoint would lock Run first and transition() would later lock
        # Case, allowing a concurrent Case-scoped writer to form a deadlock.
        CaseRepository().lock_order_id(connection, claim.tenant_id, claim.case_id)
        if result.proposal is not None:
            if checkpoint.next_step != "evaluate":
                raise ContractViolation(
                    ErrorCode.CONFLICT, "proposal result is not in the evaluate phase"
                )
            payload = checkpoint.pending_proposal_json
            if payload is None or parse_investigation_proposal(payload) != result.proposal:
                raise ContractViolation(
                    ErrorCode.CONFLICT, "proposal result changed from its checkpoint"
                )
            if assessment_evaluator is None:
                checkpoint = checkpoint.model_copy(
                    update={
                        "checkpoint_version": checkpoint.checkpoint_version + 1,
                        "next_step": "review",
                        "route_reason": "assessment_rejected",
                        "pending_proposal_json": None,
                    }
                )
            else:
                try:
                    assessment = assessment_evaluator(
                        connection, checkpoint, result.proposal, execution_time
                    )
                except ContractViolation:
                    checkpoint = checkpoint.model_copy(
                        update={
                            "checkpoint_version": checkpoint.checkpoint_version + 1,
                            "next_step": "review",
                            "route_reason": "assessment_rejected",
                            "pending_proposal_json": None,
                        }
                    )
                else:
                    InvestigationAssessmentRepository().put(
                        connection,
                        tenant_id=claim.tenant_id,
                        case_id=claim.case_id,
                        run_id=claim.run_id,
                        assessment=assessment,
                    )
                    checkpoint = checkpoint.model_copy(
                        update={
                            "checkpoint_version": checkpoint.checkpoint_version + 1,
                            "next_step": "complete",
                            "route_reason": None,
                            "pending_proposal_json": None,
                        }
                    )
        target = _run_state_for(checkpoint)
        checkpoints.save(connection, checkpoint, claim)
        # The checkpoint decides where the Run goes, not the HarnessResult: a
        # bounded slice that ran out of steps is READY again, while a slice
        # that stopped because the model may not spend more, or because the
        # provider refused, must land somewhere a human can see it instead of
        # sitting in RUNNING until the lease expires.
        runs.transition(
            connection,
            claim,
            target,
            available_at=checkpoint.available_at if target == "RETRY_AT" else None,
        )
        if slot is not None:
            AdmissionRepository().release_slot(connection, claim)
    return WorkerResult(
        tenant_id=claim.tenant_id,
        case_id=claim.case_id,
        run_id=claim.run_id,
        owner=claim.owner,
        fencing_token=claim.fencing_token,
        checkpoint=checkpoint,
        completed=checkpoint.next_step == "complete",
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
    resume_next_step: Literal["model", "tool", "evaluate"] | None = None,
    harness: Harness | None = None,
    assessment_evaluator: AssessmentEvaluator | None = None,
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
    with database.transaction(tenant_id=tenant_id, subject_id=owner) as connection:
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
        resume_next_step=resume_next_step,
        harness=harness,
        assessment_evaluator=assessment_evaluator,
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
    resume_next_step: Literal["model", "tool", "evaluate"] | None = None,
    harness: Harness | None = None,
    assessment_evaluator: AssessmentEvaluator | None = None,
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
    # A tenant-pinned worker can establish the RLS context before the queue
    # query.  A cross-tenant dispatcher intentionally leaves it unset and is
    # only safe with a dedicated dispatch mechanism/role (see ADR-0015).
    transaction = (
        database.transaction(tenant_id=tenant_id, subject_id=owner)
        if tenant_id is not None
        else database.transaction()
    )
    with transaction as connection:
        claim = runs.claim_next(connection, tenant_id, owner, lease)
        if claim is None:
            return None
        # Queue dispatch is intentionally cross-tenant for fairness.  Once a
        # claim is locked, bind the rest of this transaction to its tenant
        # before touching tenant-scoped state.
        set_transaction_context(connection, tenant_id=claim.tenant_id, subject_id=claim.owner)
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
        resume_next_step=resume_next_step,
        harness=harness,
        assessment_evaluator=assessment_evaluator,
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
    resume_next_step: Literal["model", "tool", "evaluate"] | None = None,
    harness: Harness | None = None,
    assessment_evaluator: AssessmentEvaluator | None = None,
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
                resume_next_step=resume_next_step,
                harness=harness,
                assessment_evaluator=assessment_evaluator,
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
