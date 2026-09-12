# 跨实例执行准入（A2-03）

Worker 的执行槽不能放在进程内 semaphore：多副本部署时每个进程都会“看见”自己的
容量，噪声租户可以绕过限额。A2-03 将准入状态放进 PostgreSQL 迁移 007：

- `aftercare_admission_limits` 保存 global/tenant 上限；
- `aftercare_execution_slots` 保存 `(tenant, run, owner, fencing_token, lease_until)`；
- 获取槽时先锁定限额行，再清理过期租约并统计当前占用，和 Run claim 在同一短事务中完成；
- 心跳同时续租 Run 与 slot；最终 Run 转换和 slot 释放在一个事务里完成；
- 没有配置任何 limit 时保持兼容模式，不改变现有本地 Fake Worker 行为。

因此，槽是容量控制，不是执行权。真正的执行权仍由 `aftercare_runs` 的 lease/fencing
决定；旧 Worker 即使持有过期 slot，也不能提交检查点。计数使用数据库时钟，避免机器
时钟漂移。不同租户的统计互相隔离，global limit 再提供跨租户总上限。

同一迁移还提供按 Run 的 retry budget。`retry_key` 是幂等键：重放不会重复扣预算，
预算耗尽返回 `BUDGET_EXHAUSTED`。这只限制重试次数，不代表外部供应商动作已经幂等；
UNKNOWN 的退款/补发结果仍必须经过 Action Ledger 核对。

当前实现已在真实 PostgreSQL 17 上验证全局槽竞争、释放后继续、幂等重试预留和完整
Worker 回归（268 项通过）。持久公平队列表已建立，跨租户调度策略仍需在后续压测中按
队列年龄和租户权重校准，不能把示例上限当成生产 SLA。
