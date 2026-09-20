from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from deploy.verify_rls_evidence import normalize_rls_evidence

IMAGE = "sha256:" + "a" * 64


def _harden(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "verifier": "aftercare-rls",
        "decision": "pass",
        "image_digest": IMAGE,
        "runtime_role": "aftercare_runtime",
        "tenant_table_count": 2,
        "tenant_tables": ["aftercare_cases", "aftercare_runs"],
        "forced": True,
    }
    value.update(overrides)
    return value


def _verify(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "verifier": "aftercare-rls",
        "decision": "pass",
        "image_digest": IMAGE,
        "runtime_role": "aftercare_runtime",
        "tenant_table_count": 2,
        "empty_context_rows": 0,
        "cross_tenant_write": "rejected",
        "cross_tenant_update": "hidden",
        "cross_tenant_delete": "hidden",
    }
    value.update(overrides)
    return value


def test_normalize_rls_evidence_binds_both_reports_and_source_digests(tmp_path: Path) -> None:
    harden_path = tmp_path / "rls-harden.json"
    verify_path = tmp_path / "rls-verify.json"
    harden_path.write_text(json.dumps(_harden()), encoding="utf-8")
    verify_path.write_text(json.dumps(_verify()), encoding="utf-8")
    result = normalize_rls_evidence(
        _harden(),
        _verify(),
        image_digest=IMAGE,
        harden_source={
            "bytes": harden_path.stat().st_size,
            "sha256": hashlib.sha256(harden_path.read_bytes()).hexdigest(),
        },
        verify_source={
            "bytes": verify_path.stat().st_size,
            "sha256": hashlib.sha256(verify_path.read_bytes()).hexdigest(),
        },
    )

    assert result["decision"] == "pass"
    assert result["evidence_type"] == "tenant_isolation"
    assert result["hardened"] is True
    assert result["source"]["verify"]["bytes"] == verify_path.stat().st_size


@pytest.mark.parametrize(
    "change",
    [
        {"forced": False},
        {"tenant_table_count": 1},
        {"cross_tenant_write": "accepted"},
        {"image_digest": "sha256:" + "b" * 64},
    ],
)
def test_normalize_rls_evidence_rejects_unbound_or_failed_reports(
    change: dict[str, object],
) -> None:
    harden_change = change if "forced" in change or "tenant_table_count" in change else {}
    verify_change = change if "cross_tenant_write" in change else {}
    harden = _harden(**harden_change)
    verify = _verify(**verify_change)
    if "image_digest" in change:
        harden["image_digest"] = change["image_digest"]

    with pytest.raises(ValueError):
        normalize_rls_evidence(harden, verify, image_digest=IMAGE)
