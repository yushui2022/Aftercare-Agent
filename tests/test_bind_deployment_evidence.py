from __future__ import annotations

from typing import Any, cast

import pytest

from deploy.bind_deployment_evidence import bind_preflight, bind_tenant_isolation
from deploy.deployment_acceptance import template

IMAGE = "sha256:" + "a" * 64


def _record() -> dict[str, Any]:
    return template(environment="staging", image_digest=IMAGE)


def _evidence(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "verifier": "aftercare-rls",
        "evidence_type": "tenant_isolation",
        "decision": "pass",
        "image_digest": IMAGE,
        "runtime_role": "aftercare_runtime",
        "tenant_table_count": 35,
        "empty_context_rows": 0,
        "cross_tenant_write": "rejected",
        "cross_tenant_update": "hidden",
        "cross_tenant_delete": "hidden",
        "hardened": True,
        "source": {
            "harden": {"bytes": 10, "sha256": "e" * 64},
            "verify": {"bytes": 11, "sha256": "f" * 64},
        },
    }
    value.update(overrides)
    return value


def _preflight() -> dict[str, object]:
    return {
        "schema_version": 1,
        "verifier": "aftercare-preflight",
        "image_digest": IMAGE,
        "status": "pass",
        "checks": [
            {
                "name": name,
                "status": "pass",
                **(
                    {"sslmode": "verify-full"}
                    if name in {"migration_database_url", "runtime_database_url"}
                    else {}
                ),
            }
            for name in (
                "immutable_image",
                "migration_database_url",
                "runtime_database_url",
                "oidc_issuer",
                "oidc_jwks_url",
                "oidc_audience",
                "worker_tenant",
                "metrics_backend",
                "aftercare_api_pool_bounds",
                "aftercare_worker_pool_bounds",
            )
        ],
        "errors": [],
    }


def test_binding_copies_verified_fields_and_requires_an_external_reference() -> None:
    result = bind_tenant_isolation(
        _record(), _evidence(), evidence_ref="artifact/rls-evidence.json"
    )

    check = result["checks"]["tenant_isolation"]
    assert check["status"] == "pass"
    assert check["evidence"] == "artifact/rls-evidence.json"
    assert check["tenant_table_count"] == 35
    assert check["evidence_bytes"] > 0
    assert len(check["evidence_sha256"]) == 64


def test_preflight_binding_copies_verified_image_and_checks() -> None:
    result = bind_preflight(_record(), _preflight(), evidence_ref="artifact/preflight.json")

    check = result["checks"]["preflight"]
    assert check["status"] == "pass"
    assert check["evidence"] == "artifact/preflight.json"
    assert check["schema_version"] == 1
    assert check["verifier"] == "aftercare-preflight"
    assert check["image_digest"] == IMAGE
    assert check["evidence_bytes"] > 0


def test_preflight_binding_rejects_a_different_image() -> None:
    evidence = _preflight()
    evidence["image_digest"] = "sha256:" + "b" * 64

    with pytest.raises(ValueError, match="not verified for the accepted image"):
        bind_preflight(_record(), evidence, evidence_ref="preflight.json")


def test_preflight_binding_rejects_weak_database_tls() -> None:
    evidence = _preflight()
    checks = cast(list[dict[str, Any]], evidence["checks"])
    next(item for item in checks if item["name"] == "migration_database_url")["sslmode"] = "require"

    with pytest.raises(ValueError, match="not verified for the accepted image"):
        bind_preflight(_record(), evidence, evidence_ref="preflight.json")


@pytest.mark.parametrize(
    "record_change,evidence_change",
    [
        ({"checks": {"tenant_isolation": {"status": "fail", "evidence": "old"}}}, {}),
        ({}, {"image_digest": "sha256:" + "b" * 64}),
        ({}, {"cross_tenant_write": "accepted"}),
        ({}, {"evidence_type": "other"}),
    ],
)
def test_binding_fails_closed(
    record_change: dict[str, Any], evidence_change: dict[str, object]
) -> None:
    record = _record()
    if "checks" in record_change:
        record["checks"]["tenant_isolation"] = record_change["checks"]["tenant_isolation"]

    with pytest.raises(ValueError):
        bind_tenant_isolation(record, _evidence(**evidence_change), evidence_ref="evidence.json")
