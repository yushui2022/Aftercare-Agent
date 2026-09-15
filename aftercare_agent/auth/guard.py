"""Bearer-token access policy: local verification plus revocation checking.

:class:`JwtJwksVerifier` answers "did the issuer sign this token, and is it
unexpired?".  It cannot answer "is it still valid right now?" - a revoked token
keeps verifying until it expires.  :class:`TokenAccessGuard` adds that step and
fails closed: when an introspector is configured, a missing, failed or
contradictory verdict rejects the request instead of falling back to the
signature check alone.
"""

from datetime import UTC, datetime

from aftercare_agent.domain.common import ContractViolation, ErrorCode

from .context import AuthContext
from .introspection import TokenIntrospector
from .oidc import JwtJwksVerifier, bearer_token


class TokenAccessGuard:
    """Verify a bearer token, then apply the deployment's revocation policy."""

    def __init__(
        self,
        verifier: JwtJwksVerifier,
        *,
        introspector: TokenIntrospector | None = None,
    ) -> None:
        self.verifier = verifier
        self._introspector = introspector

    def authorize(self, authorization: str, *, now: datetime | None = None) -> AuthContext:
        context = self.verifier.verify(authorization, now=now)
        if self._introspector is None:
            return context
        token = bearer_token(authorization)
        current = (now or datetime.now(UTC)).astimezone(UTC)
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
