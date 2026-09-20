"""PostgreSQL tests for the trusted REVIEW gate."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from psycopg.types.json import Jsonb

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.protocol import Checkpoint, RemainingBudget
from aftercare_agent.domain.reviews import ReviewOverrideRequest, ReviewRequest
from aftercare_agent.domain.runtime import CaseRecord, RunRecord
from aftercare_agent.domain.strategy_migrations import StrategyMigrationRequest
from aftercare_agent.persistence import (
    Database,
    ReviewRepository,
    RunRepository,
    StrategyMigrationRepository,
)


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


def test_continue_rejects_spent_model_budget_without_override(db: Database) -> None:
    request = _seed(db, "review-budget")
    checkpoint = Checkpoint(
        tenant_id=request.tenant_id,
        case_id=request.case_id,
        run_id=request.run_id,
        checkpoint_version=1,
        input_version=1,
        case_version=1,
        saved_fencing_token=1,
        definition_version="v1",
        policy_version="policy-v1",
        tool_schema_version="tools-v1",
        model_config_version="model-v1",
        protocol_version="runtime-v1",
        remaining_budget=RemainingBudget(
            model_calls=0,
            tool_calls=1,
            cost_microusd=100,
            deadline=datetime.now(UTC) + timedelta(minutes=5),
        ),
        next_step="review",
        route_reason="model_budget_exhausted",
    )
    repository = ReviewRepository()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO aftercare_checkpoints(tenant_id,case_id,run_id,checkpoint_version,"
            "saved_fencing_token,payload) VALUES (%s,%s,%s,%s,%s,%s)",
            (
                checkpoint.tenant_id,
                checkpoint.case_id,
                checkpoint.run_id,
                checkpoint.checkpoint_version,
                checkpoint.saved_fencing_token,
                Jsonb(checkpoint.model_dump(mode="json")),
            ),
        )
        repository.request(conn, request)
        with pytest.raises(ContractViolation) as caught:
            repository.decide(
                conn,
                request.tenant_id,
                request.review_id,
                reviewer="ops-reviewer",
                decision="CONTINUE",
                decision_idempotency_key="decision-budget",
            )
        assert caught.value.code is ErrorCode.BUDGET_EXHAUSTED


def test_continue_override_updates_checkpoint_and_audit_atomically(db: Database) -> None:
    request = _seed(db, "review-budget-override")
    checkpoint = Checkpoint(
        tenant_id=request.tenant_id,
        case_id=request.case_id,
        run_id=request.run_id,
        checkpoint_version=4,
        input_version=1,
        case_version=1,
        saved_fencing_token=1,
        definition_version="v1",
        policy_version="policy-v1",
        tool_schema_version="tools-v1",
        model_config_version="model-v1",
        protocol_version="runtime-v1",
        remaining_budget=RemainingBudget(
            model_calls=0,
            tool_calls=1,
            cost_microusd=100,
            deadline=datetime.now(UTC) - timedelta(minutes=1),
        ),
        next_step="review",
        route_reason="model_budget_exhausted",
    )
    repository = ReviewRepository()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO aftercare_checkpoints(tenant_id,case_id,run_id,checkpoint_version,"
            "saved_fencing_token,payload) VALUES (%s,%s,%s,%s,%s,%s)",
            (
                checkpoint.tenant_id,
                checkpoint.case_id,
                checkpoint.run_id,
                checkpoint.checkpoint_version,
                checkpoint.saved_fencing_token,
                Jsonb(checkpoint.model_dump(mode="json")),
            ),
        )
        repository.request(conn, request)
        result = repository.decide(
            conn,
            request.tenant_id,
            request.review_id,
            reviewer="ops-reviewer",
            decision="CONTINUE",
            decision_idempotency_key="decision-budget-override",
            decision_reason="approved controlled retry",
            override=ReviewOverrideRequest(
                checkpoint_version=4,
                model_calls_add=2,
                deadline_extension_seconds=60,
                reason="new carrier evidence is expected within the retry window",
            ),
        )
        assert result.decision == "CONTINUE"
        run = conn.execute(
            "SELECT state FROM aftercare_runs WHERE tenant_id=%s AND run_id=%s",
            (request.tenant_id, request.run_id),
        ).fetchone()
        assert run == ("READY",)
        payload = conn.execute(
            "SELECT payload FROM aftercare_checkpoints WHERE tenant_id=%s AND run_id=%s "
            "ORDER BY checkpoint_version DESC LIMIT 1",
            (request.tenant_id, request.run_id),
        ).fetchone()
        assert payload is not None
        resumed = Checkpoint.model_validate_json(json.dumps(payload[0], ensure_ascii=False))
        assert resumed.checkpoint_version == 5
        assert resumed.next_step == "model"
        assert resumed.route_reason is None
        assert resumed.remaining_budget.model_calls == 2
        assert resumed.remaining_budget.deadline > checkpoint.remaining_budget.deadline
        override = conn.execute(
            "SELECT created_by,model_calls_add,deadline_extension_seconds,checkpoint_version "
            "FROM aftercare_review_overrides WHERE tenant_id=%s AND review_id=%s",
            (request.tenant_id, request.review_id),
        ).fetchone()
        assert override is not None
        assert override[:4] == ("ops-reviewer", 2, 60, 4)
        event_query = (
            "SELECT payload FROM aftercare_event_payloads WHERE tenant_id=%s AND case_id=%s "
            "AND reference_id LIKE 'event-payload:review:%%:decision:%%'"
        )
        event_payload = conn.execute(event_query, (request.tenant_id, request.case_id)).fetchone()
        assert event_payload is not None
        assert event_payload[0]["override"]["model_calls_add"] == 2


def test_strategy_migration_is_audited_and_still_needs_review(db: Database) -> None:
    request = _seed(db, "review-strategy-migration")
    checkpoint = Checkpoint(
        tenant_id=request.tenant_id,
        case_id=request.case_id,
        run_id=request.run_id,
        checkpoint_version=2,
        input_version=1,
        case_version=1,
        saved_fencing_token=1,
        definition_version="v1",
        policy_version="policy-old",
        tool_schema_version="tools-old",
        model_config_version="model-old",
        strategy_id="strategy-old",
        protocol_version="runtime-v1",
        remaining_budget=RemainingBudget(
            model_calls=1,
            tool_calls=1,
            cost_microusd=100,
            deadline=datetime.now(UTC) + timedelta(minutes=5),
        ),
        next_step="review",
        route_reason="model_strategy_changed",
    )
    repository = ReviewRepository()
    migration = StrategyMigrationRepository()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO aftercare_checkpoints(tenant_id,case_id,run_id,checkpoint_version,"
            "saved_fencing_token,payload) VALUES (%s,%s,%s,%s,%s,%s)",
            (
                checkpoint.tenant_id,
                checkpoint.case_id,
                checkpoint.run_id,
                checkpoint.checkpoint_version,
                checkpoint.saved_fencing_token,
                Jsonb(checkpoint.model_dump(mode="json")),
            ),
        )
        repository.request(conn, request)
        migration_request = StrategyMigrationRequest(
            tenant_id=request.tenant_id,
            case_id=request.case_id,
            run_id=request.run_id,
            checkpoint_version=2,
            old_strategy_id="strategy-old",
            old_model_config_version="model-old",
            old_policy_version="policy-old",
            old_tool_schema_version="tools-old",
            new_strategy_id="strategy-new",
            new_model_config_version="model-new",
            new_policy_version="policy-new",
            new_tool_schema_version="tools-new",
            reason="pin a reviewed provider configuration",
            idempotency_key="migration-1",
        )
        record, replayed = migration.migrate(conn, migration_request, migrated_by="ops-migrator")
        assert not replayed and record.checkpoint_version_after == 3
        replay, replayed = migration.migrate(conn, migration_request, migrated_by="ops-migrator")
        assert replay == record and replayed
        with pytest.raises(ContractViolation) as changed:
            migration.migrate(
                conn,
                migration_request.model_copy(update={"new_strategy_id": "strategy-other"}),
                migrated_by="ops-migrator",
            )
        assert changed.value.code is ErrorCode.CONFLICT
        payload = conn.execute(
            "SELECT payload FROM aftercare_checkpoints WHERE tenant_id=%s AND run_id=%s "
            "ORDER BY checkpoint_version DESC LIMIT 1",
            (request.tenant_id, request.run_id),
        ).fetchone()
        assert payload is not None
        migrated = Checkpoint.model_validate_json(json.dumps(payload[0], ensure_ascii=False))
        assert migrated.strategy_id == "strategy-new"
        assert migrated.route_reason == "strategy_migration_ready"
        assert conn.execute(
            "SELECT event_type FROM aftercare_outbox WHERE tenant_id=%s ORDER BY case_seq",
            (request.tenant_id,),
        ).fetchall() == [("review.requested",), ("review.strategy_migrated",)]
        decided = repository.decide(
            conn,
            request.tenant_id,
            request.review_id,
            reviewer="ops-reviewer",
            decision="CONTINUE",
            decision_idempotency_key="decision-after-migration",
        )
        assert decided.decision == "CONTINUE"
        assert conn.execute(
            "SELECT state FROM aftercare_runs WHERE tenant_id=%s AND run_id=%s",
            (request.tenant_id, request.run_id),
        ).fetchone() == ("READY",)
