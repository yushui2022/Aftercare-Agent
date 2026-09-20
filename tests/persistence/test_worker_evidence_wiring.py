"""The Worker's own composition files a Run's answer as evidence (PostgreSQL)."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any, cast

import pytest

from aftercare_agent.domain.common import ContractViolation, ErrorCode, RunScope
from aftercare_agent.domain.protocol import ToolRequest
from aftercare_agent.domain.runtime import CaseRecord
from aftercare_agent.persistence import Database, RunRepository
from aftercare_agent.runtime.sandbox_executor import SandboxedToolExecutor
from aftercare_agent.runtime.worker_cli import _model_harness

SAMPLE = Path(str(files("aftercare_agent.connectors").joinpath("data/commerce-sample.json")))
# The export names its own tenant and the connector refuses to answer for
# another one, so the Case has to live where its data does.
TENANT = "tenant-demo"
ORDER = "A-1001"
CASE = "worker-evidence-case"
NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)
LOOKUP = ToolRequest(call_id="call-1", name="lookup_order", arguments_json="{}")


@pytest.fixture()
def executor(
    db: Database, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> SandboxedToolExecutor:
    """The executor the Worker CLI builds for a deployment that configured one."""
    settings = {
        "AFTERCARE_HARNESS_MODEL": "1",
        "AFTERCARE_MODEL_API_KEY": "unit-test-key",
        "AFTERCARE_MODEL_INPUT_MICROUSD_PER_TOKEN": "1",
        "AFTERCARE_MODEL_OUTPUT_MICROUSD_PER_TOKEN": "2",
        "AFTERCARE_INVESTIGATION_POLICY_ID": "worker-test-policy",
        "AFTERCARE_INVESTIGATION_POLICY_VERSION": "1",
        "AFTERCARE_ORDER_MAX_AGE_SECONDS": "86400",
        "AFTERCARE_CARRIER_MAX_AGE_SECONDS": "86400",
        "AFTERCARE_BUYER_MAX_AGE_SECONDS": "86400",
        "AFTERCARE_COMMERCE_DATASET": str(SAMPLE),
        "AFTERCARE_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
        "AFTERCARE_SOURCE_ORDER_LEDGER": "worker-erp",
        "AFTERCARE_SOURCE_CARRIER": "worker-carrier",
        "AFTERCARE_SOURCE_BUYER_CHANNEL": "worker-in-app",
    }
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    harness = _model_harness("worker-evidence", 4, db)
    assert harness is not None
    # The Harness is a partial over the composition; the executor it will call
    # is the object that has to carry the evidence recorder.
    return cast(SandboxedToolExecutor, cast(Any, harness).keywords["executor"])


def _admit(db: Database, case_id: str, *, order_id: str = ORDER) -> None:
    with db.transaction() as connection:
        for table in (
            "aftercare_investigation_egm_revocations",
            "aftercare_investigation_egm_projections",
            "aftercare_investigation_buyer_cursors",
            "aftercare_investigation_egm_bindings",
            "aftercare_investigation_observations",
            "aftercare_cases",
        ):
            connection.execute(
                f"DELETE FROM {table} WHERE tenant_id=%s AND case_id=%s", (TENANT, case_id)
            )
        for table in ("egm_audit", "egm_operations", "egm_records", "egm_cases"):
            connection.execute(
                f"DELETE FROM {table} WHERE tenant_id=%s AND case_id=%s", (TENANT, case_id)
            )
        RunRepository().create_case(
            connection, CaseRecord(tenant_id=TENANT, case_id=case_id, order_id=order_id, version=1)
        )


def _rows(db: Database, case_id: str) -> list[tuple[str, str, str]]:
    with db.connection() as connection:
        return [
            (str(row[0]), str(row[1]), str(row[2]))
            for row in connection.execute(
                "SELECT case_id, order_id, source_id FROM aftercare_investigation_observations "
                "WHERE tenant_id=%s AND case_id=%s",
                (TENANT, case_id),
            ).fetchall()
        ]


def test_the_worker_files_an_answer_under_the_case_it_is_running(
    db: Database, executor: SandboxedToolExecutor
) -> None:
    _admit(db, CASE)
    scope = RunScope(tenant_id=TENANT, case_id=CASE, run_id="run-evidence")
    executor.execute(LOOKUP, scope=scope, now=NOW)
    # Simulate a crash after EGM committed but before Aftercare saved the
    # completion receipt.  The replay must reuse the original revision.
    with db.transaction() as connection:
        connection.execute(
            "UPDATE aftercare_investigation_egm_projections "
            "SET completed_revision=NULL,egm_evidence_id=NULL WHERE tenant_id=%s AND case_id=%s",
            (TENANT, CASE),
        )
    restarted_harness = _model_harness("worker-evidence-restarted", 4, db)
    assert restarted_harness is not None
    restarted_executor = cast(
        SandboxedToolExecutor, cast(Any, restarted_harness).keywords["executor"]
    )
    # A second call id is the same unchanged fact, so it stays one ledger row.
    restarted_executor.execute(
        ToolRequest(call_id="call-2", name="lookup_order", arguments_json="{}"),
        scope=scope,
        now=NOW,
    )
    assert _rows(db, CASE) == [(CASE, ORDER, "worker-erp")]
    with db.connection() as connection:
        receipt = connection.execute(
            "SELECT expected_revision,completed_revision,egm_evidence_id "
            "FROM aftercare_investigation_egm_projections WHERE tenant_id=%s AND case_id=%s",
            (TENANT, CASE),
        ).fetchone()
    assert receipt is not None and receipt[1] > receipt[0] and receipt[2]

    other_case = f"{CASE}-other-order"
    _admit(db, other_case, order_id="A-1002")
    restarted_executor.execute(
        ToolRequest(call_id="call-other", name="lookup_order", arguments_json="{}"),
        scope=RunScope(tenant_id=TENANT, case_id=other_case, run_id="run-other"),
        now=NOW,
    )
    assert _rows(db, other_case) == [(other_case, "A-1002", "worker-erp")]


def test_a_new_worker_replays_an_egm_revocation_after_receipt_loss(
    db: Database, executor: SandboxedToolExecutor
) -> None:
    _admit(db, CASE)
    scope = RunScope(tenant_id=TENANT, case_id=CASE, run_id="run-revoke")
    executor.execute(LOOKUP, scope=scope, now=NOW)
    observer = cast(Any, executor)._observer
    assert observer is not None
    with db.connection() as connection:
        row = connection.execute(
            "SELECT evidence_id FROM aftercare_investigation_observations "
            "WHERE tenant_id=%s AND case_id=%s",
            (TENANT, CASE),
        ).fetchone()
    assert row is not None
    evidence_id = str(row[0])
    revoked_at = NOW.replace(hour=13)
    observer.revoke_evidence(
        scope,
        evidence_id=evidence_id,
        revoked_at=revoked_at,
        reason="source corrected the order snapshot",
    )
    # Recreate the EGM-committed / Aftercare-unconfirmed crash window.
    with db.transaction() as connection:
        connection.execute(
            "UPDATE aftercare_investigation_egm_revocations "
            "SET completed_revision=NULL,egm_revoked_at=NULL "
            "WHERE tenant_id=%s AND case_id=%s AND evidence_id=%s",
            (TENANT, CASE, evidence_id),
        )
    restarted_harness = _model_harness("worker-revoke-restarted", 4, db)
    assert restarted_harness is not None
    restarted_executor = cast(
        SandboxedToolExecutor, cast(Any, restarted_harness).keywords["executor"]
    )
    restarted_observer = cast(Any, restarted_executor)._observer
    replayed = restarted_observer.revoke_evidence(
        scope,
        evidence_id=evidence_id,
        revoked_at=revoked_at,
        reason="source corrected the order snapshot",
    )
    assert replayed.revoked_at == revoked_at
    with db.connection() as connection:
        receipt = connection.execute(
            "SELECT expected_revision,completed_revision,egm_revoked_at "
            "FROM aftercare_investigation_egm_revocations "
            "WHERE tenant_id=%s AND case_id=%s AND evidence_id=%s",
            (TENANT, CASE, evidence_id),
        ).fetchone()
        egm = connection.execute(
            "SELECT payload->>'revoked_at' FROM egm_records "
            "WHERE tenant_id=%s AND case_id=%s AND kind='evidence'",
            (TENANT, CASE),
        ).fetchone()
        revision = connection.execute(
            "SELECT revision FROM egm_cases WHERE tenant_id=%s AND case_id=%s",
            (TENANT, CASE),
        ).fetchone()
    assert receipt is not None and receipt[1] > receipt[0] and receipt[2] is not None
    assert egm is not None and egm[0] is not None
    assert revision == (3,)
    context = json.loads(restarted_observer.evidence_context(scope))
    assert context["observations"][0]["revoked_at"] == revoked_at.isoformat()


def test_concurrent_worker_evidence_rebases_one_egm_revision(
    db: Database, executor: SandboxedToolExecutor
) -> None:
    _admit(db, CASE)
    scope = RunScope(tenant_id=TENANT, case_id=CASE, run_id="run-concurrent-evidence")
    requests = (
        ToolRequest(call_id="order-concurrent", name="lookup_order", arguments_json="{}"),
        ToolRequest(call_id="tracking-concurrent", name="lookup_tracking", arguments_json="{}"),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(executor.execute, request, scope=scope, now=NOW) for request in requests
        ]
        results = [future.result() for future in futures]

    assert len(results) == 2
    with db.connection() as connection:
        projections = connection.execute(
            "SELECT expected_revision,completed_revision,egm_evidence_id "
            "FROM aftercare_investigation_egm_projections "
            "WHERE tenant_id=%s AND case_id=%s ORDER BY evidence_id",
            (TENANT, CASE),
        ).fetchall()
        revision = connection.execute(
            "SELECT revision FROM egm_cases WHERE tenant_id=%s AND case_id=%s", (TENANT, CASE)
        ).fetchone()
    assert len(projections) == 2
    assert all(
        completed > expected and evidence_id for expected, completed, evidence_id in projections
    )
    assert revision == (3,)


def test_a_run_for_an_unadmitted_case_records_nothing(
    db: Database, executor: SandboxedToolExecutor
) -> None:
    scope = RunScope(tenant_id=TENANT, case_id="case-never-admitted", run_id="run-1")
    with pytest.raises(ContractViolation) as error:
        executor.execute(LOOKUP, scope=scope, now=NOW)
    assert error.value.code is ErrorCode.FORBIDDEN
    assert _rows(db, "case-never-admitted") == []
