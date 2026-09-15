"""Offline checks for Case-grant delegation policy and its request models.

No database is used: these are the rules that decide what an administrator is
allowed to ask for, before anything reaches the ACL table.
"""

import pytest
from pydantic import ValidationError

from aftercare_agent.api.app import CaseGrantInput, CaseGrantRevokeInput
from aftercare_agent.auth import synthetic_context
from aftercare_agent.auth.grants import (
    GRANT_ADMIN_PERMISSION,
    GRANT_READ_PERMISSION,
    GRANTABLE_CASE_PERMISSIONS,
    authorize_case_grant,
)
from aftercare_agent.domain.common import ContractViolation, ErrorCode


def test_tenant_level_scopes_are_not_delegable_through_a_case_grant() -> None:
    """One Case row must never widen authority beyond that Case."""
    assert "case:create" not in GRANTABLE_CASE_PERMISSIONS
    assert GRANT_READ_PERMISSION not in GRANTABLE_CASE_PERMISSIONS
    assert GRANT_ADMIN_PERMISSION not in GRANTABLE_CASE_PERMISSIONS


def test_a_valid_delegation_returns_its_canonical_permissions() -> None:
    actor = frozenset({"grant:admin", "case:read", "review:decide"})
    granted = authorize_case_grant(
        actor_permissions=actor, requested=frozenset({"review:decide", "case:read"})
    )
    assert granted == frozenset({"case:read", "review:decide"})
    assert isinstance(granted, frozenset)


def test_an_empty_delegation_is_rejected() -> None:
    with pytest.raises(ContractViolation) as error:
        authorize_case_grant(actor_permissions=frozenset({"grant:admin"}), requested=frozenset())
    assert error.value.code is ErrorCode.INVALID_INPUT


@pytest.mark.parametrize(
    "requested",
    [{"case:create"}, {"grant:admin"}, {"review:readd"}, {"*"}, {"CASE:READ"}, {"case:read "}],
)
def test_a_permission_outside_the_closed_set_is_rejected(requested: set[str]) -> None:
    """The actor holds whatever it asks for; only the vocabulary may reject it."""
    with pytest.raises(ContractViolation) as error:
        authorize_case_grant(
            actor_permissions=frozenset({"grant:admin", *requested}),
            requested=frozenset(requested),
        )
    assert error.value.code is ErrorCode.INVALID_INPUT


def test_an_actor_cannot_delegate_a_permission_it_does_not_hold() -> None:
    with pytest.raises(ContractViolation) as error:
        authorize_case_grant(
            actor_permissions=frozenset({"grant:admin", "case:read"}),
            requested=frozenset({"approval:decide"}),
        )
    assert error.value.code is ErrorCode.FORBIDDEN


def test_the_administration_scope_alone_cannot_widen_a_case() -> None:
    """Holding ``grant:admin`` is not the same as holding the permission."""
    with pytest.raises(ContractViolation) as error:
        authorize_case_grant(
            actor_permissions=frozenset({GRANT_ADMIN_PERMISSION, GRANT_READ_PERMISSION}),
            requested=frozenset({"case:read"}),
        )
    assert error.value.code is ErrorCode.FORBIDDEN


@pytest.mark.parametrize("field", ["tenant_id", "case_id", "granted_by", "revoked_by", "revision"])
def test_the_grant_body_cannot_carry_authority_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        CaseGrantInput.model_validate(
            {"subject_id": "operator-1", "permissions": ["case:read"], field: "attacker"}
        )


def test_the_grant_body_requires_a_bounded_permission_set() -> None:
    with pytest.raises(ValidationError):
        CaseGrantInput(subject_id="operator-1", permissions=[])
    with pytest.raises(ValidationError):
        CaseGrantInput(subject_id="operator-1", permissions=["case:read"] * 17)


@pytest.mark.parametrize("subject_id", ["", "has space", "-leading", "a" * 161])
def test_the_grant_body_rejects_non_identifier_subjects(subject_id: str) -> None:
    with pytest.raises(ValidationError):
        CaseGrantInput(subject_id=subject_id, permissions=["case:read"])


def test_an_expected_revision_must_be_positive_when_present() -> None:
    assert (
        CaseGrantInput(subject_id="operator-1", permissions=["case:read"]).expected_revision is None
    )
    with pytest.raises(ValidationError):
        CaseGrantInput(subject_id="operator-1", permissions=["case:read"], expected_revision=0)


def test_revocation_requires_a_positive_revision() -> None:
    with pytest.raises(ValidationError):
        CaseGrantRevokeInput(expected_revision=0)
    assert CaseGrantRevokeInput(expected_revision=3).expected_revision == 3


def test_the_local_test_identity_can_exercise_the_administration_surface() -> None:
    context = synthetic_context(enabled=True, tenant_id="tenant-1", subject_id="ops")
    assert {GRANT_READ_PERMISSION, GRANT_ADMIN_PERMISSION} <= context.permissions


def test_the_local_test_identity_still_cannot_escalate_through_a_grant() -> None:
    context = synthetic_context(enabled=True, tenant_id="tenant-1", subject_id="ops")
    with pytest.raises(ContractViolation) as error:
        authorize_case_grant(
            actor_permissions=context.permissions, requested=frozenset({"case:create"})
        )
    assert error.value.code is ErrorCode.INVALID_INPUT
