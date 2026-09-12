"""Pure contracts for the durable Action Ledger.

This module does not call a provider.  It only describes a trusted, already
authorised business obligation and the immutable parameter digest used by the
PostgreSQL repository for idempotent replay detection.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from pydantic import Field

from aftercare_agent.domain.common import (
    CaseScope,
    Identifier,
    PositiveInt,
    SchemaVersion,
    Sha256,
    UtcDatetime,
)

type ActionState = Literal["RESERVED", "REQUESTED", "UNKNOWN", "CONFIRMED", "FAILED"]
type AmountMinor = str
type Currency = str


class ActionIntent(CaseScope):
    """A fixed business obligation submitted by trusted orchestration code.

    ``business_key`` is the cross-Case identity of the obligation (for
    example, a merchant/payment/refund obligation).  ``idempotency_key`` is
    the caller request identity.  Both are stored and checked; neither is a
    temporary model/tool call id.
    """

    schema_version: SchemaVersion = 1
    order_id: Identifier
    action_id: Identifier
    action_type: Identifier
    business_key: Identifier
    idempotency_key: Identifier
    parameters_sha256: Sha256
    amount_minor: AmountMinor = Field(pattern=r"^[1-9][0-9]*$", max_length=38)
    currency: Currency = Field(pattern=r"^[A-Z]{3}$")
    provider_idempotency_key: Identifier | None = None
    # Safe by default: a dispatcher must present a matching APPROVED record.
    # Low-risk internal actions can opt out explicitly after policy review.
    approval_required: bool = True


class ActionRecord(ActionIntent):
    """The durable ledger row returned by trusted persistence code."""

    state: ActionState = "RESERVED"
    provider_reference: Identifier | None = None
    result_sha256: Sha256 | None = None
    failure_code: Identifier | None = None
    fencing_token: PositiveInt | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime


@dataclass(frozen=True)
class ActionReservation:
    action: ActionRecord
    replayed: bool


def action_parameters_digest(intent: ActionIntent, *, parameters: object | None = None) -> str:
    """Return the canonical digest used by the ledger.

    Callers normally provide the digest from a validated request.  The helper
    is provided for synthetic connectors and tests so parameter canonicalising
    is explicit and deterministic rather than delegated to ``repr``.
    """

    if parameters is None:
        value: object = {
            "order_id": intent.order_id,
            "action_type": intent.action_type,
            "business_key": intent.business_key,
            "amount_minor": intent.amount_minor,
            "currency": intent.currency,
            "provider_idempotency_key": intent.provider_idempotency_key,
        }
    else:
        value = parameters
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def result_identity(
    *, provider_reference: str | None, result_sha256: str | None
) -> tuple[str | None, str | None]:
    """Identity fields used to make terminal result replays idempotent."""

    return provider_reference, result_sha256
