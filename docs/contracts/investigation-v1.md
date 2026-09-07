# 调查证据契约 v1

本契约对应 A0-02，代码是 [investigation.py](../../aftercare_agent/domain/investigation.py)，
正反例是 [test_investigation.py](../../tests/contracts/test_investigation.py)。它固定调查输入与
确定性门控，不是数据库、来源认证服务、EGM Schema 或完整调查 Harness。
任务阶段见[工程执行计划](../engineering-plan.md)，当前验证记录只维护在[状态台账](../project-status.md)。

## 1. 首版调查对象与信任入口

首版处理“买家称未收到，承运商可能报告签收”，只产生有来源的建议。
工单关闭、退款、补发和真实通知需要各自业务状态机与授权，不能由本契约输出触发。

三个入口不混用：

| 入口 | 可提供内容 | 不得推断的能力 |
|---|---|---|
| 可信宿主 | 已授权 tenant/case/order、SourceGrant、来源注册映射、评估时钟和策略 | Python 对象本身不证明认证完成 |
| 可信连接器 | 标准化 OrderSnapshot、LogisticsObservation、BuyerStatement 及原始出处 | 外部原文里的角色、租户、指令不能授予能力 |
| 模型工具 | InvestigationProposal：固定 claim 枚举 + evidence_refs | 不能自填身份、订单、金额、来源、时间或自由事实正文 |

SourceGrant 是宿主认证/授权的结果，绑定 subject_id、tenant_id、case_id、order_id 和
source_id；不是允许客户端提交的表单。registered_sources 是宿主配置的 source_id →
SourceKind 映射。ingest_observation 同时核对目标范围、来源授权与来源能力，不能只信
观察载荷里的 source_id/kind。宿主需为不同租户选择其授权注册映射；不把任意模型 JSON
反序列化成 SourceGrant，也不把裸校验函数暴露成高权限工具。

当前没有实现供应商签名验证、买家会话认证或原文下载。真实连接器在 A3/B 阶段负责这些
上游保证；字符串叫 carrier-api 不是来源真实性证明。

## 2. 三种观察及固定声明

每条观察绑定 tenant/case/order/source，以及 evidence_id、source_event_id、schema_version=1。
保留 observed_at、首次 received_at、original.artifact_id、原文 SHA-256、原文摘要/摘录和
可选 revoked_at。摘录不参与指令执行；原文引用必须在未来文件访问层再次鉴权，SHA-256
只供完整性核对，不证明来源可信。OrderSnapshot v1 只支持“授权订单台账记录存在”，
尚不引入金额、退款资格或任意订单字段推理。

| 观察 | 可支持的固定声明 | 明确不能支持 |
|---|---|---|
| OrderSnapshot / order_ledger | order_recorded | 应当退款、当前有退款额度 |
| LogisticsObservation / carrier，delivered | carrier_reported_delivered | buyer_received、买家撒谎 |
| LogisticsObservation / carrier，in_transit | carrier_reported_in_transit | 未来必定送达、当前实际位置 |
| BuyerStatement / buyer_channel，not_received | buyer_reported_not_received | 客观上一定没有收货 |
| BuyerStatement / buyer_channel，received | buyer_reported_received | 已达到关闭工单或付款的条件 |

unknown 物流状态、unclear 买家陈述可保存，但不能冒充上述确定陈述。不存在
buyer_received、refund_completed 或 case_closed 调查声明。该模块不会使用既有退款
refund_completed Schema 来表达调查结果。

## 3. 时间、撤回和重放

所有时间必须带时区，进入类型时规范成 UTC。observed_at 是来源原始观察时间，必须不晚于
首次 received_at；received_at 是可信接入层首次收到该来源事件的时间，不是每次重试时间。
未来观察/接收时间不可用于当前判断。当前契约严格拒绝未来时间，不暗设生产时钟漂移宽限。

FreshnessPolicy 必须显式提供 policy_id、正整数 policy_version，以及三种来源分别适用的
max_age_seconds。没有生产 TTL 默认值；测试中 60/120/180 秒仅为合成边界。判断使用
评估时刻减 observed_at，等于上限仍有效，超过上限为 stale。策略版本随评估结果保存；
历史判断要用当时快照/策略复盘，不能随意套用新策略改写历史结论。

撤回由可信宿主生命周期流程管理，revoked_at 不早于首次接入时间。撤回后的观察保留原文，
但不可再支持新结论；其原始 observed_at/received_at 不变。撤回与历史业务动作并不互相
撤销。v1 的 ingest_observation 不实现撤回命令；接纳撤回事件和重投时的持久状态合并
需要 A3 的来源记录/投影逻辑，不得通过伪造一个新时间戳重新激活原事件。

持久去重键是 (tenant_id, case_id, source_id, source_event_id)，evidence_id 是工单内
稳定引用。宿主先按去重键读取已有观察，再调用 ingest_observation(existing=...)；完全
相同返回原观察，变更原文、绑定或任一原时间则 CONFLICT。该纯函数不查库、不防并发；
A1/A3 必须通过唯一约束和事务实现查重/写入原子性。收到新的真实查询结果应有新的来源
事件 ID，不能用新 ID 包装旧原文来刷新它的观察时间。

## 4. 评估与调查结束语义

assess_investigation 输入宿主授权的完整工单/订单证据快照、模型提案、来源映射、策略和
评估时刻。不能只把模型选择的证据子集交给它，否则模型可以隐去矛盾。越界范围与未注册
来源直接 FORBIDDEN；重复 evidence_id 为 CONFLICT；缺失、陈旧、未来、撤回和不支持的
引用分别输出原因。一个声明引用了多条证据时，每条都必须支持它，不接受“一条正确掩盖
另一条错误”。

输出中 decisions 保存每条固定声明是否获支持，以及原始引用、来源、观察/接收时间；
missing 表示材料缺失/状态不明确，conflicts 表示需复核的矛盾，两者可同时存在。
unavailable 保留陈旧、未来、撤回的证据 ID 和原因，不静默删除。

只要完整有效证据集中出现“承运商报告签收 + 买家称未收到”，必须 HUMAN_REVIEW，
即便提案只引用了签收回执。买家先后同时存在有效的收货/未收货陈述也转复核，不能让
模型按需选一条。物流从在途更新为签收本身不视为矛盾；更完整的来源事件顺序、取代关系
和物流异常分类需后续独立扩展，不声称已覆盖任意矛盾类型。

NEEDS_MATERIAL 表示缺少当前所需材料；HUMAN_REVIEW 表示存在矛盾或被拒绝的提案；
RECOMMENDATION_READY 仅表示三类有效且足够明确的材料齐备、所提声明获支持、未命中
本版复核条件，可以进入建议生成。它不代表建议文本已生成，也不代表完整业务已经完成。
所有输出固定 authorizes_external_action=false、closes_case=false。

## 5. A0 与 A3 边界

A0 实现：版本化类型、输入/范围/来源约束、确定性新鲜度、原样重放比较、固定声明与
出处、缺材料和两类矛盾规则，配合合成正反例。

A0 没有实现：来源签名、数据库一致性、撤回命令、权限变更缓存失效、对象存储读取校验、
模型事实抽取效果、EGM 调查写入、Run 恢复、多进程防竞争和生产参数调优。

A0-03 根据这些契约组织更完整合成案件；A3-02 再实现受限 EGM 调查适配和必要的独立
EGM Schema 变更，保留现有退款适配回归。join、Run fencing、EGM revision、业务动作
幂等仍是不同边界。当前代码通过纯契约测试不能写成“调查 EGM 接入已完成”。
