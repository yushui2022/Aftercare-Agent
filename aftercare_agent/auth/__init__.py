"""Trusted authentication context boundaries for the HTTP adapter."""

from .context import AuthContext, SyntheticAuthError, synthetic_context
from .grants import CaseGrantRecord
from .oidc import (
    JwtJwksVerifier,
    JwtVerifierConfig,
    OidcConfig,
    VerifiedOidcClaims,
    auth_context_from_claims,
)

__all__ = [
    "AuthContext",
    "CaseGrantRecord",
    "JwtJwksVerifier",
    "JwtVerifierConfig",
    "OidcConfig",
    "SyntheticAuthError",
    "VerifiedOidcClaims",
    "auth_context_from_claims",
    "synthetic_context",
]
