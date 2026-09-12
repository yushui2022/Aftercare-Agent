# Action Ledger（B-01 最小闭环）

Action Ledger 是 Aftercare 对外部业务动作的持久化边界。它把“模型建议/工具调用”与“可信业务操作”分开：模型只能提出引用，可信编排器根据已授权的业务台账构造 `ActionIntent`，先在 PostgreSQL 里登记，再由后续派发器调用供应商。当前实现只包含账本和合成测试双，**不会退款、补发、发邮件或访问真实供应商**。

## 记录什么

`aftercare_agent.actions.ActionIntent` 包含租户、Case、订单、稳定 `action_id`、动作类型、跨工单 `business_key`、请求 `idempotency_key`、规范化参数 SHA-256、最小货币单位金额、ISO-4217 三字母币种和可选供应商幂等键。金额作为十进制字符串保存，禁止浮点数和前导零；原始参数正文不放在账本中，正文应放在受控的业务存储并由摘要绑定。

`business_key` 表示业务义务，而不是某个 Run。例如同一支付退款义务从两个 Case 被重新发现时，两个请求必须拿到同一 Action 行。数据库对 `(tenant_id, business_key)` 和 `(tenant_id, idempotency_key)` 都有唯一约束；同键参数、金额、币种、订单或供应商键变化会返回 `CONFLICT`，不会新建第二个动作。

## 状态和 fencing

```text
RESERVED ──> REQUESTED ──> CONFIRMED
     │             ├──────> UNKNOWN ──> CONFIRMED
     └────────────> FAILED       └────> FAILED
```

- `RESERVED`：业务动作已登记和授权，尚未请求供应商。
- `REQUESTED`：可信派发器已提交请求；网络响应还可能丢失。
- `UNKNOWN`：结果不确定，必须保留原 Action 和额度，按同一供应商幂等键核对；不能创建新 Action 盲目重做。
- `CONFIRMED`：需要供应商回执引用和回执摘要。
- `FAILED`：需要受控的失败代码；失败不是“可以随便换键重试”的授权。

`ActionRepository.mark_requested()` 和 `mark_result()` 必须拿当前 `ExecutionClaim`。仓储按 Case → Run 锁顺序核验 owner、lease 和 fencing token；过期 Worker 或被接管的旧 token 返回 `LEASE_LOST`。结果更新也写入当时的 fencing token，便于审计。网络调用必须在事务外完成，回执再以短事务写回。

终态重放是幂等的：相同状态和相同回执身份返回原行；同一 Action 的不同终态或不同回执身份返回 `CONFLICT`。`UNKNOWN` 到 `CONFIRMED/FAILED` 是对账路径，不存在 `UNKNOWN → REQUESTED` 的盲目重发边。

## 最小调用形态

```python
intent = ActionIntent(
    tenant_id="tenant-a", case_id="case-1", order_id="order-1",
    action_id="action-refund-1", action_type="refund",
    business_key="payment:p1:refund:r1", idempotency_key="case-request-1",
    parameters_sha256="…64 个小写十六进制字符…",
    amount_minor="2500", currency="USD",
    provider_idempotency_key="refund:r1",
)
reservation = ledger.reserve(connection, intent, claim=current_claim)
ledger.mark_requested(connection, reservation.action.action_id, current_claim)
# 提交事务后调用供应商；响应丢失则：
ledger.mark_result(connection, reservation.action.action_id, current_claim, state="UNKNOWN")
```

`reserve()` 返回 `ActionReservation(action, replayed)`。跨 Case 复用业务键时，返回第一次登记的 Action；调用方应把它当作引用，而不是修改该行的 Case/订单字段。B-01 还没有支付账户聚合、退款余额预留、审批和真实供应商对账；这些是 B-02/B-03 的门禁，不能由本表的唯一键代替。

## 迁移与验证

表结构位于 [006_actions.sql](../aftercare_agent/persistence/migrations/006_actions.sql)，属于显式 PostgreSQL 迁移；金额、状态、唯一键和确认/失败字段关系由数据库 CHECK 约束兜底。`ActionRepository` 位于 [persistence/actions.py](../aftercare_agent/persistence/actions.py)。

`tests/persistence/test_actions.py` 使用仅返回固定回执的 `SyntheticProvider`，覆盖：同键重放、跨 Case 同义务去重、金额/参数冲突、`UNKNOWN` 占用原 Action、确认结果重放，以及旧 Worker fencing 被拒绝。测试没有网络、供应商密钥或生产数据；未设置 `DATABASE_URL` 时安全跳过，必须在临时 PostgreSQL 上运行后才能声称通过 B-01 的持久化验收。
