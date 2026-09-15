"""Resolve a persisted Session transcript without trusting provider state.

The database stores only immutable artifact references and digests.  This
module is the small, synchronous seam a trusted Worker can use before making
a model call: it verifies scope, sequence and bytes, then returns validated
messages.  It deliberately does not read a database or execute tools.
"""

from collections.abc import Iterable
from hashlib import sha256
from typing import Literal, Protocol

from aftercare_agent.domain.common import (
    CaseScope,
    ContractViolation,
    ErrorCode,
    Identifier,
    PositiveInt,
    Sha256,
)
from aftercare_agent.domain.runtime import SessionMessage

from .responses import ResponsesInputItem


class ArtifactResolver(Protocol):
    """Trusted artifact boundary; implementations enforce their own ACL."""

    def read(self, reference_id: str) -> bytes:
        """Return the exact immutable bytes addressed by ``reference_id``."""


class ResolvedTranscriptMessage(CaseScope):
    """A verified message suitable for a model adapter input."""

    session_id: Identifier
    message_id: Identifier
    message_seq: PositiveInt
    role: Literal["user", "assistant", "tool", "system"]
    message_ref: Identifier
    message_sha256: Sha256
    content: str


class SessionTranscriptLoader:
    """Verify and resolve one session transcript in sequence order."""

    def __init__(self, resolver: ArtifactResolver, *, max_message_bytes: int = 1_048_576) -> None:
        if type(max_message_bytes) is not int or not 1 <= max_message_bytes <= 16_777_216:
            raise ContractViolation(
                ErrorCode.INVALID_INPUT, "max_message_bytes must be between 1 and 16777216"
            )
        self._resolver = resolver
        self._max_message_bytes = max_message_bytes

    def load(
        self,
        scope: CaseScope,
        session_id: str,
        messages: Iterable[SessionMessage],
    ) -> tuple[ResolvedTranscriptMessage, ...]:
        if not session_id:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "session_id is required")
        resolved: list[ResolvedTranscriptMessage] = []
        expected_seq = 1
        for message in messages:
            if (
                message.tenant_id != scope.tenant_id
                or message.case_id != scope.case_id
                or message.session_id != session_id
            ):
                raise ContractViolation(ErrorCode.FORBIDDEN, "transcript scope mismatch")
            if message.message_seq != expected_seq:
                raise ContractViolation(
                    ErrorCode.CONFLICT,
                    f"transcript sequence must be contiguous; expected {expected_seq}",
                )
            try:
                content_bytes = self._resolver.read(message.message_ref)
            except ContractViolation:
                raise
            except Exception as exc:
                raise ContractViolation(
                    ErrorCode.RETRYABLE, "transcript artifact read failed"
                ) from exc
            if not isinstance(content_bytes, bytes):
                raise ContractViolation(
                    ErrorCode.INVALID_INPUT, "artifact resolver must return bytes"
                )
            if len(content_bytes) > self._max_message_bytes:
                raise ContractViolation(
                    ErrorCode.BUDGET_EXHAUSTED, "transcript message is too large"
                )
            if sha256(content_bytes).hexdigest() != message.message_sha256:
                raise ContractViolation(ErrorCode.CONFLICT, "transcript artifact digest mismatch")
            try:
                content = content_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ContractViolation(
                    ErrorCode.INVALID_INPUT, "transcript artifact is not UTF-8"
                ) from exc
            resolved.append(
                ResolvedTranscriptMessage(
                    tenant_id=message.tenant_id,
                    case_id=message.case_id,
                    session_id=message.session_id,
                    message_id=message.message_id,
                    message_seq=message.message_seq,
                    role=message.role,
                    message_ref=message.message_ref,
                    message_sha256=message.message_sha256,
                    content=content,
                )
            )
            expected_seq += 1
        return tuple(resolved)


def build_responses_input(
    messages: Iterable[ResolvedTranscriptMessage],
) -> tuple[ResponsesInputItem, ...]:
    """Render verified transcript messages as Responses input items.

    Callers are expected to pass the output of :meth:`SessionTranscriptLoader.load`.
    Scope, ordering and role checks are repeated here so that a caller cannot
    splice messages from different sessions, skip sequence numbers or move an
    unrepresentable role into a provider request.
    """

    items: list[ResponsesInputItem] = []
    scope: tuple[str, str, str] | None = None
    expected_seq = 1
    for message in messages:
        current = (message.tenant_id, message.case_id, message.session_id)
        if scope is None:
            scope = current
        elif current != scope:
            raise ContractViolation(ErrorCode.FORBIDDEN, "transcript mixes sessions or cases")
        if message.message_seq != expected_seq:
            raise ContractViolation(
                ErrorCode.CONFLICT,
                f"transcript sequence must be contiguous; expected {expected_seq}",
            )
        if message.role == "tool":
            raise ContractViolation(
                ErrorCode.INVALID_INPUT,
                "tool output requires a function_call_output item with a call_id",
            )
        if not message.content:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "transcript message is empty")
        items.append(ResponsesInputItem(role=message.role, content=message.content))
        expected_seq += 1
    if not items:
        raise ContractViolation(ErrorCode.INVALID_INPUT, "transcript is empty")
    return tuple(items)
