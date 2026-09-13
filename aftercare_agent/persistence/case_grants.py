"""Short-transaction persistence for subject-to-Case grants."""

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, cast

import psycopg
from pydantic import TypeAdapter, ValidationError

from aftercare_agent.auth.grants import CaseGrantRecord
from aftercare_agent.domain.common import ContractViolation, ErrorCode, Identifier

_IDENTIFIER: TypeAdapter[str] = TypeAdapter(Identifier)


def _identifier(value: str, field: str) -> str:
    try:
        return _IDENTIFIER.validate_python(value)
    except ValidationError as exc:
        raise ContractViolation(ErrorCode.INVALID_INPUT, f"invalid {field}") from exc


def _permissions(values: Iterable[str]) -> tuple[str, ...]:
    try:
        result = {_IDENTIFIER.validate_python(value) for value in values}
    except (ValidationError, TypeError) as exc:
        raise ContractViolation(ErrorCode.INVALID_INPUT, "invalid grant permissions") from exc
    if not result:
        raise ContractViolation(ErrorCode.INVALID_INPUT, "grant permissions cannot be empty")
    return tuple(sorted(result))


def _utc(value: datetime | None) -> datetime:
    return (value or datetime.now(UTC)).astimezone(UTC)


class CaseGrantRepository:
    """Resolve and mutate Case grants inside a caller-owned short transaction.

    ``resolve`` takes a row lock for the duration of the caller's transaction.
    API code therefore resolves and uses a grant in the same transaction, so a
    concurrent revoke cannot pass authorization and then affect the operation.
    Grant/revoke are trusted control-plane operations; there is intentionally no
    public HTTP route for them in this slice.
    """

    def _record(self, row: tuple[object, ...]) -> CaseGrantRecord:
        fields = (
            "tenant_id",
            "subject_id",
            "case_id",
            "permissions",
            "revision",
            "granted_by",
            "granted_at",
            "expires_at",
            "revoked_at",
            "revoked_by",
            "updated_at",
        )
        try:
            values = dict(zip(fields, row, strict=False))
            # psycopg decodes PostgreSQL text[] as a mutable list, while the
            # domain record intentionally exposes immutable permissions.
            if isinstance(values.get("permissions"), list):
                values["permissions"] = frozenset(cast(list[str], values["permissions"]))
            return CaseGrantRecord.model_validate(values)
        except ValidationError as exc:
            raise ContractViolation(ErrorCode.RETRYABLE, "invalid stored Case grant") from exc

    @staticmethod
    def _row(
        connection: psycopg.Connection[Any], tenant_id: str, subject_id: str, case_id: str
    ) -> tuple[object, ...] | None:
        return connection.execute(
            "SELECT tenant_id,subject_id,case_id,permissions,revision,granted_by,"
            "granted_at,expires_at,revoked_at,revoked_by,updated_at FROM aftercare_case_grants "
            "WHERE tenant_id=%s AND subject_id=%s AND case_id=%s FOR UPDATE",
            (tenant_id, subject_id, case_id),
        ).fetchone()

    def resolve(
        self,
        connection: psycopg.Connection[Any],
        tenant_id: str,
        subject_id: str,
        case_id: str,
        *,
        now: datetime | None = None,
    ) -> CaseGrantRecord | None:
        """Load one active grant, locking it until the caller commits."""
        tenant = _identifier(tenant_id, "tenant_id")
        subject = _identifier(subject_id, "subject_id")
        case = _identifier(case_id, "case_id")
        # Let PostgreSQL's clock decide validity in production.  Tests may
        # inject a fixed instant, but an application host clock must never
        # extend an expired grant after a skew or suspend/resume event.
        if now is None:
            row = connection.execute(
                "SELECT tenant_id,subject_id,case_id,permissions,revision,granted_by,"
                "granted_at,expires_at,revoked_at,revoked_by,updated_at "
                "FROM aftercare_case_grants WHERE tenant_id=%s AND subject_id=%s "
                "AND case_id=%s AND revoked_at IS NULL AND "
                "(expires_at IS NULL OR expires_at > clock_timestamp()) FOR UPDATE",
                (tenant, subject, case),
            ).fetchone()
        else:
            instant = _utc(now)
            row = connection.execute(
                "SELECT tenant_id,subject_id,case_id,permissions,revision,granted_by,"
                "granted_at,expires_at,revoked_at,revoked_by,updated_at "
                "FROM aftercare_case_grants WHERE tenant_id=%s AND subject_id=%s "
                "AND case_id=%s AND revoked_at IS NULL AND "
                "(expires_at IS NULL OR expires_at > %s) FOR UPDATE",
                (tenant, subject, case, instant),
            ).fetchone()
        if row is None:
            return None
        return self._record(row)

    def grant(
        self,
        connection: psycopg.Connection[Any],
        *,
        tenant_id: str,
        subject_id: str,
        case_id: str,
        permissions: Iterable[str],
        granted_by: str,
        expires_at: datetime | None = None,
        expected_revision: int | None = None,
    ) -> CaseGrantRecord:
        """Create or replace a grant with an optional optimistic revision check."""
        tenant = _identifier(tenant_id, "tenant_id")
        subject = _identifier(subject_id, "subject_id")
        case = _identifier(case_id, "case_id")
        actor = _identifier(granted_by, "granted_by")
        normalized = _permissions(permissions)
        if expires_at is not None and expires_at.tzinfo is None:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "expires_at must be timezone-aware")
        row = self._row(connection, tenant, subject, case)
        if row is None:
            if expected_revision is not None:
                raise ContractViolation(ErrorCode.CONFLICT, "Case grant revision mismatch")
            inserted = connection.execute(
                "INSERT INTO aftercare_case_grants "
                "(tenant_id,subject_id,case_id,permissions,revision,granted_by,expires_at) "
                "VALUES (%s,%s,%s,%s,1,%s,%s) RETURNING tenant_id,subject_id,case_id,"
                "permissions,revision,granted_by,granted_at,expires_at,revoked_at,"
                "revoked_by,updated_at",
                (tenant, subject, case, list(normalized), actor, expires_at),
            ).fetchone()
            if inserted is None:
                raise ContractViolation(ErrorCode.RETRYABLE, "Case grant insert outcome is unknown")
            return self._record(inserted)
        current = self._record(row)
        if expected_revision is None or expected_revision != current.revision:
            raise ContractViolation(ErrorCode.CONFLICT, "Case grant revision mismatch")
        updated = connection.execute(
            "UPDATE aftercare_case_grants SET permissions=%s,revision=revision+1,"
            "granted_by=%s,expires_at=%s,revoked_at=NULL,revoked_by=NULL,"
            "updated_at=clock_timestamp() "
            "WHERE tenant_id=%s AND subject_id=%s AND case_id=%s AND revision=%s "
            "RETURNING tenant_id,subject_id,case_id,permissions,revision,granted_by,"
            "granted_at,expires_at,revoked_at,revoked_by,updated_at",
            (list(normalized), actor, expires_at, tenant, subject, case, current.revision),
        ).fetchone()
        if updated is None:
            raise ContractViolation(ErrorCode.CONFLICT, "Case grant revision mismatch")
        return self._record(updated)

    def revoke(
        self,
        connection: psycopg.Connection[Any],
        *,
        tenant_id: str,
        subject_id: str,
        case_id: str,
        revoked_by: str,
        expected_revision: int,
    ) -> CaseGrantRecord:
        """Revoke a grant; the row remains for audit and revision checks."""
        tenant = _identifier(tenant_id, "tenant_id")
        subject = _identifier(subject_id, "subject_id")
        case = _identifier(case_id, "case_id")
        actor = _identifier(revoked_by, "revoked_by")
        if type(expected_revision) is not int or expected_revision < 1:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "expected_revision must be positive")
        row = self._row(connection, tenant, subject, case)
        if row is None:
            raise ContractViolation(ErrorCode.CONFLICT, "Case grant revision mismatch")
        current = self._record(row)
        if current.revision != expected_revision:
            raise ContractViolation(ErrorCode.CONFLICT, "Case grant revision mismatch")
        updated = connection.execute(
            "UPDATE aftercare_case_grants SET revision=revision+1,revoked_at=clock_timestamp(),"
            "revoked_by=%s,updated_at=clock_timestamp() "
            "WHERE tenant_id=%s AND subject_id=%s AND case_id=%s AND revision=%s "
            "RETURNING tenant_id,subject_id,case_id,permissions,revision,granted_by,"
            "granted_at,expires_at,revoked_at,revoked_by,updated_at",
            (actor, tenant, subject, case, expected_revision),
        ).fetchone()
        if updated is None:
            raise ContractViolation(ErrorCode.CONFLICT, "Case grant revision mismatch")
        return self._record(updated)
