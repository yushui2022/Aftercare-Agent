"""Bearer-token access policy: local verification plus revocation checking.

:class:`JwtJwksVerifier` answers "did the issuer sign this token, and is it
unexpired?".  It cannot answer "is it still valid right now?" - a revoked token
keeps verifying until it expires.  :class:`TokenAccessGuard` adds that step and
fails closed: when an introspector is configured, a missing, failed or
contradictory verdict rejects the request instead of falling back to the
signature check alone.
"""

from datetime import UTC, datetime
from typing import Protocol

from aftercare_agent.domain.common import ContractViolation, ErrorCode

from .context import AuthContext
from .introspection import TokenIntrospector
from .oidc import bearer_token


class TokenVerifier(Protocol):
    """The seam the guard needs: verify one Authorization header.

    ``JwtJwksVerifier`` is the production implementation.  Declaring the seam
    keeps test doubles honest - a verifier that cannot accept the guard's
    explicit instant would otherwise fail only at request time.
    """

    def verify(self, authorization: str, *, now: datetime | None = None) -> AuthContext: ...


class TokenAccessGuard:
    """Verify a bearer token, then apply the deployment's revocation policy."""

    def __init__(
        self,
        verifier: TokenVerifier,
        *,
        introspector: TokenIntrospector | None = None,
    ) -> None:
        self.verifier = verifier
        self._introspector = introspector

    def authorize(self, authorization: str, *, now: datetime | None = None) -> AuthContext:
        # One clock reading for both steps: the signature check and the
        # revocation check must agree about "now", or a token could pass one
        # and fail the other for reasons the caller cannot see.
        current = (now or datetime.now(UTC)).astimezone(UTC)
        context = self.verifier.verify(authorization, now=current)
        if self._introspector is None:
            return context
        token = bearer_token(authorization)
        try:
            verdict = self._introspector.introspect(token, now=current)
        except ContractViolation:
            raise
        except Exception as exc:
            raise ContractViolation(
                ErrorCode.UNAUTHENTICATED, "token introspection failed"
            ) from exc
        if not verdict.active:
            raise ContractViolation(ErrorCode.UNAUTHENTICATED, "token is not active")
        if verdict.expires_at is not None and verdict.expires_at <= current:
            raise ContractViolation(ErrorCode.UNAUTHENTICATED, "token is not active")
        # The issuer is authoritative about the subject it issued the token for:
        # a verdict naming someone else means the token was substituted or the
        # JWT and the introspection call disagree, and neither may be trusted.
        if verdict.subject_id is not None and verdict.subject_id != context.subject_id:
            raise ContractViolation(ErrorCode.UNAUTHENTICATED, "introspection subject mismatch")
        if verdict.tenant_id is not None and verdict.tenant_id != context.tenant_id:
            raise ContractViolation(ErrorCode.UNAUTHENTICATED, "introspection tenant mismatch")
        return context
