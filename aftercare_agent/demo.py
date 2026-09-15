"""One-command synthetic Aftercare demonstration.

The demo uses the deterministic :class:`SyntheticAftercareFlow`, a real
PostgreSQL database, and a fake provider. It never reads a model key, calls a
network connector, or performs a refund. The output is a compact JSON report.
"""

import argparse
import json
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import psycopg

from aftercare_agent.domain.common import ContractViolation
from aftercare_agent.domain.investigation import DeliveryStatus
from aftercare_agent.domain.recommendations import assessment_digest
from aftercare_agent.persistence import (
    ApprovalRepository,
    Database,
    EventRepository,
    RunRepository,
    migrate,
)
from aftercare_agent.runtime import SyntheticAftercareFlow, SyntheticCase


def build_parser() -> argparse.ArgumentParser:
    """Build the parser without reading environment state."""
    parser = argparse.ArgumentParser(
        prog="aftercare-demo",
        description=(
            "Run the deterministic synthetic Aftercare vertical slice against PostgreSQL. "
            "No model, connector, sandbox, or real business action is called."
        ),
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="PostgreSQL DSN; defaults to DATABASE_URL (never printed)",
    )
    parser.add_argument(
        "--tenant-id",
        default="demo-tenant",
        help="Synthetic tenant identifier (default: demo-tenant)",
    )
    parser.add_argument(
        "--case-id", default=None, help="Case identifier; defaults to a fresh demo-<random> value"
    )
    parser.add_argument(
        "--order-id",
        default=None,
        help="Order identifier; defaults to the case identifier with -order",
    )
    parser.add_argument(
        "--message", default="buyer reports non-receipt", help="Synthetic customer message"
    )
    return parser


def _new_case(*, tenant_id: str, case_id: str | None, order_id: str | None) -> SyntheticCase:
    selected_case = case_id or f"demo-{uuid4().hex[:12]}"
    selected_order = order_id or f"{selected_case}-order"
    # Child IDs are tenant-scoped in the persistence schema. Deriving every
    # identity from the case keeps repeated demos isolated without deleting data.
    return SyntheticCase(
        tenant_id=tenant_id,
        case_id=selected_case,
        order_id=selected_order,
        session_id=f"{selected_case}-session",
        run_id=f"{selected_case}-run",
        wait_id=f"{selected_case}-input-wait",
        approval_run_id=f"{selected_case}-approval-run",
        approval_wait_id=f"{selected_case}-approval-wait",
        approval_id=f"{selected_case}-approval",
        action_id=f"{selected_case}-refund-action",
    )


def _existing_run(database: Database, case: SyntheticCase) -> bool:
    with database.transaction() as connection:
        return RunRepository().get(connection, case.tenant_id, case.run_id) is not None


def _run_state(database: Database, tenant_id: str, run_id: str) -> str | None:
    with database.transaction() as connection:
        run = RunRepository().get(connection, tenant_id, run_id)
    return run.state if run is not None else None


def run_demo(database: Database, case: SyntheticCase, *, message: str) -> dict[str, object]:
    """Execute one complete, fresh synthetic demo and return JSON-safe data."""
    with database.transaction() as connection:
        migrate(connection)
    if _existing_run(database, case):
        raise ValueError(
            f"run {case.run_id!r} already exists; choose a new --case-id instead of resetting data"
        )

    flow = SyntheticAftercareFlow(database, case)
    now = datetime.now(UTC)
    phases: list[dict[str, object]] = []

    flow.admit(message=message)
    phases.append({"name": "admit", "run_state": _run_state(database, case.tenant_id, case.run_id)})

    flow.seed_investigation_observations(now=now, delivery_status=DeliveryStatus.IN_TRANSIT)
    parked = flow.pause_for_customer(now=now)
    phases.append(
        {
            "name": "wait_for_customer",
            "run_state": _run_state(database, case.tenant_id, case.run_id),
            "wait_state": parked.wait.state,
            "next_step": parked.checkpoint.next_step,
            "resume_next_step": parked.checkpoint.resume_next_step,
        }
    )

    resolved = flow.reply(now=datetime.now(UTC), event_id=f"{case.case_id}-reply")
    phases.append(
        {
            "name": "customer_reply",
            "run_state": _run_state(database, case.tenant_id, case.run_id),
            "wait_state": resolved.state,
        }
    )

    resumed = flow.resume(now=datetime.now(UTC))
    assessment = flow.assess(now=datetime.now(UTC))
    phases.append(
        {
            "name": "resume_and_assess",
            "run_state": _run_state(database, case.tenant_id, case.run_id),
            "completed": resumed.completed,
            "disposition": str(assessment.disposition),
            "decision_count": len(assessment.decisions),
        }
    )

    approval = flow.request_refund_approval(now=datetime.now(UTC))
    provider_reference = flow.approve_and_confirm_refund(
        approval, now=datetime.now(UTC), decision_key=f"{case.case_id}-approve"
    )
    with database.transaction() as connection:
        action = connection.execute(
            "SELECT state FROM aftercare_actions WHERE tenant_id=%s AND action_id=%s",
            (case.tenant_id, case.action_id),
        ).fetchone()
        final_approval = ApprovalRepository().get(connection, case.tenant_id, case.approval_id)
        approval_run = RunRepository().get(connection, case.tenant_id, case.approval_run_id)
        events = EventRepository().list_case_events(
            connection, tenant_id=case.tenant_id, case_id=case.case_id
        )
    phases.append(
        {
            "name": "approve_and_fake_confirm",
            "run_state": approval_run.state if approval_run is not None else None,
            "approval": final_approval.decision if final_approval is not None else None,
            "action_state": action[0] if action is not None else None,
        }
    )

    return {
        "status": "completed",
        "mode": "synthetic",
        "boundaries": {
            "model": "none",
            "provider": "fake-only",
            "sandbox": "none",
            "database": "postgresql",
        },
        "case": {
            "tenant_id": case.tenant_id,
            "case_id": case.case_id,
            "order_id": case.order_id,
            "run_id": case.run_id,
        },
        "phases": phases,
        "assessment": {
            "disposition": str(assessment.disposition),
            "decision_count": len(assessment.decisions),
            "assessment_sha256": assessment_digest(assessment),
        },
        "approval": {
            "approval_id": approval.approval_id,
            "decision": final_approval.decision if final_approval is not None else None,
            "provider_reference": provider_reference,
        },
        "events": [
            {"case_seq": event.case_seq, "event_id": event.event_id, "event_type": event.event_type}
            for event in events
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point used by ``python -m aftercare_agent.demo``."""
    parser = build_parser()
    args = parser.parse_args(argv)
    dsn = args.database_url or os.environ.get("DATABASE_URL", "")
    if not dsn:
        parser.error("DATABASE_URL is required (or pass --database-url)")
    case = _new_case(tenant_id=args.tenant_id, case_id=args.case_id, order_id=args.order_id)
    database = Database(dsn)
    try:
        report = run_demo(database, case, message=args.message)
    except (ContractViolation, ValueError, RuntimeError) as exc:
        print(
            json.dumps(
                {"status": "error", "error_type": type(exc).__name__, "message": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    except psycopg.Error:
        # Connection errors can echo DSN details in driver messages. Keep the
        # one-line CLI report safe for CI logs and operators' terminals.
        print(
            json.dumps(
                {
                    "status": "error",
                    "error_type": "DatabaseError",
                    "message": "database operation failed",
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        # The demo borrows a pooled connection per step; closing the pool keeps
        # a one-shot CLI from leaving a maintenance thread or a socket behind.
        database.close()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "main", "run_demo"]
