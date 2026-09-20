from __future__ import annotations

from typing import Any

import pytest

from deploy.deployment_acceptance import REQUIRED_CHECKS, evaluate, template


def _record() -> dict[str, Any]:
    value = template(environment="staging", image_digest="sha256:" + "a" * 64)
    for name in REQUIRED_CHECKS:
        value["checks"][name] = {"status": "pass", "evidence": f"evidence/{name}.json"}
    value["checks"]["tenant_isolation"].update(
        {
            "verifier": "aftercare-rls",
            "decision": "pass",
            "image_digest": "sha256:" + "a" * 64,
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
            "evidence_sha256": "a" * 64,
            "evidence_bytes": 100,
        }
    )
    value["checks"]["pitr"].update(
        {
            "verifier": "aftercare-pitr",
            "decision": "pass",
            "manifest_sha256": "b" * 64,
            "evidence_sha256": "c" * 64,
            "base_backup_sha256": "d" * 64,
        }
    )
    value["checks"]["preflight"].update(
        {
            "schema_version": 1,
            "verifier": "aftercare-preflight",
            "status_result": "pass",
            "image_digest": "sha256:" + "a" * 64,
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
            "evidence_sha256": "e" * 64,
            "evidence_bytes": 100,
        }
    )
    return value


def test_deployment_acceptance_holds_template_until_all_checks_have_evidence() -> None:
    decision = evaluate(template(environment="staging", image_digest="sha256:" + "a" * 64))

    assert decision["decision"] == "hold"
    assert decision["missing_checks"] == list(REQUIRED_CHECKS)


def test_deployment_acceptance_passes_only_with_evidence_for_every_check() -> None:
    decision = evaluate(_record())

    assert decision["decision"] == "pass"
    assert decision["missing_checks"] == []
    assert decision["failed_checks"] == []
    assert decision["pitr_evidence"]["verifier"] == "aftercare-pitr"
    assert decision["pitr_evidence"]["manifest_sha256"] == "b" * 64


def test_deployment_acceptance_rejects_pass_without_evidence() -> None:
    record = _record()
    record["checks"]["rollback"] = {"status": "pass", "evidence": ""}

    with pytest.raises(ValueError, match="rollback has no evidence"):
        evaluate(record)


def test_deployment_acceptance_requires_the_pitr_gate() -> None:
    record = _record()
    del record["checks"]["pitr"]

    with pytest.raises(ValueError, match="fixed check set"):
        evaluate(record)


def test_deployment_acceptance_rejects_unverified_pitr_pass() -> None:
    record = _record()
    del record["checks"]["pitr"]["evidence_sha256"]

    with pytest.raises(ValueError, match="verified aftercare-pitr result"):
        evaluate(record)


def test_deployment_acceptance_rejects_unverified_tenant_isolation_pass() -> None:
    record = _record()
    record["checks"]["tenant_isolation"]["cross_tenant_write"] = "accepted"

    with pytest.raises(ValueError, match="aftercare-rls result"):
        evaluate(record)


def test_deployment_acceptance_rejects_preflight_without_verified_database_tls() -> None:
    record = _record()
    database_check = next(
        item
        for item in record["checks"]["preflight"]["checks"]
        if item["name"] == "runtime_database_url"
    )
    del database_check["sslmode"]

    with pytest.raises(ValueError, match="verified aftercare-preflight"):
        evaluate(record)


def test_deployment_acceptance_rejects_handcrafted_tenant_provenance() -> None:
    for field in ("hardened", "source"):
        record = _record()
        del record["checks"]["tenant_isolation"][field]

        with pytest.raises(ValueError, match="aftercare-rls result"):
            evaluate(record)


def test_deployment_acceptance_rejects_malformed_image_digest() -> None:
    record = _record()
    record["image_digest"] = "sha256:not-a-digest"

    with pytest.raises(ValueError, match="no image digest"):
        evaluate(record)
