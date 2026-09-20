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

开发连接器提供 `lookup_buyer_message` 作为受限的模型工具入口：每次返回导出中最新的
一条买方陈述，消息 ID、接收时间、断言和原文都由连接器导出，模型不能选择历史消息或
伪造来源。没有消息时该工具只产生无证据的空结果，不会写入观察账本。可信宿主另可调用
`lookup_buyer_messages` 按 `(received_at, message_id)` 稳定顺序读取历史页；页长上限 16，
游标必须来自同一订单导出，未知游标 fail closed。该分页接口不暴露给模型。模型 Worker
会在每个运行切片调用 provider 前，由可信宿主按持久游标批量导入尚未处理的买方历史；
每条消息先进入内容寻址 artifact、Aftercare 观察账本和 EGM 投影，整页成功后才推进
`aftercare_investigation_buyer_cursors`。如果在游标推进前崩溃，稳定的来源事件 ID 和证据
operation 会安全重放；可重试的导入故障会让 Run 持久进入重试，不调用模型也不消耗模型
预算。该导入保证证据状态先于模型步骤更新，但当前模型输入仍由显式 transcript 和工具结果
构造。Worker 现在还会在每个模型阶段前重新读取该 Case 的完整观察账本，生成最多 64 条、
64 KiB 的结构化 `evidence_context_json`；它包含稳定 evidence ID、来源、时间、撤回状态和
规范化事实，不包含 tenant ID 或买方自由文本。超限时整份拒绝而不静默截断，并以
`input_rejected` 路由停下，不发起 provider 请求、不扣模型调用预算。这份上下文只是模型可见
材料，不是批准；冲突、过期、撤回和最终 disposition 仍由确定性证据门判断。

模型的最终文本现在必须严格解析成 `InvestigationProposal`；自由文本、额外 scope 字段和
未知 claim 会以 `proposal_rejected` 进入人工复核。有效 proposal 先保存在 `evaluate`
检查点中，模型调用到此结束并由 Worker fencing 落库；下一次领取不会再次调用模型，而是
在 Worker 最终 PostgreSQL 事务中从完整观察账本执行确定性评估，同时写入不可变 assessment
和 `complete` 检查点。任一租约/fencing 校验失败都会回滚这两项，因此模型置信度和 JSON
本身都不能充当批准；缺少评估器或确定性评估拒绝会以 `assessment_rejected` 进入人工复核。
调查新鲜度 policy ID、版本和三类来源 TTL 必须由部署显式配置。

EGM 的调查 domain schema 现在由 Aftercare 自己版本化，显式包含 `order_record`、
`logistics_observation` 和 `buyer_statement` 证据类型及对应声明门控；部署来源 ID 会在
应用创建时绑定到 schema，并进入 EGM workspace fingerprint。当前已实现本地 EGM 应用、
Worker provider 注入、EGM 表迁移准备和运行路径的 node/revision/operation 写入接缝；
Aftercare 观察账本与 EGM 写入使用两个短事务，EGM 投影失败会让工具步骤失败并可用同一
证据 operation 重试。迁移 019 会在调用 EGM 前持久保存 operation ID 与 expected revision，
并在成功后记录 EGM revision/evidence ID；进程若在 EGM 提交后、Aftercare 确认前崩溃，
新 Worker 会先重放完全相同的命令取得幂等结果。不同证据并发遇到 revision 冲突时，只有
未完成的收据可以按 EGM 当前 revision 做 compare-and-update rebase。该路径已经在隔离的
PostgreSQL 16.13 上通过 Worker、跨 Worker 崩溃窗口和并发 revision 验收。仍需把可信撤回
投影到 EGM、完成数据库恢复矩阵和真实来源认证，不能把本机集成验收误报成生产调查闭环。
上下文读取默认 `include_long_term=False`，长期记忆需要另行证据门控。
