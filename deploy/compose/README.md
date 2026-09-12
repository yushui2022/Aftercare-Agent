# Local Compose smoke environment

This is a development-only environment for the A1 API, PostgreSQL migration and
an explicit one-shot Fake Worker slice. The default profile does not start a
Worker; the `worker` profile can target a specific Run ID or claim the next
READY Run for the configured tenant. It still has
no model adapter, connector, sandbox, or real business action. Synthetic identity
is enabled solely so the local API can be exercised; do not expose this compose
file to a network or reuse its password.

From the repository root:

```powershell
docker compose -f deploy/compose/docker-compose.yml up --build
```

The API listens on `http://localhost:8000`. A minimal local request needs the
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
the worker uses PostgreSQL `SKIP LOCKED` to claim one runnable `READY` run for
the tenant and exits with `{"status":"idle"}` when none exists. This is a
bounded poll, not yet a long-running scheduler; A2 adds durable wakeups and
fair polling.

The volume contains only local synthetic data. Production deployment still
needs real authentication, secret injection, backups, TLS, migrations policy,
resource limits, and a separate Worker deployment.
