"""Case-local ordering and replay rules; persistence and stream transport are absent."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.events import (
    DomainEvent,
    ProjectionPosition,
    consumer_application_key,
    projection_decision,
)
from aftercare_agent.domain.protocol import ArtifactReference

NOW = datetime(2026, 9, 7, 12, tzinfo=UTC)


def _event(**changes: object) -> DomainEvent:
    values: dict[str, object] = {
        "tenant_id": "tenant-1",
        "case_id": "case-1",
        "event_id": "event-1",
        "case_seq": 1,
        "event_type": "case.opened",
        "correlation_id": "workflow-1",
        "recorded_at": NOW,
        "payload": ArtifactReference(
            tenant_id="tenant-1",
            case_id="case-1",
            reference_id="payload-1",
            sha256="a" * 64,
        ),
    }
    values.update(changes)
    return DomainEvent.model_validate(values)


def _position(last_case_seq: int, **changes: object) -> ProjectionPosition:
    values: dict[str, object] = {
        "tenant_id": "tenant-1",
        "case_id": "case-1",
        "consumer_id": "ui-v1",
        "last_case_seq": last_case_seq,
    }
    values.update(changes)
    return ProjectionPosition.model_validate(values)


def test_projection_only_applies_the_next_contiguous_sequence() -> None:
    assert projection_decision(_position(0), _event(), applied_at_sequence=None) == "apply"
    assert (
        projection_decision(_position(40), _event(case_seq=42), applied_at_sequence=None)
        == "buffer_gap"
    )
    assert (
        projection_decision(_position(40), _event(case_seq=41), applied_at_sequence=None) == "apply"
    )
    assert (
        projection_decision(_position(41), _event(case_seq=42), applied_at_sequence=None) == "apply"
    )


def test_identical_applied_event_is_a_replay_even_below_the_position() -> None:
    original = _event()
    restored = DomainEvent.model_validate_json(original.model_dump_json())
    assert projection_decision(_position(3), restored, applied_at_sequence=original) == "replay"


@pytest.mark.parametrize(
    "changes",
    [
        {"event_id": "event-other"},
        {"correlation_id": "workflow-other"},
        {"event_type": "input.received"},
        {"causation_id": "previous-other"},
        {
            "payload": ArtifactReference(
                tenant_id="tenant-1",
                case_id="case-1",
                reference_id="payload-1",
                sha256="b" * 64,
            )
        },
    ],
)
def test_same_sequence_with_different_event_identity_or_content_is_conflict(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ContractViolation) as raised:
        projection_decision(_position(1), _event(**changes), applied_at_sequence=_event())
    assert raised.value.code == ErrorCode.CONFLICT


def test_absent_applied_event_cannot_be_silently_treated_as_a_replay() -> None:
    with pytest.raises(ContractViolation) as raised:
        projection_decision(_position(3), _event(), applied_at_sequence=None)
    assert raised.value.code == ErrorCode.CONFLICT


def test_applied_record_ahead_of_projection_is_an_inconsistent_checkpoint() -> None:
    with pytest.raises(ContractViolation) as raised:
        projection_decision(_position(0), _event(), applied_at_sequence=_event())
    assert raised.value.code == ErrorCode.CONFLICT


@pytest.mark.parametrize("field", ["tenant_id", "case_id"])
def test_projection_position_must_share_the_event_scope(field: str) -> None:
    with pytest.raises(ContractViolation) as raised:
        projection_decision(_position(0, **{field: "another"}), _event(), applied_at_sequence=None)
    assert raised.value.code == ErrorCode.FORBIDDEN


@pytest.mark.parametrize("field", ["tenant_id", "case_id"])
def test_event_payload_cannot_reference_a_different_scope(field: str) -> None:
    with pytest.raises(ValidationError):
        _event(**{field: "another"})


@pytest.mark.parametrize(
    "event_type", ["run.state_changed", "wait.resolved", "investigation.proposed"]
)
def test_run_related_events_require_run_identity(event_type: str) -> None:
    with pytest.raises(ValidationError):
        _event(event_type=event_type)
    assert _event(event_type=event_type, run_id="run-1").run_id == "run-1"


@pytest.mark.parametrize(
    "changes",
    [{"case_seq": 0}, {"case_seq": True}, {"schema_version": 2}, {"payload_schema_version": 2}],
)
def test_event_versions_and_sequence_are_explicit_v1_values(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _event(**changes)


def test_consumer_application_is_not_a_global_processed_flag() -> None:
    event = _event()
    assert consumer_application_key("ui-v1", event) == ("tenant-1", "case-1", "ui-v1", "event-1")
    assert consumer_application_key("ui-v1", event) != consumer_application_key("audit-v1", event)
