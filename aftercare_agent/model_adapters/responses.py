"""Strict, transport-neutral parsing for an OpenAI Responses-shaped result.

The adapter deliberately stops at a wire boundary.  A real HTTP client may
implement :class:`ModelClient`, but credentials, retries and tool execution
remain outside this module.  Runtime code receives only validated events.
"""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, Self

from pydantic import Field, ValidationError, model_validator

from aftercare_agent.domain.common import ContractModel, ContractViolation, ErrorCode, Identifier


class ModelClient(Protocol):
    """Provider transport boundary; implementations must not execute tools."""

    def create(self, request: Mapping[str, object]) -> Mapping[str, object]:
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


class ResponsesRequest(ContractModel):
    model: Identifier
    input: str = Field(min_length=1)
    tools: tuple[Identifier, ...] = ()


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


def normalize_response(
    raw: Mapping[str, object], *, allowed_tools: frozenset[str] = frozenset()
) -> NormalizedResponse:
    """Validate one complete response and normalize only supported output items."""

    response = _mapping(raw, "response")
    response_id = _string(response.get("id"), "id")
    allowed_keys = {"id", "object", "created_at", "model", "status", "output", "usage", "error"}
    unknown = set(response) - allowed_keys
    if unknown:
        raise ContractViolation(ErrorCode.INVALID_INPUT, "Responses contains unknown fields")
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
        native_json=json.dumps(response, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        events=tuple(events),
    )


def normalize_error(error: BaseException) -> NormalizedError:
    """Classify provider failures while removing key-like material from text."""

    message = re.sub(r"sk-[A-Za-z0-9_-]+", "[REDACTED]", str(error))[:500]
    status = getattr(error, "status_code", None)
    retryable = status in (408, 409, 429, 500, 502, 503, 504)
    return NormalizedError(category="provider", retryable=retryable, message=message)


class ResponsesAdapter:
    """Thin client wrapper; it never executes a returned function call."""

    def __init__(self, client: object, *, allowed_tools: frozenset[str]) -> None:
        self._client = client
        self._allowed_tools = allowed_tools

    def complete(self, request: ResponsesRequest) -> NormalizedResponse:
        unknown_tools = set(request.tools) - self._allowed_tools
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
            input=request.input,
            store=False,
            tools=[
                {
                    "type": "function",
                    "name": name,
                    "strict": True,
                    "parameters": {"type": "object", "additionalProperties": False},
                }
                for name in request.tools
            ],
        )
        if not isinstance(payload, Mapping):
            raise ContractViolation(
                ErrorCode.INVALID_INPUT, "Responses client returned a non-object"
            )
        return normalize_response(payload, allowed_tools=self._allowed_tools)
