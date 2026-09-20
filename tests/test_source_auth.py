"""Source authentication fails closed before an event reaches the ledger."""

import json
from datetime import UTC, datetime, timedelta

import pytest

import aftercare_agent.connectors as source
from aftercare_agent.connectors.commerce import ORDER_FACTS_SCHEMA
from aftercare_agent.domain.common import ContractViolation, ErrorCode

NOW = datetime(2026, 9, 19, 8, tzinfo=UTC)
SECRET = b"source-test-secret-that-is-at-least-32-bytes"


def _request(
    content: bytes, *, issued_at: datetime = NOW, secret: bytes = SECRET
) -> source.SignedSourceRequest:
    return source.build_signed_source_request(
        secret,
        key_id="key-1",
        delivery_id="delivery-1",
        issued_at=issued_at,
        observed_at=NOW - timedelta(minutes=1),
        content=content,
    )


def test_canonical_source_json_is_shared_and_rejects_unsafe_fields() -> None:
    content = source.canonical_source_json(
        {"status": "paid", "schema": ORDER_FACTS_SCHEMA, "order_id": "order-1"}
    )

    assert content == (
        b'{"order_id":"order-1","schema":"aftercare.commerce.order.v1","status":"paid"}'
    )
    with pytest.raises(ContractViolation, match="authority fields"):
        source.canonical_source_json({"schema": ORDER_FACTS_SCHEMA, "tenant_id": "tenant-a"})


def test_signature_time_and_body_authority_fail_closed() -> None:
    verifier = source.HmacSourceEventVerifier(
        {
            "key-1": source.HmacSourceCredential(
                identity=source.SourceIdentity(
                    tenant_id="tenant-a", source_id="erp-a", tool="lookup_order"
                ),
                secret=SECRET,
            )
        }
    )
    content = json.dumps(
        {"schema": ORDER_FACTS_SCHEMA, "order_id": "order-1", "status": "paid"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    event = verifier.verify(_request(content), now=NOW)
    assert (event.identity.tenant_id, event.answer.source_id) == ("tenant-a", "erp-a")

    spoof = json.dumps(
        json.loads(content) | {"source_id": "carrier-b"}, sort_keys=True, separators=(",", ":")
    ).encode()
    invalid = (
        (
            _request(content, secret=b"another-secret-that-is-also-long-enough"),
            ErrorCode.UNAUTHENTICATED,
        ),
        (_request(content, issued_at=NOW - timedelta(minutes=6)), ErrorCode.UNAUTHENTICATED),
        (_request(spoof), ErrorCode.FORBIDDEN),
    )
    for request, expected in invalid:
        with pytest.raises(ContractViolation) as caught:
            verifier.verify(request, now=NOW)
        assert caught.value.code is expected
