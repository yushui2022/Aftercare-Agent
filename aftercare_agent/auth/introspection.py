"""RFC 7662 token introspection: ask the issuer whether a token is still active.

A locally verified JWT proves only that the issuer signed it at some point in the
past; it says nothing about revocation.  This module is the provider-neutral
boundary that consults a static introspection endpoint, and every failure mode
fails closed: transport errors, oversized or malformed bodies, a missing or
non-boolean ``active`` field, and claims that contradict the verified token.

The endpoint is deployment configuration and is never taken from a token.  HTTP
credentials stay in the injected ``httpx.Client``, so no secret is stored in an
Aftercare contract model or repr.
"""

import hashlib
import math
import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urlparse

import httpx
from pydantic import Field, ValidationError, model_validator

from aftercare_agent.domain.common import (
    ContractModel,
    ContractViolation,
    ErrorCode,
    Identifier,
    UtcDatetime,
)


class IntrospectionConfig(ContractModel):
    """Static policy for the introspection call; no token-controlled URL."""

    endpoint: str
    request_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    cache_seconds: int = Field(default=5, ge=0, le=300)
    max_cache_entries: int = Field(default=4096, ge=1, le=100_000)
    max_response_bytes: int = Field(default=262_144, ge=1024, le=4_194_304)

    @model_validator(mode="after")
    def validate_endpoint(self) -> "IntrospectionConfig":
        parsed = urlparse(self.endpoint)
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("introspection endpoint must be https without query or fragment")
        return self


class IntrospectionVerdict(ContractModel):
    """The provider-neutral subset of an RFC 7662 response that Aftercare uses."""

    active: bool
    subject_id: Identifier | None = None
    tenant_id: Identifier | None = None
    expires_at: UtcDatetime | None = None


def token_fingerprint(token: str) -> str:
    """A stable, non-reversible key for caches; the token itself is never stored."""

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class TokenIntrospector(Protocol):
    def introspect(self, token: str, *, now: datetime) -> IntrospectionVerdict: ...


def _expiry(value: object) -> datetime | None:
    """RFC 7662 ``exp`` is a NumericDate; anything else is rejected, not coerced."""

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractViolation(ErrorCode.UNAUTHENTICATED, "invalid introspection expiry")
    number = float(value)
    if not math.isfinite(number):
        raise ContractViolation(ErrorCode.UNAUTHENTICATED, "invalid introspection expiry")
    try:
        return datetime.fromtimestamp(number, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise ContractViolation(ErrorCode.UNAUTHENTICATED, "invalid introspection expiry") from exc


def parse_verdict(payload: object) -> IntrospectionVerdict:
    """Read only the documented fields; unknown provider fields are ignored.

    RFC 7662 leaves the field set up to the issuer (``scope``, ``client_id``,
    ``token_type``, ``jti``, ...), so unknown keys are tolerated.  A field that
    Aftercare does read must be well typed, otherwise the token is rejected.
    """

    if not isinstance(payload, Mapping):
        raise ContractViolation(
            ErrorCode.UNAUTHENTICATED, "introspection response must be an object"
        )
    active = payload.get("active")
    if not isinstance(active, bool):
        raise ContractViolation(
            ErrorCode.UNAUTHENTICATED, "introspection response requires a boolean active"
        )
    if not active:
        return IntrospectionVerdict(active=False)
    try:
        return IntrospectionVerdict(
            active=True,
            subject_id=payload.get("sub"),
            tenant_id=payload.get("tenant_id"),
            expires_at=_expiry(payload.get("exp")),
        )
    except ValidationError as exc:
        raise ContractViolation(ErrorCode.UNAUTHENTICATED, "invalid introspection claims") from exc


class HttpTokenIntrospector:
    """Call a configured RFC 7662 endpoint with a pre-authenticated client."""

    def __init__(self, config: IntrospectionConfig, *, client: httpx.Client) -> None:
        self.config = config
        self._client = client

    def introspect(self, token: str, *, now: datetime) -> IntrospectionVerdict:
        try:
            response = self._client.post(
                self.config.endpoint,
                data={"token": token, "token_type_hint": "access_token"},
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            declared = response.headers.get("content-length")
            if declared is not None and int(declared) > self.config.max_response_bytes:
                raise ValueError("introspection response is too large")
            if len(response.content) > self.config.max_response_bytes:
                raise ValueError("introspection response is too large")
            payload = response.json()
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise ContractViolation(
                ErrorCode.UNAUTHENTICATED, "token introspection unavailable"
            ) from exc
        return parse_verdict(payload)


class CachedIntrospector:
    """Bounded TTL cache in front of an introspector.

    The cache is keyed by a token fingerprint, never the token.  A verdict is
    only reused inside its TTL, and it is never cached beyond the token's own
    expiry.  Nothing is served after an introspection failure: a miss always
    calls the issuer again rather than extending trust.
    """

    def __init__(
        self, introspector: TokenIntrospector, *, ttl_seconds: int, max_entries: int
    ) -> None:
        if ttl_seconds < 0:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "ttl_seconds must not be negative")
        if max_entries < 1:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "max_entries must be positive")
        self._introspector = introspector
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[IntrospectionVerdict, float]] = {}

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def introspect(self, token: str, *, now: datetime) -> IntrospectionVerdict:
        key = token_fingerprint(token)
        if self._ttl_seconds > 0:
            with self._lock:
                cached = self._entries.get(key)
                if cached is not None:
                    verdict, deadline = cached
                    if time.monotonic() < deadline:
                        return verdict
                    del self._entries[key]
        verdict = self._introspector.introspect(token, now=now)
        if self._ttl_seconds <= 0:
            return verdict
        if verdict.expires_at is not None and verdict.expires_at <= now.astimezone(UTC):
            return verdict
        with self._lock:
            if len(self._entries) >= self._max_entries:
                self._evict()
            self._entries[key] = (verdict, time.monotonic() + self._ttl_seconds)
        return verdict

    def _evict(self) -> None:
        """Drop expired entries first, then the oldest, while holding the lock."""

        current = time.monotonic()
        for key in [key for key, (_, deadline) in self._entries.items() if deadline <= current]:
            del self._entries[key]
        while len(self._entries) >= self._max_entries:
            del self._entries[next(iter(self._entries))]
