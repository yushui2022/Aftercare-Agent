"""PostgreSQL persistence for the trusted REVIEW gate."""

import hashlib
import json
from datetime import timedelta
from typing import Any, Literal

import psycopg
from psycopg.types.json import Jsonb

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.protocol import Checkpoint
from aftercare_agent.domain.reviews import (
    ReviewOverrideRecord,
    ReviewOverrideRequest,
    ReviewRecord,
    ReviewRequest,
)

from .gate_events import append_review_decided, append_review_requested

_NON_RESUMABLE_CONTINUE_REASONS = frozenset(
    {
        "model_budget_exhausted",
        "tool_budget_exhausted",
        "cost_budget_exhausted",
        "deadline_passed",
    }
)

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

_OVERRIDE_FIELDS = (
    "tenant_id",
    "case_id",
    "run_id",
    "review_id",
    "override_id",
    "checkpoint_version",
    "model_calls_add",
    "tool_calls_add",
    "cost_microusd_add",
    "deadline_extension_seconds",
    "reason",
    "created_by",
    "idempotency_key",
    "created_at",
)


def _record(row: tuple[Any, ...]) -> ReviewRecord:
    return ReviewRecord.model_validate(dict(zip(_FIELDS, row, strict=False)))


def _override_record(row: tuple[Any, ...]) -> ReviewOverrideRecord:
    return ReviewOverrideRecord.model_validate(dict(zip(_OVERRIDE_FIELDS, row, strict=False)))


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
        override: ReviewOverrideRequest | None = None,
    ) -> ReviewRecord:
        """Record one decision and atomically resolve its REVIEW Run.

        A budget/deadline override, when supplied, is applied to a new
        checkpoint in the same transaction as the decision and the Run state
        transition.  There is no intermediate state in which a Run is READY
        without its corresponding audit record and updated budget.
        """
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
            stored_override = self._get_override_by_key(conn, tenant_id, decision_idempotency_key)
            if (
                current.decision == decision
                and current.reviewer == reviewer
                and current.decision_idempotency_key == decision_idempotency_key
                and current.decision_reason == decision_reason
                and self._same_override(stored_override, override)
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
        if decision == "CONTINUE":
            applied_override = self._assert_resume_budget(
                conn,
                current,
                override,
                reviewer=reviewer,
                decision_idempotency_key=decision_idempotency_key,
            )
        elif override is not None:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "override requires CONTINUE")
        else:
            applied_override = None
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
        append_review_decided(conn, result, override=applied_override)
        return result

    @staticmethod
    def _get_override_by_key(
        conn: psycopg.Connection[Any], tenant_id: str, idempotency_key: str
    ) -> ReviewOverrideRecord | None:
        row = conn.execute(
            "SELECT " + ",".join(_OVERRIDE_FIELDS) + " FROM aftercare_review_overrides "
            "WHERE tenant_id=%s AND idempotency_key=%s",
            (tenant_id, idempotency_key),
        ).fetchone()
        return None if row is None else _override_record(row)

    @staticmethod
    def _same_override(
        current: ReviewOverrideRecord | None, requested: ReviewOverrideRequest | None
    ) -> bool:
        if current is None or requested is None:
            return current is None and requested is None
        return (
            current.checkpoint_version == requested.checkpoint_version
            and current.model_calls_add == requested.model_calls_add
            and current.tool_calls_add == requested.tool_calls_add
            and current.cost_microusd_add == requested.cost_microusd_add
            and current.deadline_extension_seconds == requested.deadline_extension_seconds
            and current.reason == requested.reason
        )

    @staticmethod
    def _latest_checkpoint(
        conn: psycopg.Connection[Any], review: ReviewRecord
    ) -> Checkpoint | None:
        row = conn.execute(
            "SELECT checkpoint_version, payload FROM aftercare_checkpoints "
            "WHERE tenant_id=%s AND case_id=%s AND run_id=%s "
            "ORDER BY checkpoint_version DESC LIMIT 1 FOR UPDATE",
            (review.tenant_id, review.case_id, review.run_id),
        ).fetchone()
        if row is None:
            return None
        try:
            checkpoint_version = int(row[0])
            checkpoint = Checkpoint.model_validate_json(json.dumps(row[1], ensure_ascii=False))
        except (TypeError, ValueError) as exc:
            raise ContractViolation(
                ErrorCode.RETRYABLE, "latest review checkpoint is invalid"
            ) from exc
        if checkpoint_version != checkpoint.checkpoint_version:
            raise ContractViolation(
                ErrorCode.RETRYABLE, "latest review checkpoint version is inconsistent"
            )
        if (checkpoint.tenant_id, checkpoint.case_id, checkpoint.run_id) != (
            review.tenant_id,
            review.case_id,
            review.run_id,
        ):
            raise ContractViolation(ErrorCode.FORBIDDEN, "review checkpoint scope mismatch")
        return checkpoint

    @classmethod
    def _assert_resume_budget(
        cls,
        conn: psycopg.Connection[Any],
        review: ReviewRecord,
        override: ReviewOverrideRequest | None,
        *,
        reviewer: str,
        decision_idempotency_key: str,
    ) -> ReviewOverrideRecord | None:
        """Validate a CONTINUE and, when authorized, stage its new checkpoint."""
        checkpoint = cls._latest_checkpoint(conn, review)
        if checkpoint is not None and checkpoint.route_reason == "model_strategy_changed":
            raise ContractViolation(
                ErrorCode.CONFLICT,
                "model strategy changed; deploy an explicit strategy migration before continuing",
            )
        if checkpoint is None or checkpoint.route_reason not in _NON_RESUMABLE_CONTINUE_REASONS:
            if override is not None:
                raise ContractViolation(
                    ErrorCode.CONFLICT, "review override requires an exhausted budget or deadline"
                )
            return None
        reason = checkpoint.route_reason
        if override is None:
            code = ErrorCode.BUDGET_EXHAUSTED if reason != "deadline_passed" else ErrorCode.CONFLICT
            raise ContractViolation(code, "review needs an explicit budget or deadline override")
        if override.checkpoint_version != checkpoint.checkpoint_version:
            raise ContractViolation(ErrorCode.CONFLICT, "review checkpoint version changed")
        required = {
            "model_budget_exhausted": override.model_calls_add > 0,
            "tool_budget_exhausted": override.tool_calls_add > 0,
            "cost_budget_exhausted": override.cost_microusd_add > 0,
            "deadline_passed": override.deadline_extension_seconds > 0,
        }
        if not required[reason]:
            raise ContractViolation(
                ErrorCode.INVALID_INPUT,
                f"override must increase the exhausted {reason.removesuffix('_exhausted')} budget",
            )
        budget = checkpoint.remaining_budget
        maximum = 2**63 - 1
        values = {
            "model_calls": budget.model_calls + override.model_calls_add,
            "tool_calls": budget.tool_calls + override.tool_calls_add,
            "cost_microusd": budget.cost_microusd + override.cost_microusd_add,
        }
        if any(value > maximum for value in values.values()):
            raise ContractViolation(
                ErrorCode.INVALID_INPUT, "review override exceeds budget limits"
            )
        try:
            deadline = budget.deadline + timedelta(seconds=override.deadline_extension_seconds)
        except OverflowError as exc:
            raise ContractViolation(
                ErrorCode.INVALID_INPUT, "review deadline is out of range"
            ) from exc
        updated_checkpoint = checkpoint.model_copy(
            update={
                "checkpoint_version": checkpoint.checkpoint_version + 1,
                "remaining_budget": budget.model_copy(update={**values, "deadline": deadline}),
                "next_step": "model",
                "route_reason": None,
                "available_at": None,
                "pending_tool": None,
                "pending_proposal_json": None,
                "wait_id": None,
                "wait_generation": None,
                "resume_next_step": None,
            }
        )
        conn.execute(
            "INSERT INTO aftercare_checkpoints(tenant_id,case_id,run_id,checkpoint_version,"
            "saved_fencing_token,payload) VALUES (%s,%s,%s,%s,%s,%s)",
            (
                updated_checkpoint.tenant_id,
                updated_checkpoint.case_id,
                updated_checkpoint.run_id,
                updated_checkpoint.checkpoint_version,
                updated_checkpoint.saved_fencing_token,
                Jsonb(updated_checkpoint.model_dump(mode="json")),
            ),
        )
        override_id = (
            "review-override-"
            + hashlib.sha256(
                f"{review.tenant_id}:{review.review_id}:{decision_idempotency_key}".encode()
            ).hexdigest()
        )
        row = conn.execute(
            "INSERT INTO aftercare_review_overrides("
            + ",".join(_OVERRIDE_FIELDS[:-1])
            + ") VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "RETURNING " + ",".join(_OVERRIDE_FIELDS),
            (
                review.tenant_id,
                review.case_id,
                review.run_id,
                review.review_id,
                override_id,
                checkpoint.checkpoint_version,
                override.model_calls_add,
                override.tool_calls_add,
                override.cost_microusd_add,
                override.deadline_extension_seconds,
                override.reason,
                reviewer,
                decision_idempotency_key,
            ),
        ).fetchone()
        if row is None:
            raise ContractViolation(ErrorCode.RETRYABLE, "review override outcome is unknown")
        return _override_record(row)

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
