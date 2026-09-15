"""The operator tool's files, arithmetic and exit codes, without PostgreSQL.

A backup that has never been restored is a file, not recovery capacity, so the
checks here are about the parts a drill cannot exercise: what a manifest
refuses to accept, what a verification catches, and which copies retention is
allowed to remove.
"""

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aftercare_agent.ops.backup import (
    BackupManifest,
    DatabaseSnapshot,
    MigrationRecord,
    RetentionDecision,
    RetentionPlan,
    RetentionPolicy,
    TableRows,
    apply_retention,
    default_backup_name,
    finalize_backup,
    load_manifest,
    load_manifests,
    main,
    plan_retention,
    verify_backup,
)
from aftercare_agent.ops.tooling import (
    BACKUP_NAME_PATTERN,
    CommandResult,
    IncompatibleToolError,
    MissingToolError,
    OpsError,
    child_environment,
    connection_parameters,
    conninfo_without_password,
    dsn_with_database,
    parse_major,
    resolve_tool,
    validate_backup_name,
    validate_database_name,
)
from aftercare_agent.persistence.db import known_migrations, latest_schema_version

CHECKSUM = "a" * 64
PAYLOAD = b"custom-format-dump"
TAKEN_AT = datetime(2026, 9, 15, 8, 0, tzinfo=UTC)
KNOWN = dict(known_migrations())


class _Runner:
    """A client binary that answers with whatever a test says it answers."""

    def __init__(self, *, version: str = "pg_dump (PostgreSQL) 16.13", returncode: int = 0) -> None:
        self.version = version
        self.returncode = returncode
        self.argv: tuple[str, ...] = ()

    def run(self, argv: Sequence[str], *, env: Mapping[str, str] | None = None) -> CommandResult:
        self.argv = tuple(argv)
        return CommandResult(tuple(argv), self.returncode, self.version, "")


def _snapshot(
    *, taken_at: datetime = TAKEN_AT, versions: tuple[int, ...] | None = None
) -> DatabaseSnapshot:
    # By default the snapshot describes the schema this build ships, so a
    # verification of it must agree with this build.
    records = (
        tuple(
            MigrationRecord(version=version, checksum=checksum)
            for version, checksum in sorted(KNOWN.items())
        )
        if versions is None
        else tuple(MigrationRecord(version=version, checksum=CHECKSUM) for version in versions)
    )
    return DatabaseSnapshot(
        database="aftercare",
        server_version="16.13",
        taken_at=taken_at,
        migrations=records,
        row_counts=(TableRows(table="aftercare_cases", rows=3),),
    )


def _manifest(name: str, taken_at: datetime, *, rows: int = 3) -> BackupManifest:
    return BackupManifest(
        name=name,
        created_at=taken_at,
        taken_at=taken_at,
        database="aftercare",
        server_version="16.13",
        schema_version=2,
        migrations=(
            MigrationRecord(version=1, checksum=CHECKSUM),
            MigrationRecord(version=2, checksum=CHECKSUM),
        ),
        row_counts=(TableRows(table="aftercare_cases", rows=rows),),
        dump_file=f"{name}.dump",
        dump_bytes=len(PAYLOAD),
        dump_sha256=hashlib.sha256(PAYLOAD).hexdigest(),
        tool_version="pg_dump (PostgreSQL) 16.13",
    )


def _write_backup(
    directory: Path,
    *,
    name: str = "daily-1",
    payload: bytes = PAYLOAD,
    taken_at: datetime = TAKEN_AT,
    versions: tuple[int, ...] | None = None,
) -> BackupManifest:
    directory.mkdir(parents=True, exist_ok=True)
    dump = directory / f"{name}.dump"
    dump.write_bytes(payload)
    return finalize_backup(
        directory,
        dump,
        snapshot=_snapshot(taken_at=taken_at, versions=versions),
        name=name,
        tool_version="pg_dump (PostgreSQL) 16.13",
    )


def test_a_finished_dump_becomes_a_manifest_that_round_trips(tmp_path: Path) -> None:
    manifest = _write_backup(tmp_path)
    assert manifest.dump_bytes == len(PAYLOAD)
    assert manifest.dump_sha256 == hashlib.sha256(PAYLOAD).hexdigest()
    assert manifest.schema_version == latest_schema_version()
    assert manifest.dump_path(tmp_path) == tmp_path / "daily-1.dump"
    assert manifest.manifest_path(tmp_path) == tmp_path / "daily-1.manifest.json"
    written = manifest.manifest_path(tmp_path).read_text(encoding="utf-8")
    assert written == manifest.to_json()
    assert BackupManifest.from_json(written) == manifest
    # Sorted keys keep two runs of the same backup byte-identical in a diff.
    assert written.splitlines()[1].startswith('  "created_at"')


def test_a_manifest_with_an_unknown_key_is_refused(tmp_path: Path) -> None:
    manifest = _write_backup(tmp_path)
    payload = manifest.model_dump(mode="json")
    payload["surprise"] = 1
    with pytest.raises(OpsError, match="not readable"):
        BackupManifest.from_json(json.dumps(payload))
    assert BackupManifest.from_json(manifest.to_json()) == manifest


def test_finalize_refuses_an_empty_dump_and_an_existing_manifest(tmp_path: Path) -> None:
    with pytest.raises(OpsError, match="empty"):
        _write_backup(tmp_path, payload=b"")
    _write_backup(tmp_path, name="kept")
    again = tmp_path / "kept.dump"
    again.write_bytes(PAYLOAD)
    with pytest.raises(OpsError, match="already exists"):
        finalize_backup(
            tmp_path,
            again,
            snapshot=_snapshot(),
            name="kept",
            tool_version="pg_dump (PostgreSQL) 16.13",
        )


def test_finalize_refuses_a_dump_that_is_not_there(tmp_path: Path) -> None:
    with pytest.raises(OpsError, match="does not exist"):
        finalize_backup(
            tmp_path,
            tmp_path / "ghost.dump",
            snapshot=_snapshot(),
            name="ghost",
            tool_version="pg_dump (PostgreSQL) 16.13",
        )


def test_backup_names_cannot_escape_the_directory() -> None:
    assert re.fullmatch(BACKUP_NAME_PATTERN, "aftercare-20260915T080000Z")
    for bad in ("../../etc/passwd", "a/b", "", ".hidden", "name with space", "n" * 200):
        with pytest.raises(OpsError):
            validate_backup_name(bad)
    assert validate_backup_name("daily-1") == "daily-1"


def test_a_default_name_is_the_database_and_its_recovery_point() -> None:
    assert default_backup_name(_snapshot()) == "aftercare-20260915T080000Z"


def test_verify_accepts_a_copy_this_build_can_still_read(tmp_path: Path) -> None:
    manifest = _write_backup(tmp_path)
    verification = verify_backup(tmp_path, manifest)
    assert verification.ok
    assert "backup daily-1: ok" in verification.table()
    older = _write_backup(tmp_path, name="yesterday", versions=(1,))
    older_verification = verify_backup(tmp_path, older, checksums={1: CHECKSUM, 2: CHECKSUM})
    assert older_verification.ok
    assert "1 migration(s) newer than this dump" in older_verification.table()


def test_verify_detects_a_changed_or_missing_dump(tmp_path: Path) -> None:
    manifest = _write_backup(tmp_path)
    manifest.dump_path(tmp_path).write_bytes(PAYLOAD + b"tampered")
    tampered = verify_backup(tmp_path, manifest)
    assert not tampered.ok
    assert "FAIL" in tampered.table()
    manifest.dump_path(tmp_path).unlink()
    missing = verify_backup(tmp_path, manifest)
    assert not missing.ok
    assert "missing" in missing.table()


def test_verify_flags_migration_drift_and_unknown_versions(tmp_path: Path) -> None:
    manifest = _write_backup(tmp_path)
    drifted = verify_backup(tmp_path, manifest, checksums={1: "b" * 64, 2: CHECKSUM})
    assert not drifted.ok
    assert "changed since the backup: 1" in drifted.table()
    unknown = verify_backup(tmp_path, manifest, checksums={1: CHECKSUM})
    assert not unknown.ok
    assert "no migration 2" in unknown.table()


def test_retention_keeps_the_newest_and_one_copy_per_day() -> None:
    manifests = [
        _manifest("newest-a", datetime(2026, 9, 15, 9, 0, tzinfo=UTC)),
        _manifest("newest-b", datetime(2026, 9, 15, 8, 0, tzinfo=UTC)),
        _manifest("day-14-a", datetime(2026, 9, 14, 20, 0, tzinfo=UTC)),
        _manifest("day-14-b", datetime(2026, 9, 14, 9, 0, tzinfo=UTC)),
        _manifest("day-13", datetime(2026, 9, 13, 9, 0, tzinfo=UTC)),
        _manifest("day-12", datetime(2026, 9, 12, 9, 0, tzinfo=UTC)),
    ]
    plan = plan_retention(manifests, RetentionPolicy(keep_last=2, keep_daily=2), now=TAKEN_AT)
    assert plan.kept == ("newest-a", "newest-b", "day-14-a")
    assert plan.deleted == ("day-14-b", "day-13", "day-12")
    assert "3 kept, 3 to delete" in plan.table()
    assert plan.to_json()["deleted"] == ["day-14-b", "day-13", "day-12"]


def test_retention_with_no_daily_rule_keeps_only_the_newest() -> None:
    manifests = [
        _manifest("newest", datetime(2026, 9, 15, 9, 0, tzinfo=UTC)),
        _manifest("older", datetime(2026, 9, 1, 9, 0, tzinfo=UTC)),
    ]
    plan = plan_retention(manifests, RetentionPolicy(keep_last=1, keep_daily=0), now=TAKEN_AT)
    assert plan.kept == ("newest",)
    assert plan.deleted == ("older",)


def test_a_policy_never_keeps_nothing() -> None:
    with pytest.raises(OpsError, match="at least one"):
        RetentionPolicy(keep_last=0)
    with pytest.raises(OpsError, match="negative"):
        RetentionPolicy(keep_daily=-1)
    assert plan_retention([], RetentionPolicy()).decisions == ()


def test_apply_retention_removes_the_dump_and_the_manifest(tmp_path: Path) -> None:
    for name in ("keep-me", "delete-me"):
        _write_backup(tmp_path, name=name)
    plan = RetentionPlan(
        policy=RetentionPolicy(keep_last=1, keep_daily=0),
        decided_at=TAKEN_AT,
        decisions=(
            RetentionDecision("keep-me", TAKEN_AT, True, "newest"),
            RetentionDecision("delete-me", TAKEN_AT - timedelta(days=1), False, "beyond"),
        ),
    )
    removed = apply_retention(tmp_path, plan)
    assert {path.name for path in removed} == {"delete-me.dump", "delete-me.manifest.json"}
    assert [manifest.name for manifest in load_manifests(tmp_path)] == ["keep-me"]


def test_apply_retention_refuses_to_empty_the_directory(tmp_path: Path) -> None:
    _write_backup(tmp_path, name="only-one")
    plan = RetentionPlan(
        policy=RetentionPolicy(keep_last=1),
        decided_at=TAKEN_AT,
        decisions=(RetentionDecision("only-one", TAKEN_AT, False, "beyond"),),
    )
    with pytest.raises(OpsError, match="refusing to delete every backup"):
        apply_retention(tmp_path, plan)
    assert (tmp_path / "only-one.dump").is_file()


def test_apply_retention_refuses_a_name_that_leaves_the_directory(tmp_path: Path) -> None:
    outside = tmp_path / "escape.dump"
    outside.write_bytes(PAYLOAD)
    plan = RetentionPlan(
        policy=RetentionPolicy(keep_last=1),
        decided_at=TAKEN_AT,
        decisions=(
            RetentionDecision("keep-me", TAKEN_AT, True, "newest"),
            RetentionDecision("../escape", TAKEN_AT, False, "beyond"),
        ),
    )
    with pytest.raises(OpsError, match="letters, digits"):
        apply_retention(tmp_path, plan)
    assert outside.is_file()


def test_loading_manifests_is_ordered_and_a_missing_one_is_reported(tmp_path: Path) -> None:
    second = _write_backup(tmp_path, name="second")
    first = _write_backup(tmp_path, name="first", taken_at=TAKEN_AT - timedelta(days=1))
    assert [manifest.name for manifest in load_manifests(tmp_path)] == ["first", "second"]
    assert load_manifest(tmp_path).name == "second"
    assert load_manifest(tmp_path, "first").name == "first"
    assert load_manifest(tmp_path, "first") == first
    assert second.taken_at > first.taken_at
    with pytest.raises(OpsError, match="does not exist"):
        load_manifest(tmp_path, "ghost")
    with pytest.raises(OpsError, match="holds no backup manifest"):
        load_manifest(tmp_path / "empty")


def test_a_version_string_is_read_by_its_major_number() -> None:
    assert parse_major("pg_dump (PostgreSQL) 16.13 (Ubuntu 16.13-0ubuntu0.24.04.1)") == 16
    assert parse_major("pg_restore (PostgreSQL) 17.2") == 17
    assert parse_major("16.13") == 16
    with pytest.raises(OpsError, match="version number"):
        parse_major("no version here")


def test_a_client_binary_is_checked_before_it_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AFTERCARE_PG_DUMP", raising=False)
    monkeypatch.setenv("PATH", "")
    with pytest.raises(MissingToolError, match="not on PATH"):
        resolve_tool("pg_dump", env_var="AFTERCARE_PG_DUMP", runner=_Runner())

    monkeypatch.setenv("AFTERCARE_PG_DUMP", "pg_dump")
    tool = resolve_tool("pg_dump", env_var="AFTERCARE_PG_DUMP", runner=_Runner())
    assert tool.major == 16 and tool.path == "pg_dump"
    with pytest.raises(IncompatibleToolError, match="cannot read PostgreSQL 17"):
        resolve_tool("pg_dump", env_var="AFTERCARE_PG_DUMP", runner=_Runner(), required_major=17)
    with pytest.raises(MissingToolError, match="--version failed"):
        resolve_tool("pg_dump", env_var="AFTERCARE_PG_DUMP", runner=_Runner(returncode=1))


def test_the_dsn_a_child_process_sees_has_no_password() -> None:
    dsn = "postgresql://aftercare:local-only@db.example:5432/aftercare?sslmode=require"
    command_line = conninfo_without_password(dsn)
    assert "local-only" not in command_line
    assert "sslmode=require" in command_line
    assert "db.example" in command_line
    assert child_environment(dsn) == {"PGPASSWORD": "local-only"}
    assert child_environment("postgresql://aftercare@db.example/aftercare") == {}
    assert "local-only" in dsn_with_database(dsn, "aftercare_drill_1")


def test_a_database_url_is_required_and_must_be_readable() -> None:
    with pytest.raises(OpsError, match="required"):
        connection_parameters("")
    with pytest.raises(OpsError, match="cannot parse"):
        connection_parameters("not-a-url-at-all")
    with pytest.raises(OpsError, match="cannot parse"):
        connection_parameters("postgresql://")


def test_scratch_database_names_must_be_plain_identifiers() -> None:
    assert validate_database_name("aftercare_drill_20260915T080000") == (
        "aftercare_drill_20260915T080000"
    )
    for bad in ("with space", "no-dot.", 'quoted"name', "aftercare;drop", ""):
        with pytest.raises(OpsError, match="database name"):
            validate_database_name(bad)


def test_the_cli_verifies_a_copy_and_reports_a_change(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_backup(tmp_path)
    assert main(["verify", "--directory", str(tmp_path)]) == 0
    assert "backup daily-1: ok" in capsys.readouterr().out
    (tmp_path / "daily-1.dump").write_bytes(b"changed")
    assert main(["verify", "--directory", str(tmp_path), "--all"]) == 1
    assert "FAIL" in capsys.readouterr().out
    assert main(["verify", "--directory", str(tmp_path), "--name", "ghost"]) == 2
    assert "does not exist" in capsys.readouterr().err


def test_the_cli_plans_retention_before_it_deletes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_backup(tmp_path, name="older", taken_at=TAKEN_AT - timedelta(days=1))
    _write_backup(tmp_path, name="newest")
    options = ["retention", "--directory", str(tmp_path), "--keep-last", "1", "--keep-daily", "0"]
    assert main(options) == 0
    assert "1 kept, 1 to delete" in capsys.readouterr().out
    assert (tmp_path / "older.dump").is_file()
    assert main([*options, "--apply"]) == 0
    assert "deleted 2 file(s)" in capsys.readouterr().out
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "newest.dump",
        "newest.manifest.json",
    ]


def test_the_cli_needs_a_database_url_for_the_commands_that_touch_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert main(["create", "--directory", str(tmp_path)]) == 2
    assert "DATABASE_URL" in capsys.readouterr().err
    assert main(["drill", "--directory", str(tmp_path)]) == 2
    assert main(["reconcile", "--recovery-point", "2026-09-15T08:00:00Z"]) == 2


def test_the_cli_refuses_a_recovery_point_it_cannot_read(
    capsys: pytest.CaptureFixture[str],
) -> None:
    dsn = "postgresql://aftercare@db.example/aftercare"
    assert main(["reconcile", "--dsn", dsn, "--recovery-point", "yesterday"]) == 2
    assert "ISO-8601" in capsys.readouterr().err
    assert main(["reconcile", "--dsn", dsn, "--recovery-point", "2026-09-15T08:00:00"]) == 2
    assert "timezone offset" in capsys.readouterr().err
