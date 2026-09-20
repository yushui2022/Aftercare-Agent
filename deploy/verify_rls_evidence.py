"""Normalize and bind the two ``aftercare-rls`` reports for deployment evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read RLS evidence {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"RLS evidence {path} must be a JSON object")
    return value


def _file_digest(path: Path) -> dict[str, int | str]:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read RLS evidence bytes {path}: {exc}") from exc
    return {"bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}


def _report(value: dict[str, Any], *, name: str, image_digest: str) -> None:
    if value.get("schema_version") != 1 or value.get("verifier") != "aftercare-rls":
        raise ValueError(f"{name} report has an unsupported verifier schema")
    if value.get("decision") != "pass":
        raise ValueError(f"{name} report is not a passing result")
    if value.get("image_digest") != image_digest:
        raise ValueError(f"{name} report does not match the accepted image digest")
    if not isinstance(value.get("runtime_role"), str) or not value["runtime_role"]:
        raise ValueError(f"{name} report has no runtime role")
    count = value.get("tenant_table_count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise ValueError(f"{name} report has no tenant table count")


def normalize_rls_evidence(
    harden: dict[str, Any],
    verify: dict[str, Any],
    *,
    image_digest: str,
    harden_source: dict[str, int | str] | None = None,
    verify_source: dict[str, int | str] | None = None,
) -> dict[str, Any]:
    """Return a stable tenant-isolation evidence record.

    Both reports are required so a passing runtime probe cannot be detached
    from the owner-side policy hardening step.  The caller supplies the image
    digest from the deployment record; it is never inferred from the reports.
    """

    if _IMAGE_DIGEST.fullmatch(image_digest) is None:
        raise ValueError("image digest must be sha256: followed by 64 lowercase hex characters")
    _report(harden, name="harden", image_digest=image_digest)
    _report(verify, name="verify", image_digest=image_digest)
    if harden.get("forced") is not True:
        raise ValueError("harden report does not prove FORCE ROW LEVEL SECURITY")
    runtime_role = harden["runtime_role"]
    if verify["runtime_role"] != runtime_role:
        raise ValueError("harden and verify reports use different runtime roles")
    table_count = harden["tenant_table_count"]
    if verify["tenant_table_count"] != table_count:
        raise ValueError("harden and verify reports use different tenant table counts")
    tables = harden.get("tenant_tables")
    if (
        not isinstance(tables, list)
        or len(tables) != table_count
        or not all(isinstance(table, str) and table for table in tables)
    ):
        raise ValueError("harden report has an invalid tenant table list")
    expected_probes = {
        "empty_context_rows": 0,
        "cross_tenant_write": "rejected",
        "cross_tenant_update": "hidden",
        "cross_tenant_delete": "hidden",
    }
    if any(verify.get(key) != expected for key, expected in expected_probes.items()):
        raise ValueError("verify report does not contain all passing tenant-isolation probes")
    result: dict[str, Any] = {
        "schema_version": 1,
        "verifier": "aftercare-rls",
        "evidence_type": "tenant_isolation",
        "decision": "pass",
        "image_digest": image_digest,
        "runtime_role": runtime_role,
        "tenant_table_count": table_count,
        "empty_context_rows": verify["empty_context_rows"],
        "cross_tenant_write": verify["cross_tenant_write"],
        "cross_tenant_update": verify["cross_tenant_update"],
        "cross_tenant_delete": verify["cross_tenant_delete"],
        "hardened": True,
        "source": {
            "harden": harden_source,
            "verify": verify_source,
        },
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harden", type=Path, required=True)
    parser.add_argument("--verify", type=Path, required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = normalize_rls_evidence(
            _read(args.harden),
            _read(args.verify),
            image_digest=args.image_digest,
            harden_source=_file_digest(args.harden),
            verify_source=_file_digest(args.verify),
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    serialized = json.dumps(result, indent=2, sort_keys=True) + "\n"
    try:
        args.output.write_text(serialized, encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"cannot write RLS evidence {args.output}: {exc}") from exc
    print(serialized, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
