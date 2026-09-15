"""Trusted authentication context boundaries for the HTTP adapter."""

from .context import AuthContext, SyntheticAuthError, synthetic_context
from .grants import CaseGrantRecord
from .guard import TokenAccessGuard
from .introspection import (
    CachedIntrospector,
    HttpTokenIntrospector,
    IntrospectionConfig,
    IntrospectionVerdict,
    TokenIntrospector,
    parse_verdict,
    token_fingerprint,
)
from .oidc import (
    JwtJwksVerifier,
    JwtVerifierConfig,
    OidcConfig,
    VerifiedOidcClaims,
    auth_context_from_claims,
    bearer_token,
)

__all__ = [
    "AuthContext",
    "CaseGrantRecord",
    "CachedIntrospector",
    "HttpTokenIntrospector",
    "IntrospectionConfig",
    "IntrospectionVerdict",
    "JwtJwksVerifier",
    "JwtVerifierConfig",
    "OidcConfig",
    "SyntheticAuthError",
    "TokenAccessGuard",
    "TokenIntrospector",
    "VerifiedOidcClaims",
    "auth_context_from_claims",
    "bearer_token",
    "parse_verdict",
    "synthetic_context",
    "token_fingerprint",
]
