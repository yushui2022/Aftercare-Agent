"""Offline fail-closed checks for the database CaseGrant boundary."""

from datetime import UTC, datetime
from typing import Any, cast

import pytest

from aftercare_agent.auth import AuthContext, CaseGrantRecord
from aftercare_agent.domain.common import ContractViolation, ErrorCode

NOW = datetime(2026, 9, 13, 12, tzinfo=UTC)


def _context(**changes: object) -> AuthContext:
    value: dict[str, object] = {
        "subject_id": "subject-1",
        "tenant_id": "tenant-1",
        "permissions": frozenset({"case:read", "review:read"}),
        "case_ids": frozenset({"case-1", "case-2"}),
    }
    value.update(changes)
    return AuthContext(**cast(Any, value))


def _grant(**changes: object) -> CaseGrantRecord:
    value: dict[str, object] = {
        "tenant_id": "tenant-1",
        "subject_id": "subject-1",
        "case_id": "case-1",
        "permissions": frozenset({"case:read"}),
        "revision": 1,
        "granted_by": "admin-1",
        "granted_at": NOW,
        "updated_at": NOW,
    }
    value.update(changes)
    return CaseGrantRecord(**value)  # type: ignore[arg-type]


def test_unresolved_non_synthetic_context_fails_closed() -> None:
    with pytest.raises(ContractViolation) as error:
        _context().require_case("case-1", "case:read")
    assert error.value.code is ErrorCode.FORBIDDEN


def test_token_case_ids_only_narrow_database_grant() -> None:
    context = _context()
    scoped = context.bind_case_grant(_grant(permissions=frozenset({"case:read", "review:read"})))
    scoped.require_case("case-1", "case:read")
    with pytest.raises(ContractViolation):
        context.bind_case_grant(_grant(case_id="case-3"))


def test_database_grant_cannot_elevate_token_permission() -> None:
    context = _context(permissions=frozenset({"case:read"}))
    with pytest.raises(ContractViolation) as error:
        context.bind_case_grant(_grant(permissions=frozenset({"review:decide"})))
    assert error.value.code is ErrorCode.FORBIDDEN


def test_grant_identity_must_match_context() -> None:
    with pytest.raises(ContractViolation):
        _context().bind_case_grant(_grant(subject_id="other-subject"))
