# 供应商 Webhook 到证据账本

真实订单、物流和客服渠道不能把供应商回调直接写进 Aftercare 证据账本。每个供应商
先在边界适配器中完成自己的验签和字段映射，再产生 Aftercare 规定的规范化来源事件。
这样供应商的签名算法可以替换，业务层仍只接收同一套租户、来源、工具、时间和幂等契约。

```text
供应商 HTTP 请求
    │  原始 bytes + 供应商验签
    ▼
渠道适配器（不持有业务数据库连接）
    │  固定 key_id / delivery_id / timestamps / canonical JSON
    ▼
HmacSourceEventVerifier
    │  tenant/source/tool/schema/authority/time-window
    ▼
AuthenticatedSourceIngress
    │  RunScope、artifact、观察账本、EGM operation
    ▼
确定性证据门与人工 Review/Approval
```

## 规范化输入

适配器应构造 `aftercare_agent.connectors.source_auth.SignedSourceRequest`，而不是把
供应商的 JSON 对象直接传给连接器桥：

| 字段 | 规则 |
|---|---|
| `key_id` | 由部署配置映射到固定 tenant、source 和 tool；不能从请求正文读取 |
| `delivery_id` | 稳定的供应商事件 ID；供应商重试必须复用它，不能每次生成随机 UUID |
| `issued_at` | 供应商签名或投递时间；必须在部署配置的时间窗口内 |
| `observed_at` | 事实实际发生/被供应商观察到的时间；不能用 Aftercare 当前时间代替 |
| `signature` | 规范化 HMAC 的 `v1=<64 hex>` 结果；供应商原生签名先在适配器内验证 |
| `content` | canonical JSON bytes；禁止重复键、非标准 JSON 常量和未授权 authority 字段 |

适配器必须先对供应商原始 bytes 验签，再解析 JSON。解析后重新序列化再验签会丢失
空格、Unicode 或字段顺序信息，不能作为原始签名的替代。规范化正文只能包含对应
工具的业务事实和 schema，例如订单、物流或买方陈述；`tenant_id`、`source_id`、
`source_event_id`、`subject_id`、`tool` 等 authority 字段由部署映射提供，不能由供应商
正文或模型选择。生成 canonical bytes 时使用公开的
`aftercare_agent.connectors.canonical_source_json()`，不要在每个适配器里复制 JSON
序列化选项。

## 接入顺序

1. 在供应商适配器中验证原始签名、时间窗口和事件类型；拒绝未知事件，不要把它们转成
   一个“空证据”。
2. 将供应商原生事件 ID 映射成稳定 `delivery_id`，并把供应商事实时间映射成
   `observed_at`。回调接收时间只用于运维日志，不是事实新鲜度。
3. 根据部署配置选择 `key_id`。一个 key 只能绑定一个租户、来源和工具，轮换时使用
   新 key 并在短暂重叠窗口内同时验证旧 key；不能让请求正文选择身份。
4. 生成 canonical JSON 并构造 `SignedSourceRequest`，交给
   `HmacSourceEventVerifier`。不要绕过 verifier 直接调用 `AuthenticatedSourceIngress`。
5. 使用可信路由得到 `RunScope` 后调用 `AuthenticatedSourceIngress.ingest()`。Scope
   来自已受理的 Case/Run，不能由模型参数、正文或未认证 header 提供。
6. 先持久化来源事件、artifact 和观察 operation，再确认队列/回调。未知提交结果按
   原 `delivery_id` 重放；不要生成新 ID 盲目重做。

## 重试、冲突和撤回

- 相同 `delivery_id`、相同正文和相同 scope 必须得到幂等重放结果。
- 相同 `delivery_id` 改变正文必须进入冲突，不得覆盖已记录事实。
- 不同 ID 但代表同一供应商事实时，适配器必须在边界映射成同一个稳定事件 ID；核心层
  不猜测供应商语义。
- 供应商的删除、取消或更正事件应进入明确的撤回/更正契约，不能通过刷新
  `observed_at` 伪装成新事实。
- 过期事件可以被保存为审计材料，但不能因为模型高置信度而绕过新鲜度门。

## 密钥与测试

生产 secret 由目标平台的 secret manager 注入，适配器不把原始密钥写入日志、artifact
或异常消息。轮换至少要验证：旧 key 在重叠窗口内可用、窗口结束后拒绝、错误 key 不会
改变租户/来源映射、回调重试仍然幂等。

每个供应商适配器至少需要覆盖：原始签名篡改、重复键、未知事件、过期/未来时间、重复
投递、同 ID 改正文、跨租户 scope、供应商更正/撤回和下游提交结果未知。测试应使用合成
payload；真实供应商联调再补平台自身的签名样例和证书轮换证据。

仓库已经提供 `HmacSourceEventVerifier`、`AuthenticatedSourceIngress` 及其 PostgreSQL
幂等/冲突回归。供应商原生验签适配器和真实渠道联调尚未实现；本页是接入契约，不是
某个供应商已经接通的声明。
