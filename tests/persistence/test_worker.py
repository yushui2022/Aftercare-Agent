"""Durable Worker integration tests; skipped without PostgreSQL."""

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from functools import partial
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

import aftercare_agent.runtime.worker as worker_module
from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.investigation import (
    InvestigationAssessment,
    InvestigationDisposition,
    InvestigationProposal,
    MissingMaterial,
)
from aftercare_agent.domain.protocol import ArtifactReference, Checkpoint, ToolRequest
from aftercare_agent.domain.runtime import CaseRecord, ExecutionClaim, RunRecord
from aftercare_agent.model_adapters.budget import ModelPricing
from aftercare_agent.model_adapters.responses import ResponsesAdapter, ToolSpec
from aftercare_agent.model_adapters.transport import ProviderError
from aftercare_agent.persistence import (
    AdmissionRepository,
    CheckpointRepository,
    Database,
    InvestigationAssessmentRepository,
    RunRepository,
    migrate,
)
from aftercare_agent.runtime import run_next, run_once
from aftercare_agent.runtime.harness import HarnessResult
from aftercare_agent.runtime.model_harness import HarnessLimits, run_model_harness
from aftercare_agent.runtime.worker import AssessmentEvaluator, Harness

NOW = datetime(2026, 9, 12, 12, tzinfo=UTC)
VALID_PROPOSAL = (
    '{"schema_version":1,"claims":[{"claim":"order_recorded","evidence_refs":["evidence-1"]}]}'
)


def _assessment_evaluator(
    connection: psycopg.Connection[object],
    checkpoint: Checkpoint,
    proposal: InvestigationProposal,
    now: datetime,
) -> InvestigationAssessment:
    del connection, proposal
    return InvestigationAssessment(
        tenant_id=checkpoint.tenant_id,
        case_id=checkpoint.case_id,
        order_id="order-1",
        policy_id="worker-test-policy",
        policy_version=1,
        evaluated_at=now,
        disposition=InvestigationDisposition.NEEDS_MATERIAL,
        decisions=(),
        missing=(MissingMaterial.ORDER_SNAPSHOT,),
        conflicts=(),
        unavailable=(),
    )


ASSESS: AssessmentEvaluator = _assessment_evaluator


@pytest.fixture()
def queue_db(db: Database) -> Iterator[Database]:
    """Give cross-tenant scheduler tests their own isolated queue universe."""
    schema = f"queue_test_{uuid4().hex}"
    with db.transaction() as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    value = Database(make_conninfo(db.dsn, options=f"-c search_path={schema}"))
    try:
        with value.transaction() as connection:
            migrate(connection)
        yield value
    finally:
        with db.transaction() as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _seed(db: Database, tenant: str, case_id: str, run_id: str) -> None:
    with db.transaction() as connection:
        connection.execute(
            "DELETE FROM aftercare_investigation_assessments WHERE tenant_id=%s", (tenant,)
        )
        connection.execute("DELETE FROM aftercare_checkpoints WHERE tenant_id=%s", (tenant,))
        connection.execute("DELETE FROM aftercare_runs WHERE tenant_id=%s", (tenant,))
        connection.execute("DELETE FROM aftercare_cases WHERE tenant_id=%s", (tenant,))
        runs = RunRepository()
        runs.create_case(
            connection, CaseRecord(tenant_id=tenant, case_id=case_id, order_id="order-1", version=1)
        )
        runs.create_run(
            connection,
            RunRecord(
                tenant_id=tenant,
                case_id=case_id,
                run_id=run_id,
                definition_version="v1",
                input_version=1,
            ),
        )


def test_worker_persists_bounded_slice_and_resumes_to_completion(db: Database) -> None:
    tenant, case_id, run_id = "worker-tenant", "worker-case", "worker-run"
    _seed(db, tenant, case_id, run_id)
    first = run_once(
        db,
        tenant_id=tenant,
        run_id=run_id,
        owner="worker-a",
        now=NOW,
        max_steps=2,
    )
    assert not first.completed
    assert first.fencing_token == 1
    assert first.checkpoint.next_step == "tool"

    second = run_once(
        db,
        tenant_id=tenant,
        run_id=run_id,
        owner="worker-a",
        now=NOW,
        max_steps=8,
    )
    assert second.completed
    assert second.fencing_token == 2
    assert second.checkpoint.saved_fencing_token == 2
    with db.connection() as connection:
        stored = RunRepository().get(connection, tenant, run_id)
        assert stored is not None
        assert stored.state == "COMPLETED"


def test_two_independent_workers_only_one_can_advance_a_run(db: Database) -> None:
    tenant, case_id, run_id = "worker-dual", "worker-case", "worker-run"
    _seed(db, tenant, case_id, run_id)

    def execute(owner: str) -> str:
        try:
            result = run_once(
                db,
                tenant_id=tenant,
                run_id=run_id,
                owner=owner,
                now=NOW,
                max_steps=8,
            )
            return f"completed:{result.owner}"
        except ContractViolation as error:
            return f"rejected:{error.code.value}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(execute, ("worker-a", "worker-b")))
    assert sum(item.startswith("completed:") for item in outcomes) == 1
    assert sum(item.startswith("rejected:") for item in outcomes) == 1


def test_expired_worker_cannot_save_after_takeover(db: Database) -> None:
    tenant, case_id, run_id = "worker-fence", "worker-case", "worker-run"
    _seed(db, tenant, case_id, run_id)
    first = run_once(
        db,
        tenant_id=tenant,
        run_id=run_id,
        owner="worker-a",
        now=NOW,
        max_steps=1,
    )
    with db.transaction() as connection:
        connection.execute(
            "UPDATE aftercare_runs SET lease_until=clock_timestamp() - interval '1 second' "
            "WHERE tenant_id=%s AND run_id=%s",
            (tenant, run_id),
        )
        takeover = RunRepository().claim(
            connection, tenant, run_id, "worker-b", timedelta(seconds=30)
        )
        assert takeover.fencing_token == first.fencing_token + 1
    with db.transaction() as connection:
        with pytest.raises(ContractViolation) as error:
            CheckpointRepository().save(
                connection,
                first.checkpoint,
                ExecutionClaim(
                    tenant_id=tenant,
                    case_id=case_id,
                    run_id=run_id,
                    owner="worker-a",
                    fencing_token=first.fencing_token,
                ),
            )
    assert error.value.code is ErrorCode.LEASE_LOST


def test_worker_can_claim_next_ready_run_and_reports_idle(db: Database) -> None:
    tenant, case_id, run_id = "worker-next", "worker-case", "worker-run"
    _seed(db, tenant, case_id, run_id)
    result = run_next(
        db,
        tenant_id=tenant,
        owner="worker-next",
        now=NOW,
        max_steps=8,
    )
    assert result is not None
    assert result.run_id == run_id
    assert result.completed
    assert run_next(db, tenant_id=tenant, owner="worker-next", now=NOW, max_steps=8) is None


def test_shared_worker_round_robins_across_tenants(queue_db: Database) -> None:
    _seed(queue_db, "fair-tenant-a", "fair-case-a", "fair-run-a")
    _seed(queue_db, "fair-tenant-b", "fair-case-b", "fair-run-b")

    first = run_next(queue_db, tenant_id=None, owner="fair-worker", now=NOW, max_steps=1)
    second = run_next(queue_db, tenant_id=None, owner="fair-worker", now=NOW, max_steps=1)

    assert first is not None and second is not None
    assert {first.tenant_id, second.tenant_id} == {"fair-tenant-a", "fair-tenant-b"}


def test_shared_worker_reclaims_expired_inflight_queue_row(queue_db: Database) -> None:
    tenant, case_id, run_id = "fair-reclaim", "fair-case", "fair-run"
    _seed(queue_db, tenant, case_id, run_id)
    with queue_db.transaction() as connection:
        old_claim = RunRepository().claim(
            connection, tenant, run_id, "crashed-worker", timedelta(seconds=30)
        )
        connection.execute(
            "UPDATE aftercare_runs SET lease_until=clock_timestamp() - interval '1 second' "
            "WHERE tenant_id=%s AND run_id=%s",
            (tenant, run_id),
        )
        queue = connection.execute(
            "SELECT state,claim_owner,claim_token FROM aftercare_execution_queue "
            "WHERE tenant_id=%s AND run_id=%s",
            (tenant, run_id),
        ).fetchone()
        assert queue == ("IN_FLIGHT", old_claim.owner, old_claim.fencing_token)

    result = run_next(queue_db, tenant_id=None, owner="recovery-worker", now=NOW, max_steps=8)
    assert result is not None
    assert result.fencing_token == old_claim.fencing_token + 1
    assert result.owner == "recovery-worker"


def test_shared_worker_skips_tenant_at_its_admission_limit(queue_db: Database) -> None:
    _seed(queue_db, "limited-tenant", "limited-case", "limited-run")
    _seed(queue_db, "available-tenant", "available-case", "available-run")
    with queue_db.transaction() as connection:
        admission = AdmissionRepository()
        admission.configure_limit(
            connection,
            scope_kind="tenant",
            scope_id="limited-tenant",
            max_active_slots=1,
        )
        claim = RunRepository().claim(
            connection, "limited-tenant", "limited-run", "held-worker", timedelta(seconds=30)
        )
        assert admission.acquire_slot(connection, claim, timedelta(seconds=30)) is not None

    result = run_next(queue_db, tenant_id=None, owner="fair-worker", now=NOW, max_steps=8)
    assert result is not None
    assert result.tenant_id == "available-tenant"


def test_worker_heartbeat_keeps_long_slice_lease_alive(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slice may run well past one lease as long as it keeps renewing.

    The budget is deliberately no tighter than this.  The first renewal has to
    absorb opening its connection, and a measured Windows handshake sits at
    115 ms p50 / 215 ms max against 0.8 ms for the renewal itself.  A 200 ms
    lease left that handshake racing the deadline it had to beat, so the case
    failed on roughly half of its runs on a healthy machine even though the
    Worker was behaving correctly.
    """
    tenant, case_id, run_id = "worker-heartbeat", "worker-case", "worker-run"
    _seed(db, tenant, case_id, run_id)
    original = worker_module.run_fake_harness

    def slow_harness(
        *,
        tenant_id: str,
        case_id: str,
        run_id: str,
        checkpoint: Checkpoint | None,
        now: datetime,
        max_steps: int,
    ) -> HarnessResult:
        time.sleep(1.5)
        return original(
            tenant_id=tenant_id,
            case_id=case_id,
            run_id=run_id,
            checkpoint=checkpoint,
            now=now,
            max_steps=max_steps,
        )

    monkeypatch.setattr(worker_module, "run_fake_harness", slow_harness)
    result = run_once(
        db,
        tenant_id=tenant,
        run_id=run_id,
        owner="heartbeat-worker",
        now=NOW,
        lease=timedelta(seconds=1),
        heartbeat_interval=timedelta(milliseconds=100),
    )
    assert result.completed


def test_worker_refuses_slice_after_heartbeat_observes_takeover(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, case_id, run_id = "worker-heartbeat-lost", "worker-case", "worker-run"
    _seed(db, tenant, case_id, run_id)
    original = worker_module.run_fake_harness

    def stolen_harness(
        *,
        tenant_id: str,
        case_id: str,
        run_id: str,
        checkpoint: Checkpoint | None,
        now: datetime,
        max_steps: int,
    ) -> HarnessResult:
        with db.transaction() as connection:
            connection.execute(
                "UPDATE aftercare_runs SET lease_until=clock_timestamp() - interval '1 second' "
                "WHERE tenant_id=%s AND run_id=%s",
                (tenant, run_id),
            )
            RunRepository().claim(connection, tenant, run_id, "takeover", timedelta(seconds=30))
        time.sleep(0.15)
        return original(
            tenant_id=tenant_id,
            case_id=case_id,
            run_id=run_id,
            checkpoint=checkpoint,
            now=now,
            max_steps=max_steps,
        )

    monkeypatch.setattr(worker_module, "run_fake_harness", stolen_harness)
    with pytest.raises(ContractViolation) as error:
        run_once(
            db,
            tenant_id=tenant,
            run_id=run_id,
            owner="lost-worker",
            now=NOW,
            heartbeat_interval=timedelta(milliseconds=30),
        )
    assert error.value.code is ErrorCode.LEASE_LOST
    with db.connection() as connection:
        assert CheckpointRepository().get_latest(connection, tenant, run_id) is None


ROUTE_TOOLS = (ToolSpec(name="lookup_order"),)
ROUTE_PRICING = ModelPricing(input_microusd_per_token=1, output_microusd_per_token=1)


class _RecordingProvider:
    """Answer the n-th Responses call with one text envelope; earlier calls refuse."""

    def __init__(self, text: str, *, failures: int = 0, status_code: int = 503) -> None:
        self.responses = self
        self.calls: list[dict[str, object]] = []
        self._failures = failures
        self._status_code = status_code
        self._payload: dict[str, object] = {
            "id": "resp-route",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": text}],
                }
            ],
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        }

    def create(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(dict(kwargs))
        if len(self.calls) <= self._failures:
            raise ProviderError("provider refused the call", status_code=self._status_code)
        return self._payload


class _UnusedExecutor:
    """A route test must never reach the tool phase, so a call here is a bug."""

    def execute(self, request: ToolRequest, *, scope: object, now: datetime) -> ArtifactReference:
        del scope, now
        raise AssertionError(f"unexpected tool execution: {request.name}")


def _model_harness(client: object, limits: HarnessLimits) -> Harness:
    return partial(
        run_model_harness,
        adapter=ResponsesAdapter(client, allowed_tools=frozenset(t.name for t in ROUTE_TOOLS)),
        model="deepseek-flash",
        tools=ROUTE_TOOLS,
        limits=limits,
        executor=_UnusedExecutor(),
        render_input=lambda checkpoint: "investigate",
        pricing=ROUTE_PRICING,
    )


def test_a_spent_model_budget_parks_the_run_in_review_out_of_the_queue(db: Database) -> None:
    tenant, case_id, run_id = "route-budget", "route-case", "route-run"
    _seed(db, tenant, case_id, run_id)
    client = _RecordingProvider("never asked")
    limits = HarnessLimits(model_calls=0, tool_calls=1, cost_microusd=1_000, ttl=timedelta(hours=1))
    result = run_next(
        db,
        tenant_id=tenant,
        owner="worker-route",
        now=datetime.now(UTC),
        max_steps=4,
        harness=_model_harness(client, limits),
    )
    assert result is not None and not result.completed
    assert client.calls == [], "a Run that may not spend must not reach the provider"
    assert result.checkpoint.route_reason == "model_budget_exhausted"
    with db.connection() as connection:
        stored = RunRepository().get(connection, tenant, run_id)
        queued = connection.execute(
            "SELECT 1 FROM aftercare_execution_queue WHERE tenant_id=%s AND run_id=%s",
            (tenant, run_id),
        ).fetchone()
    # REVIEW is operator-visible, unlike RUNNING until lease expiry, and its
    # missing queue row means nothing may re-dispatch it on its own.
    assert stored is not None and stored.state == "REVIEW"
    assert stored.available_at is None
    assert queued is None
    assert run_next(db, tenant_id=tenant, owner="worker-route", max_steps=4) is None


def test_a_retryable_provider_failure_waits_for_its_available_at(db: Database) -> None:
    tenant, case_id, run_id = "route-retry", "route-case", "route-run"
    _seed(db, tenant, case_id, run_id)
    backoff = timedelta(seconds=1)
    limits = HarnessLimits(
        model_calls=2,
        tool_calls=1,
        cost_microusd=1_000,
        ttl=timedelta(hours=1),
        retry_backoff=backoff,
    )
    client = _RecordingProvider(VALID_PROPOSAL, failures=1)
    harness = _model_harness(client, limits)
    parked = run_next(
        db,
        tenant_id=tenant,
        owner="worker-route",
        now=datetime.now(UTC),
        max_steps=2,
        harness=harness,
        assessment_evaluator=ASSESS,
    )
    assert parked is not None
    assert parked.checkpoint.route_reason == "provider_retryable_error"
    with db.connection() as connection:
        stored = RunRepository().get(connection, tenant, run_id)
        queued = connection.execute(
            "SELECT state,available_at FROM aftercare_execution_queue "
            "WHERE tenant_id=%s AND run_id=%s",
            (tenant, run_id),
        ).fetchone()
    assert stored is not None and stored.state == "RETRY_AT"
    assert stored.available_at == parked.checkpoint.available_at
    # Run and wake-up row share one future instant: the database clock decides.
    assert queued == ("READY", stored.available_at)
    assert (
        run_next(
            db,
            tenant_id=tenant,
            owner="worker-route",
            max_steps=2,
            harness=harness,
            assessment_evaluator=ASSESS,
        )
        is None
    )
    time.sleep(backoff.total_seconds() + 0.5)
    resumed = run_next(
        db,
        tenant_id=tenant,
        owner="worker-route",
        max_steps=2,
        harness=harness,
        assessment_evaluator=ASSESS,
    )
    assert resumed is not None and resumed.checkpoint.next_step == "evaluate"
    finalized = run_next(
        db,
        tenant_id=tenant,
        owner="worker-route",
        max_steps=2,
        harness=harness,
        assessment_evaluator=ASSESS,
    )
    assert finalized is not None and finalized.completed
    assert finalized.checkpoint.route_reason is None
    assert len(client.calls) == 2, "the released retry must reach the provider again"


def test_an_operator_release_re_queues_a_review_run_and_it_finishes(db: Database) -> None:
    tenant, case_id, run_id = "route-release", "route-case", "route-run"
    _seed(db, tenant, case_id, run_id)
    limits = HarnessLimits(model_calls=2, tool_calls=1, cost_microusd=1_000, ttl=timedelta(hours=1))
    refused = _RecordingProvider("unreachable", failures=99, status_code=401)
    parked = run_next(
        db,
        tenant_id=tenant,
        owner="worker-route",
        now=datetime.now(UTC),
        max_steps=2,
        harness=_model_harness(refused, limits),
    )
    assert parked is not None
    assert parked.checkpoint.route_reason == "provider_error"
    with db.connection() as connection:
        stored = RunRepository().get(connection, tenant, run_id)
    assert stored is not None and stored.state == "REVIEW"
    with db.transaction() as connection:
        RunRepository().transition(
            connection,
            ExecutionClaim(
                tenant_id=tenant,
                case_id=case_id,
                run_id=run_id,
                owner="operator",
                fencing_token=stored.fencing_token,
            ),
            "READY",
        )
    resumed = run_next(
        db,
        tenant_id=tenant,
        owner="worker-route",
        now=datetime.now(UTC),
        max_steps=2,
        harness=_model_harness(_RecordingProvider(VALID_PROPOSAL), limits),
        assessment_evaluator=ASSESS,
    )
    assert resumed is not None and resumed.checkpoint.next_step == "evaluate"
    finalized = run_next(
        db,
        tenant_id=tenant,
        owner="worker-route",
        now=datetime.now(UTC),
        max_steps=2,
        harness=_model_harness(_RecordingProvider(VALID_PROPOSAL), limits),
        assessment_evaluator=ASSESS,
    )
    assert finalized is not None and finalized.completed
    assert resumed.fencing_token == parked.fencing_token + 1
    with db.connection() as connection:
        final = RunRepository().get(connection, tenant, run_id)
        assessment = InvestigationAssessmentRepository().get_latest(
            connection, tenant_id=tenant, case_id=case_id, run_id=run_id
        )
    assert final is not None and final.state == "COMPLETED"
    assert assessment is not None
    assert assessment.assessment.disposition is InvestigationDisposition.NEEDS_MATERIAL
