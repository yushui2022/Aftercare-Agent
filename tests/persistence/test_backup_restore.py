"""A real dump, restored into a real scratch database; skipped without PostgreSQL.

A restore drill is the only acceptance a backup has, so these tests run the
real ``pg_dump`` and ``pg_restore`` binaries against the configured database.
They skip, with the reason, when the client is missing or older than the
server, because a client that cannot read this server would prove nothing.
"""

import os
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest

from aftercare_agent.domain.runtime import CaseRecord, RunRecord
from aftercare_agent.ops.backup import (
    create_backup,
    main,
    run_restore_drill,
    verify_backup,
)
from aftercare_agent.ops.drill_records import drill_record_path, load_drill_records
from aftercare_agent.ops.reconcile import build_reconciliation
from aftercare_agent.ops.tooling import (
    DUMP_ENV,
    RESTORE_ENV,
    IncompatibleToolError,
    MissingToolError,
    OpsError,
    SubprocessRunner,
    dsn_with_database,
    parse_major,
    resolve_tool,
)
from aftercare_agent.persistence import Database, RunRepository, migrate
from aftercare_agent.persistence.db import latest_schema_version

TENANT = "ops-backup"


@pytest.fixture()
def dsn() -> str:
    value = os.environ.get("DATABASE_URL")
    if not value:
        pytest.skip("DATABASE_URL is not configured")
    return value


@pytest.fixture()
def db(dsn: str) -> Database:
    value = Database(dsn)
    with value.transaction() as connection:
        migrate(connection)
    return value


@pytest.fixture()
def directory(tmp_path: Path) -> Path:
    return tmp_path / "backups"


@pytest.fixture()
def client_tools(dsn: str) -> None:
    """Skip unless the client binaries can read the server under test."""
    with psycopg.connect(dsn) as connection:
        row = connection.execute("SHOW server_version").fetchone()
    required = parse_major("unknown" if row is None else str(row[0]))
    for name, env_var in (("pg_dump", DUMP_ENV), ("pg_restore", RESTORE_ENV)):
        try:
            resolve_tool(name, env_var=env_var, runner=SubprocessRunner(), required_major=required)
        except (MissingToolError, IncompatibleToolError) as exc:
            pytest.skip(str(exc))


def _database_exists(dsn: str, name: str) -> bool:
    with psycopg.connect(dsn_with_database(dsn, "postgres"), autocommit=True) as connection:
        row = connection.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
    return row is not None


def _drop_database(dsn: str, name: str) -> None:
    with psycopg.connect(dsn_with_database(dsn, "postgres"), autocommit=True) as connection:
        connection.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _seed_case(db: Database, *, case_id: str, run_id: str, tenant: str = TENANT) -> None:
    with db.transaction() as connection:
        connection.execute("DELETE FROM aftercare_actions WHERE tenant_id=%s", (tenant,))
        connection.execute("DELETE FROM aftercare_runs WHERE tenant_id=%s", (tenant,))
        connection.execute("DELETE FROM aftercare_cases WHERE tenant_id=%s", (tenant,))
        runs = RunRepository()
        runs.create_case(
            connection,
            CaseRecord(tenant_id=tenant, case_id=case_id, order_id="order-1", version=1),
        )
        runs.create_run(
            connection,
            RunRecord(
                tenant_id=tenant,
                case_id=case_id,
                run_id=run_id,
                definition_version="v1",
                input_version=1,
            ),
        )


def _count(db: Database, table: str, tenant: str | None = TENANT) -> int:
    """Rows in one table, for one tenant or, with ``None``, for all of them."""
    with db.transaction() as connection:
        if tenant is None:
            row = connection.execute(f"SELECT count(*) FROM {table}").fetchone()
        else:
            row = connection.execute(
                f"SELECT count(*) FROM {table} WHERE tenant_id=%s", (tenant,)
            ).fetchone()
    return 0 if row is None else int(row[0])


def _insert_action(
    db: Database, *, action_id: str, state: str, age: timedelta, tenant: str = TENANT
) -> None:
    """Place an Action at a known distance from the recovery point."""
    confirmed = state == "CONFIRMED"
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO aftercare_actions(tenant_id,case_id,order_id,action_id,action_type,"
            "business_key,idempotency_key,parameters_sha256,amount_minor,currency,state,"
            "provider_reference,result_sha256,failure_code) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                tenant,
                "ops-case",
                "order-1",
                action_id,
                "refund",
                f"business:{action_id}",
                f"request:{action_id}",
                "a" * 64,
                100,
                "USD",
                state,
                "psp-reference-1" if confirmed else None,
                "b" * 64 if confirmed else None,
                "declined" if state == "FAILED" else None,
            ),
        )
        connection.execute(
            "UPDATE aftercare_actions SET updated_at = clock_timestamp() - %s::interval "
            "WHERE tenant_id=%s AND action_id=%s",
            (f"{age.total_seconds()} seconds", tenant, action_id),
        )


def test_a_backup_records_the_schema_and_every_row_count(
    db: Database, dsn: str, directory: Path, client_tools: None
) -> None:
    _seed_case(db, case_id="ops-case", run_id="ops-run")
    manifest = create_backup(dsn, directory)
    counts = {record.table: record.rows for record in manifest.row_counts}
    assert manifest.schema_version == latest_schema_version()
    assert manifest.name.startswith(manifest.database)
    # A row count describes the whole table, not this file's tenant: other
    # tests share the database, so the comparison is against every tenant.
    assert counts["aftercare_cases"] == _count(db, "aftercare_cases", tenant=None)
    assert _count(db, "aftercare_cases") == 1
    assert _count(db, "aftercare_runs") == 1
    assert len(counts) > 20
    assert manifest.dump_path(directory).read_bytes()[:5] == b"PGDMP"
    assert verify_backup(directory, manifest).ok


def test_the_drill_restores_the_copy_and_compares_every_count(
    db: Database, dsn: str, directory: Path, client_tools: None
) -> None:
    _seed_case(db, case_id="ops-case", run_id="ops-run")
    manifest = create_backup(dsn, directory)
    drill = run_restore_drill(dsn, directory, manifest)
    assert drill.ok
    assert drill.restore_seconds > 0
    assert drill.verify_seconds > 0
    assert drill.upgrade_seconds == 0
    assert drill.rto_seconds == pytest.approx(drill.restore_seconds + drill.verify_seconds)
    assert drill.snapshot_age_seconds >= 0
    assert "restored row counts" in drill.table()
    assert not _database_exists(dsn, drill.scratch_database)


def test_the_drill_compares_the_dump_rather_than_the_live_database(
    db: Database, dsn: str, directory: Path, client_tools: None
) -> None:
    _seed_case(db, case_id="ops-case", run_id="ops-run")
    manifest = create_backup(dsn, directory)
    with db.transaction() as connection:
        RunRepository().create_case(
            connection,
            CaseRecord(tenant_id=TENANT, case_id="ops-later", order_id="order-2", version=1),
        )
    recorded = {record.table: record.rows for record in manifest.row_counts}["aftercare_cases"]
    assert _count(db, "aftercare_cases", tenant=None) == recorded + 1
    assert _count(db, "aftercare_cases") == 2
    # The row arrived after the snapshot the dump was taken from, so it is in
    # neither the dump nor the manifest: the comparison still has to hold.
    assert run_restore_drill(dsn, directory, manifest).ok


def test_the_drill_rehearses_the_upgrade_path_on_the_restored_copy(
    db: Database, dsn: str, directory: Path, client_tools: None
) -> None:
    _seed_case(db, case_id="ops-case", run_id="ops-run")
    manifest = create_backup(dsn, directory)
    drill = run_restore_drill(dsn, directory, manifest, upgrade=True)
    assert drill.ok
    # A backup taken by this build is already at this build's schema, so the
    # rehearsal applies nothing; it still proves the migration path runs on
    # restored data.  Rehearsing an older schema needs a dump taken before the
    # migration existed, which only a deployment that has both can produce.
    assert drill.migrations_applied == ()
    assert "upgrade reaches this build" in drill.table()


def test_a_truncated_dump_fails_the_drill_and_leaves_no_copy(
    db: Database, dsn: str, directory: Path, client_tools: None
) -> None:
    _seed_case(db, case_id="ops-case", run_id="ops-run")
    manifest = create_backup(dsn, directory)
    dump = manifest.dump_path(directory)
    payload = dump.read_bytes()
    dump.write_bytes(payload[: len(payload) // 2])
    assert not verify_backup(directory, manifest).ok
    with pytest.raises(OpsError, match="pg_restore failed"):
        run_restore_drill(dsn, directory, manifest, scratch_database="aftercare_drill_broken")
    assert not _database_exists(dsn, "aftercare_drill_broken")


def test_a_scratch_database_is_never_reused_by_accident(
    db: Database, dsn: str, directory: Path, client_tools: None
) -> None:
    _seed_case(db, case_id="ops-case", run_id="ops-run")
    manifest = create_backup(dsn, directory)
    scratch = "aftercare_drill_reused"
    try:
        kept = run_restore_drill(
            dsn, directory, manifest, scratch_database=scratch, keep_scratch=True
        )
        assert kept.ok
        assert _database_exists(dsn, scratch)
        with pytest.raises(OpsError, match="already exists"):
            run_restore_drill(dsn, directory, manifest, scratch_database=scratch)
        assert run_restore_drill(
            dsn, directory, manifest, scratch_database=scratch, replace_scratch=True
        ).ok
        assert not _database_exists(dsn, scratch)
    finally:
        _drop_database(dsn, scratch)


def test_reconciliation_lists_the_actions_a_restore_cannot_describe(
    db: Database, dsn: str, directory: Path, client_tools: None
) -> None:
    _seed_case(db, case_id="ops-case", run_id="ops-run")
    manifest = create_backup(dsn, directory)
    _insert_action(db, action_id="unknown-old", state="UNKNOWN", age=timedelta(days=1))
    _insert_action(db, action_id="confirmed-new", state="CONFIRMED", age=timedelta(seconds=0))
    _insert_action(db, action_id="confirmed-inside", state="CONFIRMED", age=timedelta(minutes=30))
    with db.transaction() as connection:
        report = build_reconciliation(
            connection,
            recovery_point=manifest.taken_at,
            safety_margin_seconds=0.0,
            tenant_id=TENANT,
        )
    assert {entry.action_id: entry.reason for entry in report.entries} == {
        "unknown-old": "unknown_outcome",
        "confirmed-new": "changed_since_cut",
    }
    assert report.needs_review
    with db.transaction() as connection:
        widened = build_reconciliation(
            connection,
            recovery_point=manifest.taken_at,
            safety_margin_seconds=3600.0,
            tenant_id=TENANT,
        )
        other_tenant = build_reconciliation(
            connection,
            recovery_point=manifest.taken_at,
            safety_margin_seconds=0.0,
            tenant_id="nobody",
        )
    # The safety margin is what keeps a commit that raced the dump inside the
    # review list instead of silently outside it.
    assert "confirmed-inside" in {entry.action_id for entry in widened.entries}
    assert other_tenant.entries == ()


def test_the_drill_command_records_the_restore_beside_the_dump(
    db: Database, dsn: str, directory: Path, client_tools: None
) -> None:
    _seed_case(db, case_id="ops-case", run_id="ops-run")
    manifest = create_backup(dsn, directory)
    # Before the drill the directory holds a dump nobody has ever restored, so a
    # stated drill interval cannot be met yet.
    assert load_drill_records(directory) == ()
    assert main(["status", "--directory", str(directory), "--drill-interval-seconds", "86400"]) == 1

    assert main(["drill", "--directory", str(directory), "--dsn", dsn, "--upgrade"]) == 0

    record = load_drill_records(directory)[-1]
    assert record.name == manifest.name
    assert record.ok is True
    assert record.recovery_point == manifest.taken_at
    assert record.rto_seconds is not None and record.rto_seconds > 0
    assert {check.check for check in record.checks} >= {"restored tables", "restored row counts"}
    assert drill_record_path(directory, manifest.name).is_file()
    # The same directory now answers both questions with the budgets stated.
    assert (
        main(
            [
                "status",
                "--directory",
                str(directory),
                "--rpo-seconds",
                "86400",
                "--drill-interval-seconds",
                "86400",
            ]
        )
        == 0
    )


def test_a_drill_that_cannot_finish_is_recorded_as_a_failure(
    db: Database, dsn: str, directory: Path, client_tools: None
) -> None:
    _seed_case(db, case_id="ops-case", run_id="ops-run")
    manifest = create_backup(dsn, directory)
    dump = manifest.dump_path(directory)
    payload = dump.read_bytes()
    dump.write_bytes(payload[: len(payload) // 2])

    assert main(["drill", "--directory", str(directory), "--dsn", dsn]) == 2

    record = load_drill_records(directory)[-1]
    assert record.name == manifest.name
    assert record.ok is False
    assert record.rto_seconds is None
    assert record.error is not None and "pg_restore" in record.error
    # A recorded failure is still a failure: status must not read it as a
    # restore, and must not read the missing record as "nobody tried".
    assert main(["status", "--directory", str(directory), "--require-newest-drill"]) == 1
    assert (
        main(
            [
                "status",
                "--directory",
                str(directory),
                "--drill-interval-seconds",
                "86400",
            ]
        )
        == 1
    )
