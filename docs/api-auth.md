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

## 合成身份模式（仅本地/测试）

A1-02 仅提供显式本地/测试身份：

```powershell
$env:DATABASE_URL = "postgresql://..."
$env:AFTERCARE_ALLOW_SYNTHETIC_IDENTITY = "1"
uvicorn aftercare_agent.api.app:app
```

请求使用 `X-Synthetic-Tenant` 和 `X-Synthetic-Subject` 仅用于测试。未设置允许开关时，
即使带有这两个 Header 也返回 `401`；这不是生产认证方案。若同时提交 Bearer，Bearer
路径优先，验签失败时绝不会回退到合成 Header；一旦配置真实 OIDC verifier，合成身份
也会被强制关闭。

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

## JWT/JWKS Bearer 验证（D-01-01/02）

当同时配置下列变量时，默认 App 使用 `JwtJwksVerifier` 读取标准
`Authorization: Bearer <JWT>`：

```powershell
$env:AFTERCARE_OIDC_ISSUER = "https://idp.example/"
$env:AFTERCARE_OIDC_AUDIENCE = "aftercare-api"
$env:AFTERCARE_OIDC_JWKS_URL = "https://idp.example/.well-known/jwks.json"
$env:AFTERCARE_OIDC_REQUIRE_CASE_IDS = "1"  # 默认值；只有明确的租户级角色才设为 0
```

JWKS URL 必须是静态 HTTPS 配置，不能由 Token 的 `iss` 或 Header 控制。允许算法默认只
包含 `RS256`/`ES256`（生产应按 IdP 文档进一步收窄），拒绝 `none`、HMAC 混淆、未知
`kid`、非签名用途的 key 和非 `JWT`/`at+jwt` 类型。JWKS 在内存中短期缓存；未知 `kid`
最多触发一次受冷却时间限制的强制刷新，刷新失败或缓存过期时 fail closed，不使用无限期旧
密钥。HTTP 客户端不跟随重定向并限制响应大小；Bearer 不会写入日志。

验签之后还会严格检查 `iss`、`aud`、`sub`、`exp`、`iat`、可选 `nbf`、最大 Token 寿命，
并把 `tenant_id`、`permissions`/空格分隔的 `scope`、`case_ids` 映射为不可由请求体覆盖
的 `AuthContext`。`auth.oidc.auth_context_from_claims()` 仍是纯 claims 校验边界，不能
被误当作 JWT 验签器。

这只是认证切片，不是完整企业授权：Token 中的租户与 Case 声明必须由可信 IdP/授权服务
签发。当前尚未有数据库 `CaseGrant`/RLS，也没有 Token 撤销或 introspection；长寿命 Token、
显式关闭 `AFTERCARE_OIDC_REQUIRE_CASE_IDS` 或把全租户权限映射给普通用户都不应直接用于生产。
需要人工控制面时，仍须在权限映射中显式授予 `review:*`/`approval:*` scope。运行依赖为
`PyJWT[crypto]` 与 `httpx`，密钥轮换、JWKS 可用性、授权映射和审计留痕需在 D-01 后续验收。
