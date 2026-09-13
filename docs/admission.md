# 跨实例执行准入（A2-03）

Worker 的执行槽不能放在进程内 semaphore：多副本部署时每个进程都会“看见”自己的
容量，噪声租户可以绕过限额。A2-03 将准入状态和可恢复调度提示放进 PostgreSQL 迁移
007/008：

- `aftercare_admission_limits` 保存 global/tenant 上限；
- `aftercare_execution_slots` 保存 `(tenant, run, owner, fencing_token, lease_until)`；
- 获取槽时先锁定限额行，再清理过期租约并统计当前占用，和 Run claim 在同一短事务中完成；
- 心跳同时续租 Run 与 slot；若本切片原先持有的 slot 缺失、过期或被新 fence
  接管，slot 续租会返回 `LEASE_LOST`，Heartbeat fail-closed，不会只延长 Run
  lease；最终 Run 转换和 slot 释放在一个事务里完成；
- `aftercare_execution_queue` 由 008 的 Run 状态触发器维护：READY/RETRY_AT 自动入队，
  RUNNING 保存 owner/token，等待/完成/取消自动出队；旧数据库启动时会回填队列；
- `run_next(tenant_id=None)` 按持久 tenant cursor 轮转，并跳过已满的 global/tenant 槽；
  IN_FLIGHT 队列项以 Run 的过期 lease 为回收条件，不另造一套执行权；
- 没有配置任何 limit 时保持兼容模式，不改变现有本地 Fake Worker 行为。

因此，槽是容量控制，不是执行权。真正的执行权仍由 `aftercare_runs` 的 lease/fencing
决定；旧 Worker 即使持有过期 slot，也不能提交检查点。计数使用数据库时钟，避免机器
时钟漂移。不同租户的统计互相隔离，global limit 再提供跨租户总上限。

同一迁移还提供按 Run 的 retry budget。`retry_key` 是幂等键：重放不会重复扣预算，
预算耗尽返回 `BUDGET_EXHAUSTED`。这只限制重试次数，不代表外部供应商动作已经幂等；
UNKNOWN 的退款/补发结果仍必须经过 Action Ledger 核对。

当前实现已在真实 PostgreSQL 17 上验证全局槽竞争、释放后继续、幂等重试预留、跨租户
轮转、过期 IN_FLIGHT 回收和完整 Worker 回归。公平性目前是基础 round-robin cursor，
还没有按队列年龄/租户权重校准，也不能把示例上限当成生产 SLA；生产压测与 Linux 试点
仍是后续门禁。
