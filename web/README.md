# Aftercare 运营工作台（A3-03-a）

React + TypeScript + Vite 的最小运营工作台，用于在本地/开发环境查看并决定人工
Review 与 Approval。它只调用控制面只读查询与决定端点，不持有任何业务权威，也不
直接接触数据库。

## 它能做什么

- 按认证身份列出可访问工单（后端按数据库 `CaseGrant` 收紧；合成身份仅限本租户）
- 查看工单详情：状态、可见权限、Run 状态与等待绑定
- 查看并决定该工单下待处理的 Review（继续 / 取消）与 Approval（批准 / 拒绝）
- 查看并管理该工单的访问授权：列出全部授权行（含已撤销与已过期历史）、授予或替换某个
  主体的权限、按 revision 撤销
- 查看工单事件时间线，并默认按 SSE 实时订阅（游标续传 + 断线重连 + 去重），可随时暂停改为一次性回放

决定使用 `Idempotency-Key`，重复点击返回同一条不可变决定；决定人永远取自认证主体，
不来自请求体。当前身份缺少 `review:decide` / `approval:decide` 时，界面会禁用决定按钮。

## 本地运行

后端（需要 PostgreSQL）：

```powershell
$env:DATABASE_URL = "postgresql://aftercare:local-only-aftercare@127.0.0.1:55433/aftercare"
$env:AFTERCARE_ALLOW_SYNTHETIC_IDENTITY = "1"   # 仅本地/测试
uv run --locked uvicorn aftercare_agent.api.app:app --host 127.0.0.1 --port 8000
```

前端：

```powershell
cd web
pnpm install --frozen-lockfile
pnpm dev        # http://127.0.0.1:5173
```

Vite 开发服务器把 `/api/*` 代理到 `http://127.0.0.1:8000`（见 `vite.config.ts`），
因此浏览器不需要跨域配置。页面左上角的租户/操作员输入会作为合成身份 Header 发送；
这是显式的测试入口，未开启 `AFTERCARE_ALLOW_SYNTHETIC_IDENTITY` 时后端返回 `401`，
一旦配置真实 OIDC verifier 合成身份会被强制关闭。

质量检查：

```powershell
pnpm test        # vitest：SSE 帧解析、游标续传、去重封顶与退避
pnpm typecheck   # tsc --noEmit（strict）
pnpm build       # 类型检查 + 生产构建到 dist/
```

事件订阅不使用 `EventSource`（浏览器无法为它附加自定义 Header），而是用
`fetch` + `ReadableStream` 增量解析 SSE 帧，并用最后看到的 `case_seq` 在重连后续订；
`401`/`403` 视为终止错误而不是重试。细节见 [事件文档](../docs/events.md#工作台实时订阅a3-03-b)。

## 工单访问管理（D-01-05 / D-01-06）

「访问管理」面板消费 `GET/POST /v1/cases/{case_id}/grants` 与撤销端点（见
[API 与认证边界](../docs/api-auth.md#case-授权管理面d-01-05)）：

- 可授予权限的复选框只列出**服务端闭集 ∩ 当前身份自己的权限**；缺少的权限会以一行提示
  说明无法授出，因为服务端同样拒绝授出调用者没有的权限。
- 首次授权不发送 `expected_revision`；「替换 / 重新授权」会带上面板上读到的 revision，
  重放或用过期 revision 会得到 `409`，界面照实显示错误而不会静默重试。
- 撤销按钮携带同一 revision；行不会被删除，而是留下 `revoked_at`/`revoked_by` 供审计。
- 面板是否出现由服务端决定：读到 `403` 时静默隐藏，不显示操作员无法处理的错误。真实
  Bearer 身份的 Case 投影只包含其授权与 Token scope 的交集（不含 `grant:read`），所以
  不能用投影判断能不能管理。
- 每次授予、替换与撤销都由服务端在同一事务写入 `case_grant.granted` / `case_grant.revoked`
  事件，因此时间线本身就是访问变更的审计轨迹。

已知边界：工单队列只列出当前身份**已有授权**的工单，所以真实访问管理员暂时只能管理自己
本来就参与处理的工单；"列出租户内任意工单并接管"需要服务端提供面向 `grant:read` 的发现
能力，属于后续切片。

## 边界

- 这是开发者工作台，不是生产管理面：没有登录页、没有 RLS；访问管理只覆盖单个工单，
  没有租户级的授权总览。
- 不显示租户、租约持有人、fencing token、证据/参数摘要或策略版本等内部字段。
- 只读查询与决定都不触发任何真实退款、补发或外发通知。
