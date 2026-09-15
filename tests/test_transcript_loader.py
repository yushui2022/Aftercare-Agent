"""Transcript resolution remains scoped, ordered and digest-verified."""

from datetime import UTC, datetime
from hashlib import sha256

import pytest

from aftercare_agent.domain.common import CaseScope, ContractViolation, ErrorCode
from aftercare_agent.domain.runtime import SessionMessage
from aftercare_agent.model_adapters import (
    ResolvedTranscriptMessage,
    SessionTranscriptLoader,
    build_responses_input,
)


class Resolver:
    def __init__(self, values: dict[str, bytes]) -> None:
        self.values = values

    def read(self, reference_id: str) -> bytes:
        return self.values[reference_id]


def message(seq: int, ref: str, content: bytes) -> SessionMessage:
    return SessionMessage(
        tenant_id="tenant-1",
        case_id="case-1",
        session_id="session-1",
        message_id=f"message-{seq}",
        message_seq=seq,
        role="user" if seq == 1 else "assistant",
        message_ref=ref,
        message_sha256=sha256(content).hexdigest(),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_loader_resolves_verified_ordered_messages() -> None:
    resolver = Resolver({"a": b"hello", "b": b"world"})
    loaded = SessionTranscriptLoader(resolver).load(
        CaseScope(tenant_id="tenant-1", case_id="case-1"),
        "session-1",
        [message(1, "a", b"hello"), message(2, "b", b"world")],
    )
    assert [(item.message_seq, item.role, item.content) for item in loaded] == [
        (1, "user", "hello"),
        (2, "assistant", "world"),
    ]


def test_loader_rejects_scope_gap_and_digest_mismatch() -> None:
    scope = CaseScope(tenant_id="tenant-1", case_id="case-1")
    loader = SessionTranscriptLoader(Resolver({"a": b"hello"}))
    with pytest.raises(ContractViolation) as error:
        loader.load(scope, "session-1", [message(2, "a", b"hello")])
    assert error.value.code is ErrorCode.CONFLICT
    wrong = message(1, "a", b"other")
    with pytest.raises(ContractViolation) as error:
        loader.load(scope, "session-1", [wrong])
    assert error.value.code is ErrorCode.CONFLICT


def test_loader_rejects_cross_case_and_oversized_artifact() -> None:
    scope = CaseScope(tenant_id="tenant-1", case_id="case-1")
    loader = SessionTranscriptLoader(Resolver({"a": b"hello"}), max_message_bytes=4)
    with pytest.raises(ContractViolation) as error:
        loader.load(scope, "session-1", [message(1, "a", b"hello")])
    assert error.value.code is ErrorCode.BUDGET_EXHAUSTED
    foreign = message(1, "a", b"hello").model_copy(update={"case_id": "case-2"})
    with pytest.raises(ContractViolation) as error:
        SessionTranscriptLoader(Resolver({"a": b"hello"})).load(scope, "session-1", [foreign])
    assert error.value.code is ErrorCode.FORBIDDEN


def load_one() -> tuple[ResolvedTranscriptMessage, ...]:
    return SessionTranscriptLoader(Resolver({"a": b"hello"})).load(
        CaseScope(tenant_id="tenant-1", case_id="case-1"), "session-1", [message(1, "a", b"hello")]
    )


def test_responses_input_renders_verified_turns_in_order() -> None:
    items = build_responses_input(load_one())
    assert [(item.role, item.content) for item in items] == [("user", "hello")]


def test_responses_input_rejects_unrepresentable_role_and_mixed_scope() -> None:
    loaded = load_one()
    with pytest.raises(ContractViolation) as error:
        build_responses_input([loaded[0].model_copy(update={"role": "tool"})])
    assert error.value.code is ErrorCode.INVALID_INPUT
    with pytest.raises(ContractViolation) as error:
        build_responses_input([loaded[0], loaded[0].model_copy(update={"session_id": "session-2"})])
    assert error.value.code is ErrorCode.FORBIDDEN


def test_responses_input_rejects_empty_blank_and_gapped_transcripts() -> None:
    loaded = load_one()
    with pytest.raises(ContractViolation) as error:
        build_responses_input([])
    assert error.value.code is ErrorCode.INVALID_INPUT
    with pytest.raises(ContractViolation) as error:
        build_responses_input([loaded[0].model_copy(update={"content": ""})])
    assert error.value.code is ErrorCode.INVALID_INPUT
    with pytest.raises(ContractViolation) as error:
        build_responses_input([loaded[0].model_copy(update={"message_seq": 2})])
    assert error.value.code is ErrorCode.CONFLICT
