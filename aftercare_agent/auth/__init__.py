"""Trusted authentication context boundaries for the HTTP adapter."""

from .context import AuthContext, SyntheticAuthError, synthetic_context
from .oidc import OidcConfig, VerifiedOidcClaims, auth_context_from_claims

__all__ = [
    "AuthContext",
    "OidcConfig",
    "SyntheticAuthError",
    "VerifiedOidcClaims",
    "auth_context_from_claims",
    "synthetic_context",
]
