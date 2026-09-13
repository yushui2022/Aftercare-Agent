# A1-02 API 与认证边界

当前提供一个最小 FastAPI HTTP 适配器：

- `POST /v1/cases`：受理一个 `OpenCaseInput`，要求 `Idempotency-Key`。
- `GET /v1/cases/{case_id}/runs/{run_id}`：按认证租户读取 Run。
- `GET /v1/cases/{case_id}/reviews/{review_id}`：读取工单范围内的人工 Review。
- `POST /v1/cases/{case_id}/reviews/{review_id}/decision`：提交 Review 决定。
- `GET /v1/cases/{case_id}/approvals/{approval_id}`：读取工单范围内的审批记录。
- `POST /v1/cases/{case_id}/approvals/{approval_id}/decision`：提交审批决定。

路由只负责输入解码、认证上下文、错误映射和调用 Repository。Case、Session、Run 的 ID
由服务端生成；请求体不能选择 `tenant_id`、`case_id`、`principal`、`owner` 或 fencing
token。受理幂等和数据库事务由 `AdmissionRepository` 负责。

## 合成身份模式

A1-02 仅提供显式本地/测试身份：

```powershell
$env:DATABASE_URL = "postgresql://..."
$env:AFTERCARE_ALLOW_SYNTHETIC_IDENTITY = "1"
uvicorn aftercare_agent.api.app:app
```

请求使用 `X-Synthetic-Tenant` 和 `X-Synthetic-Subject` 仅用于测试。未设置允许开关时，
即使带有这两个 Header 也返回 `401`；这不是生产认证方案。真实 OIDC/企业身份提供者和
权限映射属于后续生产准入任务。

API 端到端测试覆盖同键重放、同键改参冲突、跨租户读取拒绝和生产模式拒绝合成身份。
测试使用临时 PostgreSQL 与合成数据；不会发送真实业务动作。

容器探针使用 `/healthz`（仅表示进程存活）和 `/readyz`（执行一次数据库连通性检查）。
就绪检查失败时返回 `503`；它不替代迁移策略、连接池健康度或真实 OIDC 授权。

## 控制面 Review/Approval

控制面路由只接受决定本身和可选的理由。请求体使用 `extra="forbid"`，因此不能携带
`tenant_id`、`case_id`、`reviewer`、`approver` 或其他权限字段。租户来自认证上下文，工单
来自路径并经过 `AuthContext.require_case()`，执行人的主体来自认证上下文的 `subject_id`：

```json
{"decision": "CONTINUE", "decision_reason": "证据已核对"}
```

Review 需要 `review:read`/`review:decide`，审批需要 `approval:read`/`approval:decide`。
决定端点必须携带 `Idempotency-Key` Header；它会经过 Identifier 约束校验，并作为底层
Repository 的决定幂等键。相同主体、决定、理由和键的重放返回同一不可变决定；修改其中任意
一项返回 `409`。Repository 会在一个短数据库事务内再次锁定 Case 和门控记录，因而 API
层的预读不能绕过并发、有效期、自审和 Review→READY/CANCELLED 规则。

响应是专门的 operator projection：包含资源 ID、Case、Run 和（审批的）Action、决定、
执行主体（`actor`）和时间；不会返回租户、请求人、证据/参数摘要、策略版本、等待绑定或
决定幂等键等内部门控字段。

找不到记录或路径工单与记录不匹配统一返回 `403`，避免泄露其他工单是否存在；缺少认证是
`401`，缺少权限是 `403`，幂等重放冲突是 `409`，输入模型/标识不合法是 `400/422`。API
只写门控台账、Inbox 唤醒和 Outbox 事件，不在 HTTP 请求内调用真实退款或其他供应商。

合成身份模式会在显式 `AFTERCARE_ALLOW_SYNTHETIC_IDENTITY=1` 时授予本地测试所需的
operator scope；这不是生产授权方案，也不能由请求 Header 自行声明权限。生产部署应将
已验签的 OIDC claims 映射为相应权限后再接入同一组路由。

`auth.oidc.auth_context_from_claims()` 提供真实身份的下一层边界：上游 JWT/JWKS 验签器
传入已验证 claims，Aftercare 再校验 issuer、audience、过期/生效时间、tenant 和 Case
范围，生成不可由请求体覆盖的 `AuthContext`。本项目不在该模块内实现 JWT 验签，也不把
示例 IdP 当作生产配置；部署时必须绑定实际 JWKS、密钥轮换和权限映射。
