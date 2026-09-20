"""Authenticate normalized source events before they enter the evidence path.

Vendor adapters may use different native webhook schemes.  At the trusted
Aftercare boundary they emit this small, canonical contract: a deployment
owned key id identifies one tenant, source and tool, while the signature binds
that identity to the delivery id, both timestamps and the exact JSON bytes.
Nothing in the signed body can choose its own authority.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Literal, Protocol

from pydantic import Field

from aftercare_agent.domain.common import (
    ContractModel,
    ContractViolation,
    ErrorCode,
    Identifier,
    UtcDatetime,
    utc,
)

from .commerce import (
    BUYER_FACTS_SCHEMA,
    ORDER_FACTS_SCHEMA,
    TRACKING_FACTS_SCHEMA,
    ConnectorAnswer,
)

type EvidenceTool = Literal["lookup_order", "lookup_tracking", "lookup_buyer_message"]

TOOL_SCHEMAS: dict[EvidenceTool, str] = {
    "lookup_order": ORDER_FACTS_SCHEMA,
    "lookup_tracking": TRACKING_FACTS_SCHEMA,
    "lookup_buyer_message": BUYER_FACTS_SCHEMA,
}
AUTHORITY_FIELDS = frozenset({"tenant_id", "source_id", "source_event_id", "subject_id", "tool"})
SIGNATURE_VERSION = "aftercare-source-v1"
MAX_SOURCE_EVENT_BYTES = 262_144


class SourceIdentity(ContractModel):
    """Deployment mapping for one connector credential."""

    tenant_id: Identifier
    source_id: Identifier
    tool: EvidenceTool


class SignedSourceRequest(ContractModel):
    """Canonical connector-to-core event; suitable for an HTTP adapter."""

    key_id: Identifier
    delivery_id: Identifier
    issued_at: UtcDatetime
    observed_at: UtcDatetime
    signature: str = Field(pattern=r"^v1=[0-9a-f]{64}$")
    content: bytes = Field(min_length=2, max_length=MAX_SOURCE_EVENT_BYTES)


@dataclass(frozen=True)
class HmacSourceCredential:
    """A secret and its fixed authority; the secret is excluded from repr."""

    identity: SourceIdentity
    secret: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not 32 <= len(self.secret) <= 4096:
            raise ValueError("source credential secret must contain 32 to 4096 bytes")


@dataclass(frozen=True)
class VerifiedSourceEvent:
    """An authenticated event ready for the existing evidence bridge."""

    key_id: str
    identity: SourceIdentity
    issued_at: datetime
    answer: ConnectorAnswer


class SourceEventVerifier(Protocol):
    def verify(self, request: SignedSourceRequest, *, now: datetime) -> VerifiedSourceEvent: ...


def _timestamp(value: datetime) -> str:
    return utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _signed_bytes(request: SignedSourceRequest) -> bytes:
    fields = (
        SIGNATURE_VERSION,
        request.key_id,
        request.delivery_id,
        _timestamp(request.issued_at),
        _timestamp(request.observed_at),
    )
    return "\n".join(fields).encode("utf-8") + b"\n" + request.content


def sign_source_request(
    secret: bytes,
    *,
    key_id: str,
    delivery_id: str,
    issued_at: datetime,
    observed_at: datetime,
    content: bytes,
) -> str:
    """Create the v1 signature used by an adapter or integration fixture."""
    unsigned = SignedSourceRequest(
        key_id=key_id,
        delivery_id=delivery_id,
        issued_at=issued_at,
        observed_at=observed_at,
        signature="v1=" + "0" * 64,
        content=content,
    )
    digest = hmac.new(secret, _signed_bytes(unsigned), sha256).hexdigest()
    return f"v1={digest}"


def build_signed_source_request(
    secret: bytes,
    *,
    key_id: str,
    delivery_id: str,
    issued_at: datetime,
    observed_at: datetime,
    content: bytes,
) -> SignedSourceRequest:
    """Build a validated signed request for an adapter or integration fixture."""
    signature = sign_source_request(
        secret,
        key_id=key_id,
        delivery_id=delivery_id,
        issued_at=issued_at,
        observed_at=observed_at,
        content=content,
    )
    return SignedSourceRequest(
        key_id=key_id,
        delivery_id=delivery_id,
        issued_at=issued_at,
        observed_at=observed_at,
        signature=signature,
        content=content,
    )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _json_object(content: bytes) -> dict[str, object]:
    try:
        value = json.loads(
            content,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ContractViolation(ErrorCode.EVIDENCE_REJECTED, "source event is not JSON") from exc
    if not isinstance(value, dict):
        raise ContractViolation(ErrorCode.EVIDENCE_REJECTED, "source event is not an object")
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    if canonical != content:
        raise ContractViolation(ErrorCode.EVIDENCE_REJECTED, "source event JSON is not canonical")
    return value


def canonical_source_json(value: Mapping[str, object]) -> bytes:
    """Serialize a normalized source body using the verifier's JSON contract."""

    if not all(isinstance(key, str) for key in value):
        raise ContractViolation(ErrorCode.EVIDENCE_REJECTED, "source event keys must be strings")
    if AUTHORITY_FIELDS.intersection(value):
        raise ContractViolation(ErrorCode.FORBIDDEN, "source event body contains authority fields")
    try:
        return json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ContractViolation(ErrorCode.EVIDENCE_REJECTED, "source event is not JSON") from exc


class HmacSourceEventVerifier:
    """Verify a normalized event and map its key to fixed source authority."""

    def __init__(
        self,
        credentials: dict[str, HmacSourceCredential],
        *,
        max_age: timedelta = timedelta(minutes=5),
        clock_skew: timedelta = timedelta(seconds=30),
    ) -> None:
        if not credentials or any(not key for key in credentials):
            raise ValueError("at least one named source credential is required")
        if max_age <= timedelta(0) or clock_skew < timedelta(0):
            raise ValueError("source signature time windows are invalid")
        self._credentials = dict(credentials)
        self._max_age = max_age
        self._clock_skew = clock_skew

    def verify(self, request: SignedSourceRequest, *, now: datetime) -> VerifiedSourceEvent:
        checked_at = utc(now)
        credential = self._credentials.get(request.key_id)
        if credential is None:
            raise ContractViolation(ErrorCode.UNAUTHENTICATED, "source key is unknown")
        issued_at = utc(request.issued_at)
        if issued_at < checked_at - self._max_age or issued_at > checked_at + self._clock_skew:
            raise ContractViolation(
                ErrorCode.UNAUTHENTICATED, "source signature is outside its window"
            )
        expected = "v1=" + hmac.new(credential.secret, _signed_bytes(request), sha256).hexdigest()
        if not hmac.compare_digest(request.signature, expected):
            raise ContractViolation(ErrorCode.UNAUTHENTICATED, "source signature is invalid")
        observed_at = utc(request.observed_at)
        if observed_at > issued_at + self._clock_skew:
            raise ContractViolation(
                ErrorCode.EVIDENCE_REJECTED, "source event is dated in the future"
            )
        body = _json_object(request.content)
        if AUTHORITY_FIELDS.intersection(body):
            raise ContractViolation(
                ErrorCode.FORBIDDEN, "source event body contains authority fields"
            )
        identity = credential.identity
        if body.get("schema") != TOOL_SCHEMAS[identity.tool]:
            raise ContractViolation(ErrorCode.FORBIDDEN, "source key cannot publish this schema")
        return VerifiedSourceEvent(
            key_id=request.key_id,
            identity=identity,
            issued_at=issued_at,
            answer=ConnectorAnswer(
                tool=identity.tool,
                source_id=identity.source_id,
                source_event_id=request.delivery_id,
                observed_at=observed_at,
                body=body,
            ),
        )


__all__ = [
    "AUTHORITY_FIELDS",
    "EvidenceTool",
    "HmacSourceCredential",
    "HmacSourceEventVerifier",
    "MAX_SOURCE_EVENT_BYTES",
    "SIGNATURE_VERSION",
    "SignedSourceRequest",
    "SourceEventVerifier",
    "SourceIdentity",
    "TOOL_SCHEMAS",
    "VerifiedSourceEvent",
    "build_signed_source_request",
    "canonical_source_json",
    "sign_source_request",
]
