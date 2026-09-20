# Action Ledger 与审批门禁（B-01 / B-02-01）

Action Ledger 是 Aftercare 对外部业务动作的持久化边界。它把“模型建议/工具调用”与“可信业务操作”分开：模型只能提出引用，可信编排器根据业务台账构造 `ActionIntent`，先在 PostgreSQL 里登记，再由后续派发器调用供应商。当前已加入 B-02-01 的持久化审批门禁，但**不会退款、补发、发邮件或访问真实供应商**。

## 记录什么

`aftercare_agent.actions.ActionIntent` 包含租户、Case、订单、稳定 `action_id`、动作类型、跨工单 `business_key`、请求 `idempotency_key`、规范化参数 SHA-256、最小货币单位金额、ISO-4217 三字母币种、可选供应商幂等键和默认开启的 `approval_required`。金额作为十进制字符串保存，禁止浮点数和前导零；原始参数正文不放在账本中，正文应放在受控的业务存储并由摘要绑定。

`business_key` 表示业务义务，而不是某个 Run。例如同一支付退款义务从两个 Case 被重新发现时，两个请求必须拿到同一 Action 行。数据库对 `(tenant_id, business_key)` 和 `(tenant_id, idempotency_key)` 都有唯一约束；同键参数、金额、币种、订单或供应商键变化会返回 `CONFLICT`，不会新建第二个动作。

## 状态和 fencing

```text
RESERVED ──> REQUESTED ──> CONFIRMED
     │             ├──────> UNKNOWN ──> CONFIRMED
     └────────────> FAILED       └────> FAILED
```

- `RESERVED`：业务动作已登记，尚未请求供应商；若 `approval_required=true`，此状态不代表已获审批。
- `REQUESTED`：可信派发器已提交请求；网络响应还可能丢失。
- `UNKNOWN`：结果不确定，必须保留原 Action 和额度，按同一供应商幂等键核对；不能创建新 Action 盲目重做。
- `CONFIRMED`：需要供应商回执引用和回执摘要。
- `FAILED`：需要受控的失败代码；失败不是“可以随便换键重试”的授权。

`ActionRepository.mark_requested()` 和 `mark_receipt()` 必须拿当前 `ExecutionClaim`。仓储按 Case → Run → Action → Approval 锁顺序核验 owner、lease、fencing token 和审批；高风险动作缺少匹配的 `APPROVED`、参数摘要或当前策略版本时 fail closed。结果更新也写入当时的 fencing token，便于审计。网络调用必须在事务外完成，回执再以短事务写回。底层 `mark_result()` 只保留为已校验回执的状态机入口，provider 适配器应使用 `mark_receipt()`。

## 审批台账（B-02-01）

`ApprovalRepository.request()` 在锁定 Case → Action 后创建一条 `PENDING` 记录。记录绑定 `tenant_id/case_id/action_id`、Action 参数摘要、`policy_version`、可信 `requested_by` 和数据库时钟校验的 `expires_at`；同一 Action 不能有第二条审批。需要唤醒 Agent 时，`ApprovalRequest` 还绑定已有的 `run_id/wait_id/wait_generation`，并要求 Wait 的 `kind=approval`、`correlation_key=approval_id`、`condition_version=action digest`。`decide()` 只接受可信调用方传入的批准人，禁止申请人自批；相同决定幂等重放，换决定或换决定键返回 `CONFLICT`。

派发器必须在同一短事务内调用 `mark_requested(..., approval_id=..., policy_version=...)`。它重新锁定当前审批并检查 APPROVED、未过期、精确 Action 摘要和调用方当前策略版本，然后才允许 `RESERVED → REQUESTED`。`ApprovalRepository.expire()` 供定时扫描器显式结算过期 PENDING；派发仍以数据库当前时间再次检查，不依赖扫描器及时运行。

绑定 Wait 的审批决定在同一 Case → Run → Wait → Action → Approval 事务里写入 Inbox，并通过已持有的 Wait 锁结算：批准或拒绝都只唤醒对应代次，重复决定不会产生第二个 wakeup；已经超时/取消的 Wait 不会被迟到审批复活。未绑定 Wait 的审批仍可作为独立台账，适合提前审批。当前已有 operator 读取/决定 API（合成身份或静态 JWKS Bearer），CaseGrant 已提供最小 Case 资源授权；支付聚合、真实 IdP/RLS 和供应商回执仍未完成。审批正文以数据库行作为权威，不信任模型提交的 approver、金额或自由事实文本。

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
approval, _ = approvals.request(connection, ApprovalRequest(
    tenant_id=intent.tenant_id, case_id=intent.case_id,
    approval_id="approval-refund-1", action_id=intent.action_id,
    action_parameters_sha256=intent.parameters_sha256,
    policy_version="refund-v1", requested_by="agent-service",
    expires_at=expiry_from_trusted_policy,
))
# 另一个具备审批权限的服务完成 decide() 并提交后：
requested = ledger.mark_requested(
    connection, reservation.action.action_id, current_claim,
    approval_id=approval.approval_id, policy_version="refund-v1",
)
# 提交事务后在事务外调用 ActionProvider；响应丢失则构造 UNKNOWN 回执：
receipt = provider.request(requested)
ledger.mark_receipt(connection, receipt, current_claim)
```

`reserve()` 返回 `ActionReservation(action, replayed)`。跨 Case 复用业务键时，返回第一次登记的 Action；调用方应把它当作引用，而不是修改该行的 Case/订单字段。`SyntheticActionProvider` 仅用于 local/integration profile，支持稳定回执和 UNKNOWN 后的 `lookup()`，不产生外部副作用。支付账户聚合、退款余额预留、审批等待联动和真实供应商对账仍是后续工作，不能由本表的唯一键代替。

## 迁移与验证

表结构位于 [006_actions.sql](../aftercare_agent/persistence/migrations/006_actions.sql) 和 [010_approvals.sql](../aftercare_agent/persistence/migrations/010_approvals.sql)，属于显式 PostgreSQL 迁移；金额、状态、唯一键、审批决定和确认/失败字段关系由数据库 CHECK 约束兜底。`ActionRepository` 和 `ApprovalRepository` 位于 `aftercare_agent/persistence/`。

`tests/persistence/test_actions.py` 和 `test_approvals.py` 使用仅返回固定回执的测试双，覆盖：同键重放、跨 Case 同义务去重、金额/参数冲突、审批缺失 fail closed、审批决定幂等、自批拒绝、过期拒绝、策略/摘要重查、`UNKNOWN` 占用原 Action、确认结果重放，以及旧 Worker fencing 被拒绝。测试没有网络、供应商密钥或生产数据；未设置 `DATABASE_URL` 时安全跳过，必须在临时 PostgreSQL 上运行后才能声称通过当前持久化验收。
