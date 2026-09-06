"""Trusted worker-side EGM adapter. No HTTP server is required.

Only expose propose_completion/context to model tools. Registration and receipt
ingestion belong to the trusted business orchestrator and provider connector.
"""
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from evidence_gated_memory.application import EvidenceApplication, Principal


class RefundIntent(BaseModel):
    """Expected action copied from the ledger, not proof of authorization."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    order_id: str = Field(min_length=1, max_length=160)
    action_id: str = Field(min_length=1, max_length=160)
    amount_minor: str = Field(pattern=r"^[1-9][0-9]*$")
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class AftercareEvidence:
    def __init__(self, application: EvidenceApplication, case_id: str, *,
                 orchestrator: Principal, connector: Principal, model_tools: Principal):
        if len({p.tenant_id for p in (orchestrator, connector, model_tools)}) != 1:
            raise ValueError("all roles must belong to the same authenticated tenant")
        if not model_tools.permissions <= {"context:read", "claim:write"}:
            raise ValueError("model tool principal must not have privileged write permissions")
        self.application = application
        self.case_id = case_id
        self.orchestrator = orchestrator
        self.connector = connector
        self.model_tools = model_tools

    def register_refund(self, intent: RefundIntent, *, operation_id: str, expected_revision: int):
        return self.application.create_node(self.orchestrator, self.case_id, {
            "operation_id": operation_id, "expected_revision": expected_revision,
            "node_type": "refund_completion", "title": "Verify expected refund receipt",
            "anchors": intent.model_dump(),
        })

    def ingest_receipt(self, node_id: str, raw_normalized_json: str, observed_at: datetime,
                       *, operation_id: str, expected_revision: int):
        return self.application.ingest(self.connector, self.case_id, {
            "operation_id": operation_id, "expected_revision": expected_revision,
            "node_id": node_id, "evidence_type": "refund_api_response",
            "source_system": "refund_api", "observed_at": observed_at,
            "content": raw_normalized_json,
        })

    def propose_completion(self, node_id: str, evidence_refs: list[str], *,
                           operation_id: str, expected_revision: int):
        # No model-authored order, amount, success status or free-form fact text.
        return self.application.assert_fact(self.model_tools, self.case_id, {
            "operation_id": operation_id, "expected_revision": expected_revision,
            "node_id": node_id, "claim_type": "refund_completed",
            "text": "The expected refund has a matching successful receipt.",
            "evidence_refs": evidence_refs,
        })

    def context(self):
        return self.application.context(self.model_tools, self.case_id)
