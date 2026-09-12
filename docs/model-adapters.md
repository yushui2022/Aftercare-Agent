# Responses 模型适配边界（A3-01）

`aftercare_agent.model_adapters.responses` 只负责 Responses 的 wire boundary：
`normalize_response()` 解析完整响应，`ResponsesAdapter` 把一个已配置的 provider
client 约束为 `store=false` 的单次调用。模块不读取 API key、不持有数据库事务，也不执行
任何工具。

解析结果同时提供渲染文本、经过白名单和参数门控的本地 `ToolCall`、原生 JSON，以及
`web_search_call`/`file_search_call` 等 provider 托管工具事件。托管工具不会伪装成
Aftercare 的本地动作意图；它们必须由上层决定是否允许、如何计费和如何审计。

未知 output 类型、未知内容块、非 JSON 对象参数、缺失 call_id/name 或不在
`allowed_tools` 中的工具都会拒绝。通过解析不等于授权执行：Runtime 仍需用租约、
Case scope、工具 schema、预算和 Action Ledger 再次门控；模型不能填写租户、Case、
订单或供应商凭证。

`ResponsesAdapter.complete()` 接收 `ResponsesRequest`，只把请求模型、输入、严格工具
定义和 `store=false` 交给 client 的 `responses.create()`。网络错误由
`normalize_error()` 统一脱敏并标注是否可重试；实际退避、预算扣减、检查点和原生响应
持久化由 Worker 上层负责。这样 provider SDK 可以替换，Runtime 不必理解供应商私有
异常或凭证。

当前适配器仍是 A3-01 的边界实现，不代表已经接入真实模型、SSE、沙箱或业务动作。

`model_adapters.budget.ModelUsageBudget` 可在 Worker 检查点之外做一次明确的 usage
结算：输入 token、输出 token 和 micro-USD 均用整数，`ModelPricing` 只允许非负单价。
provider 响应通过 schema 校验后才能扣减；任一维度超额返回 `BUDGET_EXHAUSTED`，不会
静默截断或把浮点价格写入账本。真实生产仍需把预算扣减与 Run checkpoint/租约放进同一
持久事务，并按供应商账单校准价格。
