"""Validate evidence produced by a real PostgreSQL point-in-time recovery.

PITR starts from a *physical* base backup produced by ``pg_basebackup`` and
replays archived WAL.  It is a different artifact from the logical
``pg_dump`` files handled by :mod:`aftercare_agent.ops.backup`; confusing the
two would make a recovery report look bound while proving nothing about WAL
replay.  This module therefore validates a physical base-backup manifest and
an external recovery report separately.

No function here starts PostgreSQL or claims that a recovery happened.  A
platform-specific runner must do that work and record references to its
archive, target-reached and application-state evidence.  Missing or mixed
evidence is a hold, never a pass.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import Field, ValidationError, model_validator

from aftercare_agent.domain.common import NonNegativeInt, Sha256, UtcDatetime

from .tooling import BackupName, OpsError, OpsModel
from .wal_archive import parse_lsn

PITR_EVIDENCE_VERSION = 1
PITR_CHECKS = (
    "base_backup",
    "wal_archive",
    "target_reached",
    "application_state",
    "action_reconciliation",
)
_ARTIFACT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_LSN = r"^[0-9A-Fa-f]{1,8}/[0-9A-Fa-f]{1,8}$"


class PhysicalBackupManifest(OpsModel):
    """A digest and WAL interval for one physical ``pg_basebackup`` artifact."""

    schema_version: Literal[1] = 1
    name: BackupName
    artifact: str = Field(pattern=_ARTIFACT.pattern)
    created_at: UtcDatetime
    started_at: UtcDatetime
    finished_at: UtcDatetime
    server_version: str = Field(min_length=1, max_length=200)
    wal_start_lsn: str = Field(pattern=_LSN)
    wal_stop_lsn: str = Field(pattern=_LSN)
    artifact_bytes: NonNegativeInt
    artifact_sha256: Sha256

    @model_validator(mode="after")
    def _valid_interval(self) -> PhysicalBackupManifest:
        if self.finished_at < self.started_at:
            raise ValueError("physical backup finished_at precedes started_at")
        if self.artifact_bytes <= 0:
            raise ValueError("physical backup artifact is empty")
        if parse_lsn(self.wal_stop_lsn) < parse_lsn(self.wal_start_lsn):
            raise ValueError("physical backup WAL interval is reversed")
        return self

    def to_json(self) -> str:
        return (
            json.dumps(self.model_dump(mode="json"), indent=2, sort_keys=True, ensure_ascii=False)
            + "\n"
        )

    @classmethod
    def from_json(cls, text: str, *, source: str) -> PhysicalBackupManifest:
        try:
            return cls.model_validate_json(text)
        except ValidationError as exc:
            raise OpsError(f"{source} is not a readable physical backup manifest: {exc}") from exc


class PitrCheck(OpsModel):
    """One externally observed property of a PITR rehearsal."""

    status: Literal["pass", "fail", "not_run"]
    evidence: str | None = None
    detail: str = Field(default="", max_length=400)

    @model_validator(mode="after")
    def _pass_needs_evidence(self) -> PitrCheck:
        if self.status == "pass" and (self.evidence is None or not self.evidence.strip()):
            raise ValueError("a passed PITR check needs an evidence reference")
        return self


class PitrEvidence(OpsModel):
    """Versioned output expected from a deployment-specific PITR runner."""

    schema_version: Literal[1] = 1
    base_backup_name: BackupName
    base_backup_sha256: Sha256
    target_time: UtcDatetime
    started_at: UtcDatetime
    finished_at: UtcDatetime
    recovered_lsn: str = Field(pattern=_LSN)
    wal_replayed_segments: NonNegativeInt
    checks: dict[str, PitrCheck]

    @model_validator(mode="after")
    def _shape_and_clock(self) -> PitrEvidence:
        if set(self.checks) != set(PITR_CHECKS):
            raise ValueError("PITR evidence must contain the fixed check set")
        if self.finished_at < self.started_at:
            raise ValueError("PITR finished_at precedes started_at")
        if self.target_time > self.finished_at:
            raise ValueError("PITR target_time is after the recovery finished")
        return self

    @property
    def rto_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()

    def to_json(self) -> str:
        return (
            json.dumps(self.model_dump(mode="json"), indent=2, sort_keys=True, ensure_ascii=False)
            + "\n"
        )

    @classmethod
    def from_json(cls, text: str, *, source: str) -> PitrEvidence:
        try:
            return cls.model_validate_json(text)
        except ValidationError as exc:
            raise OpsError(f"{source} is not readable PITR evidence: {exc}") from exc


class PhysicalBackupLike(Protocol):
    """The manifest fields needed by the verifier, kept import-cycle free."""

    @property
    def name(self) -> str: ...

    @property
    def artifact(self) -> str: ...

    @property
    def artifact_bytes(self) -> int: ...

    @property
    def artifact_sha256(self) -> str: ...

    @property
    def finished_at(self) -> datetime: ...

    @property
    def wal_stop_lsn(self) -> str: ...


def _parse_evidence(value: PitrEvidence | Mapping[str, Any]) -> PitrEvidence:
    if isinstance(value, PitrEvidence):
        return value
    try:
        return PitrEvidence.model_validate(value)
    except ValidationError as exc:
        raise OpsError(f"PITR evidence is not readable: {exc}") from exc


def verify_pitr(
    manifest: PhysicalBackupLike, evidence: PitrEvidence | Mapping[str, Any]
) -> dict[str, Any]:
    """Bind PITR evidence to one physical base-backup manifest."""

    record = _parse_evidence(evidence)
    if record.base_backup_name != manifest.name:
        raise OpsError("PITR evidence names a different physical base backup")
    if record.base_backup_sha256 != manifest.artifact_sha256:
        raise OpsError("PITR evidence is bound to a different physical base backup")
    if record.target_time <= manifest.finished_at:
        raise OpsError("PITR target_time must be after the physical base backup finished")
    if parse_lsn(record.recovered_lsn) <= parse_lsn(manifest.wal_stop_lsn):
        raise OpsError("PITR recovered_lsn does not advance beyond the base backup WAL")

    checks = {
        name: {
            "status": record.checks[name].status,
            "evidence": record.checks[name].evidence,
            "detail": record.checks[name].detail,
        }
        for name in PITR_CHECKS
    }
    missing = [name for name, check in checks.items() if check["status"] == "not_run"]
    failed = [name for name, check in checks.items() if check["status"] == "fail"]
    return {
        "schema_version": PITR_EVIDENCE_VERSION,
        "verifier": "aftercare-pitr",
        "decision": "pass" if not missing and not failed else "hold",
        "base_backup_name": manifest.name,
        "base_backup_artifact": manifest.artifact,
        "base_backup_sha256": manifest.artifact_sha256,
        "base_backup_finished_at": manifest.finished_at.astimezone(UTC).isoformat(),
        "target_time": record.target_time.astimezone(UTC).isoformat(),
        "recovered_lsn": record.recovered_lsn,
        "wal_replayed_segments": record.wal_replayed_segments,
        "started_at": record.started_at.astimezone(UTC).isoformat(),
        "finished_at": record.finished_at.astimezone(UTC).isoformat(),
        "rto_seconds": round(record.rto_seconds, 3),
        "checks": checks,
        "missing_checks": missing,
        "failed_checks": failed,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_pitr_file(manifest_path: Path, evidence_path: Path) -> dict[str, Any]:
    """Load, hash-check and verify a physical manifest plus recovery report."""

    try:
        manifest = PhysicalBackupManifest.from_json(
            manifest_path.read_text(encoding="utf-8"), source=str(manifest_path)
        )
        artifact = manifest_path.parent / manifest.artifact
        if not artifact.is_file():
            raise OpsError(f"physical backup artifact {artifact} is missing")
        if artifact.stat().st_size != manifest.artifact_bytes:
            raise OpsError("physical backup artifact size differs from its manifest")
        if _sha256(artifact) != manifest.artifact_sha256:
            raise OpsError("physical backup artifact SHA-256 differs from its manifest")
        raw = evidence_path.read_bytes()
    except (OSError, UnicodeError) as exc:
        raise OpsError(f"cannot read PITR input: {exc}") from exc
    evidence = PitrEvidence.from_json(raw.decode("utf-8"), source=str(evidence_path))
    result = verify_pitr(manifest, evidence)
    result["manifest_file"] = str(manifest_path)
    result["evidence_file"] = str(evidence_path)
    result["manifest_sha256"] = _sha256(manifest_path)
    result["evidence_sha256"] = _sha256(evidence_path)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-manifest", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--json", dest="json_path", type=Path)
    args = parser.parse_args(argv)
    try:
        result = verify_pitr_file(args.base_manifest, args.evidence)
    except (OSError, UnicodeError, OpsError) as exc:
        print(f"aftercare-backup pitr: {exc}")
        return 2
    serialized = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.json_path is not None:
        args.json_path.write_text(serialized, encoding="utf-8", newline="\n")
    print(serialized, end="")
    return 0 if result["decision"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
