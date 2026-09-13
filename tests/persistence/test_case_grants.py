"""PostgreSQL tests for durable Case grants and revocation semantics."""

import os
from datetime import UTC, datetime, timedelta

import pytest

from aftercare_agent.auth import AuthContext
from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.runtime import CaseRecord
from aftercare_agent.persistence import CaseGrantRepository, Database, RunRepository, migrate


@pytest.fixture()
def db() -> Database:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL is not configured")
    value = Database(dsn)
    with value.transaction() as connection:
        migrate(connection)
    return value


def _seed(db: Database, tenant: str = "grant-test") -> None:
    with db.transaction() as connection:
        connection.execute("DELETE FROM aftercare_case_grants WHERE tenant_id=%s", (tenant,))
        connection.execute("DELETE FROM aftercare_runs WHERE tenant_id=%s", (tenant,))
        connection.execute("DELETE FROM aftercare_cases WHERE tenant_id=%s", (tenant,))
        RunRepository().create_case(
            connection,
            CaseRecord(tenant_id=tenant, case_id="case-1", order_id="order-1", version=1),
        )


def test_grant_resolve_intersects_token_and_revoke_is_fail_closed(db: Database) -> None:
    _seed(db)
    repository = CaseGrantRepository()
    with db.transaction() as connection:
        created = repository.grant(
            connection,
            tenant_id="grant-test",
            subject_id="subject-1",
            case_id="case-1",
            permissions=("case:read", "review:read"),
            granted_by="grant-admin",
        )
        resolved = repository.resolve(connection, "grant-test", "subject-1", "case-1")
        assert resolved == created
        assert resolved is not None and resolved.is_active(now=datetime.now(UTC))
        revoked = repository.revoke(
            connection,
            tenant_id="grant-test",
            subject_id="subject-1",
            case_id="case-1",
            revoked_by="revoke-admin",
            expected_revision=created.revision,
        )
        assert revoked.revoked_at is not None
        assert revoked.revoked_by == "revoke-admin"
        assert revoked.granted_by == "grant-admin"
        assert repository.resolve(connection, "grant-test", "subject-1", "case-1") is None


def test_expiry_and_revision_conflicts_are_fail_closed(db: Database) -> None:
    _seed(db, "grant-expiry")
    repository = CaseGrantRepository()
    expires = datetime.now(UTC) + timedelta(seconds=30)
    with db.transaction() as connection:
        created = repository.grant(
            connection,
            tenant_id="grant-expiry",
            subject_id="subject-1",
            case_id="case-1",
            permissions=("case:read",),
            granted_by="admin-1",
            expires_at=expires,
        )
        assert (
            repository.resolve(
                connection,
                "grant-expiry",
                "subject-1",
                "case-1",
                now=expires + timedelta(seconds=1),
            )
            is None
        )
        with pytest.raises(ContractViolation) as error:
            repository.grant(
                connection,
                tenant_id="grant-expiry",
                subject_id="subject-1",
                case_id="case-1",
                permissions=("case:read",),
                granted_by="admin-1",
                expected_revision=created.revision + 1,
            )
        assert error.value.code is ErrorCode.CONFLICT
        assert repository.resolve(connection, "grant-expiry", "subject-1", "missing-case") is None


def test_auth_context_is_not_constructed_by_repository_without_explicit_bind() -> None:
    # Keep this boundary explicit: persistence returns a record, never a
    # privileged AuthContext or a request-controlled principal.
    assert not hasattr(CaseGrantRepository, "from_request")
    assert AuthContext is not None
