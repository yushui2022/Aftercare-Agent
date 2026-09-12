# PostgreSQL 持久化骨架（A1-01）

当前仓库包含 A1-01 的第一版持久化骨架：`aftercare_agent/persistence/` 提供显式迁移、短事务连接工厂、Case/Run 建立、Run 租约领取/续租，以及带当前执行权校验的检查点读写。

## 运行边界

```text
Database.transaction()
    └─ migrate(connection)       # 显式 SQL 迁移
    └─ RunRepository.claim()     # FOR UPDATE + clock_timestamp + fencing
    └─ CheckpointRepository.save() # 当前 owner/token/租约校验
```

迁移使用 `aftercare_schema_migrations` 记录版本，业务表使用 `(tenant_id, case_id, ...)` 范围键。Run 领取必须在一个短事务中完成；模型、连接器和人工等待不能放在数据库事务内。租约时间来自 PostgreSQL `clock_timestamp()`，不是 Worker 本地时钟。旧 owner 的 token 或已过期租约不能续租或写检查点。

## 本地测试

配置只用于测试数据库的 `DATABASE_URL` 后运行：

```powershell
$env:DATABASE_URL = "postgresql://..."
.venv\Scripts\python.exe -m pytest -q tests/persistence
```

未配置 `DATABASE_URL` 时，集成测试会跳过，不应把跳过当作 PostgreSQL 验收。当前测试覆盖迁移后 claim token 单调递增、过期接管、旧 owner 续租拒绝、检查点范围和执行权校验、受理幂等重放/改参冲突，以及 Case version compare-and-swap。

## 当前未完成

这不是完整运行时：状态转换事件、Wait/Inbox/Outbox、两个独立进程的故障恢复和真实 PostgreSQL 并发验收仍待 A1/A2 实施。当前 SQL 也不包含生产 RLS、备份恢复、HA 或保留删除策略。
