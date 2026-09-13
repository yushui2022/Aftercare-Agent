# 运行时契约 v1：身份、执行片段、等待与事件

本契约对应 A0-02。实现是 [公共类型](../../aftercare_agent/domain/common.py)、
[运行对象与前置条件](../../aftercare_agent/domain/runtime.py)、[检查点与工具请求](../../aftercare_agent/domain/protocol.py)、
[等待判定](../../aftercare_agent/domain/waits.py)、[事件顺序判定](../../aftercare_agent/domain/events.py)。
它们是纯类型与确定性规则，不连接数据库、不执行工具、不创建队列或 Worker。
调查语义另见[调查证据契约](investigation-v1.md)，进度只见[状态台账](../project-status.md)。

## 1. 范围与版本

首版目标是“未收到货调查”：输入受理 → 模拟只读工具 → 材料草稿 → 等待模拟回复 →
形成有出处的建议。阶段 A 不真实退款、补发或发邮件。下面保留 WAITING_APPROVAL 等
扩展状态的位置，不代表审批/支付模块已实现，也不允许调查工具自行启用这些能力。

ContractModel 使用 strict、extra=forbid、frozen 和嵌套实例重新验证。计数/版本不能用
字符串或 bool 代替；内部标识为 1–160 字符的 ASCII 不透明标识，禁止空格与控制字符。
业务说明、买家原文不是 Identifier；外部渠道不兼容的标识应先由连接器显式映射。
时间必须带时区并规范成 UTC。Pydantic 类型只校验数据，不证明身份、来源或事务安全。
model_construct / model_copy(update=...) 等跳过校验的路径不得用于加载不可信输入或写入记录。

记录的 schema_version=1；新建 Python 记录可以默认填 1，持久序列化必须保存该字段。
decode_checkpoint 要求线上 JSON 显式提供整数版本，缺失/类型错为 INVALID_INPUT，
未知整数版本为 UNSUPPORTED_VERSION；不补默认值重启未知检查点。其他记录进入持久化
边界时同样必须先检查存储版本，不能用缺失版本的历史 JSON 冒充新建对象。

JSON 工具/检查点解码拒绝重复 key、NaN/Infinity、非对象、非法 UTF-8 和超过 65,536
UTF-8 字节的输入。该上限是 v1 内联元数据的协议限制，不是生产吞吐或附件大小建议；
原文、协议转录和大型结果放在受授权的不可变引用中，不把整份对话内联到检查点。

## 2. 对象关系与可信输入

| 对象 | 必须绑定 | 生命周期与唯一性要求 |
|---|---|---|
| CaseRecord | tenant、case、order、业务 version | 持久案件；关闭不由 Run/EGM 完成自动触发 |
| SessionRecord | tenant、case、session、channel | 一个 Session 只属于一个 Case，不等于 HTTP/SSE 连接 |
| RunRecord | tenant、case、run、输入/定义版本 | 一个调查目标；可跨等待和 Worker 接管 |
| StepRecord | tenant、case、run、step、输入版本 | 一项逻辑工作；重试保留 step_id |
| AttemptRecord | tenant、case、run、step、attempt、尝试序号、fence | 一次实际尝试，不能作为退款幂等键 |
| WaitRecord | tenant、case、run、wait、generation | 一代持久等待，结算只成功一次 |

数据库必须落实复合外键/唯一约束；运行对象可以保存标识，不能只因标识相同就推断归属。
validate_hierarchy 要求提供宿主读取的关联记录，逐级验证 Session/Run/Step/Attempt。
Attempt 的历史 token 可以不同于当前 Run token；它是历史执行来源，不是当前授权。

CaseGrant 只能由认证/授权层产生，包含 subject、tenant、授权 case_ids 和 permissions；
authorize_case 检查租户、Case 和权限，不能把客户端提交的同结构 JSON 当成 Grant。
受理前核对商家身份与 case:create 权限，并由可信订单台账确认订单归属。读取/重放已有
结果同样重新鉴权，不能借幂等键读取他人工单。未来 Worker 从队列取出的对象也须重新
加载、检查归属；队列字段不是认证凭证。

OpenCaseInput 只允许 order_id、channel、不可变 message_ref 及 message_sha256、
固定 goal 和 schema_version，不允许注入 tenant、case、principal 或 Run owner。
引用由接入层保存/核对原文后绑定，声明了 SHA-256 不等于已校验文件内容。host 生成
case_id/session_id/run_id；请求中的 channel 是受理类型，真实渠道身份仍由认证层确认。

## 3. 受理幂等

唯一键为 (tenant_id, entrypoint="case.open.v1", idempotency_key)。摘要计算对象是
tenant、entrypoint、校验后的完整 OpenCaseInput；包含默认 goal/version、订单、渠道及
原文引用/摘要。不包含 trace_id、临时 request_id、请求到达时间或新的随机 Run ID。

编码固定为 UTF-8 JSON，key 排序、无多余分隔空格、ensure_ascii=false；不做大小写转换、
Unicode NFC、文本 trim 或订单格式猜测。字段顺序不同、显式/省略相同默认值等价；字段
内容或原文哈希不同不等价。原文引用必须不可变，不能在重放时换掉其内容。

数据库受理事务：先认证与授权 → 插入唯一键/摘要或锁定已有记录 → 相同摘要返回原
Case/Session/Run → 不同摘要 CONFLICT → 新输入则创建对象和事件/任务 → 一起提交。
纯 admission_digest / check_admission_replay 不实现查重或并发插入。未提交不能返回
“已持久受理”；提交结果不确定时以原键重放，不分配新键重复建案。

## 4. Run 状态转换与执行权

| 当前状态 | 允许目标 | 除合法边之外必须满足的条件 |
|---|---|---|
| READY | RUNNING、CANCELLED | 领取当前 Run 或授权取消 |
| RUNNING | READY、WAITING_INPUT、WAITING_APPROVAL、RETRY_AT、REVIEW、COMPLETED、CANCELLED | 持有效执行权、当前业务版本，并原子保存相应记录 |
| WAITING_INPUT / WAITING_APPROVAL | READY、CANCELLED | 当前等待代次结算，或授权取消并结清活动等待 |
| RETRY_AT | READY、CANCELLED | 数据库时间已到 available_at，或授权取消 |
| REVIEW | READY、CANCELLED | 可信复核决定与新输入版本，或授权取消 |
| COMPLETED / CANCELLED | 无 | 新目标/新消息需要新的关联 Run，不能原地复活 |

validate_transition 只检查合法边，不自行证明授权、到期或建议完成。RUNNING 必须有
owner、正 token 和 lease_until；其他状态不得保留租约。等待状态必须绑定 wait_id 与
generation，RETRY_AT 必须有 available_at；其他状态不能偷偷带这些活动字段。

Run 行是唯一执行权威。队列 Task 只是唤醒提示，不向同一个 Run 另外发一套独立 token。
领取 READY 或接管已过期的 RUNNING 时，fencing_token 严格递增；普通续租不增加 token。
只有 state=RUNNING、owner/token 精确匹配且数据库 now < lease_until 才可续租/受保护写入。
等于到期时已经失权，即使没有新 Worker 接管也不能自行续租复活。

next_fencing_token 只计算候选值，不锁行、不写回。A1 必须在同一短事务内原子判断与
更新；受保护提交持续持有权威 Run 行锁至事务结束。普通 SELECT 后再写不是 fencing。
时间在取得相关锁后读取数据库当前时钟，例如 clock_timestamp()；不能把等待锁之前的
事务起始时间或 Worker 本机时间当成最新租约检查时间。

业务 Case version、Run token、EGM revision、checkpoint_version、case_seq 与未来
Action 幂等键互不替代。assert_case_version 使用刚锁定的 Case 记录检查业务版本，不
从旧 Run/检查点猜当前版本。跨 Run 的外部动作仍需 B 阶段 Action Ledger。

A 阶段涉及多个行的写事务统一先 Case → Run → Wait/Task/输入应用记录 → 必要的 EGM
Case 锁，排序同类 ID 后取锁；不得另一路径倒序取锁。纯 Run 续租可以只锁 Run，但
不能随后再反向申请 Case。A1 迁移/Repository 必须具体落实和测试这一顺序；B 阶段加入
支付聚合时需扩展锁协议。模型、HTTP、沙箱、人工等待均在数据库事务之外。

取消会撤销后续 Run 推进资格，但不能撤回已经发出的外部请求。迟到可信回执经独立接入
保存，再由当前执行者核对，旧 Worker 不因此重新获得执行权。

## 5. 检查点和完整工具调用

Checkpoint 保存输入/Case/检查点版本、历史 token、Step/Attempt、定义/策略/工具/
模型配置版本、原生协议版本与引用、工具结果、证据和 Action 引用、剩余预算及下一步。
当下一步是 `wait` 时，新写入的 checkpoint 还保存 `resume_next_step`，表示等待
结算后唯一允许进入的模型/工具/评估阶段；这样恢复位置属于 Run 的持久事实，而不是
Daemon 的全局配置。早于该字段的 schema-v1 checkpoint 可以由可信调用方显式提供兼容
参数恢复，Worker 不会自行猜测。
ArtifactReference 带 tenant/case/reference_id/SHA-256；字段绑定相等不等于文件存在、
归属正确或内容已核验，解引用必须由存储授权层检查。相同检查点不得有重复 call_id 结果。

预算显式记录剩余模型次数、工具次数、微美元成本上限与 deadline，不提供生产默认值。
当前工具请求校验检查工具次数/截止时间；模型计费估计、并发额度预留、消费与返还由
A1/A3 实现，不能用纯对象防止并发透支。工具结果记录 succeeded/error/unknown，未知
结果不允许因为恢复而盲目重复外部动作。

恢复先检查 schema 与可用协议解码器，再核对 scope、输入/Case 版本。版本变化需要显式
重新装配/核对并生成新检查点，不静默覆盖旧内容。已保存定义/策略/工具/模型配置必须
能从版本仓库精确取回；assert_resume_compatible 只实现 scope、输入和协议兼容检查，
配置版本仓库尚未实现。旧 checkpoint 的 saved_fencing_token 只是历史；新持有者读取
旧检查点合法，但提交必须使用当前 Run 的新 token。

ToolRequest 只表示完整请求的候选记录，不是已授权执行。stream_complete 与工具白名单
由可信 ModelAdapter/Harness 绑定，不从模型 arguments 中读取。v1 示例工具契约是：

| 工具名 | 模型参数 | 可信宿主绑定 |
|---|---|---|
| lookup_order | 空对象 | 当前租户、工单、订单与只读连接器 |
| lookup_tracking | 空对象 | 当前订单的授权物流查询范围 |
| request_material_draft | 去重 questions 枚举：确认地址/检查投递位置/提供签收材料 | 当前 Case 的草稿保存位置；没有发送权限 |

缺少完整结束信号、截断 JSON、未知/未获准工具、跨范围参数、未知字段、重复 key 与
超预算请求都不能生成可执行参数。validate_tool_request 仅返回已校验参数，不调用任何
工具，不证明当前 Run 有执行权。调查提案使用独立 InvestigationProposal 契约，A3 才
将这些 schema 组合进真正的工具注册表；原生模型 delta 不作为业务事件。

## 6. 等待、早到回复和超时

等待有 PENDING → ACTIVE → SATISFIED/TIMED_OUT/CANCELLED 的持久生命周期。
先注册稳定 wait_id/generation/correlation_key 和意图，再产生通知草稿；回复可以早于
ACTIVE，但须晚于原意图创建时间。当前纯函数对 PENDING 返回 none，输入仍保留 Inbox，
不能丢弃。A 阶段只有合成回复；真实通知必须通过 B 阶段外部动作门禁。

匹配键严格包含 tenant/case/run/wait/generation/kind/correlation_key/condition_version。
宿主从已认证渠道解析并校验绑定，模型不能填这些字段。旧代次、错误类型、错误范围的
输入不推进当前等待，原始输入仍保留用于核查或其他消费者。

InboxSignal 另含带 Case 范围的不可变正文 payload 引用和 SHA-256。check_inbox_replay
要求稳定 event_id、正文引用/摘要、所有绑定和首次 received_at 原样保持，避免同一来源
消息键换了买家正文却被当成正常重投。实际字节核验与首次接收时间赋值仍由可信接入层负责。

v1 选择“首个合法结算事务成功提交”分支，不实行严格接收截止时间：now == deadline
可以尝试超时，但若锁内已经存在匹配的提交回复，回复优先；deadline 后而超时提交前
到达的回复也可获胜。纯 resolution_candidate 只选候选，传入 None 必须表示权威查找
确认没有匹配输入，不能把过期缓存/未查找误当空结果。

A2 激活等待时查询已提交 Inbox；接收输入时同样保存持久匹配任务，并扫描未匹配事件。
超时事务锁定当前代次后重新检查 Inbox。对多个匹配输入按可信首次接收时间、event_id
排序选择，消费者应用与状态约束保证只有一个赢家。Reply/timeout 并发下的实际锁定、
重查与失败重试尚未实现，不能用这里的单线程判定测试替代故障验收。

三个不同唯一键：

- Inbox：(tenant_id, source_id, source_event_id)，同键换绑定/内容必须冲突，不只是丢掉重投。
- 消费者应用：(tenant_id, case_id, consumer_id, event_id)，不能用全局 processed=true。
- 后继唤醒：(tenant_id, case_id, run_id, wait_id, generation)，对应同一代等待唯一结算。

Wait 结算、消费者应用、Run 转 READY、唯一唤醒和领域事件必须同事务提交，之后才 ACK。
终态再次收到回复不再次结算，迟到材料保存并进入复核/新目标路径。WAITING、RETRY_AT
只占持久记录，不保留线程、活动事务、执行槽或沙箱。

## 7. 事件顺序、幂等消费和 SSE 边界

DomainEvent 的 v1 字段为 event_id、schema_version、tenant/case、case_seq、event_type、
payload_schema_version、带 scope/hash 的 payload 引用、可选 run_id、correlation_id、
causation_id 和 recorded_at。运行事件必须有 run_id。架构文章里含 Action 的示例只是
目标示意，不是本版已启用支付事件。完整载荷 schema/引用存储仍需对应业务模块实现。

case_seq 是每 Case 锁内事务计数器的正整数，从 1 开始；回滚不消耗已提交连续序号。
业务 version 独立，不要求一项业务变化恰好发一条事件。全局自增 ID、trace_id 和最大
event_id 不能代表全库提交水位。Outbox 扫描持久待发状态，不用 MAX(id) 跳过晚提交行。

projection_decision 规定：下一连续序号 apply；前方有缺口 buffer_gap；旧序号只有与
已应用事件完整一致才 replay，否则 CONFLICT。同序号不同 ID、引用、时间或 payload
摘要不能伪装重复。另需持久唯一 event_id 防止同事件被重新编号，此检查不由纯函数的
单条输入自动完成；A2 必须建立索引并验证它。

投影状态、已应用索引、消费者记录、UI 流追加同事务提交。缺口要持久暂存/补读，不能
只返回 buffer_gap 后把消息 ACK 掉。源事件已归档时改用一致快照及其水位重建。
SSE 以后使用授权投影，不重放全部模型 token；游标绑定租户、订阅范围与授权版本，
权限变化/超过保留窗口需要新快照。OTel 是诊断，不是业务事件或审计主账本。

## 8. 错误分类与验收

| code | 处理要求 |
|---|---|
| invalid_input | 修正参数/截断/结构错误；不执行候选工具 |
| unauthenticated / forbidden | 认证或范围不满足；不靠重试获得权限 |
| conflict | 幂等改参、旧业务版本、非法状态；重新读取/明确核对 |
| lease_lost | 停止推进，用当前执行者恢复；旧身份不能自行复活 |
| rate_limited / retryable | 受预算约束的持久退避，可带非负整数 retry_after_seconds |
| outcome_unknown | 核对原操作结果，不解释成失败并换键重做 |
| unsupported_version | 保留原记录，升级解码器/显式迁移，不重置状态 |
| evidence_rejected | 证据时间/来源等不符合要求，保留原因 |
| budget_exhausted | 让出资源或转复核，不能无限工具循环 |

Failure 的重试时间只允许 RATE_LIMITED/RETRYABLE。模型/接口输入的 Pydantic
ValidationError 由未来入口统一映射为 INVALID_INPUT；不要把库堆栈或客户原文原样暴露。
本轮不固定 HTTP 状态码，也没有实现自动重试器。

正反例位于 [运行对象测试](../../tests/contracts/test_runtime.py)、
[等待测试](../../tests/contracts/test_waits.py)、[事件测试](../../tests/contracts/test_events.py)、
[检查点/工具测试](../../tests/contracts/test_protocol.py)。它们验证类型、纯 guard 和判定，
不证明 PostgreSQL 原子领取、崩溃恢复、数据真实性、供应商 exactly-once 或生产容量。
这些实际能力必须在 A1/A2/A3 的实现与真实集成验收中补足。
