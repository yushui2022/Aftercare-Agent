"""Environment-driven composition of the model, sandbox and connector slice.

Everything a deployment has to decide -- provider credentials, the imported
export, the artifact root, source identities, pricing and the harness budget
-- is read in this module and nowhere else, so the modules it wires stay
constructible from a unit test.  Nothing here opens a database connection:
the trusted host injects the case binding, and a coordinate that costs money
or changes what a Run can reach has no default to fall back on.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import timedelta
from functools import partial
from typing import cast

from aftercare_agent.artifacts import ContentAddressedArtifactStore
from aftercare_agent.connectors.commerce import CommerceConnector, CommerceSources
from aftercare_agent.connectors.dataset import load_commerce_dataset
from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.protocol import Checkpoint
from aftercare_agent.model_adapters import (
    DEFAULT_MODEL,
    ModelPricing,
    ProviderEndpoint,
    ResponsesAdapter,
    ResponsesHttpClient,
    ResponsesInputItem,
    ToolSpec,
)
from aftercare_agent.model_adapters.responses import ResponsesInput
from aftercare_agent.sandbox import FakeSandboxProvider, SandboxSpec
from aftercare_agent.sandbox.manager import SandboxManager, SandboxTierPolicy

from .model_harness import HarnessLimits, run_model_harness
from .sandbox_executor import CaseBinding, SandboxedToolExecutor
from .worker import Harness

DEFAULT_TIERS = SandboxTierPolicy(
    tiers={
        "lookup": SandboxSpec(
            image="aftercare-tools", cpu_millis=250, memory_mib=256, max_artifact_bytes=262_144
        ),
        "draft": SandboxSpec(
            image="aftercare-tools", cpu_millis=500, memory_mib=512, max_artifact_bytes=262_144
        ),
    }
)

TOOL_TIERS: Mapping[str, str] = {
    "lookup_order": "lookup",
    "lookup_tracking": "lookup",
    "request_material_draft": "draft",
}

_NO_ARGUMENTS: Mapping[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {},
}
_DRAFT_ARGUMENTS: Mapping[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "questions": {
            "type": "array",
            "minItems": 1,
            "maxItems": 3,
            "items": {
                "type": "string",
                "enum": [
                    "confirm_address",
                    "check_delivery_location",
                    "provide_delivery_photo",
                ],
            },
        }
    },
    "required": ["questions"],
}

INVESTIGATION_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(name="lookup_order", parameters=_NO_ARGUMENTS),
    ToolSpec(name="lookup_tracking", parameters=_NO_ARGUMENTS),
    ToolSpec(name="request_material_draft", parameters=_DRAFT_ARGUMENTS),
)

SYSTEM_PROMPT = (
    "You investigate one after-sales case. The order is already bound to this Run, "
    "so ask for facts by tool name and never invent an order id, amount or date. "
    "Call one tool per turn. When the facts are enough, answer in one short paragraph."
)


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ContractViolation(ErrorCode.INVALID_INPUT, f"{name} must be configured")
    return value


def _positive_int(env: Mapping[str, str], name: str, default: str | None = None) -> int:
    raw = env.get(name, "").strip() or default
    if raw is None:
        raise ContractViolation(ErrorCode.INVALID_INPUT, f"{name} must be configured")
    try:
        value = int(raw)
    except ValueError as exc:
        raise ContractViolation(ErrorCode.INVALID_INPUT, f"{name} must be an integer") from exc
    if value < 1:
        raise ContractViolation(ErrorCode.INVALID_INPUT, f"{name} must be positive")
    return value


def _seconds(env: Mapping[str, str], name: str, default: str) -> timedelta:
    return timedelta(seconds=_positive_int(env, name, default))


def build_sandbox_executor(
    env: Mapping[str, str], *, owner: str, case_binding: CaseBinding
) -> SandboxedToolExecutor:
    """Wire a read-only connector to tiered sandboxes and a content-addressed store."""
    dataset = load_commerce_dataset(_required(env, "AFTERCARE_COMMERCE_DATASET"))
    connector = CommerceConnector(
        dataset,
        sources=CommerceSources(
            order_ledger=_required(env, "AFTERCARE_SOURCE_ORDER_LEDGER"),
            carrier=_required(env, "AFTERCARE_SOURCE_CARRIER"),
            buyer_channel=_required(env, "AFTERCARE_SOURCE_BUYER_CHANNEL"),
        ),
    )
    store = ContentAddressedArtifactStore(
        _required(env, "AFTERCARE_ARTIFACT_ROOT"),
        max_bytes=_positive_int(env, "AFTERCARE_ARTIFACT_MAX_BYTES", "1048576"),
    )
    manager = SandboxManager(
        FakeSandboxProvider(capacity=_positive_int(env, "AFTERCARE_SANDBOX_CAPACITY", "4")),
        DEFAULT_TIERS,
        lease=_seconds(env, "AFTERCARE_SANDBOX_LEASE_SECONDS", "60"),
    )
    return SandboxedToolExecutor(
        manager=manager,
        connector=connector,
        store=store,
        bindings=case_binding,
        tiers=TOOL_TIERS,
        owner=owner,
        max_result_bytes=_positive_int(env, "AFTERCARE_SANDBOX_RESULT_BYTES", "262144"),
    )


def make_render_input(
    store: ContentAddressedArtifactStore,
    case_binding: CaseBinding,
    *,
    max_result_bytes: int = 65_536,
) -> Callable[[Checkpoint], ResponsesInput]:
    """Render prior tool evidence for the model, re-reading it from the store.

    The transcript arrives through the same scope-checked, digest-checked read
    path an auditor would use, so a checkpoint cannot smuggle bytes into a
    prompt that the store would not hand back.
    """

    def render(checkpoint: Checkpoint) -> ResponsesInput:
        order_id = case_binding.order_id_for(checkpoint)
        items = [
            ResponsesInputItem(role="system", content=SYSTEM_PROMPT),
            ResponsesInputItem(
                role="user",
                content=(
                    f"Case {checkpoint.case_id}, order {order_id}. "
                    f"Tool calls left: {checkpoint.remaining_budget.tool_calls}."
                ),
            ),
        ]
        for result in checkpoint.tool_results:
            raw = store.read(checkpoint, result.artifact)
            if len(raw) > max_result_bytes:
                raise ContractViolation(
                    ErrorCode.BUDGET_EXHAUSTED, "tool result is too large to render"
                )
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ContractViolation(
                    ErrorCode.INVALID_INPUT, "tool result is not UTF-8"
                ) from exc
            items.append(ResponsesInputItem(role="user", content=f"tool {result.call_id}: {text}"))
        return tuple(items)

    return render


def build_model_harness(
    env: Mapping[str, str],
    *,
    executor: SandboxedToolExecutor,
    case_binding: CaseBinding,
    store: ContentAddressedArtifactStore,
    max_steps: int = 8,
) -> Harness:
    """Build the model-driven Harness, with credentials resolved here only."""
    endpoint = ProviderEndpoint.from_env(env)
    adapter = ResponsesAdapter(
        ResponsesHttpClient(endpoint),
        allowed_tools=frozenset(tool.name for tool in INVESTIGATION_TOOLS),
    )
    pricing = ModelPricing(
        input_microusd_per_token=_positive_int(env, "AFTERCARE_MODEL_INPUT_MICROUSD_PER_TOKEN"),
        # Neither price has a default: a guessed rate would mis-budget real
        # money silently, so an unpriced deployment stops instead.
        output_microusd_per_token=_positive_int(env, "AFTERCARE_MODEL_OUTPUT_MICROUSD_PER_TOKEN"),
    )
    return cast(
        Harness,
        partial(
            run_model_harness,
            max_steps=max_steps,
            adapter=adapter,
            model=env.get("AFTERCARE_MODEL", "").strip() or DEFAULT_MODEL,
            tools=INVESTIGATION_TOOLS,
            limits=HarnessLimits(
                model_calls=_positive_int(env, "AFTERCARE_MODEL_CALLS", "8"),
                tool_calls=_positive_int(env, "AFTERCARE_TOOL_CALLS", "4"),
                cost_microusd=_positive_int(env, "AFTERCARE_MODEL_COST_MICROUSD", "1000000"),
                ttl=_seconds(env, "AFTERCARE_HARNESS_TTL_SECONDS", "300"),
            ),
            executor=executor,
            render_input=make_render_input(store, case_binding),
            pricing=pricing,
        ),
    )


def harness_from_environment(
    *, owner: str, case_binding: CaseBinding, max_steps: int = 8
) -> Harness:
    """Compose the whole slice from the process environment."""
    env = dict(os.environ)
    executor = build_sandbox_executor(env, owner=owner, case_binding=case_binding)
    return build_model_harness(
        env,
        executor=executor,
        case_binding=case_binding,
        store=executor.store,
        max_steps=max_steps,
    )
