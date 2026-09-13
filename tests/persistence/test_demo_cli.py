"""PostgreSQL smoke coverage for the copyable synthetic demo."""

import os
from typing import cast
from uuid import uuid4

import pytest

from aftercare_agent.demo import _new_case, run_demo
from aftercare_agent.persistence import Database


@pytest.fixture()
def db() -> Database:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL is not configured")
    return Database(dsn)


def test_demo_runs_one_complete_fake_aftercare_slice(db: Database) -> None:
    case = _new_case(
        tenant_id=f"demo-test-{uuid4().hex[:10]}",
        case_id=f"case-{uuid4().hex[:10]}",
        order_id=None,
    )

    report = run_demo(db, case, message="synthetic demo message")

    assert report["status"] == "completed"
    assert report["boundaries"] == {
        "model": "none",
        "provider": "fake-only",
        "sandbox": "none",
        "database": "postgresql",
    }
    approval = cast(dict[str, object], report["approval"])
    phases = cast(list[dict[str, object]], report["phases"])
    events = cast(list[dict[str, object]], report["events"])
    assert approval["decision"] == "APPROVED"
    assert phases[-1]["action_state"] == "CONFIRMED"
    assert [event["event_type"] for event in events] == [
        "approval.requested",
        "approval.decided",
    ]
