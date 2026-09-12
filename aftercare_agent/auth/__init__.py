"""Trusted authentication context boundaries for the HTTP adapter."""

from .context import AuthContext, SyntheticAuthError, synthetic_context

__all__ = ["AuthContext", "SyntheticAuthError", "synthetic_context"]
