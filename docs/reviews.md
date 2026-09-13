# Human Review 持久化边界

调查评估的 `HUMAN_REVIEW` 不是一个可以被普通 Worker 领取的执行状态，而是需要可信人工决定的暂停点。`ReviewRequest` 由宿主根据完整 assessment 创建，携带 `tenant_id/case_id/run_id`、输入版本、原因、策略版本和证据摘要；模型只能影响证据引用，不能选择 reviewer 或最终状态。

## 状态与事务

Review Run 先由可信编排器置为 `REVIEW`，再在同一短事务内写入 `aftercare_reviews`。`ReviewRepository` 始终先锁 Case，再锁 Run 和 Review：

```text
REVIEW + pending request
        ├─ CONTINUE → READY → durable queue → Worker claim
        └─ CANCEL   → CANCELLED（不入队）
```

决定使用 `(tenant_id, review_id)` 和决定幂等键去重；相同决定完整重放返回原记录，换 reviewer、决定或 key 返回 `CONFLICT`。申请人不能自审，输入版本变化会使决定失败。`resolve_review()` 只处理已经记录的决定，适合宿主在提交结果不确定时安全重试。

Review 的 `CONTINUE` 只恢复执行权，不授予退款或其他外部动作权限。Action 仍须独立经过参数摘要、审批策略和 provider 幂等键检查；`CANCEL` 不会把旧 Action 变成可派发状态。

当前实现是可信 PostgreSQL 运行时边界，并提供显式合成身份下的 operator 读取/决定 API；JWT/JWKS Bearer 验证和最小 PostgreSQL CaseGrant 已接入。CaseGrant 在短事务内按 tenant/subject/case 锁定，Token case_ids 仅作收窄；尚未接人工工作台、真实 IdP 权限映射、供应商连接器或生产通知通道。
