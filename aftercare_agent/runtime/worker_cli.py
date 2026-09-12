"""Command-line entry point for one slice or a long-lived Worker."""

import json
import math
import os
import signal
from datetime import timedelta
from threading import Event
from types import FrameType

from aftercare_agent.persistence import Database

from .worker import WorkerLoopResult, WorkerResult, run_daemon, run_next, run_once


def _flag(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


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


def main() -> int:
    dsn = os.environ.get("DATABASE_URL", "")
    tenant_id = os.environ.get("AFTERCARE_TENANT_ID", "")
    run_id = os.environ.get("AFTERCARE_RUN_ID", "")
    owner = os.environ.get("AFTERCARE_WORKER_ID", "worker-local")
    if not dsn or (run_id and not tenant_id):
        raise SystemExit(
            "DATABASE_URL is required; AFTERCARE_TENANT_ID is required when running a specific Run"
        )
    max_steps = _positive_int("AFTERCARE_MAX_STEPS", "8")
    lease = _seconds("AFTERCARE_LEASE_SECONDS", "30")
    heartbeat_text = os.environ.get("AFTERCARE_HEARTBEAT_SECONDS", "")
    heartbeat = _seconds("AFTERCARE_HEARTBEAT_SECONDS", heartbeat_text) if heartbeat_text else None
    database = Database(dsn)
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
        )
    else:
        slice_result = run_next(
            database,
            tenant_id=tenant_id,
            owner=owner,
            lease=lease,
            max_steps=max_steps,
            heartbeat_interval=heartbeat,
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
