"""Connector answers reach the evidence ledger; skipped without PostgreSQL."""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path

from evidence_gated_memory.application import Principal

from aftercare_agent.artifacts import ContentAddressedArtifactStore
from aftercare_agent.connectors import CommerceConnector, CommerceSources, load_commerce_dataset
from aftercare_agent.domain.common import RunScope
from aftercare_agent.domain.investigation import (
    ClaimProposal,
    FreshnessPolicy,
    InvestigationClaim,
    InvestigationDisposition,
    InvestigationProposal,
    InvestigationScope,
    MissingMaterial,
    SourceGrant,
)
from aftercare_agent.domain.protocol import ToolRequest
from aftercare_agent.domain.runtime import CaseRecord
from aftercare_agent.investigation import (
    ConnectorEvidenceRecorder,
    InvestigationEvidenceAdapter,
    registered_source_kinds,
)
from aftercare_agent.persistence import Database, RunRepository
from aftercare_agent.runtime.sandbox_executor import SandboxedToolExecutor, StaticCaseBinding
from aftercare_agent.runtime.wiring import DEFAULT_TIERS, TOOL_TIERS
from aftercare_agent.sandbox import FakeSandboxProvider, SandboxManager

SAMPLE = Path(str(files("aftercare_agent.connectors").joinpath("data/commerce-sample.json")))
SOURCES = CommerceSources(order_ledger="erp-api", carrier="carrier-api", buyer_channel="in-app")
# The export is dated 2026-08-20; the case has to live where its data does,
# because the connector refuses to answer for another tenant.
TENANT = "tenant-demo"
CASE = "bridge-case"
ORDER = "A-1001"
SUBJECT = "commerce-connector"
NOW = datetime(2026, 8, 21, 12, tzinfo=UTC)
POLICY = FreshnessPolicy(
    policy_id="investigation-v1",
    policy_version=1,
    order_max_age_seconds=604_800,
    carrier_max_age_seconds=604_800,
    buyer_max_age_seconds=604_800,
)


class _UnusedEgm:
    """This slice records the ledger; the EGM ingest needs the fixed schema."""

    def ingest(self, principal: Principal, case_id: str, command: Mapping[str, object]) -> object:
        raise AssertionError("the EGM ingest path is not part of this test")

    def context(self, principal: Principal, case_id: str, **kwargs: object) -> object:
        raise AssertionError("the EGM context path is not part of this test")


def _principal(subject: str, permission: str, *, registry: Mapping[str, str]) -> Principal:
    return Principal(
        subject=subject,
        tenant_id=TENANT,
        permissions=frozenset({permission}),
        case_ids=frozenset({CASE}),
        source_systems=frozenset(registry),
    )


def _request(name: str, call_id: str, arguments: str = "{}") -> ToolRequest:
    return ToolRequest(call_id=call_id, name=name, arguments_json=arguments)


def _executor(
    db: Database, tmp_path: Path
) -> tuple[SandboxedToolExecutor, InvestigationEvidenceAdapter]:
    scope = InvestigationScope(tenant_id=TENANT, case_id=CASE, order_id=ORDER)
    connector = CommerceConnector(load_commerce_dataset(SAMPLE), sources=SOURCES)
    registry = registered_source_kinds(connector)
    adapter = InvestigationEvidenceAdapter(
        _UnusedEgm(),
        scope,
        connector=_principal(SUBJECT, "evidence:write", registry=registry),
        model_tools=_principal("model-tools", "context:read", registry={}),
        registered_sources=registry,
    )
    executor = SandboxedToolExecutor(
        manager=SandboxManager(
            FakeSandboxProvider(capacity=2), DEFAULT_TIERS, lease=timedelta(minutes=5)
        ),
        connector=connector,
        store=ContentAddressedArtifactStore(tmp_path / "artifacts"),
        bindings=StaticCaseBinding(order_id=ORDER),
        tiers=TOOL_TIERS,
        owner="worker-bridge",
        observer=ConnectorEvidenceRecorder(
            database=db,
            adapter=adapter,
            # One grant per source, each for this case only: the host decides
            # which sources this Run may speak for.
            grants={
                source_id: SourceGrant(
                    **scope.model_dump(), subject_id=SUBJECT, source_id=source_id
                )
                for source_id in (SOURCES.order_ledger, SOURCES.carrier)
            },
        ),
    )
    return executor, adapter


def test_a_connector_answer_reaches_the_ledger_once_and_survives_replay(
    db: Database, tmp_path: Path
) -> None:
    with db.transaction() as connection:
        connection.execute(
            "DELETE FROM aftercare_investigation_observations WHERE tenant_id=%s", (TENANT,)
        )
        connection.execute("DELETE FROM aftercare_cases WHERE tenant_id=%s", (TENANT,))
        RunRepository().create_case(
            connection, CaseRecord(tenant_id=TENANT, case_id=CASE, order_id=ORDER, version=1)
        )
    executor, adapter = _executor(db, tmp_path)
    scope = RunScope(tenant_id=TENANT, case_id=CASE, run_id="run-1")

    first = executor.execute(_request("lookup_order", "call-1"), scope=scope, now=NOW)
    # The same unchanged fact fetched again under a new call id is the same
    # bytes, so the ledger has to see one fact rather than a conflicting second.
    again = executor.execute(_request("lookup_order", "call-2"), scope=scope, now=NOW)
    executor.execute(_request("lookup_tracking", "call-3"), scope=scope, now=NOW)
    # A material draft asks the buyer for facts, so it is not evidence.
    executor.execute(
        _request("request_material_draft", "call-4", '{"questions":["confirm_address"]}'),
        scope=scope,
        now=NOW,
    )
    assert first.sha256 == again.sha256
    assert executor.store.holds(scope, first)

    with db.connection() as connection:
        observations = adapter.persisted_observations(connection)
        citations = {item.kind: item.evidence_id for item in observations}
        newest = max(item.observed_at for item in observations)
        assessment = adapter.assess_persisted(
            connection,
            InvestigationProposal(
                claims=(
                    ClaimProposal(
                        claim=InvestigationClaim.ORDER_RECORDED,
                        evidence_refs=(citations["order_snapshot"],),
                    ),
                    ClaimProposal(
                        claim=InvestigationClaim.CARRIER_REPORTED_DELIVERED,
                        evidence_refs=(citations["logistics_observation"],),
                    ),
                )
            ),
            POLICY,
            now=newest + timedelta(hours=1),
        )

    assert [item.kind for item in observations] == ["logistics_observation", "order_snapshot"]
    assert observations[1].original.sha256 == first.sha256
    assert [decision.accepted for decision in assessment.decisions] == [True, True]
    # Two recorded facts are citable and the case still is not closed: the
    # buyer's own statement is missing, so the disposition asks for it.
    assert assessment.missing == (MissingMaterial.BUYER_STATEMENT,)
    assert assessment.disposition is InvestigationDisposition.NEEDS_MATERIAL
    assert assessment.authorizes_external_action is False
