"""A real provider envelope must stay parseable.

These fixtures are captures from a live DeepSeek Responses endpoint, with the
chain-of-thought text removed.  They exist because the parser previously
rejected *every* real response: it treated an unrecognised envelope key as a
protocol violation, and had no case for the ``reasoning`` item that a
reasoning model emits before its answer.
"""

from __future__ import annotations

import json
from pathlib import Path

from aftercare_agent.model_adapters.responses import normalize_response

FIXTURES = Path(__file__).parent / "fixtures" / "model_wire"
ALLOWED = frozenset({"lookup_order"})


def _capture(name: str) -> dict[str, object]:
    raw = json.loads((FIXTURES / f"deepseek_{name}.json").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def test_reasoning_envelope_parses_without_storing_the_chain_of_thought() -> None:
    normalized = normalize_response(_capture("text"), allowed_tools=ALLOWED)
    assert normalized.text == "ok"
    assert [event.kind for event in normalized.events] == ["reasoning", "message"]
    assert normalized.events[0].text is None
    # The audit copy keeps the reasoning item but never its content.
    assert '"type":"reasoning"' in normalized.native_json
    assert "reasoning text removed" not in normalized.native_json


def test_live_function_call_arguments_are_extracted_unchanged() -> None:
    normalized = normalize_response(_capture("tool"), allowed_tools=ALLOWED)
    assert [call.name for call in normalized.tool_calls] == ["lookup_order"]
    assert dict(normalized.tool_calls[0].arguments) == {"order_id": "A-1001"}
    assert normalized.usage is not None and normalized.usage.total_tokens > 0
