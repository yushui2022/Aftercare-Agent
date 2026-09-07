"""Pure boundary tests, not model execution or crash-recovery integration tests."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from aftercare_agent.domain.common import ContractViolation, ErrorCode, RunScope
from aftercare_agent.domain.protocol import (
    ArtifactReference,
    BoundLookupArguments,
    Checkpoint,
    MaterialDraftArguments,
    RemainingBudget,
    ToolRequest,
    ToolResultReference,
    assert_resume_compatible,
    decode_checkpoint,
    strict_json_object,
    validate_tool_request,
)

NOW = datetime(2026, 9, 7, 0, 0, tzinfo=UTC)
SCOPE = RunScope(tenant_id="merchant-1", case_id="case-1", run_id="run-1")


def budget(**changes: object) -> RemainingBudget:
    fields: dict[str, object] = {
        "model_calls": 4,
        "tool_calls": 4,
        "cost_microusd": 10_000,
        "deadline": NOW + timedelta(minutes=5),
    }
    fields.update(changes)
    return RemainingBudget.model_validate(fields)


def artifact(**changes: object) -> ArtifactReference:
    fields: dict[str, object] = {
        "tenant_id": "merchant-1",
        "case_id": "case-1",
        "reference_id": "blob-1",
        "sha256": "a" * 64,
    }
    fields.update(changes)
    return ArtifactReference.model_validate(fields)


def checkpoint(**changes: object) -> Checkpoint:
    fields: dict[str, object] = {
        **SCOPE.model_dump(),
        "schema_version": 1,
        "checkpoint_version": 1,
        "input_version": 1,
        "case_version": 3,
        "saved_fencing_token": 7,
        "step_id": "step-1",
        "attempt_id": "attempt-1",
        "definition_version": "investigator-v1",
        "policy_version": "non-receipt-v1",
        "tool_schema_version": "tools-v1",
        "model_config_version": "fake-v1",
        "protocol_version": "fake-transcript-v1",
        "protocol_ref": artifact(),
        "remaining_budget": budget(),
        "next_step": "evaluate",
    }
    fields.update(changes)
    return Checkpoint.model_validate(fields)


def test_checkpoint_round_trip_and_old_fence_is_only_provenance() -> None:
    stored = checkpoint()
    restored = decode_checkpoint(stored.model_dump_json())
    assert restored == stored
    assert restored.saved_fencing_token == 7
    assert_resume_compatible(
        restored,
        SCOPE,
        expected_input_version=1,
        expected_case_version=3,
        protocol_version="fake-transcript-v1",
    )


@pytest.mark.parametrize(
    "version, code",
    [
        (2, ErrorCode.UNSUPPORTED_VERSION),
        ("1", ErrorCode.INVALID_INPUT),
        (True, ErrorCode.INVALID_INPUT),
        (None, ErrorCode.INVALID_INPUT),
    ],
)
def test_checkpoint_bad_version_rejected(version: object, code: ErrorCode) -> None:
    fields = checkpoint().model_dump(mode="json")
    fields["schema_version"] = version
    with pytest.raises(ContractViolation) as caught:
        decode_checkpoint(json.dumps(fields))
    assert caught.value.code == code


def test_checkpoint_missing_wire_version_is_not_defaulted() -> None:
    fields = checkpoint().model_dump(mode="json")
    fields.pop("schema_version")
    with pytest.raises(ContractViolation):
        decode_checkpoint(json.dumps(fields))


@pytest.mark.parametrize(
    "changes",
    [
        {"next_step": "wait"},
        {"next_step": "retry"},
        {"wait_id": "wait-1", "wait_generation": 1},
        {"step_id": None},
        {"saved_fencing_token": True},
        {"input_version": "1"},
    ],
)
def test_incoherent_checkpoint_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        checkpoint(**changes)


def test_checkpoint_rejects_cross_case_artifact_and_duplicate_call_result() -> None:
    with pytest.raises(ValidationError):
        checkpoint(protocol_ref=artifact(case_id="case-other"))
    result = ToolResultReference(
        call_id="call-1",
        step_id="step-1",
        attempt_id="attempt-1",
        outcome="succeeded",
        artifact=artifact(),
    )
    with pytest.raises(ValidationError):
        checkpoint(tool_results=(result, result))


@pytest.mark.parametrize(
    "input_version, case_version, protocol, code",
    [
        (2, 3, "fake-transcript-v1", ErrorCode.CONFLICT),
        (1, 4, "fake-transcript-v1", ErrorCode.CONFLICT),
        (1, 3, "unknown-v2", ErrorCode.UNSUPPORTED_VERSION),
    ],
)
def test_resume_does_not_silently_change_inputs_or_decoder(
    input_version: int,
    case_version: int,
    protocol: str,
    code: ErrorCode,
) -> None:
    with pytest.raises(ContractViolation) as caught:
        assert_resume_compatible(
            checkpoint(),
            SCOPE,
            expected_input_version=input_version,
            expected_case_version=case_version,
            protocol_version=protocol,
        )
    assert caught.value.code == code


def test_resume_scope_and_naive_time_rejected() -> None:
    with pytest.raises(ContractViolation) as caught:
        assert_resume_compatible(
            checkpoint(),
            RunScope(tenant_id="merchant-1", case_id="case-1", run_id="other"),
            expected_input_version=1,
            expected_case_version=3,
            protocol_version="fake-transcript-v1",
        )
    assert caught.value.code == ErrorCode.FORBIDDEN
    with pytest.raises(ValidationError):
        budget(deadline=NOW.replace(tzinfo=None))


@pytest.mark.parametrize(
    "payload",
    [
        '{"x": 1, "x": 2}',
        '{"x": {"a": 1, "a": 2}}',
        '{"x": NaN}',
        '{"x": Infinity}',
        "[]",
        "null",
        '{"x":',
        '{"x":"' + "a" * 65_536 + '"}',
    ],
    ids=[
        "duplicate",
        "nested-duplicate",
        "nan",
        "infinity",
        "array",
        "null",
        "partial",
        "oversized",
    ],
)
def test_ambiguous_or_invalid_json_rejected(payload: str) -> None:
    with pytest.raises(ContractViolation) as caught:
        strict_json_object(payload)
    assert caught.value.code == ErrorCode.INVALID_INPUT


@pytest.mark.parametrize("payload", ['{"x": 1e999}', '{"x": -1e999}', chr(0xD800)])
def test_non_finite_numbers_and_non_utf8_input_are_rejected(payload: str) -> None:
    with pytest.raises(ContractViolation) as caught:
        strict_json_object(payload)
    assert caught.value.code == ErrorCode.INVALID_INPUT


@pytest.mark.parametrize("name", ["lookup_order", "lookup_tracking"])
def test_only_bound_lookup_arguments_accepted(name: str) -> None:
    request = ToolRequest(call_id="call-1", name=name, arguments_json="{}")
    parsed = validate_tool_request(
        request,
        stream_complete=True,
        allowed_tools=frozenset((name,)),
        budget=budget(),
        now=NOW,
    )
    assert isinstance(parsed, BoundLookupArguments)


def test_draft_is_a_validated_request_not_a_send_operation() -> None:
    request = ToolRequest(
        call_id="call-1",
        name="request_material_draft",
        arguments_json='{"questions":["check_delivery_location"]}',
    )
    parsed = validate_tool_request(
        request,
        stream_complete=True,
        allowed_tools=frozenset((request.name,)),
        budget=budget(),
        now=NOW,
    )
    assert isinstance(parsed, MaterialDraftArguments)
    assert parsed.questions == ("check_delivery_location",)


@pytest.mark.parametrize(
    "name, arguments, complete, allowed, code",
    [
        ("lookup_order", "{}", False, "lookup_order", ErrorCode.INVALID_INPUT),
        ("lookup_order", '{"tenant_id":"other"}', True, "lookup_order", ErrorCode.INVALID_INPUT),
        (
            "lookup_tracking",
            '{"order_id":"other"}',
            True,
            "lookup_tracking",
            ErrorCode.INVALID_INPUT,
        ),
        ("lookup_order", "{}", True, "lookup_tracking", ErrorCode.FORBIDDEN),
        ("refund", "{}", True, "refund", ErrorCode.FORBIDDEN),
        ("lookup_order", '{"x":', True, "lookup_order", ErrorCode.INVALID_INPUT),
        (
            "request_material_draft",
            '{"questions":[]}',
            True,
            "request_material_draft",
            ErrorCode.INVALID_INPUT,
        ),
    ],
)
def test_partial_unknown_or_scope_overriding_tool_never_becomes_validated_arguments(
    name: str,
    arguments: str,
    complete: bool,
    allowed: str,
    code: ErrorCode,
) -> None:
    request = ToolRequest(call_id="call-1", name=name, arguments_json=arguments)
    with pytest.raises(ContractViolation) as caught:
        validate_tool_request(
            request,
            stream_complete=complete,
            allowed_tools=frozenset((allowed,)),
            budget=budget(),
            now=NOW,
        )
    assert caught.value.code == code


@pytest.mark.parametrize("changes", [{"tool_calls": 0}, {"deadline": NOW}])
def test_no_tool_after_budget_or_deadline(changes: dict[str, object]) -> None:
    with pytest.raises(ContractViolation) as caught:
        validate_tool_request(
            ToolRequest(call_id="call-1", name="lookup_order", arguments_json="{}"),
            stream_complete=True,
            allowed_tools=frozenset(("lookup_order",)),
            budget=budget(**changes),
            now=NOW,
        )
    assert caught.value.code == ErrorCode.BUDGET_EXHAUSTED
