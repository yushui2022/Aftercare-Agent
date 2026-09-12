# A1-02 API 与认证边界

当前提供一个最小 FastAPI HTTP 适配器：

- `POST /v1/cases`：受理一个 `OpenCaseInput`，要求 `Idempotency-Key`。
- `GET /v1/cases/{case_id}/runs/{run_id}`：按认证租户读取 Run。

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

`auth.oidc.auth_context_from_claims()` 提供真实身份的下一层边界：上游 JWT/JWKS 验签器
传入已验证 claims，Aftercare 再校验 issuer、audience、过期/生效时间、tenant 和 Case
范围，生成不可由请求体覆盖的 `AuthContext`。本项目不在该模块内实现 JWT 验签，也不把
示例 IdP 当作生产配置；部署时必须绑定实际 JWKS、密钥轮换和权限映射。
