# Changelog

所有值得关注的用户可见变化都记录在这里。版本尚未发布时，内容放在
`[Unreleased]`；验证结果和未完成边界以 [项目状态](docs/project-status.md)
为准。

## [Unreleased]

### Added

- 增加 `aftercare_investigation` 调查证据 schema，覆盖订单、物流和买方陈述。
- 增加 Aftercare Case 到 EGM node 的绑定及 revision/operation 投影接缝。
- 增加 `lookup_buyer_message` 开发连接器和证据桥接。
- 增加开源路线、部署 profile、模型 provider 边界和安全发布说明。
- 增加来源 Webhook 规范化接入指南与共享 canonical JSON helper。
- 增加 provider-neutral ActionProvider/ProviderReceipt 回执契约，固定 UNKNOWN、终态
  凭证和回执归属校验。
- 增加无副作用的 `SyntheticActionProvider`，并让 local vertical slice 通过
  `request()`/`mark_receipt()` 验证同一 provider 边界。
- 增加 integration Compose profile：迁移与 runtime 数据库身份分离，包含 runtime
  DDL 拒绝检查、API readiness 和 `aftercare-demo --skip-migrate` smoke。
- CI 增加 integration profile 的真实 Compose smoke，并用退出清理保证临时数据库资源不残留。
- Review 决定对预算耗尽和 deadline 路由增加 fail-closed 检查，避免没有 override 时把同一 Run 无限重放。

### Changed

- JEV 不再属于 Aftercare 或 EGM 的活动产品路径；模型接入保持 provider-neutral。
- 默认路径继续使用确定性证据门、人工协作和 Action Ledger 约束模型建议。

### Validation limits

- 当前离线回归和静态检查已覆盖仓库代码；真实 PostgreSQL Worker 端到端、目标身份、
  真实供应商和生产部署仍需在对应环境验收。
