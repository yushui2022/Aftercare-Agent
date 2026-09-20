"""The tool execution path: validated intent, leased sandbox, stored evidence."""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from importlib.resources import files
from pathlib import Path

import pytest

from aftercare_agent.artifacts import ContentAddressedArtifactStore
from aftercare_agent.connectors import CommerceConnector, CommerceSources, load_commerce_dataset
from aftercare_agent.domain.common import ContractViolation, ErrorCode, RunScope
from aftercare_agent.domain.protocol import Checkpoint, ToolRequest
from aftercare_agent.model_adapters import ModelPricing, ResponsesAdapter
from aftercare_agent.runtime.harness import HarnessResult
from aftercare_agent.runtime.model_harness import HarnessLimits, run_model_harness
from aftercare_agent.runtime.sandbox_executor import (
    MATERIAL_DRAFT_SCHEMA,
    AnswerObserver,
    SandboxedToolExecutor,
    StaticCaseBinding,
)
from aftercare_agent.runtime.wiring import INVESTIGATION_TOOLS, TOOL_TIERS, make_render_input
from aftercare_agent.sandbox import (
    FakeSandboxProvider,
    SandboxManager,
    SandboxSpec,
    SandboxTierPolicy,
)

SAMPLE = Path(str(files("aftercare_agent.connectors").joinpath("data/commerce-sample.json")))
SOURCES = CommerceSources(order_ledger="erp-api", carrier="carrier-api", buyer_channel="in-app")
SCOPE = RunScope(tenant_id="tenant-demo", case_id="case-1", run_id="run-1")
NOW = datetime(2026, 9, 16, 12, tzinfo=UTC)
LEASE = timedelta(minutes=5)
TIERS = SandboxTierPolicy(
    tiers={
        "lookup": SandboxSpec(image="tools", cpu_millis=250, memory_mib=256),
        "draft": SandboxSpec(image="tools", cpu_millis=500, memory_mib=512),
    }
)
PRICING = ModelPricing(input_microusd_per_token=1, output_microusd_per_token=2)


def _parts(
    tmp_path: Path,
    *,
    order_id: str = "A-1001",
    capacity: int = 1,
    observer: AnswerObserver | None = None,
) -> tuple[SandboxedToolExecutor, ContentAddressedArtifactStore, SandboxManager]:
    store = ContentAddressedArtifactStore(tmp_path / "artifacts")
    manager = SandboxManager(FakeSandboxProvider(capacity=capacity), TIERS, lease=LEASE)
    executor = SandboxedToolExecutor(
        manager=manager,
        connector=CommerceConnector(load_commerce_dataset(SAMPLE), sources=SOURCES),
        store=store,
        bindings=StaticCaseBinding(order_id=order_id),
        tiers=TOOL_TIERS,
        owner="worker-a",
        observer=observer,
    )
    return executor, store, manager


def _request(name: str, arguments: str = "{}", call_id: str = "call-1") -> ToolRequest:
    return ToolRequest(call_id=call_id, name=name, arguments_json=arguments)


class _ScriptedClient:
    """Deterministic provider stand-in that records what it was asked."""

    def __init__(self, payloads: list[Mapping[str, object]]) -> None:
        self.responses = self
        self.requests: list[dict[str, object]] = []
        self._payloads = payloads

    def create(self, **payload: object) -> Mapping[str, object]:
        self.requests.append(dict(payload))
        return self._payloads.pop(0)


def _answer(text: str, *, tokens: int = 5) -> Mapping[str, object]:
    return {
        "id": "resp-answer",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        "usage": {"input_tokens": tokens, "output_tokens": tokens, "total_tokens": 2 * tokens},
    }


def _tool_call(name: str, arguments: str = "{}", call_id: str = "call-1") -> Mapping[str, object]:
    return {
        "id": f"resp-{call_id}",
        "status": "completed",
        "output": [
            {
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": arguments,
                "status": "completed",
            }
        ],
        "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
    }


def test_a_lookup_returns_evidence_addressed_by_its_own_digest(tmp_path: Path) -> None:
    executor, store, _ = _parts(tmp_path)
    reference = executor.execute(_request("lookup_order"), scope=SCOPE, now=NOW)
    assert reference.reference_id == "tool-result:call-1"
    content = store.read(SCOPE, reference)
    assert reference.sha256 == sha256(content).hexdigest()
    assert b'"A-1001"' in content


def test_replaying_a_finished_step_reapplies_the_same_evidence(tmp_path: Path) -> None:
    executor, store, _ = _parts(tmp_path)
    first = executor.execute(_request("lookup_order"), scope=SCOPE, now=NOW)
    replay = executor.execute(_request("lookup_order"), scope=SCOPE, now=NOW)
    assert replay == first
    assert store.read(SCOPE, replay) == store.read(SCOPE, first)


def test_capacity_comes_back_after_success_and_after_failure(tmp_path: Path) -> None:
    executor, _, manager = _parts(tmp_path, order_id="A-9999")
    with pytest.raises(ContractViolation) as error:
        executor.execute(_request("lookup_order"), scope=SCOPE, now=NOW)
    assert error.value.code is ErrorCode.INVALID_INPUT
    assert manager.sweep(now=NOW).active == 0
    replacement = manager.acquire(request_key="next", tier="lookup", owner="worker-a")
    assert replacement.state == "READY"


def test_a_tool_argument_cannot_smuggle_the_order_or_extra_fields(tmp_path: Path) -> None:
    executor, _, _ = _parts(tmp_path)
    with pytest.raises(ContractViolation) as error:
        executor.execute(_request("lookup_order", '{"order_id": "A-1002"}'), scope=SCOPE, now=NOW)
    assert error.value.code is ErrorCode.INVALID_INPUT


def test_a_tool_without_a_tier_never_reaches_a_sandbox(tmp_path: Path) -> None:
    executor, _, manager = _parts(tmp_path)
    with pytest.raises(ContractViolation) as error:
        executor.execute(_request("transfer_funds"), scope=SCOPE, now=NOW)
    assert error.value.code is ErrorCode.FORBIDDEN
    assert manager.sweep(now=NOW).active == 0


def test_a_material_draft_is_pure_and_bounded(tmp_path: Path) -> None:
    executor, store, _ = _parts(tmp_path)
    request = _request("request_material_draft", '{"questions": ["confirm_address"]}')
    first = executor.execute(request, scope=SCOPE, now=NOW)
    assert first == executor.execute(request, scope=SCOPE, now=NOW)
    body = store.read(SCOPE, first).decode("utf-8")
    assert MATERIAL_DRAFT_SCHEMA in body


def test_a_buyer_lookup_reaches_the_observer_as_buyer_evidence(tmp_path: Path) -> None:
    seen: list[dict[str, object]] = []

    def observe(**payload: object) -> None:
        seen.append(payload)

    executor, _, _ = _parts(tmp_path, observer=observe)
    executor.execute(_request("lookup_buyer_message"), scope=SCOPE, now=NOW)
    assert len(seen) == 1
    answer = seen[0]["answer"]
    assert getattr(answer, "tool", None) == "lookup_buyer_message"


def test_the_harness_drives_the_model_through_a_sandboxed_tool(tmp_path: Path) -> None:
    executor, store, manager = _parts(tmp_path)
    binding = StaticCaseBinding(order_id="A-1001")
    client = _ScriptedClient(
        [
            _tool_call("lookup_order"),
            _answer(
                '{"schema_version":1,"claims":[{"claim":"order_recorded",'
                '"evidence_refs":["evidence-1"]}]}'
            ),
        ]
    )
    context_scopes: list[RunScope] = []

    def evidence_context(scope: RunScope) -> str:
        context_scopes.append(scope)
        return '{"schema":"aftercare.investigation-evidence-context.v1","observations":[]}'

    adapter = ResponsesAdapter(
        client, allowed_tools=frozenset(tool.name for tool in INVESTIGATION_TOOLS)
    )
    limits = HarnessLimits(
        model_calls=4, tool_calls=2, cost_microusd=1_000, ttl=timedelta(minutes=5)
    )
    render_input = make_render_input(store, binding, evidence_context=evidence_context)

    def run(checkpoint: Checkpoint | None = None) -> HarnessResult:
        return run_model_harness(
            tenant_id=SCOPE.tenant_id,
            case_id=SCOPE.case_id,
            run_id=SCOPE.run_id,
            checkpoint=checkpoint,
            now=NOW,
            adapter=adapter,
            model="scripted-model",
            tools=INVESTIGATION_TOOLS,
            limits=limits,
            executor=executor,
            render_input=render_input,
            pricing=PRICING,
        )

    proposed = run()
    assert proposed.checkpoint.next_step == "tool"
    observed = run(proposed.checkpoint)
    assert observed.checkpoint.next_step == "model"
    proposed = run(observed.checkpoint)
    assert proposed.checkpoint.next_step == "evaluate"
    result = run(proposed.checkpoint)
    assert not result.completed
    assert result.proposal is not None
    assert result.tool_calls == 1
    checkpoint = result.checkpoint
    assert checkpoint.next_step == "evaluate"
    assert checkpoint.remaining_budget.model_calls == 2
    assert checkpoint.remaining_budget.tool_calls == 1
    assert context_scopes == [SCOPE, SCOPE]
    first_input = client.requests[0]["input"]
    assert isinstance(first_input, list)
    assert "evidence_context_json" in str(first_input[-1]["content"])
    # The evidence reached the model through the store, not through a shortcut.
    second_input = client.requests[1]["input"]
    assert isinstance(second_input, list)
    last_item = second_input[-1]
    assert isinstance(last_item, dict)
    assert "A-1001" in str(last_item["content"])
    # And the sandbox gave its capacity back when the step finished.
    assert manager.sweep(now=NOW).active == 0
