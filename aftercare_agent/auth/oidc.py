"""Provider-neutral OIDC claims and JWT/JWKS verification boundaries.

``auth_context_from_claims`` remains a pure conversion boundary for callers
that already have a verified token.  ``JwtJwksVerifier`` is the HTTP adapter
used by the FastAPI process: it verifies a bearer token locally against a
cached, configured JWKS and only then constructs an :class:`AuthContext`.
No token-controlled URL, algorithm, tenant or permission is trusted.
"""

import math
import threading
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
import jwt
from pydantic import Field, TypeAdapter, ValidationError, model_validator

from aftercare_agent.domain.common import ContractModel, ContractViolation, ErrorCode, Identifier

from .context import AuthContext


class OidcConfig(ContractModel):
    issuer: Identifier
    audience: Identifier
    clock_skew_seconds: int = Field(default=30, ge=0, le=300)


AllowedAlgorithm = Literal["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"]


class JwtVerifierConfig(OidcConfig):
    """Static policy for the production bearer-token verifier.

    ``jwks_url`` is deployment configuration, never read from a token.  The
    default algorithm and token type allow common OIDC access tokens while
    still rejecting ``none``/HMAC confusion.  Deployments should narrow both
    lists to the provider's documented values.
    """

    jwks_url: str
    algorithms: tuple[AllowedAlgorithm, ...] = ("RS256", "ES256")
    token_types: tuple[str, ...] = ("JWT", "at+jwt")
    jwks_cache_seconds: int = Field(default=300, ge=1, le=86_400)
    jwks_refresh_cooldown_seconds: int = Field(default=5, ge=0, le=300)
    request_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    max_token_lifetime_seconds: int = Field(default=3600, ge=1, le=86_400)
    max_case_ids: int = Field(default=256, ge=1, le=10_000)
    tenant_claim: Identifier = "tenant_id"
    permissions_claim: Identifier = "permissions"
    scope_claim: Identifier = "scope"
    case_ids_claim: Identifier = "case_ids"
    # Case grants are resolved against PostgreSQL by the API.  Token case_ids
    # are an optional attenuation bound; requiring them here would make a
    # legitimate create request impossible because the new Case ID is server
    # generated.  The API still fails closed when its DB grant is absent.
    require_case_ids: bool = False

    @model_validator(mode="after")
    def validate_security_policy(self) -> "JwtVerifierConfig":
        parsed = urlparse(self.jwks_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("jwks_url must be an https URL without query or fragment")
        if not self.algorithms:
            raise ValueError("at least one JWT algorithm is required")
        if len(set(self.algorithms)) != len(self.algorithms):
            raise ValueError("JWT algorithms must be unique")
        if not self.token_types:
            raise ValueError("at least one JWT token type is required")
        if any(not value or any(char.isspace() for char in value) for value in self.token_types):
            raise ValueError("JWT token types must be non-empty and contain no whitespace")
        return self


class VerifiedOidcClaims(ContractModel):
    """Claims after signature, issuer and token-type checks by an IdP verifier."""

    issuer: Identifier
    subject: Identifier
    audience: Identifier | tuple[Identifier, ...]
    tenant_id: Identifier
    permissions: frozenset[Identifier] = frozenset()
    case_ids: frozenset[Identifier] | None = None
    expires_at: datetime
    issued_at: datetime | None = None


def auth_context_from_claims(
    claims: VerifiedOidcClaims | Mapping[str, object],
    config: OidcConfig,
    *,
    now: datetime,
) -> AuthContext:
    """Convert verified claims into a scoped context or reject them."""
    try:
        verified = (
            claims
            if isinstance(claims, VerifiedOidcClaims)
            else VerifiedOidcClaims.model_validate(claims)
        )
    except ValueError as exc:
        raise ContractViolation(ErrorCode.UNAUTHENTICATED, "invalid verified OIDC claims") from exc
    if verified.issuer != config.issuer:
        raise ContractViolation(ErrorCode.UNAUTHENTICATED, "OIDC issuer mismatch")
    audiences = (verified.audience,) if isinstance(verified.audience, str) else verified.audience
    if config.audience not in audiences:
        raise ContractViolation(ErrorCode.UNAUTHENTICATED, "OIDC audience mismatch")
    current = now.astimezone(UTC)
    if current >= verified.expires_at.astimezone(UTC) + timedelta(
        seconds=config.clock_skew_seconds
    ):
        raise ContractViolation(ErrorCode.UNAUTHENTICATED, "OIDC token expired")
    if verified.issued_at is not None and verified.issued_at.astimezone(UTC) > current + timedelta(
        seconds=config.clock_skew_seconds
    ):
        raise ContractViolation(ErrorCode.UNAUTHENTICATED, "OIDC token is not yet valid")
    return AuthContext(
        subject_id=verified.subject,
        tenant_id=verified.tenant_id,
        permissions=verified.permissions,
        case_ids=verified.case_ids,
        synthetic=False,
    )


def bearer_token(value: str) -> str:
    """Parse one RFC 6750 bearer value without accepting ambiguous input."""
    parts = value.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise ContractViolation(ErrorCode.UNAUTHENTICATED, "invalid bearer token")
    token = parts[1].strip()
    if not token or any(char.isspace() for char in token):
        raise ContractViolation(ErrorCode.UNAUTHENTICATED, "invalid bearer token")
    return token


def _numeric_date(value: object, claim: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractViolation(ErrorCode.UNAUTHENTICATED, f"invalid {claim} claim")
    number = float(value)
    if not math.isfinite(number):
        raise ContractViolation(ErrorCode.UNAUTHENTICATED, f"invalid {claim} claim")
    return number


class JwtJwksVerifier:
    """Thread-safe cached verifier for signed OIDC access tokens.

    The key set is fetched only from the static HTTPS URL.  An unknown ``kid``
    triggers at most one forced refresh during the cooldown window, then the
    request fails closed.  Expired caches are never used after a refresh
    failure, preventing an IdP outage from becoming indefinite trust.
    """

    def __init__(self, config: JwtVerifierConfig, *, client: httpx.Client | None = None) -> None:
        self.config = config
        self._client = client or httpx.Client(
            timeout=config.request_timeout_seconds,
            follow_redirects=False,
        )
        self._owns_client = client is None
        self._lock = threading.Lock()
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at = 0.0
        self._last_unknown_refresh = float("-inf")

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _fetch_keys(self) -> dict[str, jwt.PyJWK]:
        try:
            response = self._client.get(self.config.jwks_url)
            response.raise_for_status()
            content_length = response.headers.get("content-length")
            if content_length is not None and int(content_length) > 1_048_576:
                raise ValueError("JWKS response is too large")
            if len(response.content) > 1_048_576:
                raise ValueError("JWKS response is too large")
            payload = response.json()
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise ContractViolation(ErrorCode.UNAUTHENTICATED, "JWKS unavailable") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("keys"), list):
            raise ContractViolation(ErrorCode.UNAUTHENTICATED, "invalid JWKS")
        keys: dict[str, jwt.PyJWK] = {}
        for raw in payload["keys"]:
            if not isinstance(raw, dict):
                raise ContractViolation(ErrorCode.UNAUTHENTICATED, "invalid JWKS key")
            kid = raw.get("kid")
            key_alg = raw.get("alg")
            if not isinstance(kid, str) or not kid or not isinstance(key_alg, str):
                raise ContractViolation(ErrorCode.UNAUTHENTICATED, "invalid JWKS key")
            if raw.get("use", "sig") != "sig" or key_alg not in self.config.algorithms:
                continue
            try:
                jwk = jwt.PyJWK.from_dict(raw, algorithm=key_alg)
            except (TypeError, ValueError, jwt.PyJWTError) as exc:
                raise ContractViolation(ErrorCode.UNAUTHENTICATED, "invalid JWKS key") from exc
            if kid in keys:
                raise ContractViolation(ErrorCode.UNAUTHENTICATED, "duplicate JWKS kid")
            keys[kid] = jwk
        if not keys:
            raise ContractViolation(ErrorCode.UNAUTHENTICATED, "JWKS has no usable keys")
        return keys

    def _key_for(self, kid: str) -> jwt.PyJWK:
        now = time.monotonic()
        with self._lock:
            cached = self._keys.get(kid)
            fresh = now - self._fetched_at < self.config.jwks_cache_seconds
            if cached is not None and fresh:
                return cached
            unknown = cached is None
            if (
                unknown
                and now - self._last_unknown_refresh < self.config.jwks_refresh_cooldown_seconds
            ):
                raise ContractViolation(ErrorCode.UNAUTHENTICATED, "unknown signing key")
            if unknown:
                self._last_unknown_refresh = now
            keys = self._fetch_keys()
            self._keys = keys
            self._fetched_at = time.monotonic()
            key = keys.get(kid)
            if key is None:
                raise ContractViolation(ErrorCode.UNAUTHENTICATED, "unknown signing key")
            return key

    def verify(self, authorization: str, *, now: datetime | None = None) -> AuthContext:
        """Verify an Authorization header and map claims to AuthContext."""
        token = bearer_token(authorization)
        try:
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            algorithm = header.get("alg")
            token_type = header.get("typ")
            if (
                not isinstance(kid, str)
                or not kid
                or not isinstance(algorithm, str)
                or algorithm not in self.config.algorithms
                or not isinstance(token_type, str)
                or token_type not in self.config.token_types
            ):
                raise ContractViolation(ErrorCode.UNAUTHENTICATED, "invalid bearer token")
            key = self._key_for(kid)
            if key.algorithm_name != algorithm:
                raise ContractViolation(ErrorCode.UNAUTHENTICATED, "signing key algorithm mismatch")
            claims: dict[str, Any] = jwt.decode(
                token,
                key=key,
                algorithms=[algorithm],
                issuer=self.config.issuer,
                audience=self.config.audience,
                options={
                    "require": ["exp", "iat", "iss", "sub", "aud"],
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                },
            )
            current = (now or datetime.now(UTC)).astimezone(UTC)
            current_seconds = current.timestamp()
            expires = _numeric_date(claims.get("exp"), "exp")
            issued = _numeric_date(claims.get("iat"), "iat")
            if current_seconds >= expires + self.config.clock_skew_seconds:
                raise ContractViolation(ErrorCode.UNAUTHENTICATED, "OIDC token expired")
            if issued > current_seconds + self.config.clock_skew_seconds:
                raise ContractViolation(ErrorCode.UNAUTHENTICATED, "OIDC token is not yet valid")
            if expires <= issued or expires - issued > self.config.max_token_lifetime_seconds:
                raise ContractViolation(ErrorCode.UNAUTHENTICATED, "invalid token lifetime")
            if "nbf" in claims and current_seconds + self.config.clock_skew_seconds < _numeric_date(
                claims["nbf"], "nbf"
            ):
                raise ContractViolation(ErrorCode.UNAUTHENTICATED, "OIDC token is not yet valid")
            subject = claims.get("sub")
            tenant = claims.get(self.config.tenant_claim)
            permissions = _permissions(claims.get(self.config.permissions_claim))
            permissions |= _permissions(claims.get(self.config.scope_claim), split_scope=True)
            case_ids = _case_ids(
                claims.get(self.config.case_ids_claim), max_count=self.config.max_case_ids
            )
            if self.config.require_case_ids and case_ids is None:
                raise ContractViolation(ErrorCode.UNAUTHENTICATED, "case scope is required")
            audience = claims.get("aud")
            if isinstance(audience, list):
                audience = tuple(audience)
            canonical: dict[str, object] = {
                "issuer": claims.get("iss"),
                "subject": subject,
                "audience": audience,
                "tenant_id": tenant,
                "permissions": frozenset(permissions),
                "case_ids": case_ids,
                "expires_at": datetime.fromtimestamp(expires, tz=UTC),
                "issued_at": datetime.fromtimestamp(issued, tz=UTC),
            }
            return auth_context_from_claims(canonical, self.config, now=current)
        except ContractViolation:
            raise
        except (jwt.PyJWTError, TypeError, ValueError, OverflowError) as exc:
            raise ContractViolation(ErrorCode.UNAUTHENTICATED, "invalid bearer token") from exc


def _permissions(value: object, *, split_scope: bool = False) -> set[str]:
    if value is None:
        return set()
    if split_scope:
        if not isinstance(value, str):
            raise ContractViolation(ErrorCode.UNAUTHENTICATED, "invalid scope claim")
        raw_values: Sequence[object] = value.split()
    elif isinstance(value, (list, tuple, set, frozenset)):
        raw_values = list(value)
    else:
        raise ContractViolation(ErrorCode.UNAUTHENTICATED, "invalid permissions claim")
    result: set[str] = set()
    adapter: TypeAdapter[str] = TypeAdapter(Identifier)
    for item in raw_values:
        try:
            result.add(adapter.validate_python(item))
        except ValidationError as exc:
            raise ContractViolation(ErrorCode.UNAUTHENTICATED, "invalid permission claim") from exc
    return result


def _case_ids(value: object, *, max_count: int) -> frozenset[str] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise ContractViolation(ErrorCode.UNAUTHENTICATED, "invalid case scope claim")
    if len(value) > max_count:
        raise ContractViolation(ErrorCode.UNAUTHENTICATED, "case scope claim is too large")
    adapter: TypeAdapter[str] = TypeAdapter(Identifier)
    try:
        return frozenset(adapter.validate_python(item) for item in value)
    except ValidationError as exc:
        raise ContractViolation(ErrorCode.UNAUTHENTICATED, "invalid case scope claim") from exc
