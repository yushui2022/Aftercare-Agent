"""Back up PostgreSQL, drill the restore, and keep the copies bounded.

``aftercare-backup`` is an operator tool, not part of the request path.  A file
on disk only becomes recovery capacity once a restore has been rehearsed, so a
run produces three things: the dump, a manifest that says what the dump
contains, and a drill report that says what happened when that dump was
restored into a scratch database.  A backup job that exited zero is none of
them.

The dump and the manifest come from one repeatable-read transaction: the
transaction exports a snapshot, reads the schema version, the migration
checksums and the row counts from that snapshot, and hands the same snapshot to
``pg_dump --snapshot``.  The counts in the manifest therefore describe the dump
exactly, which is what makes "the restored database has the same rows" a check
rather than an approximation.

Nothing here talks to a supplier, a model or the runtime.  Recovering a
database does not undo the external actions taken after the recovery point;
that part is :mod:`aftercare_agent.ops.reconcile`.
"""

import argparse
import contextlib
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import psycopg
from psycopg import sql
from pydantic import Field, ValidationError

from aftercare_agent.domain.common import NonNegativeInt, Sha256, UtcDatetime
from aftercare_agent.ops.reconcile import (
    DEFAULT_RECONCILE_LIMIT,
    DEFAULT_SAFETY_MARGIN_SECONDS,
    build_reconciliation,
)
from aftercare_agent.ops.tooling import (
    DUMP_ENV,
    MAINTENANCE_DATABASES,
    RESTORE_ENV,
    CommandRunner,
    OpsError,
    OpsModel,
    SubprocessRunner,
    Tool,
    child_environment,
    conninfo_without_password,
    dsn_with_database,
    parse_major,
    resolve_tool,
    validate_database_name,
)
from aftercare_agent.persistence.db import known_migrations, latest_schema_version, migrate

DUMP_SUFFIX = ".dump"
MANIFEST_SUFFIX = ".manifest.json"
DEFAULT_KEEP_LAST = 3
DEFAULT_KEEP_DAILY = 7
DEFAULT_SCRATCH_PREFIX = "aftercare_drill_"
BACKUP_NAME_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}"

type BackupName = Annotated[
    str, Field(min_length=1, max_length=96, pattern=rf"^{BACKUP_NAME_PATTERN}$")
]
type DatabaseName = Annotated[
    str, Field(min_length=1, max_length=63, pattern=r"^[A-Za-z0-9_][A-Za-z0-9_$-]*$")
]
type TableName = Annotated[
    str, Field(min_length=1, max_length=63, pattern=r"^[A-Za-z_][A-Za-z0-9_$]*$")
]
type VersionText = Annotated[str, Field(min_length=1, max_length=200)]

# --------------------------------------------------------------------------
# Describing a database and a dump
# --------------------------------------------------------------------------


class MigrationRecord(OpsModel):
    """One row of ``aftercare_schema_migrations`` as the backup saw it."""

    version: NonNegativeInt
    checksum: Sha256


class TableRows(OpsModel):
    table: TableName
    rows: NonNegativeInt


class DatabaseSnapshot(OpsModel):
    """What one repeatable-read transaction saw, and when it saw it."""

    database: DatabaseName
    server_version: VersionText
    taken_at: UtcDatetime
    migrations: tuple[MigrationRecord, ...]
    row_counts: tuple[TableRows, ...]

    @property
    def schema_version(self) -> int:
        return max((record.version for record in self.migrations), default=0)


class BackupManifest(OpsModel):
    """Everything a restore needs to know about one dump.

    A dump alone cannot answer "which schema is in here" or "how many rows
    should come back"; without those answers a restore is a file copy that
    happened to exit zero.
    """

    manifest_version: Literal[1] = 1
    name: BackupName
    created_at: UtcDatetime
    taken_at: UtcDatetime
    database: DatabaseName
    server_version: VersionText
    schema_version: NonNegativeInt
    migrations: tuple[MigrationRecord, ...]
    row_counts: tuple[TableRows, ...]
    dump_file: str
    dump_bytes: NonNegativeInt
    dump_sha256: Sha256
    tool_version: VersionText

    def dump_path(self, directory: Path) -> Path:
        return directory / self.dump_file

    def manifest_path(self, directory: Path) -> Path:
        return directory / f"{self.name}{MANIFEST_SUFFIX}"

    def to_json(self) -> str:
        payload = self.model_dump(mode="json")
        return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    @classmethod
    def from_json(cls, text: str) -> "BackupManifest":
        try:
            return cls.model_validate_json(text)
        except ValidationError as exc:
            raise OpsError(f"manifest is not readable: {exc}") from exc


def validate_backup_name(name: str) -> str:
    """Refuse a name that could escape the backup directory."""
    if not re.fullmatch(BACKUP_NAME_PATTERN, name):
        raise OpsError(
            f"backup names take letters, digits, dot, dash and underscore (got {name!r})"
        )
    return name


def _scalar(connection: psycopg.Connection[Any], query: str) -> Any:
    row = connection.execute(query).fetchone()
    if row is None:
        raise OpsError(f"the server returned no row for {query!r}")
    return row[0]


def read_migrations(connection: psycopg.Connection[Any]) -> tuple[MigrationRecord, ...]:
    rows = connection.execute(
        "SELECT version, checksum FROM aftercare_schema_migrations ORDER BY version"
    ).fetchall()
    records = []
    for version, checksum in rows:
        if checksum is None:
            raise OpsError(
                f"migration {version} has no pinned checksum; start the service once so "
                "migrate() pins it before the database is backed up"
            )
        records.append(MigrationRecord(version=int(version), checksum=str(checksum).strip()))
    return tuple(records)


def read_row_counts(connection: psycopg.Connection[Any]) -> tuple[TableRows, ...]:
    """Row counts for every Aftercare table, read in the caller's snapshot."""
    names = connection.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE' "
        "AND starts_with(table_name, 'aftercare_') ORDER BY table_name"
    ).fetchall()
    counts = []
    for (name,) in names:
        table = str(name)
        total = connection.execute(
            sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
        ).fetchone()
        counts.append(TableRows(table=table, rows=0 if total is None else int(total[0])))
    return tuple(counts)


def read_schema(
    connection: psycopg.Connection[Any],
) -> tuple[tuple[MigrationRecord, ...], tuple[TableRows, ...]]:
    return read_migrations(connection), read_row_counts(connection)


def read_snapshot(connection: psycopg.Connection[Any]) -> DatabaseSnapshot:
    """Describe this database.  The caller must already be in its snapshot."""
    return DatabaseSnapshot(
        database=str(_scalar(connection, "SELECT current_database()")),
        server_version=str(_scalar(connection, "SHOW server_version")),
        taken_at=_scalar(connection, "SELECT clock_timestamp()"),
        migrations=read_migrations(connection),
        row_counts=read_row_counts(connection),
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def default_backup_name(snapshot: DatabaseSnapshot) -> str:
    stamp = snapshot.taken_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{snapshot.database}-{stamp}"


def finalize_backup(
    directory: Path,
    dump_path: Path,
    *,
    snapshot: DatabaseSnapshot,
    name: str,
    tool_version: str,
    created_at: datetime | None = None,
) -> BackupManifest:
    """Hash a finished dump and write the manifest beside it."""
    validate_backup_name(name)
    manifest_path = directory / f"{name}{MANIFEST_SUFFIX}"
    if manifest_path.exists():
        raise OpsError(f"{manifest_path} already exists; pick another name for this dump")
    if not dump_path.is_file():
        raise OpsError(f"{dump_path} does not exist")
    size = dump_path.stat().st_size
    if size == 0:
        raise OpsError(f"{dump_path} is empty; the dump wrote no data")
    try:
        manifest = BackupManifest(
            name=name,
            created_at=created_at or datetime.now(UTC),
            taken_at=snapshot.taken_at,
            database=snapshot.database,
            server_version=snapshot.server_version,
            schema_version=snapshot.schema_version,
            migrations=snapshot.migrations,
            row_counts=snapshot.row_counts,
            dump_file=dump_path.name,
            dump_bytes=size,
            dump_sha256=_sha256_file(dump_path),
            tool_version=tool_version,
        )
    except ValidationError as exc:
        raise OpsError(f"cannot describe the backup: {exc}") from exc
    manifest_path.write_text(manifest.to_json(), encoding="utf-8", newline="\n")
    return manifest


def create_backup(
    dsn: str,
    directory: Path,
    *,
    name: str | None = None,
    runner: CommandRunner | None = None,
    tool: Tool | None = None,
) -> BackupManifest:
    """Dump one database and describe the dump in a manifest beside it."""
    runner = runner or SubprocessRunner()
    directory.mkdir(parents=True, exist_ok=True)
    if name is not None:
        validate_backup_name(name)
    dump_path: Path | None = None
    connection = psycopg.connect(dsn, autocommit=True)
    try:
        connection.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        snapshot_id = str(_scalar(connection, "SELECT pg_export_snapshot()"))
        snapshot = read_snapshot(connection)
        dump_tool = tool or resolve_tool(
            "pg_dump",
            env_var=DUMP_ENV,
            runner=runner,
            required_major=parse_major(snapshot.server_version),
        )
        chosen = name or default_backup_name(snapshot)
        dump_path = directory / f"{chosen}{DUMP_SUFFIX}"
        if dump_path.exists():
            raise OpsError(f"{dump_path} already exists; pick another name for this dump")
        result = runner.run(
            (
                dump_tool.path,
                "--dbname",
                conninfo_without_password(dsn),
                "--format",
                "custom",
                "--snapshot",
                snapshot_id,
                "--file",
                str(dump_path),
            ),
            env=child_environment(dsn),
        )
        if result.returncode != 0:
            raise OpsError(
                f"pg_dump failed with exit code {result.returncode}: {result.stderr.strip()}"
            )
        # The exported snapshot stays valid only until this transaction ends.
        connection.execute("COMMIT")
        return finalize_backup(
            directory,
            dump_path,
            snapshot=snapshot,
            name=chosen,
            tool_version=dump_tool.version,
        )
    except BaseException as exc:
        if dump_path is not None and dump_path.exists():
            dump_path.unlink()
        if isinstance(exc, OpsError) or not isinstance(exc, Exception):
            raise
        raise OpsError(f"backup failed: {exc}") from exc
    finally:
        connection.close()


# --------------------------------------------------------------------------
# Verifying a dump and rehearsing its restore
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """One check, its verdict, and the numbers behind the verdict."""

    check: str
    ok: bool
    detail: str


def _findings_block(findings: Sequence[Finding]) -> str:
    mark = {True: "ok  ", False: "FAIL"}
    return "\n".join(
        f"  [{mark[finding.ok]}] {finding.check}: {finding.detail}" for finding in findings
    )


def _describe(values: Sequence[Any], limit: int = 8) -> str:
    shown = [str(value) for value in values[:limit]]
    if len(values) > limit:
        shown.append(f"... {len(values) - limit} more")
    return ", ".join(shown)


def _check(label: str, ok: bool, good: str, bad: str) -> Finding:
    return Finding(label, ok, good if ok else bad)


def findings_json(findings: Sequence[Finding]) -> list[dict[str, Any]]:
    return [
        {"check": finding.check, "ok": finding.ok, "detail": finding.detail} for finding in findings
    ]


def load_manifests(directory: Path) -> tuple[BackupManifest, ...]:
    manifests = [
        BackupManifest.from_json(path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob(f"*{MANIFEST_SUFFIX}"))
    ]
    manifests.sort(key=lambda manifest: manifest.taken_at)
    return tuple(manifests)


def load_manifest(directory: Path, name: str | None = None) -> BackupManifest:
    if name is not None:
        path = directory / f"{validate_backup_name(name)}{MANIFEST_SUFFIX}"
        if not path.is_file():
            raise OpsError(f"{path} does not exist")
        return BackupManifest.from_json(path.read_text(encoding="utf-8"))
    manifests = load_manifests(directory)
    if not manifests:
        raise OpsError(f"{directory} holds no backup manifest")
    return manifests[-1]


@dataclass(frozen=True)
class Verification:
    """A dump on disk, judged against its manifest and this build."""

    name: str
    findings: tuple[Finding, ...]

    @property
    def ok(self) -> bool:
        return all(finding.ok for finding in self.findings)

    def table(self) -> str:
        verdict = "ok" if self.ok else "failed"
        return f"backup {self.name}: {verdict}\n" + _findings_block(self.findings)

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "findings": findings_json(self.findings)}


def verify_backup(
    directory: Path,
    manifest: BackupManifest,
    *,
    checksums: Mapping[int, str] | None = None,
) -> Verification:
    """Check the dump against its manifest and the migrations this build ships."""
    known = dict(known_migrations() if checksums is None else checksums)
    findings: list[Finding] = []
    path = manifest.dump_path(directory)
    if not path.is_file():
        findings.append(Finding("dump present", False, f"{path} is missing"))
    else:
        size = path.stat().st_size
        findings.append(
            _check(
                "dump size",
                size == manifest.dump_bytes,
                f"{size} bytes, as recorded",
                f"{size} bytes, manifest says {manifest.dump_bytes}",
            )
        )
        digest = _sha256_file(path)
        findings.append(
            _check(
                "dump sha256",
                digest == manifest.dump_sha256,
                f"{digest[:16]}..., as recorded",
                f"file {digest[:16]}... vs manifest {manifest.dump_sha256[:16]}...",
            )
        )
    unknown = sorted({record.version for record in manifest.migrations} - set(known))
    findings.append(
        _check(
            "schema is known to this build",
            not unknown,
            "every recorded migration exists in this build",
            f"this build has no migration {_describe(unknown)}",
        )
    )
    newer = sorted(set(known) - {record.version for record in manifest.migrations})
    findings.append(
        Finding(
            "schema version",
            True,
            f"migration {manifest.schema_version} of {latest_schema_version()}; "
            f"{len(newer)} migration(s) newer than this dump",
        )
    )
    drifted = sorted(
        record.version
        for record in manifest.migrations
        if record.version in known and known[record.version] != record.checksum
    )
    findings.append(
        _check(
            "migration checksums",
            not drifted,
            "matches this build",
            f"changed since the backup: {_describe(drifted)}",
        )
    )
    findings.append(
        _check(
            "row counts recorded",
            bool(manifest.row_counts),
            f"{len(manifest.row_counts)} table(s) for the drill to compare",
            "the manifest records no Aftercare table",
        )
    )
    return Verification(name=manifest.name, findings=tuple(findings))


def _compare(
    expected_migrations: Sequence[MigrationRecord],
    expected_rows: Sequence[TableRows],
    actual_migrations: Sequence[MigrationRecord],
    actual_rows: Sequence[TableRows],
) -> list[Finding]:
    expected_versions = {record.version: record.checksum for record in expected_migrations}
    actual_versions = {record.version: record.checksum for record in actual_migrations}
    expected_counts = {record.table: record.rows for record in expected_rows}
    actual_counts = {record.table: record.rows for record in actual_rows}
    missing = sorted(set(expected_counts) - set(actual_counts))
    added = sorted(set(actual_counts) - set(expected_counts))
    differences = sorted(
        f"{table} {expected_counts[table]}/{actual_counts[table]}"
        for table in set(expected_counts) & set(actual_counts)
        if expected_counts[table] != actual_counts[table]
    )
    return [
        _check(
            "restored migrations",
            expected_versions == actual_versions,
            "identical to the manifest",
            f"manifest {_describe(sorted(expected_versions))} vs restored "
            f"{_describe(sorted(actual_versions))}",
        ),
        _check(
            "restored tables",
            not missing and not added,
            f"{len(expected_counts)} table(s) restored in full",
            f"missing {_describe(missing)}; unexpected {_describe(added)}",
        ),
        _check(
            "restored row counts",
            not differences,
            f"every table holds the {len(expected_counts)} recorded counts",
            f"manifest/restored: {_describe(differences)}",
        ),
    ]


@dataclass(frozen=True)
class RestoreDrill:
    """What happened when a dump was restored into a scratch database."""

    name: str
    taken_at: datetime
    scratch_database: str
    started_at: datetime
    finished_at: datetime
    snapshot_age_seconds: float
    restore_seconds: float
    verify_seconds: float
    upgrade_seconds: float
    migrations_applied: tuple[int, ...]
    rows_changed_by_upgrade: tuple[str, ...]
    findings: tuple[Finding, ...]

    @property
    def ok(self) -> bool:
        return all(finding.ok for finding in self.findings)

    @property
    def rto_seconds(self) -> float:
        """Restore, verify and upgrade on this host, for this dump."""
        return self.restore_seconds + self.verify_seconds + self.upgrade_seconds

    def table(self) -> str:
        verdict = "ok" if self.ok else "failed"
        lines = [
            f"restore drill {self.name}: {verdict}",
            f"  scratch database   {self.scratch_database}",
            f"  recovery point     {self.taken_at.astimezone(UTC).isoformat()}",
            f"  recovery point age {self.snapshot_age_seconds:.1f}s at drill start",
            f"  restore            {self.restore_seconds:.3f}s",
            f"  verify             {self.verify_seconds:.3f}s",
        ]
        if self.migrations_applied:
            lines.append(
                f"  upgrade            {self.upgrade_seconds:.3f}s "
                f"(applied {_describe(self.migrations_applied)})"
            )
        else:
            lines.append(f"  upgrade            {self.upgrade_seconds:.3f}s")
        lines.append(f"  RTO                {self.rto_seconds:.3f}s on this host, for this dump")
        lines.append("  RPO                not measured here: the drill shows which recovery")
        lines.append("                     point the dump is, not the cadence that sets it")
        if self.rows_changed_by_upgrade:
            lines.append(f"  rows changed by upgrade: {_describe(self.rows_changed_by_upgrade)}")
        return "\n".join(lines) + "\n" + _findings_block(self.findings)

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "taken_at": self.taken_at.astimezone(UTC).isoformat(),
            "scratch_database": self.scratch_database,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "snapshot_age_seconds": round(self.snapshot_age_seconds, 3),
            "restore_seconds": round(self.restore_seconds, 3),
            "verify_seconds": round(self.verify_seconds, 3),
            "upgrade_seconds": round(self.upgrade_seconds, 3),
            "rto_seconds": round(self.rto_seconds, 3),
            "migrations_applied": list(self.migrations_applied),
            "rows_changed_by_upgrade": list(self.rows_changed_by_upgrade),
            "ok": self.ok,
            "findings": findings_json(self.findings),
        }


def _connect_maintenance(dsn: str) -> psycopg.Connection[Any]:
    last: Exception | None = None
    for name in MAINTENANCE_DATABASES:
        try:
            return psycopg.connect(dsn_with_database(dsn, name), autocommit=True)
        except psycopg.Error as exc:
            last = exc
    raise OpsError(f"cannot reach a maintenance database to create the scratch copy: {last}")


def _create_scratch(dsn: str, scratch: str, *, replace: bool) -> None:
    admin = _connect_maintenance(dsn)
    try:
        exists = admin.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (scratch,)
        ).fetchone()
        if exists is not None and not replace:
            raise OpsError(f"database {scratch} already exists; drop it or pass --replace-scratch")
        if exists is not None:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(scratch)))
        admin.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(scratch))
        )
    finally:
        admin.close()


def _drop_scratch(dsn: str, scratch: str) -> None:
    admin = _connect_maintenance(dsn)
    try:
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(scratch))
        )
    finally:
        admin.close()


def run_restore_drill(
    dsn: str,
    directory: Path,
    manifest: BackupManifest,
    *,
    runner: CommandRunner | None = None,
    tool: Tool | None = None,
    scratch_database: str | None = None,
    replace_scratch: bool = False,
    keep_scratch: bool = False,
    upgrade: bool = False,
) -> RestoreDrill:
    """Restore a dump into a fresh database and compare it with its manifest.

    The drill is the acceptance for a backup: it restores into a database that
    did not exist a moment ago, compares the schema and every Aftercare row
    count against the manifest, and drops the copy again.  ``upgrade`` runs the
    migrations this build ships on top of the restored copy, which rehearses
    the schema upgrade path against real data instead of an empty database.
    """
    runner = runner or SubprocessRunner()
    started_at = datetime.now(UTC)
    scratch = validate_database_name(
        scratch_database
        or f"{DEFAULT_SCRATCH_PREFIX}{started_at.astimezone(UTC).strftime('%Y%m%dT%H%M%S')}"
    )
    restore_tool = tool or resolve_tool(
        "pg_restore",
        env_var=RESTORE_ENV,
        runner=runner,
        required_major=parse_major(manifest.tool_version),
    )
    _create_scratch(dsn, scratch, replace=replace_scratch)
    scratch_dsn = dsn_with_database(dsn, scratch)
    failed = False
    try:
        mark = time.monotonic()
        result = runner.run(
            (
                restore_tool.path,
                "--dbname",
                conninfo_without_password(scratch_dsn),
                "--no-owner",
                "--no-privileges",
                "--single-transaction",
                "--exit-on-error",
                str(manifest.dump_path(directory)),
            ),
            env=child_environment(dsn),
        )
        restore_seconds = time.monotonic() - mark
        if result.returncode != 0:
            raise OpsError(
                f"pg_restore failed with exit code {result.returncode}: {result.stderr.strip()}"
            )
        mark = time.monotonic()
        with psycopg.connect(scratch_dsn) as restored:
            migrations, row_counts = read_schema(restored)
        findings = _compare(manifest.migrations, manifest.row_counts, migrations, row_counts)
        verify_seconds = time.monotonic() - mark
        applied: tuple[int, ...] = ()
        changed: tuple[str, ...] = ()
        upgrade_seconds = 0.0
        if upgrade:
            mark = time.monotonic()
            with psycopg.connect(scratch_dsn) as restored:
                with restored.transaction():
                    migrate(restored)
                upgraded_migrations, upgraded_rows = read_schema(restored)
            upgrade_seconds = time.monotonic() - mark
            before = {record.version for record in migrations}
            applied = tuple(sorted({record.version for record in upgraded_migrations} - before))
            reached = max((record.version for record in upgraded_migrations), default=0)
            findings.append(
                _check(
                    "upgrade reaches this build",
                    reached == latest_schema_version(),
                    f"the restored copy is at migration {reached}",
                    f"the restored copy stopped at migration {reached}",
                )
            )
            restored_counts = {record.table: record.rows for record in upgraded_rows}
            changed = tuple(
                sorted(
                    record.table
                    for record in row_counts
                    if restored_counts.get(record.table) != record.rows
                )
            )
            findings.append(
                _check(
                    "upgrade keeps the rows",
                    not changed,
                    f"all {len(row_counts)} table(s) kept their row counts",
                    f"row counts changed for {_describe(changed)}",
                )
            )
        findings.append(
            # Keeping the copy is a choice the caller made, not a failed check:
            # a copy that had to go is dropped by _drop_scratch, which raises
            # if the server refuses to remove it.
            Finding(
                "scratch copy",
                True,
                "dropped after the drill"
                if not keep_scratch
                else f"{scratch} kept for inspection; drop it when finished",
            )
        )
    except BaseException:
        failed = True
        raise
    finally:
        if not keep_scratch:
            if failed:
                # Cleanup must not hide the failure that caused it.
                with contextlib.suppress(OpsError):
                    _drop_scratch(dsn, scratch)
            else:
                _drop_scratch(dsn, scratch)
    return RestoreDrill(
        name=manifest.name,
        taken_at=manifest.taken_at,
        scratch_database=scratch,
        started_at=started_at,
        finished_at=datetime.now(UTC),
        snapshot_age_seconds=(started_at - manifest.taken_at).total_seconds(),
        restore_seconds=restore_seconds,
        verify_seconds=verify_seconds,
        upgrade_seconds=upgrade_seconds,
        migrations_applied=applied,
        rows_changed_by_upgrade=changed,
        findings=tuple(findings),
    )


# --------------------------------------------------------------------------
# Retention
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RetentionPolicy:
    """How much history to keep.  ``keep_last`` may never be zero."""

    keep_last: int = DEFAULT_KEEP_LAST
    keep_daily: int = DEFAULT_KEEP_DAILY

    def __post_init__(self) -> None:
        if self.keep_last < 1:
            raise OpsError("keep_last must keep at least one backup")
        if self.keep_daily < 0:
            raise OpsError("keep_daily cannot be negative")


@dataclass(frozen=True)
class RetentionDecision:
    name: str
    taken_at: datetime
    keep: bool
    reason: str


@dataclass(frozen=True)
class RetentionPlan:
    policy: RetentionPolicy
    decided_at: datetime
    decisions: tuple[RetentionDecision, ...]

    @property
    def kept(self) -> tuple[str, ...]:
        return tuple(decision.name for decision in self.decisions if decision.keep)

    @property
    def deleted(self) -> tuple[str, ...]:
        return tuple(decision.name for decision in self.decisions if not decision.keep)

    def table(self) -> str:
        lines = [
            f"retention keep-last={self.policy.keep_last} keep-daily={self.policy.keep_daily}: "
            f"{len(self.kept)} kept, {len(self.deleted)} to delete"
        ]
        for decision in self.decisions:
            action = "keep  " if decision.keep else "delete"
            lines.append(
                f"  [{action}] {decision.name} "
                f"{decision.taken_at.astimezone(UTC).isoformat()} ({decision.reason})"
            )
        return "\n".join(lines)

    def to_json(self) -> dict[str, Any]:
        return {
            "keep_last": self.policy.keep_last,
            "keep_daily": self.policy.keep_daily,
            "decided_at": self.decided_at.isoformat(),
            "kept": list(self.kept),
            "deleted": list(self.deleted),
            "decisions": [
                {
                    "name": decision.name,
                    "taken_at": decision.taken_at.astimezone(UTC).isoformat(),
                    "keep": decision.keep,
                    "reason": decision.reason,
                }
                for decision in self.decisions
            ],
        }


def plan_retention(
    manifests: Sequence[BackupManifest],
    policy: RetentionPolicy,
    *,
    now: datetime | None = None,
) -> RetentionPlan:
    """Decide which backups survive, newest first.

    Two rules cover what a deployment needs before it has a policy of its own:
    the newest ``keep_last`` copies always survive, and after that the newest
    copy of each of ``keep_daily`` distinct days survives.  Nothing here
    deletes; a plan is what an operator reads before :func:`apply_retention`.
    """
    ordered = sorted(manifests, key=lambda manifest: manifest.taken_at, reverse=True)
    days: list[date] = []
    decisions = []
    for index, manifest in enumerate(ordered):
        day = manifest.taken_at.astimezone(UTC).date()
        if index < policy.keep_last:
            keep, reason = True, f"within the newest {policy.keep_last}"
        elif policy.keep_daily and len(days) < policy.keep_daily and day not in days:
            keep, reason = True, "newest of its day"
        else:
            keep, reason = False, "beyond retention"
        if keep and day not in days:
            days.append(day)
        decisions.append(
            RetentionDecision(
                name=manifest.name, taken_at=manifest.taken_at, keep=keep, reason=reason
            )
        )
    return RetentionPlan(
        policy=policy,
        decided_at=now or datetime.now(UTC),
        decisions=tuple(decisions),
    )


def apply_retention(directory: Path, plan: RetentionPlan) -> tuple[Path, ...]:
    """Delete what the plan rejected, and refuse to empty the directory."""
    if not plan.kept:
        raise OpsError("refusing to delete every backup; keep at least one")
    removed: list[Path] = []
    for decision in plan.decisions:
        if decision.keep:
            continue
        name = validate_backup_name(decision.name)
        for path in (directory / f"{name}{DUMP_SUFFIX}", directory / f"{name}{MANIFEST_SUFFIX}"):
            if path.is_file():
                path.unlink()
                removed.append(path)
    return tuple(removed)


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def _require_dsn(value: str | None) -> str:
    dsn = value or os.environ.get("DATABASE_URL", "")
    if not dsn:
        raise OpsError("a database URL is required: pass --dsn or set DATABASE_URL")
    return dsn


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aftercare-backup",
        description=(
            "Dump PostgreSQL with a manifest, drill the restore into a scratch database, "
            "and keep the copies bounded.  Runs no model, connector or supplier call."
        ),
    )
    actions = parser.add_subparsers(dest="action", required=True)

    create = actions.add_parser("create", help="dump a database and write its manifest")
    create.add_argument("--directory", required=True, type=Path, help="where copies live")
    create.add_argument("--dsn", default=None, help="PostgreSQL DSN; defaults to DATABASE_URL")
    create.add_argument("--name", default=None, help="default is <database>-<UTC timestamp>")

    verify = actions.add_parser("verify", help="check a dump against its manifest and this build")
    verify.add_argument("--directory", required=True, type=Path)
    verify.add_argument("--name", default=None, help="backup name; defaults to the newest")
    verify.add_argument("--all", dest="verify_all", action="store_true", help="check every backup")
    verify.add_argument("--json", default=None, type=Path)

    drill = actions.add_parser("drill", help="restore a dump into a scratch database and compare")
    drill.add_argument("--directory", required=True, type=Path)
    drill.add_argument("--dsn", default=None)
    drill.add_argument("--name", default=None)
    drill.add_argument("--scratch-database", default=None)
    drill.add_argument("--replace-scratch", action="store_true", help="drop an existing scratch DB")
    drill.add_argument("--keep-scratch", action="store_true", help="leave the copy in place")
    drill.add_argument("--upgrade", action="store_true", help="migrate the restored copy here")
    drill.add_argument("--json", default=None, type=Path, help="write the report to this path")

    retention = actions.add_parser("retention", help="plan, and optionally apply, backup deletion")
    retention.add_argument("--directory", required=True, type=Path)
    retention.add_argument("--keep-last", type=int, default=DEFAULT_KEEP_LAST)
    retention.add_argument("--keep-daily", type=int, default=DEFAULT_KEEP_DAILY)
    retention.add_argument("--apply", action="store_true", help="delete what the plan rejected")
    retention.add_argument("--json", default=None, type=Path)

    reconcile = actions.add_parser(
        "reconcile", help="list the external actions a restore to this point cannot describe"
    )
    reconcile.add_argument("--dsn", default=None)
    reconcile.add_argument("--recovery-point", default=None, help="ISO-8601 recovery point")
    reconcile.add_argument("--directory", default=None, type=Path, help="read it from a manifest")
    reconcile.add_argument("--name", default=None)
    reconcile.add_argument("--tenant-id", default=None)
    reconcile.add_argument("--limit", type=int, default=DEFAULT_RECONCILE_LIMIT)
    reconcile.add_argument(
        "--safety-margin-seconds", type=float, default=DEFAULT_SAFETY_MARGIN_SECONDS
    )
    reconcile.add_argument("--fail-on-open", action="store_true", help="exit 1 when not empty")
    reconcile.add_argument("--json", default=None, type=Path)
    return parser


def _create_command(args: argparse.Namespace) -> int:
    manifest = create_backup(_require_dsn(args.dsn), args.directory, name=args.name)
    print(f"backup {manifest.name} written to {args.directory}")
    print(f"  recovery point  {manifest.taken_at.astimezone(UTC).isoformat()}")
    print(f"  schema          migration {manifest.schema_version} of {latest_schema_version()}")
    print(f"  dump            {manifest.dump_bytes} bytes, sha256 {manifest.dump_sha256[:16]}...")
    print("  next            aftercare-backup drill --directory <dir> --upgrade")
    return 0


def _verify_command(args: argparse.Namespace) -> int:
    directory: Path = args.directory
    manifests = (
        load_manifests(directory) if args.verify_all else (load_manifest(directory, args.name),)
    )
    if not manifests:
        raise OpsError(f"{directory} holds no backup manifest")
    verifications = [verify_backup(directory, manifest) for manifest in manifests]
    for verification in verifications:
        print(verification.table())
    if args.json:
        _write_json(
            args.json,
            {
                "verified": len(verifications),
                "ok": all(item.ok for item in verifications),
                "backups": [item.to_json() for item in verifications],
            },
        )
    return 0 if all(item.ok for item in verifications) else 1


def _drill_command(args: argparse.Namespace) -> int:
    dsn = _require_dsn(args.dsn)
    directory: Path = args.directory
    manifest = load_manifest(directory, args.name)
    drill = run_restore_drill(
        dsn,
        directory,
        manifest,
        scratch_database=args.scratch_database,
        replace_scratch=args.replace_scratch,
        keep_scratch=args.keep_scratch,
        upgrade=args.upgrade,
    )
    print(drill.table())
    if args.json:
        _write_json(args.json, drill.to_json())
    return 0 if drill.ok else 1


def _retention_command(args: argparse.Namespace) -> int:
    policy = RetentionPolicy(keep_last=int(args.keep_last), keep_daily=int(args.keep_daily))
    directory: Path = args.directory
    plan = plan_retention(load_manifests(directory), policy)
    print(plan.table())
    if args.apply:
        removed = apply_retention(directory, plan)
        print(f"deleted {len(removed)} file(s)")
    if args.json:
        _write_json(args.json, plan.to_json())
    return 0


def _recovery_point(args: argparse.Namespace) -> datetime:
    if args.recovery_point:
        try:
            value = datetime.fromisoformat(str(args.recovery_point))
        except ValueError as exc:
            raise OpsError(f"--recovery-point is not an ISO-8601 timestamp: {exc}") from exc
        if value.tzinfo is None or value.utcoffset() is None:
            raise OpsError("--recovery-point needs a timezone offset")
        return value
    if args.directory is None:
        raise OpsError("pass --recovery-point, or --directory to read it from a manifest")
    return load_manifest(args.directory, args.name).taken_at


def _reconcile_command(args: argparse.Namespace) -> int:
    recovery_point = _recovery_point(args)
    with psycopg.connect(_require_dsn(args.dsn)) as connection:
        report = build_reconciliation(
            connection,
            recovery_point=recovery_point,
            safety_margin_seconds=float(args.safety_margin_seconds),
            tenant_id=args.tenant_id,
            limit=int(args.limit),
        )
    print(report.table())
    if args.json:
        _write_json(args.json, report.to_json())
    if args.fail_on_open and report.needs_review:
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.action == "create":
            return _create_command(args)
        if args.action == "verify":
            return _verify_command(args)
        if args.action == "drill":
            return _drill_command(args)
        if args.action == "retention":
            return _retention_command(args)
        return _reconcile_command(args)
    except OpsError as exc:
        print(f"aftercare-backup: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
