"""Strict, transport-neutral parsing for an OpenAI Responses-shaped result.

The adapter deliberately stops at a wire boundary.  A real HTTP client may
implement :class:`ModelClient`, but credentials, retries and tool execution
remain outside this module.  Runtime code receives only validated events.
"""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, Self

from pydantic import Field, ValidationError, model_validator

from aftercare_agent.domain.common import ContractModel, ContractViolation, ErrorCode, Identifier

if TYPE_CHECKING:
    from .budget import ModelPricing, ModelUsageBudget


class ModelClient(Protocol):
    """Provider transport boundary; implementations must not execute tools.

    ``ResponsesAdapter`` calls ``create(model=..., input=..., store=...,
    tools=...)``, which is the keyword shape both a provider SDK and this
    repository's own HTTP transport expose.  Naming that boundary is what lets
    a scripted stand-in and a real endpoint be swapped without the parser
    noticing.
    """

    def create(self, **payload: object) -> Mapping[str, object]:
        """Return one provider response without mutating Aftercare state."""


class ResponseUsage(ContractModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def total_matches(self) -> Self:
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("Responses usage total_tokens is inconsistent")
        return self


@dataclass(frozen=True)
class ResponseEvent:
    """A validated Runtime event, never an executable tool result."""

    kind: str
    text: str | None = None
    call_id: str | None = None
    name: str | None = None
    arguments: Mapping[str, object] | None = None


class ToolCall(ContractModel):
    call_id: Identifier
    name: Identifier
    arguments: Mapping[str, object]


class HostedToolEvent(ContractModel):
    type: Identifier
    payload: Mapping[str, object]


class NormalizedResponse(ContractModel):
    response_id: Identifier
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    hosted_tool_events: tuple[HostedToolEvent, ...] = ()
    usage: ResponseUsage | None = None
    native_json: str
    events: tuple[ResponseEvent, ...] = ()


class ResponsesInputItem(ContractModel):
    """One verified turn rendered as a provider-shaped input item.

    Only roles that Responses represents as a plain message item are allowed.
    Tool output is not a message: the provider protocol expects a
    ``function_call_output`` item carrying the originating ``call_id``, which a
    durable ``SessionMessage`` reference does not hold.  Mapping it here would
    invent protocol semantics, so such a transcript is rejected instead.
    """

    role: Literal["user", "assistant", "system"]
    content: str = Field(min_length=1)


type ResponsesInput = str | tuple[ResponsesInputItem, ...]


class ToolSpec(ContractModel):
    """One provider-facing tool declaration.

    The parameter schema is part of the declaration on purpose: a function
    sent without properties can only ever be called with an empty argument
    object, which makes it useless for something like ``lookup_order``.
    """

    name: Identifier
    parameters: Mapping[str, object] = Field(
        default_factory=lambda: {"type": "object", "additionalProperties": False}
    )


class ResponsesRequest(ContractModel):
    model: Identifier
    input: ResponsesInput
    tools: tuple[ToolSpec, ...] = ()

    @model_validator(mode="after")
    def input_is_present(self) -> Self:
        if isinstance(self.input, str):
            if not self.input:
                raise ValueError("Responses input must not be empty")
        elif not self.input:
            raise ValueError("Responses input item list must not be empty")
        return self


class NormalizedError(ContractModel):
    category: Identifier
    retryable: bool
    message: str


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ContractViolation(ErrorCode.INVALID_INPUT, f"Responses {label} must be an object")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractViolation(ErrorCode.INVALID_INPUT, f"Responses {label} must be non-empty")
    return value


def _usage(raw: Mapping[str, object]) -> ResponseUsage:
    usage = _mapping(raw.get("usage"), "usage")
    try:
        input_tokens = usage["input_tokens"]
        output_tokens = usage["output_tokens"]
    except KeyError as exc:
        raise ContractViolation(ErrorCode.INVALID_INPUT, "Responses usage is incomplete") from exc
    if type(input_tokens) is not int or type(output_tokens) is not int:
        raise ContractViolation(ErrorCode.INVALID_INPUT, "Responses usage tokens must be integers")
    total = usage.get("total_tokens", input_tokens + output_tokens)
    if type(total) is not int:
        raise ContractViolation(
            ErrorCode.INVALID_INPUT, "Responses total_tokens must be an integer"
        )
    try:
        return ResponseUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total,
        )
    except ValidationError as exc:
        raise ContractViolation(
            ErrorCode.INVALID_INPUT, "Responses usage total_tokens is inconsistent"
        ) from exc


def _message(item: Mapping[str, object]) -> ResponseEvent:
    if item.get("status") not in (None, "completed"):
        raise ContractViolation(ErrorCode.INVALID_INPUT, "Responses message is not complete")
    content = item.get("content")
    if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
        raise ContractViolation(ErrorCode.INVALID_INPUT, "Responses message content must be a list")
    texts: list[str] = []
    for block in content:
        value = _mapping(block, "message content block")
        if value.get("type") != "output_text":
            raise ContractViolation(ErrorCode.INVALID_INPUT, "unsupported Responses content block")
        texts.append(_string(value.get("text"), "output_text"))
    return ResponseEvent(kind="message", text="".join(texts))


def _function_call(
    item: Mapping[str, object], allowed_tools: frozenset[str]
) -> tuple[ResponseEvent, ToolCall]:
    name = _string(item.get("name"), "function name")
    if name not in allowed_tools:
        raise ContractViolation(ErrorCode.FORBIDDEN, f"tool is not allowed: {name}")
    call_id = _string(item.get("call_id"), "function call_id")
    arguments_text = item.get("arguments")
    if not isinstance(arguments_text, str):
        raise ContractViolation(ErrorCode.INVALID_INPUT, "function arguments must be JSON text")
    try:
        arguments = json.loads(arguments_text)
    except json.JSONDecodeError as exc:
        raise ContractViolation(
            ErrorCode.INVALID_INPUT, "function arguments are invalid JSON"
        ) from exc
    if not isinstance(arguments, dict) or any(not isinstance(key, str) for key in arguments):
        raise ContractViolation(ErrorCode.INVALID_INPUT, "function arguments must be a JSON object")
    expected = {
        "lookup_order": {"order_id"},
        "lookup_tracking": {"order_id"},
        "lookup_buyer_message": set(),
        "request_material_draft": {"questions"},
    }.get(name)
    if expected is not None and set(arguments) - expected:
        raise ContractViolation(
            ErrorCode.INVALID_INPUT, "function arguments contain unknown fields"
        )
    if item.get("status") not in (None, "completed"):
        raise ContractViolation(ErrorCode.INVALID_INPUT, "function call is not complete")
    return (
        ResponseEvent(kind="function_call", call_id=call_id, name=name, arguments=arguments),
        ToolCall(call_id=call_id, name=name, arguments=arguments),
    )


def _auditable(response: Mapping[str, object]) -> Mapping[str, object]:
    """Return the envelope with chain-of-thought text removed.

    ``native_json`` is persisted for audit and replay, so it must not become a
    back door for the private reasoning that ``events`` deliberately drops.
    The reasoning item itself survives, so an auditor can still see that the
    provider reasoned before answering.
    """

    output = response.get("output")
    if not isinstance(output, Sequence) or isinstance(output, (str, bytes)):
        return response
    if not any(isinstance(item, Mapping) and item.get("type") == "reasoning" for item in output):
        return response
    trimmed: list[object] = [
        {key: value for key, value in item.items() if key != "content"}
        if isinstance(item, Mapping) and item.get("type") == "reasoning"
        else item
        for item in output
    ]
    return {**response, "output": trimmed}


def normalize_response(
    raw: Mapping[str, object], *, allowed_tools: frozenset[str] = frozenset()
) -> NormalizedResponse:
    """Validate one complete response and normalize only supported output items."""

    response = _mapping(raw, "response")
    response_id = _string(response.get("id"), "id")
    # A provider envelope carries plenty of metadata this runtime never reads
    # (sampling knobs, moderation, cache hints, service tier...).  Rejecting
    # unknown top-level keys made every real response unparseable, and a new
    # envelope field is not a safety problem: the fields that matter are
    # validated below, and an unrecognised *output* item still fails closed
    # because it may be hiding a tool call.
    if response.get("status") not in (None, "completed"):
        raise ContractViolation(ErrorCode.INVALID_INPUT, "Responses response is not complete")
    output = response.get("output")
    if not isinstance(output, Sequence) or isinstance(output, (str, bytes)):
        raise ContractViolation(ErrorCode.INVALID_INPUT, "Responses output must be a list")
    events: list[ResponseEvent] = []
    tool_calls: list[ToolCall] = []
    hosted: list[HostedToolEvent] = []
    texts: list[str] = []
    for raw_item in output:
        item = _mapping(raw_item, "output item")
        item_type = item.get("type")
        if item_type == "message":
            message = _message(item)
            events.append(message)
            if message.text:
                texts.append(message.text)
        elif item_type == "function_call":
            event, tool_call = _function_call(item, allowed_tools)
            events.append(event)
            tool_calls.append(tool_call)
        elif item_type == "reasoning":
            # The provider reasoned before answering.  Record that it happened
            # and drop the content: chain-of-thought is not business evidence,
            # and this repository does not persist private reasoning.
            events.append(ResponseEvent(kind="reasoning"))
        elif item_type in ("web_search_call", "file_search_call"):
            hosted.append(HostedToolEvent(type=item_type, payload=dict(item)))
        else:
            raise ContractViolation(
                ErrorCode.INVALID_INPUT, f"unsupported Responses output type: {item_type}"
            )
    usage = _usage(response) if "usage" in response else None
    return NormalizedResponse(
        response_id=response_id,
        text="".join(texts),
        tool_calls=tuple(tool_calls),
        hosted_tool_events=tuple(hosted),
        usage=usage,
        native_json=json.dumps(
            _auditable(response), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
        events=tuple(events),
    )


def normalize_error(error: BaseException) -> NormalizedError:
    """Classify provider failures while removing key-like material from text."""

    message = re.sub(r"sk-[A-Za-z0-9_-]+", "[REDACTED]", str(error))[:500]
    status = getattr(error, "status_code", None)
    retryable = status in (408, 409, 429, 500, 502, 503, 504)
    return NormalizedError(category="provider", retryable=retryable, message=message)


def _wire_input(value: ResponsesInput) -> str | list[dict[str, str]]:
    """Render a request input for the provider without leaking local models."""

    if isinstance(value, str):
        return value
    return [item.model_dump() for item in value]


class ResponsesAdapter:
    """Thin client wrapper; it never executes a returned function call."""

    def __init__(self, client: object, *, allowed_tools: frozenset[str]) -> None:
        self._client = client
        self._allowed_tools = allowed_tools

    def complete(self, request: ResponsesRequest) -> NormalizedResponse:
        unknown_tools = {tool.name for tool in request.tools} - self._allowed_tools
        if unknown_tools:
            raise ContractViolation(ErrorCode.FORBIDDEN, "request contains an unallowed tool")
        endpoint = getattr(self._client, "responses", None)
        create = getattr(endpoint, "create", None)
        if not callable(create):
            raise ContractViolation(
                ErrorCode.INVALID_INPUT, "Responses client has no create endpoint"
            )
        payload = create(
            model=request.model,
            input=_wire_input(request.input),
            store=False,
            tools=[
                {
                    "type": "function",
                    "name": tool.name,
                    "strict": True,
                    "parameters": dict(tool.parameters),
                }
                for tool in request.tools
            ],
        )
        if not isinstance(payload, Mapping):
            raise ContractViolation(
                ErrorCode.INVALID_INPUT, "Responses client returned a non-object"
            )
        return normalize_response(payload, allowed_tools=self._allowed_tools)

    def complete_with_budget(
        self,
        request: ResponsesRequest,
        budget: "ModelUsageBudget",
        pricing: "ModelPricing",
    ) -> tuple[NormalizedResponse, "ModelUsageBudget"]:
        """Complete a call and return the next chargeable budget snapshot.

        The caller persists the returned snapshot with its checkpoint. A
        response without usage is rejected instead of being treated as free.
        """
        response = self.complete(request)
        if response.usage is None:
            raise ContractViolation(
                ErrorCode.INVALID_INPUT, "Responses usage is required for budgeted calls"
            )
        return response, budget.charge(response.usage, pricing)
