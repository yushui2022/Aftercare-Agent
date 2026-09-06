# ADR-0001：有条件采用 Evidence-Gated-Memory 作为证据准入组件

状态：有条件采用，生产准入未通过。本文保留修复前评估和 0.5 HTTP-first 决策历史；其部署默认值及“Adapter/PostgreSQL 后端未实现”等状态已由 [ADR-0002](0002-embedded-egm.md) 和 [0.6 嵌入式接入](../integrations/egm-embedded.md) 取代。业务权威、来源认证和高风险动作限制继续有效。

评估日期：2026-09-06。

评估对象：[Evidence-Gated-Memory](https://github.com/yushui2022/Evidence-Gated-Memory)，固定代码提交 [a16e3de](https://github.com/yushui2022/Evidence-Gated-Memory/tree/a16e3de7ca51727d6f16b8d335bb75ce70e9b4f6)。该提交的包元数据声明版本 0.4.0、Alpha；main 中包含未发布或文档尚未同步的实现，不能仅凭版本号判断能力。

## 1. 决策摘要

后续实现说明：下文引用 a16e3de 的代码与探针用于保留修复前依据。当前接入基线已更新为 EGM 0.5.0 本地源码，详见[服务接入与验收](../integrations/egm-service.md)。新版本已具备原始 JSON 状态与对象绑定、task-scoped Facts/L1/L2/L3、SQLite v4 原子存储，以及认证、角色隔离、幂等和 revision 控制的 HTTP 服务。不是仅在 Aftercare 文档中提出这些能力。

**可以利用，而且与售后业务高度相关；但应复用它的证据与结论准入能力，不把它提升为整个 Aftercare 的状态权威。**

Aftercare 优先验证 EGM 对四个问题的帮助：

1. 结论引用了哪些可追溯证据？
2. 所需证据类型是否齐全、来源标签是否符合规则、时效是否满足要求？
3. 某项证据撤销或过期之后，哪些结论需要重新评估？
4. 模型下一轮能否看到精简的任务结构与证据引用，而非不断增长的工具日志？

它比通用偏好记忆更贴近“有回执才能宣布完成”的售后需求。但是，当前门控不验证任意自然语言结论是否被原始回执语义支持，新 HTTP 服务提供租户/工单授权，但仍不提供退款幂等或分布式执行权。

因此采用方式更新为 **受控 Evidence Adapter + EGM HTTP 服务**。服务在同一宿主串行每工单的事务，不向所有业务 Worker 共享裸 egm.db；Aftercare 仍需实现 Adapter 与业务流程。

## 2. 修复前固定代码中已值得复用的能力

| 能力 | 已看到的实现 | 在售后中的用途 |
|---|---|---|
| 原始证据与索引 | record_evidence 保存 refs 文件及 Evidence 索引 | 保留 API 原文、材料与可下钻引用 |
| 事实准入 | assert_fact 进入 propose → check → commit 路径 | 阻止缺失要求证据的结论直接成为可用 Fact |
| 门控拒绝反馈 | 返回原因、缺失类型与 suggested_action | 指导补查资料，而不是只得到 False |
| 证据时效与血缘 | freshness、revoke_evidence、sweep_expired、依赖索引 | 标记过期支持，级联失效依赖事实 |
| 任务解释图 | Task / TaskNode / TaskEdge、Mermaid 投影 | 呈现调查阶段与证据关系 |
| 上下文构建 | build_context 与 read_ref | 精简主上下文，按需查阅原始证据 |
| 长期记忆候选 | CandidateAtom、SourceSpan、候选审核与晋升 | 后续验证可追溯的经验沉淀 |

实现依据：[公共 API](https://github.com/yushui2022/Evidence-Gated-Memory/blob/a16e3de7ca51727d6f16b8d335bb75ce70e9b4f6/src/evidence_gated_memory/core/memory.py)、[门控引擎](https://github.com/yushui2022/Evidence-Gated-Memory/blob/a16e3de7ca51727d6f16b8d335bb75ce70e9b4f6/src/evidence_gated_memory/core/gates.py)、[上下文构建](https://github.com/yushui2022/Evidence-Gated-Memory/blob/a16e3de7ca51727d6f16b8d335bb75ce70e9b4f6/src/evidence_gated_memory/core/context.py)。

“Fact”在这里是通过当前 Schema 准入的数据对象名称，不等于已经得到完整业务真值证明。来源片段哈希证明的是片段对应关系，也不证明候选摘要语义正确，更不是自动计算出的可信概率。

## 3. 必须保留在 Aftercare 的权威边界

| 事项 | 权威持有者 | EGM 的角色 |
|---|---|---|
| 订单、支付与物流实际状态 | 对应业务系统及其核验记录 | 保存证据引用与可用结论 |
| Case / Run / Step / Attempt | Aftercare PostgreSQL | 映射成可重建的解释图 |
| 租约、fencing、队列与配额 | Aftercare 调度与准入层 | 不负责 |
| 审批、幂等键、Action Ledger | Aftercare 工具与业务控制层 | 引用必要证据，不授予执行权 |
| 回执是否对应本次成功动作 | 可信连接器与业务校验器 | 对已分类证据再做准入 |
| 模型展示与补查建议 | Harness | 提供事实、拒绝原因和任务上下文 |
| 合规保留与审计 | 独立持久记录及企业策略 | 调试与证据链记录的补充 |

EGM 文档中 TaskGraph 作为其内部记忆权威的描述，不应直接移植成 Aftercare 工单权威。这里明确区分“EGM 自己的任务图状态”与“业务平台的工单状态”。

即使 EGM 的节点进入 DONE，Case 仍需满足业务关闭条件。反过来，某条旧证据变得不可用于当前判断，也不能抹掉 Action Ledger 中已经确认发生的历史动作。

## 4. 目标数据流：在四个位置接入，但保留业务检查

~~~text
外部工具原始结果
    │
    ▼
可信工具网关
  鉴权 / 来源认证 / 订单绑定 / 响应语义 / 敏感字段处理
    │
    ▼
业务数据库 + 证据产物存储 + Outbox
    │
    ▼
受控 Evidence Adapter
  来源事件去重 / 版本绑定 / 单写者 / 证据分类
    │
    ▼
EGM record_evidence → assert_fact → 解释图门控
    │
    ▼
带引用的可用事实 / 缺项与拒绝原因
    │
    ▼
Harness 组装上下文 → 模型提出下一步
    │
    ▼
Aftercare 重新校验权限与业务状态 → 受控执行
~~~

四个接入时机：

- 工具返回时，先保存权威原始记录，再向 EGM 导入经过权限与类型校验的证据。
- 需要形成业务结论时，先验证结构化业务字段与结论关系，再调用 assert_fact；高风险事实优先由已验证字段按模板构造，而不是直接批准任意自然语言。
- 需要展示调查步骤完成时，通过受限的节点类型与 transition_node 更新解释图；业务 Step / Case 的推进仍经 Aftercare 状态机。
- 模型调用之前，调用 build_context 获取证据上下文，再与供应商协议要求的消息和工具结果共同组装。

EGM 上下文不是 Responses 或 Messages 的完整协议历史，不能直接替换待处理工具调用、关联 ID 和供应商要求保留的上下文项。

## 5. 不能忽略的三个本地反例

在固定提交、内置 REFUND Schema、合成数据及无网络条件下，实测如下：

| 输入 | 修复前 a16e3de 结果 | Aftercare 接入必须做到 |
|---|---|---|
| 证据类型为 refund_api_response，但正文 status=failed | “退款已完成”的 Fact 与对应节点 DONE 均被接受 | 必须解析并验证成功语义，失败回执不能成为成功证据 |
| 回执正文与 metadata 属于 ORD-999，结论及节点属于 ORD-123 | Fact 与 DONE 均被接受 | 验证租户、订单、操作、金额、币种及审批绑定 |
| 同一 workspace 中保存 A 工单的长期记忆，给 B 工单调用 build_context(task_id=B) | 默认上下文出现 A 的长期记忆 | task_id 不是 ACL；隔离 workspace，并在初期关闭自动长期记忆注入 |

这不是说缺证据、过期和来源门控没有价值，而是说明它们检查的是特定结构性条件，不是完整的业务授权与语义正确性。

详见[评估与验证记录](../research/evidence-gated-memory.md)。这些路径已在后续 EGM 本地源码中修复；表格保留为修复前结果，新行为由契约、上下文隔离与 HTTP 测试覆盖。

## 6. Adapter 必须限制哪些入口

以下为持续适用的 Adapter 契约。0.5.0 HTTP 已落实身份、范围、来源权限、观察时间、节点绑定及受限入口；真实供应商认证、审批与执行权仍需 Aftercare 实现：

- 租户与操作者来自服务端认证，不从模型参数推导。
- source_system、证据类型、观察时间和 TTL 由受信任连接器与版本化策略赋值。允许列表匹配字符串，不等于验证了响应来源。
- 回放保留原始 observed_at 和来源版本，不能以导入时间把旧证据重新变“新”。
- 高风险证据没有有效 TTL、时间在异常未来或来源版本未知时拒绝准入；不能沿用 UNKNOWN 一概可用的默认语义。
- 固定允许的 node_type、claim_type 和状态转换；没有匹配规则的关键动作默认拒绝，而不是允许模型创造新类型避开门控。
- 不向模型暴露 commit_fact、手工状态 CRUD、直接记忆晋升、可写 GateResult、底层 Store 或任意工作区路径。
- 每次 read_ref、查询、检索与导出都验证访问范围。给 ID 添加租户前缀只是防冲突，不构成授权。
- suggested_action 只是一项补证据建议，仍须走工具权限与审批；不能把“调用退款 API 获得证据”直接翻译成授权执行退款。

修复前代码中的关键边界：record_evidence 接受调用方的来源、时间和 TTL；未匹配 state_gates 的路径不会因为“没有规则”自动拒绝；UNKNOWN 时效可以通过可用性检查；低层状态修改与手工记忆入口仍存在。[证据入口](https://github.com/yushui2022/Evidence-Gated-Memory/blob/a16e3de7ca51727d6f16b8d335bb75ce70e9b4f6/src/evidence_gated_memory/core/memory.py#L627)、[状态门控](https://github.com/yushui2022/Evidence-Gated-Memory/blob/a16e3de7ca51727d6f16b8d335bb75ce70e9b4f6/src/evidence_gated_memory/core/gates.py#L269)、[时效语义](https://github.com/yushui2022/Evidence-Gated-Memory/blob/a16e3de7ca51727d6f16b8d335bb75ce70e9b4f6/src/evidence_gated_memory/core/freshness.py#L55)

## 7. 售后 Schema 不能直接照搬退款 Demo

修复前内置 REFUND 只要求新鲜 refund_api_response，没有检查回执里的 status、金额和目标对象。新 REFUND 已增加 status/order_id 契约，新 AFTERCARE 进一步绑定租户、案件、操作、金额和币种；仍不覆盖全部售后流程。以下链接用于保留历史依据。[内置退款 Schema](https://github.com/yushui2022/Evidence-Gated-Memory/blob/a16e3de7ca51727d6f16b8d335bb75ce70e9b4f6/src/evidence_gated_memory/schemas/builtin/refund.yaml)

后续应在 Aftercare 中维护自己的版本化领域 Schema。下面是待设计的概念映射，**不是现成 EGM 类型，也不是加入 YAML 就能自动执行的字段判断**：

| 售后结论 | 候选证据类别 | 额外业务约束 |
|---|---|---|
| 买家报告未收到货 | buyer_statement | 只证明买家的陈述，不证明实际未配送 |
| 承运商记录显示签收 | carrier_tracking_snapshot / delivery_proof | 运单、订单、时间与来源匹配，不自动推出买家本人签收 |
| 建议补发符合规则 | order_snapshot / policy_snapshot | 资格计算由业务代码完成，检查重复售后与适用政策 |
| 某项动作已获批准 | approved_action | 审批绑定请求摘要、身份、额度和有效期 |
| 补发或退款已确认 | execution_receipt_confirmed | 仅由受信任校验器确认成功后生成，绑定稳定 action_id |

原始失败、超时与待处理回执同样应保存，供核对使用，但不能分类成 execution_receipt_confirmed。EGM 的类型门控发生在这一步业务校验之后。

证据过期意味着不宜继续支持某个当前判断，不意味着历史退款被撤销。收到撤销或冲突事件后，应更新证据版本、失效相关派生结论、标记 ReviewRequired，并核对业务事实；不得重发已确认动作。

## 8. 单写者、PostgreSQL 与恢复如何共存

### 修复前建议：隔离的单写者 PoC

当前方案更新：业务 Worker 通过受限 HTTP 服务访问，不直接拥有 workspace 写权。同宿主多服务 Worker 使用独立连接和 revision/operation 协议，每个工单串行；不同工单独立数据库。下列网络共享目录、业务租约和跨库一致性警告仍然成立。

使用固定代码版本，在受控进程中独占一个工单工作区。不同租户默认使用不同工作区，初期进一步按 Case 隔离；关闭默认长期记忆注入，只使用经过范围检查的事实与任务图。

workspace 中的 egm.db、refs 和 offload 都是数据，不是可以任意删除的缓存。只有原始证据、输入事件、策略版本及映射已可靠保存在权威存储，并验证可重建后，才能把这份 EGM 数据视为可重建投影。

不要让多个 Worker 通过网络共享目录同时写同一个 workspace。SQLite WAL 和 busy_timeout 不能实现业务租约，也不能让失去租约的旧进程停止写文件。[存储配置](https://github.com/yushui2022/Evidence-Gated-Memory/blob/a16e3de7ca51727d6f16b8d335bb75ce70e9b4f6/src/evidence_gated_memory/storage/sqlite.py#L320)、[上游生产边界说明](https://github.com/yushui2022/Evidence-Gated-Memory/blob/a16e3de7ca51727d6f16b8d335bb75ce70e9b4f6/docs/production.md)

### 第二阶段：显式的投影一致性

PostgreSQL 先提交原始事件、业务变化和 Outbox；随后由单写者导入 EGM。不能假设 PostgreSQL 事务与 EGM 的 SQLite、refs 文件自动共享原子提交。

Adapter 需要维护 event_id 与 EGM 对象 ID 的映射、投影版本和可恢复导入状态。直接调用 record_evidence 仍生成新 ID；新 HTTP 的 operation_id 可原样重放持久响应，避免重复证据。必须保存稳定请求，不在重试时刷新 observed_at；遇到明确的 revision 冲突先核对状态。业务事件映射与跨库导入恢复仍由 Adapter 负责。

对于带副作用的关键决策，要求投影覆盖当前需要的 Case / evidence 版本，并在提交动作时重新校验权威状态。投影滞后或不可用时，不允许依据陈旧的 accepted 结果继续推进危险动作。

如果新 Worker 接管，优先使用新的执行代次工作区，从已保存输入重建，并用 fencing / 业务版本拒绝旧代次结果。不要仅因旧租约过期，就让新旧进程同时写原来的 SQLite 文件。

修复前上游高层操作包含分次提交：原始 ref 写入、证据索引、事实、状态、审计及级联失效不构成一个覆盖全部内容的原子事务。恢复必须能识别中断与不完整投影，而不是把“磁盘上有 egm.db”视为一致性证明。[原始证据保存顺序](https://github.com/yushui2022/Evidence-Gated-Memory/blob/a16e3de7ca51727d6f16b8d335bb75ce70e9b4f6/src/evidence_gated_memory/core/memory.py#L651)、[事实与审计提交](https://github.com/yushui2022/Evidence-Gated-Memory/blob/a16e3de7ca51727d6f16b8d335bb75ce70e9b4f6/src/evidence_gated_memory/core/memory.py#L1280)

后续 0.5.0 已实现高层原子事务、SQLite 内原文与 offload、可恢复文件投影、持久幂等和 HTTP 服务。历史文件型 refs 仍需备份。PostgreSQL 后端和跨机 HA 仍未实现，不能与已完成的服务化混为一谈。

## 9. 与 Letta、TencentDB Agent Memory 的关系

EGM 优先评估的是当前案件的证据、事实准入和解释图；Letta、TencentDB Agent Memory 继续作为跨会话上下文与长期经验组织的参考。它们不是完全相同的产品，也不需要为了“完整”而全部接入。

Aftercare 第一阶段不建设多套长期记忆系统。如果 EGM 的候选记忆已经满足某类受控经验需求，就先验证其来源、权限、冲突与质量；若不足，再按明确问题选择其他能力。

EGM 自身的 CandidateAtom 候选入口已经存在，不能沿用旧文档把所有候选晋升都说成尚未实现；但候选来源哈希、调用方提供的 confidence 和冲突标记，也不等于对记忆真实性、规则优先级或租户访问权的完整验证。

## 10. 验收与回退

进入真实业务前至少需要验证：

- 缺少证据、失败回执、错误订单、金额或币种不匹配均不能生成成功结论。
- raw source、观察时间、TTL、GateResult 和 node_type 不能由模型自由赋值绕过控制。
- 无 ACL 的长期记忆不会因 task_id 参数被误当作隔离后的结果。
- 导入事件重投、EGM 写入中断、旧写者恢复和投影滞后不会推进危险动作。
- 证据撤销能触发复核，但不重做已经确认的外部副作用。
- 量化补证据成功率、错误放行率、错误拒绝率、来源覆盖、上下文总成本及额外延迟。

上线顺序应是离线验证 → 只观测的影子评估 → 受限业务启用。影子阶段仅记录 EGM 建议，不改变现有业务决定；未来一旦启用为必要门控，故障时应暂停对应高风险路径或转人工，不能静默放行。

如果效果不优于显式证据规则与简单上下文构建，保留证据入口和业务契约，撤下 EGM 适配即可。不要让是否继续使用一个库决定能否读取工单原始证据和历史回执。

## 11. 来源与许可备注

判断优先依据固定提交的代码与测试，README / production 文档有少量落后于实现的描述。例如显式 TaskEdge 已有多节点环拒绝，但这不代表 parent_id 与事实血缘全图都已具备完整并发无环约束。

pyproject 声明 MIT，但该提交未包含独立 LICENSE 文件。历史评估只做分析和文档引用；后续已修改独立 EGM 源码，但 Aftercare 仓库仍未复制实现或增加运行依赖；实际分发前应核对上游许可证文件与所选发布版本。[包元数据](https://github.com/yushui2022/Evidence-Gated-Memory/blob/a16e3de7ca51727d6f16b8d335bb75ce70e9b4f6/pyproject.toml)
