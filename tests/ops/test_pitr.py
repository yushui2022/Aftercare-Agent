from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aftercare_agent.ops.backup import main
from aftercare_agent.ops.pitr import (
    PITR_CHECKS,
    PhysicalBackupManifest,
    PitrEvidence,
    verify_pitr,
)
from aftercare_agent.ops.tooling import OpsError

STARTED = datetime(2026, 9, 20, 8, 0, tzinfo=UTC)
FINISHED = datetime(2026, 9, 20, 8, 0, 3, tzinfo=UTC)
DIGEST = "a" * 64


def _manifest(*, wal_stop_lsn: str = "0/16B3748") -> PhysicalBackupManifest:
    return PhysicalBackupManifest(
        name="base-20260920T080000Z",
        artifact="base.tar",
        created_at=FINISHED,
        started_at=STARTED,
        finished_at=FINISHED,
        server_version="17.11",
        wal_start_lsn="0/16B0000",
        wal_stop_lsn=wal_stop_lsn,
        artifact_bytes=1,
        artifact_sha256=DIGEST,
    )


def _evidence(**overrides: object) -> dict[str, object]:
    checks = {name: {"status": "pass", "evidence": f"evidence/{name}.json"} for name in PITR_CHECKS}
    value: dict[str, object] = {
        "schema_version": 1,
        "base_backup_name": "base-20260920T080000Z",
        "base_backup_sha256": DIGEST,
        "target_time": "2026-09-20T08:00:05Z",
        "started_at": "2026-09-20T08:01:00Z",
        "finished_at": "2026-09-20T08:01:03Z",
        "recovered_lsn": "0/16B4000",
        "wal_replayed_segments": 1,
        "checks": checks,
    }
    value.update(overrides)
    return value


def test_pitr_verifier_binds_a_physical_backup_and_keeps_rto() -> None:
    result = verify_pitr(_manifest(), _evidence())

    assert result["decision"] == "pass"
    assert result["base_backup_sha256"] == DIGEST
    assert result["rto_seconds"] == 3.0


def test_pitr_holds_when_a_required_check_was_not_run() -> None:
    checks = _evidence()["checks"]
    assert isinstance(checks, dict)
    checks["application_state"] = {"status": "not_run"}

    result = verify_pitr(_manifest(), _evidence(checks=checks))

    assert result["decision"] == "hold"
    assert result["missing_checks"] == ["application_state"]


def test_pitr_rejects_mixed_base_digest_or_non_advancing_recovery() -> None:
    with pytest.raises(OpsError, match="different physical base"):
        verify_pitr(_manifest(), _evidence(base_backup_sha256="b" * 64))
    with pytest.raises(OpsError, match="does not advance"):
        verify_pitr(_manifest(), _evidence(recovered_lsn="0/16B3748"))


def test_pitr_rejects_target_at_or_before_base_finish() -> None:
    with pytest.raises(OpsError, match="after the physical base"):
        verify_pitr(_manifest(), _evidence(target_time=FINISHED.isoformat()))


def test_passed_check_without_external_evidence_is_rejected() -> None:
    checks = _evidence()["checks"]
    assert isinstance(checks, dict)
    checks["wal_archive"] = {"status": "pass", "evidence": ""}

    with pytest.raises(ValueError, match="passed PITR check"):
        PitrEvidence.model_validate(_evidence(checks=checks))


def test_pitr_command_hash_checks_the_physical_artifact(tmp_path: Path) -> None:
    directory = tmp_path / "base"
    directory.mkdir()
    artifact = directory / "base.tar"
    artifact.write_bytes(b"physical-base")
    manifest = _manifest()
    manifest = manifest.model_copy(
        update={
            "artifact_bytes": artifact.stat().st_size,
            "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        }
    )
    manifest_path = directory / "base.manifest.json"
    manifest_path.write_text(manifest.to_json(), encoding="utf-8")
    evidence_path = tmp_path / "pitr.json"
    evidence_path.write_text(
        json.dumps(_evidence(base_backup_sha256=manifest.artifact_sha256)),
        encoding="utf-8",
    )
    output = tmp_path / "verified.json"

    assert (
        main(
            [
                "pitr",
                "--base-manifest",
                str(manifest_path),
                "--evidence",
                str(evidence_path),
                "--json",
                str(output),
            ]
        )
        == 0
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["verifier"] == "aftercare-pitr"
    assert payload["manifest_sha256"]
