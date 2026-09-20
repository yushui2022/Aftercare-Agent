"""Bind verified machine evidence into a deployment acceptance record."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

try:  # package entry point
    from deploy.deployment_acceptance import (
        REQUIRED_CHECKS,
        _preflight_result_is_verified,
        _tenant_result_is_verified,
        evaluate,
    )
except ModuleNotFoundError:  # direct ``python deploy/*.py`` entry point
    from deployment_acceptance import (  # type: ignore[import-not-found, no-redef]
        REQUIRED_CHECKS,
        _preflight_result_is_verified,
        _tenant_result_is_verified,
        evaluate,
    )


_TENANT_FIELDS = (
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


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON {path} must be an object")
    return value


def bind_tenant_isolation(
    record: dict[str, Any], evidence: dict[str, Any], *, evidence_ref: str
) -> dict[str, Any]:
    """Return a new record with a verified tenant-isolation check bound."""

    if not isinstance(evidence_ref, str) or not evidence_ref.strip():
        raise ValueError("tenant-isolation evidence reference must not be empty")
    if evidence.get("evidence_type") != "tenant_isolation":
        raise ValueError("evidence is not a tenant-isolation result")
    canonical = json.dumps(
        evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    image_digest = record.get("image_digest")
    if not isinstance(image_digest, str):
        raise ValueError("deployment acceptance has no image digest")
    verified_evidence = dict(evidence)
    verified_evidence.update(
        {
            "evidence_sha256": hashlib.sha256(canonical).hexdigest(),
            "evidence_bytes": len(canonical),
        }
    )
    if not _tenant_result_is_verified(verified_evidence, image_digest=image_digest):
        raise ValueError("tenant-isolation evidence is not verified for the accepted image")
    checks = record.get("checks")
    if not isinstance(checks, dict) or set(checks) != set(REQUIRED_CHECKS):
        raise ValueError("deployment acceptance does not contain the fixed check set")
    current = checks["tenant_isolation"]
    if not isinstance(current, dict):
        raise ValueError("tenant_isolation check is malformed")
    if current.get("status") == "fail":
        raise ValueError("cannot replace a failed tenant_isolation check")
    bound = dict(current)
    bound.update({name: verified_evidence[name] for name in _TENANT_FIELDS})
    bound.update({"status": "pass", "evidence": evidence_ref})
    updated_checks = dict(checks)
    updated_checks["tenant_isolation"] = bound
    updated = dict(record)
    updated["checks"] = updated_checks
    # Validate the complete resulting record, including image and PITR rules.
    evaluate(updated)
    return updated


def bind_preflight(
    record: dict[str, Any], evidence: dict[str, Any], *, evidence_ref: str
) -> dict[str, Any]:
    """Return a new record with a verified deployment-preflight result bound."""

    if not isinstance(evidence_ref, str) or not evidence_ref.strip():
        raise ValueError("preflight evidence reference must not be empty")
    image_digest = record.get("image_digest")
    if not isinstance(image_digest, str):
        raise ValueError("deployment acceptance has no image digest")
    canonical = json.dumps(
        evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    verified_evidence = dict(evidence)
    verified_evidence.update(
        {
            "evidence_sha256": hashlib.sha256(canonical).hexdigest(),
            "evidence_bytes": len(canonical),
        }
    )
    if not _preflight_result_is_verified(verified_evidence, image_digest=image_digest):
        raise ValueError("preflight evidence is not verified for the accepted image")
    checks = record.get("checks")
    if not isinstance(checks, dict) or set(checks) != set(REQUIRED_CHECKS):
        raise ValueError("deployment acceptance does not contain the fixed check set")
    current = checks["preflight"]
    if not isinstance(current, dict):
        raise ValueError("preflight check is malformed")
    if current.get("status") == "fail":
        raise ValueError("cannot replace a failed preflight check")
    bound = dict(current)
    bound.update(
        {
            "schema_version": verified_evidence["schema_version"],
            "verifier": verified_evidence["verifier"],
            "status_result": verified_evidence["status"],
            "image_digest": verified_evidence["image_digest"],
            "checks": verified_evidence["checks"],
            "errors": verified_evidence["errors"],
            "evidence_sha256": verified_evidence["evidence_sha256"],
            "evidence_bytes": verified_evidence["evidence_bytes"],
            "status": "pass",
            "evidence": evidence_ref,
        }
    )
    updated_checks = dict(checks)
    updated_checks["preflight"] = bound
    updated = dict(record)
    updated["checks"] = updated_checks
    evaluate(updated)
    return updated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument("--tenant-isolation-evidence", type=Path, required=True)
    parser.add_argument("--evidence-ref", required=True)
    parser.add_argument("--preflight-evidence", type=Path)
    parser.add_argument("--preflight-evidence-ref")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = _read(args.record)
        if (args.preflight_evidence is None) != (args.preflight_evidence_ref is None):
            raise ValueError("preflight evidence and evidence reference must be supplied together")
        if args.preflight_evidence is not None and args.preflight_evidence_ref is not None:
            result = bind_preflight(
                result,
                _read(args.preflight_evidence),
                evidence_ref=args.preflight_evidence_ref,
            )
        result = bind_tenant_isolation(
            result,
            _read(args.tenant_isolation_evidence),
            evidence_ref=args.evidence_ref,
        )
        serialized = json.dumps(result, indent=2, sort_keys=True) + "\n"
        args.output.write_text(serialized, encoding="utf-8")
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(serialized, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
