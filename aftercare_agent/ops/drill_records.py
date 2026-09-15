"""The record a restore drill leaves beside the backup it rehearsed.

A dump is a file; recovery capacity is a dump that has been restored and
compared against what it claimed to hold.  The drill proves that, and this
module is where the proof is kept: one small JSON file next to the manifest,
written whether the drill passed or failed.  A schedule can then ask "when was
this backup last restored, and did that work?" without reading anyone's logs,
and :mod:`aftercare_agent.ops.freshness` is what asks.

Nothing here opens a database and nothing here decides anything: it stores what
a drill did and reads it back.
"""

import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, ValidationError

from aftercare_agent.domain.common import UtcDatetime

from .tooling import BackupName, OpsError, OpsModel, validate_backup_name

DRILL_SUFFIX = ".drill.json"
DRILL_RECORD_VERSION = 1
MAX_DETAIL = 400


class DrillCheck(OpsModel):
    """One check from the drill, kept so the verdict stays readable."""

    check: Annotated[str, Field(min_length=1, max_length=120)]
    ok: bool
    detail: Annotated[str, Field(max_length=MAX_DETAIL)]


class DrillRecord(OpsModel):
    """What the last drill of one backup did, whether or not it worked.

    A failure is recorded as carefully as a success: "this backup was drilled
    last night and the restore failed" is what a schedule needs to hear, and a
    missing file cannot be told apart from "nobody ever tried".
    """

    record_version: Literal[1] = 1
    name: BackupName
    recovery_point: UtcDatetime
    finished_at: UtcDatetime
    ok: bool
    rto_seconds: Annotated[float, Field(ge=0)] | None = None
    error: Annotated[str, Field(max_length=MAX_DETAIL)] | None = None
    checks: tuple[DrillCheck, ...] = ()

    def to_json(self) -> str:
        payload = self.model_dump(mode="json")
        return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    @classmethod
    def from_json(cls, text: str, *, source: str) -> "DrillRecord":
        try:
            return cls.model_validate_json(text)
        except ValidationError as exc:
            raise OpsError(f"{source} is not a readable drill record: {exc}") from exc


def drill_record_path(directory: Path, name: str) -> Path:
    """Where the record for *name* lives; the name is validated first."""
    return directory / f"{validate_backup_name(name)}{DRILL_SUFFIX}"


def write_drill_record(directory: Path, record: DrillRecord) -> Path:
    """Write the record beside the dump it describes."""
    path = drill_record_path(directory, record.name)
    path.write_text(record.to_json(), encoding="utf-8", newline="\n")
    return path


def load_drill_records(directory: Path) -> tuple[DrillRecord, ...]:
    """Every record in *directory*, oldest first.

    A record that cannot be read is an error rather than a skipped file: a
    schedule that could not tell "no drill was ever recorded" from "the record
    is unreadable" would report the wrong one of the two.
    """
    records = [
        DrillRecord.from_json(path.read_text(encoding="utf-8"), source=str(path))
        for path in sorted(directory.glob(f"*{DRILL_SUFFIX}"))
    ]
    records.sort(key=lambda record: record.finished_at)
    return tuple(records)
