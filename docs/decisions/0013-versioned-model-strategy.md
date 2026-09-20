# ADR-0013：模型策略按 Run 版本化，禁止静默漂移

状态：接受；适用于当前 Aftercare 开源版和真实部署路线。

## 决策

模型 provider、模型标识、策略配置、业务策略版本和工具 schema 组成一份
`ModelStrategy`。新 Run 创建时把这组身份写入 checkpoint。恢复已有 Run 时，当前部署的
`config_version` 必须与 checkpoint 一致；不一致就在任何模型调用前路由为
`model_strategy_changed → REVIEW`。

普通 Review `CONTINUE` 和预算/deadline `review:override` 都不能绕过策略漂移。策略切换必须
由后续独立的迁移入口完成，并记录旧 Run、新策略版本、操作者、理由和生效边界。当前版本
策略迁移 API 由 [ADR-0014](0014-strategy-migration-control-plane.md) 单独定义，不把策略切换塞进 Review override。

## 原因

仅从进程环境读取 `AFTERCARE_MODEL` 会让滚动部署悄悄改变尚未完成 Run 的语义。模型名称相同
也不能证明提示、工具 schema、业务策略或供应商端配置相同。证据门控系统必须能回答某个结论
当时使用了哪套策略，并能在版本不一致时停下来让人工或迁移控制面处理。

## 运行边界

- `ModelStrategy` 只描述配置身份，不拥有数据库连接、EGM 写权限或业务动作权限。
- 新 Run 可以使用新策略；已有 Run 必须继续使用原配置，或先经过显式迁移。
- `model_strategy_changed` 是安全路由，不是模型错误，也不是预算耗尽；它不能通过加钱继续。
- provider 仍然可替换；JEV 不进入 Aftercare 或 EGM 主线。

## 后续验收

独立的策略切换任务需要提供版本迁移/回滚 API、权限、审计事件、灰度范围、失败恢复、成本
与延迟观测，并用确定性规则、人工参考和至少一个替代 provider 做 shadow 对照。在此之前，
部署只允许通过保持 `AFTERCARE_MODEL_CONFIG_VERSION` 不变来滚动更新。
