"""PostgreSQL tests for the trusted REVIEW gate."""

from datetime import timedelta

import pytest

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.reviews import ReviewRequest
from aftercare_agent.domain.runtime import CaseRecord, RunRecord
from aftercare_agent.persistence import Database, ReviewRepository, RunRepository


def _seed(db: Database, tenant: str = "review-test") -> ReviewRequest:
    with db.transaction() as conn:
        conn.execute("DELETE FROM aftercare_reviews WHERE tenant_id=%s", (tenant,))
        conn.execute("DELETE FROM aftercare_execution_queue WHERE tenant_id=%s", (tenant,))
        conn.execute("DELETE FROM aftercare_runs WHERE tenant_id=%s", (tenant,))
        conn.execute("DELETE FROM aftercare_cases WHERE tenant_id=%s", (tenant,))
        runs = RunRepository()
        runs.create_case(
            conn,
            CaseRecord(tenant_id=tenant, case_id="case-1", order_id="order-1", version=1),
        )
        runs.create_run(
            conn,
            RunRecord(
                tenant_id=tenant,
                case_id="case-1",
                run_id="run-1",
                definition_version="v1",
                input_version=1,
                state="REVIEW",
            ),
        )
    return ReviewRequest(
        tenant_id=tenant,
        case_id="case-1",
        run_id="run-1",
        review_id="review-1",
        reason_code="evidence-conflict",
        evidence_sha256="a" * 64,
        policy_version="review-v1",
        requested_by="agent-service",
        input_version=1,
    )


def test_request_and_continue_resolution_requeues_run(db: Database) -> None:
    request = _seed(db)
    repository = ReviewRepository()
    with db.transaction() as conn:
        record, replayed = repository.request(conn, request)
        assert record.decision is None and not replayed
        assert conn.execute(
            "SELECT event_type FROM aftercare_outbox WHERE tenant_id=%s AND case_id=%s "
            "ORDER BY case_seq",
            (request.tenant_id, request.case_id),
        ).fetchall() == [("review.requested",)]
        same, replayed = repository.request(conn, request)
        assert same == record and replayed
        decided = repository.decide(
            conn,
            request.tenant_id,
            request.review_id,
            reviewer="ops-reviewer",
            decision="CONTINUE",
            decision_idempotency_key="decision-1",
            decision_reason="new carrier evidence accepted",
        )
        assert decided.decision == "CONTINUE"
        assert conn.execute(
            "SELECT event_type FROM aftercare_outbox WHERE tenant_id=%s AND case_id=%s "
            "ORDER BY case_seq",
            (request.tenant_id, request.case_id),
        ).fetchall() == [("review.requested",), ("review.decided",)]
        assert conn.execute(
            "SELECT state FROM aftercare_runs WHERE tenant_id=%s AND run_id=%s",
            (request.tenant_id, request.run_id),
        ).fetchone() == ("READY",)
        assert conn.execute(
            "SELECT state FROM aftercare_execution_queue WHERE tenant_id=%s AND run_id=%s",
            (request.tenant_id, request.run_id),
        ).fetchone() == ("READY",)
        assert repository.resolve_review(conn, request.tenant_id, request.review_id) == decided
        assert conn.execute(
            "SELECT count(*) FROM aftercare_outbox WHERE tenant_id=%s AND case_id=%s",
            (request.tenant_id, request.case_id),
        ).fetchone() == (2,)


def test_old_decision_replay_cannot_resolve_a_later_review(db: Database) -> None:
    request = _seed(db, "review-replay")
    repository = ReviewRepository()
    with db.transaction() as conn:
        repository.request(conn, request)
        decided = repository.decide(
            conn,
            request.tenant_id,
            request.review_id,
            reviewer="ops-reviewer",
            decision="CONTINUE",
            decision_idempotency_key="decision-1",
        )
        runs = RunRepository()
        claim = runs.claim(
            conn, request.tenant_id, request.run_id, "worker-a", timedelta(seconds=30)
        )
        runs.transition(conn, claim, "REVIEW")
        later = request.model_copy(update={"review_id": "review-2"})
        repository.request(conn, later)
        assert (
            repository.decide(
                conn,
                request.tenant_id,
                request.review_id,
                reviewer="ops-reviewer",
                decision="CONTINUE",
                decision_idempotency_key="decision-1",
            )
            == decided
        )
        assert repository.resolve_review(conn, request.tenant_id, request.review_id) == decided
        assert repository.request(conn, request) == (decided, True)
        assert conn.execute(
            "SELECT state FROM aftercare_runs WHERE tenant_id=%s AND run_id=%s",
            (request.tenant_id, request.run_id),
        ).fetchone() == ("REVIEW",)
        pending = repository.get(conn, later.tenant_id, later.review_id)
        assert pending is not None and pending.decision is None


def test_cancel_resolution_is_terminal_and_replay_is_exact(db: Database) -> None:
    request = _seed(db, "review-cancel")
    repository = ReviewRepository()
    with db.transaction() as conn:
        repository.request(conn, request)
        decided = repository.decide(
            conn,
            request.tenant_id,
            request.review_id,
            reviewer="ops-reviewer",
            decision="CANCEL",
            decision_idempotency_key="decision-cancel",
            decision_reason="cannot establish delivery facts",
        )
        assert decided.decision == "CANCEL"
        assert conn.execute(
            "SELECT state FROM aftercare_runs WHERE tenant_id=%s AND run_id=%s",
            (request.tenant_id, request.run_id),
        ).fetchone() == ("CANCELLED",)
        replay = repository.decide(
            conn,
            request.tenant_id,
            request.review_id,
            reviewer="ops-reviewer",
            decision="CANCEL",
            decision_idempotency_key="decision-cancel",
            decision_reason="cannot establish delivery facts",
        )
        assert replay == decided
        with pytest.raises(ContractViolation) as changed:
            repository.decide(
                conn,
                request.tenant_id,
                request.review_id,
                reviewer="ops-reviewer",
                decision="CONTINUE",
                decision_idempotency_key="decision-other",
            )
        assert changed.value.code is ErrorCode.CONFLICT


def test_review_rejects_self_decision_and_stale_input(db: Database) -> None:
    request = _seed(db, "review-guards")
    repository = ReviewRepository()
    with db.transaction() as conn:
        repository.request(conn, request)
        with pytest.raises(ContractViolation) as self_review:
            repository.decide(
                conn,
                request.tenant_id,
                request.review_id,
                reviewer=request.requested_by,
                decision="CANCEL",
                decision_idempotency_key="self",
            )
        assert self_review.value.code is ErrorCode.FORBIDDEN
        conn.execute(
            "UPDATE aftercare_runs SET input_version=2 WHERE tenant_id=%s AND run_id=%s",
            (request.tenant_id, request.run_id),
        )
        with pytest.raises(ContractViolation) as stale:
            repository.decide(
                conn,
                request.tenant_id,
                request.review_id,
                reviewer="ops-reviewer",
                decision="CANCEL",
                decision_idempotency_key="stale",
            )
        assert stale.value.code is ErrorCode.CONFLICT
