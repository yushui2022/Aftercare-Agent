"""Harden and verify the PostgreSQL row-level security boundary."""

from __future__ import annotations

import argparse
import json
import re
import uuid
from collections.abc import Sequence
from typing import Any

import psycopg
from psycopg import sql

from aftercare_agent.config import environment_secret

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_POLICY_NAME = "aftercare_tenant_context"


def normalize_image_digest(value: str) -> str:
    """Accept only the immutable image identity used by release evidence."""

    if not _SHA256.fullmatch(value):
        raise ValueError("image digest must be sha256: followed by 64 lowercase hex characters")
    return value


def image_digest_from_reference(value: str) -> str:
    """Extract the digest from the exact image reference used by a workload."""

    if "@" not in value:
        raise ValueError("image reference must contain an immutable @sha256 digest")
    name, digest = value.rsplit("@", 1)
    if not name.strip():
        raise ValueError("image reference must include an image name")
    return normalize_image_digest(digest)


def _tenant_tables(connection: psycopg.Connection[Any]) -> tuple[str, ...]:
    rows = connection.execute(
        """
        SELECT DISTINCT c.relname
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        JOIN pg_attribute AS a ON a.attrelid = c.oid
        WHERE n.nspname = 'public'
          AND c.relkind IN ('r', 'p')
          AND c.relname LIKE 'aftercare_%'
          AND a.attname = 'tenant_id'
          AND NOT a.attisdropped
        ORDER BY c.relname
        """
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _role_safety(connection: psycopg.Connection[Any], role_name: str) -> list[str]:
    rows = connection.execute(
        """
        WITH RECURSIVE role_tree(oid) AS (
            SELECT oid FROM pg_roles WHERE rolname = %s
            UNION
            SELECT membership.roleid
            FROM pg_auth_members AS membership
            JOIN role_tree AS child ON child.oid = membership.member
        )
        SELECT roles.rolname
        FROM role_tree
        JOIN pg_roles AS roles ON roles.oid = role_tree.oid
        WHERE roles.rolsuper OR roles.rolbypassrls
        ORDER BY roles.rolname
        """,
        (role_name,),
    ).fetchall()
    if not connection.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role_name,)).fetchone():
        raise RuntimeError(f"runtime role does not exist: {role_name}")
    return [str(row[0]) for row in rows]


def _policy_state(connection: psycopg.Connection[Any], table_name: str) -> tuple[bool, bool, bool]:
    row = connection.execute(
        """
        SELECT c.relrowsecurity,
               c.relforcerowsecurity,
               EXISTS (
                   SELECT 1
                   FROM pg_policy AS policy
                   WHERE policy.polrelid = c.oid
                     AND policy.polname = %s
                     AND pg_get_expr(policy.polqual, policy.polrelid) LIKE '%%aftercare.tenant_id%%'
                     AND pg_get_expr(policy.polwithcheck, policy.polrelid)
                         LIKE '%%aftercare.tenant_id%%'
               )
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = %s
        """,
        (_POLICY_NAME, table_name),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"tenant table disappeared while checking RLS: {table_name}")
    return bool(row[0]), bool(row[1]), bool(row[2])


def _assert_hardened(connection: psycopg.Connection[Any], tables: tuple[str, ...]) -> None:
    if not tables:
        raise RuntimeError("no public Aftercare table with tenant_id was found")
    failures = [table for table in tables if _policy_state(connection, table) != (True, True, True)]
    if failures:
        joined = ", ".join(failures)
        raise RuntimeError(f"tenant tables are not fully hardened for RLS: {joined}")


def _harden(dsn: str, *, runtime_role: str, image_digest: str) -> dict[str, Any]:
    with psycopg.connect(dsn) as connection:
        with connection.transaction():
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ("aftercare:rls",)
            )
            tables = _tenant_tables(connection)
            unsafe_roles = _role_safety(connection, runtime_role)
            if unsafe_roles:
                joined = ", ".join(unsafe_roles)
                raise RuntimeError(
                    f"runtime role can reach a superuser or BYPASSRLS role: {joined}"
                )
            for table in tables:
                enabled, forced, has_policy = _policy_state(connection, table)
                if not has_policy:
                    raise RuntimeError(f"tenant table has no approved policy: {table}")
                identifier = sql.Identifier("public", table)
                if not enabled:
                    connection.execute(
                        sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(identifier)
                    )
                if not forced:
                    connection.execute(
                        sql.SQL("ALTER TABLE {} FORCE ROW LEVEL SECURITY").format(identifier)
                    )
            _assert_hardened(connection, tables)
            report = {
                "schema_version": 1,
                "verifier": "aftercare-rls",
                "decision": "pass",
                "image_digest": image_digest,
                "runtime_role": runtime_role,
                "tenant_table_count": len(tables),
                "tenant_tables": list(tables),
                "forced": True,
            }
    return report


def _set_tenant(connection: psycopg.Connection[Any], tenant_id: str) -> None:
    connection.execute("SELECT set_config('aftercare.tenant_id', %s, true)", (tenant_id,))
    connection.execute("SELECT set_config('aftercare.subject_id', '', true)")


def _case_count(connection: psycopg.Connection[Any], case_ids: tuple[str, ...]) -> int:
    row = connection.execute(
        "SELECT count(*) FROM public.aftercare_cases WHERE case_id = ANY(%s)",
        (list(case_ids),),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _verify_probe(connection: psycopg.Connection[Any]) -> dict[str, Any]:
    tables = _tenant_tables(connection)
    _assert_hardened(connection, tables)
    current_role = connection.execute("SELECT current_user").fetchone()
    assert current_role is not None
    role_state = connection.execute(
        "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"
    ).fetchone()
    if role_state is None:
        raise RuntimeError("runtime verification role disappeared")
    if role_state[0]:
        raise RuntimeError("runtime verification connection has superuser or BYPASSRLS privileges")

    marker = uuid.uuid4().hex
    tenant_a, tenant_b = f"rls-probe-{marker}-a", f"rls-probe-{marker}-b"
    case_a, case_b = f"rls-probe-{marker}-case-a", f"rls-probe-{marker}-case-b"
    case_ids = (case_a, case_b)
    connection.execute("BEGIN")
    try:
        _set_tenant(connection, "")
        if _case_count(connection, case_ids) != 0:
            raise RuntimeError("empty tenant context exposed rows")
        _set_tenant(connection, tenant_a)
        connection.execute(
            "INSERT INTO public.aftercare_cases(tenant_id, case_id, order_id, version) "
            "VALUES (%s, %s, %s, 1)",
            (tenant_a, case_a, f"{case_a}-order"),
        )
        _set_tenant(connection, tenant_b)
        connection.execute(
            "INSERT INTO public.aftercare_cases(tenant_id, case_id, order_id, version) "
            "VALUES (%s, %s, %s, 1)",
            (tenant_b, case_b, f"{case_b}-order"),
        )
        if _case_count(connection, case_ids) != 1:
            raise RuntimeError("tenant context did not isolate its own row")
        connection.execute("SAVEPOINT rls_cross_insert")
        try:
            connection.execute(
                "INSERT INTO public.aftercare_cases(tenant_id, case_id, order_id, version) "
                "VALUES (%s, %s, %s, 1)",
                (tenant_a, f"{case_a}-cross-write", f"{case_a}-cross-order"),
            )
        except psycopg.errors.InsufficientPrivilege:
            connection.execute("ROLLBACK TO SAVEPOINT rls_cross_insert")
        else:
            connection.execute("ROLLBACK TO SAVEPOINT rls_cross_insert")
            raise RuntimeError("cross-tenant insert was accepted")
        finally:
            connection.execute("RELEASE SAVEPOINT rls_cross_insert")
        updated = connection.execute(
            "UPDATE public.aftercare_cases SET status = 'IN_REVIEW' "
            "WHERE tenant_id = %s AND case_id = %s",
            (tenant_a, case_a),
        ).rowcount
        deleted = connection.execute(
            "DELETE FROM public.aftercare_cases WHERE tenant_id = %s AND case_id = %s",
            (tenant_a, case_a),
        ).rowcount
        if updated != 0 or deleted != 0:
            raise RuntimeError("cross-tenant update or delete was accepted")
        return {
            "schema_version": 1,
            "verifier": "aftercare-rls",
            "decision": "pass",
            "runtime_role": str(current_role[0]),
            "tenant_table_count": len(tables),
            "empty_context_rows": 0,
            "cross_tenant_write": "rejected",
            "cross_tenant_update": "hidden",
            "cross_tenant_delete": "hidden",
        }
    finally:
        connection.execute("ROLLBACK")


def _verify(dsn: str, *, runtime_role: str, image_digest: str) -> dict[str, Any]:
    with psycopg.connect(dsn, autocommit=True) as connection:
        current = connection.execute("SELECT current_user").fetchone()
        if current is None or str(current[0]) != runtime_role:
            connected_as = current[0] if current else "unknown"
            raise RuntimeError(f"runtime DSN connected as {connected_as}, expected {runtime_role}")
        report = _verify_probe(connection)
    report["image_digest"] = image_digest
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("harden", "verify"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--runtime-role", required=True)
        subparser.add_argument("--image", required=True, help="the exact immutable image reference")
    args = parser.parse_args(argv)
    dsn = environment_secret("DATABASE_URL") or ""
    if not dsn:
        parser.error("DATABASE_URL is required")
    try:
        digest = image_digest_from_reference(args.image)
        if args.command == "harden":
            report = _harden(dsn, runtime_role=args.runtime_role, image_digest=digest)
        else:
            report = _verify(dsn, runtime_role=args.runtime_role, image_digest=digest)
    except (RuntimeError, ValueError, psycopg.Error) as exc:
        parser.error(str(exc))
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
