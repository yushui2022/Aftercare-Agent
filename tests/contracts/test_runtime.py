"""Pure runtime contract examples; these do not exercise database locking."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from aftercare_agent.domain.common import CaseScope, ContractViolation, ErrorCode
from aftercare_agent.domain.runtime import (
    AdmissionKey,
    AttemptRecord,
    CaseGrant,
    CaseRecord,
    ExecutionClaim,
    Failure,
    OpenCaseInput,
    Permission,
    RunRecord,
    RunState,
    SessionRecord,
    StepRecord,
    admission_digest,
    assert_case_version,
    assert_execution_right,
    authorize_case,
    check_admission_replay,
    next_fencing_token,
    validate_hierarchy,
    validate_transition,
)

NOW = datetime(2026, 9, 7, 12, tzinfo=UTC)


def _run(**changes: object) -> RunRecord:
    values: dict[str, object] = {
        "tenant_id": "tenant-1",
        "case_id": "case-1",
        "run_id": "run-1",
        "definition_version": "investigation-v1",
        "input_version": 1,
    }
    values.update(changes)
    return RunRecord.model_validate(values)


def _running(**changes: object) -> RunRecord:
    values: dict[str, object] = {
        "state": "RUNNING",
        "fencing_token": 3,
        "lease_owner": "worker-1",
        "lease_until": NOW + timedelta(seconds=30),
    }
    values.update(changes)
    return _run(**values)


def _claim(**changes: object) -> ExecutionClaim:
    values: dict[str, object] = {
        "tenant_id": "tenant-1",
        "case_id": "case-1",
        "run_id": "run-1",
        "owner": "worker-1",
        "fencing_token": 3,
    }
    values.update(changes)
    return ExecutionClaim.model_validate(values)


def _case(**changes: object) -> CaseRecord:
    values: dict[str, object] = {
        "tenant_id": "tenant-1",
        "case_id": "case-1",
        "order_id": "order-1",
        "version": 1,
    }
    values.update(changes)
    return CaseRecord.model_validate(values)


def test_authorization_uses_host_grant_not_identity_shaped_payload() -> None:
    grant = CaseGrant(
        subject_id="support-1",
        tenant_id="tenant-1",
        case_ids=("case-1",),
        permissions=("case:read",),
    )
    scope = CaseScope(tenant_id="tenant-1", case_id="case-1")
    authorize_case(grant, scope, "case:read")
    denied_examples: tuple[tuple[CaseScope, Permission], ...] = (
        (CaseScope(tenant_id="tenant-2", case_id="case-1"), "case:read"),
        (CaseScope(tenant_id="tenant-1", case_id="case-2"), "case:read"),
        (scope, "case:write"),
    )
    for denied_scope, denied_permission in denied_examples:
        with pytest.raises(ContractViolation) as raised:
            authorize_case(grant, denied_scope, denied_permission)
        assert raised.value.code == ErrorCode.FORBIDDEN


@pytest.mark.parametrize("injected", ["tenant_id", "case_id", "subject_id", "permissions"])
def test_open_case_body_rejects_injected_identity(injected: str) -> None:
    body: dict[str, object] = {
        "order_id": "order-1",
        "channel": "buyer",
        "message_ref": "message-1",
        "message_sha256": "a" * 64,
        injected: "attacker-chosen",
    }
    with pytest.raises(ValidationError):
        OpenCaseInput.model_validate(body)


def test_admission_digest_canonicalizes_keys_and_materializes_defaults() -> None:
    key = AdmissionKey(tenant_id="tenant-1", idempotency_key="request-1")
    first = OpenCaseInput.model_validate_json(
        '{"order_id":"order-1","channel":"buyer","message_ref":"message-1",'
        '"message_sha256":"' + "a" * 64 + '"}'
    )
    reordered = OpenCaseInput.model_validate_json(
        '{ "goal":"investigate_non_receipt", "message_ref":"message-1", '
        '"channel":"buyer", "order_id":"order-1", "schema_version":1, '
        '"message_sha256":"' + "a" * 64 + '" }'
    )
    digest = admission_digest(key, first)
    assert len(digest) == 64
    assert digest == admission_digest(key, reordered)
    check_admission_replay(digest, admission_digest(key, reordered))
    # The storage key defines request identity; the digest describes semantic input.
    another_key = AdmissionKey(tenant_id="tenant-1", idempotency_key="request-2")
    assert digest == admission_digest(another_key, first)
    another_tenant = AdmissionKey(tenant_id="tenant-2", idempotency_key="request-1")
    assert digest != admission_digest(another_tenant, first)


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("order_id", "order-2"),
        ("message_ref", "message-2"),
        ("channel", "support"),
        ("message_sha256", "b" * 64),
    ],
)
def test_same_admission_key_with_changed_semantics_conflicts(field: str, changed: str) -> None:
    key = AdmissionKey(tenant_id="tenant-1", idempotency_key="request-1")
    body = OpenCaseInput(
        order_id="order-1", channel="buyer", message_ref="message-1", message_sha256="a" * 64
    )
    altered = OpenCaseInput.model_validate({**body.model_dump(), field: changed})
    with pytest.raises(ContractViolation) as raised:
        check_admission_replay(admission_digest(key, body), admission_digest(key, altered))
    assert raised.value.code == ErrorCode.CONFLICT


def test_hierarchy_accepts_same_scope_and_exact_ancestry() -> None:
    case = _case()
    run = _run(session_id="session-1")
    session = SessionRecord(
        tenant_id="tenant-1", case_id="case-1", session_id="session-1", channel="buyer"
    )
    step = StepRecord(
        tenant_id="tenant-1",
        case_id="case-1",
        run_id="run-1",
        step_id="step-1",
        kind="tool",
        input_version=1,
    )
    attempt = AttemptRecord(
        tenant_id="tenant-1",
        case_id="case-1",
        run_id="run-1",
        step_id="step-1",
        attempt_id="attempt-1",
        attempt_number=1,
        fencing_token=2,
        status="SUCCEEDED",
    )
    validate_hierarchy(case, run, session=session, step=step, attempt=attempt)
    validate_hierarchy(case, _run())


@pytest.mark.parametrize("field", ["tenant_id", "case_id"])
def test_hierarchy_rejects_run_scope_mismatch(field: str) -> None:
    with pytest.raises(ContractViolation) as raised:
        validate_hierarchy(_case(), _run(**{field: "another"}))
    assert raised.value.code == ErrorCode.FORBIDDEN


@pytest.mark.parametrize(
    "change", [{"tenant_id": "tenant-2"}, {"case_id": "case-2"}, {"session_id": "session-2"}]
)
def test_hierarchy_rejects_wrong_session(change: dict[str, object]) -> None:
    values: dict[str, object] = {
        "tenant_id": "tenant-1",
        "case_id": "case-1",
        "session_id": "session-1",
        "channel": "buyer",
    }
    session = SessionRecord.model_validate({**values, **change})
    with pytest.raises(ContractViolation) as raised:
        validate_hierarchy(_case(), _run(session_id="session-1"), session=session)
    assert raised.value.code == ErrorCode.FORBIDDEN


def test_hierarchy_does_not_infer_missing_or_unbound_session() -> None:
    with pytest.raises(ContractViolation):
        validate_hierarchy(_case(), _run(session_id="session-1"))
    session = SessionRecord(
        tenant_id="tenant-1", case_id="case-1", session_id="session-1", channel="buyer"
    )
    with pytest.raises(ContractViolation):
        validate_hierarchy(_case(), _run(), session=session)


@pytest.mark.parametrize("field", ["tenant_id", "case_id", "run_id"])
def test_hierarchy_rejects_step_mismatch(field: str) -> None:
    values: dict[str, object] = {
        "tenant_id": "tenant-1",
        "case_id": "case-1",
        "run_id": "run-1",
        "step_id": "step-1",
        "kind": "tool",
        "input_version": 1,
    }
    step = StepRecord.model_validate({**values, field: "another"})
    with pytest.raises(ContractViolation) as raised:
        validate_hierarchy(_case(), _run(), step=step)
    assert raised.value.code == ErrorCode.FORBIDDEN


@pytest.mark.parametrize("field", ["tenant_id", "case_id", "run_id", "step_id"])
def test_hierarchy_rejects_attempt_mismatch(field: str) -> None:
    step = StepRecord(
        tenant_id="tenant-1",
        case_id="case-1",
        run_id="run-1",
        step_id="step-1",
        kind="tool",
        input_version=1,
    )
    values: dict[str, object] = {
        "tenant_id": "tenant-1",
        "case_id": "case-1",
        "run_id": "run-1",
        "step_id": "step-1",
        "attempt_id": "attempt-1",
        "attempt_number": 1,
        "fencing_token": 3,
        "status": "STARTED",
    }
    attempt = AttemptRecord.model_validate({**values, field: "another"})
    with pytest.raises(ContractViolation) as raised:
        validate_hierarchy(_case(), _run(), step=step, attempt=attempt)
    assert raised.value.code == ErrorCode.FORBIDDEN
    with pytest.raises(ContractViolation):
        validate_hierarchy(_case(), _run(), attempt=attempt)


def test_execution_right_is_valid_strictly_before_deadline() -> None:
    assert_execution_right(_running(), _claim(), db_now=NOW)
    assert_execution_right(
        _running(), _claim(), db_now=NOW.astimezone(timezone(timedelta(hours=8)))
    )
    assert_execution_right(
        _running(), _claim(), db_now=NOW + timedelta(seconds=30, microseconds=-1)
    )


@pytest.mark.parametrize("seconds", [30, 31])
def test_expired_holder_cannot_write_even_without_a_takeover(seconds: int) -> None:
    with pytest.raises(ContractViolation) as raised:
        assert_execution_right(_running(), _claim(), db_now=NOW + timedelta(seconds=seconds))
    assert raised.value.code == ErrorCode.LEASE_LOST


@pytest.mark.parametrize(
    "change", [{"owner": "worker-2"}, {"fencing_token": 2}, {"fencing_token": 4}]
)
def test_execution_right_rejects_owner_and_fence_mismatch(change: dict[str, object]) -> None:
    with pytest.raises(ContractViolation) as raised:
        assert_execution_right(_running(), _claim(**change), db_now=NOW)
    assert raised.value.code == ErrorCode.LEASE_LOST


@pytest.mark.parametrize("field", ["tenant_id", "case_id", "run_id"])
def test_execution_claim_must_belong_to_the_same_run(field: str) -> None:
    with pytest.raises(ContractViolation) as raised:
        assert_execution_right(_running(), _claim(**{field: "another"}), db_now=NOW)
    assert raised.value.code == ErrorCode.FORBIDDEN


def test_non_running_record_and_naive_clock_do_not_authorize_execution() -> None:
    with pytest.raises(ContractViolation) as raised:
        assert_execution_right(_run(fencing_token=3), _claim(), db_now=NOW)
    assert raised.value.code == ErrorCode.LEASE_LOST
    with pytest.raises(ValueError, match="timezone-aware"):
        assert_execution_right(_running(), _claim(), db_now=NOW.replace(tzinfo=None))


def test_claim_increments_token_and_does_not_mutate_authoritative_record() -> None:
    ready = _run(fencing_token=3)
    assert next_fencing_token(ready, db_now=NOW) == 4
    assert ready.fencing_token == 3
    expired = _running(lease_until=NOW)
    assert next_fencing_token(expired, db_now=NOW) == 4
    with pytest.raises(ContractViolation):
        next_fencing_token(_running(), db_now=NOW)
    with pytest.raises(ContractViolation, match="exhausted"):
        next_fencing_token(_run(fencing_token=2**63 - 1), db_now=NOW)


@pytest.mark.parametrize(
    "changes",
    [
        {"state": "WAITING_INPUT", "wait_id": "wait-1", "wait_generation": 1},
        {"state": "WAITING_APPROVAL", "wait_id": "wait-1", "wait_generation": 1},
        {"state": "RETRY_AT", "available_at": NOW - timedelta(seconds=1)},
        {"state": "REVIEW"},
        {"state": "COMPLETED"},
        {"state": "CANCELLED"},
    ],
)
def test_non_ready_states_need_their_own_transition_before_claim(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ContractViolation) as raised:
        next_fencing_token(_run(**changes), db_now=NOW)
    assert raised.value.code == ErrorCode.CONFLICT


@pytest.mark.parametrize(
    "changes",
    [
        {"state": "RUNNING"},
        {"state": "RUNNING", "lease_owner": "worker-1", "lease_until": NOW, "fencing_token": 0},
        {"lease_owner": "worker-1"},
        {"lease_until": NOW},
        {"state": "WAITING_INPUT"},
        {"state": "WAITING_INPUT", "wait_id": "wait-1"},
        {"state": "WAITING_APPROVAL", "wait_generation": 1},
        {"wait_id": "wait-1", "wait_generation": 1},
        {"state": "RETRY_AT"},
        {"available_at": NOW},
        {"predecessor_run_id": "run-1"},
        {"input_version": True},
        {"fencing_token": -1},
        {"fencing_token": 2**63},
    ],
)
def test_run_state_combinations_and_counters_are_validated(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _run(**changes)


@pytest.mark.parametrize(
    ("previous", "target"),
    [
        ("READY", "RUNNING"),
        ("RUNNING", "WAITING_INPUT"),
        ("WAITING_INPUT", "READY"),
        ("WAITING_APPROVAL", "CANCELLED"),
        ("RETRY_AT", "READY"),
        ("REVIEW", "READY"),
        ("RUNNING", "COMPLETED"),
    ],
)
def test_legal_run_edges(previous: RunState, target: RunState) -> None:
    validate_transition(previous, target)


@pytest.mark.parametrize(
    ("previous", "target"),
    [
        ("COMPLETED", "READY"),
        ("CANCELLED", "RUNNING"),
        ("READY", "COMPLETED"),
        ("WAITING_INPUT", "RUNNING"),
        ("RUNNING", "RUNNING"),
    ],
)
def test_terminal_runs_and_skipped_edges_cannot_be_revived(
    previous: RunState, target: RunState
) -> None:
    with pytest.raises(ContractViolation) as raised:
        validate_transition(previous, target)
    assert raised.value.code == ErrorCode.CONFLICT


def test_case_business_version_is_not_a_fence_or_boolean() -> None:
    assert_case_version(_case(version=3), 3)
    for wrong in (1, 4, True):
        with pytest.raises(ContractViolation) as raised:
            assert_case_version(_case(version=3), wrong)
        assert raised.value.code == ErrorCode.CONFLICT


@pytest.mark.parametrize("code", [ErrorCode.RATE_LIMITED, ErrorCode.RETRYABLE])
def test_timed_retry_hint_has_nonnegative_integer_seconds(code: ErrorCode) -> None:
    failure = Failure(code=code, message="retry later", retry_after_seconds=0)
    assert failure.retry_after_seconds == 0
    for wrong in (-1, True, 0.5):
        with pytest.raises(ValidationError):
            Failure.model_validate(
                {"code": code, "message": "retry later", "retry_after_seconds": wrong}
            )


@pytest.mark.parametrize(
    "code",
    [code for code in ErrorCode if code not in (ErrorCode.RATE_LIMITED, ErrorCode.RETRYABLE)],
)
def test_correction_and_unknown_outcome_errors_cannot_request_timed_replay(code: ErrorCode) -> None:
    assert Failure(code=code, message="requires handling").retry_after_seconds is None
    with pytest.raises(ValidationError):
        Failure(code=code, message="requires handling", retry_after_seconds=1)
