"""Trusted business-action contracts and provider boundary.

The ledger is deliberately separate from model tool calls.  An Action is a
durable business obligation; retries and a lost network response must refer to
the same row instead of creating another external operation.
"""

from .ledger import (
    ActionIntent,
    ActionProvider,
    ActionRecord,
    ActionReservation,
    ActionState,
    ProviderReceipt,
    ProviderReceiptState,
    action_parameters_digest,
    validate_provider_receipt,
)
from .providers import SyntheticActionProvider

__all__ = [
    "ActionIntent",
    "ActionRecord",
    "ActionReservation",
    "ActionProvider",
    "ActionState",
    "action_parameters_digest",
    "ProviderReceipt",
    "ProviderReceiptState",
    "validate_provider_receipt",
    "SyntheticActionProvider",
]
