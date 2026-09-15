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

## Connection pools

API and Worker each hold one bounded pool per process (default min 1, max 8
for the API and max 4 for the Worker, acquisition timeout 5 s). Compose passes
`AFTERCARE_DB_POOL_MIN_SIZE`, `AFTERCARE_DB_POOL_MAX_SIZE` and
`AFTERCARE_DB_ACQUIRE_TIMEOUT_SECONDS` through, so the ceiling can be raised
per service. These are bounded defaults, not a sizing result: a pool that
cannot hand out a connection inside the timeout fails closed instead of
queueing forever. See [ADR-0006](../../docs/decisions/0006-bounded-connection-pool.md).

The volume contains only local synthetic data. Production deployment still
needs real authentication, secret injection, backups, TLS, migrations policy,
resource limits, and a separate Worker deployment.

## 合成 Aftercare 演示

如果只想验证完整的业务边界，不需要先手工调用 API。下面的命令会在
PostgreSQL 中迁移 schema，并由一个进程运行一条新的合成 Case：

`admit → WAITING_INPUT → Inbox 唤醒 → checkpoint 恢复 → 可信证据评估 →
WAITING_APPROVAL → 批准 → fake provider CONFIRMED`。

先启动本地 PostgreSQL（使用仓库自带的开发 Compose）：

```powershell
docker compose -f deploy/compose/docker-compose.yml up -d --wait postgres
$env:DATABASE_URL = 'postgresql://aftercare:local-only-aftercare@localhost:55433/aftercare'
uv run aftercare-demo
```

PostgreSQL 的默认宿主端口是 `55433`，可用 `AFTERCARE_POSTGRES_PORT` 覆盖；
这避免占用机器上已有的 `5432` 服务。

命令默认生成 `demo-<random>` Case，因此重复运行不会删除或重置已有数据。也
可以固定租户和 Case 便于演示，但同一个 Case 不能重复执行；若它已存在，请
换一个 `--case-id`。输出是一行 JSON，包含 Case 标识、每个持久阶段、最终
assessment digest、审批/动作结果和按 `case_seq` 排序的 Outbox 事件。

```powershell
uv run aftercare-demo --tenant-id tenant-demo --case-id demo-001
```

这是确定性 Fake Harness 演示：没有模型 API、供应商凭证、沙箱或真实退款。
`aftercare-demo` 只证明 PostgreSQL 事务、Wait/Inbox、检查点恢复和 Action
门禁可以连成一条可复制路径；生产部署仍需真实身份、连接器、审批工作台、
租约调度和安全沙箱。
