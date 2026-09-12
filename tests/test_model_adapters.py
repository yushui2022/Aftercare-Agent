from __future__ import annotations

from typing import cast

import pytest

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.model_adapters.responses import (
    ResponsesAdapter,
    ResponsesRequest,
    normalize_error,
    normalize_response,
)


def response(
    *events: dict[str, object], usage: dict[str, object] | None = None
) -> dict[str, object]:
    value: dict[str, object] = {
        "id": "resp_1",
        "object": "response",
        "created_at": 1,
        "model": "model-1",
        "status": "completed",
        "output": list(events),
    }
    if usage is not None:
        value["usage"] = usage
    return value


def function_call(
    *, name: str = "lookup_order", arguments: str = "{}", status: str = "completed"
) -> dict[str, object]:
    return {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_1",
        "name": name,
        "arguments": arguments,
        "status": status,
    }


def test_normalize_response_extracts_text_tool_and_usage_without_executing() -> None:
    normalized = normalize_response(
        response(
            {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "需要补充材料"}],
            },
            function_call(
                name="request_material_draft", arguments='{"questions":["confirm_address"]}'
            ),
            usage={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
        ),
        allowed_tools=frozenset({"request_material_draft"}),
    )
    assert normalized.response_id == "resp_1"
    assert normalized.text == "需要补充材料"
    assert normalized.tool_calls[0].name == "request_material_draft"
    assert normalized.usage is not None and normalized.usage.total_tokens == 14
    assert '"function_call"' in normalized.native_json


def test_hosted_tool_is_retained_but_not_returned_as_local_intent() -> None:
    normalized = normalize_response(
        response(
            {
                "type": "web_search_call",
                "id": "search_1",
                "status": "completed",
                "action": {"query": "tracking"},
            }
        )
    )
    assert len(normalized.hosted_tool_events) == 1
    assert not normalized.tool_calls


@pytest.mark.parametrize(
    "event,code",
    [
        (function_call(name="unknown_tool"), ErrorCode.FORBIDDEN),
        (function_call(arguments='{"unexpected":true}'), ErrorCode.INVALID_INPUT),
        (function_call(arguments="{", status="completed"), ErrorCode.INVALID_INPUT),
        (function_call(status="in_progress"), ErrorCode.INVALID_INPUT),
    ],
)
def test_incomplete_unknown_or_invalid_tools_are_rejected(
    event: dict[str, object], code: ErrorCode
) -> None:
    with pytest.raises(ContractViolation) as error:
        normalize_response(response(event), allowed_tools=frozenset({"lookup_order"}))
    assert error.value.code is code


def test_unknown_native_fields_and_incoherent_usage_are_rejected() -> None:
    payload = response(function_call())
    payload["new_provider_field"] = True
    with pytest.raises(ContractViolation) as error:
        normalize_response(payload, allowed_tools=frozenset({"lookup_order"}))
    assert error.value.code is ErrorCode.INVALID_INPUT

    with pytest.raises(ContractViolation):
        normalize_response(
            response(usage={"input_tokens": 3, "output_tokens": 3, "total_tokens": 2})
        )


class FakeEndpoint:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.kwargs: dict[str, object] | None = None

    def create(self, **kwargs: object) -> object:
        self.kwargs = kwargs
        return self.payload


class FakeClient:
    def __init__(self, payload: object) -> None:
        self.responses = FakeEndpoint(payload)


def test_client_protocol_keeps_store_false_and_scoped_tool_names() -> None:
    client = FakeClient(response())
    adapter = ResponsesAdapter(client, allowed_tools=frozenset({"lookup_order"}))
    result = adapter.complete(
        ResponsesRequest(model="model-1", input="inspect order", tools=("lookup_order",))
    )
    assert result.response_id == "resp_1"
    assert client.responses.kwargs is not None
    assert client.responses.kwargs["store"] is False
    tools = cast(list[dict[str, object]], client.responses.kwargs["tools"])
    assert tools[0]["name"] == "lookup_order"
    assert tools[0]["strict"] is True


def test_error_normalization_redacts_credentials_and_classifies_retry() -> None:
    class ProviderError(Exception):
        status_code = 503

    error = normalize_error(ProviderError("temporary sk-abcdefghijklmnop failure"))
    assert error.category == "provider"
    assert error.retryable is True
    assert "sk-" not in error.message
