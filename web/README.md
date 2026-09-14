# Aftercare 运营工作台（A3-03-a）

React + TypeScript + Vite 的最小运营工作台，用于在本地/开发环境查看并决定人工
Review 与 Approval。它只调用控制面只读查询与决定端点，不持有任何业务权威，也不
直接接触数据库。

## 它能做什么

- 按认证身份列出可访问工单（后端按数据库 `CaseGrant` 收紧；合成身份仅限本租户）
- 查看工单详情：状态、可见权限、Run 状态与等待绑定
- 查看并决定该工单下待处理的 Review（继续 / 取消）与 Approval（批准 / 拒绝）
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

## 边界

- 这是开发者工作台，不是生产管理面：没有登录页、没有 RLS、没有权限管理 UI。
- 不显示租户、租约持有人、fencing token、证据/参数摘要或策略版本等内部字段。
- 只读查询与决定都不触发任何真实退款、补发或外发通知。
