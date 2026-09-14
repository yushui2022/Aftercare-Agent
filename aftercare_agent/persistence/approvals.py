"""Transactional persistence for the Action approval gate."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal, cast

import psycopg

from aftercare_agent.domain.approvals import (
    ApprovalRecord,
    ApprovalRequest,
    assert_approval_usable,
)
from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.protocol import ArtifactReference
from aftercare_agent.domain.waits import InboxSignal, WaitRecord

from .gate_events import append_approval_decided, append_approval_requested
from .waits import WaitRepository

_APPROVAL_FIELDS = (
    "tenant_id",
    "case_id",
    "approval_id",
    "action_id",
    "run_id",
    "wait_id",
    "wait_generation",
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

_WAIT_FIELDS = (
    "tenant_id",
    "case_id",
    "run_id",
    "wait_id",
    "generation",
    "kind",
    "correlation_key",
    "condition_version",
    "created_at",
    "deadline",
    "state",
    "resolved_by_event_id",
)


def _record(row: tuple[Any, ...]) -> ApprovalRecord:
    return ApprovalRecord.model_validate(dict(zip(_APPROVAL_FIELDS, row, strict=False)))


def _wait(row: tuple[Any, ...]) -> WaitRecord:
    return WaitRecord.model_validate(dict(zip(_WAIT_FIELDS, row, strict=False)))


def _decision_payload_sha256(
    *, approval_id: str, decision: str, decision_key: str, reason: str | None
) -> str:
    encoded = json.dumps(
        {
            "approval_id": approval_id,
            "decision": decision,
            "decision_idempotency_key": decision_key,
            "decision_reason": reason,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


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

    def list_for_case(
        self,
        conn: psycopg.Connection[Any],
        tenant_id: str,
        case_id: str,
        *,
        limit: int = 50,
    ) -> list[ApprovalRecord]:
        """List one Case's approvals, newest first, without granting authority."""
        rows = conn.execute(
            "SELECT " + ",".join(_APPROVAL_FIELDS) + " FROM aftercare_approvals "
            "WHERE tenant_id=%s AND case_id=%s "
            "ORDER BY created_at DESC, approval_id DESC LIMIT %s",
            (tenant_id, case_id, limit),
        ).fetchall()
        return [_record(row) for row in rows]

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

    def _lock_run_wait(
        self,
        conn: psycopg.Connection[Any],
        *,
        tenant_id: str,
        case_id: str,
        run_id: str,
        wait_id: str,
        wait_generation: int,
        approval_id: str,
        action_parameters_sha256: str,
    ) -> tuple[WaitRecord, tuple[Any, ...]]:
        run = conn.execute(
            "SELECT state,wait_id,wait_generation FROM aftercare_runs "
            "WHERE tenant_id=%s AND case_id=%s AND run_id=%s FOR UPDATE",
            (tenant_id, case_id, run_id),
        ).fetchone()
        if run is None:
            raise ContractViolation(ErrorCode.FORBIDDEN, "run not found")
        if run[0] != "WAITING_APPROVAL" or run[1:] != (wait_id, wait_generation):
            raise ContractViolation(ErrorCode.CONFLICT, "run is not waiting for this approval")
        row = conn.execute(
            "SELECT " + ",".join(_WAIT_FIELDS) + " FROM aftercare_waits "
            "WHERE tenant_id=%s AND case_id=%s AND run_id=%s AND wait_id=%s "
            "AND generation=%s FOR UPDATE",
            (tenant_id, case_id, run_id, wait_id, wait_generation),
        ).fetchone()
        if row is None:
            raise ContractViolation(ErrorCode.FORBIDDEN, "approval wait not found")
        wait = _wait(row)
        if wait.kind != "approval" or wait.state not in ("PENDING", "ACTIVE"):
            raise ContractViolation(ErrorCode.CONFLICT, "approval wait is no longer active")
        if (
            wait.correlation_key != approval_id
            or wait.condition_version != action_parameters_sha256
        ):
            raise ContractViolation(ErrorCode.CONFLICT, "approval wait binding changed")
        return wait, cast(tuple[Any, ...], run)

    def request(
        self, conn: psycopg.Connection[Any], request: ApprovalRequest
    ) -> tuple[ApprovalRecord, bool]:
        """Create one pending approval, or return an exact replay."""

        self._lock_case(conn, request.tenant_id, request.case_id)
        existing = self.get(conn, request.tenant_id, request.approval_id)
        if existing is not None:
            action = self._lock_action(conn, request.tenant_id, request.action_id)
            if action[0] != request.case_id:
                raise ContractViolation(ErrorCode.FORBIDDEN, "approval case does not match action")
            if action[1] != request.action_parameters_sha256:
                raise ContractViolation(
                    ErrorCode.CONFLICT, "approval parameters do not match action"
                )
            if not self._same_request(existing, request):
                raise ContractViolation(ErrorCode.CONFLICT, "approval replay changed")
            return existing, True
        if request.run_id is not None:
            assert request.wait_id is not None and request.wait_generation is not None
            self._lock_run_wait(
                conn,
                tenant_id=request.tenant_id,
                case_id=request.case_id,
                run_id=request.run_id,
                wait_id=request.wait_id,
                wait_generation=request.wait_generation,
                approval_id=request.approval_id,
                action_parameters_sha256=request.action_parameters_sha256,
            )
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
            "run_id,wait_id,wait_generation,action_parameters_sha256,policy_version,"
            "requested_by,expires_at,decision) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING') "
            "ON CONFLICT DO NOTHING RETURNING " + ",".join(_APPROVAL_FIELDS),
            (
                request.tenant_id,
                request.case_id,
                request.approval_id,
                request.action_id,
                request.run_id,
                request.wait_id,
                request.wait_generation,
                request.action_parameters_sha256,
                request.policy_version,
                request.requested_by,
                request.expires_at,
            ),
        ).fetchone()
        if inserted is not None:
            result = _record(inserted)
            append_approval_requested(conn, result)
            return result, False
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

    @staticmethod
    def _same_request(current: ApprovalRecord, request: ApprovalRequest) -> bool:
        return all(
            (
                current.tenant_id == request.tenant_id,
                current.case_id == request.case_id,
                current.approval_id == request.approval_id,
                current.action_id == request.action_id,
                current.run_id == request.run_id,
                current.wait_id == request.wait_id,
                current.wait_generation == request.wait_generation,
                current.action_parameters_sha256 == request.action_parameters_sha256,
                current.policy_version == request.policy_version,
                current.requested_by == request.requested_by,
                current.expires_at == request.expires_at,
            )
        )

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
        # The Case lock serializes this path with a concurrent decision.  Read
        # again after it so an idempotent click can replay even though the
        # winning decision already moved the Run out of WAITING_APPROVAL.
        initial = self.get(conn, tenant_id, approval_id)
        if initial is None:
            raise ContractViolation(ErrorCode.FORBIDDEN, "approval not found")
        wait_binding: tuple[WaitRecord, tuple[Any, ...]] | None = None
        if initial.run_id is not None and initial.decision == "PENDING":
            assert initial.wait_id is not None and initial.wait_generation is not None
            wait_binding = self._lock_run_wait(
                conn,
                tenant_id=tenant_id,
                case_id=initial.case_id,
                run_id=initial.run_id,
                wait_id=initial.wait_id,
                wait_generation=initial.wait_generation,
                approval_id=initial.approval_id,
                action_parameters_sha256=initial.action_parameters_sha256,
            )
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
        result = _record(updated)
        if wait_binding is not None:
            wait, run = wait_binding
            assert result.run_id is not None
            assert result.wait_id is not None and result.wait_generation is not None
            payload_sha256 = _decision_payload_sha256(
                approval_id=result.approval_id,
                decision=result.decision,
                decision_key=decision_idempotency_key,
                reason=decision_reason,
            )
            signal = InboxSignal(
                tenant_id=result.tenant_id,
                case_id=result.case_id,
                run_id=result.run_id,
                event_id=f"approval:{result.approval_id}:{decision_idempotency_key}",
                source_id="approval-service",
                source_event_id=f"{result.approval_id}:{decision_idempotency_key}",
                wait_id=result.wait_id,
                generation=result.wait_generation,
                kind="approval",
                correlation_key=wait.correlation_key,
                condition_version=wait.condition_version,
                received_at=self._db_now(conn).astimezone(UTC),
                payload=ArtifactReference(
                    tenant_id=result.tenant_id,
                    case_id=result.case_id,
                    reference_id=f"approval-decision:{result.approval_id}:{decision_idempotency_key}",
                    sha256=payload_sha256,
                ),
            )
            WaitRepository().resolve_locked(conn, wait, run, signal)
        append_approval_decided(conn, result)
        return result

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
        result = _record(updated)
        append_approval_decided(conn, result)
        return result

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
