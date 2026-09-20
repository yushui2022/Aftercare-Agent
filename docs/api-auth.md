# A1-02 API 与认证边界

当前提供一个最小 FastAPI HTTP 适配器：

- `POST /v1/cases`：受理一个 `OpenCaseInput`，要求 `Idempotency-Key`。
- `GET /v1/cases`：列出当前身份可访问的工单，按创建时间倒序，使用 keyset 游标分页。
- `GET /v1/cases/{case_id}`：读取工单头与其 Run 投影。
- `GET /v1/cases/{case_id}/runs/{run_id}`：按认证租户读取 Run。
- `GET /v1/cases/{case_id}/reviews`：列出该工单范围内的人工 Review。
- `GET /v1/cases/{case_id}/reviews/{review_id}`：读取工单范围内的人工 Review。
- `POST /v1/cases/{case_id}/reviews/{review_id}/decision`：提交 Review 决定。
- `GET /v1/cases/{case_id}/approvals`：列出该工单范围内的审批记录。
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

容器探针使用 `/healthz`（仅表示进程存活）和 `/readyz`（检查数据库连通性及当前
Aftercare migration 版本/checksum）。就绪检查失败时返回 `503`。deployment profile
使用独立 `aftercare-migrate` Job，并令 API/Worker 设置 `AFTERCARE_AUTO_MIGRATE=0`；
完整身份与启动顺序见 [deployment profile](deployment.md)。探针不替代连接池容量测量或
真实 OIDC 授权。

## 控制面 Review/Approval

控制面路由只接受决定本身、可选的理由和（仅限有额外权限时的）预算 override。请求体使用 `extra="forbid"`，因此不能携带
`tenant_id`、`case_id`、`reviewer`、`approver` 或其他权限字段。租户来自认证上下文，工单
来自路径并经过 `AuthContext.require_case()`，执行人的主体来自认证上下文的 `subject_id`：

```json
{"decision": "CONTINUE", "decision_reason": "证据已核对"}
```

预算耗尽或 deadline 失效的 Run 不能用普通 `CONTINUE` 绕过门禁。持有额外
`review:override` 的操作员可以在同一次决定中提交单调增加的 checkpoint 版本、模型/工具/成本
预算和最多 7 天的 deadline 延长，例如：

```json
{"decision":"CONTINUE","decision_reason":"主管批准受控重试",
 "override":{"checkpoint_version":7,"model_calls_add":2,"tool_calls_add":1,
 "cost_microusd_add":5000,"deadline_extension_seconds":3600,
 "reason":"承运商补充回执预计在一小时内到达"}}
```

override、更新后的 checkpoint、Review 决定和 `REVIEW→READY` 状态转换在一个 PostgreSQL
事务中提交，并写入不可变的 `aftercare_review_overrides` 审计表及 `review.decided` 事件快照。
checkpoint 版本变化、缺少对应的耗尽维度或没有 `review:override` 权限都会 fail closed；当前只
支持预算/deadline 增量，模型策略切换通过独立的 `strategy:migrate` 入口处理；迁移本身不
替代后续 Review 决定。

Review 需要 `review:read`/`review:decide`，预算 override 另外需要 `review:override`，策略迁移另外需要 `strategy:migrate`，审批需要 `approval:read`/`approval:decide`。
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

## 工单发现与运营工作台（A3-03-a）

此前控制面只能按已知 ID 读取，操作员无法"发现自己该处理的工单"。本轮补上发现能力：

- `GET /v1/cases` 需要租户级 `case:read`。真实身份的分页恒由活动
  `aftercare_case_grants` 行收紧：被撤销或过期的授权不会出现在结果里，Token 的
  `case_ids` 只能进一步收窄（空集合直接返回空页）。显式开启的合成身份只看自己租户，
  不跨租户。
- 分页是 `(created_at, case_id)` keyset 游标：整页返回时给出 `next_created_at` /
  `next_case_id`，不满一页表示到底；`limit` 上限 200，只给一半游标返回 `400`。
- `GET /v1/cases/{case_id}` 返回工单头与 Run 投影。Run 投影只包含 `run_id`、`state`、
  `input_version`、等待绑定与租约到期时间；不返回 `tenant_id`、`lease_owner`、
  `fencing_token`。
- `GET /v1/cases/{case_id}/reviews` 与 `.../approvals` 分别需要 `review:read` 与
  `approval:read`，并复用同一 operator projection（不含证据/参数摘要、策略版本、等待绑定）。
- 工单不存在或不属于调用方租户时统一返回 `403`，与单资源端点保持一致；合成身份也不再
  把"未知工单"表现为空集合。

`web/` 下的 React + TypeScript + Vite 工作台消费这些端点：列出工单、查看详情与事件，
并以认证主体提交 Review/Approval 决定。它是开发者工作台，不含登录页、权限管理 UI 或
生产认证；页面上的租户/操作员输入只是显式的合成身份测试入口。

## JWT/JWKS Bearer 验证（D-01-01/02）

当同时配置下列变量时，默认 App 使用 `JwtJwksVerifier` 读取标准
`Authorization: Bearer <JWT>`：

```powershell
$env:AFTERCARE_OIDC_ISSUER = "https://idp.example/"
$env:AFTERCARE_OIDC_AUDIENCE = "aftercare-api"
$env:AFTERCARE_OIDC_JWKS_URL = "https://idp.example/.well-known/jwks.json"
$env:AFTERCARE_OIDC_REQUIRE_CASE_IDS = "0"  # token 上界可选；CaseGrant 仍始终必需
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

这仍不是完整企业授权：`015_case_grants.sql` 和 `CaseGrantRepository` 已提供最小的
数据库授权切片，按 `(tenant_id, subject_id, case_id)` 保存权限、revision、有效期和撤销
审计。每个 Case 路由在自己的短事务中锁定活动行；没有行、已撤销或已过期均返回 `403`。
Token 的 `case_ids` 只能进一步收窄数据库结果，Token scope 与数据库权限取交集，不能凭
声明新增权限。新建 Case 时创建者的 `case:read` grant 与受理事务一起写入；幂等重放不会
为另一主体自动补授权。授权管理 HTTP 已由 D-01-05 一节补齐，第一次有了把工单移交给他人的
受审计入口；管理 UI、RLS、实时授权事件和真实 IdP 演练仍然没有（Token 撤销/introspection
见下一节）。长寿命 Token、把全租户权限映射给普通用户都不应直接用于生产。
需要人工控制面时，仍须在权限映射和 CaseGrant 中显式授予 `review:*`/`approval:*` scope。

### JWT + CaseGrant 闭环验收（D-01-09）

仓库现在有一条真实边界回归 `tests/test_oidc_casegrant_acceptance.py`：它使用生产
`JwtJwksVerifier` 验证实际 RSA 签名 JWT，再由 API 在 PostgreSQL 中解析 CaseGrant。
同一条链路依次证明“没有授权返回 `403`、授予 `case:read` 后返回 `200`、撤销后再次返回
`403`”；Token 的 `case_ids` 仍只是数据库授权的收窄条件。JWKS 使用确定性的测试传输，
所以这条回归不连接外部 IdP，也不把合成身份当作替代路径。接入目标 IdP 前，可在临时
PostgreSQL 上运行：

```powershell
$env:DATABASE_URL = "postgresql://..."
uv run --locked pytest tests/test_oidc_casegrant_acceptance.py -q
```

这证明的是应用边界和数据库授权闭环，不等于目标 IdP 的密钥轮换、网络策略、introspection、
RLS 或生产权限运营已经验收；这些仍需在选定部署环境中单独留证。运行依赖为
`PyJWT[crypto]` 与 `httpx`。

## Token 撤销与 Introspection（D-01-04）

本地验签只能证明 Token 由签发方签发，不能证明它现在仍然有效：被撤销的 Token 在自然过期
前会一直通过验签。本轮补上 provider-neutral 的 RFC 7662 撤销判定：

```powershell
$env:AFTERCARE_OIDC_INTROSPECTION_URL = "https://idp.example/oauth2/introspect"
$env:AFTERCARE_OIDC_INTROSPECTION_CLIENT_ID = "aftercare-api"
$env:AFTERCARE_OIDC_INTROSPECTION_CLIENT_SECRET = "<provider secret>"
```

部署环境也可只设置 `AFTERCARE_OIDC_INTROSPECTION_CLIENT_SECRET_FILE`，令其指向
secret manager 挂载的单行文件；不能同时设置明文变量与 `_FILE` 变量。

- 这三个变量必须与 OIDC 三元组同时配置。URL 必须是静态 HTTPS（不接受 query 或
  fragment），凭据缺失或 URL 不合法时进程启动即失败，不会静默降级成"只验签"。
- `HttpTokenIntrospector` 用 `application/x-www-form-urlencoded` POST `token` 与
  `token_type_hint=access_token`。凭据只存在于注入的 `httpx.Client`（`BasicAuth`），
  既不进入契约模型也不进 `repr`，Token 本身不写日志。
- 响应按 RFC 7662 读取：`active` 缺失或不是布尔值即拒绝；`sub`、`tenant_id`、`exp` 若
  存在则必须类型正确、且与已验证 Token 一致。字段集由签发方决定，未知字段被忽略。
- `CachedIntrospector` 以 Token 的 SHA-256 指纹为键，有界且带 TTL，只缓存 TTL 内的判定，
  已过期的判定不缓存；失败也不缓存，下一次仍会问签发方。
- `TokenAccessGuard` 在验签之后执行判定。任何失败——`active=false`、`sub`/`tenant_id`
  不一致、判定已过期、传输/解析/超大响应错误或未预期异常——统一 fail-closed 为
  `401 unauthenticated`，绝不回退到合成身份。

本轮撤销相关测试全部离线（`httpx.MockTransport` 与 `TestClient`，认证边界之前不访问
数据库）。仍未验证：真实 IdP 的 introspection 端点与凭据轮换、IdP 侧限流和超时行为、
RLS、权限管理 UI 与生产演练。撤销判定不替代 CaseGrant：它回答"Token 是否仍然活跃"，
不回答"这个主体能不能操作这个 Case"。

## Case 授权管理面（D-01-05）

此前 `CaseGrantRepository.grant()/revoke()` 只有一个调用点：受理新 Case 时给创建者授
`case:read`。于是租户内没有任何人能改动授权，最普通的运营动作——把一件工单移交给另一个
操作员或主管——无法完成。本轮开放管理面（决策见
[ADR-0004](decisions/0004-case-grant-administration.md)）：

| 路由 | 所需权限 | 行为 |
| --- | --- | --- |
| `GET /v1/cases/{case_id}/grants` | 租户级 `grant:read` | 列出该 Case 的全部授权行（含已撤销），按 `subject_id` 升序，`limit` 上限 200 |
| `POST /v1/cases/{case_id}/grants` | 租户级 `grant:admin` | 授予或替换一个主体的授权 |
| `POST /v1/cases/{case_id}/grants/{subject_id}/revoke` | 租户级 `grant:admin` | 撤销授权；行保留用于审计与 revision 校验 |

```json
{"subject_id": "operator-7", "permissions": ["case:read", "review:decide"],
 "expires_at": "2026-09-16T00:00:00+00:00", "expected_revision": 1}
```

管理权是租户级角色，管理面**不**解析调用者自己的 `CaseGrant`：若管理动作也要求先持有该
Case 的 grant，第一条授权永远发不出去。防止它变成提权通道的是另外两条不变量：

- 闭集：可授予权限只有 `case:read`、`review:read`、`review:decide`、`review:override`、`strategy:migrate`、`approval:read`、
  `approval:decide`。`case:create` 与 `grant:*` 故意不在其中——一张 Case 行不能放大成
  租户级权限；闭集之外的字符串返回 `400` 且不落库。
- 委派上限：不能授出调用者自己没有的权限，越界返回 `403`，因此管理面无法自我提权。需要
  授出 `approval:decide` 的管理员必须自己先持有该 scope，角色分配属于 IdP 而不是本 API。

请求体禁止 `tenant_id`、`case_id`、`granted_by`、`revoked_by`、`revision`；租户、Case 和
执行人取自认证上下文，响应投影也不返回租户。未知 Case 与其他租户的 Case 都返回 `403`，
与既有单资源端点一致。`expires_at` 必须带时区且晚于数据库 `clock_timestamp()`，否则返回
`400`。替换与撤销都必须携带读到的 `expected_revision`；重放或用过期 revision 返回 `409`，
避免一次超时重试把同一行静默改写第二次。

每次变更在同一个事务内追加 `case_grant.granted` / `case_grant.revoked` 事件和完整快照，
因此授权历史可以直接按 Case 事件流审计；这两类事件没有 `run_id`，授权变更不是 Run 推进的
一部分。管理面改变的是权威状态而不是调用者的即时权限：`CaseGrant` 仍是“使用时”的权威，
撤销不会中断已经在途的请求。`web/` 工作台已经消费这三个端点（单工单访问管理面板，见
[工作台说明](../web/README.md#工单访问管理d-01-05--d-01-06--d-01-07)）：面板只在服务端
允许时出现，每次变更后时间线会按事件流自然刷新。响应携带 `delegable` 与 `can_administer`
两个字段描述**调用者**而不是 Case，理由见
[ADR-0005](decisions/0005-access-administration-discovery.md) 决策第 6 条：Case 投影是
与 grant 的交集，在该 Case 上没有 grant 的租户管理员在投影里看不到 `grant:admin`，客户端
无法据此判断表单是否该出现。仍未完成：租户级授权总览、RLS、真实 IdP 权限映射与演练。

## 访问管理发现（D-01-07）

移交需要先能命名目标工单，而 `GET /v1/cases` 是数据面入口——每行都来自活动 `CaseGrant`，
所以不参与任何工单的访问管理员看到的是空队列，手上虽有 `grant:admin` 却无从移交。本切片
开放控制面发现入口（决策见
[ADR-0005](decisions/0005-access-administration-discovery.md)）：

| 路由 | 所需权限 | 行为 |
| --- | --- | --- |
| `GET /v1/administration/cases` | 租户级 `grant:read` | 列出本租户工单的标识与进度，按创建时间/`case_id` 的 keyset 游标分页，`limit` 上限 200 |

它返回 `case_id`、`order_id`、`status`、`version`、`created_at`，**没有** Case 内容、
Run、Review、Approval 或事件流：这些仍逐工单判定 `CaseGrant`，`grant:read` 不参与它们的
授权。换句话说，本切片放宽的是控制面，数据面没有放宽——如果 `grant:read` 能打开 Case
内容，它就等价于“租户管理员对所有客户数据的常驻读权”，与逐工单 grant 收敛权限的立场相反。

行的 `permissions` 只报调用者自己的租户级管理 scope（`grant:read`、`grant:admin`），
刻意不复用 `_case_summary()` 的逐 Case 投影：在该 Case 上没有 grant 的调用者本来就没有
任何 Case 权限，报出 `case:read` 会让工作台去打开下一个请求必然拒绝的内容。`grant:read`
因此必须只发给真正的访问管理员：工单编号、订单号与状态对这个 scope 完全可见。

分页参数与响应契约与数据面工单列表一致（`status`、`limit`、`after_created_at`、
`after_case_id`、`next_created_at`、`next_case_id`），便于工作台共用同一套分页代码；
但两侧游标不共享，因为判定不同、行集合也不同。token 的 `case_ids` 仍是上界：它只收窄这个
清单，不会放宽任何读取。`web/` 工作台把结果渲染成"本租户其他工单"区块，行内标注“仅可
管理访问”，选中时只显示授权面板与说明，不调用任何内容路由。
