"""A small durable Worker loop around the database-backed Run lease."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from aftercare_agent.domain.protocol import Checkpoint
from aftercare_agent.domain.runtime import ExecutionClaim
from aftercare_agent.persistence import CheckpointRepository, Database, RunRepository

from .harness import HarnessResult, run_fake_harness


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


def _execute_claim(
    database: Database,
    *,
    claim: ExecutionClaim,
    previous: Checkpoint | None,
    execution_time: datetime,
    max_steps: int,
) -> WorkerResult:
    result: HarnessResult = run_fake_harness(
        tenant_id=claim.tenant_id,
        case_id=claim.case_id,
        run_id=claim.run_id,
        checkpoint=previous,
        now=execution_time,
        max_steps=max_steps,
    )
    checkpoint = result.checkpoint.model_copy(update={"saved_fencing_token": claim.fencing_token})
    runs = RunRepository()
    checkpoints = CheckpointRepository()
    with database.transaction() as connection:
        checkpoints.save(connection, checkpoint, claim)
        # A bounded slice gives the execution right back so another Worker
        # can resume it without waiting for lease expiry.
        runs.transition(connection, claim, "COMPLETED" if result.completed else "READY")
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
) -> WorkerResult:
    """Claim one Run, execute a bounded Fake Harness slice, and persist it.

    The model/tool loop runs outside database transactions. A short final
    transaction checks the same fencing token before writing the checkpoint;
    only then is a completed Run transitioned to ``COMPLETED``.
    """
    execution_time = now or datetime.now(UTC)
    runs = RunRepository()
    checkpoints = CheckpointRepository()
    with database.transaction() as connection:
        claim = runs.claim(connection, tenant_id, run_id, owner, lease)
        previous = checkpoints.get_latest(connection, tenant_id, run_id)

    return _execute_claim(
        database,
        claim=claim,
        previous=previous,
        execution_time=execution_time,
        max_steps=max_steps,
    )


def run_next(
    database: Database,
    *,
    tenant_id: str,
    owner: str,
    now: datetime | None = None,
    lease: timedelta = timedelta(seconds=30),
    max_steps: int = 8,
) -> WorkerResult | None:
    """Claim and execute one runnable Run, or return ``None`` when idle."""
    execution_time = now or datetime.now(UTC)
    runs = RunRepository()
    checkpoints = CheckpointRepository()
    with database.transaction() as connection:
        claim = runs.claim_next(connection, tenant_id, owner, lease)
        if claim is None:
            return None
        previous = checkpoints.get_latest(connection, tenant_id, claim.run_id)
    return _execute_claim(
        database,
        claim=claim,
        previous=previous,
        execution_time=execution_time,
        max_steps=max_steps,
    )
