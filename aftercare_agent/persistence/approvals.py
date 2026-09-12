"""Transactional persistence for the Action approval gate."""

from datetime import UTC, datetime
from typing import Any, Literal, cast

import psycopg

from aftercare_agent.domain.approvals import (
    ApprovalRecord,
    ApprovalRequest,
    assert_approval_usable,
)
from aftercare_agent.domain.common import ContractViolation, ErrorCode

_APPROVAL_FIELDS = (
    "tenant_id",
    "case_id",
    "approval_id",
    "action_id",
    "action_parameters_sha256",
    "policy_version",
    "requested_by",
    "expires_at",
    "decision",
    "approver",
    "decision_idempotency_key",
    "decision_reason",
    "created_at",
    "decided_at",
    "updated_at",
)


def _record(row: tuple[Any, ...]) -> ApprovalRecord:
    return ApprovalRecord.model_validate(dict(zip(_APPROVAL_FIELDS, row, strict=False)))


class ApprovalRepository:
    """PostgreSQL implementation with fail-closed, idempotent decisions."""

    def get(
        self, conn: psycopg.Connection[Any], tenant_id: str, approval_id: str
    ) -> ApprovalRecord | None:
        row = conn.execute(
            "SELECT "
            + ",".join(_APPROVAL_FIELDS)
            + " FROM aftercare_approvals WHERE tenant_id=%s AND approval_id=%s",
            (tenant_id, approval_id),
        ).fetchone()
        return None if row is None else _record(row)

    @staticmethod
    def _db_now(conn: psycopg.Connection[Any]) -> datetime:
        row = conn.execute("SELECT clock_timestamp()").fetchone()
        assert row is not None
        return cast(datetime, row[0])

    @staticmethod
    def _lock_case(conn: psycopg.Connection[Any], tenant_id: str, case_id: str) -> None:
        if (
            conn.execute(
                "SELECT 1 FROM aftercare_cases WHERE tenant_id=%s AND case_id=%s FOR UPDATE",
                (tenant_id, case_id),
            ).fetchone()
            is None
        ):
            raise ContractViolation(ErrorCode.FORBIDDEN, "case not found")

    @staticmethod
    def _lock_action(
        conn: psycopg.Connection[Any], tenant_id: str, action_id: str
    ) -> tuple[Any, ...]:
        row = conn.execute(
            "SELECT case_id,parameters_sha256,approval_required FROM aftercare_actions "
            "WHERE tenant_id=%s AND action_id=%s FOR UPDATE",
            (tenant_id, action_id),
        ).fetchone()
        if row is None:
            raise ContractViolation(ErrorCode.FORBIDDEN, "action not found")
        return cast(tuple[Any, ...], row)

    def request(
        self, conn: psycopg.Connection[Any], request: ApprovalRequest
    ) -> tuple[ApprovalRecord, bool]:
        """Create one pending approval, or return an exact replay."""

        self._lock_case(conn, request.tenant_id, request.case_id)
        action = self._lock_action(conn, request.tenant_id, request.action_id)
        if action[0] != request.case_id:
            raise ContractViolation(ErrorCode.FORBIDDEN, "approval case does not match action")
        if action[1] != request.action_parameters_sha256:
            raise ContractViolation(ErrorCode.CONFLICT, "approval parameters do not match action")
        if not action[2]:
            raise ContractViolation(ErrorCode.CONFLICT, "action does not require approval")
        now = self._db_now(conn)
        if request.expires_at <= now:
            raise ContractViolation(
                ErrorCode.INVALID_INPUT, "approval expiry must be in the future"
            )
        inserted = conn.execute(
            "INSERT INTO aftercare_approvals(tenant_id,case_id,approval_id,action_id,"
            "action_parameters_sha256,policy_version,requested_by,expires_at,decision) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'PENDING') ON CONFLICT DO NOTHING RETURNING "
            + ",".join(_APPROVAL_FIELDS),
            (
                request.tenant_id,
                request.case_id,
                request.approval_id,
                request.action_id,
                request.action_parameters_sha256,
                request.policy_version,
                request.requested_by,
                request.expires_at,
            ),
        ).fetchone()
        if inserted is not None:
            return _record(inserted), False
        rows = conn.execute(
            "SELECT " + ",".join(_APPROVAL_FIELDS) + " FROM aftercare_approvals WHERE tenant_id=%s "
            "AND (approval_id=%s OR action_id=%s) FOR UPDATE",
            (request.tenant_id, request.approval_id, request.action_id),
        ).fetchall()
        if not rows:
            raise ContractViolation(ErrorCode.RETRYABLE, "approval request outcome is unknown")
        for row in rows:
            current = _record(row)
            if current.approval_id == request.approval_id:
                if current != ApprovalRecord(
                    **request.model_dump(),
                    decision="PENDING",
                    created_at=current.created_at,
                    decided_at=None,
                    updated_at=current.updated_at,
                ):
                    raise ContractViolation(ErrorCode.CONFLICT, "approval replay changed")
                return current, True
        raise ContractViolation(ErrorCode.CONFLICT, "action already has another approval")

    def decide(
        self,
        conn: psycopg.Connection[Any],
        tenant_id: str,
        approval_id: str,
        *,
        approver: str,
        decision: Literal["APPROVED", "REJECTED"],
        decision_idempotency_key: str,
        decision_reason: str | None = None,
    ) -> ApprovalRecord:
        """Record one decision; repeated identical clicks return the row."""

        initial = self.get(conn, tenant_id, approval_id)
        if initial is None:
            raise ContractViolation(ErrorCode.FORBIDDEN, "approval not found")
        self._lock_case(conn, tenant_id, initial.case_id)
        action = self._lock_action(conn, tenant_id, initial.action_id)
        row = conn.execute(
            "SELECT "
            + ",".join(_APPROVAL_FIELDS)
            + " FROM aftercare_approvals WHERE tenant_id=%s AND approval_id=%s FOR UPDATE",
            (tenant_id, approval_id),
        ).fetchone()
        if row is None:
            raise ContractViolation(ErrorCode.FORBIDDEN, "approval not found")
        current = _record(row)
        if current.case_id != action[0] or current.action_parameters_sha256 != action[1]:
            raise ContractViolation(ErrorCode.CONFLICT, "approval action binding changed")
        if current.decision != "PENDING":
            if (
                current.decision == decision
                and current.approver == approver
                and current.decision_idempotency_key == decision_idempotency_key
                and current.decision_reason == decision_reason
            ):
                return current
            raise ContractViolation(ErrorCode.CONFLICT, "approval decision replay changed")
        if current.requested_by == approver:
            raise ContractViolation(ErrorCode.FORBIDDEN, "requester cannot approve its own action")
        if self._db_now(conn) >= current.expires_at:
            raise ContractViolation(ErrorCode.FORBIDDEN, "approval has expired")
        updated = conn.execute(
            "UPDATE aftercare_approvals SET decision=%s,approver=%s,"
            "decision_idempotency_key=%s,decision_reason=%s,decided_at=clock_timestamp(),"
            "updated_at=clock_timestamp() WHERE tenant_id=%s AND approval_id=%s RETURNING "
            + ",".join(_APPROVAL_FIELDS),
            (
                decision,
                approver,
                decision_idempotency_key,
                decision_reason,
                tenant_id,
                approval_id,
            ),
        ).fetchone()
        assert updated is not None
        return _record(updated)

    def expire(
        self, conn: psycopg.Connection[Any], tenant_id: str, approval_id: str
    ) -> ApprovalRecord:
        """Lazily settle an expired pending request under the normal lock order.

        A periodic sweeper can call this method, but dispatch never relies on
        the sweeper: it checks the database clock again immediately before the
        external request.
        """

        initial = self.get(conn, tenant_id, approval_id)
        if initial is None:
            raise ContractViolation(ErrorCode.FORBIDDEN, "approval not found")
        self._lock_case(conn, tenant_id, initial.case_id)
        self._lock_action(conn, tenant_id, initial.action_id)
        row = conn.execute(
            "SELECT "
            + ",".join(_APPROVAL_FIELDS)
            + " FROM aftercare_approvals WHERE tenant_id=%s AND approval_id=%s FOR UPDATE",
            (tenant_id, approval_id),
        ).fetchone()
        if row is None:
            raise ContractViolation(ErrorCode.FORBIDDEN, "approval not found")
        current = _record(row)
        if current.decision != "PENDING" or self._db_now(conn) < current.expires_at:
            return current
        updated = conn.execute(
            "UPDATE aftercare_approvals SET decision='EXPIRED',"
            "approver='approval-expiry-reconciler',"
            "decision_idempotency_key=%s,decided_at=clock_timestamp(),"
            "updated_at=clock_timestamp() WHERE tenant_id=%s AND approval_id=%s RETURNING "
            + ",".join(_APPROVAL_FIELDS),
            (f"expiry:{approval_id}", tenant_id, approval_id),
        ).fetchone()
        assert updated is not None
        return _record(updated)

    def lock_for_dispatch(
        self,
        conn: psycopg.Connection[Any],
        *,
        tenant_id: str,
        case_id: str,
        action_id: str,
        approval_id: str,
        action_parameters_sha256: str,
        policy_version: str,
    ) -> ApprovalRecord:
        """Lock and verify the approval after Case→Run→Action locks."""

        row = conn.execute(
            "SELECT "
            + ",".join(_APPROVAL_FIELDS)
            + " FROM aftercare_approvals WHERE tenant_id=%s AND approval_id=%s FOR UPDATE",
            (tenant_id, approval_id),
        ).fetchone()
        if row is None:
            raise ContractViolation(ErrorCode.FORBIDDEN, "approval not found")
        approval = _record(row)
        assert_approval_usable(
            approval,
            tenant_id=tenant_id,
            case_id=case_id,
            action_id=action_id,
            action_parameters_sha256=action_parameters_sha256,
            policy_version=policy_version,
            db_now=self._db_now(conn).astimezone(UTC),
        )
        return approval


__all__ = ["ApprovalRepository"]
