"""PostgreSQL persistence for the trusted REVIEW gate."""

from typing import Any, Literal

import psycopg

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.reviews import ReviewRecord, ReviewRequest

from .gate_events import append_review_decided, append_review_requested

_FIELDS = (
    "tenant_id",
    "case_id",
    "run_id",
    "review_id",
    "reason_code",
    "evidence_sha256",
    "policy_version",
    "requested_by",
    "input_version",
    "decision",
    "reviewer",
    "decision_idempotency_key",
    "decision_reason",
    "created_at",
    "decided_at",
    "updated_at",
)


def _record(row: tuple[Any, ...]) -> ReviewRecord:
    return ReviewRecord.model_validate(dict(zip(_FIELDS, row, strict=False)))


class ReviewRepository:
    """Case-scoped, one-shot review requests with fenced Run resolution."""

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
    def _lock_run(
        conn: psycopg.Connection[Any], tenant_id: str, case_id: str, run_id: str
    ) -> tuple[Any, ...]:
        row = conn.execute(
            "SELECT state,input_version,wait_id,wait_generation FROM aftercare_runs "
            "WHERE tenant_id=%s AND case_id=%s AND run_id=%s FOR UPDATE",
            (tenant_id, case_id, run_id),
        ).fetchone()
        if row is None:
            raise ContractViolation(ErrorCode.FORBIDDEN, "run not found")
        return tuple(row)

    def get(
        self, conn: psycopg.Connection[Any], tenant_id: str, review_id: str
    ) -> ReviewRecord | None:
        row = conn.execute(
            "SELECT " + ",".join(_FIELDS) + " FROM aftercare_reviews "
            "WHERE tenant_id=%s AND review_id=%s",
            (tenant_id, review_id),
        ).fetchone()
        return None if row is None else _record(row)

    def list_for_case(
        self,
        conn: psycopg.Connection[Any],
        tenant_id: str,
        case_id: str,
        *,
        limit: int = 50,
    ) -> list[ReviewRecord]:
        """List one Case's reviews, newest first, without granting authority."""
        rows = conn.execute(
            "SELECT " + ",".join(_FIELDS) + " FROM aftercare_reviews "
            "WHERE tenant_id=%s AND case_id=%s "
            "ORDER BY created_at DESC, review_id DESC LIMIT %s",
            (tenant_id, case_id, limit),
        ).fetchall()
        return [_record(row) for row in rows]

    def request(
        self, conn: psycopg.Connection[Any], request: ReviewRequest
    ) -> tuple[ReviewRecord, bool]:
        """Register one review while the Run is durably in REVIEW.

        The caller may transition RUNNING→REVIEW before this method in the
        same transaction.  Case→Run is locked before the review row, so a
        concurrent resolver cannot observe a half-created request.
        """
        self._lock_case(conn, request.tenant_id, request.case_id)
        run = self._lock_run(conn, request.tenant_id, request.case_id, request.run_id)
        existing = conn.execute(
            "SELECT " + ",".join(_FIELDS) + " FROM aftercare_reviews "
            "WHERE tenant_id=%s AND review_id=%s FOR UPDATE",
            (request.tenant_id, request.review_id),
        ).fetchone()
        if existing is not None:
            current = _record(existing)
            if not self._same_request(current, request):
                raise ContractViolation(ErrorCode.CONFLICT, "review request replay changed")
            return current, True
        if run[0] != "REVIEW":
            raise ContractViolation(ErrorCode.CONFLICT, "review request requires REVIEW run")
        if int(run[1]) != request.input_version:
            raise ContractViolation(ErrorCode.CONFLICT, "review input version changed")
        inserted = conn.execute(
            "INSERT INTO aftercare_reviews(" + ",".join(_FIELDS[:9]) + ") "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING "
            + ",".join(_FIELDS),
            (
                request.tenant_id,
                request.case_id,
                request.run_id,
                request.review_id,
                request.reason_code,
                request.evidence_sha256,
                request.policy_version,
                request.requested_by,
                request.input_version,
            ),
        ).fetchone()
        if inserted is not None:
            result = _record(inserted)
            append_review_requested(conn, result)
            return result, False
        row = conn.execute(
            "SELECT " + ",".join(_FIELDS) + " FROM aftercare_reviews "
            "WHERE tenant_id=%s AND review_id=%s FOR UPDATE",
            (request.tenant_id, request.review_id),
        ).fetchone()
        if row is None:
            raise ContractViolation(ErrorCode.RETRYABLE, "review request outcome is unknown")
        current = _record(row)
        if not self._same_request(current, request):
            raise ContractViolation(ErrorCode.CONFLICT, "review request replay changed")
        return current, True

    @staticmethod
    def _same_request(current: ReviewRecord, request: ReviewRequest) -> bool:
        return all(
            current.model_dump()[field] == value for field, value in request.model_dump().items()
        )

    def decide(
        self,
        conn: psycopg.Connection[Any],
        tenant_id: str,
        review_id: str,
        *,
        reviewer: str,
        decision: Literal["CONTINUE", "CANCEL"],
        decision_idempotency_key: str,
        decision_reason: str | None = None,
    ) -> ReviewRecord:
        """Record one decision and atomically resolve its REVIEW Run."""
        initial = self.get(conn, tenant_id, review_id)
        if initial is None:
            raise ContractViolation(ErrorCode.FORBIDDEN, "review not found")
        self._lock_case(conn, tenant_id, initial.case_id)
        run = self._lock_run(conn, tenant_id, initial.case_id, initial.run_id)
        row = conn.execute(
            "SELECT " + ",".join(_FIELDS) + " FROM aftercare_reviews "
            "WHERE tenant_id=%s AND review_id=%s FOR UPDATE",
            (tenant_id, review_id),
        ).fetchone()
        if row is None:
            raise ContractViolation(ErrorCode.FORBIDDEN, "review not found")
        current = _record(row)
        if current.case_id != initial.case_id or current.run_id != initial.run_id:
            raise ContractViolation(ErrorCode.CONFLICT, "review scope changed")
        if current.decision is not None:
            if (
                current.decision == decision
                and current.reviewer == reviewer
                and current.decision_idempotency_key == decision_idempotency_key
                and current.decision_reason == decision_reason
            ):
                # The initial decision and Run resolution commit together.
                # The Run may already have advanced or entered a later
                # REVIEW.  Replaying this decision must never route it again.
                return current
            raise ContractViolation(ErrorCode.CONFLICT, "review decision replay changed")
        if run[0] != "REVIEW":
            raise ContractViolation(ErrorCode.CONFLICT, "review run is no longer in REVIEW")
        if int(run[1]) != current.input_version:
            raise ContractViolation(ErrorCode.CONFLICT, "review input version changed")
        if reviewer == current.requested_by:
            raise ContractViolation(ErrorCode.FORBIDDEN, "requester cannot review its own run")
        updated = conn.execute(
            "UPDATE aftercare_reviews SET decision=%s,reviewer=%s,"
            "decision_idempotency_key=%s,decision_reason=%s,decided_at=clock_timestamp(),"
            "updated_at=clock_timestamp() WHERE tenant_id=%s AND review_id=%s RETURNING "
            + ",".join(_FIELDS),
            (
                decision,
                reviewer,
                decision_idempotency_key,
                decision_reason,
                tenant_id,
                review_id,
            ),
        ).fetchone()
        assert updated is not None
        result = _record(updated)
        self._resolve_locked(conn, result, run)
        append_review_decided(conn, result)
        return result

    def resolve_review(
        self,
        conn: psycopg.Connection[Any],
        tenant_id: str,
        review_id: str,
    ) -> ReviewRecord:
        """Replay a committed decision without routing the Run again.

        ``decide`` records and resolves in one transaction, so a committed
        terminal decision has already been applied.  The Run may since have
        advanced or entered another review; an older decision cannot revive
        it or resolve that newer review.
        """
        initial = self.get(conn, tenant_id, review_id)
        if initial is None:
            raise ContractViolation(ErrorCode.FORBIDDEN, "review not found")
        self._lock_case(conn, tenant_id, initial.case_id)
        self._lock_run(conn, tenant_id, initial.case_id, initial.run_id)
        row = conn.execute(
            "SELECT " + ",".join(_FIELDS) + " FROM aftercare_reviews "
            "WHERE tenant_id=%s AND review_id=%s FOR UPDATE",
            (tenant_id, review_id),
        ).fetchone()
        if row is None:
            raise ContractViolation(ErrorCode.FORBIDDEN, "review not found")
        current = _record(row)
        if current.decision is None:
            raise ContractViolation(ErrorCode.CONFLICT, "review decision is still pending")
        return current

    @staticmethod
    def _resolve_locked(
        conn: psycopg.Connection[Any], review: ReviewRecord, run: tuple[Any, ...]
    ) -> None:
        target = "READY" if review.decision == "CONTINUE" else "CANCELLED"
        if run[0] == target:
            return
        if run[0] != "REVIEW":
            raise ContractViolation(ErrorCode.CONFLICT, "review run cannot be resolved")
        conn.execute(
            "UPDATE aftercare_runs SET state=%s,lease_owner=NULL,lease_until=NULL,"
            "wait_id=NULL,wait_generation=NULL,available_at=NULL WHERE tenant_id=%s "
            "AND case_id=%s AND run_id=%s AND state='REVIEW'",
            (target, review.tenant_id, review.case_id, review.run_id),
        )


__all__ = ["ReviewRepository"]
