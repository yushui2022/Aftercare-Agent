"""Command-line entry point for one slice or a long-lived Worker."""

import json
import logging
import math
import os
import signal
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from threading import Event
from types import FrameType

from aftercare_agent.artifacts import ContentAddressedArtifactStore
from aftercare_agent.config import environment_secret
from aftercare_agent.connectors import CommerceConnector
from aftercare_agent.domain.common import ContractViolation, ErrorCode, RunScope
from aftercare_agent.domain.investigation import (
    FreshnessPolicy,
)
from aftercare_agent.investigation import (
    RunScopedEvidenceRecorder,
    build_investigation_application,
)
from aftercare_agent.observability import metrics_from_environment
from aftercare_agent.persistence import (
    CaseRepository,
    Database,
    QueueStats,
    RunRepository,
    assert_schema_current,
    queue_sampler_from_environment,
    sampler_from_environment,
)
from aftercare_agent.persistence import migrate as migrate_aftercare
from aftercare_agent.persistence.migrate_cli import assert_egm_schema_current

from .judgment import DeterministicEvidenceJudgmentGate, JudgmentGate
from .wiring import harness_from_environment
from .worker import (
    Harness,
    WorkerLoopResult,
    WorkerResult,
    run_daemon,
    run_next,
    run_once,
)


@dataclass(frozen=True)
class _ModelComponents:
    harness: Harness | None
    assessment_evaluator: JudgmentGate | None


@dataclass(frozen=True)
class _PersistedCaseBinding:
    """Resolve the immutable order binding from the admitted Case.

    A daemon may claim Runs for many tenants and orders, so an environment-wide
    order id cannot be authoritative.  The lookup stays lazy because the model
    Harness is assembled before a Run is claimed, and each lookup uses a short
    transaction from the Worker's bounded pool.
    """

    database: Database

    def order_id_for(self, scope: RunScope) -> str:
        with self.database.transaction() as connection:
            return CaseRepository().lock_order_id(connection, scope.tenant_id, scope.case_id)


@dataclass(frozen=True)
class _QueueStatsSource:
    """Read queue diagnostics through the Worker's bounded pool."""

    database: Database
    tenant: str | None

    def queue_stats(self) -> QueueStats:
        with self.database.transaction() as connection:
            return RunRepository().queue_stats(connection, self.tenant)


def _model_components(
    owner: str,
    max_steps: int,
    database: Database,
    *,
    auto_migrate: bool = True,
) -> _ModelComponents:
    """Build the model-driven Harness only when a deployment asks for it.

    The default stays the deterministic fake plan, so an unconfigured worker
    cannot start spending provider budget or reaching a sandbox by accident.
    """
    if not _flag(os.environ.get("AFTERCARE_HARNESS_MODEL", "")):
        return _ModelComponents(None, None)
    _ensure_egm_schema(database, auto_migrate=auto_migrate)
    recorder: RunScopedEvidenceRecorder | None = None
    policy = _investigation_policy()

    def recorder_for(connector: CommerceConnector) -> RunScopedEvidenceRecorder:
        nonlocal recorder
        if recorder is None:
            recorder = RunScopedEvidenceRecorder(
                database=database,
                connector=connector,
                application_for=lambda registered: build_investigation_application(
                    _egm_provider(database), registered
                ),
            )
        return recorder

    def before_run_for(
        connector: CommerceConnector, store: ContentAddressedArtifactStore
    ) -> Callable[[RunScope], None]:
        def import_history(scope: RunScope) -> None:
            recorder_for(connector).import_buyer_history(scope=scope, store=store)

        return import_history

    def evidence_context_for(connector: CommerceConnector) -> Callable[[RunScope], str]:
        return recorder_for(connector).evidence_context

    def recorder_for_scope(scope: RunScope) -> RunScopedEvidenceRecorder:
        # The gate is assembled before a Run is claimed; recorder resolution
        # therefore stays lazy and remains bound to the admitted Case.
        if recorder is None:
            raise ContractViolation(ErrorCode.CONFLICT, "evidence recorder is not initialized")
        return recorder

    try:
        return _ModelComponents(
            harness=harness_from_environment(
                owner=owner,
                case_binding=_PersistedCaseBinding(database),
                max_steps=max_steps,
                # Bound here rather than left to a deployment flag: an answer the
                # model was shown reaches the evidence ledger in the same slice, or
                # the audit trail is only as good as that flag.
                observer_for=recorder_for,
                before_run_for=before_run_for,
                evidence_context_for=evidence_context_for,
            ),
            assessment_evaluator=DeterministicEvidenceJudgmentGate(
                recorder_for=recorder_for_scope,
                policy=policy,
            ),
        )
    except (ContractViolation, ValueError) as error:
        raise SystemExit(f"model Harness is not configured: {error}") from error


def _model_harness(owner: str, max_steps: int, database: Database) -> Harness | None:
    """Compatibility seam used by focused composition tests."""
    return _model_components(owner, max_steps, database).harness


def _egm_provider(database: Database) -> object:
    """Use the worker's bounded pool for the embedded EGM application."""
    from evidence_gated_memory.storage.postgres import PostgresProvider

    return PostgresProvider(database.connection)  # type: ignore[no-untyped-call]


def _ensure_aftercare_schema(database: Database, *, auto_migrate: bool) -> None:
    """Apply or read-only verify the application schema before claiming work."""
    with database.transaction() as connection:
        if auto_migrate:
            migrate_aftercare(connection)
        assert_schema_current(connection)


def _ensure_egm_schema(database: Database, *, auto_migrate: bool) -> None:
    """Apply or read-only verify EGM before a model Run can write evidence."""
    from evidence_gated_memory.storage.postgres import migrate

    with database.transaction() as connection:
        if auto_migrate:
            migrate(connection)  # type: ignore[no-untyped-call]
        assert_egm_schema_current(connection)


def _flag(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _auto_migrate() -> bool:
    value = os.environ.get("AFTERCARE_AUTO_MIGRATE", "1")
    if value not in {"0", "1"}:
        raise SystemExit("AFTERCARE_AUTO_MIGRATE must be 0 or 1")
    return value == "1"


def _seconds(name: str, default: str) -> timedelta:
    try:
        value = float(os.environ.get(name, default))
    except ValueError as exc:
        raise SystemExit(f"{name} must be a positive number") from exc
    if value <= 0 or not math.isfinite(value):
        raise SystemExit(f"{name} must be a positive number")
    return timedelta(seconds=value)


def _positive_int(name: str, default: str) -> int:
    try:
        value = int(os.environ.get(name, default))
    except ValueError as exc:
        raise SystemExit(f"{name} must be a positive integer") from exc
    if value < 1:
        raise SystemExit(f"{name} must be a positive integer")
    return value


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is required for the model Harness")
    return value


def _investigation_policy() -> FreshnessPolicy:
    return FreshnessPolicy(
        policy_id=_required("AFTERCARE_INVESTIGATION_POLICY_ID"),
        policy_version=_positive_int("AFTERCARE_INVESTIGATION_POLICY_VERSION", ""),
        order_max_age_seconds=_positive_int("AFTERCARE_ORDER_MAX_AGE_SECONDS", ""),
        carrier_max_age_seconds=_positive_int("AFTERCARE_CARRIER_MAX_AGE_SECONDS", ""),
        buyer_max_age_seconds=_positive_int("AFTERCARE_BUYER_MAX_AGE_SECONDS", ""),
    )


def main() -> int:
    # Uvicorn configures API logging, but the standalone Worker has no host
    # logger.  Install a minimal stderr handler only when the process has none
    # so JSON diagnostics from LoggingMetrics are visible by default and an
    # embedding deployment can keep its own logging configuration.
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    dsn = environment_secret("DATABASE_URL") or ""
    tenant_id = os.environ.get("AFTERCARE_TENANT_ID", "")
    run_id = os.environ.get("AFTERCARE_RUN_ID", "")
    owner = os.environ.get("AFTERCARE_WORKER_ID", "worker-local")
    if not dsn or (run_id and not tenant_id):
        raise SystemExit(
            "DATABASE_URL is required; AFTERCARE_TENANT_ID is required when running a specific Run"
        )
    if _flag(os.environ.get("AFTERCARE_WORKER_REQUIRE_TENANT", "")) and not tenant_id:
        raise SystemExit(
            "AFTERCARE_TENANT_ID is required when AFTERCARE_WORKER_REQUIRE_TENANT is enabled"
        )
    max_steps = _positive_int("AFTERCARE_MAX_STEPS", "8")
    lease = _seconds("AFTERCARE_LEASE_SECONDS", "30")
    heartbeat_text = os.environ.get("AFTERCARE_HEARTBEAT_SECONDS", "")
    heartbeat = _seconds("AFTERCARE_HEARTBEAT_SECONDS", heartbeat_text) if heartbeat_text else None
    database = Database(dsn)
    metrics = metrics_from_environment()
    sampler = sampler_from_environment(database, metrics, component="worker")
    queue_sampler = queue_sampler_from_environment(
        _QueueStatsSource(database, tenant_id or None), metrics, component="worker"
    )
    try:
        auto_migrate = _auto_migrate()
        _ensure_aftercare_schema(database, auto_migrate=auto_migrate)
        components = _model_components(
            owner,
            max_steps,
            database,
            auto_migrate=auto_migrate,
        )
        if sampler is not None:
            sampler.start()
        if queue_sampler is not None:
            queue_sampler.start()
        return _run(
            database,
            tenant_id=tenant_id,
            run_id=run_id,
            owner=owner,
            max_steps=max_steps,
            lease=lease,
            heartbeat=heartbeat,
            harness=components.harness,
            assessment_evaluator=components.assessment_evaluator,
        )
    finally:
        if sampler is not None:
            sampler.stop()
        if queue_sampler is not None:
            queue_sampler.stop()
        # A slice, and more so a daemon loop, borrows a pooled connection per
        # unit of work.  Closing the pool here returns them and stops the pool
        # threads before the process exits.
        database.close()


def _run(
    database: Database,
    *,
    tenant_id: str,
    run_id: str,
    owner: str,
    max_steps: int,
    lease: timedelta,
    heartbeat: timedelta | None,
    harness: Harness | None,
    assessment_evaluator: JudgmentGate | None,
) -> int:
    if _flag(os.environ.get("AFTERCARE_WORKER_DAEMON", "")):
        stop = Event()
        max_iterations_text = os.environ.get("AFTERCARE_MAX_ITERATIONS", "")
        max_iterations = (
            _positive_int("AFTERCARE_MAX_ITERATIONS", max_iterations_text)
            if max_iterations_text
            else None
        )

        def stop_worker(signum: int, frame: FrameType | None) -> None:
            del signum, frame
            stop.set()

        old_int = signal.signal(signal.SIGINT, stop_worker)
        old_term = signal.signal(signal.SIGTERM, stop_worker)
        try:
            daemon_result: WorkerLoopResult = run_daemon(
                database,
                tenant_id=tenant_id or None,
                owner=owner,
                stop_event=stop,
                idle_sleep=_seconds("AFTERCARE_IDLE_SLEEP_SECONDS", "1"),
                lease=lease,
                heartbeat_interval=heartbeat,
                max_steps=max_steps,
                max_iterations=max_iterations,
                on_error=lambda error: print(
                    json.dumps(
                        {"status": "worker_error", "error_type": type(error).__name__},
                        sort_keys=True,
                    )
                ),
                harness=harness,
                assessment_evaluator=assessment_evaluator,
            )
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)
        print(
            json.dumps(
                {
                    "status": "stopped",
                    "iterations": daemon_result.iterations,
                    "claimed": daemon_result.claimed,
                    "completed": daemon_result.completed,
                    "idle_polls": daemon_result.idle_polls,
                    "failures": daemon_result.failures,
                },
                sort_keys=True,
            )
        )
        return 0
    slice_result: WorkerResult | None
    if run_id:
        slice_result = run_once(
            database,
            tenant_id=tenant_id,
            run_id=run_id,
            owner=owner,
            lease=lease,
            max_steps=max_steps,
            heartbeat_interval=heartbeat,
            harness=harness,
            assessment_evaluator=assessment_evaluator,
        )
    else:
        slice_result = run_next(
            database,
            tenant_id=tenant_id or None,
            owner=owner,
            lease=lease,
            max_steps=max_steps,
            heartbeat_interval=heartbeat,
            harness=harness,
            assessment_evaluator=assessment_evaluator,
        )
        if slice_result is None:
            print(json.dumps({"status": "idle"}))
            return 0
    print(
        json.dumps(
            {
                "tenant_id": slice_result.tenant_id,
                "case_id": slice_result.case_id,
                "run_id": slice_result.run_id,
                "owner": slice_result.owner,
                "fencing_token": slice_result.fencing_token,
                "completed": slice_result.completed,
                "next_step": slice_result.checkpoint.next_step,
                "checkpoint_version": slice_result.checkpoint.checkpoint_version,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
