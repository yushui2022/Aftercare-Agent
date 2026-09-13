"""Waiting v1 rules shared by the Inbox and durable wake-up implementation."""

from datetime import datetime
from typing import Literal, Self

from pydantic import model_validator

from .common import (
    ContractViolation,
    ErrorCode,
    Identifier,
    PositiveInt,
    RunScope,
    SchemaVersion,
    UtcDatetime,
    require_same_case,
    utc,
)
from .protocol import ArtifactReference


class WaitRecord(RunScope):
    schema_version: SchemaVersion = 1
    wait_id: Identifier
    generation: PositiveInt
    kind: Literal["input", "approval"]
    correlation_key: Identifier
    condition_version: Identifier
    created_at: UtcDatetime
    deadline: UtcDatetime
    state: Literal["PENDING", "ACTIVE", "SATISFIED", "TIMED_OUT", "CANCELLED"]
    resolved_by_event_id: Identifier | None = None

    @model_validator(mode="after")
    def coherent_resolution(self) -> Self:
        if self.deadline <= self.created_at:
            raise ValueError("wait deadline must follow registration")
        if (self.state == "SATISFIED") != (self.resolved_by_event_id is not None):
            raise ValueError("only a satisfied wait requires the winning input event")
        return self


class InboxSignal(RunScope):
    """Authenticated, normalized input. Channel signatures are a later connector concern."""

    schema_version: SchemaVersion = 1
    event_id: Identifier
    source_id: Identifier
    source_event_id: Identifier
    wait_id: Identifier
    generation: PositiveInt
    kind: Literal["input", "approval"]
    correlation_key: Identifier
    condition_version: Identifier
    received_at: UtcDatetime
    payload: ArtifactReference

    @model_validator(mode="after")
    def payload_scope(self) -> Self:
        require_same_case(self, self.payload)
        return self


def matches_wait(wait: WaitRecord, signal: InboxSignal) -> bool:
    fields = (
        "tenant_id",
        "case_id",
        "run_id",
        "wait_id",
        "generation",
        "kind",
        "correlation_key",
        "condition_version",
    )
    return all(getattr(wait, field) == getattr(signal, field) for field in fields)


def resolution_candidate(
    wait: WaitRecord,
    *,
    current_generation: int,
    db_now: datetime,
    committed_reply: InboxSignal | None,
) -> Literal["satisfied", "timed_out", "none"]:
    """None reply must mean a fresh, authoritative no-match lookup under the write lock."""
    now = utc(db_now)
    if (
        type(current_generation) is not int
        or wait.generation != current_generation
        or wait.state != "ACTIVE"
        or now < wait.created_at
    ):
        return "none"
    if (
        committed_reply is not None
        and matches_wait(wait, committed_reply)
        and wait.created_at <= committed_reply.received_at <= now
    ):
        # Deadline enables timeout; it is not a strict reply cutoff in v1.
        return "satisfied"
    return "timed_out" if now >= wait.deadline else "none"


def inbox_dedup_key(signal: InboxSignal) -> tuple[str, str, str]:
    return signal.tenant_id, signal.source_id, signal.source_event_id


def check_inbox_replay(stored: InboxSignal, incoming: InboxSignal) -> None:
    if stored != incoming:
        raise ContractViolation(
            ErrorCode.CONFLICT, "input replay changed identity, content or time"
        )


def wakeup_key(wait: WaitRecord) -> tuple[str, str, str, str, int]:
    return wait.tenant_id, wait.case_id, wait.run_id, wait.wait_id, wait.generation
