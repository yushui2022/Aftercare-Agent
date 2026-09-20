"""Deterministic action providers used by local and integration profiles.

The synthetic provider models an external system without performing any
business side effect.  It deliberately keeps its state outside PostgreSQL so
the Action Ledger remains the source of truth for the Aftercare obligation.
"""

import hashlib

from aftercare_agent.domain.common import ContractViolation, ErrorCode

from .ledger import ActionRecord, ProviderReceipt


class SyntheticActionProvider:
    """A stable, in-memory provider for local and integration verification."""

    def __init__(self, *, unknown_first: bool = False) -> None:
        self.unknown_first = unknown_first
        self.requests: dict[str, int] = {}
        self._confirmed: dict[str, ProviderReceipt] = {}

    def request(self, action: ActionRecord) -> ProviderReceipt:
        """Return a deterministic receipt for the Action's provider key."""
        key = action.provider_idempotency_key
        if key is None:
            raise ContractViolation(
                ErrorCode.INVALID_INPUT, "synthetic provider needs a provider idempotency key"
            )
        attempts = self.requests.get(key, 0) + 1
        self.requests[key] = attempts
        existing = self._confirmed.get(key)
        if existing is not None:
            return existing
        reference = f"synthetic-refund:{action.action_id}"
        digest = hashlib.sha256(reference.encode("utf-8")).hexdigest()
        confirmed = ProviderReceipt(
            action_id=action.action_id,
            provider_idempotency_key=key,
            state="CONFIRMED",
            provider_reference=reference,
            result_sha256=digest,
        )
        self._confirmed[key] = confirmed
        if self.unknown_first and attempts == 1:
            return ProviderReceipt(
                action_id=action.action_id,
                provider_idempotency_key=key,
                state="UNKNOWN",
            )
        return confirmed

    def lookup(self, action: ActionRecord) -> ProviderReceipt:
        """Reconcile the same external key without creating a new operation."""
        key = action.provider_idempotency_key
        if key is None:
            raise ContractViolation(
                ErrorCode.INVALID_INPUT, "synthetic provider needs a provider idempotency key"
            )
        existing = self._confirmed.get(key)
        if existing is not None:
            return existing
        return self.request(action)


__all__ = ["SyntheticActionProvider"]
