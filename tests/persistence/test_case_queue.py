"""PostgreSQL tests for operator Case discovery and work-queue reads.

Every test allocates its own tenant id so repeated runs never collide and no
cleanup of a shared tenant is required.
"""

import os
from datetime import datetime
from uuid import uuid4

import pytest

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.runtime import CaseRecord
from aftercare_agent.persistence import (
    CaseGrantRepository,
    CaseRepository,
    Database,
    RunRepository,
    migrate,
)


@pytest.fixture()
def db() -> Database:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL is not configured")
    value = Database(dsn)
    with value.transaction() as connection:
        migrate(connection)
    return value


def _tenant(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def _seed_cases(db: Database, tenant: str, count: int) -> list[str]:
    case_ids = [f"case-{index:02d}" for index in range(count)]
    with db.transaction() as connection:
        runs = RunRepository()
        for case_id in case_ids:
            runs.create_case(
                connection,
                CaseRecord(
                    tenant_id=tenant,
                    case_id=case_id,
                    order_id=f"order-{case_id}",
                    version=1,
                ),
            )
    return case_ids


def _grant(db: Database, tenant: str, subject: str, case_id: str, *permissions: str) -> None:
    with db.transaction() as connection:
        CaseGrantRepository().grant(
            connection,
            tenant_id=tenant,
            subject_id=subject,
            case_id=case_id,
            permissions=permissions or ("case:read",),
            granted_by="admin",
        )


def test_list_accessible_is_grant_scoped_and_hides_revoked(db: Database) -> None:
    tenant = _tenant("queue-grants")
    _seed_cases(db, tenant, 3)
    _grant(db, tenant, "subject-1", "case-00")
    _grant(db, tenant, "subject-1", "case-01", "case:read", "review:read")
    _grant(db, tenant, "subject-2", "case-01")
    _grant(db, tenant, "subject-review-only", "case-02", "review:read")
    with db.transaction() as connection:
        revoked = CaseGrantRepository().grant(
            connection,
            tenant_id=tenant,
            subject_id="subject-1",
            case_id="case-02",
            permissions=("case:read",),
            granted_by="admin",
        )
        CaseGrantRepository().revoke(
            connection,
            tenant_id=tenant,
            subject_id="subject-1",
            case_id="case-02",
            revoked_by="admin",
            expected_revision=revoked.revision,
        )

    queue = CaseRepository()
    with db.transaction() as connection:
        first = queue.list_accessible(connection, tenant_id=tenant, subject_id="subject-1")
        second = queue.list_accessible(connection, tenant_id=tenant, subject_id="subject-2")
        review_only = queue.list_accessible(
            connection, tenant_id=tenant, subject_id="subject-review-only"
        )
        stranger = queue.list_accessible(connection, tenant_id=tenant, subject_id="subject-3")

    assert {entry.case_id for entry in first} == {"case-00", "case-01"}
    assert {entry.case_id for entry in second} == {"case-01"}
    assert review_only == []
    assert stranger == []
    granted = {entry.case_id: entry.permissions for entry in first}
    assert granted["case-00"] == frozenset({"case:read"})
    assert granted["case-01"] == frozenset({"case:read", "review:read"})


def test_list_accessible_treats_token_case_ids_as_upper_bound(db: Database) -> None:
    tenant = _tenant("queue-narrow")
    _seed_cases(db, tenant, 2)
    _grant(db, tenant, "subject-1", "case-00")
    _grant(db, tenant, "subject-1", "case-01")

    queue = CaseRepository()
    with db.transaction() as connection:
        narrowed = queue.list_accessible(
            connection,
            tenant_id=tenant,
            subject_id="subject-1",
            case_ids=frozenset({"case-00"}),
        )
        empty = queue.list_accessible(
            connection,
            tenant_id=tenant,
            subject_id="subject-1",
            case_ids=frozenset(),
        )

    assert [entry.case_id for entry in narrowed] == ["case-00"]
    assert empty == []


def test_expired_grant_is_not_discoverable(db: Database) -> None:
    tenant = _tenant("queue-expiry")
    _seed_cases(db, tenant, 1)
    _grant(db, tenant, "subject-1", "case-00")
    with db.transaction() as connection:
        # Move the grant into the past while keeping expires_at > granted_at,
        # so the CHECK constraint still holds and the expiry is unambiguous.
        connection.execute(
            "UPDATE aftercare_case_grants SET granted_at=clock_timestamp() - interval '1 hour', "
            "expires_at=clock_timestamp() - interval '30 minutes' "
            "WHERE tenant_id=%s AND subject_id=%s AND case_id=%s",
            (tenant, "subject-1", "case-00"),
        )

    with db.transaction() as connection:
        assert (
            CaseRepository().list_accessible(connection, tenant_id=tenant, subject_id="subject-1")
            == []
        )


def test_keyset_pagination_visits_every_row_once(db: Database) -> None:
    tenant = _tenant("queue-paging")
    expected = set(_seed_cases(db, tenant, 5))
    for case_id in expected:
        _grant(db, tenant, "subject-1", case_id)

    queue = CaseRepository()
    seen: list[str] = []
    cursor_created: datetime | None = None
    cursor_case: str | None = None
    for _ in range(10):
        with db.transaction() as connection:
            page = queue.list_accessible(
                connection,
                tenant_id=tenant,
                subject_id="subject-1",
                limit=2,
                after_created_at=cursor_created,
                after_case_id=cursor_case,
            )
        if not page:
            break
        seen.extend(entry.case_id for entry in page)
        cursor_created = page[-1].created_at
        cursor_case = page[-1].case_id
        if len(page) < 2:
            break

    assert len(seen) == len(set(seen)) == 5
    assert set(seen) == expected


def test_list_for_tenant_never_crosses_tenant_boundary(db: Database) -> None:
    tenant_a = _tenant("queue-tenant-a")
    tenant_b = _tenant("queue-tenant-b")
    _seed_cases(db, tenant_a, 2)
    _seed_cases(db, tenant_b, 1)

    queue = CaseRepository()
    with db.transaction() as connection:
        first = queue.list_for_tenant(connection, tenant_id=tenant_a)
        second = queue.list_for_tenant(connection, tenant_id=tenant_b)

    assert {entry.case_id for entry in first} == {"case-00", "case-01"}
    assert [entry.case_id for entry in second] == ["case-00"]


def test_invalid_page_parameters_are_rejected(db: Database) -> None:
    tenant = _tenant("queue-limits")
    _seed_cases(db, tenant, 1)
    queue = CaseRepository()
    with db.transaction() as connection:
        with pytest.raises(ContractViolation) as too_large:
            queue.list_for_tenant(connection, tenant_id=tenant, limit=10_000)
        assert too_large.value.code is ErrorCode.INVALID_INPUT
        with pytest.raises(ContractViolation) as half_cursor:
            queue.list_for_tenant(connection, tenant_id=tenant, after_case_id="case-00")
        assert half_cursor.value.code is ErrorCode.INVALID_INPUT
