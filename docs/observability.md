# 可观测性边界

`aftercare_agent.observability.Tracer` 是 OTel 的窄适配接口。核心代码不强制安装或绑定
某个 exporter；测试使用 `InMemoryTracer`，生产可注入 `OpenTelemetryTracer`。Span 属性
只允许有限的租户、事件类型等低敏标签，不写入买家正文、模型 prompt、凭证或退款响应。

Outbox publisher 在网络发布外包围 `aftercare.outbox.publish` span。异常会标记 error 并
继续走已有重试/ACK 语义；trace 是诊断投影，不是审计账本，也不能替代 Inbox、Action
Ledger、Run fencing 或数据库事件。生产接入仍需配置采样、脱敏、留存和 exporter，并把
trace/span 关联到日志与事件 correlation_id。
