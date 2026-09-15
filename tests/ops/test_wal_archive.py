"""What an archive directory can be asked, without a server to ask.

These tests build archive directories out of file names, which is all the
contiguity and coverage checks read, and hand ``evaluate_archive`` a stated
archiver state.  The one thing they cannot fake -- a server that really
archived a segment -- is exercised in tests/persistence/test_wal_archive.py.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aftercare_agent.observability import InMemoryMetrics
from aftercare_agent.ops.backup import BackupManifest, main
from aftercare_agent.ops.freshness import Verdict
from aftercare_agent.ops.tooling import OpsError
from aftercare_agent.ops.wal_archive import (
    ArchiveBudget,
    ArchiveReport,
    ArchiverState,
    Segment,
    adjacent,
    evaluate_archive,
    format_lsn,
    format_segment_size,
    parse_lsn,
    parse_segment_size,
    read_inventory,
    report_archive_status,
    segment_holding,
)

CHECKSUM = "a" * 64
SEGMENT_BYTES = 16 << 20
CHECKED_AT = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def _name(timeline: int, log: int, segno: int) -> str:
    return f"{timeline:08X}{log:08X}{segno:08X}"


def _archive(tmp_path: Path, names: list[str], *, subdirectory: bool = False) -> Path:
    directory = tmp_path / "archive"
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_bytes(b"wal")
    if subdirectory:
        (directory / "lost+found").mkdir(exist_ok=True)
    return directory


def _archiver(
    *,
    archived_count: int = 3,
    last_archived_wal: str | None = "000000010000000000000010",
    last_archived_time: datetime | None = CHECKED_AT - timedelta(seconds=20),
    failed_count: int = 0,
    last_failed_wal: str | None = None,
    last_failed_time: datetime | None = None,
    segment_bytes: int = SEGMENT_BYTES,
    in_recovery: bool = False,
) -> ArchiverState:
    return ArchiverState(
        archived_count=archived_count,
        last_archived_wal=last_archived_wal,
        last_archived_time=last_archived_time,
        failed_count=failed_count,
        last_failed_wal=last_failed_wal,
        last_failed_time=last_failed_time,
        current_wal="000000010000000000000012",
        segment_bytes=segment_bytes,
        in_recovery=in_recovery,
    )


def _manifest(*, wal_lsn: str | None = "0/16B3748", name: str = "daily-1") -> BackupManifest:
    # A manifest written before this slice has no WAL position, so it is a
    # version 1 manifest; the boundary refuses the mix.
    return BackupManifest(
        manifest_version=1 if wal_lsn is None else 2,
        name=name,
        created_at=CHECKED_AT,
        taken_at=CHECKED_AT,
        database="aftercare",
        server_version="16.13",
        schema_version=16,
        wal_lsn=wal_lsn,
        migrations=(),
        row_counts=(),
        dump_file=f"{name}.dump",
        dump_bytes=12,
        dump_sha256=CHECKSUM,
        tool_version="pg_dump (PostgreSQL) 16.13",
    )


def _verdict(report: ArchiveReport, claim: str) -> Verdict:
    for verdict in report.verdicts:
        if claim in verdict.claim:
            return verdict
    raise AssertionError(
        f"no verdict about {claim!r} in {[item.claim for item in report.verdicts]}"
    )


def test_a_segment_size_is_read_the_way_postgresql_writes_it() -> None:
    assert parse_segment_size("16MB") == SEGMENT_BYTES
    assert parse_segment_size("16777216") == SEGMENT_BYTES
    assert parse_segment_size(" 1GB ") == 1 << 30
    assert format_segment_size(SEGMENT_BYTES) == "16MB"
    assert format_segment_size(1 << 30) == "1024MB"


def test_a_segment_size_that_is_not_a_power_of_two_is_refused() -> None:
    with pytest.raises(OpsError, match="power of two"):
        parse_segment_size("15MB")
    with pytest.raises(OpsError, match="power of two"):
        parse_segment_size("512GB")
    with pytest.raises(OpsError, match="cannot read"):
        parse_segment_size("sixteen megabytes")


def test_a_wal_position_round_trips_through_its_text_form() -> None:
    position = parse_lsn("0/16B3748")
    assert position == 0x16B3748
    assert format_lsn(position) == "0/16B3748"
    assert parse_lsn("A/0") == 0xA << 32
    with pytest.raises(OpsError, match="cannot read a WAL position"):
        parse_lsn("16B3748")


def test_a_position_needs_the_segment_size_to_name_its_file() -> None:
    # 0x16B3748 is 1.42 of a 16MB segment, so it lives in the second one.
    assert segment_holding(0x16B3748, SEGMENT_BYTES) == (0, 1)
    assert segment_holding(0x16B3748, 1 << 20) == (0, 22)
    assert segment_holding(0x100000000, SEGMENT_BYTES) == (1, 0)


def test_a_segment_name_is_parsed_or_refused() -> None:
    segment = Segment.parse("000000010000000000000010")
    assert segment == Segment(timeline=1, log=0, segno=0x10)
    assert segment is not None and segment.name == "000000010000000000000010"
    assert Segment.parse("00000001000000000000001") is None
    assert Segment.parse("00000001000000000000001g") is None
    assert Segment.parse("000000010000000000000010.partial") is None


def test_neighbours_are_recognised_across_a_log_boundary_whatever_the_size() -> None:
    assert adjacent(Segment(1, 0, 0x10), Segment(1, 0, 0x11))
    assert adjacent(Segment(1, 0, 0xFF), Segment(1, 1, 0))
    assert not adjacent(Segment(1, 0, 0x10), Segment(1, 0, 0x12))
    assert not adjacent(Segment(1, 0, 0x10), Segment(1, 2, 0))


def test_an_archive_directory_is_classified_by_file_name(tmp_path: Path) -> None:
    directory = _archive(
        tmp_path,
        [
            _name(1, 0, 0x10),
            _name(1, 0, 0x11) + ".partial",
            "00000002.history",
            "README",
        ],
        subdirectory=True,
    )
    inventory = read_inventory(directory)
    assert [segment.name for segment in inventory.segments] == [_name(1, 0, 0x10)]
    assert inventory.partial_files == (_name(1, 0, 0x11) + ".partial",)
    assert inventory.history_files == ("00000002.history",)
    assert inventory.other_files == 2
    assert inventory.newest == Segment(1, 0, 0x10)
    assert inventory.oldest == Segment(1, 0, 0x10)


def test_a_directory_that_cannot_be_read_is_not_an_empty_one(tmp_path: Path) -> None:
    with pytest.raises(OpsError, match="does not exist"):
        read_inventory(tmp_path / "missing")
    file_path = tmp_path / "not-a-directory"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(OpsError, match="is not a directory"):
        read_inventory(file_path)


def test_a_contiguous_archive_has_no_gaps(tmp_path: Path) -> None:
    inventory = read_inventory(
        _archive(tmp_path, [_name(1, 0, 0x10), _name(1, 0, 0x11), _name(1, 0, 0x12)])
    )
    assert inventory.gaps(SEGMENT_BYTES) == ()
    report = evaluate_archive(inventory, checked_at=CHECKED_AT)
    assert report.ok
    assert _verdict(report, "unbroken chain").ok


def test_a_hole_is_found_and_counted_when_the_size_is_known(tmp_path: Path) -> None:
    inventory = read_inventory(
        _archive(tmp_path, [_name(1, 0, 0x10), _name(1, 0, 0x13), _name(1, 0, 0x14)])
    )
    gaps = inventory.gaps(SEGMENT_BYTES)
    assert len(gaps) == 1
    assert gaps[0].after == Segment(1, 0, 0x10)
    assert gaps[0].before == Segment(1, 0, 0x13)
    assert gaps[0].missing == 2
    assert "2 segment(s) missing" in gaps[0].describe()
    report = evaluate_archive(inventory, checked_at=CHECKED_AT, segment_bytes=SEGMENT_BYTES)
    assert not report.ok
    assert not _verdict(report, "unbroken chain").ok


def test_a_hole_is_still_found_without_the_segment_size(tmp_path: Path) -> None:
    inventory = read_inventory(_archive(tmp_path, [_name(1, 0, 0x10), _name(1, 0, 0x13)]))
    gaps = inventory.gaps()
    assert len(gaps) == 1
    assert gaps[0].missing is None
    assert "needs the segment size" in gaps[0].describe()
    report = evaluate_archive(inventory, checked_at=CHECKED_AT)
    assert not report.ok
    assert "pass --segment-size" in _verdict(report, "unbroken chain").detail


def test_each_timeline_is_checked_on_its_own(tmp_path: Path) -> None:
    # After a failover the same log and segment name come back under a new
    # timeline; that is not a hole in the archive.
    inventory = read_inventory(
        _archive(tmp_path, [_name(1, 0, 0x10), _name(1, 0, 0x11), _name(2, 0, 0x0F)])
    )
    assert inventory.gaps(SEGMENT_BYTES) == ()
    report = evaluate_archive(inventory, checked_at=CHECKED_AT, segment_bytes=SEGMENT_BYTES)
    assert report.ok
    assert report.timeline_count == 2
    assert any("timelines" in note for note in report.notes)


def test_an_empty_archive_cannot_be_recovery_capacity(tmp_path: Path) -> None:
    report = evaluate_archive(read_inventory(_archive(tmp_path, [])), checked_at=CHECKED_AT)
    assert not report.ok
    assert report.segment_count == 0
    assert "no WAL segment has been archived" in report.problems[0]


def test_a_partially_copied_segment_is_a_problem(tmp_path: Path) -> None:
    directory = _archive(tmp_path, [_name(1, 0, 0x10), _name(1, 0, 0x11) + ".partial"])
    report = evaluate_archive(read_inventory(directory), checked_at=CHECKED_AT)
    assert not report.ok
    assert any("partially copied" in problem for problem in report.problems)


def test_a_stated_segment_size_that_contradicts_the_server_is_an_error(tmp_path: Path) -> None:
    inventory = read_inventory(_archive(tmp_path, [_name(1, 0, 0x10)]))
    with pytest.raises(OpsError, match="contradicts the server"):
        evaluate_archive(
            inventory, archiver=_archiver(), segment_bytes=1 << 20, checked_at=CHECKED_AT
        )


def test_the_archiver_state_is_read_from_the_servers_own_numbers() -> None:
    archiver = _archiver()
    assert not archiver.stuck_on_failure
    assert archiver.segment_bytes == SEGMENT_BYTES
    failed = _archiver(
        failed_count=1,
        last_failed_wal="000000010000000000000011",
        last_failed_time=CHECKED_AT - timedelta(seconds=5),
    )
    assert failed.stuck_on_failure
    recovered = _archiver(
        failed_count=1,
        last_failed_wal="00000001000000000000000F",
        last_failed_time=CHECKED_AT - timedelta(seconds=120),
    )
    assert not recovered.stuck_on_failure


def test_a_stuck_archiver_is_reported_even_though_the_directory_looks_healthy(
    tmp_path: Path,
) -> None:
    inventory = read_inventory(_archive(tmp_path, [_name(1, 0, 0x10), _name(1, 0, 0x11)]))
    report = evaluate_archive(
        inventory,
        archiver=_archiver(
            failed_count=2,
            last_failed_wal="000000010000000000000012",
            last_failed_time=CHECKED_AT - timedelta(seconds=3),
        ),
        checked_at=CHECKED_AT,
    )
    assert not report.ok
    stuck = _verdict(report, "completing segments")
    assert not stuck.ok
    assert "not advancing" in stuck.detail


def test_an_archiver_that_never_completed_a_segment_is_not_protection(tmp_path: Path) -> None:
    inventory = read_inventory(_archive(tmp_path, []))
    report = evaluate_archive(
        inventory,
        archiver=_archiver(archived_count=0, last_archived_wal=None, last_archived_time=None),
        checked_at=CHECKED_AT,
    )
    assert not report.ok
    assert not _verdict(report, "completing segments").ok


def test_the_lag_is_measured_against_the_clock_the_check_was_given(tmp_path: Path) -> None:
    inventory = read_inventory(_archive(tmp_path, [_name(1, 0, 0x10)]))
    report = evaluate_archive(inventory, archiver=_archiver(), checked_at=CHECKED_AT)
    assert report.lag_seconds == pytest.approx(20.0)
    assert not report.budget.configured
    assert "no archive lag stated" in report.table()
    assert "no segment has been archived" not in report.table()


def test_a_lag_budget_is_judged_only_when_a_deployment_states_one(tmp_path: Path) -> None:
    inventory = read_inventory(_archive(tmp_path, [_name(1, 0, 0x10)]))
    inside = evaluate_archive(
        inventory,
        archiver=_archiver(),
        budget=ArchiveBudget(lag_seconds=60.0),
        checked_at=CHECKED_AT,
    )
    assert inside.ok
    assert _verdict(inside, "inside the RPO budget").ok
    outside = evaluate_archive(
        inventory,
        archiver=_archiver(),
        budget=ArchiveBudget(lag_seconds=5.0),
        checked_at=CHECKED_AT,
    )
    assert not outside.ok
    assert not _verdict(outside, "inside the RPO budget").ok


def test_a_lag_budget_without_a_server_is_an_error_rather_than_a_pass(tmp_path: Path) -> None:
    inventory = read_inventory(_archive(tmp_path, [_name(1, 0, 0x10)]))
    with pytest.raises(OpsError, match="needs a live server"):
        evaluate_archive(inventory, budget=ArchiveBudget(lag_seconds=60.0), checked_at=CHECKED_AT)
    with pytest.raises(OpsError, match="positive"):
        ArchiveBudget(lag_seconds=0.0)


def test_a_dump_inside_the_archive_is_covered(tmp_path: Path) -> None:
    inventory = read_inventory(
        _archive(tmp_path, [_name(1, 0, 0x11), _name(1, 0, 0x12), _name(1, 0, 0x13)])
    )
    report = evaluate_archive(
        inventory,
        manifest=_manifest(wal_lsn="0/16B3748"),
        segment_bytes=SEGMENT_BYTES,
        checked_at=CHECKED_AT,
    )
    assert report.coverage is not None and report.coverage.ok
    assert report.ok
    assert _verdict(report, "reaches the newest backup").ok


def test_a_dump_newer_than_the_archive_is_not_covered(tmp_path: Path) -> None:
    inventory = read_inventory(_archive(tmp_path, [_name(1, 0, 0x10)]))
    report = evaluate_archive(
        inventory,
        manifest=_manifest(wal_lsn="0/20000000"),
        segment_bytes=SEGMENT_BYTES,
        checked_at=CHECKED_AT,
    )
    assert report.coverage is not None and not report.coverage.ok
    assert not report.ok
    assert "no archive behind it" in _verdict(report, "reaches the newest backup").detail


def test_a_manifest_without_a_wal_position_is_said_to_be_too_old(tmp_path: Path) -> None:
    inventory = read_inventory(_archive(tmp_path, [_name(1, 0, 0x10)]))
    report = evaluate_archive(
        inventory,
        manifest=_manifest(wal_lsn=None),
        segment_bytes=SEGMENT_BYTES,
        checked_at=CHECKED_AT,
    )
    assert report.coverage is None
    assert any("predates WAL positions" in note for note in report.notes)
    assert not any("reaches the newest backup" in verdict.claim for verdict in report.verdicts)


def test_coverage_without_a_segment_size_is_stated_rather_than_passed(tmp_path: Path) -> None:
    inventory = read_inventory(_archive(tmp_path, [_name(1, 0, 0x10)]))
    report = evaluate_archive(
        inventory, manifest=_manifest(), segment_bytes=None, checked_at=CHECKED_AT
    )
    assert report.coverage is None
    assert any("segment size" in note for note in report.notes)


def test_the_report_carries_the_numbers_a_schedule_would_alert_on(tmp_path: Path) -> None:
    inventory = read_inventory(
        _archive(tmp_path, [_name(1, 0, 0x10), _name(1, 0, 0x13)]),
    )
    report = evaluate_archive(
        inventory,
        archiver=_archiver(),
        manifest=_manifest(wal_lsn="0/16B3748"),
        budget=ArchiveBudget(lag_seconds=60.0),
        checked_at=CHECKED_AT,
    )
    payload = report.to_json()
    assert payload["ok"] is False
    assert payload["segments"] == 2
    assert payload["gaps"][0]["missing"] == 2
    assert payload["budget"] == {"lag_seconds": 60.0, "configured": True}
    assert payload["coverage"]["manifest"] == "daily-1"
    assert payload["archiver"]["stuck_on_failure"] is False
    assert payload["lag_seconds"] == pytest.approx(20.0)
    assert str(inventory.directory) == payload["directory"]


def test_the_metrics_keep_the_label_set_bounded(tmp_path: Path) -> None:
    inventory = read_inventory(_archive(tmp_path, [_name(1, 0, 0x10)]))
    report = evaluate_archive(
        inventory,
        archiver=_archiver(),
        manifest=_manifest(),
        budget=ArchiveBudget(lag_seconds=60.0),
        checked_at=CHECKED_AT,
    )
    metrics = InMemoryMetrics()
    report_archive_status(metrics, report, component="backup")
    names = metrics.names()
    assert "aftercare.backup.wal_segments" in names
    assert "aftercare.backup.wal_lag_seconds" in names
    assert "aftercare.backup.wal_covers_newest_backup" in names
    assert metrics.value("aftercare.backup.wal_ok") == 1.0
    assert metrics.value("aftercare.backup.wal_lag_seconds") == pytest.approx(20.0)
    for metric in metrics.metrics:
        assert metric.attributes == {"component": "backup"}


def test_the_command_reports_a_directory_that_cannot_be_trusted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _archive(tmp_path, [_name(1, 0, 0x10), _name(1, 0, 0x13)])
    assert main(["wal", "--archive-dir", str(directory)]) == 1
    printed = capsys.readouterr().out
    assert "gaps             1" in printed
    assert "archiver         not read" in printed


def test_the_command_passes_a_contiguous_archive_and_writes_its_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _archive(tmp_path, [_name(1, 0, 0x10), _name(1, 0, 0x11)])
    report_path = tmp_path / "wal.json"
    code = main(
        [
            "wal",
            "--archive-dir",
            str(directory),
            "--segment-size",
            "16MB",
            "--json",
            str(report_path),
        ]
    )
    assert code == 0
    assert '"ok": true' in report_path.read_text(encoding="utf-8")
    assert "gaps             none" in capsys.readouterr().out


def test_the_command_refuses_arguments_that_cannot_be_honoured(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _archive(tmp_path, [_name(1, 0, 0x10)])
    assert main(["wal", "--archive-dir", str(directory), "--segment-size", "15MB"]) == 2
    assert "power of two" in capsys.readouterr().err
    assert main(["wal", "--archive-dir", str(directory), "--name", "daily-1"]) == 2
    assert "--name needs --directory" in capsys.readouterr().err
    assert main(["wal", "--archive-dir", str(directory), "--archive-lag-seconds", "60"]) == 2
    assert "needs a live server" in capsys.readouterr().err
