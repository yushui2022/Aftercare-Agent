"""Validate a target-environment deployment acceptance record."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

REQUIRED_CHECKS = (
    "preflight",
    "migration",
    "runtime_permissions",
    "tenant_isolation",
    "readiness",
    "worker_recovery",
    "secret_rotation",
    "rollback",
    "pitr",
)
_STATUSES = frozenset({"pass", "not_run", "fail"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PREFLIGHT_CHECKS = frozenset(
    {
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
    }
)


def _preflight_result_is_verified(value: dict[str, Any], *, image_digest: str) -> bool:
    checks = value.get("checks")
    database_checks = (
        [
            item
            for item in checks
            if isinstance(item, dict)
            and item.get("name") in {"migration_database_url", "runtime_database_url"}
        ]
        if isinstance(checks, list)
        else []
    )
    return (
        value.get("schema_version", 1) == 1
        and value.get("verifier") == "aftercare-preflight"
        and value.get("image_digest") == image_digest
        and value.get("status_result", value.get("status")) == "pass"
        and value.get("errors") == []
        and isinstance(checks, list)
        and {
            item.get("name")
            for item in checks
            if isinstance(item, dict) and item.get("status") == "pass"
        }
        >= _PREFLIGHT_CHECKS
        and all(isinstance(item, dict) and item.get("status") == "pass" for item in checks)
        and len(database_checks) == 2
        and all(item.get("sslmode") == "verify-full" for item in database_checks)
    )


def _pitr_result_is_verified(value: dict[str, Any]) -> bool:
    """Require the structured output produced by ``aftercare-backup pitr``."""

    return (
        value.get("verifier") == "aftercare-pitr"
        and value.get("decision") == "pass"
        and all(
            isinstance(value.get(name), str) and _SHA256.fullmatch(value[name])
            for name in ("manifest_sha256", "evidence_sha256", "base_backup_sha256")
        )
    )


def _tenant_result_is_verified(value: dict[str, Any], *, image_digest: str) -> bool:
    """Require the machine-readable output from ``aftercare-rls verify``.

    A URL or a screenshot is useful for an operator, but it cannot close the
    tenant-isolation gate by itself.  The verifier output binds the probes to
    the exact immutable image that is being accepted.
    """

    source = value.get("source")
    source_valid = isinstance(source, dict) and all(
        isinstance(source.get(name), dict)
        and isinstance(source[name].get("bytes"), int)
        and not isinstance(source[name]["bytes"], bool)
        and source[name]["bytes"] > 0
        and isinstance(source[name].get("sha256"), str)
        and _SHA256.fullmatch(source[name]["sha256"]) is not None
        for name in ("harden", "verify")
    )
    return (
        value.get("hardened") is True
        and source_valid
        and isinstance(value.get("evidence_sha256"), str)
        and _SHA256.fullmatch(value["evidence_sha256"]) is not None
        and isinstance(value.get("evidence_bytes"), int)
        and not isinstance(value["evidence_bytes"], bool)
        and value["evidence_bytes"] > 0
        and value.get("verifier") == "aftercare-rls"
        and value.get("decision") == "pass"
        and value.get("image_digest") == image_digest
        and isinstance(value.get("runtime_role"), str)
        and bool(value["runtime_role"])
        and isinstance(value.get("tenant_table_count"), int)
        and not isinstance(value["tenant_table_count"], bool)
        and value["tenant_table_count"] > 0
        and value.get("empty_context_rows") == 0
        and value.get("cross_tenant_write") == "rejected"
        and value.get("cross_tenant_update") == "hidden"
        and value.get("cross_tenant_delete") == "hidden"
    )


def template(*, environment: str, image_digest: str) -> dict[str, Any]:
    checks = {name: {"status": "not_run", "evidence": None} for name in REQUIRED_CHECKS}
    checks["pitr"].update(
        {
            "verifier": None,
            "decision": "not_run",
            "manifest_sha256": None,
            "evidence_sha256": None,
            "base_backup_sha256": None,
        }
    )
    checks["preflight"].update(
        {
            "schema_version": None,
            "verifier": None,
            "status_result": "not_run",
            "image_digest": None,
            "checks": None,
            "errors": None,
            "evidence_sha256": None,
            "evidence_bytes": None,
        }
    )
    checks["tenant_isolation"].update(
        {
            "verifier": None,
            "decision": "not_run",
            "image_digest": None,
            "runtime_role": None,
            "tenant_table_count": None,
            "empty_context_rows": None,
            "cross_tenant_write": None,
            "cross_tenant_update": None,
            "cross_tenant_delete": None,
            "hardened": None,
            "source": None,
            "evidence_sha256": None,
            "evidence_bytes": None,
        }
    )
    return {
        "schema_version": 1,
        "environment": environment,
        "image_digest": image_digest,
        "checks": checks,
    }


def evaluate(record: dict[str, Any]) -> dict[str, Any]:
    if record.get("schema_version") != 1:
        raise ValueError("deployment acceptance must use schema v1")
    environment = record.get("environment")
    image_digest = record.get("image_digest")
    if not isinstance(environment, str) or not environment:
        raise ValueError("deployment acceptance has no environment")
    if (
        not isinstance(image_digest, str)
        or not image_digest.startswith("sha256:")
        or not _SHA256.fullmatch(image_digest.removeprefix("sha256:"))
    ):
        raise ValueError("deployment acceptance has no image digest")
    checks = record.get("checks")
    if not isinstance(checks, dict) or set(checks) != set(REQUIRED_CHECKS):
        raise ValueError("deployment acceptance does not contain the fixed check set")
    missing: list[str] = []
    failed: list[str] = []
    for name in REQUIRED_CHECKS:
        value = checks[name]
        if not isinstance(value, dict) or value.get("status") not in _STATUSES:
            raise ValueError(f"deployment acceptance has an invalid status for {name}")
        status = value["status"]
        evidence = value.get("evidence")
        if status == "pass" and (not isinstance(evidence, str) or not evidence.strip()):
            raise ValueError(f"passed deployment check {name} has no evidence reference")
        if name == "pitr" and status == "pass" and not _pitr_result_is_verified(value):
            raise ValueError(
                "passed deployment check pitr must include a verified aftercare-pitr result "
                "and manifest, evidence, and base-backup SHA-256 values"
            )
        if (
            name == "preflight"
            and status == "pass"
            and not _preflight_result_is_verified(value, image_digest=image_digest)
        ):
            raise ValueError(
                "passed deployment check preflight must include a verified "
                "aftercare-preflight result for the accepted image"
            )
        if (
            name == "tenant_isolation"
            and status == "pass"
            and not _tenant_result_is_verified(value, image_digest=image_digest)
        ):
            raise ValueError(
                "passed deployment check tenant_isolation must include a verified "
                "aftercare-rls result for the accepted image and all isolation probes"
            )
        if status == "not_run":
            missing.append(name)
        elif status == "fail":
            failed.append(name)
    decision = "pass" if not missing and not failed else "hold"
    pitr = checks["pitr"]
    pitr_evidence = {
        name: pitr.get(name)
        for name in (
            "verifier",
            "decision",
            "manifest_sha256",
            "evidence_sha256",
            "base_backup_sha256",
        )
        if name in pitr
    }
    preflight = checks["preflight"]
    preflight_evidence = {
        name: preflight.get(name)
        for name in (
            "schema_version",
            "verifier",
            "status_result",
            "image_digest",
            "checks",
            "errors",
            "evidence_sha256",
            "evidence_bytes",
        )
        if name in preflight
    }
    tenant = checks["tenant_isolation"]
    tenant_evidence = {
        name: tenant.get(name)
        for name in (
            "verifier",
            "decision",
            "image_digest",
            "runtime_role",
            "tenant_table_count",
            "empty_context_rows",
            "cross_tenant_write",
            "cross_tenant_update",
            "cross_tenant_delete",
            "hardened",
            "source",
            "evidence_sha256",
            "evidence_bytes",
        )
        if name in tenant
    }
    return {
        "schema_version": 1,
        "decision": decision,
        "environment": environment,
        "image_digest": image_digest,
        "missing_checks": missing,
        "failed_checks": failed,
        "preflight_evidence": preflight_evidence,
        "pitr_evidence": pitr_evidence,
        "tenant_isolation_evidence": tenant_evidence,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--record", type=Path)
    source.add_argument("--template", action="store_true")
    parser.add_argument("--environment", default="target")
    parser.add_argument("--image-digest", default="sha256:replace-me")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--fail-on-hold", action="store_true")
    args = parser.parse_args()
    try:
        if args.template:
            result = template(environment=args.environment, image_digest=args.image_digest)
        else:
            result = evaluate(json.loads(args.record.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    serialized = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    if args.fail_on_hold and result.get("decision") == "hold":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
