# ADR-0014：策略迁移必须独立于 Review 放行

## 状态

已接受，2026-09-20。

## 决策

当已持久化 Run 的 `strategy_id`、模型配置、策略或工具 schema 与当前部署不一致时，Worker
必须先把 Run 路由为 `model_strategy_changed → REVIEW`。迁移由持有 Case 级
`strategy:migrate` 权限的操作员通过独立入口完成，入口要求旧身份、目标身份、checkpoint
版本和理由完全匹配，并写入不可变的 `aftercare_strategy_migrations` 审计行和
`review.strategy_migrated` 事件。

迁移只产生新的 checkpoint，路由原因改为 `strategy_migration_ready`，Run 仍保持 `REVIEW`。
普通 Review 仍须随后选择 `CONTINUE` 或 `CANCEL`；迁移权限不包含退款、补发、通知或其他
外部动作权限。幂等键只允许完全相同的迁移重放，改参、跨 Case、无 pending Review、非
策略漂移路由和陈旧 checkpoint 一律拒绝。

## 理由

模型策略变化和人工业务决定是两个不同的事实。把二者塞进 `review:override` 会让预算权限
意外变成模型切换权限，也无法回答“谁批准了策略变化”和“谁批准了继续执行”。独立控制面
保留旧 checkpoint、目标身份、操作者和理由，便于回滚前核对与事故调查，同时不让迁移本身
绕过 Aftercare 的人工门。

## 后果

增加一个迁移权限、审计表、事件类型和 API 路由；部署切换需要先执行迁移，再由 Review
工作流放行旧 Run。当前只提供 PostgreSQL 和 API 边界，真实 IdP 的权限映射、发布编排和
目标环境演练仍属于部署准入工作。
