# 工程执行计划

版本：1；更新日期：2026-09-15。本文固定任务 ID、依赖、目标文件和验收，不记录实时完成状态；唯一进度入口是 [project-status.md](project-status.md)。选型见 [tech-stack.md](tech-stack.md)，行为约束见 [system-design.md](system-design.md)。

2026-09-15 补记：A1-05 在实现中新增但未登记到本计划，现按原有 ID 回填，依赖与验收一并写清，不改动既有编号。其后续工作按归属拆分，不并入本任务：transcript→模型输入映射归 A3-01，真实沙箱后端归 C-01/C-02。

2026-09-15 补记：台账与 [api-auth.md](api-auth.md) 一直引用 D-01-01/02，但本计划只登记过 D-01-03，现按实际交付内容补齐两行，不新增编号。D-01-01/02 是验签与认证入口，D-01-03 是数据库资源授权，D-01-04 是在其之上补齐的撤销判定。

## 1. 工程目标与首版范围

首个端到端版本先用“订单未收到，但物流可能显示签收”这条调查作为纵向切片：受理 → 查订单/物流 provider → 形成待核实事实 → 生成补充资料草稿 → 持久等待 → Worker 被停止 → 收到回复 → 另一 Worker 恢复 → 输出有来源的建议。Local profile 使用合成 provider；真实 provider 通过同一契约接入。

阶段 A 的完成含义是“调查建议已生成，仍可交人工判断”，不是退款成功或 Case 自动关闭。通知和外部动作必须经过对应 provider、审批与幂等门禁；Local profile 默认不向真实客户发送、不退款、不补发、不操作商家账号，也不处理任意可执行附件。A0–A2 不依赖真实模型 API Key。

阶段总顺序是 A0 → A1 → A2 → A3。B 的台账可在 A2 后开展，完整业务闭环验收依赖 A3；C 的隔离工具可在 A2 后与 B 并行，真实业务副作用试点必须经过 B 的对应门禁。D 按实际启用的功能验收，不为“齐全”强制安装所有候选组件。

本文是执行范围，不是自动执行授权。每轮先匹配用户请求，只领取已获准且前置条件满足的任务。

## 2. 任务状态与完成定义

任务状态只写在状态台账：BACKLOG（未开始）、READY（依赖满足的候选，不代表获准开工）、IN_PROGRESS、BLOCKED、DONE、DEFERRED。未在状态表列出的下方任务默认 BACKLOG；必须保留 ID，不因改名重新编号。

DONE 需要同时满足：目标文件可定位、对应验收有实际证据、无已知阻断缺陷、文档与代码边界一致。提交/推送/部署是另外的状态，不能用 DONE 推断发生过这些动作。Fake 验收不能替代真实供应商、真实模型或生产压测。

每个代码任务开始前确定负责人和文件范围。可并行的工作只在契约固定后并行；主 Agent 统一维护状态台账，子 Agent 汇报变更与验证证据。改变核心不变量时先写 ADR，再同步接口、测试与计划。

## 3. DOC：执行记录基线

### DOC-001 — 建立执行与恢复记录

- 目标：不依赖聊天摘要即可知道技术方向、事实状态与下一步。
- 文件：根 AGENTS.md、本文、tech-stack.md、project-status.md，以及 README/ROADMAP/贡献指南的入口对齐。
- 验收：本地链接、任务 ID/状态引用、文档一致性和 Git whitespace 检查；已有架构 Mermaid 语法不被改坏。

### DOC-002 — 定义开源版 v0.1 准入门禁

- 依赖：DOC-001、A0-01；可与 A3-02 的实现切片并行，但不得把生产部署或真实业务动作默认为本任务范围。
- 目标：把 Aftercare 作为真实售后场景开源参考实现的交付边界、三种运行配置、适配器路线、仓库整理、可复现安装、开发容器、测试/CI、安全、许可证和后续部署缺口写成一份可执行的准入文档，并让 README 指向它。
- 文件：docs/open-source-v0.1-readiness.md、README.md、docs/project-status.md；如后续落实许可证、安全政策或容器改动，分别建立独立切片和验证记录。
- 验收：文档能区分 local/integration/deployment profile 及其验收门槛；每项缺口有状态、目标文件、验证方式和真实风险；README 不把适配就绪误写成环境已验收；明确当前尚未完成的生产部署、真实供应商联调和 EGM 调查 schema，而不缩小业务内核的目标范围。
- 边界：这项完成不表示任何业务运行时任务完成。

### DOC-003 — 收敛模型与真实部署路线

- 依赖：DOC-002、A0-02；不替代 A3/B/C/D 的实现任务。
- 目标：在模型 provider 可替换的前提下，按“业务契约 → 调查证据闭环 → 人工与动作台账 → 集成环境 → 目标环境部署”的顺序推进成熟度；具体模型不进入关键路径，直到 Harness、权限、降级和成本/延迟验收完成。
- 文件：`docs/decisions/0011-model-provider-boundary.md`、`docs/open-source-v0.1-readiness.md`、`docs/project-status.md`；运行时只在对应 A3/B/C/D 切片中实现。
- 验收：主线不依赖特定模型；local profile 可离线运行；integration profile 有可替换 provider 和失败语义；deployment profile 的身份、连接器、EGM、沙箱、备份、观测和回滚都有目标环境证据。
- 顺序：先完成 A3-02 调查 EGM schema 与来源桥接，再完成 A3-04 业务 Harness，随后推进 B-02/B-03 人工和外部动作闭环，最后推进 C/D 的部署、恢复、容量和运营验收。
- 风险：如果过早把模型表现当成产品完成度，会掩盖证据、权限和外部动作的故障；如果只做 local profile，又无法证明目标环境可部署。每个阶段都必须分别记录代码完成、适配就绪和环境验收。

## 4. A0：工程基线与业务契约

### A0-01 — 可复现 Python 包与测试入口

- 目标：新环境能安装正确版本的 Aftercare/EGM 并运行现有退款证据回归。
- 文件：pyproject.toml、uv.lock、.python-version、必要的包/测试配置与开发说明；不移动或覆盖既有 evidence.py 的行为。
- 实施：按技术栈固定 Python 3.13 的已验证补丁、Aftercare 的直接依赖、EGM 确切提交；配置 Ruff/mypy/pytest。先核对 EGM 脏工作区，不将无关未提交内容打成“固定提交”的包。
- 验收：干净隔离环境锁定安装、包可导入、现有测试通过、lint/type 检查有明确范围；不把忽略全部错误当通过。记录实际版本和命令。
- 风险：本地 editable 依赖遮蔽打包缺失，旧缓存掩盖缺依赖，错误地把 EGM 的全部 dev 依赖带入运行镜像。

### A0-02 — 运行时与调查接口定稿

- 依赖：可以与 A0-01 的环境准备并行；进入 A1 前必须通过契约评审。
- 文件：[运行时契约](contracts/runtime-v1.md)、[调查证据契约](contracts/investigation-v1.md)，及 aftercare_agent/domain/、tests/contracts/ 中最小类型和校验测试。
- 目标：固定身份来源、Case/Session/Run/Step/Attempt/Wait 的关系、业务 ID、受理幂等、状态转换、检查点与事件版本，以及完整/错误工具请求的语义。
- 数据边界：记录哪些字段是可信宿主赋值，哪些允许模型选择；租户/订单/来源/金额/观察时间不能因来自模型 JSON 而可信。
- 验收：两名实现者能对同一个示例得到相同的输入校验、状态变化和错误分类；正反例可由测试表达。不是再画一张无接口的架构图。

契约至少明确下面八项，不要求此时编写完整生产实现：

| 契约 | 必须固定的要点 |
|---|---|
| 受理与身份 | 服务端授权 Tenant/Case；幂等键的租户/入口范围、请求哈希、同键改参数冲突 |
| Run 执行权 | Run 权威 owner/token/lease_until；DB 时间；原子领取/续租/受保护提交 |
| 检查点 | schema_version、输入版本、工具结果/协议引用、预算和下一步；不支持版本不能静默恢复 |
| 等待 | wait_id/generation、匹配键、deadline、早到回复、超时竞争、后继唤醒的同事务边界 |
| 事件 | 事件 ID、Case 内序号、因果关联、每消费者去重、投影检查点 |
| 调查证据 | 订单快照、物流观察、买家陈述各自可支持的声明；来源、绑定、新鲜度与撤回规则 |
| 错误 | 参数错误、未授权、冲突、失去租约、限流、可重试、结果未知，不合并成一个 retry |
| 调查结束 | 需要哪些引用、哪些矛盾必须人审、何时只产出建议；不得直接等同 Case 关闭 |

### A0-03 — 合成案件与确定性评测基线

- 依赖：A0-01、A0-02。
- 文件：evals/cases/、evals/expectations/、模拟订单/物流数据与测试。
- 范围：正常、缺材料、矛盾、陈旧来源、另一订单、另一租户、无法判定、重复输入、伪造指令。
- 验收：相同输入具有稳定的预期业务判断和失败类别；数据全部合成，不泄露真实客户资料。
- 注意：“承运商报告签收”不等于“买家实际收到”；现有 refund_completed 不能代替调查声明。

A0 出口：环境可复现、接口已评审、合成案件有预期；无需真实模型、前端、沙箱或付款权限。

## 5. A1：确定性持久运行骨架

| ID | 目标与文件/模块 | 依赖 | 验收 |
|---|---|---|---|
| A1-01 | persistence/：Case/Session/Run/Task/Step/Attempt/Checkpoint 的最小 PG 迁移、Repository、事务边界和执行权 | A0-01、A0-02 | 重复创建不重复受理；Run 单调 token；旧/过期持有者不能提交；迁移失败和版本冲突可识别 |
| A1-02 | api/、auth/：受理、读取 Case/Run；显式本地合成身份与真实认证接口边界 | A1-01 | 跨租户/工单引用拒绝；请求体不能自行选择授权身份；生产模式拒绝测试身份 |
| A1-03 | runtime/、model_adapters/fake、connectors/：有限片段、FakePlanner、模拟只读工具、结构化日志 | A1-01、A0-03 | 固定步骤可从检查点恢复；有轮数/时长/工具预算；不持长数据库事务等待工具 |
| A1-04 | deploy/compose/、CI 与实际开发说明：最小 PG/API/Worker 环境 | A1-01；完整启动验收依赖 A1-02、A1-03 | 干净环境可按已验证命令启动；CI 使用真实 PG；无真凭证；存储与卷位置明确 |
| A1-05 | persistence/、domain/、sandbox/：append-only Session transcript 引用（tenant/case/session 隔离、连续序号、message_id 幂等、游标读取）与 provider-neutral 沙箱生命周期契约 | A1-01 | 重复追加不产生第二条消息；跨租户/Case/Session 读取为空或被拒绝；序号连续且可在重启后按游标恢复；沙箱契约不含具体后端实现 |

A1 从第一版就包含基础租约/fencing，不先写一个以 Python 内存锁为权威、之后再替换的运行时。暂不引入完整支付 Action Ledger、对象存储平台或观测套件。

A1-05 只固定持久引用与契约边界：message 正文留在 artifact store，真实 artifact store、Responses transcript 到模型输入的映射，以及 E2B/Kubernetes 沙箱后端分别属于 A3-01、C-01 和 C-02，不在本任务内验收。

A1 出口：可以从 API 受理并驱动一个受权限约束的固定调查步骤；执行权可撤销，状态保存在 PostgreSQL，而非 HTTP 请求对象中。

## 6. A2：等待、可靠事件与跨实例恢复

| ID | 目标与文件/模块 | 依赖 | 验收 |
|---|---|---|---|
| A2-01 | runtime/waits、events/：Wait、Inbox、每消费者应用记录、原子唤醒、Outbox、到期扫描 | A1-01、A1-03 | 重投不重复推进；早到回复和超时竞争不丢唤醒；通知仅草稿/模拟记录 |
| A2-02 | tests/integration、tests/faults：两个独立 Worker 的领取、失联、接管、旧 Worker 返回；`LeaseHeartbeat` 的续期延迟预算（心跳线程独占一条连接、首个续期立即执行、按绝对截止时间排程） | A1-04、A2-01 | B 接管后 A 写入被拒绝；重启恢复原 Run；等待释放执行槽；未提前确认未提交输入；租约窗口只需覆盖“心跳间隔 + 一次往返”，续期不得为每次 tick 付建连开销 |
| A2-03 | runtime/admission：全局/租户有界执行槽、重试预算和基础公平调度 | A2-02 | 噪声租户不能无限占满执行资源；限额计入跨实例而非每进程各自一份；不宣称已知生产并发上限 |

A2 出口：不调用真实模型，也能重复证明受理、等待、重投和恢复不变量。SSE 前端尚未接入，但事件/投影的游标和乱序语义已具备可测试的接口。

## 7. A3：调查 Agent、证据接入与最小工作台

| ID | 目标与文件/模块 | 依赖 | 验收 |
|---|---|---|---|
| A3-01 | model_adapters/：Responses、完整调用校验、原生协议保存、错误和用量归一化 | A2-02、A0-02 | 半截参数不执行；未知/越权工具拒绝；Fake 回归保持；真实调用另受凭证/预算/数据门禁 |
| A3-02 | investigation/：订单/物流/买家材料的受限 EGM 接入；固定调查 schema、Case→EGM node 绑定和 revision/operation 投影 | A2-01、A0-02、A0-03 | 来源不能冒充；订单/租户/时间错配拒绝；“报告签收”不升级为实际收货；重复观察与 EGM operation 可恢复；真实 PostgreSQL Worker、撤回/恢复矩阵和来源认证分别验收；保持退款回归兼容 |
| A3-03 | web/、api/SSE、events/projection：React/TS 工作台与授权进度 | A1-02、A2-01 | 一致快照与续传；Case 事件乱序不倒退；权限变化重建范围；不泄露模型密钥或后台凭证 |
| A3-04 | evals/、集成说明：有限调查 Harness 的完整业务评测 | A2-03、A3-01、A3-02、A3-03 | 同一闭环可复现；建议有来源；不确定结果转复核；分别报告离线工程测试和真实模型效果/成本 |

A3-01 的 Fake/协议测试可在没有付费凭证时完成；若真实模型验收尚未运行，明确保留部分完成/阻塞项，不能把 A3 整体标 DONE。具体模型不在此硬编码，根据任务集验证后固定配置。

EGM schema 新增是独立仓库变更，必须先检查该仓库约束与用户授权、现存未提交内容。Aftercare 的依赖固定提交只在对应 EGM 验收完成后更新，不能用未固定的本地新 schema 冒充可复现接入。

A3 出口：本篇第 1 节的完整调查闭环可展示且可恢复；仍没有自动发邮件、退款和补发权限。

## 8. B：受控外部业务动作

| ID | 目标与文件/模块 | 依赖 | 验收 |
|---|---|---|---|
| B-01 | actions/、persistence/：Action Ledger、参数哈希、业务唯一键、支付聚合与额度预留 | A2-02、业务动作契约评审 | 跨 Case 同义务去重；同键不同金额冲突；UNKNOWN 仍占额度；不同义务总额受约束 |
| B-02 | 审批、派发、模拟供应商、回执核对、EGM join/投影测试 | B-01 | 审批绑定参数/身份/策略/有效期；迟到超时不覆盖成功；本地回滚不导致盲目重发 |
| B-03 | 已选供应商测试连接器、Webhook 验证、通知/退款对账与人工接管 | B-02、A3-04、所选渠道授权 | 稳定外部键、结果未知核对、来源签名/去重通过；测试环境与真实账户明确分开 |

B-02 按以下可验证切片推进，避免把“审批台账”误当成完整人工闭环：

- **B-02-01**：持久化审批记录，绑定 Action 参数摘要、策略版本、申请/审批身份和有效期；派发缺少匹配批准时 fail closed。
- **B-02-02**：审批请求可绑定 `WAITING_APPROVAL` 的 `run_id/wait_id/generation`；决定在 Case → Run → Wait → Action → Approval 锁序中写入 Inbox 并原子唤醒对应 Run。
- **B-02-03**：真实认证/审批 API、支付聚合、模拟/真实供应商、回执核对和 joined 事务故障矩阵。未完成 B-02-03 前，不开放真实外部动作。

真实邮件同样是外部动作；如需要提前发送，必须提前抽出并完成对应的 B 类能力，不能靠“只是一封通知”绕过操作台账。不支持可靠幂等/查询的供应商，在未知结果下保留人工核对路径。

## 9. C：按需沙箱与试点部署

| ID | 目标与文件/模块 | 依赖 | 验收 |
|---|---|---|---|
| C-01 | sandbox/：Fake Provider、allocation_id、资源租约、预算、产物、Reconciler | A2-02、沙箱接口评审 | 创建响应丢失可找回；重复分配可核对；销毁确认前不盲目释放容量 |
| C-02 | 选一个真实后端、模板、隔离、出口和短期能力 | C-01、驻留/权限/预算确认 | 不触达数据库和主密钥；租户隔离、OOM/失联、过期回收与脏环境不复用经过验证 |
| C-03 | 对象存储、OTel、发布与回滚、按需要的 K8s 配置；`deploy/deployment_preflight.py` 负责启动前配置形状检查，`deploy/deployment_acceptance.py` 固定目标环境九项验收记录（含租户隔离与 PITR，见 [ADR-0012](decisions/0012-pitr-as-deployment-gate.md)），`deploy/release_evidence.py`、`deploy/release_policy.py` 与 CI `release-evidence` Job 维护发布证据及晋级判定 | A3-03、试点环境确认；启用沙箱时还必须完成 C-02 | 文件授权与留存明确；诊断不替代审计；能在选定环境发布/回滚且不谎报业务回滚；启动前配置错误 fail closed，目标环境九项检查均需外部证据，PITR 必须绑定物理基线与恢复报告，镜像身份、SBOM/provenance、依赖与 secret 扫描结果可归档，未完成签名/目标仓库复扫/目标环境验收时晋级判定为 hold |

最小 Compose 计划在 A1 交付；C 才增加隔离执行与试点配套。K8s 不是本阶段无条件必选项。无文件/脚本能力的首个试点可以不启用沙箱，必须如实限定功能；启用不可信执行则不能跳过 C 验收。

## 10. D：生产准入与基于测量的扩展

| ID | 目标 | 验收 |
|---|---|---|
| D-01-13 | 参考部署环境与 RLS 边界（[ADR-0015](decisions/0015-target-deployment-and-rls-boundary.md)）：固定 Kubernetes + 托管 PostgreSQL 参考路径、migration/runtime/backup 角色分层、事务本地租户上下文和 RLS 实施顺序；镜像提供 `aftercare-rls harden/verify`，integration 结果写入可归档 JSON，由 `verify_rls_evidence.py` 归一化并由 `bind_deployment_evidence.py` 写回验收记录；通过记录还必须保留归一化来源的 `hardened` 标记和两份原始报告摘要 | 先完成统一租户上下文接缝；integration PostgreSQL 以 runtime 角色验证正确/错误/空上下文、连接池借还、owner `FORCE RLS` 和跨租户读写，并保存、归一化、绑定 harden/verify 产物；API Bearer、CaseGrant、Worker claim、SSE polling 都走同一接缝；RLS 策略与目标环境角色属性有可复核证据；未完成目标 Job 实际运行前不把参考 YAML 或本机 Compose 宣称为生产隔离 |
| D-01 | 真实认证/授权、密钥、渠道/数据审查、人工接管与生产变更规则 | 仅授权人员和任务可执行对应动作；测试身份禁用；明确可用范围与操作留痕 |
| D-01-01 | auth/oidc.py：provider-neutral JWT/JWKS 验签与 claims 映射；静态 HTTPS JWKS、算法/typ 白名单、短期缓存、未知 kid 单次冷却刷新 | A1-02 | 验签失败、过期/未生效、超长寿命、错误 iss/aud、`none`/HMAC 混淆、非签名用途 key 一律 fail-closed；claims 映射出的 AuthContext 不可由请求体覆盖 |
| D-01-02 | api/：`Authorization: Bearer` 认证入口；Bearer 优先于合成身份且验签失败不回退 | D-01-01 | 携带 Bearer 时合成 Header 被忽略；配置真实 verifier 后合成身份强制关闭；缺认证 `401` 且带 `WWW-Authenticate` |
| D-01-03 | auth/、persistence/、api/：PostgreSQL CaseGrant 资源授权切片；token case_ids 仅作收窄，撤销/过期在短事务内 fail-closed | A1-02、A1-01 | `(tenant_id,subject_id,case_id)` grant 迁移与 revision；API 同事务锁定活动授权；创建者与受理原子授予；撤销/过期及跨主体回归通过；授权管理 HTTP 由 D-01-05 开放 |
| D-01-04 | auth/introspection.py、auth/guard.py、api/：RFC 7662 撤销判定；静态 HTTPS endpoint、凭据只存注入客户端、有界 TTL 缓存、验签后按 active/sub/tenant 一致性 fail-closed | D-01-01、D-01-02 | endpoint 非 HTTPS 或凭据缺失时拒绝启动；`active=false`、sub/tenant 不一致、已过期判定、传输/解析/超大响应失败均返回 `UNAUTHENTICATED` 且不回退到合成身份；缓存不存 token 本身、有界且不缓存已过期判定 |
| D-01-05 | auth/grants.py、persistence/、api/：Case 授权管理面；租户级 grant scope、可授予闭集与委派上限、乐观并发替换/撤销、同事务审计事件 | D-01-03、D-01-04 | 管理员能把他人工单移交给另一主体并由其真实决策 Review；撤销后重新 `403`；重放或过期 revision 返回 `409`；非管理员 `403` 且无写入；闭集外权限 `400` 且不落库；不能授出自己没有的权限；每次变更在同一事务写入 `case_grant.granted`/`case_grant.revoked` 与完整快照 |
| D-01-06 | web/：单工单访问管理工作台；闭集/委派上限的界面映射、revision 乐观并发、审计事件回显 | D-01-05、A3-03 | 授权后可在他人持有的工单上完成授予、撤销与替换，并在事件时间线看到 case_grant.granted/case_grant.revoked；越权、越界权限与过期 revision 都给出可读错误；前端单测、`tsc --noEmit` 与生产构建通过，并有真实浏览器的端到端冒烟 |
| D-01-07 | auth/grants.py、persistence/、api/：面向 `grant:read` 的租户工单发现；控制面清单、清单投影闭集与授权台账的调用者字段 | D-01-05 | 持 `grant:read` 的管理员能列出本租户工单标识，并据此移交一件自己未参与的工单；清单只含标识与进度，不含 Case 内容、Run、Review、Approval 或事件流，内容路由仍逐工单判定；清单 `permissions` 只报调用者自己的管理 scope，绝不复用逐 Case 投影；token `case_ids` 只收窄清单；台账返回 `delegable`/`can_administer` 并由服务端裁决；非管理员 `403`，跨租户不可见 |
| D-01-08 | Review 恢复控制面：`review:override`、预算/deadline 增量契约、不可变 override 审计表、checkpoint 与 `REVIEW→READY` 原子提交 | A3-01、D-01-05 | 普通 `CONTINUE` 不能绕过耗尽门；仅有额外权限且 checkpoint 版本匹配时才能按耗尽维度增加预算或延长 deadline；旧 Worker/重复幂等键/跨 Case 记录被拒绝；事件快照、回归和 API 文档能证明操作者、理由、增量和新 checkpoint 版本；模型策略切换另立任务，不在本切片伪装完成 |
| D-01-09 | 真实身份边界验收：真实 JWT/JWKS 验签、数据库 CaseGrant、撤销后的 API 重新授权闭环 | D-01-01、D-01-03、D-01-05 | 使用实际 `JwtJwksVerifier`（不是 stub）和 PostgreSQL CaseGrant，通过 API 验证无授权→拒绝、授权→成功、撤销→再次拒绝；token claim 只能收窄数据库授权；验收不连接生产 IdP，但保留可替换 provider 接缝和明确的目标环境待办 |
| D-01-10 | 版本化模型策略边界（[ADR-0013](decisions/0013-versioned-model-strategy.md)）：`ModelStrategy`、checkpoint 配置版本和策略漂移 fail-closed | D-01-08、D-01-09、DOC-003 | 新 Run 记录 strategy/model/config/policy/tool schema 身份；恢复时版本不一致在任何模型调用前进入 `model_strategy_changed → REVIEW`；普通 Review/预算 override 不能绕过；策略热切换、迁移和回滚入口另立任务并保留旧 Run 的审计可追溯性 |
| D-01-11 | 策略迁移控制面：`strategy:migrate`、`022_strategy_migrations.sql`、显式旧/新身份契约、checkpoint 迁移与不可变事件；迁移后仍必须由普通 Review 决定是否继续 | D-01-10、D-01-05 | 只有 Case 授权的迁移操作者可在 `model_strategy_changed` 停止点按 checkpoint 版本执行一次迁移；同键重放返回同一审计记录，改参/跨 Case/无 pending Review/非策略漂移 fail closed；新 checkpoint 带 `strategy_migration_ready`，Run 仍是 REVIEW，后续 `CONTINUE` 才能进入 READY；迁移事件、离线契约回归和真实 PostgreSQL 事务回归通过 |
| D-01-12 | 策略迁移真实身份验收：生产 `JwtJwksVerifier` + PostgreSQL CaseGrant + API 的无授权/授权/撤销闭环 | D-01-11、D-01-09 | 没有 CaseGrant 时策略迁移返回 `403`；授予 `strategy:migrate` 后同一 JWT 可成功迁移；撤销后同一 JWT 再次返回 `403`；验收使用实际签名 JWT、真实 JWKS verifier 和真实 PostgreSQL，不连接外部生产 IdP |
| D-02 | PostgreSQL/对象备份恢复、保留删除、RPO/RTO、版本升级；已交付 `aftercare-backup`（[ADR-0008](decisions/0008-backup-and-restore-drills.md)、[ADR-0009](decisions/0009-backup-freshness-and-drill-records.md)、[ADR-0010](decisions/0010-wal-archive-checks.md)）以及物理 PITR 证据校验器和本机 Docker profile：`create` 在同一次 `REPEATABLE READ` + `pg_export_snapshot()` 里读 schema/行数并让 `pg_dump --snapshot` 导出，dump 与清单描述同一时刻；`verify` 离线核对摘要、字节数与迁移漂移；`drill` 把 dump 恢复到新建临时库后逐表比对行数、可选跑 `migrate()` 演练升级、最后删除副本，并在 dump 旁边写一条 `<name>.drill.json`（失败也写，且不掩盖原异常）；`status` 按部署给出的 RPO/演练预算判定"最新恢复点多老、最近一次成功演练多老"，预算没有默认值、未配置只报告不判定；`wal` 检查恢复点**之后**的 WAL 归档是不是连续、归档器还在不在推进、有没有覆盖最新那份 dump，滞后预算同样没有默认值，`--dsn` 可选（服务器不可用时正是最需要查归档的时候）；`pitr` 绑定物理 `pg_basebackup` 清单的文件摘要、字节数和 WAL 起止位置，只校验证据；`retention` 先出计划再由 `--apply` 执行、永不删空并连同演练记录一起删；`reconcile` 从 `aftercare_actions` 列出 `UNKNOWN` 与恢复点之后被更新的 Action（默认留 60 s 安全边界）。本机 Docker `postgres:17` 已真实完成物理基线 → WAL 前滚 → `restore_command` 时间点恢复，五项 PITR 检查通过；传统 dump 演练与新鲜度、归档链路也有真实 PostgreSQL 实测，报告见[运维文档](operations/backup-restore.md) | 实际恢复演练、恢复点之后外部动作核对；不是只检查备份任务显示成功。仍未完成：目标部署环境的 PITR、对象存储与异地副本、备份加密与密钥托管、按部署目标给出的 RPO/RTO **数值**、调度接线（只在文档里给出示例，未在目标环境执行过）、多租户级选择性恢复 |
| D-03 | 稳态/突发/集中唤醒压测、成本、资源限额和扩容；已交付 `aftercare-capacity` 池容量 sweep 与池指标（[ADR-0007](decisions/0007-pool-metrics-and-capacity.md)）：steady/burst/wake 三种形状、按 `max_size` 扫描、以 p95 预算为判据、`--service-time-ms` 与 `--min-size` 分别建模“工作单元占槽多久”和“突发要不要付握手”，退出码 0/1 可直接用于 CI；每个进程默认每 10 s 发布 `aftercare.db.pool.*`（`AFTERCARE_POOL_METRICS=0` 关闭），Worker 另发布 runnable durable queue 的 `aftercare.queue.runnable_runs` 与 `aftercare.queue.oldest_age_seconds`（`AFTERCARE_QUEUE_METRICS=0` 关闭，查询只读短事务，唯一标签 `component`）。本机实测（16 并发、10 ms 单元、p95 预算 50 ms）：懒增长 `min_size=1` 时 1–32 没有尺寸达标（steady 的 p95 124–262 ms，上限 8→32 时 in_use 峰值只从 8 升到 10）；预热 `min_size=16` 时 16 达标（p95 31.3–39.1 ms、池排队 0 ms）且 32 无差别；纯 `SELECT 1` 对照下 1/8/16 都达标。结论是突发并发先撞握手与懒增长、不是上限，因此默认值不变（API 1/8、Worker 1/4） | 公布版本、工作负载、失败率、延迟/队列年龄和资源成本，不拿样例参数当 SLA；方法、口径与本机报告已给出（[容量报告](capacity/README.md)），仍缺容器/CI 复跑、OTel 导出器（C-03）、目标环境告警阈值与生产容量结论 |
| D-04 | 按瓶颈评估扩展组件：已按测量启用有界连接池（[ADR-0006](decisions/0006-bounded-connection-pool.md)），Broker、暖池、ACP、长期记忆与额外供应商保留为候选 | 连接池：每工作单元仍按需借还，池有界（默认 min 1 / max 8，可用 `AFTERCARE_DB_POOL_*` 与环境超时覆盖）、借用有上限且超时 fail-closed、`startup()` 预热、心跳改为 `open()`/`release()` 借还并占一个槽、`Database.direct()` 保留无池对照。实测：25 个连续事务只落到 1 条物理连接、稳态 0.47 ms/事务（对照每事务建连 114.8 ms），同一批 509 个既有用例本机 79.0 s → 30.4 s，`PostgresEventTail` 每轮轮询不再付建连并有回归用例。未启用项各写触发条件。D-03 已给出定标方法与本机 sweep（见 D-03 行），池指标已接入并默认发布（ADR-0007）；仍缺容器/CI 复跑与生产环境定标 |

D-01-13 的前置条件：参考部署与 RLS 边界见 [ADR-0015](decisions/0015-target-deployment-and-rls-boundary.md)。统一事务租户上下文接缝、迁移 023、runtime `NOBYPASSRLS`、镜像内 `aftercare-rls harden/verify` 和 integration PostgreSQL 正确/错误/空上下文、连接池借还、owner `FORCE RLS`、跨租户读写验收已完成；API Bearer、CaseGrant、Worker claim、SSE polling 已走同一接缝。剩余工作是目标 Linux/Kubernetes Job 实际运行、真实 IdP、备份/队列复核和 deployment acceptance，在此之前不把本机 Compose 宣称为生产隔离。

D 的前置条件取决于启用功能，但生产调用不允许跳过真实身份与授权检查；含外部动作需 B 验收，含不可信工具需 C 验收。真实渠道身份/数据约束要在第一次真实接入前检查，不能等到最终上线日。

## 11. 跨阶段的八项核心验收

1. 租户/Case 隔离：伪造身份、越界引用和共享工具闭包不能跨范围读取或写入。
2. 可撤销执行权：两个 Worker 只能有一个当前持有者；旧 token/过期租约不能提交。
3. 原子输入应用：重复投递不重复推进；等待转换、消费者记录和后继任务同事务。
4. 等待恢复：早到回复、超时和旧代次不丢唤醒；等待期间无常驻事务和执行槽。
5. 证据语义：来源、对象、时间、声明分别约束；观察与结论不可混同。
6. 模型/通知边界：完整参数前不执行；不可信工具请求拒绝；A 阶段没有真实外发。
7. 检查点/事件可靠性：已提交变化不因崩溃或分配/提交乱序而漏消费，重复应用安全。
8. 外部动作一致性：UNKNOWN 不盲目重做；同义务跨 Case 去重；joined 本地事务可一起回滚，但不能撤回外部 HTTP。

每项均记录测试环境、源码版本、数据来源、命令、结果和未覆盖项。合成业务质量评测与并发故障测试分别报告，不能用一个“成功率”概括全部质量。

## 12. 执行循环与接手检查点

开始一个任务：核对授权/依赖 → 阅读契约与源码 → 选定文件 → 写失败用例或验收脚本 → 实现最小闭环 → 运行检查 → 复核边界 → 更新状态。没有要求为每个小改动写大型设计或完整伪代码。

任务结束或可控交接时，状态记录必须回答：当前 task_id；实际完成了什么；哪些文件未提交；跑了哪些验证及其结果；什么没验证；剩余最小下一步；是否需要用户选择；是否提交、推送或部署。终端 session/PID 只有在确实仍运行且接手需要时记录，并注明再次核验，不能当永久有效句柄。

当前最大风险：阶段范围膨胀、把 EGM 退款切片当通用调查、把进度文件当授权、把历史测试当本轮结果，以及把模拟数据闭环当真实业务价值。用上述任务与验收控制这些风险，而不是继续增加框架数量。
