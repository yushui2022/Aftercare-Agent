from datetime import datetime, timezone
import json

import pytest

from aftercare_agent.evidence import AftercareEvidence, RefundIntent
from evidence_gated_memory.application import EvidenceApplication, Principal
from evidence_gated_memory.storage.providers import SqliteProvider


@pytest.mark.parametrize("status, accepted", [("completed", True), ("failed", False)])
def test_embedded_refund_adapter(tmp_path, status, accepted):
    app = EvidenceApplication(SqliteProvider(tmp_path))
    def actor(subject, permissions, sources=()):
        return Principal(subject=subject, tenant_id="merchant-1", case_ids={"case-1"},
                         permissions=permissions, source_systems=sources)
    adapter = AftercareEvidence(app, "case-1",
        orchestrator=actor("harness", {"task:write"}),
        connector=actor("payments", {"evidence:write"}, {"refund_api"}),
        model_tools=actor("model-tools", {"claim:write", "context:read"}))
    intent = RefundIntent(order_id="order-1", action_id="action-1", amount_minor="2500", currency="USD")
    node = adapter.register_refund(intent, operation_id="create", expected_revision=0)
    raw = json.dumps({**intent.model_dump(), "tenant_id": "merchant-1", "case_id": "case-1",
                      "receipt_id": "receipt-1", "status": status})
    observed_at = datetime.now(timezone.utc)
    receipt = adapter.ingest_receipt(node["result"]["id"], raw, observed_at,
                                    operation_id="receipt", expected_revision=1)
    assert adapter.ingest_receipt(node["result"]["id"], raw, observed_at,
                                  operation_id="receipt", expected_revision=1) == receipt
    result = adapter.propose_completion(node["result"]["id"], [receipt["result"]["id"]],
                                       operation_id="assert", expected_revision=2)
    assert result["result"]["accepted"] is accepted
    assert ("[FACT]" in adapter.context()["context"]) is accepted
    if accepted:
        assert result["result"]["fact"]["text"] == "The expected refund has a matching successful receipt."


@pytest.mark.parametrize("change", [
    {"tenant_id": "other-merchant"}, {"permissions": {"task:write", "context:read"}},
])
def test_adapter_rejects_unscoped_or_privileged_model_role(tmp_path, change):
    app = EvidenceApplication(SqliteProvider(tmp_path))
    host = Principal(subject="host", tenant_id="merchant-1", case_ids={"case-1"},
                     permissions={"task:write"})
    connector = Principal(subject="connector", tenant_id="merchant-1", case_ids={"case-1"},
                          permissions={"evidence:write"}, source_systems={"refund_api"})
    model_config = dict(subject="model", tenant_id="merchant-1", case_ids={"case-1"},
                        permissions={"claim:write", "context:read"})
    model_config.update(change)
    with pytest.raises(ValueError):
        AftercareEvidence(app, "case-1", orchestrator=host, connector=connector,
                          model_tools=Principal(**model_config))
