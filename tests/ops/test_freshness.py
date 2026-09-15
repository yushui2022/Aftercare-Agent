"""What a schedule can learn from a backup directory, without PostgreSQL.

Freshness is a claim about the past: how old the newest recovery point is, and
whether restoring it ever worked.  These tests write directories by hand, so
every verdict -- inside the budget, past it, never drilled, a dump that is
missing, or a deployment that stated no budget at all -- is exercised without a
database.
"""

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from aftercare_agent.observability import InMemoryMetrics
from aftercare_agent.ops.backup import (
    BackupManifest,
    DatabaseSnapshot,
    MigrationRecord,
    RetentionPolicy,
    TableRows,
    apply_retention,
    finalize_backup,
    load_manifests,
    main,
    plan_retention,
)
from aftercare_agent.ops.drill_records import (
    DrillCheck,
    DrillRecord,
    drill_record_path,
    load_drill_records,
    write_drill_record,
)
from aftercare_agent.ops.freshness import (
    BACKUP_METRIC_PREFIX,
    RPO_ENV,
    BackupStatus,
    StatusBudget,
    evaluate_status,
    report_backup_status,
)
from aftercare_agent.ops.tooling import OpsError

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
PAYLOAD = b"custom-format-dump"
CHECKSUM = "a" * 64
TOOL_VERSION = "pg_dump (PostgreSQL) 16.13"
RPO_CLAIM = "the newest recovery point is inside the RPO budget"
INTERVAL_CLAIM = "a restore has been proven inside the drill interval"
RESTORED_CLAIM = "the newest backup has been restored"


def _write_backup(
    directory: Path, name: str, taken_at: datetime, *, keep_dump: bool = True
) -> BackupManifest:
    directory.mkdir(parents=True, exist_ok=True)
    dump = directory / f"{name}.dump"
    dump.write_bytes(PAYLOAD)
    manifest = finalize_backup(
        directory,
        dump,
        snapshot=DatabaseSnapshot(
            database="aftercare",
            server_version="16.13",
            taken_at=taken_at,
            migrations=(MigrationRecord(version=16, checksum=CHECKSUM),),
            row_counts=(TableRows(table="aftercare_cases", rows=3),),
        ),
        name=name,
        tool_version=TOOL_VERSION,
    )
    if not keep_dump:
        dump.unlink()
    return manifest


def _record(
    directory: Path,
    name: str,
    *,
    taken_at: datetime,
    finished_at: datetime,
    ok: bool = True,
    error: str | None = None,
) -> DrillRecord:
    checks = (
        (DrillCheck(check="restored tables", ok=True, detail="31 table(s) restored in full"),)
        if ok
        else ()
    )
    record = DrillRecord(
        name=name,
        recovery_point=taken_at,
        finished_at=finished_at,
        ok=ok,
        rto_seconds=1.5 if ok else None,
        error=error,
        checks=checks,
    )
    write_drill_record(directory, record)
    return record


def _status(directory: Path, *, budget: StatusBudget | None = None) -> BackupStatus:
    return evaluate_status(
        directory,
        load_manifests(directory),
        load_drill_records(directory),
        budget=budget,
        now=NOW,
    )


def _claims(status: BackupStatus) -> dict[str, bool]:
    return {verdict.claim: verdict.ok for verdict in status.verdicts}


def test_an_undrilled_backup_is_reported_and_fails_no_budget_of_its_own(tmp_path: Path) -> None:
    _write_backup(tmp_path, "daily-1", NOW - timedelta(hours=2))
    status = _status(tmp_path)
    assert status.dump_count == 1
    assert status.drilled_names == ()
    assert status.undrilled_names == ("daily-1",)
    assert status.last_drill is None
    assert status.verdicts == ()
    # Nothing was stated, so nothing failed -- and the report says that rather
    # than quietly implying the backup is proven.
    assert status.ok is True
    assert status.budget.configured is False
    assert "budgets          none stated" in status.table()
    assert status.newest_dump_age_seconds == pytest.approx(7200.0)


def test_a_budget_is_never_met_by_a_directory_that_holds_nothing(tmp_path: Path) -> None:
    status = _status(tmp_path, budget=StatusBudget(rpo_seconds=3600))
    assert status.ok is False
    assert status.problems == (f"{tmp_path} holds no backup manifest at all",)
    assert _claims(status)[RPO_CLAIM] is False
    assert status.verdicts[0].observed_seconds is None


def test_a_dump_that_is_not_there_is_a_problem_not_a_policy_question(tmp_path: Path) -> None:
    _write_backup(tmp_path, "daily-1", NOW - timedelta(minutes=5), keep_dump=False)
    status = _status(tmp_path)
    assert status.ok is False
    assert status.problems == ("daily-1 has no dump: daily-1.dump is missing",)


def test_a_restored_backup_inside_both_budgets_passes(tmp_path: Path) -> None:
    taken = NOW - timedelta(hours=2)
    _write_backup(tmp_path, "daily-1", taken)
    _record(tmp_path, "daily-1", taken_at=taken, finished_at=NOW - timedelta(hours=1))
    status = _status(
        tmp_path, budget=StatusBudget(rpo_seconds=10_800, drill_interval_seconds=7_200)
    )
    assert status.ok is True
    assert status.drilled_names == ("daily-1",)
    assert status.undrilled_names == ()
    # Stating a drill interval also asks whether the newest backup itself has
    # been restored: three claims, all met.
    assert _claims(status) == {RPO_CLAIM: True, INTERVAL_CLAIM: True, RESTORED_CLAIM: True}


def test_a_stale_recovery_point_fails_the_rpo_budget(tmp_path: Path) -> None:
    _write_backup(tmp_path, "daily-1", NOW - timedelta(hours=5))
    status = _status(tmp_path, budget=StatusBudget(rpo_seconds=3600))
    verdict = status.verdicts[0]
    assert status.ok is False
    assert verdict.claim == RPO_CLAIM
    assert verdict.observed_seconds == pytest.approx(18_000.0)
    assert verdict.limit_seconds == 3600
    assert "past the 3600s budget" in verdict.detail


def test_a_stale_drill_fails_even_when_the_backup_is_fresh(tmp_path: Path) -> None:
    taken = NOW - timedelta(minutes=10)
    _write_backup(tmp_path, "daily-1", taken)
    _record(tmp_path, "daily-1", taken_at=taken, finished_at=NOW - timedelta(days=3))
    status = _status(tmp_path, budget=StatusBudget(drill_interval_seconds=86_400))
    claims = _claims(status)
    assert status.ok is False
    assert claims[INTERVAL_CLAIM] is False
    assert claims[RESTORED_CLAIM] is True


def test_a_failed_drill_is_recorded_and_does_not_count_as_a_restore(tmp_path: Path) -> None:
    taken = NOW - timedelta(hours=1)
    _write_backup(tmp_path, "daily-1", taken)
    _record(
        tmp_path,
        "daily-1",
        taken_at=taken,
        finished_at=NOW - timedelta(minutes=30),
        ok=False,
        error="pg_restore failed with exit code 1",
    )
    status = _status(tmp_path, budget=StatusBudget(require_newest_drill=True))
    assert status.drilled_names == ()
    assert status.undrilled_names == ("daily-1",)
    assert status.last_drill is not None
    assert status.last_drill.ok is False
    assert status.last_drill.error == "pg_restore failed with exit code 1"
    assert status.newest_ok_drill is None
    assert status.newest_ok_drill_age_seconds is None
    assert status.ok is False
    assert _claims(status)[RESTORED_CLAIM] is False


def test_an_older_restored_backup_does_not_cover_a_newer_dump(tmp_path: Path) -> None:
    old = NOW - timedelta(hours=6)
    _write_backup(tmp_path, "daily-1", old)
    _write_backup(tmp_path, "daily-2", NOW - timedelta(minutes=5))
    _record(tmp_path, "daily-1", taken_at=old, finished_at=NOW - timedelta(hours=5))
    status = _status(tmp_path, budget=StatusBudget(drill_interval_seconds=86_400))
    claims = _claims(status)
    assert status.drilled_names == ("daily-1",)
    assert status.undrilled_names == ("daily-2",)
    assert status.newest_dump_name == "daily-2"
    assert claims[INTERVAL_CLAIM] is True
    assert claims[RESTORED_CLAIM] is False
    assert status.ok is False


def test_records_round_trip_and_refuse_keys_they_do_not_know(tmp_path: Path) -> None:
    taken = NOW - timedelta(hours=1)
    _write_backup(tmp_path, "daily-1", taken)
    record = _record(tmp_path, "daily-1", taken_at=taken, finished_at=NOW)
    path = drill_record_path(tmp_path, "daily-1")
    assert path.read_text(encoding="utf-8") == record.to_json()
    assert load_drill_records(tmp_path) == (record,)
    payload = json.loads(record.to_json())
    payload["surprise"] = 1
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(OpsError, match="not a readable drill record"):
        load_drill_records(tmp_path)


def test_a_record_may_not_name_a_file_outside_the_directory(tmp_path: Path) -> None:
    with pytest.raises(OpsError, match="backup names take"):
        drill_record_path(tmp_path, "../escape")
    with pytest.raises(ValidationError):
        DrillRecord(name="../escape", recovery_point=NOW, finished_at=NOW, ok=True)


def test_an_unreadable_record_fails_loudly_instead_of_looking_like_no_record(
    tmp_path: Path,
) -> None:
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "broken.drill.json").write_text("{not json", encoding="utf-8")
    assert main(["status", "--directory", str(tmp_path)]) == 2


def test_metrics_are_labelled_by_component_and_carry_no_paths(tmp_path: Path) -> None:
    taken = NOW - timedelta(hours=3)
    _write_backup(tmp_path, "daily-1", taken)
    _record(tmp_path, "daily-1", taken_at=taken, finished_at=NOW - timedelta(hours=2))
    status = _status(tmp_path, budget=StatusBudget(rpo_seconds=10_800))
    metrics = InMemoryMetrics()
    report_backup_status(metrics, status, component="backup")
    assert metrics.names() == {
        f"{BACKUP_METRIC_PREFIX}.dumps",
        f"{BACKUP_METRIC_PREFIX}.dumps.drilled",
        f"{BACKUP_METRIC_PREFIX}.dumps.undrilled",
        f"{BACKUP_METRIC_PREFIX}.budget_ok",
        f"{BACKUP_METRIC_PREFIX}.newest_dump_age_seconds",
        f"{BACKUP_METRIC_PREFIX}.newest_drill_age_seconds",
        f"{BACKUP_METRIC_PREFIX}.last_drill_ok",
    }
    assert metrics.value(f"{BACKUP_METRIC_PREFIX}.budget_ok") == 1.0
    assert metrics.value(f"{BACKUP_METRIC_PREFIX}.dumps.drilled") == 1.0
    assert metrics.value(f"{BACKUP_METRIC_PREFIX}.last_drill_ok") == 1.0
    for metric in metrics.metrics:
        assert metric.attributes == {"component": "backup"}
        assert str(tmp_path) not in metric.name
        assert "daily-1" not in metric.name


def test_retention_removes_a_record_with_the_dump_it_describes(tmp_path: Path) -> None:
    old = NOW - timedelta(days=3)
    _write_backup(tmp_path, "old", old)
    _record(tmp_path, "old", taken_at=old, finished_at=old + timedelta(minutes=1))
    _write_backup(tmp_path, "new", NOW - timedelta(minutes=1))
    _record(tmp_path, "new", taken_at=NOW - timedelta(minutes=1), finished_at=NOW)
    plan = plan_retention(load_manifests(tmp_path), RetentionPolicy(keep_last=1, keep_daily=0))
    assert plan.deleted == ("old",)
    removed = apply_retention(tmp_path, plan)
    assert {path.name for path in removed} == {"old.dump", "old.manifest.json", "old.drill.json"}
    assert not drill_record_path(tmp_path, "old").exists()
    assert drill_record_path(tmp_path, "new").exists()


def test_the_status_command_exits_on_the_budgets_it_was_given(tmp_path: Path) -> None:
    # These run against the real clock, so the copies are placed relative to it
    # rather than to the fixed NOW the unit tests use.
    now = datetime.now(UTC)
    taken = now - timedelta(hours=5)
    _write_backup(tmp_path, "daily-1", taken)
    _record(tmp_path, "daily-1", taken_at=taken, finished_at=now - timedelta(hours=4))
    report = tmp_path / "status.json"
    assert main(["status", "--directory", str(tmp_path), "--rpo-seconds", "3600"]) == 1
    assert (
        main(
            [
                "status",
                "--directory",
                str(tmp_path),
                "--rpo-seconds",
                "86400",
                "--drill-interval-seconds",
                "86400",
                "--json",
                str(report),
            ]
        )
        == 0
    )
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["budget"]["rpo_seconds"] == 86400.0
    assert payload["budget"]["configured"] is True
    assert payload["newest_dump"] == "daily-1"
    assert payload["dumps"] == 1
    assert [verdict["claim"] for verdict in payload["verdicts"]] == [
        RPO_CLAIM,
        INTERVAL_CLAIM,
        RESTORED_CLAIM,
    ]


def test_the_status_command_reads_budgets_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_backup(tmp_path, "daily-1", datetime.now(UTC) - timedelta(minutes=1))
    monkeypatch.setenv(RPO_ENV, "1")
    assert main(["status", "--directory", str(tmp_path)]) == 1
    monkeypatch.setenv(RPO_ENV, "not-a-number")
    assert main(["status", "--directory", str(tmp_path)]) == 2


def test_the_status_command_can_log_its_metrics(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _write_backup(tmp_path, "daily-1", datetime.now(UTC) - timedelta(minutes=1))
    with caplog.at_level(logging.INFO, logger="aftercare_agent.metrics"):
        assert main(["status", "--directory", str(tmp_path), "--emit-metrics"]) == 0
    lines = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "aftercare_agent.metrics"
    ]
    names = {line["metric"] for line in lines}
    assert f"{BACKUP_METRIC_PREFIX}.budget_ok" in names
    assert all(line["attributes"] == {"component": "backup"} for line in lines)


def test_status_never_claims_a_budget_it_was_not_given(tmp_path: Path) -> None:
    _write_backup(tmp_path, "daily-1", datetime.now(UTC) - timedelta(minutes=1))
    report = tmp_path / "status.json"
    assert main(["status", "--directory", str(tmp_path), "--json", str(report)]) == 0
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["budget"] == {
        "rpo_seconds": None,
        "drill_interval_seconds": None,
        "require_newest_drill": False,
        "configured": False,
    }
    assert payload["verdicts"] == []
    assert payload["undrilled"] == ["daily-1"]
