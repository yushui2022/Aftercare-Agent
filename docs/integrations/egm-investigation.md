# Investigation EGM adapter（A3-02）

`aftercare_agent.investigation.InvestigationEvidenceAdapter` 把调查契约和 EGM
应用层连接起来，但不把 EGM 当作订单、物流或退款的业务权威。

连接器使用可信 `Principal` 和 `SourceGrant` 写入规范化观察。Adapter 从已经校验的
`InvestigationEvidence` 推导 `source_system`、证据类型和 canonical JSON；模型不能提交
租户、Case、订单、来源、时间或自由事实正文。模型只产生 `InvestigationProposal`，由
`assess_investigation()` 对完整授权观察集做确定性评估，缺材料、陈述冲突、过期和未知引用
都会显式保留，评估结果不会授权外部退款或补发。

业务层可用 `persist_observation()` 将已校验观察写入
`aftercare_investigation_observations`，用 `(tenant, case, source_id, source_event_id)`
去重；`persisted_observations()` / `assess_persisted()` 在新 Worker 或进程重启后重载完整
工单账本。撤回会更新结构化列和 JSON 原文的 `revoked_at`，不会刷新原始观察时间。

EGM 的 domain schema 必须显式包含 `order_record`、`logistics_observation` 和
`buyer_statement` 证据类型及对应节点/门控；当前仓库仍固定依赖 EGM 0.6.0 的退款 schema，
因此真实调查 schema 接入需要单独评审和固定 EGM 版本，不能把离线 Adapter 测试误报成生产
调查闭环。上下文读取默认 `include_long_term=False`，长期记忆需要另行证据门控。
