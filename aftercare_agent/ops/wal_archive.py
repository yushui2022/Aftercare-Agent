"""Is anything protecting the transactions between two dumps?

A dump is a moment.  On its own, the newest moment a deployment can recover is
"when the last dump finished", so a dump-only RPO is the dump interval.
Continuous archiving narrows that: once PostgreSQL copies every completed WAL
segment into an archive, every moment after the first archived segment becomes
recoverable, and the newest recoverable moment is bounded by how far the
archiver has fallen behind -- a quantity in seconds, measured against the live
server rather than guessed from the schedule.

Two failures hide in there.  The archive can stop advancing: ``archive_command``
starts failing and PostgreSQL retries the same segment indefinitely while every
other signal a deployment watches stays green.  And the archive can grow a hole
-- a segment deleted by hand, a copy that never finished -- which makes every
segment after the gap useless for recovery even though the directory still
looks full.  :func:`evaluate_archive` looks for both, answers whether the
archive covers the newest dump, and refuses to answer the one question that
belongs to a deployment: how much lag is too much.

Nothing here writes to the archive or to the server.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import psycopg

from aftercare_agent.observability import MetricRecord, Metrics

from .freshness import BACKUP_METRIC_PREFIX, Verdict, age_text
from .tooling import OpsError

ARCHIVE_LAG_ENV = "AFTERCARE_BACKUP_ARCHIVE_LAG_SECONDS"
SEGMENT_PATTERN = re.compile(r"\A([0-9A-F]{8})([0-9A-F]{8})([0-9A-F]{8})\Z")
LSN_PATTERN = re.compile(r"\A([0-9A-Fa-f]{1,8})/([0-9A-Fa-f]{1,8})\Z")
SIZE_PATTERN = re.compile(r"\A(\d+)\s*(kB|MB|GB|B)?\Z")
HISTORY_SUFFIX = ".history"
PARTIAL_SUFFIX = ".partial"
LOG_SHIFT = 32
LOW_BITS = 0xFFFFFFFF
MIN_SEGMENT_BYTES = 1 << 20
MAX_SEGMENT_BYTES = 1 << 30
SIZE_UNITS = {"": 1, "B": 1, "kB": 1 << 10, "MB": 1 << 20, "GB": 1 << 30}


def parse_segment_size(text: str) -> int:
    """Read ``16MB``, ``16777216`` or ``16 MB`` as a byte count."""
    match = SIZE_PATTERN.fullmatch(text.strip())
    if match is None:
        raise OpsError(f"cannot read a WAL segment size from {text!r}; write it like 16MB")
    value = int(match.group(1)) * SIZE_UNITS[match.group(2) or ""]
    if not MIN_SEGMENT_BYTES <= value <= MAX_SEGMENT_BYTES or value & (value - 1):
        raise OpsError(f"a WAL segment is a power of two between 1MB and 1GB (got {text!r})")
    return value


def format_segment_size(value: int) -> str:
    return f"{value // (1 << 20)}MB" if value >= (1 << 20) else f"{value}B"


def parse_lsn(text: str) -> int:
    """The position behind PostgreSQL's ``0/16B3748`` notation."""
    match = LSN_PATTERN.fullmatch(text.strip())
    if match is None:
        raise OpsError(f"cannot read a WAL position from {text!r}; write it like 0/16B3748")
    return (int(match.group(1), 16) << LOG_SHIFT) | int(match.group(2), 16)


def format_lsn(value: int) -> str:
    return f"{value >> LOG_SHIFT:X}/{(value & LOW_BITS):X}"


def segment_holding(lsn: int, segment_bytes: int) -> tuple[int, int]:
    """The ``(log, segno)`` of the segment that holds *lsn*.

    The file name counts segments, not bytes, so locating an LSN needs the
    configured segment size; the position alone cannot say which file holds it.
    """
    return lsn >> LOG_SHIFT, (lsn & LOW_BITS) >> (segment_bytes.bit_length() - 1)


def segments_per_log(segment_bytes: int) -> int:
    return 1 << (LOG_SHIFT - (segment_bytes.bit_length() - 1))


@dataclass(frozen=True, order=True)
class Segment:
    """One WAL segment, as its file name describes it."""

    timeline: int
    log: int
    segno: int

    @property
    def name(self) -> str:
        return f"{self.timeline:08X}{self.log:08X}{self.segno:08X}"

    def index(self, segment_bytes: int) -> int:
        """A position that differs by one between neighbours on one timeline."""
        return self.log * segments_per_log(segment_bytes) + self.segno

    @classmethod
    def parse(cls, name: str) -> "Segment | None":
        match = SEGMENT_PATTERN.fullmatch(name)
        if match is None:
            return None
        timeline, log, segno = (int(part, 16) for part in match.groups())
        return cls(timeline=timeline, log=log, segno=segno)


def adjacent(previous: Segment, following: Segment) -> bool:
    """Are these two neighbours, whatever the segment size happens to be?

    Inside one log the file name counts up by one per segment; at a boundary the
    next log starts at zero.  That holds for every segment size, so contiguity
    never has to guess one -- only naming the files inside a hole does.
    """
    if following.log == previous.log:
        return following.segno == previous.segno + 1
    return following.log == previous.log + 1 and following.segno == 0


@dataclass(frozen=True)
class Gap:
    """A hole between two archived segments."""

    timeline: int
    after: Segment
    before: Segment
    missing: int | None

    def describe(self) -> str:
        span = f"timeline {self.timeline:08X}: {self.after.name} then {self.before.name}"
        if self.missing is None:
            return f"{span} (the number of missing segments needs the segment size)"
        return f"{span}, {self.missing} segment(s) missing"


@dataclass(frozen=True)
class ArchiveInventory:
    """What one archive directory holds, read from file names alone."""

    directory: Path
    per_timeline: Mapping[int, tuple[Segment, ...]]
    history_files: tuple[str, ...]
    partial_files: tuple[str, ...]
    other_files: int

    @property
    def timelines(self) -> tuple[int, ...]:
        return tuple(sorted(self.per_timeline))

    @property
    def newest_timeline(self) -> int | None:
        timelines = self.timelines
        return timelines[-1] if timelines else None

    @property
    def segments(self) -> tuple[Segment, ...]:
        return tuple(
            segment for timeline in self.timelines for segment in self.per_timeline[timeline]
        )

    @property
    def newest(self) -> Segment | None:
        timeline = self.newest_timeline
        if timeline is None:
            return None
        segments = self.per_timeline[timeline]
        return segments[-1] if segments else None

    @property
    def oldest(self) -> Segment | None:
        timeline = self.timelines[0] if self.timelines else None
        if timeline is None:
            return None
        segments = self.per_timeline[timeline]
        return segments[0] if segments else None

    def gaps(self, segment_bytes: int | None = None) -> tuple[Gap, ...]:
        """Holes inside a timeline, oldest first.

        Each timeline is checked on its own: after a failover or a recovery the
        same log/segment name appears again under a new timeline, so comparing
        across timelines would invent holes out of a healthy archive.
        """
        found: list[Gap] = []
        for timeline in self.timelines:
            segments = self.per_timeline[timeline]
            for previous, following in zip(segments, segments[1:], strict=False):
                if adjacent(previous, following):
                    continue
                missing: int | None = None
                if segment_bytes is not None:
                    missing = following.index(segment_bytes) - previous.index(segment_bytes) - 1
                found.append(
                    Gap(
                        timeline=timeline,
                        after=previous,
                        before=following,
                        missing=missing,
                    )
                )
        return tuple(found)


def read_inventory(directory: Path) -> ArchiveInventory:
    """List an archive directory; one that cannot be read is not an empty one."""
    if not directory.exists():
        raise OpsError(f"{directory} does not exist")
    if not directory.is_dir():
        raise OpsError(f"{directory} is not a directory")
    try:
        entries = sorted(directory.iterdir(), key=lambda entry: entry.name)
    except OSError as exc:
        raise OpsError(f"cannot list {directory}: {exc}") from exc
    per_timeline: dict[int, list[Segment]] = {}
    history: list[str] = []
    partials: list[str] = []
    other = 0
    for entry in entries:
        name = entry.name
        if not entry.is_file():
            other += 1
            continue
        if name.endswith(HISTORY_SUFFIX):
            history.append(name)
            continue
        if name.endswith(PARTIAL_SUFFIX):
            partials.append(name)
            continue
        segment = Segment.parse(name)
        if segment is None:
            other += 1
            continue
        per_timeline.setdefault(segment.timeline, []).append(segment)
    return ArchiveInventory(
        directory=directory,
        per_timeline={
            timeline: tuple(sorted(segments)) for timeline, segments in per_timeline.items()
        },
        history_files=tuple(history),
        partial_files=tuple(partials),
        other_files=other,
    )


@dataclass(frozen=True)
class ArchiverState:
    """What the live server says about its own archiver."""

    archived_count: int
    last_archived_wal: str | None
    last_archived_time: datetime | None
    failed_count: int
    last_failed_wal: str | None
    last_failed_time: datetime | None
    current_wal: str | None
    segment_bytes: int
    in_recovery: bool

    @property
    def stuck_on_failure(self) -> bool:
        """Is the newest failure newer than the newest success?

        ``pg_stat_archiver`` keeps only the last failure, so a transient one
        that was later overtaken by a success is not a stuck archiver: the
        question is which of the two happened last.
        """
        if self.last_failed_time is None:
            return False
        if self.last_archived_time is None:
            return True
        return self.last_failed_time > self.last_archived_time


def _scalar(connection: psycopg.Connection[Any], query: str) -> Any:
    row = connection.execute(query).fetchone()
    if row is None:
        raise OpsError(f"the server returned no row for {query!r}")
    return row[0]


def read_archiver_state(connection: psycopg.Connection[Any]) -> ArchiverState:
    """Read the archiver's own view of itself, plus where the WAL is now."""
    row = connection.execute(
        "SELECT archived_count, last_archived_wal, last_archived_time, failed_count, "
        "last_failed_wal, last_failed_time FROM pg_stat_archiver"
    ).fetchone()
    if row is None:
        raise OpsError("the server returned no pg_stat_archiver row")
    archived_count, last_wal, last_time, failed_count, failed_wal, failed_time = row
    segment_bytes = parse_segment_size(str(_scalar(connection, "SHOW wal_segment_size")))
    in_recovery = bool(_scalar(connection, "SELECT pg_is_in_recovery()"))
    current_wal: str | None = None
    if not in_recovery:
        current_wal = str(_scalar(connection, "SELECT pg_walfile_name(pg_current_wal_lsn())"))
    return ArchiverState(
        archived_count=int(archived_count),
        last_archived_wal=None if last_wal is None else str(last_wal),
        last_archived_time=last_time,
        failed_count=int(failed_count),
        last_failed_wal=None if failed_wal is None else str(failed_wal),
        last_failed_time=failed_time,
        current_wal=current_wal,
        segment_bytes=segment_bytes,
        in_recovery=in_recovery,
    )


@dataclass(frozen=True)
class ArchiveBudget:
    """The lag a deployment is willing to lose; unstated means unjudged.

    The lag is not the dump interval.  A dump says how far back a full copy
    reaches; the archive gap says how much of the most recent work is not
    recoverable at all.  Both are RPO-shaped, they measure different things,
    and neither has a default here.
    """

    lag_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.lag_seconds is not None and self.lag_seconds <= 0:
            raise OpsError("the archive lag budget must be a positive number of seconds")

    @property
    def configured(self) -> bool:
        return self.lag_seconds is not None

    def to_json(self) -> dict[str, Any]:
        return {"lag_seconds": self.lag_seconds, "configured": self.configured}


class ManifestLike(Protocol):
    """The part of a backup manifest an archive check reads."""

    @property
    def name(self) -> str: ...

    @property
    def wal_lsn(self) -> str | None: ...


@dataclass(frozen=True)
class Coverage:
    """Whether the archive reaches the WAL the newest dump needs."""

    manifest: str
    lsn: str
    ok: bool
    needed_segment: str
    newest_segment: str
    detail: str


@dataclass(frozen=True)
class ArchiveReport:
    """What one archive directory looks like from the outside."""

    directory: Path
    checked_at: datetime
    budget: ArchiveBudget
    segment_bytes: int | None
    segment_bytes_source: str
    timeline_count: int
    segment_count: int
    oldest_segment: str | None
    newest_segment: str | None
    gaps: tuple[Gap, ...]
    history_files: tuple[str, ...]
    partial_files: tuple[str, ...]
    other_files: int
    archiver: ArchiverState | None
    coverage: Coverage | None
    lag_seconds: float | None
    verdicts: tuple[Verdict, ...]
    problems: tuple[str, ...]
    notes: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.problems and all(verdict.ok for verdict in self.verdicts)

    def table(self) -> str:
        lines = [
            f"wal archive {self.directory}: {'ok' if self.ok else 'failed'}",
            f"  checked at       {self.checked_at.astimezone(UTC).isoformat()}",
        ]
        if self.segment_count == 0:
            lines.append("  segments         none")
        else:
            lines.append(
                f"  segments         {self.segment_count} over {self.timeline_count} "
                f"timeline(s) ({self.oldest_segment} .. {self.newest_segment})"
            )
        if self.segment_bytes is None:
            lines.append("  segment size     not stated and not readable from a server")
        else:
            lines.append(
                f"  segment size     {format_segment_size(self.segment_bytes)} "
                f"({self.segment_bytes_source})"
            )
        if not self.gaps:
            lines.append("  gaps             none")
        else:
            lines.append(f"  gaps             {len(self.gaps)}")
            for gap in self.gaps[:5]:
                lines.append(f"                   {gap.describe()}")
            if len(self.gaps) > 5:
                lines.append(f"                   and {len(self.gaps) - 5} more")
        if self.archiver is None:
            lines.append("  archiver         not read; pass --dsn to ask the server")
        else:
            lines.append(
                f"  archiver         newest segment {age_text(self.lag_seconds)} "
                f"({self.archiver.last_archived_wal or 'never'}), "
                f"{self.archiver.failed_count} failure(s)"
            )
        if self.coverage is None:
            lines.append("  newest backup    not checked against this archive")
        else:
            reached = "covered" if self.coverage.ok else "NOT covered"
            lines.append(
                f"  newest backup    {self.coverage.manifest} {reached} "
                f"(needs {self.coverage.needed_segment}, archive ends at "
                f"{self.coverage.newest_segment})"
            )
        if not self.budget.configured:
            lines.append("  budgets          no archive lag stated, so lag cannot fail here")
        for verdict in self.verdicts:
            lines.append(
                f"  [{'ok  ' if verdict.ok else 'FAIL'}] {verdict.claim}: {verdict.detail}"
            )
        for problem in self.problems:
            lines.append(f"  [FAIL] {problem}")
        for note in self.notes:
            lines.append(f"  [note] {note}")
        return "\n".join(lines)

    def to_json(self) -> dict[str, Any]:
        return {
            "directory": str(self.directory),
            "checked_at": self.checked_at.astimezone(UTC).isoformat(),
            "ok": self.ok,
            "budget": self.budget.to_json(),
            "segment_bytes": self.segment_bytes,
            "segment_bytes_source": self.segment_bytes_source,
            "timelines": self.timeline_count,
            "segments": self.segment_count,
            "oldest_segment": self.oldest_segment,
            "newest_segment": self.newest_segment,
            "gaps": [
                {
                    "timeline": gap.timeline,
                    "after": gap.after.name,
                    "before": gap.before.name,
                    "missing": gap.missing,
                }
                for gap in self.gaps
            ],
            "history_files": list(self.history_files),
            "partial_files": list(self.partial_files),
            "other_files": self.other_files,
            "lag_seconds": None if self.lag_seconds is None else round(self.lag_seconds, 3),
            "archiver": None
            if self.archiver is None
            else {
                "archived_count": self.archiver.archived_count,
                "last_archived_wal": self.archiver.last_archived_wal,
                "last_archived_time": None
                if self.archiver.last_archived_time is None
                else self.archiver.last_archived_time.astimezone(UTC).isoformat(),
                "failed_count": self.archiver.failed_count,
                "last_failed_wal": self.archiver.last_failed_wal,
                "last_failed_time": None
                if self.archiver.last_failed_time is None
                else self.archiver.last_failed_time.astimezone(UTC).isoformat(),
                "current_wal": self.archiver.current_wal,
                "in_recovery": self.archiver.in_recovery,
                "stuck_on_failure": self.archiver.stuck_on_failure,
            },
            "coverage": None
            if self.coverage is None
            else {
                "manifest": self.coverage.manifest,
                "wal_lsn": self.coverage.lsn,
                "ok": self.coverage.ok,
                "needed_segment": self.coverage.needed_segment,
                "newest_segment": self.coverage.newest_segment,
                "detail": self.coverage.detail,
            },
            "verdicts": [verdict.to_json() for verdict in self.verdicts],
            "problems": list(self.problems),
            "notes": list(self.notes),
        }


def _segment_size(stated: int | None, archiver: ArchiverState | None) -> tuple[int | None, str]:
    """The segment size to count with, and where it came from.

    The server's own value wins, because a name only means something next to
    the setting that produced it.  A stated size that contradicts the server is
    an operator error rather than a finding, so it fails here.
    """
    served = None if archiver is None else archiver.segment_bytes
    if stated is not None and served is not None and stated != served:
        raise OpsError(
            f"--segment-size {format_segment_size(stated)} contradicts the server's "
            f"{format_segment_size(served)}"
        )
    if served is not None:
        return served, "from the server"
    if stated is not None:
        return stated, "as stated"
    return None, "unstated"


def _seconds_between(earlier: datetime, later: datetime) -> float:
    return (later - earlier).total_seconds()


def _contiguity_verdict(
    gaps: tuple[Gap, ...], inventory: ArchiveInventory, segment_bytes: int | None
) -> Verdict:
    claim = "the archived segments form an unbroken chain"
    count = len(inventory.segments)
    if not gaps:
        return Verdict(
            claim,
            True,
            None,
            None,
            f"{count} segment(s) on {len(inventory.timelines)} timeline(s) with no hole",
        )
    first = gaps[0]
    detail = (
        f"{len(gaps)} hole(s), the first between {first.after.name} and {first.before.name} "
        f"on timeline {first.timeline:08X}; everything after a hole is unusable for recovery"
    )
    if segment_bytes is None:
        detail += " -- pass --segment-size to count what is missing"
    return Verdict(claim, False, None, None, detail)


def _archiver_verdict(archiver: ArchiverState) -> Verdict:
    claim = "the archiver is completing segments"
    if archiver.last_archived_wal is None or archiver.archived_count == 0:
        if archiver.stuck_on_failure:
            return Verdict(
                claim,
                False,
                None,
                None,
                f"no segment has ever been archived and the newest attempt "
                f"({archiver.last_failed_wal}) failed; nothing is protected yet",
            )
        return Verdict(
            claim,
            False,
            None,
            None,
            "the archiver has not completed a segment yet; nothing is protected yet",
        )
    archived_at = archiver.last_archived_time
    stamp = "at an unrecorded time" if archived_at is None else f"at {archived_at.isoformat()}"
    if archiver.stuck_on_failure:
        return Verdict(
            claim,
            False,
            None,
            None,
            (
                f"the newest attempt ({archiver.last_failed_wal}) failed after the newest "
                f"success ({archiver.last_archived_wal} {stamp}); PostgreSQL is retrying the "
                "same segment, so the archive is not advancing"
            ),
        )
    return Verdict(
        claim,
        True,
        None,
        None,
        f"{archiver.archived_count} segment(s) archived, newest "
        f"{archiver.last_archived_wal} {stamp}",
    )


def _lag_verdict(observed: float | None, limit: float) -> Verdict:
    claim = "the archive lag is inside the RPO budget"
    if observed is None:
        return Verdict(
            claim, False, limit, None, "no segment has been archived, so there is no lag to measure"
        )
    if observed <= limit:
        return Verdict(
            claim,
            True,
            limit,
            observed,
            f"the newest archived segment is {age_text(observed)}, inside {limit:.0f}s",
        )
    return Verdict(
        claim,
        False,
        limit,
        observed,
        f"the newest archived segment is {age_text(observed)}, past the {limit:.0f}s budget",
    )


def _coverage_verdict(coverage: Coverage) -> Verdict:
    return Verdict(
        "the archive reaches the newest backup",
        coverage.ok,
        None,
        None,
        coverage.detail,
    )


def _coverage(
    inventory: ArchiveInventory,
    manifest: ManifestLike | None,
    segment_bytes: int | None,
    notes: list[str],
) -> Coverage | None:
    """Does the archive hold the WAL the newest dump needs to roll forward?"""
    if manifest is None:
        return None
    newest = inventory.newest
    if manifest.wal_lsn is None:
        notes.append(
            f"{manifest.name} predates WAL positions in manifests; a new dump can be checked"
        )
        return None
    if segment_bytes is None:
        notes.append(
            "checking coverage needs the WAL segment size; pass --segment-size or --dsn, "
            "because a position alone cannot name the file that holds it"
        )
        return None
    needed_log, needed_segno = segment_holding(parse_lsn(manifest.wal_lsn), segment_bytes)
    needed = Segment(timeline=0, log=needed_log, segno=needed_segno)
    if newest is None:
        notes.append("there is no archived segment to compare the newest backup against")
        return None
    ok = (newest.log, newest.segno) >= (needed.log, needed.segno)
    if ok:
        detail = (
            f"the archive ends at {newest.name}, at or past the segment holding "
            f"{manifest.wal_lsn} ({needed.name}), so {manifest.name} can be rolled forward"
        )
    else:
        detail = (
            f"the archive ends at {newest.name} but {manifest.name} needs WAL from "
            f"{manifest.wal_lsn} ({needed.name}); the work since that dump has no archive "
            "behind it"
        )
    return Coverage(
        manifest=manifest.name,
        lsn=manifest.wal_lsn,
        ok=ok,
        needed_segment=needed.name,
        newest_segment=newest.name,
        detail=detail,
    )


def evaluate_archive(
    inventory: ArchiveInventory,
    *,
    archiver: ArchiverState | None = None,
    manifest: ManifestLike | None = None,
    budget: ArchiveBudget | None = None,
    segment_bytes: int | None = None,
    checked_at: datetime | None = None,
) -> ArchiveReport:
    """Answer what this archive can be asked, and say what it cannot."""
    budget = budget or ArchiveBudget()
    clock = checked_at or datetime.now(UTC)
    if budget.configured and archiver is None:
        raise OpsError("an archive lag budget needs a live server to measure against; pass --dsn")
    effective, source = _segment_size(segment_bytes, archiver)

    problems: list[str] = []
    notes: list[str] = []
    if not inventory.segments:
        problems.append(
            f"no WAL segment has been archived into {inventory.directory}; a dump on its own "
            "is the only protection this database has"
        )
    if inventory.partial_files:
        shown = ", ".join(inventory.partial_files[:3])
        problems.append(
            f"{len(inventory.partial_files)} partially copied segment(s) are in the archive "
            f"({shown}); an interrupted copy is not recovery capacity"
        )
    if inventory.other_files:
        notes.append(
            f"{inventory.other_files} file(s) in the directory are not WAL segments; ignored"
        )

    gaps = inventory.gaps(effective)
    verdicts = [_contiguity_verdict(gaps, inventory, effective)]
    if archiver is not None:
        verdicts.append(_archiver_verdict(archiver))
    coverage = _coverage(inventory, manifest, effective, notes)
    if coverage is not None:
        verdicts.append(_coverage_verdict(coverage))

    lag = None
    if archiver is not None and archiver.last_archived_time is not None:
        lag = _seconds_between(archiver.last_archived_time, clock)
    if budget.lag_seconds is not None:
        verdicts.append(_lag_verdict(lag, budget.lag_seconds))

    timelines = inventory.timelines
    if len(timelines) > 1:
        notes.append(
            f"the archive holds {len(timelines)} timelines; only the newest "
            f"({timelines[-1]:08X}) is reached without a recovery target, and contiguity is "
            "checked within each one"
        )
    if archiver is not None and archiver.in_recovery:
        notes.append(
            "the server is in recovery, so it archives nothing itself; the primary's archiver "
            "is the one that feeds this directory"
        )
    return ArchiveReport(
        directory=inventory.directory,
        checked_at=clock,
        budget=budget,
        segment_bytes=effective,
        segment_bytes_source=source,
        timeline_count=len(timelines),
        segment_count=len(inventory.segments),
        oldest_segment=None if inventory.oldest is None else inventory.oldest.name,
        newest_segment=None if inventory.newest is None else inventory.newest.name,
        gaps=gaps,
        history_files=inventory.history_files,
        partial_files=inventory.partial_files,
        other_files=inventory.other_files,
        archiver=archiver,
        coverage=coverage,
        lag_seconds=lag,
        verdicts=tuple(verdicts),
        problems=tuple(problems),
        notes=tuple(notes),
    )


def report_archive_status(metrics: Metrics, report: ArchiveReport, *, component: str) -> None:
    """Publish archive health under the same bounded label set as freshness."""
    attributes = {"component": component}
    gauges: dict[str, float] = {
        "wal_ok": 1.0 if report.ok else 0.0,
        "wal_segments": float(report.segment_count),
        "wal_timelines": float(report.timeline_count),
        "wal_gaps": float(len(report.gaps)),
    }
    if report.lag_seconds is not None:
        gauges["wal_lag_seconds"] = float(report.lag_seconds)
    if report.archiver is not None:
        gauges["wal_archiver_failures"] = float(report.archiver.failed_count)
    if report.coverage is not None:
        gauges["wal_covers_newest_backup"] = 1.0 if report.coverage.ok else 0.0
    for suffix, value in gauges.items():
        metrics.record(
            MetricRecord(f"{BACKUP_METRIC_PREFIX}.{suffix}", value, "gauge", dict(attributes))
        )
