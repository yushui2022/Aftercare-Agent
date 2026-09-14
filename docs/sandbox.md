# 沙箱控制面契约（C-01）

`aftercare_agent.sandbox.FakeSandboxProvider` 是一个本地、非安全边界的控制面替身。它
刻画真实 E2B/Firecracker/Kubernetes provider 必须满足的生命周期不变量：

- `request_key` 幂等创建，丢失创建响应后重试会返回原 `allocation_id`；
- 分配和执行是两个阶段：`READY` 表示已经分配但尚未开始运行，持有当前
  `owner/fencing_token` 的 Worker 必须显式调用 `start()` 进入 `RUNNING`；重复启动是幂等的；
- allocation 带 owner、fencing token 和数据库/控制面时间意义上的 lease；旧 owner 不能写产物；
- CPU、内存和产物大小有显式上限；
- lease 过期由 `reconcile()` 标记为 `DESTROY_REQUESTED`，但在 provider 返回销毁确认前仍占用容量；
- 只有确认后进入 `DESTROYED`，新 allocation 才能复用容量。

这层只负责“能否分配、谁能续租、是否可以回收”的控制面语义，不运行不可信代码，也不
提供内核隔离、网络出口、凭证注入或对象存储。生产接入时可将同一接口映射到 E2B
自托控制面或 Kubernetes Agent Sandbox；业务数据库、租户配额、Action Ledger 和模型
权限仍必须留在 Aftercare trusted service 中。

`SandboxProvider` 是运行时依赖的最小 `Protocol`，位于
`aftercare_agent.sandbox.contracts`。它刻意不暴露 Kubernetes API、E2B SDK 或数据库连接，
只约束分配、显式启动、续租、销毁确认、产物写入和回收扫描。真实适配器应把 provider 的
外部资源 ID 映射为 `allocation_id`，在网络超时后通过 `request_key`/外部标签找回资源，
并在 `confirm_destroy()` 成功前继续计入容量；适配器不能因为 HTTP 请求超时就假设资源已
销毁。当前 `FakeSandboxProvider` 仍然是测试替身，不提供安全隔离。
