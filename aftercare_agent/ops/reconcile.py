"""What a restore cannot undo: the external actions taken after the copy.

Recovering a database rolls back the record of an action, not the action.  A
refund the restored copy shows as CONFIRMED may have failed a minute later in
the database that was lost, and an UNKNOWN outcome stays unknown whether or not
a copy still says UNKNOWN.  This module turns that difference into a review
list: one row per Action whose stored outcome the restored copy cannot be
trusted to describe.

It performs no provider call and resolves nothing by itself.  UNKNOWN is a
durable outcome, so it is listed even when it predates the recovery point; an
operator reconciles it against the same Action, never by re-issuing the
obligation under a second key.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import psycopg

from aftercare_agent.actions.ledger import ActionState
from aftercare_agent.domain.common import Identifier, NonNegativeInt, UtcDatetime
from aftercare_agent.ops.tooling import OpsError, OpsModel

DEFAULT_SAFETY_MARGIN_SECONDS = 60.0
DEFAULT_RECONCILE_LIMIT = 200
MAX_RECONCILE_LIMIT = 1000

type ReconciliationReason = Literal["unknown_outcome", "changed_since_cut"]

_ACTION_COLUMNS = (
    "tenant_id",
    "case_id",
    "action_id",
    "action_type",
    "state",
    "provider_reference",
    "provider_idempotency_key",
    "updated_at",
)


class ReconciliationEntry(OpsModel):
    """One Action whose stored outcome a restore to this point cannot prove."""

    tenant_id: Identifier
    case_id: Identifier
    action_id: Identifier
    action_type: Identifier
    state: ActionState
    provider_reference: Identifier | None = None
    provider_idempotency_key: Identifier | None = None
    updated_at: UtcDatetime
    reason: ReconciliationReason


class ReconciliationReport(OpsModel):
    """The review list an operator works through after a recovery."""

    recovery_point: UtcDatetime
    cut_at: UtcDatetime
    safety_margin_seconds: float
    generated_at: UtcDatetime
    limit: NonNegativeInt
    entries: tuple[ReconciliationEntry, ...]

    @property
    def needs_review(self) -> bool:
        return bool(self.entries)

    @property
    def unknown_outcomes(self) -> tuple[ReconciliationEntry, ...]:
        return tuple(entry for entry in self.entries if entry.reason == "unknown_outcome")

    def table(self) -> str:
        lines = [
            f"reconciliation after {self.recovery_point.isoformat()}: "
            f"{len(self.entries)} action(s) to review "
            f"(cut {self.cut_at.isoformat()}, margin {self.safety_margin_seconds:g}s)"
        ]
        for entry in self.entries:
            lines.append(
                f"  [{entry.reason}] {entry.tenant_id}/{entry.case_id} {entry.action_id} "
                f"{entry.action_type} state={entry.state} updated={entry.updated_at.isoformat()}"
            )
        if not self.entries:
            lines.append("  no Action changed after the cut and none is UNKNOWN")
        elif len(self.entries) < self.limit:
            lines.append(
                "  the cut trails the recovery point by the margin, so a commit that "
                "raced the dump stays in this list"
            )
        elif len(self.entries) >= self.limit:
            lines.append(f"  list capped at {self.limit}; narrow it with --tenant-id")
        return "\n".join(lines)

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def classify(state: ActionState, updated_at: datetime, cut: datetime) -> ReconciliationReason:
    """Why an Action needs review after a restore to *cut*."""
    if state == "UNKNOWN":
        return "unknown_outcome"
    if updated_at > cut:
        return "changed_since_cut"
    raise OpsError("row is outside the reconciliation window")


def reconciliation_entry(row: Sequence[Any], *, cut: datetime) -> ReconciliationEntry:
    data = dict(zip(_ACTION_COLUMNS, row, strict=False))
    return ReconciliationEntry(**data, reason=classify(data["state"], data["updated_at"], cut))


def build_reconciliation(
    connection: psycopg.Connection[Any],
    *,
    recovery_point: datetime,
    safety_margin_seconds: float = DEFAULT_SAFETY_MARGIN_SECONDS,
    tenant_id: str | None = None,
    limit: int = DEFAULT_RECONCILE_LIMIT,
    generated_at: datetime | None = None,
) -> ReconciliationReport:
    """List the Actions a restore to *recovery_point* cannot be trusted to describe.

    The cut is deliberately earlier than the recovery point.  A transaction
    that committed while the dump was being taken may or may not be inside it,
    and a review list that is a minute too wide costs a minute of operator
    time, while one that is too narrow costs a duplicate external action.
    """
    if not 1 <= limit <= MAX_RECONCILE_LIMIT:
        raise OpsError(f"limit must be between 1 and {MAX_RECONCILE_LIMIT}")
    if safety_margin_seconds < 0:
        raise OpsError("safety margin cannot be negative")
    cut = recovery_point - timedelta(seconds=safety_margin_seconds)
    clauses = ["(state = 'UNKNOWN' OR updated_at > %(cut)s)"]
    parameters: dict[str, Any] = {"cut": cut, "limit": limit}
    if tenant_id is not None:
        clauses.append("tenant_id = %(tenant_id)s")
        parameters["tenant_id"] = tenant_id
    rows = connection.execute(
        "SELECT "
        + ",".join(_ACTION_COLUMNS)
        + " FROM aftercare_actions WHERE "
        + " AND ".join(clauses)
        + " ORDER BY updated_at DESC, action_id LIMIT %(limit)s",
        parameters,
    ).fetchall()
    return ReconciliationReport(
        recovery_point=recovery_point,
        cut_at=cut,
        safety_margin_seconds=safety_margin_seconds,
        generated_at=generated_at or datetime.now(UTC),
        limit=limit,
        entries=tuple(reconciliation_entry(row, cut=cut) for row in rows),
    )
