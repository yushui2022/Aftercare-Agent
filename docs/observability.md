# 可观测性边界

`aftercare_agent.observability.Tracer` 是 OTel 的窄适配接口。核心代码不强制安装或绑定
某个 exporter；测试使用 `InMemoryTracer`，生产可注入 `OpenTelemetryTracer`。实现会在
所有 sink 边界强制执行 allowlist：span 只保留有限的租户、Case、Run、步骤、事件和
关联标识，metric 只保留 `component` 标签，单值最多 128 个字符。未知键、空值、NUL
值会被丢弃，因此调用方误传买家正文、模型 prompt、凭证或退款响应时不会进入诊断系统。

Outbox publisher 在网络发布外包围 `aftercare.outbox.publish` span。异常会标记 error 并
继续走已有重试/ACK 语义；trace 是诊断投影，不是审计账本，也不能替代 Inbox、Action
Ledger、Run fencing 或数据库事件。这个边界只负责诊断投影；生产接入仍需配置
exporter、采样、脱敏、留存和告警策略，并把 trace/span 关联到日志与事件
correlation_id。

进程指标后端由 `AFTERCARE_METRICS_BACKEND` 选择，默认是 `logging`，即标准错误上的
有界 JSON 行；设置为 `otel` 才会加载可选的 `OpenTelemetryMetrics` bridge。核心依赖
不携带 OTel SDK 或 exporter，因此目标环境必须在镜像/运行时另外提供兼容的 OTel
API、SDK、exporter 和 provider 配置；没有这些组件时启动会失败，而不会静默退回日志。

Worker 另外发布 `aftercare.queue.runnable_runs` 与
`aftercare.queue.oldest_age_seconds`。它们只统计当前可运行的 durable Run，查询是
短事务只读快照，不领取任务、不改变租约；指标只使用 `component` 标签。队列指标
默认每 10 秒采样，可用 `AFTERCARE_QUEUE_METRICS=0` 关闭，并用
`AFTERCARE_QUEUE_METRICS_INTERVAL_SECONDS` 调整间隔。
