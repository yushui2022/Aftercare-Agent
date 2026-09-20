"""Transactional Action Ledger repository.

The repository records an external obligation before any provider call.  It
does not invoke a provider and it never treats a lost response as permission
to create a second Action.  Provider dispatchers must call the state methods
with a current Run claim, keeping external I/O outside the transaction.
"""

from typing import Any, Literal

import psycopg

from aftercare_agent.actions.ledger import (
    ActionIntent,
    ActionRecord,
    ActionReservation,
    ActionState,
    ProviderReceipt,
    validate_provider_receipt,
)
from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.runtime import ExecutionClaim

from .approvals import ApprovalRepository

_ACTION_FIELDS = (
    "tenant_id",
    "case_id",
    "order_id",
    "action_id",
    "action_type",
    "business_key",
    "idempotency_key",
    "parameters_sha256",
    "amount_minor",
    "currency",
    "provider_idempotency_key",
    "approval_required",
    "state",
    "provider_reference",
    "result_sha256",
    "failure_code",
    "fencing_token",
    "created_at",
    "updated_at",
)

_RESULT_STATE: frozenset[str] = frozenset(("UNKNOWN", "CONFIRMED", "FAILED"))
_TRANSITIONS: dict[str, frozenset[str]] = {
    "RESERVED": frozenset(("REQUESTED", "FAILED")),
    "REQUESTED": frozenset(("UNKNOWN", "CONFIRMED", "FAILED")),
    "UNKNOWN": frozenset(("CONFIRMED", "FAILED")),
    "CONFIRMED": frozenset(),
    "FAILED": frozenset(),
}


def _record(row: tuple[Any, ...]) -> ActionRecord:
    data = dict(zip(_ACTION_FIELDS, row, strict=False))
    # psycopg maps NUMERIC to Decimal.  Keep the public amount representation
    # as a canonical minor-unit string, avoiding floating point arithmetic.
    data["amount_minor"] = str(data["amount_minor"])
    return ActionRecord.model_validate(data)


class ActionRepository:
    """PostgreSQL implementation of the idempotent Action Ledger."""

    @staticmethod
    def _claim_valid(conn: psycopg.Connection[Any], claim: ExecutionClaim) -> None:
        """Lock Case then Run and verify the current lease/fence."""
        case = conn.execute(
            "SELECT 1 FROM aftercare_cases WHERE tenant_id=%s AND case_id=%s FOR UPDATE",
            (claim.tenant_id, claim.case_id),
        ).fetchone()
        if case is None:
            raise ContractViolation(ErrorCode.FORBIDDEN, "case not found")
        run = conn.execute(
            "SELECT state,lease_owner,fencing_token,lease_until FROM aftercare_runs "
            "WHERE tenant_id=%s AND case_id=%s AND run_id=%s FOR UPDATE",
            (claim.tenant_id, claim.case_id, claim.run_id),
        ).fetchone()
        if (
            run is None
            or run[0] != "RUNNING"
            or run[1] != claim.owner
            or int(run[2]) != claim.fencing_token
        ):
            raise ContractViolation(ErrorCode.LEASE_LOST, "execution lease lost")
        if (
            conn.execute(
                "SELECT 1 FROM aftercare_runs WHERE tenant_id=%s AND case_id=%s AND run_id=%s "
                "AND lease_until > clock_timestamp()",
                (claim.tenant_id, claim.case_id, claim.run_id),
            ).fetchone()
            is None
        ):
            raise ContractViolation(ErrorCode.LEASE_LOST, "execution lease lost")

    @staticmethod
    def _assert_claim_scope(intent: ActionIntent, claim: ExecutionClaim | None) -> None:
        if claim is not None and (intent.tenant_id, intent.case_id) != (
            claim.tenant_id,
            claim.case_id,
        ):
            raise ContractViolation(ErrorCode.FORBIDDEN, "action scope does not match claim")

    def get(
        self, conn: psycopg.Connection[Any], tenant_id: str, action_id: str
    ) -> ActionRecord | None:
        row = conn.execute(
            "SELECT "
            + ",".join(_ACTION_FIELDS)
            + " FROM aftercare_actions WHERE tenant_id=%s AND action_id=%s",
            (tenant_id, action_id),
        ).fetchone()
        return None if row is None else _record(row)

    def reserve(
        self,
        conn: psycopg.Connection[Any],
        intent: ActionIntent,
        *,
        claim: ExecutionClaim | None = None,
    ) -> ActionReservation:
        """Insert a RESERVED action or return a semantically equal replay.

        A repeated business key is intentionally deduplicated even when it
        comes from another Case.  Reusing a key with changed financial or
        provider parameters is a hard conflict, not a new Action.
        """
        self._assert_claim_scope(intent, claim)
        if claim is not None:
            self._claim_valid(conn, claim)
        inserted = conn.execute(
            "INSERT INTO aftercare_actions(tenant_id,case_id,order_id,action_id,action_type,"
            "business_key,idempotency_key,parameters_sha256,amount_minor,currency,"
            "provider_idempotency_key,approval_required,state) VALUES "
            "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'RESERVED') "
            "ON CONFLICT DO NOTHING RETURNING " + ",".join(_ACTION_FIELDS),
            (
                intent.tenant_id,
                intent.case_id,
                intent.order_id,
                intent.action_id,
                intent.action_type,
                intent.business_key,
                intent.idempotency_key,
                intent.parameters_sha256,
                intent.amount_minor,
                intent.currency,
                intent.provider_idempotency_key,
                intent.approval_required,
            ),
        ).fetchone()
        if inserted is not None:
            return ActionReservation(_record(inserted), False)

        # Unique-key conflicts are resolved while locked so a concurrent
        # reservation cannot be mistaken for a missing/retryable outcome.
        rows = conn.execute(
            "SELECT " + ",".join(_ACTION_FIELDS) + " FROM aftercare_actions WHERE tenant_id=%s AND "
            "(action_id=%s OR business_key=%s OR idempotency_key=%s) FOR UPDATE",
            (intent.tenant_id, intent.action_id, intent.business_key, intent.idempotency_key),
        ).fetchall()
        if not rows:
            raise ContractViolation(ErrorCode.RETRYABLE, "action reservation outcome is unknown")
        candidates = [_record(row) for row in rows]
        # An idempotency key belongs to exactly one request shape, including
        # the caller-selected action id.  It must not silently alias another.
        for current in candidates:
            if current.idempotency_key == intent.idempotency_key:
                if current.action_id != intent.action_id or not self._same_parameters(
                    current, intent
                ):
                    raise ContractViolation(
                        ErrorCode.CONFLICT,
                        "action idempotency key reused with different input",
                    )
                return ActionReservation(current, True)
        for current in candidates:
            if current.action_id == intent.action_id and not self._same_parameters(current, intent):
                raise ContractViolation(ErrorCode.CONFLICT, "action id reused with different input")
        for current in candidates:
            if current.business_key == intent.business_key:
                if not self._same_parameters(current, intent):
                    raise ContractViolation(
                        ErrorCode.CONFLICT, "business key reused with different input"
                    )
                return ActionReservation(current, True)
        raise ContractViolation(ErrorCode.CONFLICT, "action identity conflicts")

    @staticmethod
    def _same_parameters(current: ActionRecord, intent: ActionIntent) -> bool:
        return (
            current.order_id == intent.order_id
            and current.action_type == intent.action_type
            and current.business_key == intent.business_key
            and current.parameters_sha256 == intent.parameters_sha256
            and current.amount_minor == intent.amount_minor
            and current.currency == intent.currency
            and current.provider_idempotency_key == intent.provider_idempotency_key
            and current.approval_required == intent.approval_required
        )

    def _locked(
        self, conn: psycopg.Connection[Any], tenant_id: str, action_id: str
    ) -> ActionRecord:
        row = conn.execute(
            "SELECT "
            + ",".join(_ACTION_FIELDS)
            + " FROM aftercare_actions WHERE tenant_id=%s AND action_id=%s FOR UPDATE",
            (tenant_id, action_id),
        ).fetchone()
        if row is None:
            raise ContractViolation(ErrorCode.FORBIDDEN, "action not found")
        return _record(row)

    def mark_requested(
        self,
        conn: psycopg.Connection[Any],
        action_id: str,
        claim: ExecutionClaim,
        *,
        approval_id: str | None = None,
        policy_version: str | None = None,
    ) -> ActionRecord:
        """Mark RESERVED as REQUESTED under the current Run fence."""
        self._claim_valid(conn, claim)
        current = self._locked(conn, claim.tenant_id, action_id)
        if current.case_id != claim.case_id:
            raise ContractViolation(ErrorCode.FORBIDDEN, "action scope does not match claim")
        if current.state == "RESERVED":
            if current.approval_required:
                if approval_id is None:
                    raise ContractViolation(ErrorCode.FORBIDDEN, "action approval is required")
                if policy_version is None:
                    raise ContractViolation(ErrorCode.INVALID_INPUT, "approval policy is required")
                ApprovalRepository().lock_for_dispatch(
                    conn,
                    tenant_id=current.tenant_id,
                    case_id=current.case_id,
                    action_id=current.action_id,
                    approval_id=approval_id,
                    action_parameters_sha256=current.parameters_sha256,
                    policy_version=policy_version,
                )
            elif approval_id is not None:
                raise ContractViolation(
                    ErrorCode.INVALID_INPUT, "approval supplied for exempt action"
                )
            return self._update_state(conn, current, "REQUESTED", claim=claim)
        if current.state in ("REQUESTED", "UNKNOWN", "CONFIRMED", "FAILED"):
            return current
        raise ContractViolation(ErrorCode.CONFLICT, "invalid action state")

    def mark_result(
        self,
        conn: psycopg.Connection[Any],
        action_id: str,
        claim: ExecutionClaim,
        *,
        state: Literal["UNKNOWN", "CONFIRMED", "FAILED"],
        provider_reference: str | None = None,
        result_sha256: str | None = None,
        failure_code: str | None = None,
    ) -> ActionRecord:
        """Record provider outcome while fencing stale Run owners.

        ``UNKNOWN`` is a durable outcome: it continues to occupy any future
        business allowance and must be reconciled against the same Action.
        """
        if state not in _RESULT_STATE:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "invalid action result state")
        if state == "CONFIRMED" and (not provider_reference or not result_sha256):
            raise ContractViolation(
                ErrorCode.INVALID_INPUT, "confirmed result needs provider reference and digest"
            )
        if state == "FAILED" and not failure_code:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "failed result needs failure code")
        if state != "CONFIRMED" and result_sha256 is not None:
            raise ContractViolation(
                ErrorCode.INVALID_INPUT, "result digest is only valid for confirmation"
            )
        self._claim_valid(conn, claim)
        current = self._locked(conn, claim.tenant_id, action_id)
        if current.case_id != claim.case_id:
            raise ContractViolation(ErrorCode.FORBIDDEN, "action scope does not match claim")
        if current.state == state:
            identity = (current.provider_reference, current.result_sha256, current.failure_code)
            requested = (provider_reference, result_sha256, failure_code)
            if identity != requested:
                raise ContractViolation(ErrorCode.CONFLICT, "action result replay changed")
            return current
        if state not in _TRANSITIONS[current.state]:
            raise ContractViolation(ErrorCode.CONFLICT, "illegal action state transition")
        return self._update_state(
            conn,
            current,
            state,
            claim=claim,
            provider_reference=provider_reference,
            result_sha256=result_sha256,
            failure_code=failure_code,
        )

    def mark_receipt(
        self,
        conn: psycopg.Connection[Any],
        receipt: ProviderReceipt,
        claim: ExecutionClaim,
    ) -> ActionRecord:
        """Persist a provider-neutral receipt under the current Run fence.

        Provider I/O must happen before this call and outside the database
        transaction.  The locked Action is checked against the receipt before
        delegating to the existing state-transition guard, so a receipt from a
        different Action or provider idempotency key cannot be attached.
        """
        self._claim_valid(conn, claim)
        current = self._locked(conn, claim.tenant_id, receipt.action_id)
        if current.case_id != claim.case_id:
            raise ContractViolation(ErrorCode.FORBIDDEN, "action scope does not match claim")
        validate_provider_receipt(current, receipt)
        return self.mark_result(
            conn,
            receipt.action_id,
            claim,
            state=receipt.state,
            provider_reference=receipt.provider_reference,
            result_sha256=receipt.result_sha256,
            failure_code=receipt.failure_code,
        )

    def _update_state(
        self,
        conn: psycopg.Connection[Any],
        current: ActionRecord,
        state: ActionState,
        *,
        claim: ExecutionClaim,
        provider_reference: str | None = None,
        result_sha256: str | None = None,
        failure_code: str | None = None,
    ) -> ActionRecord:
        row = conn.execute(
            "UPDATE aftercare_actions SET state=%s,provider_reference=%s,result_sha256=%s,"
            "failure_code=%s,fencing_token=%s,updated_at=clock_timestamp() "
            "WHERE tenant_id=%s AND action_id=%s RETURNING " + ",".join(_ACTION_FIELDS),
            (
                state,
                provider_reference,
                result_sha256,
                failure_code,
                claim.fencing_token,
                current.tenant_id,
                current.action_id,
            ),
        ).fetchone()
        assert row is not None
        return _record(row)


__all__ = ["ActionRepository"]
