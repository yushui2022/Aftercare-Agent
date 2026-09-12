"""Provider-neutral OIDC claims boundary.

This module consumes claims that an upstream JWT/JWKS verifier has already
validated. It never parses or verifies a bearer token and never accepts an
identity from a request body.
"""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from pydantic import Field

from aftercare_agent.domain.common import ContractModel, ContractViolation, ErrorCode, Identifier

from .context import AuthContext


class OidcConfig(ContractModel):
    issuer: Identifier
    audience: Identifier
    clock_skew_seconds: int = Field(default=30, ge=0, le=300)


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
