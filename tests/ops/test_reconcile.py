"""The review list a recovery leaves behind, without touching a database."""

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import psycopg
import pytest

from aftercare_agent.ops.reconcile import (
    ReconciliationEntry,
    ReconciliationReport,
    build_reconciliation,
    classify,
    reconciliation_entry,
)
from aftercare_agent.ops.tooling import OpsError

CUT = datetime(2026, 9, 15, 8, 0, tzinfo=UTC)
ROW = (
    "tenant-1",
    "case-1",
    "action-1",
    "refund",
    "CONFIRMED",
    "psp-reference-1",
    "refund:r1",
    CUT + timedelta(seconds=5),
)


def _entry(action_id: str, reason: str) -> ReconciliationEntry:
    return ReconciliationEntry(
        tenant_id="tenant-1",
        case_id="case-1",
        action_id=action_id,
        action_type="refund",
        state="UNKNOWN" if reason == "unknown_outcome" else "CONFIRMED",
        updated_at=CUT,
        reason=cast(Any, reason),
    )


def test_an_unknown_outcome_is_reviewed_however_old_it_is() -> None:
    # UNKNOWN is a durable outcome, not a stale copy of one.
    assert classify("UNKNOWN", CUT - timedelta(days=10), CUT) == "unknown_outcome"


def test_an_action_that_changed_after_the_cut_is_reviewed() -> None:
    assert classify("CONFIRMED", CUT + timedelta(seconds=1), CUT) == "changed_since_cut"
    assert classify("FAILED", CUT + timedelta(seconds=1), CUT) == "changed_since_cut"


def test_an_action_inside_the_copy_is_not_reviewed() -> None:
    for state in ("RESERVED", "REQUESTED", "CONFIRMED", "FAILED"):
        with pytest.raises(OpsError, match="outside the reconciliation window"):
            classify(cast(Any, state), CUT - timedelta(seconds=1), CUT)
    # The cut itself belongs to the copy: only strictly later rows are listed.
    with pytest.raises(OpsError):
        classify("CONFIRMED", CUT, CUT)


def test_a_row_becomes_an_entry_with_its_reason() -> None:
    entry = reconciliation_entry(ROW, cut=CUT)
    assert entry.action_id == "action-1"
    assert entry.provider_reference == "psp-reference-1"
    assert entry.reason == "changed_since_cut"
    assert reconciliation_entry(ROW[:4] + ("UNKNOWN",) + ROW[5:], cut=CUT).reason == (
        "unknown_outcome"
    )


def test_the_report_says_when_there_is_nothing_to_review() -> None:
    report = ReconciliationReport(
        recovery_point=CUT,
        cut_at=CUT,
        safety_margin_seconds=0.0,
        generated_at=CUT,
        limit=200,
        entries=(),
    )
    assert not report.needs_review
    assert report.unknown_outcomes == ()
    assert "none is UNKNOWN" in report.table()


def test_the_report_counts_the_outcomes_and_says_when_it_is_capped() -> None:
    entries = (
        _entry("action-unknown", "unknown_outcome"),
        _entry("action-changed", "changed_since_cut"),
    )
    report = ReconciliationReport(
        recovery_point=CUT,
        cut_at=CUT - timedelta(seconds=60),
        safety_margin_seconds=60.0,
        generated_at=CUT,
        limit=2,
        entries=entries,
    )
    assert report.needs_review
    assert [entry.action_id for entry in report.unknown_outcomes] == ["action-unknown"]
    text = report.table()
    assert "[unknown_outcome] tenant-1/case-1 action-unknown" in text
    assert "list capped at 2" in text
    payload = report.to_json()
    assert payload["entries"][0]["reason"] == "unknown_outcome"


def test_a_limit_or_margin_the_query_cannot_serve_is_refused() -> None:
    connection = cast(psycopg.Connection[Any], None)
    with pytest.raises(OpsError, match="limit must be between"):
        build_reconciliation(connection, recovery_point=CUT, limit=0)
    with pytest.raises(OpsError, match="limit must be between"):
        build_reconciliation(connection, recovery_point=CUT, limit=1001)
    with pytest.raises(OpsError, match="cannot be negative"):
        build_reconciliation(connection, recovery_point=CUT, safety_margin_seconds=-1.0)
