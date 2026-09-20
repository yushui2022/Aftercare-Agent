# Local development profile

This is the reproducible local profile for the A1 API, PostgreSQL migration and
an explicit one-shot Worker slice. The default profile does not start a Worker;
the `worker` profile can target a specific Run ID or claim the next runnable Run
for the configured tenant. If `AFTERCARE_TENANT_ID` is empty, the Worker uses the
durable PostgreSQL queue's cross-tenant fairness cursor. Provider choices are
bounded for local use: model, connector, sandbox and external action adapters
are replaceable integration seams, while synthetic identity is enabled only so
the local API can be exercised. Do not expose this compose file to a network or
reuse its password.

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

To execute one bounded Worker slice after obtaining a `run_id` from the
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

Each process also publishes pool health every 10 s as one JSON line on its log
(`aftercare.db.pool.*`, disabled with `AFTERCARE_POOL_METRICS=0`). Measuring
whether a ceiling should change is a separate job: run
`aftercare-capacity` against a real database (see
[ADR-0007](../../docs/decisions/0007-pool-metrics-and-capacity.md) and the
[capacity reports](../../docs/capacity/README.md)) -- on a development host,
pre-warming to the concurrency the process must carry mattered far more than
raising the ceiling.

The Worker also publishes runnable durable-queue depth and oldest runnable age
(`aftercare.queue.runnable_runs` and `aftercare.queue.oldest_age_seconds`) every
10 s. Both use only the `component=worker` label; the query is a short,
read-only snapshot and does not claim work or inspect customer payloads. Set
`AFTERCARE_QUEUE_METRICS=0` to disable it, or change
`AFTERCARE_QUEUE_METRICS_INTERVAL_SECONDS` for a measured deployment interval.

Both services select their metric sink with `AFTERCARE_METRICS_BACKEND`.
The default `logging` backend needs no extra package. `otel` is an explicit
deployment choice and requires the selected image to include an OpenTelemetry
SDK/exporter/provider setup; the core image does not include those packages.
Build the image with `--build-arg AFTERCARE_EXTRAS=observability` to include the
locked SDK and OTLP gRPC exporter. This extra supplies libraries only: the target
platform still configures the provider, endpoint, sampling and retention, and the
image records the choice in `io.aftercare.build.extras`.

The volume contains only local synthetic data. The platform-neutral
[deployment profile](../../docs/deployment.md) separates migration and runtime
database identities and disables runtime DDL; a selected target still needs
real authentication, secret injection, TLS and measured resource limits.
Backups are an operator job, not a
Compose service: `aftercare-backup` writes a dump plus a manifest, drills the
restore into a scratch database, records what the drill did next to the dump,
and keeps the copies bounded; `status` then answers "how old is the newest
recovery point, and when was a restore last proven to work" against budgets the
deployment states, and `wal` checks that the WAL archive behind that
point is contiguous, still advancing, and reaches the newest dump (see
[ADR-0008](../../docs/decisions/0008-backup-and-restore-drills.md),
[ADR-0009](../../docs/decisions/0009-backup-freshness-and-drill-records.md),
[ADR-0010](../../docs/decisions/0010-wal-archive-checks.md) and
the [runbook](../../docs/operations/backup-restore.md)); it does not drill a
point-in-time recovery, and does not cover off-site copies or encryption.

## Local profile 演示

如果只想验证完整的业务边界，不需要先手工调用 API。下面的命令会在
PostgreSQL 中迁移 schema，并由一个进程运行一条新的合成 Case：

`admit → WAITING_INPUT → Inbox 唤醒 → checkpoint 恢复 → 可信证据评估 →
WAITING_APPROVAL → 批准 → SyntheticActionProvider CONFIRMED`。

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

这是确定性的 local profile 演示：不需要模型 API、供应商凭证或外部动作权限。
`aftercare-demo` 证明 PostgreSQL 事务、Wait/Inbox、检查点恢复和 Action
门禁可以连成一条可复制路径；接入真实身份、连接器、审批工作台、租约调度
和安全沙箱时，仍沿用同一套业务契约并按目标环境验收。

## Integration profile

`docker-compose.integration.yml` 是仓库内可复现的集成 smoke profile。它按迁移身份 →
RLS owner harden → runtime DDL/跨租户探针 → 无 DDL API → 合成 vertical slice 的顺序启动，API readiness 必须先通过，最后由
`aftercare-demo --skip-migrate` 验证 PostgreSQL、等待/唤醒、检查点恢复、证据评定、审批和
`SyntheticActionProvider` 回执路径。它仍然不接真实 IdP、模型或业务供应商：

```powershell
docker compose -f deploy/compose/docker-compose.integration.yml up --build -d postgres migrate api
docker compose -f deploy/compose/docker-compose.integration.yml run --no-deps --build --rm smoke
docker compose -f deploy/compose/docker-compose.integration.yml down -v
```

成功标准是 `runtime-check` 和 `smoke` 退出码均为 0，且 JSON 报告包含 `status=completed`、`APPROVED` 和
`CONFIRMED`。临时数据库卷应在第二条命令中删除；该 profile 是 integration 证据，
不能代替目标环境的 IdP、TLS、备份、容量和真实供应商验收。`force-rls` 和 `runtime-check`
还会把机器可读结果写入 `AFTERCARE_INTEGRATION_EVIDENCE_DIR` 指定的目录（默认是仓库外的
`.integration-evidence/`）：分别为 `rls-harden.json` 和 `rls-verify.json`。CI 会把这两份
文件作为 `integration-rls-evidence` artifact 归档；本地若要保留报告，可先设置该变量到
G 盘临时目录。CI 随后调用 [`deploy/verify_rls_evidence.py`](../verify_rls_evidence.py)，
把两份原始报告绑定成 `rls-evidence.json`；它同时校验 harden 和 verify 使用同一 runtime
role、租户表数量和镜像 digest。`--no-deps --build` 让 smoke 使用当前源码构建的镜像，
同时避免重放已经完成的迁移/RLS 一次性服务。
