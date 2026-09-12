# Local Compose smoke environment

This is a development-only environment for the A1 API, PostgreSQL migration and
an explicit one-shot Fake Worker slice. The default profile does not start a
Worker; the `worker` profile can target a specific Run ID or claim the next
runnable Run for the configured tenant. If `AFTERCARE_TENANT_ID` is empty, the
Worker uses the durable PostgreSQL queue's cross-tenant fairness cursor. It still has
no model adapter, connector, sandbox, or real business action. Synthetic identity
is enabled solely so the local API can be exercised; do not expose this compose
file to a network or reuse its password.

From the repository root:

```powershell
docker compose -f deploy/compose/docker-compose.yml up --build
```

The API listens on `http://localhost:8000` (override the host port with
`AFTERCARE_API_PORT` if it is already occupied). A minimal local request needs the
`X-Synthetic-Tenant`, `X-Synthetic-Subject`, and `Idempotency-Key` headers. Stop
and remove the development volume with:

```powershell
docker compose -f deploy/compose/docker-compose.yml down -v
```

To execute one bounded Fake Worker slice after obtaining a `run_id` from the
API, set `AFTERCARE_TENANT_ID` and `AFTERCARE_RUN_ID`, then run:

```powershell
$env:AFTERCARE_TENANT_ID = "tenant-1"
$env:AFTERCARE_RUN_ID = "run-from-api"
docker compose -f deploy/compose/docker-compose.yml --profile worker run --rm worker
```

The command exits after one lease-held slice and prints a JSON checkpoint
summary. Repeat it to resume a `READY` run. If `AFTERCARE_RUN_ID` is omitted,
the worker uses PostgreSQL `FOR UPDATE SKIP LOCKED` and the durable execution
queue to claim one runnable `READY`/due retry run (for the configured tenant,
or fairly across tenants when it is empty) and exits with
`{"status":"idle"}` when none exists. This remains a bounded poll; daemon
mode is the long-running scheduler entry point.

For a local daemon that shares one Worker pool across all tenants, leave
`AFTERCARE_TENANT_ID` empty and set `AFTERCARE_WORKER_DAEMON=1`:

```powershell
$env:AFTERCARE_TENANT_ID = ""
$env:AFTERCARE_WORKER_DAEMON = "1"
docker compose -f deploy/compose/docker-compose.yml --profile worker up --build worker
```

The Worker waits for the API readiness probe, which ensures PostgreSQL
migrations have completed before it starts polling. This remains development
configuration; production should run migrations as a separately audited job.

The volume contains only local synthetic data. Production deployment still
needs real authentication, secret injection, backups, TLS, migrations policy,
resource limits, and a separate Worker deployment.
