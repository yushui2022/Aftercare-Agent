"""Is the newest backup recent enough, and has it been restored?

One backup directory holds two different answers.  How old the newest
recoverable point is answers "how much work would a disaster cost us" -- an RPO
question, and only a deployment can answer it.  When a restore was last proven
to work answers "does recovery still happen at all" -- a drill question, which
a schedule can answer on its own.  :func:`evaluate_status` reports both and
fails only against budgets a deployment states: an unset budget is reported as
unset and never as satisfied, because a tool that invents an RPO has invented a
promise nobody made.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from aftercare_agent.observability import MetricRecord, Metrics

from .drill_records import DrillRecord
from .tooling import OpsError

BACKUP_METRIC_PREFIX = "aftercare.backup"
RPO_ENV = "AFTERCARE_BACKUP_RPO_SECONDS"
DRILL_INTERVAL_ENV = "AFTERCARE_BACKUP_DRILL_INTERVAL_SECONDS"


class ManifestLike(Protocol):
    """The part of a manifest a freshness check reads.

    Read-only properties, because the manifest that satisfies this is a frozen
    model: a check never writes to what it measures.
    """

    @property
    def name(self) -> str: ...

    @property
    def taken_at(self) -> datetime: ...

    @property
    def dump_file(self) -> str: ...


@dataclass(frozen=True)
class StatusBudget:
    """Limits a deployment states; none of them has a default.

    A default RPO would be this tool promising a recovery target on behalf of
    whoever deployed it, and that is not the tool's promise to make.  An unset
    budget is therefore reported as unset, never as met.

    ``require_newest_drill`` asks a stronger question than the interval does:
    not "was anything restored recently" but "has *this* backup ever been
    restored".  Sending an undrilled dump off site is sending a file.
    """

    rpo_seconds: float | None = None
    drill_interval_seconds: float | None = None
    require_newest_drill: bool = False

    def __post_init__(self) -> None:
        limits = (("RPO", self.rpo_seconds), ("drill interval", self.drill_interval_seconds))
        for label, value in limits:
            if value is not None and value <= 0:
                raise OpsError(f"the {label} budget must be a positive number of seconds")

    @property
    def configured(self) -> bool:
        return any((self.rpo_seconds, self.drill_interval_seconds, self.require_newest_drill))

    def to_json(self) -> dict[str, Any]:
        return {
            "rpo_seconds": self.rpo_seconds,
            "drill_interval_seconds": self.drill_interval_seconds,
            "require_newest_drill": self.require_newest_drill,
            "configured": self.configured,
        }


@dataclass(frozen=True)
class Verdict:
    """One claim, the limit it was measured against, and what was measured."""

    claim: str
    ok: bool
    limit_seconds: float | None
    observed_seconds: float | None
    detail: str

    def to_json(self) -> dict[str, Any]:
        return {
            "claim": self.claim,
            "ok": self.ok,
            "limit_seconds": self.limit_seconds,
            "observed_seconds": (
                None if self.observed_seconds is None else round(self.observed_seconds, 3)
            ),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class BackupStatus:
    """What one backup directory looks like from the outside."""

    directory: Path
    checked_at: datetime
    budget: StatusBudget
    dump_count: int
    drilled_names: tuple[str, ...]
    undrilled_names: tuple[str, ...]
    newest_dump_name: str | None
    newest_dump_age_seconds: float | None
    last_drill: DrillRecord | None
    last_drill_age_seconds: float | None
    newest_ok_drill: DrillRecord | None
    newest_ok_drill_age_seconds: float | None
    verdicts: tuple[Verdict, ...]
    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.problems and all(verdict.ok for verdict in self.verdicts)

    def table(self) -> str:
        lines = [
            f"backup status {self.directory}: {'ok' if self.ok else 'failed'}",
            f"  checked at       {self.checked_at.astimezone(UTC).isoformat()}",
            f"  backups          {self.dump_count} "
            f"({len(self.drilled_names)} restored, {len(self.undrilled_names)} never restored)",
        ]
        if self.newest_dump_name is None:
            lines.append("  newest dump      none")
        else:
            lines.append(
                f"  newest dump      {self.newest_dump_name} "
                f"({age_text(self.newest_dump_age_seconds)})"
            )
        if self.last_drill is None:
            lines.append("  last drill       never recorded in this directory")
        else:
            outcome = "ok" if self.last_drill.ok else "failed"
            lines.append(
                f"  last drill       {self.last_drill.name} {outcome} "
                f"({age_text(self.last_drill_age_seconds)})"
            )
        if self.newest_ok_drill is not None:
            lines.append(
                f"  last good drill  {self.newest_ok_drill.name} "
                f"({age_text(self.newest_ok_drill_age_seconds)})"
            )
        if not self.budget.configured:
            lines.append("  budgets          none stated, so nothing here can fail")
        for verdict in self.verdicts:
            lines.append(
                f"  [{'ok  ' if verdict.ok else 'FAIL'}] {verdict.claim}: {verdict.detail}"
            )
        for problem in self.problems:
            lines.append(f"  [FAIL] {problem}")
        return "\n".join(lines)

    def to_json(self) -> dict[str, Any]:
        return {
            "directory": str(self.directory),
            "checked_at": self.checked_at.astimezone(UTC).isoformat(),
            "ok": self.ok,
            "budget": self.budget.to_json(),
            "dumps": self.dump_count,
            "drilled": list(self.drilled_names),
            "undrilled": list(self.undrilled_names),
            "newest_dump": self.newest_dump_name,
            "newest_dump_age_seconds": (
                None
                if self.newest_dump_age_seconds is None
                else round(self.newest_dump_age_seconds, 3)
            ),
            "last_drill": None
            if self.last_drill is None
            else self.last_drill.model_dump(mode="json"),
            "last_drill_age_seconds": (
                None
                if self.last_drill_age_seconds is None
                else round(self.last_drill_age_seconds, 3)
            ),
            "newest_ok_drill_age_seconds": (
                None
                if self.newest_ok_drill_age_seconds is None
                else round(self.newest_ok_drill_age_seconds, 3)
            ),
            "verdicts": [verdict.to_json() for verdict in self.verdicts],
            "problems": list(self.problems),
        }


def age_text(seconds: float | None) -> str:
    if seconds is None:
        return "unknown age"
    if seconds < 90:
        return f"{seconds:.0f}s old"
    if seconds < 5400:
        return f"{seconds / 60:.1f}min old"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h old"
    return f"{seconds / 86400:.1f}d old"


def _seconds_between(earlier: datetime, later: datetime) -> float:
    return max(0.0, (later - earlier).total_seconds())


def evaluate_status(
    directory: Path,
    manifests: Sequence[ManifestLike],
    records: Sequence[DrillRecord],
    *,
    budget: StatusBudget | None = None,
    now: datetime | None = None,
) -> BackupStatus:
    """Judge one backup directory against the budgets a deployment stated.

    Every verdict names the limit it used, so a report cannot be read as a
    judgement the deployment never asked for.  A problem -- a manifest whose
    dump is missing, a directory with no backup at all -- fails the status
    regardless of budgets: those are not policy questions.
    """
    checked_at = now or datetime.now(UTC)
    if checked_at.tzinfo is None:
        raise OpsError("the clock reading must carry a timezone")
    budget = budget or StatusBudget()
    ordered = sorted(manifests, key=lambda manifest: manifest.taken_at)
    ordered_records = sorted(records, key=lambda record: record.finished_at)

    problems: list[str] = []
    if not ordered:
        problems.append(f"{directory} holds no backup manifest at all")
    for manifest in ordered:
        if not (directory / manifest.dump_file).is_file():
            problems.append(f"{manifest.name} has no dump: {manifest.dump_file} is missing")

    restored = {record.name for record in ordered_records if record.ok}
    drilled = tuple(manifest.name for manifest in ordered if manifest.name in restored)
    undrilled = tuple(manifest.name for manifest in ordered if manifest.name not in restored)
    newest = ordered[-1] if ordered else None
    newest_age = None if newest is None else _seconds_between(newest.taken_at, checked_at)
    last_drill = ordered_records[-1] if ordered_records else None
    last_drill_age = (
        None if last_drill is None else _seconds_between(last_drill.finished_at, checked_at)
    )
    good = [record for record in ordered_records if record.ok]
    newest_ok = good[-1] if good else None
    newest_ok_age = (
        None if newest_ok is None else _seconds_between(newest_ok.finished_at, checked_at)
    )

    verdicts: list[Verdict] = []
    if budget.rpo_seconds is not None:
        verdicts.append(_rpo_verdict(newest, newest_age, budget.rpo_seconds))
    if budget.drill_interval_seconds is not None:
        verdicts.append(_drill_verdict(newest_ok, newest_ok_age, budget.drill_interval_seconds))
    if budget.require_newest_drill or budget.drill_interval_seconds is not None:
        verdicts.append(_newest_drill_verdict(newest, restored))

    return BackupStatus(
        directory=directory,
        checked_at=checked_at,
        budget=budget,
        dump_count=len(ordered),
        drilled_names=drilled,
        undrilled_names=undrilled,
        newest_dump_name=None if newest is None else newest.name,
        newest_dump_age_seconds=newest_age,
        last_drill=last_drill,
        last_drill_age_seconds=last_drill_age,
        newest_ok_drill=newest_ok,
        newest_ok_drill_age_seconds=newest_ok_age,
        verdicts=tuple(verdicts),
        problems=tuple(problems),
    )


def _rpo_verdict(newest: ManifestLike | None, observed: float | None, limit: float) -> Verdict:
    claim = "the newest recovery point is inside the RPO budget"
    if newest is None or observed is None:
        return Verdict(claim, False, limit, None, "there is no recovery point to measure")
    if observed <= limit:
        detail = (
            f"the newest recovery point ({newest.name}) is {age_text(observed)}, "
            f"inside {limit:.0f}s"
        )
        return Verdict(claim, True, limit, observed, detail)
    detail = (
        f"the newest recovery point ({newest.name}) is {age_text(observed)}, "
        f"past the {limit:.0f}s budget"
    )
    return Verdict(claim, False, limit, observed, detail)


def _drill_verdict(newest_ok: DrillRecord | None, observed: float | None, limit: float) -> Verdict:
    claim = "a restore has been proven inside the drill interval"
    if newest_ok is None or observed is None:
        return Verdict(
            claim,
            False,
            limit,
            None,
            "no drill has ever been recorded in this directory",
        )
    if observed <= limit:
        detail = (
            f"the newest restored backup ({newest_ok.name}) is {age_text(observed)}, "
            f"inside {limit:.0f}s"
        )
        return Verdict(claim, True, limit, observed, detail)
    detail = (
        f"the newest restored backup ({newest_ok.name}) is {age_text(observed)}, "
        f"past the {limit:.0f}s interval"
    )
    return Verdict(claim, False, limit, observed, detail)


def _newest_drill_verdict(newest: ManifestLike | None, restored: set[str]) -> Verdict:
    claim = "the newest backup has been restored"
    if newest is None:
        return Verdict(claim, False, None, None, "there is no backup to restore")
    if newest.name in restored:
        return Verdict(
            claim, True, None, None, f"{newest.name} was restored and compared at least once"
        )
    return Verdict(
        claim,
        False,
        None,
        None,
        (
            f"{newest.name} has never been restored; an unrehearsed dump is a file, "
            "not recovery capacity"
        ),
    )


def report_backup_status(metrics: Metrics, status: BackupStatus, *, component: str) -> None:
    """Publish freshness under a bounded label set.

    The only attribute is which component reported, for the same reason the
    pool gauges carry one: a backup gauge labelled with a database name or a
    directory would explode cardinality and leak where copies live.
    """
    attributes = {"component": component}
    gauges: dict[str, float] = {
        "dumps": float(status.dump_count),
        "dumps.drilled": float(len(status.drilled_names)),
        "dumps.undrilled": float(len(status.undrilled_names)),
        "budget_ok": 1.0 if status.ok else 0.0,
    }
    if status.newest_dump_age_seconds is not None:
        gauges["newest_dump_age_seconds"] = float(status.newest_dump_age_seconds)
    if status.newest_ok_drill_age_seconds is not None:
        gauges["newest_drill_age_seconds"] = float(status.newest_ok_drill_age_seconds)
    if status.last_drill is not None:
        gauges["last_drill_ok"] = 1.0 if status.last_drill.ok else 0.0
    for suffix, value in gauges.items():
        metrics.record(
            MetricRecord(f"{BACKUP_METRIC_PREFIX}.{suffix}", value, "gauge", dict(attributes))
        )
