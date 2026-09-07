"""Wait candidate decisions, not durable activation or concurrent transaction tests."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.protocol import ArtifactReference
from aftercare_agent.domain.waits import (
    InboxSignal,
    WaitRecord,
    check_inbox_replay,
    inbox_dedup_key,
    matches_wait,
    resolution_candidate,
    wakeup_key,
)

REGISTERED = datetime(2026, 9, 7, 12, tzinfo=UTC)
DEADLINE = REGISTERED + timedelta(hours=1)


def _wait(**changes: object) -> WaitRecord:
    values: dict[str, object] = {
        "tenant_id": "tenant-1",
        "case_id": "case-1",
        "run_id": "run-1",
        "wait_id": "wait-1",
        "generation": 1,
        "kind": "input",
        "correlation_key": "correlation-1",
        "condition_version": "material-v1",
        "created_at": REGISTERED,
        "deadline": DEADLINE,
        "state": "ACTIVE",
    }
    values.update(changes)
    return WaitRecord.model_validate(values)


def _signal(**changes: object) -> InboxSignal:
    values: dict[str, object] = {
        "tenant_id": "tenant-1",
        "case_id": "case-1",
        "run_id": "run-1",
        "event_id": "event-1",
        "source_id": "synthetic-buyer",
        "source_event_id": "message-1",
        "wait_id": "wait-1",
        "generation": 1,
        "kind": "input",
        "correlation_key": "correlation-1",
        "condition_version": "material-v1",
        "received_at": REGISTERED + timedelta(seconds=1),
    }
    values.update(changes)
    values.setdefault(
        "payload",
        ArtifactReference(
            tenant_id=str(values["tenant_id"]),
            case_id=str(values["case_id"]),
            reference_id="raw-message-1",
            sha256="a" * 64,
        ),
    )
    return InboxSignal.model_validate(values)


def test_input_replay_preserves_scope_content_and_original_time() -> None:
    original = _signal()
    check_inbox_replay(original, InboxSignal.model_validate_json(original.model_dump_json()))
    alternatives = (
        _signal(case_id="case-2"),
        _signal(received_at=REGISTERED + timedelta(seconds=2)),
        _signal(
            payload=ArtifactReference(
                tenant_id="tenant-1",
                case_id="case-1",
                reference_id="raw-message-1",
                sha256="b" * 64,
            )
        ),
    )
    for changed in alternatives:
        assert inbox_dedup_key(original) == inbox_dedup_key(changed)
        with pytest.raises(ContractViolation) as caught:
            check_inbox_replay(original, changed)
        assert caught.value.code == ErrorCode.CONFLICT
    with pytest.raises(ValidationError):
        _signal(
            payload=ArtifactReference(
                tenant_id="tenant-1",
                case_id="other",
                reference_id="raw-message-1",
                sha256="a" * 64,
            )
        )


def test_early_reply_is_retained_but_only_active_wait_can_be_resolved() -> None:
    reply = _signal()
    pending = _wait(state="PENDING")
    assert matches_wait(pending, reply)
    assert (
        resolution_candidate(
            pending,
            current_generation=1,
            db_now=REGISTERED + timedelta(minutes=5),
            committed_reply=reply,
        )
        == "none"
    )
    active = WaitRecord.model_validate({**pending.model_dump(), "state": "ACTIVE"})
    assert (
        resolution_candidate(
            active,
            current_generation=1,
            db_now=REGISTERED + timedelta(minutes=5),
            committed_reply=reply,
        )
        == "satisfied"
    )
    assert pending.state == "PENDING"


def test_deadline_enables_timeout_but_is_not_a_strict_reply_cutoff() -> None:
    late_reply = _signal(received_at=DEADLINE + timedelta(minutes=1))
    assert (
        resolution_candidate(
            _wait(),
            current_generation=1,
            db_now=DEADLINE + timedelta(minutes=2),
            committed_reply=late_reply,
        )
        == "satisfied"
    )


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        (DEADLINE - timedelta(microseconds=1), "none"),
        (DEADLINE, "timed_out"),
        (DEADLINE + timedelta(seconds=1), "timed_out"),
    ],
)
def test_timeout_boundary_uses_database_clock(instant: datetime, expected: str) -> None:
    assert (
        resolution_candidate(_wait(), current_generation=1, db_now=instant, committed_reply=None)
        == expected
    )


def test_matching_committed_reply_wins_over_a_timeout_candidate_at_deadline() -> None:
    assert (
        resolution_candidate(
            _wait(),
            current_generation=1,
            db_now=DEADLINE,
            committed_reply=_signal(received_at=DEADLINE),
        )
        == "satisfied"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"tenant_id": "tenant-2"},
        {"case_id": "case-2"},
        {"run_id": "run-2"},
        {"wait_id": "wait-2"},
        {"generation": 2},
        {"kind": "approval"},
        {"correlation_key": "correlation-2"},
        {"condition_version": "material-v2"},
    ],
)
def test_reply_requires_every_scope_generation_and_condition_binding(
    changes: dict[str, object],
) -> None:
    signal = _signal(**changes)
    assert not matches_wait(_wait(), signal)
    assert (
        resolution_candidate(
            _wait(),
            current_generation=1,
            db_now=REGISTERED + timedelta(minutes=5),
            committed_reply=signal,
        )
        == "none"
    )


@pytest.mark.parametrize("current_generation", [0, 2, True])
def test_stale_wait_generation_cannot_resolve_or_time_out(current_generation: int) -> None:
    assert (
        resolution_candidate(
            _wait(),
            current_generation=current_generation,
            db_now=DEADLINE,
            committed_reply=_signal(),
        )
        == "none"
    )
    assert (
        resolution_candidate(
            _wait(), current_generation=current_generation, db_now=DEADLINE, committed_reply=None
        )
        == "none"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"state": "SATISFIED", "resolved_by_event_id": "winner-1"},
        {"state": "TIMED_OUT"},
        {"state": "CANCELLED"},
    ],
)
def test_settled_wait_cannot_choose_a_second_branch(changes: dict[str, object]) -> None:
    settled = _wait(**changes)
    assert (
        resolution_candidate(
            settled,
            current_generation=1,
            db_now=DEADLINE + timedelta(seconds=1),
            committed_reply=_signal(),
        )
        == "none"
    )
    assert (
        resolution_candidate(
            settled,
            current_generation=1,
            db_now=DEADLINE + timedelta(seconds=1),
            committed_reply=None,
        )
        == "none"
    )


@pytest.mark.parametrize(
    "received_at", [REGISTERED - timedelta(seconds=1), REGISTERED + timedelta(minutes=6)]
)
def test_observation_before_registration_or_after_current_clock_cannot_satisfy(
    received_at: datetime,
) -> None:
    assert (
        resolution_candidate(
            _wait(),
            current_generation=1,
            db_now=REGISTERED + timedelta(minutes=5),
            committed_reply=_signal(received_at=received_at),
        )
        == "none"
    )


def test_clock_before_registration_or_naive_clock_is_not_a_valid_resolution() -> None:
    assert (
        resolution_candidate(
            _wait(),
            current_generation=1,
            db_now=REGISTERED - timedelta(seconds=1),
            committed_reply=None,
        )
        == "none"
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        resolution_candidate(
            _wait(),
            current_generation=1,
            db_now=DEADLINE.replace(tzinfo=None),
            committed_reply=None,
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"deadline": REGISTERED},
        {"deadline": REGISTERED - timedelta(seconds=1)},
        {"state": "SATISFIED"},
        {"state": "ACTIVE", "resolved_by_event_id": "event-1"},
        {"state": "TIMED_OUT", "resolved_by_event_id": "event-1"},
        {"generation": 0},
        {"generation": True},
    ],
)
def test_wait_record_rejects_incoherent_state(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _wait(**changes)


def test_inbox_identity_and_wakeup_identity_are_different() -> None:
    first = _signal()
    assert inbox_dedup_key(first) == ("tenant-1", "synthetic-buyer", "message-1")
    assert inbox_dedup_key(first) == inbox_dedup_key(_signal(event_id="event-redelivery"))
    assert inbox_dedup_key(first) != inbox_dedup_key(_signal(source_id="other-source"))
    assert inbox_dedup_key(first) != inbox_dedup_key(_signal(tenant_id="tenant-2"))
    assert wakeup_key(_wait()) == ("tenant-1", "case-1", "run-1", "wait-1", 1)
    assert wakeup_key(_wait()) != wakeup_key(_wait(generation=2))
