"""Runtime v1 records and pure guards; persistence must enforce them atomically."""

import hashlib
import json
from datetime import datetime
from typing import Literal, Self

from pydantic import Field, model_validator

from .common import (
    CaseScope,
    ContractModel,
    ContractViolation,
    ErrorCode,
    Identifier,
    NonNegativeInt,
    PositiveInt,
    RunScope,
    SchemaVersion,
    Sha256,
    UtcDatetime,
    require_same_case,
    utc,
)

type Permission = Literal["case:create", "case:read", "case:write", "run:cancel"]
type RunState = Literal[
    "READY",
    "RUNNING",
    "WAITING_INPUT",
    "WAITING_APPROVAL",
    "RETRY_AT",
    "REVIEW",
    "COMPLETED",
    "CANCELLED",
]


class CaseGrant(ContractModel):
    """Host-authenticated scope snapshot, never accepted as a request body."""

    subject_id: Identifier
    tenant_id: Identifier
    case_ids: tuple[Identifier, ...]
    permissions: tuple[Permission, ...]


def authorize_case(grant: CaseGrant, scope: CaseScope, permission: Permission) -> None:
    if (
        grant.tenant_id != scope.tenant_id
        or scope.case_id not in grant.case_ids
        or permission not in grant.permissions
    ):
        raise ContractViolation(ErrorCode.FORBIDDEN, "case access denied")


class OpenCaseInput(ContractModel):
    schema_version: SchemaVersion = 1
    order_id: Identifier
    channel: Literal["buyer", "support", "synthetic"]
    message_ref: Identifier
    message_sha256: Sha256
    goal: Literal["investigate_non_receipt"] = "investigate_non_receipt"


class AdmissionKey(ContractModel):
    tenant_id: Identifier
    entrypoint: Literal["case.open.v1"] = "case.open.v1"
    idempotency_key: Identifier


def admission_digest(key: AdmissionKey, body: OpenCaseInput) -> str:
    """Hash validated semantic fields; no trimming, case folding or Unicode NFC."""
    value = {
        "tenant_id": key.tenant_id,
        "entrypoint": key.entrypoint,
        "payload": body.model_dump(mode="json"),
    }
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def check_admission_replay(stored_digest: str, requested_digest: str) -> None:
    if stored_digest != requested_digest:
        raise ContractViolation(ErrorCode.CONFLICT, "idempotency key reused with different input")


class CaseRecord(CaseScope):
    schema_version: SchemaVersion = 1
    order_id: Identifier
    version: PositiveInt
    status: Literal["OPEN", "IN_REVIEW", "CLOSED"] = "OPEN"


class SessionRecord(CaseScope):
    schema_version: SchemaVersion = 1
    session_id: Identifier
    channel: Literal["buyer", "support", "synthetic"]


class RunRecord(RunScope):
    schema_version: SchemaVersion = 1
    session_id: Identifier | None = None
    predecessor_run_id: Identifier | None = None
    goal: Literal["investigate_non_receipt"] = "investigate_non_receipt"
    definition_version: Identifier
    input_version: PositiveInt
    state: RunState = "READY"
    fencing_token: NonNegativeInt = 0
    lease_owner: Identifier | None = None
    lease_until: UtcDatetime | None = None
    wait_id: Identifier | None = None
    wait_generation: PositiveInt | None = None
    available_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def coherent_state(self) -> Self:
        if self.predecessor_run_id == self.run_id:
            raise ValueError("a run cannot be its own predecessor")
        if self.state == "RUNNING":
            if not self.lease_owner or self.lease_until is None or self.fencing_token < 1:
                raise ValueError("RUNNING requires an owner, deadline and positive fence")
        elif self.lease_owner is not None or self.lease_until is not None:
            raise ValueError("only RUNNING may retain an execution lease")
        if self.state in ("WAITING_INPUT", "WAITING_APPROVAL"):
            if self.wait_id is None or self.wait_generation is None:
                raise ValueError("waiting requires its exact wait and generation")
        elif self.wait_id is not None or self.wait_generation is not None:
            raise ValueError("only a waiting run may bind an active wait")
        if (self.state == "RETRY_AT") != (self.available_at is not None):
            raise ValueError("only RETRY_AT requires available_at")
        return self


class StepRecord(RunScope):
    schema_version: SchemaVersion = 1
    step_id: Identifier
    kind: Literal["model", "tool", "evaluate"]
    input_version: PositiveInt


class AttemptRecord(RunScope):
    schema_version: SchemaVersion = 1
    step_id: Identifier
    attempt_id: Identifier
    attempt_number: PositiveInt
    fencing_token: PositiveInt
    status: Literal["STARTED", "SUCCEEDED", "FAILED", "UNKNOWN"]


def validate_hierarchy(
    case: CaseRecord,
    run: RunRecord,
    *,
    session: SessionRecord | None = None,
    step: StepRecord | None = None,
    attempt: AttemptRecord | None = None,
) -> None:
    """Caller supplies records read from trusted storage, not user-created objects."""
    require_same_case(case, run)
    if run.session_id is not None:
        if session is None or session.session_id != run.session_id:
            raise ContractViolation(ErrorCode.FORBIDDEN, "missing or mismatched session")
    elif session is not None:
        raise ContractViolation(ErrorCode.FORBIDDEN, "run has no bound session")
    if session is not None:
        require_same_case(case, session)
    if step is not None:
        require_same_case(case, step)
        if step.run_id != run.run_id:
            raise ContractViolation(ErrorCode.FORBIDDEN, "step belongs to another run")
    if attempt is not None:
        require_same_case(case, attempt)
        if step is None or attempt.run_id != run.run_id or attempt.step_id != step.step_id:
            raise ContractViolation(ErrorCode.FORBIDDEN, "attempt ancestry mismatch")


class ExecutionClaim(RunScope):
    owner: Identifier
    fencing_token: PositiveInt


def assert_execution_right(run: RunRecord, claim: ExecutionClaim, *, db_now: datetime) -> None:
    require_same_case(run, claim)
    if run.run_id != claim.run_id:
        raise ContractViolation(ErrorCode.FORBIDDEN, "execution claim belongs to another run")
    if (
        run.state != "RUNNING"
        or run.lease_owner != claim.owner
        or run.fencing_token != claim.fencing_token
        or run.lease_until is None
        or utc(db_now) >= run.lease_until
    ):
        raise ContractViolation(ErrorCode.LEASE_LOST, "execution right is no longer valid")


def next_fencing_token(run: RunRecord, *, db_now: datetime) -> int:
    now = utc(db_now)
    reclaimable = run.state == "RUNNING" and run.lease_until is not None and now >= run.lease_until
    if run.state != "READY" and not reclaimable:
        raise ContractViolation(ErrorCode.CONFLICT, "run is not claimable")
    if run.fencing_token == 2**63 - 1:
        raise ContractViolation(ErrorCode.CONFLICT, "fencing token exhausted")
    return run.fencing_token + 1


def assert_case_version(case: CaseRecord, expected_version: int) -> None:
    if type(expected_version) is not int or case.version != expected_version:
        raise ContractViolation(ErrorCode.CONFLICT, "case version changed")


_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    "READY": frozenset(("RUNNING", "CANCELLED")),
    "RUNNING": frozenset(
        (
            "READY",
            "WAITING_INPUT",
            "WAITING_APPROVAL",
            "RETRY_AT",
            "REVIEW",
            "COMPLETED",
            "CANCELLED",
        )
    ),
    "WAITING_INPUT": frozenset(("READY", "CANCELLED")),
    "WAITING_APPROVAL": frozenset(("READY", "CANCELLED")),
    "RETRY_AT": frozenset(("READY", "CANCELLED")),
    "REVIEW": frozenset(("READY", "CANCELLED")),
    "COMPLETED": frozenset(),
    "CANCELLED": frozenset(),
}


def validate_transition(previous: RunState, target: RunState) -> None:
    """Legal edge only; does not prove authorization, readiness or completion."""
    if target not in _TRANSITIONS.get(previous, frozenset()):
        raise ContractViolation(ErrorCode.CONFLICT, "illegal run transition")


class Failure(ContractModel):
    code: ErrorCode
    message: str = Field(min_length=1, max_length=500)
    retry_after_seconds: NonNegativeInt | None = None

    @model_validator(mode="after")
    def retry_hint(self) -> Self:
        if self.retry_after_seconds is not None and self.code not in (
            ErrorCode.RATE_LIMITED,
            ErrorCode.RETRYABLE,
        ):
            raise ValueError("this failure requires correction/reconciliation, not timed replay")
        return self
