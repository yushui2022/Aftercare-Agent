"""Command-line entry point for one durable Worker execution slice."""

import json
import os
from datetime import timedelta

from aftercare_agent.persistence import Database

from .worker import WorkerResult, run_next, run_once


def main() -> int:
    dsn = os.environ.get("DATABASE_URL", "")
    tenant_id = os.environ.get("AFTERCARE_TENANT_ID", "")
    run_id = os.environ.get("AFTERCARE_RUN_ID", "")
    owner = os.environ.get("AFTERCARE_WORKER_ID", "worker-local")
    if not dsn or not tenant_id:
        raise SystemExit("DATABASE_URL and AFTERCARE_TENANT_ID are required")
    try:
        max_steps = int(os.environ.get("AFTERCARE_MAX_STEPS", "8"))
        lease_seconds = int(os.environ.get("AFTERCARE_LEASE_SECONDS", "30"))
    except ValueError as exc:
        raise SystemExit(
            "AFTERCARE_MAX_STEPS and AFTERCARE_LEASE_SECONDS must be integers"
        ) from exc
    database = Database(dsn)
    result: WorkerResult | None
    if run_id:
        result = run_once(
            database,
            tenant_id=tenant_id,
            run_id=run_id,
            owner=owner,
            lease=timedelta(seconds=lease_seconds),
            max_steps=max_steps,
        )
    else:
        result = run_next(
            database,
            tenant_id=tenant_id,
            owner=owner,
            lease=timedelta(seconds=lease_seconds),
            max_steps=max_steps,
        )
        if result is None:
            print(json.dumps({"status": "idle"}))
            return 0
    print(
        json.dumps(
            {
                "tenant_id": result.tenant_id,
                "case_id": result.case_id,
                "run_id": result.run_id,
                "owner": result.owner,
                "fencing_token": result.fencing_token,
                "completed": result.completed,
                "next_step": result.checkpoint.next_step,
                "checkpoint_version": result.checkpoint.checkpoint_version,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
