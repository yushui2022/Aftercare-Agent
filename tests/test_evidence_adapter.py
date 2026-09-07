import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from evidence_gated_memory.application import EvidenceApplication, Principal
from evidence_gated_memory.storage.providers import SqliteProvider

from aftercare_agent.evidence import AftercareEvidence, RefundIntent


@pytest.mark.parametrize("status, accepted", [("completed", True), ("failed", False)])
def test_embedded_refund_adapter(tmp_path: Path, status: str, accepted: bool) -> None:
    # EGM 0.6's constructor has no annotations; keep this exception at the boundary.
    app = EvidenceApplication(SqliteProvider(tmp_path))  # type: ignore[no-untyped-call]

    def actor(subject: str, permissions: set[str], sources: tuple[str, ...] = ()) -> Principal:
        return Principal(
            subject=subject,
            tenant_id="merchant-1",
            case_ids=frozenset({"case-1"}),
            permissions=frozenset(permissions),
            source_systems=frozenset(sources),
        )

    adapter = AftercareEvidence(
        app,
        "case-1",
        orchestrator=actor("harness", {"task:write"}),
        connector=actor("payments", {"evidence:write"}, ("refund_api",)),
        model_tools=actor("model-tools", {"claim:write", "context:read"}),
    )
    intent = RefundIntent(
        order_id="order-1", action_id="action-1", amount_minor="2500", currency="USD"
    )
    node = adapter.register_refund(intent, operation_id="create", expected_revision=0)
    assert node["revision"] == 1
    assert node["operation_id"] == "create"
    node_id = node["result"]["id"]
    assert isinstance(node_id, str)
    raw = json.dumps(
        {
            **intent.model_dump(),
            "tenant_id": "merchant-1",
            "case_id": "case-1",
            "receipt_id": "receipt-1",
            "status": status,
        }
    )
    observed_at = datetime.now(UTC)
    receipt = adapter.ingest_receipt(
        node_id, raw, observed_at, operation_id="receipt", expected_revision=1
    )
    assert receipt["revision"] == 2
    assert receipt["operation_id"] == "receipt"
    receipt_id = receipt["result"]["id"]
    assert isinstance(receipt_id, str)
    assert (
        adapter.ingest_receipt(
            node_id, raw, observed_at, operation_id="receipt", expected_revision=1
        )
        == receipt
    )
    result = adapter.propose_completion(
        node_id, [receipt_id], operation_id="assert", expected_revision=2
    )
    assert result["revision"] == 3
    assert result["operation_id"] == "assert"
    assert result["result"]["accepted"] is accepted
    context = adapter.context()
    assert context["revision"] == 3
    assert isinstance(context["context"], str)
    assert ("[FACT]" in context["context"]) is accepted
    if accepted:
        fact = result["result"]["fact"]
        assert isinstance(fact, dict)
        assert fact["text"] == "The expected refund has a matching successful receipt."


@pytest.mark.parametrize(
    "change",
    [
        {"tenant_id": "other-merchant"},
        {"permissions": {"task:write", "context:read"}},
    ],
)
def test_adapter_rejects_unscoped_or_privileged_model_role(
    tmp_path: Path, change: dict[str, object]
) -> None:
    # EGM 0.6's constructor has no annotations; this does not suppress project checks.
    app = EvidenceApplication(SqliteProvider(tmp_path))  # type: ignore[no-untyped-call]
    host = Principal(
        subject="host",
        tenant_id="merchant-1",
        case_ids=frozenset({"case-1"}),
        permissions=frozenset({"task:write"}),
    )
    connector = Principal(
        subject="connector",
        tenant_id="merchant-1",
        case_ids=frozenset({"case-1"}),
        permissions=frozenset({"evidence:write"}),
        source_systems=frozenset({"refund_api"}),
    )
    model_config: dict[str, object] = dict(
        subject="model",
        tenant_id="merchant-1",
        case_ids={"case-1"},
        permissions={"claim:write", "context:read"},
    )
    model_config.update(change)
    with pytest.raises(ValueError):
        AftercareEvidence(
            app,
            "case-1",
            orchestrator=host,
            connector=connector,
            model_tools=Principal.model_validate(model_config),
        )
