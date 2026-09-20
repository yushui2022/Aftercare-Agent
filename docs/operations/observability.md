# 运行观测接入手册

Aftercare 的诊断指标不是审计账本。它们用于回答“有没有积压、哪里变慢、哪个进程
失败”，不能证明退款、审批、证据或外部副作用已经发生。审计事实仍以 PostgreSQL
中的 Run、Wait、Action Ledger、Inbox/Outbox 和证据记录为准。

## 默认日志后端

默认 `AFTERCARE_METRICS_BACKEND=logging`。API 和 Worker 将有界 JSON 行写到
`aftercare_agent.metrics` logger；独立 Worker CLI 会把它写到 stderr。平台应收集这些
行并保留原始时间戳、服务名、实例名和镜像 revision，但不应把完整请求正文、prompt、
凭证或客户消息加入日志字段。

最小的 Worker 队列观测包含：

- `aftercare.queue.runnable_runs`：当前可领取或可回收的 Run 数量；
- `aftercare.queue.oldest_age_seconds`：其中最老 Run 从进入 durable queue 到现在的年龄；
- `aftercare.db.pool.*`：进程连接池健康和等待情况。

只有 `component` 是指标标签。队列指标为空时表示当前没有可运行任务，不能直接当作
Worker 故障；应结合 Worker 心跳、进程状态和 `/readyz` 判断。告警阈值必须由目标环境的
业务延迟预算和容量压测给出，仓库不提供一个伪装成 SLA 的默认秒数。常用告警形状是
`runnable_runs > 0` 且 `oldest_age_seconds` 连续多个采样周期超过预算。

## OTel 后端

设置 `AFTERCARE_METRICS_BACKEND=otel` 才会加载可选 OTel bridge。核心镜像不捆绑 SDK、
exporter 或 provider。需要在构建时显式选择可复现的 `observability` extra：

```bash
docker buildx build \\
  --build-arg AFTERCARE_EXTRAS=observability \\
  --build-arg AFTERCARE_REVISION="$(git rev-parse HEAD)" \\
  -f deploy/Dockerfile .
```

这个 extra 只提供 Python SDK、OTLP gRPC exporter 和 SDK runtime；它不会自动创建
`MeterProvider`、注入 endpoint，也不会替平台决定采样或留存。目标环境仍必须提供
provider/exporter 配置，并在 deployment acceptance 记录中保存连通性和采样/留存策略。
镜像 OCI label `io.aftercare.build.extras` 会记录构建是否选用了该 extra。依赖缺失或
backend 值拼写错误时进程 fail closed，不会静默降级到日志。

OTel 只接收 allowlist 后的低敏 span/metric 属性：span 关联字段有长度上限，metric
只允许 `component`。禁止把 prompt、供应商响应、DSN、token、买家正文或完整 traceback
写进属性。生产采样、留存和访问控制由目标平台负责，不能用 trace 替代审计记录。

## 变更和验收

修改指标名、属性 allowlist、采样间隔或后端时，应先在本地运行：

```bash
uv run pytest tests/test_observability.py tests/persistence/test_queue_metrics.py
python deploy/deployment_preflight.py --env-file /secure/path/aftercare.env
```

目标环境还应实际证明：日志或 OTel 信号可查询、队列积压告警会触发、采样失败不会改变
Run 租约/领取语义、滚动重启后指标仍带正确镜像 revision。证据引用写入
`deployment-acceptance.json`，不要把客户数据复制进仓库。
