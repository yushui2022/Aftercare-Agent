# PostgreSQL 持久化骨架（A1-01）

当前仓库包含 A1/A2 的持久化骨架：`aftercare_agent/persistence/` 提供显式迁移、短事务连接工厂、Case/Run 建立、Run 租约领取/续租，以及带当前执行权校验的检查点读写；009 增加工单级调查观察账本。

## 运行边界

```text
Database.transaction()
    └─ migrate(connection)       # 显式 SQL 迁移
    └─ RunRepository.claim()     # FOR UPDATE + clock_timestamp + fencing
    └─ CheckpointRepository.save() # 当前 owner/token/租约校验
```

迁移使用 `aftercare_schema_migrations` 记录版本和 SHA-256，并在启动时使用事务级 advisory lock 串行迁移。业务表使用 `(tenant_id, case_id, ...)` 范围键。Run 领取必须在一个短事务中完成；模型、连接器和人工等待不能放在数据库事务内。租约时间来自 PostgreSQL `clock_timestamp()`，不是 Worker 本地时钟。旧 owner 的 token 或已过期租约不能续租或写检查点。

`aftercare_investigation_observations` 保留规范化绑定列和完整 canonical JSON。唯一键同时约束
工单内 `evidence_id` 与来源事件 `(source_id, source_event_id)`；同键重放必须逐字段相同，
否则返回 `CONFLICT`。`InvestigationObservationRepository.list_case()` 按 evidence ID 返回完整
快照，供 `assess_investigation()` 在进程重启后重新计算；EGM 不是这张业务账本的替代品。

## 本地测试

配置只用于测试数据库的 `DATABASE_URL` 后运行：

```powershell
$env:DATABASE_URL = "postgresql://..."
.venv\Scripts\python.exe -m pytest -q tests/persistence
```

未配置 `DATABASE_URL` 时，集成测试会跳过，不应把跳过当作 PostgreSQL 验收。当前测试覆盖迁移后 claim token 单调递增、过期接管、旧 owner 续租拒绝、检查点范围和执行权校验、受理幂等重放/改参冲突，以及 Case version compare-and-swap。

## 当前未完成

这不是完整运行时：真实业务 Harness、生产认证、供应商连接器、沙箱和真实 EGM 调查 schema
仍待实施。Wait/Inbox/Outbox、发布租约、常驻 Worker、基础 fencing、调查观察账本和 Action
Ledger 已有真实 PostgreSQL 验收；备份、恢复演练、保留删除与恢复后核对已由
[`aftercare-backup`](operations/backup-restore.md) 提供（[ADR-0008](decisions/0008-backup-and-restore-drills.md)），
当前 SQL 仍不包含生产 RLS、HA、WAL 归档/时间点恢复或备份加密。
