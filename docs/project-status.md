# 当前工程状态与接手点

格式版本：1。最后核验日期：2026-09-16（Asia/Shanghai）。记录者：本轮主 Agent。

本文件是进度与交接的唯一台账，不是实际代码/测试的替代证据，也不是自动执行授权。先读根 [AGENTS.md](../AGENTS.md)，任务定义见 [工程执行计划](engineering-plan.md)。

## 1. 当前工作位置

| 字段 | 值 |
|---|---|
| 本轮请求范围 | 用户 2026-09-16 明确本轮范围与授权：“先提交一下”，然后继续“把模型驱动的失败路径做成持久路由（含 Worker 按 `next_step` 决定 Run 状态，需要真实 PG 故障测试）→ 把连接器答案桥进 EGM 变成可审计证据 → 才谈真实沙箱后端”，并授权提交与推送本轮。本切片只做第一段：把模型/工具失败与预算耗尽做成**持久路由**（`RouteReason` + 检查点不变式 + Worker 把 Run 落到 `REVIEW`/`RETRY_AT` + 真实 PostgreSQL 故障测试）。整库目标仍是“企业级高并发沙箱审计记忆门控 Agent”，连接器业务数据参照 Accio CommerceAgentBench 的场景形状（只作形状参考，不 vendor 其数据、品牌资源或录制响应）；不部署、不发布包、不连接生产数据库、不做真实退款或外发 |
| 当前任务 | A3-01 第四段：模型驱动失败路径的持久路由。**问题**：模型预算耗尽、provider 拒绝、工具意图被拒这三类失败此前要么抛异常穿出切片、要么把 Run 留在 `RUNNING` 直到租约过期——前者丢掉“已经买下的那次调用”的记账，后者在数据库里看起来像“有人在跑”，却没有任何状态能让运维发现它。**做法**：`domain/protocol.py` 新增 `RouteReason`（9 个原因）与 `Checkpoint.route_reason`，并加不变式“只有 `review`/`retry` 检查点带原因”；`runtime/model_harness.py` 的失败路径不再抛异常，改为 `_route()` 落一个可续跑检查点（`retry` 必须带 `available_at`），`_cost()` 在 provider 未报 usage 时返回 `None`（失败只扣一次 `model_calls`，**不猜钱**），可重试故障按 `retry_backoff` 落 `retry`、其余落 `review`，工具意图校验失败与执行器拒绝**故意不重试**（拒绝不消耗工具预算，重试只会无限重放同一个坏连接器）；`runtime/worker.py` 用 `_RUN_STATE_FOR_STEP` 把 `complete`/`review`/`retry` 映射到 `COMPLETED`/`REVIEW`/`RETRY_AT`，其余阶段仍回 `READY`；`runtime/wiring.py` 新增 `AFTERCARE_HARNESS_RETRY_BACKOFF_SECONDS`。`REVIEW` 是基础设施里早就存在、此前从未被用的状态（`RUNNING→REVIEW` 合法、触发器会删掉它的队列行＝必须显式放行），所以“需要人处理”不占用 `WAITING_INPUT`（那会把 Run 绑到 wait 行）。真实 PostgreSQL 故障测试 3 项覆盖：预算耗尽落 `REVIEW` 且队列无行、可重试故障落 `RETRY_AT` 且到期前拿不到而到期后能拿到并真的再请求 provider、致命故障落 `REVIEW` 后经 `REVIEW→READY` 放行并跑完。 |
| 当前阶段 | A1-03 DONE；A1-04 最小 Worker/Compose 已有；A2-01 Wait/Inbox/Outbox 与 gap buffer 基础已落地；A2-02 心跳/常驻轮询已落地并完成续期延迟预算修正；B-01 Action Ledger 最小闭环已落地；A3-03-a/b 工作台与 SSE 已完成；D-01-04 撤销判定、D-01-05 授权管理面、D-01-06 工作台访问管理与 D-01-07 租户工单发现已落地；D-04 有界连接池（ADR-0006）、D-03 池指标/容量 sweep（ADR-0007）、D-02 备份/清单/恢复演练（ADR-0008）、演练记录与新鲜度判定（ADR-0009）与 WAL 归档检查（ADR-0010）已落地；A3-01 第四段（持久路由：失败与预算耗尽落 `REVIEW`/`RETRY_AT`，Worker 按检查点决定 Run 状态）已落地并推送 |
| 下一项代码候选 | 模型接线剩余：把 `route_reason` 接进 operator 投影（现在 REVIEW 可见，“为什么”只在检查点负载里），并给预算耗尽后的放行/提额一个运维入口（原地放行只会再落同一条路由，是空操作）；沙箱剩余：真实后端（E2B/K8s）、暖池、出网与短期凭证、独立回收进程与管理 API（C-02 剩余）；连接器剩余：把连接器答案转成 `OrderSnapshot`/`LogisticsObservation` 并入 EGM、支付/退款与供应商回执核对（A3-02、B-02-03）；分层调度：`aftercare_execution_queue.priority` 只被读、从未被写，需要 class 生产者让队列排序、审批阈值与沙箱规格按层生效。D-02 剩余：PITR 演练、对象存储与异地副本、备份加密、RPO/RTO 数值、演练调度接线；D-03 剩余：容器/CI 复跑 sweep、业务队列年龄指标、生产定标；C-03：OTel 导出器；D-01：真实 IdP 演练与权限映射、RLS、租户级授权总览 |
| 活跃实现任务 | 本切片改 `aftercare_agent/domain/protocol.py`（`RouteReason`、`Checkpoint.route_reason` 与不变式）、`aftercare_agent/runtime/model_harness.py`（`_route()`/`_cost()`/`_spent_one_call()`、失败路径不再抛异常、`retry_backoff`）、`aftercare_agent/runtime/worker.py`（`_RUN_STATE_FOR_STEP`/`_run_state_for()`：按检查点而不是 `HarnessResult.completed` 决定 Run 状态）、`aftercare_agent/runtime/wiring.py`（`AFTERCARE_HARNESS_RETRY_BACKOFF_SECONDS`）；测试改 `tests/test_model_harness.py`（失败路径改为断言路由，新增 retryable、空 turn、工具预算用例）与 `tests/persistence/test_worker.py`（3 项真实 PostgreSQL 故障路由用例）。上一段的 `artifacts.py`、`sandbox/manager.py`、`connectors/`、`runtime/sandbox_executor.py`、`runtime/wiring.py` 已提交并推送（见第 5 节）。Broker、暖池、ACP、长期记忆与额外供应商仍未启用 |
| 本轮外部行为 | **本轮真的发起了外部调用**：对 `https://api.deepseek.com/responses` 用 `deepseek-flash` 跑了真模型驱动的真链路——模型连续提议 `lookup_order`、`lookup_tracking`、`request_material_draft`，每一次都真的在一份沙箱租约里执行并把答案写进内容寻址存储，最终一轮给出引用真实订单事实的结论。这些是开发验证调用，不构成业务动作（没有真实退款、补发或外发）。凭据由用户提供并以环境变量传入，未写入仓库任何文件；**该 key 已在明文对话中暴露，建议吊销重发**。本机离线全量 `547 passed, 123 skipped`，真实 PostgreSQL 16.13（55450 临时集群，`AFTERCARE_REQUIRE_DRILLS=1`）`670 passed`、零跳过，`ruff format --check .`/`ruff check .`/严格 `mypy aftercare_agent tests evals`（149 文件）通过。**未运行**：前端（未改 `web/`）、远端 CI（未推送）。提交与 CI 结果见第 5、6 节 |

## 2. 核验过的源码基线

| 仓库 | 已核验源码 HEAD | 用途 |
| Aftercare-Agent | bdda584a6037828c44ccbaf693c1db725e84cef6 | 本切片（持久路由）的提交前基线（`main`，工具执行面提交，CI run [35056169894](https://github.com/yushui2022/Aftercare-Agent/actions/runs/35056169894) 为 `success`，`postgres:17` 上 `670 passed` 零跳过）；交付提交编号以 Git 日志为准 |
| Aftercare-Agent | 43826ab566f44c4bcb0d0be6200e15a0f8b97f5b | 本轮 D-02 第三刀的提交前基线（`main`，D-02 第二刀台账提交，CI run [34970707263](https://github.com/yushui2022/Aftercare-Agent/actions/runs/34970707263) 为 `success`）；交付提交编号以 Git 日志为准 |
| Aftercare-Agent | 5cd93fa0a759af636c68f7dc419b83a39b076190 | 本轮 D-03 的提交前基线（`main`，D-04 台账提交，CI run 34962774856 为 `success`）；交付提交编号以 Git 日志为准 |
| Aftercare-Agent | e44989c8ac6e8cc15087e257724fd514082a1670 | 本轮 D-02 的提交前基线（`main`，D-03 台账提交，CI run 34965278041 为 `success`）；交付提交编号以 Git 日志为准 |
| Aftercare-Agent | 82b15d5fe22d63012069598f819c4c7989a4bbc1 | 本轮 D-02 第二刀的提交前基线（`main`，D-02 台账提交，CI run [34968581691](https://github.com/yushui2022/Aftercare-Agent/actions/runs/34968581691) 为 `success`）；交付提交编号以 Git 日志为准 |
| Aftercare-Agent | d3faa6d7911508943ec720787488c60eb195fdaf | 本轮 D-04 的提交前基线（`main`，D-01-07 台账提交，CI run 34959917168 为 `success`）；交付提交编号以 Git 日志为准 |
| Aftercare-Agent | 021b57cdcb493d7bc6baef6e3e742d2f5e35217d | 本轮 D-01-07 的提交前基线（`main`，A2-02 心跳修复的台账提交，CI run 34957312393 为 `success`）；交付提交编号以 Git 日志为准 |
| Evidence-Gated-Memory | 9c7c5d196f8e703fdc7c70546cff0dc94cc78dcd | 源码 0.6.0、嵌入式应用层与 PostgreSQL；已固定在 Aftercare 依赖中 |
这是本轮改动开始时核验的源码基线。D-04 起 `Database` 默认持有每进程一个有界 psycopg 连接池（ADR-0006）；D-03 起 `Database.stats()` 是池健康的唯一读取口，每个进程默认每 10 s 发布一次 `aftercare.db.pool.*`（ADR-0007）；D-02 起 `db.known_migrations()` 是迁移清单的唯一定义，`migrate()` 与备份清单都读它，因此“清单里的迁移摘要”和“实际建表的那份迁移”不可能各说一套（ADR-0008），第二刀起 `ops/tooling.py` 的 `BACKUP_NAME_PATTERN` 是备份与演练记录名字的唯一边界，两者不可能各写一套校验（ADR-0009）；第三刀起备份清单是 v2 并带 dump 覆盖到的 WAL 位置（`wal_lsn`，与恢复点同一快照），`wal` 只把它当输入、不重算，v1 清单仍可读但覆盖问题答不了。历史验证条目保留原日期和当时边界；本轮交付后的 HEAD 和远端状态以 Git 日志为准。

本机位置：G:\Projects\Aftercare-Agent；EGM 相邻仓库 G:\Projects\Evidence-Gated-Memory。脚本应使用仓库相对路径或显式配置，不把本机布局当其他贡献者的强制前提。

## 3. 已有与没有的东西

已存在：aftercare_agent/evidence.py 及其回归；EGM 的公共应用层、PostgreSQL 后端与 join；架构、ADR 和接入资料。工作区新增可安装的 Aftercare 0.1.0a0：pyproject.toml、uv.lock、.python-version、py.typed，以及 tests/test_package_contract.py 和[开发指南](development.md)。A0-03 新增 `evals/` 合成案件目录、固定期望、确定性 runner、12 案件回归和[评测说明](evals.md)。D-02 新增 `aftercare_agent/ops/`：`aftercare-backup` 的 `create`/`verify`/`drill`/`retention`/`reconcile` 与[备份与恢复](operations/backup-restore.md)。D-02 第二刀新增 `aftercare-backup status`（按预算判定恢复点与演练的新鲜度）与 `ops/drill_records.py`、`ops/freshness.py`；第三刀新增 `aftercare-backup wal`（归档连续性、归档器推进与覆盖最新 dump 三项判定）与 `ops/wal_archive.py`，备份清单升到 v2 并记下 dump 覆盖到的 WAL 位置。

尚未存在：完整业务 Harness、支付聚合、真实供应商连接器、沙箱接线、生产调度体系和高吞吐 live broker tail。调查 EGM 适配器和 PostgreSQL 观察账本已建立代码边界，但真实 EGM 调查 schema 尚未固定。当前已有 Fake Harness、合成 Aftercare 纵向切片（含订单/物流/买家观察与来源引用评估）、一次性/常驻 Worker、Wait/Inbox/Outbox publisher、gap buffer、Action Ledger、审批台账/派发门禁、审批 Wait 原子唤醒、跨实例 admission 最小闭环、显式合成身份或静态 JWKS Bearer 的 Review/Approval 控制面 API、CaseGrant 及其授权管理 HTTP（移交/授权/撤销，见 ADR-0004）、操作员工单发现 API、React 工作台、case-scoped SSE replay/tail、D-03 的池指标与容量 sweep，以及 D-02 的备份清单、恢复演练与恢复后核对。仍不是完整执行服务。真实 IdP 演练与权限映射、RLS、授权管理 UI、生产认证验收、浏览器可访问性、高吞吐 broker、OTel 导出器、跨环境容量结论、按时间点的 PITR 演练本身、异地与加密副本、定期演练的自动化与告警仍未完成（`status` 与 `wal` 只做判定，不含调度）；`aftercare-capacity` 与 `aftercare-backup` 的实测都只在一台开发机上跑过（恢复演练另在 CI 的 `postgres:17` 上跑过，见第 6 节）。不要输出不存在的完整服务启动命令。

当前 evidence.py 负责固定退款完成声明的证据验证；`investigation/egm.py` 负责受限调查观察写入与确定性评估。domain 已有运行时、协议、等待、事件与订单/物流/买家材料的独立纯契约；PostgreSQL 观察账本与重载已实现，但真实调查 EGM schema 与实际来源认证仍未实现，不能视为“EGM 已有所以调查已接通”。

技术选择已写入 [tech-stack.md](tech-stack.md)。当前实际环境是 Windows、CPython 3.13.15、uv 0.9.26；EGM 0.6.0 固定完整 Git SHA，运行依赖与测试工具在 uv.lock 中锁定。FastAPI、PostgreSQL 服务、前端 TypeScript/React/Vite、Node/pnpm、模型和沙箱仍需在各自任务引入，不能把选型表当作已安装清单。

## 4. 任务状态表

未列出的执行计划任务均为 BACKLOG。READY 只是依赖满足的候选，不代表获得了未来会话的实现授权。

| Task ID | 状态 | 证据或剩余事项 |
|---|---|---|
| DOC-001 | DONE | 四份核心记录与导航已写；两项只读复核完成；9 文件、62 本地链接、25 任务定义和 7 图语法检查通过，见第 6 节 |
| A0-01 | DONE | 锁定安装、Ruff/mypy、开发环境 9 项测试、sdist→wheel、仓库外 9 项测试和文档检查通过；见第 6 节 |
| A0-02 | DONE | 两份契约、7 个 domain 模块、5 个契约测试模块；206 项回归、类型/格式、39 类 JSON Schema、sdist→wheel 与仓库外回归通过，见第 6 节 |
| A0-03 | DONE | 12 个合成案件、v1 期望、确定性 runner 与跨进程 digest 回归通过；见第 6 节 |
| A1-01 | DONE | 新增 PostgreSQL 迁移、受理幂等、Case/Session/Step/Attempt/Run/Checkpoint Repository；隔离 PostgreSQL 17 容器中 5 项集成测试通过 |
| A1-02 | DONE | 新增 FastAPI 受理/读取与 Review/Approval operator 控制面、显式合成身份边界和公开响应投影；JWT/JWKS Bearer 与 CaseGrant 入口已接入，真实 IdP 演练仍待完成 |
| A1-03 | DONE | 新增数据库无关 FakePlanner/Harness；固定只读工具链、预算消耗、检查点 JSON round-trip 与跨 scope 拒绝通过 |
| A1-04 | IN PROGRESS | 最小 Worker/CLI、`SKIP LOCKED` READY Run 领取、`deploy/Dockerfile`、开发 Compose 和合成 Aftercare CLI 已新增；长运行基础已移入 A2-02。CI 解释器解析已修复，GitHub Actions run #44（提交 `b0ddffe`）已成功；Linux 完整启动、生产部署与重启矩阵仍待完成 |
| A2-01 | IN PROGRESS | 新增 Outbox、Inbox、消费者应用记录、Wait/wakeup、gap buffer 与 `ProjectionRepository`；真实 PostgreSQL 下事件/投影相关测试通过；SSE 回放与有界 polling tail 已接入工作台，高吞吐 live broker 仍待补 |
| A2-02 | IN PROGRESS | `LeaseHeartbeat`、可停止 `run_daemon()`、Outbox publisher 租约/重试与故障注入已实现；锁超时、失联接管和旧 Worker fencing 已在真实 PostgreSQL 验证；心跳续期延迟预算已修正——心跳线程用新接缝 `Database.open()` 独占一条连接并跨 tick 复用、首个续期立即执行、按绝对截止时间排程，续期不再为每次 tick 付建连开销，异常仍 fail-closed；完整重启矩阵和远端 CI 仍待补齐 |
| A2-03 | DONE | PostgreSQL 全局/租户执行槽、slot 租约心跳、按 Run 幂等重试预算，以及 007/008 durable queue、Run 状态同步触发器、tenant cursor 轮转和过期 IN_FLIGHT 回收已接入；真实 PostgreSQL 通过跨租户/回收/槽竞争测试；这是基础骨架，权重校准与生产压测仍未完成 |
| B-01 | IN PROGRESS | `ActionIntent`、Action Ledger、跨 Case business key 幂等、UNKNOWN/CONFIRMED/FAILED 与 claim fencing 已实现；审批门禁已补入但支付聚合、额度预留和真实供应商对账仍待实现 |
| B-02 | IN PROGRESS | B-02-01/02 已实现 `ApprovalRepository`、010/011 迁移、参数/策略/身份/有效期绑定、重复决定幂等、`RESERVED` 派发 fail-closed 和绑定 Wait 的 Inbox 原子唤醒；Review 013 已实现 `REVIEW→READY/CANCELLED` 决定边界；已补齐 `approval.*`/`review.*` Outbox 事件、Case 序号分配、不可变门控快照和显式 operator API；支付聚合和供应商回执核对仍待实现 |
| A3-01 | IN PROGRESS | 模型端已接真实 provider：严格 Responses wire parser、`ResponsesAdapter`、整数 token/cost budget、`SessionTranscriptLoader`/`build_responses_input()`、`model_adapters/transport.py`（凭据只从环境读，异常与结果都不渲染凭据）；`runtime/model_harness.py` 把“模型提议 → 校验 → 执行 → 检查点”接成可续跑切片，`Checkpoint.pending_tool` 让崩在工具阶段不必重买一次模型调用；失败与预算耗尽走**持久路由**（`RouteReason` + Worker 映射 `REVIEW`/`RETRY_AT`），并有真实 PostgreSQL 故障测试。已在真实 provider 上实测 3 轮工具调用与跨片恢复；仍缺：`route_reason` 的 operator 投影、提额/放行的运维入口、生产预算与定价、完整业务 Harness |
| A3-02 | IN PROGRESS | 新增 `InvestigationEvidenceAdapter` 与 PostgreSQL 观察账本：可信连接器规范化写入、来源事件去重/撤回/重载、模型仅提交 `InvestigationProposal`、完整授权观察集确定性评估与默认禁用长期记忆；真实调查 EGM schema、来源认证和模型接线仍待实现 |
| A3-03 | IN PROGRESS | 新增 case-scoped SSE replay、`Last-Event-ID`/after 游标、持久事件分页、有界 PostgreSQL polling tail 和 React 工作台；高吞吐 live broker tail、真实认证和生产压测仍待实现 |
| A3-03-a | DONE | 新增操作员工单发现：可访问工单列表（活动 CaseGrant 收紧、`case_ids` 仅收窄、keyset 游标）、工单详情（Run 投影不含 tenant/lease/fence）、工单下 Review/Approval 列表，以及 `web/` 最小 React+TS+Vite 工作台（列表、详情、决定、事件时间线）；真实 PostgreSQL 全量 `362 passed`（含新增 11 项），前端 `tsc --noEmit` 与 `vite build` 通过，合成身份端到端冒烟通过；live broker tail、真实认证与生产压测仍待实现 |
| A3-03-b | DONE | 工作台接入 SSE 实时订阅：`follow=true&limit=200&wait_seconds=60`，按 `case_seq` 游标续订串接有界读，`fetch`+`ReadableStream` 增量解析（不使用无法带 Header 的 `EventSource`），按 `case_seq` 去重并封顶 500 条，指数退避重连、`401/403` 终止不重试，界面显示 连接中/实时/重连中/已暂停 并可暂停改一次性回放；`web/src/sse.test.ts` 13 项、`tsc --noEmit`、`vite build` 通过，真实后端 `follow` 参数返回 `200 text/event-stream`、非法 `limit` 返回 `400`；订阅级授权、真实 IdP 与高吞吐 broker tail 仍未实现 |
| A3-03-c | IN PROGRESS | SSE follow 改为异步生成器，数据库短轮询放入线程、等待异步 sleep；工作台接入 keyset 加载更多、筛选/身份切换请求代际保护和稳定决策幂等键；异步 tail、前端 API 与回归通过，仍需生产连接池/broker 和真实身份验收 |
| A3-04 | IN PROGRESS | 新增离线闭环评测 `evals/loop.py`：脚本化 Responses client → 工具白名单/整数预算/`parse_investigation_proposal()` → 确定性评估，逐案报告 disposition、引用来源、未知引用、token 与合成成本，并给出稳定 digest 和可恢复的 Harness 步进；12 案件全部通过（1 个有来源建议、6 个转复核或补材料、5 个结构化/边界拒绝）；真实模型效果与账单、Harness 的等待/审批分支和生产压测仍未完成 |
| A1-05 | IN PROGRESS | 新增 SessionMessage append-only transcript 引用表/Repository（tenant/case/session 隔离、连续序号、message_id 幂等、游标读取）与 provider-neutral `SandboxProvider` 生命周期契约；已按 2026-09-15 回填进行计划（原只存在于本台账）；真实 artifact store、Kubernetes/E2B 后端仍待实现，transcript→模型输入映射归 A3-01 |
| C-01 | IN PROGRESS | 新增非安全边界 `FakeSandboxProvider`：allocation 幂等、fencing lease、资源/产物预算、过期回收和销毁确认前保留容量；4 项离线测试通过；真实 E2B/Kubernetes 后端未接入 |
| D-01 | IN PROGRESS | 新增 provider-neutral `JwtJwksVerifier`、FastAPI Bearer 入口、`015_case_grants.sql`/`CaseGrantRepository`、D-01-04 的撤销判定、D-01-05 的授权管理面与 D-01-06 的工作台访问管理：静态 HTTPS JWKS、算法/typ 白名单、短期缓存、未知 `kid` 单次刷新、过期缓存 fail-closed、tenant/scope/Case claim 映射；CaseGrant 默认 fail-closed，token `case_ids` 仅可收窄，grant 与 API 操作同短事务锁定；撤销判定在验签后 fail-closed；管理面用租户级 `grant:read`/`grant:admin`、可授予闭集、委派上限、乐观并发与同事务审计事件，工作台已消费；真实 IdP 演练、RLS 与跨工单授权发现仍待实现 |
| D-01-03 | DONE | PostgreSQL CaseGrant 按 `(tenant_id,subject_id,case_id)` 持久化 scope/revision/有效期/撤销审计；AuthContext 非 synthetic 未绑定时 fail-closed，API 在业务短事务锁定 grant；创建者授权与受理原子提交，撤销/过期/跨主体及 token 收窄回归通过；真实 IdP、introspection、RLS 和管理面仍待完成 |
| D-01-04 | DONE | 新增 `auth/introspection.py` 与 `auth/guard.py`：RFC 7662 判定（静态 HTTPS endpoint、凭据仅存注入的 httpx 客户端、`active` 布尔校验、sub/tenant/exp 与已验证 Token 一致性、有界 TTL 缓存且不缓存已过期判定、传输/解析/超大响应 fail-closed），`TokenAccessGuard` 在验签之后用同一时钟读数执行两步检查且失败统一为 `401 unauthenticated`、不回退合成身份，`auth.guard.TokenVerifier` Protocol 固定验签接缝，`create_default_app()` 按 `AFTERCARE_OIDC_INTROSPECTION_*` 接线并在关机时关闭自有客户端；新增 `tests/test_token_revocation.py` 72 项离线回归；真实 IdP 端点、凭据轮换、RLS 与管理面仍属 D-01 其余部分 |
| D-01-05 | DONE | 新增 Case 授权管理面：`GET/POST /v1/cases/{case_id}/grants` 与 `POST /v1/cases/{case_id}/grants/{subject_id}/revoke`，租户级 `grant:read`/`grant:admin`，可授予权限闭集（排除 `case:create` 与 `grant:*`），不能授出自己没有的权限，替换/撤销需 `expected_revision`（重放 `409`），每次变更同事务写入 `case_grant.granted`/`case_grant.revoked` 与完整快照；离线 25 项 + 真实 PostgreSQL 10 项回归通过（整机满载时的心跳用例抖动与本项无关，见第 6 节）；管理 UI 见 D-01-06；RLS 与真实 IdP 演练属 D-01 其余部分 |
| D-01-06 | DONE | 新增工作台单工单访问管理：`web/src/grants.ts`（闭集镜像、委派上限、有效期与 revision 校验、状态派生）、`GrantPanel.tsx`（列出/授予/替换/撤销）与 `App.tsx` 接线（服务端拒绝时静默隐藏面板）；前端 38 项单测、`tsc --noEmit` 与生产构建通过，并用真实浏览器对真实 API + 真实 PostgreSQL 走完授予→撤销→替换全流程（8 项断言，含 SSE 实时事件）；同时修掉时间线折叠缺陷（`reduce` 把下标当 `cap`，新事件到达会清空时间线）；跨工单发现见 D-01-07；租户级授权总览仍属 D-01 其余部分 |
| D-01-07 | DONE | 新增面向 `grant:read` 的租户工单发现（控制面）：`GET /v1/administration/cases`（只返回 `case_id`/`order_id`/`status`/`version`/`created_at`，与工单列表共用 keyset 游标契约，`limit` 上限 200）、清单投影闭集 `ADMINISTRATION_PERMISSIONS`（只报调用者自己的 `grant:*`，绝不复用逐 Case 投影，因此数据面没有放宽：内容/Run/Review/Approval/事件流仍逐工单判定）、`CaseGrantListResponse` 新增 `delegable`/`can_administer`（描述调用者而不是 Case——投影是与 grant 的交集，不持有该 Case grant 的租户管理员在投影里看不到 `grant:admin`）、`CaseRepository.list_for_tenant()` 扩为两类命名调用者并接受只收窄的 `case_ids` 上界；`tests/test_operator_grants_integration.py` 新增 5 项（发现他人未参与的工单、非 `grant:read` 者 403、租户边界、token `case_ids` 只收窄、keyset 分页），真实 PostgreSQL 全量 `509 passed`；工作台拆成"工单队列"（数据面）与"本租户其他工单"（控制面，选中时只渲染授权面板，读不到清单静默消失），前端 43 项单测 + `tsc --noEmit` + 生产构建通过；两轮真实浏览器冒烟（`95384c3` 修掉两个只有浏览器才暴露的缺陷：仅可管理的行仍订阅事件流被 403、清单区块复用队列空态文案），见第 6 节；决策见 [ADR-0005](decisions/0005-access-administration-discovery.md)；租户级授权总览、RLS 与真实 IdP 演练仍属 D-01 其余部分 |
| D-02 | IN PROGRESS | `aftercare-backup` 已交付 `create`/`verify`/`drill`/`retention`/`reconcile`/`status`（ADR-0008、ADR-0009）与第三刀 `wal`（ADR-0010）。`wal` 回答三件事：归档段是否成链（只读段名，不需要段大小）、归档器是否还在推进（`pg_stat_archiver` 的最后失败晚于最后成功 = 卡死，只有服务器能答）、归档是否覆盖最新那份 dump（清单 v2 的 `wal_lsn`，与恢复点同一快照）；滞后预算 `--archive-lag-seconds` 没有默认值，`--dsn` 可选（服务器没了也要能查目录），`.partial` 按问题处理。真实 `archive_mode` 集群实测三幕：覆盖判定退出 0；破坏 `archive_command` 后连续性仍 `ok`、只有“归档器是否在推进”退出 1；删掉中间段且不问服务器退出 1（`gaps 1`）。CI 加装 `postgresql-17` 并用绝对路径自证；演练类跳过在 `AFTERCARE_REQUIRE_DRILLS=1` 下算失败（本切片第一次推送 `629 passed, 3 skipped` 却 `success`，正是这条闸门要挡住的）。**仍未完成**：按时间点的 PITR 演练本身、对象存储与异地副本、备份加密与密钥托管、按部署目标的 RPO/RTO **数值**、调度接线（文档示例，未在目标环境执行过）、多租户选择性恢复 |
| D-03 | IN PROGRESS | 交付池容量定标方法与池指标（[ADR-0007](decisions/0007-pool-metrics-and-capacity.md)）：`Database.stats()` 返回只含整数的 `PoolStats` 快照；`observability.py` 新增 Metrics 接缝（`InMemoryMetrics`/`LoggingMetrics`/可选 OTel bridge）；`persistence/pool_metrics.py` 的 `PoolStatsSampler` 每 10 s 发布 6 个 gauge 与 7 个 counter（唯一标签 `component`，`AFTERCARE_POOL_METRICS=0` 关闭，采样失败只停线程而不失败进程）；`aftercare-capacity` 按 steady/burst/wake 扫 `max_size` 并以 p95 预算给出达标尺寸（退出码 0/1），`--service-time-ms` 与 `--min-size` 分别建模工作单元占槽时长与预热。实测（本机、16 并发、10 ms 单元、p95 预算 50 ms）：懒增长 `min_size=1` 时 1–32 没有尺寸达标（steady 的 p95 124–262 ms，上限 8→32 时 in_use 峰值只从 8 升到 10）；预热 16 时 `max_size=16` 达标（p95 31.3–39.1 ms、池排队 0 ms）且 32 无差别；纯 `SELECT 1` 对照下 1/8/16 都达标 25 ms。结论：突发先撞握手与懒增长而非上限，默认值不变；已给出可复现的压测方法与口径、三份本机报告与默认指标发布路径；仍缺容器/CI 复跑、面向业务的队列年龄指标、OTel 导出器（C-03）与生产容量结论 |
| D-04 | IN PROGRESS | 新增有界连接池（[ADR-0006](decisions/0006-bounded-connection-pool.md)）：`Database` 默认带池（min 1 / max 8 / 借用超时 5 s，`AFTERCARE_DB_POOL_*` 可覆盖，`direct()` 保留无池对照）、`startup()` 预热、借用超时 fail-closed（`RETRYABLE` → 503）、心跳改用 `open()`/`release()` 并占一个槽、`create_default_app()` 关闭自己建的池；实测 25 个连续事务在池上限 1 时固定复用 1 条物理连接（任意平台都成立，已固化为回归用例）、本机稳态 0.47 ms/事务（对照每事务建连 114.8 ms），同一批 509 个既有用例本机 79.0 s → 30.4 s，`PostgresEventTail` 每轮轮询不再付建连（有回归用例）；未启用项各写触发条件。D-03 已给出定标方法与单机报告（见 D-03 行），池指标已接入，仍缺容器/CI 复跑与生产定标 |
| C-03 | IN PROGRESS | 新增 provider-neutral Tracer、InMemoryTracer、可选 OTel bridge，并为 Outbox publish 埋点；离线回归通过；Exporter、采样/留存和生产监控尚未配置 |

## 5. 工作区与提交边界

本切片工作区改动范围（**已提交并推送**）：修改 `aftercare_agent/domain/protocol.py`（`RouteReason`、`Checkpoint.route_reason` 与不变式）、`aftercare_agent/runtime/model_harness.py`（`_route()`/`_cost()`/`_spent_one_call()`、失败路径不再抛异常、`retry_backoff`）、`aftercare_agent/runtime/worker.py`（`_RUN_STATE_FOR_STEP`/`_run_state_for()`：按检查点而不是 `HarnessResult.completed` 决定 Run 状态）、`aftercare_agent/runtime/wiring.py`（`AFTERCARE_HARNESS_RETRY_BACKOFF_SECONDS`）、`tests/test_model_harness.py`、`tests/persistence/test_worker.py`、本页。上一段（模型端接线 + Worker 接真 Harness + 工具执行面）已提交并推送为 `7170ed4`、`f667662`、`bdda584`，其基线 CI run [35056169894](https://github.com/yushui2022/Aftercare-Agent/actions/runs/35056169894) 为 `success`（`postgres:17` 上 `670 passed` 零跳过）。

EGM 仓库仍有 README.md 修改，assets/egm-roman-banner.png、assets/egm-roman-banner.prompt.md、docs/benchmark-history.md 未跟踪。这些不是本轮工程文件任务的产物，未修改、暂存或回滚。不能使用“清理工作区”删除它们，也不能把它们默默打入固定提交依赖。

提交授权：用户 2026-09-16 明确授权“先提交一下”并“也可以先授权我把本轮提交并推送”，本切片据此提交并推送；上一轮授权不自动沿用（[AGENTS.md](../AGENTS.md) 工作纪律）。推送前已在真实 PostgreSQL 16.13 与本机静态检查上验证，CI 结论并入下一次真实改动（不单独提交“只记录 CI 结果”的提交）。

本机验证环境：55450 端口上还有一个临时 PostgreSQL 16 集群（数据目录 `G:\DevCache\Temp\aftercare-pg\data`，DSN `postgresql://aftercare:aftercarelocal@127.0.0.1:55450/aftercare`），本轮的 670 项集成验证跑在它上面；它不属于仓库，用完可 `pg_ctl stop -D` 关闭。5432 是既有的 Windows 服务，本轮未触碰。

本地提交成功后，记录可随该提交进入本仓库的新 worktree；尚未推送时，另一台机器或 GitHub 不能自动取得它。跨机器交接需另行授权推送或明确的提交传递方式，不把本地提交等同远端同步。

## 6. 验证台账

- 2026-09-16 / A3-01 持久路由（本机，已提交并推送）：把失败与预算耗尽从“异常/RUNNING 悬置”改成**持久路由**。新增 `RouteReason` 与 `Checkpoint.route_reason`（不变式：只有 `review`/`retry` 带原因）；harness 的 9 条失败路径都落可续跑检查点，`retry` 带 `available_at`；provider 未报 usage 时只扣一次 `model_calls`，不猜成本；工具意图被拒与执行器拒绝走 `review` 且不重试。Worker 侧 `_RUN_STATE_FOR_STEP` 把 `complete`/`review`/`retry` 映射为 `COMPLETED`/`REVIEW`/`RETRY_AT`，`RETRY_AT` 必须带 `available_at`。**真实 PostgreSQL 16.13 故障测试 3 项**：（1）`model_calls=0` 时 provider **一次都没被调用**、Run 落 `REVIEW`、队列里没有行且 `run_next` 返回 `None`；（2）503 可重试故障落 `RETRY_AT`，Run 与唤醒行带同一个未来时刻，到期前 `claim_next` 拿不到、`time.sleep(1.5)` 后拿到并**真的再次请求 provider**（`len(client.calls) == 2`）后 `COMPLETED`；（3）401 致命故障落 `REVIEW`，经 `REVIEW→READY` 放行后重新入队、跑完 `COMPLETED`、fencing token 递增。本机离线 `549 passed, 126 skipped`（新增 4 项、删除 3 项重复用例），真实 PostgreSQL 16.13 干净重建库后全量 `675 passed` 零跳过零失败；`ruff format --check .`/`ruff check .`/严格 `mypy`（149 文件）通过。**行数预算：新增产品 182 行 / 新增测试 242 行 = 1.33，超出 ≤0.5 的切片预算**——本切片的交付本体就是失败路径的不变量，用户明确要求“需要真实 PG 故障测试”，已先删掉两条被真实 PG 测试覆盖的重复用例后仍超；这是本次记录的偏差，不作为后续切片的先例。**本机已知抖动**：机器同时跑别的重活时，`tests/test_sse_api.py` 的 2 项 tail 用例与 `tests/persistence/test_pool.py::test_an_exhausted_pool_fails_closed_and_keeps_serving` 会间歇失败（0.5 s 借用/轮询预算）；同样的 3 项失败已在未改动的 `bdda584` worktree 上复现，安静机器上再跑是 `675 passed` 零失败，因此判定为既有环境抖动而不是本切片回归。**未做**：真实沙箱后端、连接器答案入 EGM、提额/放行入口、前端与远端 CI 观察。
- 2026-09-16 / A3-01 模型端接力 + 工具执行面（本机，未提交）：把“模型提议 → 校验 → 沙箱租约 → 连接器作答 → 内容寻址产物 → 检查点”整条链跑通并留下实测。新增模块：`artifacts.py`（SHA-256 寻址、tenant/case 分区、原子写、读取复核范围与摘要）、`sandbox/manager.py`（tier 策略 + 两阶段释放 + sweep）、`connectors/`（只读、确定性、来源身份部署给定）、`runtime/sandbox_executor.py`（订单绑定来自 Run scope，不来自工具参数）、`runtime/wiring.py`（环境组装，缺坐标 fail closed）。**真实 provider 实测**：`deepseek-flash` 连续 3 轮返回 `reasoning` + `function_call`，harness 依次执行 `lookup_order` → `lookup_tracking` → `request_material_draft`；三次沙箱分配全部归还（sweep `active=0, destroyed=3`）；两次不同 `call_id` 的 `lookup_order` 得到**同一个 sha256** `720ca7ba7776570f…`，即“重放不产生新事实”被真实复现；跨片恢复也实测通过——第一片在 `max_steps=3` 用尽后停在 `next_step=tool`，第二片用**新建的沙箱容量与新的 provider 客户端**从同一检查点续跑，`completed=True`、`next_step=complete`，模型最终答复引用了 `A-1001`/`buyer-77`/`CNY 957.00`/`pay-5501`/`delivered` 等真实导出事实（证据经文是 `make_render_input` 从 artifact 存储读回的，不是内存传递）。定价用显式合成值（1/2 microUSD per token），只验证接线不冒充供应商价目。本机离线 `547 passed, 123 skipped`（较 509 基线新增 38 项），真实 PostgreSQL 16.13 `670 passed`（零跳过，`AFTERCARE_REQUIRE_DRILLS=1`），`ruff format --check .`/`ruff check .`/严格 `mypy`（149 文件）通过。行数预算：新增产品 982 行 / 新增测试 414 行 = **0.42 ≤ 0.5**（样例导出 `commerce-sample.json` 计数据不计产品代码）。**未做**：真实沙箱后端（仍是 `FakeSandboxProvider`，不是安全边界）、暖池与出网、连接器答案入 EGM、前端与远端 CI、提交与推送。**本轮实测到的新缺口**见第 7 节。

- 2026-09-16 / A3-01 模型端接线（本机，未提交）：把模型这一头接上真实 provider。**起因是实测发现旧适配器会拒绝每一个真实响应**——`normalize_response` 用 7 个字段的顶层白名单，而 `deepseek-flash` 的 Responses 信封有 35 个字段；且它没有 `reasoning` 输出项分支，推理模型的第一项输出必然抛 `unsupported Responses output type`。改动：顶层改为只校验被消费的字段、未知信封字段忽略，未知 **output** 项仍 fail-closed（可能藏着工具调用）；新增 `reasoning` 分支，只记 `ResponseEvent(kind="reasoning", text=None)`，新增 `_auditable()` 在写 `native_json` 前剥掉思维链正文；`ResponsesRequest.tools` 由 `tuple[Identifier, ...]` 改为 `tuple[ToolSpec, ...]` 并携带参数 schema（旧实现发 `{"type":"object","additionalProperties":false}` 无 properties，模型只能回空参数，`lookup_order` 拿不到 `order_id`）；`ModelClient` Protocol 的签名从 `create(request)` 修成真实的 `create(**payload)`；新增 `model_adapters/transport.py`。**真实 provider 证据**：对 `https://api.deepseek.com/responses` 实发请求。最小文本补全返回 `output=[reasoning, message]`、`text="ok"`；带 `lookup_order(order_id)` schema 的请求返回 `output=[reasoning, function_call]`，抽出 `{'order_id': 'A-1001'}`，`usage=(input 300, output 67, total 367)`；`ProviderEndpoint` 的 repr 打印 `api_key='[REDACTED]'`。两次真实响应已固化为 `tests/fixtures/model_wire/`（思维链正文剥除）并由 `tests/test_model_wire_compat.py` 离线回归。本机离线全量 `511 passed, 123 skipped in 27.51s`（上一版 509），`ruff format --check .`/`ruff check .`/严格 `mypy aftercare_agent tests evals`（135 文件）通过。**未运行**：真实 PostgreSQL 全量（本切片未触及持久化路径；5432 是既有 Windows 服务未触碰，55450 临时集群已关）、前端（未改 `web/`）、远端 CI（未推送）、Worker 内 `run_fake_harness` 替换（未做，仍写死）。**安全**：本轮用户以明文提供了 provider key，已要求吊销重发；仓库内没有任何位置写入该 key。

- 2026-09-15 / 流程：测试与文档预算。改动：`AGENTS.md` 新增“测试与文档预算”一节；`docs/development.md` §3 增加预算入口与“收尾跑一次全量、不在调试循环里反复跑”的说明；新增 `tests/conftest.py`，并从 21 个测试文件删除 21 份重复 fixture。验证（本机 Windows，提交见下一行）：`ruff check .` 通过；`ruff format --check .` 133 文件已格式化；`mypy` 128 源文件无问题；离线 `pytest -q` 509 passed / 123 skipped（用例总数 632 不变）；真实 PostgreSQL 16.13（端口 55450，`AFTERCARE_REQUIRE_DRILLS=1`）`632 passed in 132.33s`，证明 fixture 合并未改变任何集成用例的行为（含此前不预先迁移 schema 的 `test_api_integration.py`）。未做：没有新增守卫测试来防止比率回退（靠 AGENTS.md 规则约束，不靠用例）；没有删除任何已有用例；`docs/architecture.md` 等手册的体量问题本轮不动，留待按版本批量对齐。
- 2026-09-15 / D-02 第三刀 CI 闸门（提交 `e8b8b9b`）：GitHub Actions run [34976339343](https://github.com/yushui2022/Aftercare-Agent/actions/runs/34976339343) 的 `Static checks` 与 `Test with PostgreSQL` 全部通过，`postgres:17` 上 `632 passed, 98 warnings in 19.95s`、**零跳过**；该提交起两个演练模块在 `AFTERCARE_REQUIRE_DRILLS=1` 下把跳过变成失败，本机验证：不设 `DATABASE_URL` 时 `tests/persistence/test_backup_restore.py` 由 9 skipped 变成 9 errors，理由是 `AFTERCARE_REQUIRE_DRILLS=1 but this drill cannot run: DATABASE_URL is not configured`。唯一标注仍是 GitHub 对 actions Node.js 20 的弃用提示。
- 2026-09-15 / D-02 第三刀 CI 修复（提交 `130352f`）：GitHub Actions run [34975613601](https://github.com/yushui2022/Aftercare-Agent/actions/runs/34975613601) 全部通过，`postgres:17` 上 **`632 passed, 98 warnings in 20.10s`**、**零跳过**——3 项 WAL 归档集成用例在 CI 上真跑了（它们自己 `initdb` 一个 `archive_mode = on` 的临时集群）。安装步骤现在用 `/usr/lib/postgresql/17/bin/{pg_dump,initdb} --version` 自证，日志里可见 `pg_dump (PostgreSQL) 17.11`。
- 2026-09-15 / D-02 第三刀 首次推送（提交 `30d3c27`）：GitHub Actions run [34975104046](https://github.com/yushui2022/Aftercare-Agent/actions/runs/34975104046) 结论是 `success`，但这是**假绿**：`Test with PostgreSQL` 报 `629 passed, 3 skipped`，三项 WAL 归档用例因为临时集群起不来而跳过（`pg_ctl: could not start server`），而工具安装步骤本身在 `initdb --version` 上以 127 失败却被 `continue-on-error` 吃掉。两点原因都查明并修在 `130352f`：`$GITHUB_PATH` 只对后续步骤生效，自检因此命中 runner 自带客户端且根本没有 `initdb`；Debian/Ubuntu 把默认 Unix socket 编译成 `/var/run/postgresql`，属于发行版自己的 `postgres` 用户，别人起的集群会在监听前退出。
- 2026-09-15 / D-02 第三刀 本机端到端实测（原文写进 ADR-0010）：用项目 venv 的 `aftercare-backup` 对真实 `archive_mode = on` 的临时集群跑三幕——正常时 `wal --dsn ... --directory ... --archive-lag-seconds 600` 退出 **0**（`segments 1 over 1 timeline(s)`、`gaps none`、`demo-1 covered (needs 000000000000000000000001, ...)`）；把 `archive_command` 改成 `exit 1` 后目录检查**仍 `ok`**，只有“归档器是否在推进”退出 **1**（`PostgreSQL is retrying the same segment, so the archive is not advancing`）；删掉中间段且**不问服务器**时退出 **1**（`gaps 1`，并说明 `everything after a hole is unusable for recovery -- pass --segment-size to count what is missing`）。三段输出是当前代码的真实输出。
- 2026-09-15 / D-02 第三刀 本机验证（提交前）：真实 PostgreSQL 16.13 全量 **`632 passed, 108 warnings in 77.25s`**（上一版 599，本轮新增 33 = 离线 30 + 集成 3）；离线全量 **`509 passed, 123 skipped`**（上一版 476）；`ruff format --check .`、`ruff check .`、严格 `mypy aftercare_agent tests evals`（132 文件）与 `docker compose config --quiet`（rc=0）全部通过。另跑了一次镜像 CI 条件的全量（`DATABASE_URL` + `AFTERCARE_REQUIRE_DRILLS=1`）：`1 failed, 631 passed`，唯一失败是**已知抖动**用例 `tests/persistence/test_pool_capacity.py::test_a_pool_narrower_than_its_workload_queues`（整机长跑负载下偶发，台账 D-03 行已记同源风险），单独复跑 `4 passed in 2.13s`。**未运行**：容器内的备份/演练、对象存储或异地副本、备份加密、按时间点的 PITR 恢复演练、调度接线。前端本轮未改也未运行。
- 2026-09-15 / D-02 第二刀 台账提交 CI（提交 `080d1c0`）：GitHub Actions run [34970541701](https://github.com/yushui2022/Aftercare-Agent/actions/runs/34970541701) 的 `Static checks` 与 `Test with PostgreSQL` 全部通过，`postgres:17` 上 `599 passed, 98 warnings in 18.52s`、零跳过——这次提交只改 `docs/project-status.md`，测试数与前一条 run 一致，说明台账改动没有带动任何代码行为；唯一标注仍是 GitHub 对 actions Node.js 20 的弃用提示。
- 2026-09-15 / D-02 第二刀 远端 CI 验收（提交 `59e5945`）：GitHub Actions run [34970075358](https://github.com/yushui2022/Aftercare-Agent/actions/runs/34970075358) 的 `Static checks` 与 `Test with PostgreSQL` 全部通过，`postgres:17` 上 **`599 passed, 98 warnings in 21.12s`**、**零跳过**——本切片新增的 17 项离线用例与 2 项恢复演练用例在 CI 里真跑了（上一版 `580 passed`）。唯一标注仍是 GitHub 对 actions Node.js 20 的弃用提示，与本切片无关。
- 2026-09-15 / D-02 第二刀 本机验证（提交前）：真实 PostgreSQL 16.13（临时集群，端口 55450）重建库后全量 **`599 passed, 113 warnings in 61.23s`**（上一版 580 项，本切片新增 19 项 = 离线 17 + 集成 2）；无 `DATABASE_URL` 的离线全量 **`476 passed, 123 skipped`**（上一版 459 项，跳过的全部是需要显式 PostgreSQL 的集成用例）；`ruff format --check .`（129 文件 already formatted）、`ruff check .`（All checks passed）、严格 `mypy aftercare_agent tests evals`（129 文件，无问题）与 `docker compose -f deploy/compose/docker-compose.yml config --quiet`（rc=0）全部通过。**一次真实失败被记录而不是被掩盖**：第一次跑全量时 `tests/persistence/test_pool_capacity.py::test_a_pool_narrower_than_its_workload_queues`（D-03 的时序敏感容量探针，断言 `narrow.p95_ms > sized.p95_ms`）失败，该文件本切片未改；单独跑其中 4 项全过，重建库后重跑全量 `599 passed`，判断为同源时序抖动而非本切片回归，D-03 行已有同源风险记载。前端本轮未改也未运行。**未运行**：容器内的备份/演练与 `status`、对象存储或异地副本、备份加密、WAL 归档与 PITR、任何 cron/systemd/K8s 调度接线、生产环境的 RPO/RTO 判定。
- 2026-09-15 / D-02 第二刀 本机端到端实测（原文写进 ADR-0009）：用项目 venv 的 `aftercare-backup` 在临时目录跑完整链路——`create` 后 `status --drill-interval-seconds 86400` 退出 **1**，理由是 “never recorded in this directory” 与 “has never been restored”（区分“演练过但失败”与“从没试过”正是这次改动的目的）；`drill --upgrade` 退出 **0**（restore 0.609 s / verify 0.228 s / upgrade 0.310 s，**RTO 1.147 s**，31 张表行数一致）并写出 `<name>.drill.json`；随后 `status --rpo-seconds 86400 --drill-interval-seconds 604800` 退出 **0**；不给预算时输出 `budgets none stated` 且不作判定（**未配置不等于已满足**）。
- 2026-09-15 / D-02 台账提交 CI（提交 `0947b7c`）：GitHub Actions run [34968364743](https://github.com/yushui2022/Aftercare-Agent/actions/runs/34968364743) 的 `Static checks` 与 `Test with PostgreSQL` 全部通过，`postgres:17` 上 `580 passed, 98 warnings in 20.27s`、零跳过——这次提交只改 `docs/project-status.md`，测试数与前一条 run 一致，说明台账改动没有带动任何代码行为；唯一标注仍是 GitHub 对 actions Node.js 20 的弃用提示。
- 2026-09-15 / D-02 远端 CI 验收（提交 `1e9d9ef`）：GitHub Actions run [34967559546](https://github.com/yushui2022/Aftercare-Agent/actions/runs/34967559546) 的 `Static checks` 与 `Test with PostgreSQL` 全部通过，`postgres:17` 上 **`580 passed, 98 warnings in 13.15s`**、**零跳过**——7 项恢复演练用例在 CI 里真跑了。第一次推送的 run [34967406421](https://github.com/yushui2022/Aftercare-Agent/actions/runs/34967406421)（提交 `1afe587`）同样是 `success`，但报告 `573 passed, 7 skipped`：7 项跳过全部是 `tests/persistence/test_backup_restore.py` 的演练用例，原因是 PATH 里第一个 `pg_dump` 是 ubuntu 自带的客户端 16，而服务端是 `postgres:17`，用例按设计带原因（`pg_dump 16 cannot read PostgreSQL 17; install a matching client`）跳过而不是假通过。只装 `postgresql-client-17` 不够——必须把 `/usr/lib/postgresql/17/bin` 放到 `GITHUB_PATH` 前面，这正是 `1e9d9ef` 修的那一行。唯一标注仍是 GitHub 对 actions Node.js 20 的弃用提示，与本切片无关。
- 2026-09-15 / D-02 本机验证（提交前）：真实 PostgreSQL 16.13（临时集群 `G:\DevCache\Temp\aftercare-pg`，端口 55450）重建库后全量 **`580 passed, 119 warnings in 55.88s`**（上一版 541 项，本轮新增 39 项 = 离线 32 + 集成 7）；无 `DATABASE_URL` 的离线全量 **`459 passed, 121 skipped`**（上一版 427 项，跳过的全部是需要显式 PostgreSQL 的集成用例）；`ruff format --check .`、`ruff check .`、严格 `mypy aftercare_agent tests evals`（126 文件）与 `docker compose -f deploy/compose/docker-compose.yml config --quiet`（rc=0）全部通过。前端本轮未改也未运行。**未运行**：容器内的备份/演练、对象存储或异地副本、备份加密、WAL 归档与 PITR、定期演练自动化。
- 2026-09-15 / D-02 恢复演练实测（本机、临时 PostgreSQL 16.13、客户端 16.13，库含整套回归的合成数据）：报告原文见 [docs/operations/2026-09-15-restore-drill.json](operations/2026-09-15-restore-drill.json)。dump 111,950 字节（字节数与 SHA-256 记进清单）；31 张 `aftercare_` 表；schema 迁移 16；`restore 0.703 s` / `verify 0.158 s` / `upgrade 0.151 s`，**RTO 1.012 s**，执行时恢复点年龄 5.8 s；6 项检查全部 `ok`（迁移摘要一致、31 张表全还原、行数完全一致、升级到迁移 16、升级后行数不变、临时副本已删）。同一库上 `reconcile` 按“恢复点 − 60 s”列出无法描述的 Action：演练现场那次是 7 条（2 条 `UNKNOWN`、5 条在上次恢复点之后被更新，含安全边界把 `confirmed-inside` 一并纳入），但那次现场输出没有归档；本轮随后又用归档的清单复跑同一条命令（同一个临时库，但库已被之后的全量回归重写过）得到 19 条——2 条 `unknown_outcome` + 17 条 `changed_since_cut`（`queue-*`、`operator-approval-*`、`vertical-slice`、`ops-backup` 等），因为 cut 固定在那个恢复点上，之后被回归写过的每个 Action 都会进列表。两个数字都对，只是描述不同的库状态：**恢复后核对要按被恢复的那个恢复点取值，不能事后拿“现在”当基准**。**这只证明该恢复点与这台机器的恢复能力，不是 RPO/RTO 承诺**——没有 WAL 归档，两次 dump 之间到底丢多少数据仍然未决，要按部署目标单独决定。
- 2026-09-15 / D-02 一致性读验证：确认 `pg_export_snapshot()` 导出的快照可以同时喂给同一事务里的普通查询和 `pg_dump --snapshot`，只要 dump 结束前不 COMMIT，清单读到的行数就与 dump 内容同源——这是“清单描述的就是这份 dump”的前提，已由演练路径与 `tests/persistence/test_backup_restore.py` 覆盖，不是假设。
- 2026-09-15 / D-03 远端 CI 验收（提交 `e3bedbc`）：GitHub Actions run [34965278041](https://github.com/yushui2022/Aftercare-Agent/actions/runs/34965278041) 的 `Static checks` 与 `Test with PostgreSQL` 全部通过，`postgres:17` 上 **`541 passed, 98 warnings in 16.50s`**，与本机重建后的临时 PostgreSQL 16.13 上 `541 passed`（34.06 s）逐项一致。警告从上一版 CI 的 92 条升到 98 条、本机 118 条，增量全部来自既有的 FastAPI `on_event` 弃用提示——每个测试 app 现在多注册一个同类 shutdown 钩子，没有新类别。唯一标注仍是 GitHub 对 actions Node.js 20 的弃用提示，与本切片无关。
- 2026-09-15 / D-03 容量 sweep（本机实测，临时 PostgreSQL 16.13 / CPython 3.13.15，报告见 [docs/capacity](capacity/README.md)）：16 并发、每个工作单元一次 10 ms 短事务、p95 预算 50 ms。(1) 懒增长（`min_size=1`，即当前进程行为）扫 1/2/4/8/16/32：**没有尺寸达标**，steady 的 p95 分别是 262.56 / 129.90 / 124.92 / 168.09 / 128.02 / 123.83 ms，每轮池累计排队 9.8–53.9 s；`max_size` 从 8 提到 32 时 in_use 峰值只从 8 升到 10、该轮新开连接 7→8（本机建连 p50 约 115 ms，池一次只长一条）。(2) 预热到 16（`min_size=16`）扫 16/32：`max_size=16` 达标（steady/burst/wake 的 p95 32.89 / 31.31 / 39.05 ms，池排队 **0 ms**，in_use 峰值 16），`max_size=32` 与 16 无差别（p95 38.56 / 31.60 / 36.24 ms，in_use 峰值仍是 16）。(3) 纯开销对照（`--service-time-ms 0`，单元 = `SELECT 1`）扫 1/8/16、预算 25 ms：三个尺寸都达标，p50 2.69–8.57 ms。结论：**突发并发先撞的是握手与懒增长，不是上限**，因此默认值不变——API 1/8、Worker 1/4 仍然是**有界默认值**，不是容量结论。
- 2026-09-15 / D-03 探针口径修正（本轮自测发现的工具缺陷）：第一次 sweep 用裸 `SELECT 1` 当工作单元，结果 1/8/16 全都达标（p50 2.7–8.6 ms），因为单元比一次借用开销还短，`max_size` 根本没有机会成为瓶颈。这不是池的结论，是工作负载的问题：因此给探针加了 `--service-time-ms`（一个单元占住连接多久，用 `SELECT pg_sleep(%s)` 实现）与 `--min-size`（每个被扫的池预热多少条），并把 `in_use`/`wait` 的峰值采样次数（`watcher_samples`）一并写进报告，避免把采样值当确定值读。
- 2026-09-15 / D-03 本机验证：真实 PostgreSQL 16.13 重建库后全量 **`541 passed`（34.06 s）**（上一版 517 项，本轮新增 24 项）；离线全量 **`427 passed, 114 skipped`**；`ruff format --check .`（118 文件）、`ruff check .`、严格 `mypy aftercare_agent tests evals`（118 文件）与 `docker compose -f deploy/compose/docker-compose.yml config --quiet`（rc=0）全部通过。前端本轮未改也未运行。**未运行**：容器内的容量 sweep、生产或 CI 环境的容量结论、OTel 导出器配置。
- 2026-09-15 / D-04 远端 CI 验收（修复提交 `116f904`）：GitHub Actions run [34962581221](https://github.com/yushui2022/Aftercare-Agent/actions/runs/34962581221) 的 `Static checks` 与 `Test with PostgreSQL` 全部通过，`postgres:17` 上 **`517 passed, 92 warnings in 12.96s`**（本机同批 32.7–34.1 s，CI Linux 更快）。首个 run [34962296661](https://github.com/yushui2022/Aftercare-Agent/actions/runs/34962296661)（提交 `ea8280a`）在 `tests/persistence/test_pool.py::test_a_released_connection_goes_back_to_the_pool` 上失败（`assert 181 == 182`，`1 failed, 516 passed, 92 warnings in 14.94s`）：根因是本轮自己写的断言超出了池能承诺的范围——归还走池的后台维护任务，Linux 上借用者抢先一步，池就会按设计多开一条连接。修复把两条复用回归都锁成“池上限 1”（此时池无法长大，借用必然落回归还回来的那条连接），并把这个机制写进 ADR-0006 的代价与边界。

- 2026-09-15 / D-04 本地验证（提交前）：`ruff format --check .`（113 文件已格式化）、`ruff check .`、`mypy aftercare_agent tests evals`（113 源文件）全通过；无 `DATABASE_URL` 的离线全量 `407 passed, 110 skipped`；重建临时 PostgreSQL 16.13 集群（`G:\DevCache\Temp\aftercare-pg`，端口 55450）后真实 PostgreSQL 全量 **`517 passed`**（本轮三次重跑 33.20 s / 34.10 s / 33 s 级；warnings 109–110，全部是 FastAPI `on_event` 与 Starlette/httpx 的既有弃用提示），其中新增 8 项（`tests/persistence/test_pool.py` 7 项与 SSE 轮询复用 1 项）；`tests/persistence/test_pool.py` 单跑 `7 passed in 3.89s`、`tests/test_sse_api.py` 单跑 `5 passed`；`-W error::DeprecationWarning` 下池测试仍 `7 passed`（没有隐式 open 的弃用警告）。未运行：远端 CI（见后条）、前端门禁（本轮未改 `web/`）。

- 2026-09-15 / D-04 连接池效果测量（本机、同一临时 PostgreSQL；探针脚本放在 `G:\DevCache\Temp\aftercare-agent`，未提交）：(1) 25 个连续 `Database.transaction()`（每次 `SELECT pg_backend_pid()`）：`direct()` 落到 25 条不同后端、2.87 s（114.8 ms/事务），池化只落到 **1 条后端**（本机；上限设 1 时任意平台都必然如此，见回归用例）、0.14 s（含首个事务的一次建连；稳态 `SELECT 1` 对照为 0.47 ms/事务）。(2) 借用前健康检查的成本：裸池 0.36 ms/次借用，加 `check_connection` 后 0.45 ms/次。(3) 同一批 509 个既有用例，各自在重建空库后跑一次：基线（`git stash` 掉本轮改动）**78.98 s**，池化 **30.35 s**。

- 2026-09-15 / D-01-07 远端 CI 验收（修复提交 `95384c3`）：GitHub Actions run [34959917168](https://github.com/yushui2022/Aftercare-Agent/actions/runs/34959917168) 的 `Static checks` 与 `Test with PostgreSQL` 全部通过，`postgres:17` 上报告 **`509 passed, 84 warnings in 12.22s`**，与本机重建后的干净临时 PostgreSQL 16.13 上的 `509 passed`（76.93 s）逐项一致。

- 2026-09-15 / D-01-07 远端 CI 验收（交付提交 `f04cc09`）：GitHub Actions run [34958850865](https://github.com/yushui2022/Aftercare-Agent/actions/runs/34958850865) 的 `Static checks` 与 `Test with PostgreSQL` 全部通过，`postgres:17` 上报告 **`509 passed, 84 warnings in 14.79s`**。该提交是本切片第一次推送，进入本轮时 `main` 是绿色的（上一轮 run [34957312393](https://github.com/yushui2022/Aftercare-Agent/actions/runs/34957312393) 为 `success`），本轮不需要先修回归。

- 2026-09-15 / D-01-07 本地验证（提交前）：`ruff format --check .`（112 文件已格式化）、`ruff check .`、`mypy aftercare_agent tests evals`（112 源文件）全通过；无 `DATABASE_URL` 的离线全量 `407 passed, 102 skipped`；重建临时 PostgreSQL 16.13 集群（`G:\DevCache\Temp\aftercare-pg`，端口 55450，不碰既有 5432）后真实 PostgreSQL 全量 **`509 passed, 84 warnings in 76.93s`**；`tests/test_operator_grants_integration.py` 15 项通过（新增 5 项）。前端：`pnpm test` **43 passed**（新增 `cases.test.ts` 4 项与 `api.test.ts` 1 项）、`pnpm typecheck`、`pnpm build` 通过。未运行：真实 IdP、远端 CI（本条目在推送前写入，CI 结果见下条）。

- 2026-09-15 / D-01-07 真实浏览器端到端（真实 PostgreSQL + 真实 HTTP）：两轮 Playwright 冒烟，脚本保存在 `G:\DevCache\Temp\aftercare-agent`（`serve_admin_smoke.py` 起服务、`ui_smoke_admin.py` 与 `ui_smoke_d0107.py` 断言，未提交进仓库）。(1) **真实 Bearer 管理员**（进程内构造 app，verifier 用 seam double，合成身份关闭；Playwright 用 `page.route` 注入 `Authorization`，因为工作台只认合成身份输入框）：10 项全过——持 `grant:read`/`grant:admin` 但没有该工单 grant 的身份队列为空、控制面区块列出工单并标注“仅可管理访问”、选中后只渲染说明 + 授权面板、手动移交第三方成功（断言新行 `r1 · 由 access-admin`）、失败请求只有两条预期的 `403`（该身份的 `administration/cases` 与 `grants`），**没有任何内容路由被请求**；切到该工单的实际持有者后内容面板与实时时间线正常渲染、访问面板按 D-01-06 规则静默隐藏。(2) **合成身份回归**：9 项全过（队列、详情、授权面板、授予→撤销→替换、SSE 实时事件）。这一轮浏览器验证抓出并修掉了两个缺陷，见提交 `95384c3`。

- 2026-09-15 / A2-02 远端 CI 验收（提交 `230e518`）：推送后 GitHub Actions run [34957131285](https://github.com/yushui2022/Aftercare-Agent/actions/runs/34957131285) 的 `Static checks` 与 `Test with PostgreSQL` 全部通过，`postgres:17` 上报告 **`504 passed, 74 warnings in 14.72s`**，与本机重建后的干净临时 PostgreSQL 16.13 上的 `504 passed`（71.69 s）逐项一致。上一次变红的 run [34955596978](https://github.com/yushui2022/Aftercare-Agent/actions/runs/34955596978) 失败在同一用例，本次修复同时覆盖它，`main` 已恢复绿色。唯一标注仍是 GitHub 对 actions Node.js 20 的弃用提示，与本切片无关。

- 2026-09-15 / A2-02 心跳续期延迟预算（根因测量）：起点是 CI run 34955596978 失败的心跳用例。本机在独立临时 PostgreSQL 16.13 上采样 25 次：`psycopg.connect` p50 **115.2 ms**（min 86.5 ms、max 215.3 ms），而同一次 tick 里真正的续期事务（`RunRepository.renew` 的 UPDATE + 提交）p50 **0.8 ms**（max 1.4 ms）。原实现每次 tick 都走 `Database.transaction()`，即每次续期都先付一次建连；在 200 ms 租约 + 30 ms 间隔下，单次 tick 只剩约 133 ms 预算，因此本机连续单跑 20 次失败 **11 次**、CI 上失败一次。结论：这不是负载抖动，而是把建连开销放进了租约的截止预算。

- 2026-09-15 / A2-02 修复与回归：`LeaseHeartbeat` 改为心跳线程独占一条连接（由新接缝 `Database.open()` 创建、跨 tick 复用、在 `_run` 的 finally 里关闭，线程外不共享）、首个续期在进入循环时立即执行、按绝对截止时间（`monotonic` 累加间隔）排程而非“睡满间隔再计”，异常路径仍立即 stop 并 fail-closed（不做重连，连接断开与租约被抢占同样处理）。`tests/test_worker_contract.py` 的假 Database 只保留 `open()`，使“每 tick 借一条连接”的回归直接失败；新增 2 项回归（首个间隔未到就续期、多次续期只开一条连接且退出时关闭）。`tests/persistence/test_worker.py` 的长切片用例改为 1 s 租约 / 100 ms 间隔 / 1.5 s 切片，并把建连测量写进 docstring 说明为何不能更紧。本机验证：修复前该用例单跑 20 次失败 11 次，修复后同样 20 次 **0 失败**；离线全量新增 2 项后 `407 passed, 97 skipped`；重建后的干净临时 PostgreSQL 16.13 上全量 **`504 passed`（71.69 s）**；`ruff format --check .`（112 文件）、`ruff check .` 与严格 `mypy aftercare_agent tests evals`（112 文件）通过。远端 CI 见本节首条（`success`，`504 passed`）；前端门禁本轮未运行，因为未改 `web/`。

- 2026-09-15 / 同源但本轮未修的观察（SSE tail 建连预算）：全量真实 PostgreSQL 的一轮运行中出现过 1 次 `tests/test_sse_api.py::test_postgres_tail_observes_an_event_committed_after_it_starts` 失败（`assert [] == [2]`，当轮整套 99.74 s，明显受机器负载影响）。对照实验：stash 掉本轮改动后在干净基线连跑 8 次 8 过，恢复改动后再连跑 8 次同样 8 过，因此不是本轮回归。机制与心跳同源——`PostgresEventTail.stream()` 每轮轮询调用一次 `database.transaction()`，即每轮付一次建连（本机 p50 115 ms），用例的 300 ms 尾部窗口在慢建连主机上可能只来得及轮询一次。**本轮不改该用例也不改 tail 代码**：没有可稳定复现的失败，放宽阈值属于未经验证的改动；正确修法是 D-04 的连接池，已连同“本机全量 71.69 s / CI Linux 12.4 s”的差异写入计划 D-04 行。


- 2026-09-15 / D-01-06 工作台访问管理（前端）：`pnpm test` 38 项通过（新增 `web/src/grants.test.ts`、`api.test.ts` 与 `sse.test.ts` 的对应回归）、`tsc --noEmit` 与 `vite build`（243 KB JS / 6.97 KB CSS）通过。真实浏览器端到端：Playwright + Chromium 驱动 Vite 开发服务器，对真实 FastAPI 与独立临时 PostgreSQL 16.13 完成 8 项断言——工单可发现、访问管理面板渲染、创建者授权显示为生效中、表单授予 `review:read`+`approval:read`、SSE 时间线收到 `operator-9:granted:1`、撤销后徽标变为已撤销、时间线收到 `operator-9:revoked:2`、"替换 / 重新授权"用预填 revision 把已撤销行恢复为生效中（截图 `G:\DevCache\Temp\aftercare-agent\grants-ui.png`，未提交）。
- 2026-09-15 / 工作台实时时间线缺陷（本轮发现并修复）：时间线用 `incoming.reduce(appendEvent, current)` 折叠事件批次，而 `Array.prototype.reduce` 会把元素下标作为第三个实参传入，正好落进 `appendEvent(..., cap)`，单事件批次的结果因此是 `[]`。真实浏览器诊断记录：授予授权后 `.timeline` 条目从 2 变 0、流状态仍显示"实时"，即新事件到达反而清空时间线；最小复现 `[e1,e2].reduce(appendEvent, [])` 不返回原批次。修复为 `appendEvents()` 显式折叠并补 4 项回归，修复后同一浏览器流程通过。附带消除每次页面加载的 `/favicon.ico` 404（新增 `web/public/favicon.svg` 并在 `index.html` 声明）。
- 2026-09-15 / D-01-06 Python 侧复跑：本轮未改动 Python 代码，仍复跑门禁以确认没有连带影响——`ruff format --check`（112 文件）、`ruff check`、严格 `mypy aftercare_agent tests evals`（112 文件）与离线全量 `405 passed, 97 skipped` 通过；重建后的干净临时 PostgreSQL 16.13 上全量 `502 passed`。未运行：真实 IdP、RLS、跨工单发现（尚未实现）与远端 CI（推送后补记）；前端门禁不在 CI 覆盖范围内，本轮前端结论只来自本机。
- 2026-09-15 / D-01-05 远端 CI 验收（提交 `f7818d5`）：推送后 GitHub Actions run [34953807687](https://github.com/yushui2022/Aftercare-Agent/actions/runs/34953807687) 的 `Static checks` 与 `Test with PostgreSQL` 全部通过，`postgres:17` 上报告 `502 passed, 74 warnings in 13.73s`，与本机 PostgreSQL 16.13 上的 `502 passed` 一致，D-01-05 的数据库侧结论因此不再只依赖本机临时集群。整个 job 48 秒完成；唯一标注是 GitHub 对 actions 的 Node.js 20 弃用提示，与本切片无关。
- 2026-09-15 / D-01-05 授权管理面（本机真实 PostgreSQL）：离线全量 `405 passed, 97 skipped`；在独立临时 PostgreSQL 16.13 集群（`G:\DevCache\Temp\aftercare-pg\data`，端口 55450，与既有 5432 服务实例完全隔离，用 `D:\postgresql\16\bin` 的 `initdb`/`pg_ctl` 新建）上全量 `501 passed, 1 failed`，其中新增的 `tests/test_operator_grants_integration.py` 10 项全部通过。唯一失败是 `tests/persistence/test_worker.py::test_worker_heartbeat_keeps_long_slice_lease_alive`：该用例给 200 ms 租约、30 ms 心跳间隔并让 harness 睡 500 ms，整机满载时心跳线程被调度延迟就报 `lease heartbeat failed`；单独运行两次均通过，把本切片全部改动 stash 后在干净基线上复现同一失败（`1 failed, 466 passed`，与 CI 的 467 项一致），因此判定为既有的负载相关抖动，不是本切片回归。本轮还修掉两处只有真实 PostgreSQL 才能暴露的问题：`CaseGrantInput` 的模型级 `strict=True` 会拒绝 RFC 3339 字符串形式的 `expires_at`（返回 422 而不是设计中的 `400`），改为该字段 `strict=False`、时区规则仍由处理器按数据库 `clock_timestamp()` 判定；`test_access_changes_appear_in_the_case_stream` 原本假设事件流从授权事件开始，实际事件流携带该 Case 既有历史，改为断言历史前缀不变、其后恰好追加两个授权事件且 `case_seq` 连续。`ruff format --check .`、`ruff check .` 与严格 `mypy aftercare_agent tests evals`（112 文件）通过。未运行：真实 IdP、RLS、浏览器端与远端 CI（推送后补记）。
- 2026-09-15 / D-01-04 远端 CI 回归与修复（提交 `cd6ba9b`）：推送后 GitHub Actions run [34950559478](https://github.com/yushui2022/Aftercare-Agent/actions/runs/34950559478) 的 `Static checks` 通过，但 `Test with PostgreSQL` 失败（`2 failed, 463 passed`），两项都是 `tests/test_api_integration.py` 的 `_StubBearerVerifier.verify() got an unexpected keyword argument 'now'`。本机 87 项集成测试因 Docker 守护进程未就绪全部跳过，只有 CI 能暴露该问题。根因不是测试写法，而是接缝缺少类型：`create_app(oidc_verifier=...)` 接受具体类 `JwtJwksVerifier`，测试替身只能靠 `cast` 绕过类型检查，签名漂移在编译期不可见。修复：新增 `auth.guard.TokenVerifier` Protocol（`verify(authorization, *, now=None) -> AuthContext`），`TokenAccessGuard` 与 `create_app` 改为面向该 Protocol，`_StubBearerVerifier` 去掉 `cast` 并按真实签名接收 `now`。实证该修复有效：把替身签名改回缺少 `now` 后 mypy 报 `Following member(s) of "_StubBearerVerifier" have conflicts`，即同类缺陷现在会在 `Static checks` 阶段被拦住。同时把 guard 的验签与撤销判定收敛到同一个时钟读数，并新增 2 项回归锁定（显式 `now` 与自动时钟两种情况都要求两步看到同一时刻）。修复后本机离线全量 `380 passed, 87 skipped`，`ruff format --check`（109 文件）、`ruff check` 与严格 mypy（109 文件）通过；修复提交 `e3a7586` 的远端结果见下一条。


- 2026-09-15 / D-01-04 远端 CI 验收（提交 `e3a7586`）：GitHub Actions run [34951354092](https://github.com/yushui2022/Aftercare-Agent/actions/runs/34951354092) 的 `Static checks` 与 `Test with PostgreSQL` 全部通过，真实 PostgreSQL 17 上报告 `467 passed, 52 warnings`，正好等于本机 380 项离线测试加上本机因 Docker 未就绪而跳过的 87 项集成测试，因此撤销切片修复后的全量范围已有远端证据。该结果只对应提交 `e3a7586`，不代表后续改动；唯一注解仍是 actions/checkout 与 setup-uv 的 Node 20 弃用提示，不影响结果。


- 2026-09-15 / D-01-04 撤销与 introspection 边界：新增 `auth/introspection.py`（`IntrospectionConfig` 静态策略、`IntrospectionVerdict`、`token_fingerprint`、`parse_verdict`、`HttpTokenIntrospector`、`CachedIntrospector`）和 `auth/guard.py`（`TokenAccessGuard`），并在 `create_app(..., introspector=...)` 与 `create_default_app()` 的 `AFTERCARE_OIDC_INTROSPECTION_URL`/`_CLIENT_ID`/`_CLIENT_SECRET` 上接线。关键行为：判定与凭据分离（凭据只存在于注入的 `httpx.Client`，不进契约模型或 `repr`，Token 不写日志）；`active` 缺失或非布尔值即拒绝，`sub`/`tenant_id`/`exp` 若存在必须类型正确且与已验证 Token 一致；缓存以 SHA-256 指纹为键、有界、只缓存 TTL 内的判定、已过期判定不缓存，失败不缓存；验签之后的任何失败（`active=false`、sub/tenant 不一致、判定过期、传输/解析/超大响应错误、未预期异常）统一 fail-closed 为 `401 unauthenticated`，不回退合成身份。新增 `tests/test_token_revocation.py`（含 API 层撤销拒绝与"不降级为合成身份"）。本机离线全量 `380 passed, 87 skipped`（含首轮 70 项与后续 2 项时钟一致性回归）；`ruff format --check`（109 文件）、`ruff check` 与严格 mypy（`aftercare_agent tests evals`，109 文件）通过。**本轮没有调用任何真实 IdP，也没有运行需要 PostgreSQL 的集成测试**：本机 Docker 守护进程仍未就绪，87 项集成测试跳过；该切片在认证边界内结束，不触达数据库。真实 IdP introspection 端点、凭据轮换、IdP 限流/超时行为和 RLS 仍未验证。


- 2026-09-15 / A3-04 远端 CI 验收（提交 `044824b`）：GitHub Actions run [34948559550](https://github.com/yushui2022/Aftercare-Agent/actions/runs/34948559550) 全部步骤通过，`Test with PostgreSQL` 报告 `395 passed, 38 warnings`。本机因 Docker 引擎始终未就绪而无法运行的 87 项集成测试在真实 PostgreSQL 17 上通过，新增的 16 项闭环评测与模型边界测试在本地与 CI 结果一致。该数字只对应提交 `044824b`，不代表后续改动。

- 2026-09-15 / A3-04 离线闭环评测：新增 `evals/loop.py`，用脚本化 Responses client 驱动真实 `ResponsesAdapter`（`store=false`、工具白名单、整数 token/成本预算），再经 `parse_investigation_proposal()` 解析模型 JSON，最后交给既有的确定性评估；逐案报告 disposition、引用来源、未被引用的接受项、未知引用、错误码与 token/合成成本，并输出稳定 digest 与可从检查点恢复的 Harness 步进。同时补上域层缺口 `parse_investigation_proposal()`——此前没有任何入口能把模型 JSON 变成 `InvestigationProposal`：未知字段、scope/prose 字段、未知 claim、空 claims、空 evidence_refs 和重复键 JSON 一律 `INVALID_INPUT`。为复用期望匹配，`evals/runner.py` 抽出 `expected_case()`/`assess_proposal()`，重构前后 `python -m evals.runner` 的 digest 完全一致（`80ddf7f0…`），证明行为未变。12 个案件的结果：1 个 `recommendation_ready`（引用 `carrier`）、6 个转人工复核或补材料、3 个结构化错误（跨订单、跨租户、重复证据）、1 个模型边界拒绝、1 个冲突；闭环步进 5、工具调用 3、可恢复。本机离线全量 `308 passed, 87 skipped`；`ruff format --check`、`ruff check` 与严格 mypy（106 文件）通过。**本轮没有调用任何真实 provider，也没有运行需要 PostgreSQL 的集成测试**：本机 Docker 守护进程以 `-WindowStyle Hidden` 启动后进程立即退出、引擎始终未就绪，因此数据库侧验证仍以 CI 为准。合成单价 3/15 micro-USD per token 只证明计费路径与回归稳定性，不是 provider 账单、模型效果或 SLA。

- 2026-09-15 / 远端 CI 验收（提交 `e1a210a`）：推送后 GitHub Actions run [34947008994](https://github.com/yushui2022/Aftercare-Agent/actions/runs/34947008994) 完成，`Set up Python`、`Install locked dependencies`、`Static checks` 与 `Test with PostgreSQL` 全部通过；`Test with PostgreSQL` 报告 `379 passed, 38 warnings`，即本机因 Docker 守护进程未运行而跳过的 87 项集成测试在真实 PostgreSQL 17 上独立通过，补齐了 2026-09-15 离线条目的未覆盖范围。该结果只对应提交 `e1a210a`，不代表后续改动；唯一注解是 actions/checkout 与 setup-uv 仍指向 Node 20 的弃用提示，不影响结果。

- 2026-09-15 / A3-01 transcript→Responses input 映射与在途切片修复：新增 `build_responses_input()`，把 `SessionTranscriptLoader` 已校验的消息渲染成新的 `ResponsesInputItem`（仅 user/assistant/system）；`ResponsesRequest.input` 扩展为非空字符串或非空输入项元组，`ResponsesAdapter` 只在 wire 边界把输入项展开成 provider 侧列表。映射重复校验租户/Case/Session 范围、连续序号与角色，`tool` 角色 fail-closed（持久引用不携带 Responses 要求的 `call_id`，当普通消息发送等于伪造协议语义）；空 transcript、空内容和不连续序号同样拒绝。同时修掉在途切片的 2 个 Ruff 错误（`transcript.py` 未使用导入、测试 `UP012`）。离线全量 `292 passed, 87 skipped`；`ruff format --check`、`ruff check` 与严格 mypy（`aftercare_agent tests evals`，104 文件）通过。本轮没有真实 provider 调用；本机 Docker 守护进程未运行，**所有需要 `DATABASE_URL` 的集成测试均未执行**（87 项跳过），上述数字只覆盖离线范围。Worker 内租约/预算/检查点接线、真实 artifact store 仍未实现。

- 2026-09-14 / A1-05 Session transcript 与 C-01 沙箱契约：新增 `SessionMessage` 和 `016_session_messages.sql`，按 tenant/case/session 保存不可变 artifact 引用、摘要、角色和连续 `message_seq`；短锁保证并发追加，`message_id` 完全重放幂等，列表支持游标且跨租户/Case 为空。新增 provider-neutral `SandboxProvider` Protocol，Fake provider 增加带 owner/fencing 的显式 `READY → RUNNING` 幂等启动；真实 artifact store、Responses transcript adapter、Kubernetes/E2B 后端仍未接入。离线全量 `284 passed, 87 skipped`；临时 PostgreSQL 17 全量 `371 passed, 38 warnings`；Ruff、format、严格 mypy、文档检查和 `git diff --check` 通过。

- 2026-09-14 / A3-03-c 与 A2-01 并发可靠性：`PostgresEventTail.stream_async()` 将短数据库轮询放入线程、空闲等待改为异步 sleep，FastAPI follow SSE 使用异步生成器并在线程中执行 Bearer/CaseGrant 复核；工作台新增 keyset 加载更多、筛选/身份切换代际保护和稳定决策幂等键，重复提交在请求完成前禁用且失败可重试；Outbox 拒绝非连续显式 `case_seq`，领取批量限制为 1–500。离线全量 `281 passed, 84 skipped`；临时 PostgreSQL 17 全量 `365 passed, 38 warnings`；前端 Vitest `15 passed`、TypeScript 与 Vite build 通过；Ruff、format、严格 mypy、`uv lock --check`、`git diff --check` 和文档检查通过。仍未完成有界 AsyncConnectionPool、LISTEN/NOTIFY 或 Kafka/NATS/Redis broker、真实身份验收和生产压测。


- 2026-09-14 / 远端 CI 修复：核查 GitHub Actions 发现 `main` 上自 run #37 起连续失败（可见 7 次记录全为 `failure`，21–35 秒内结束）；用 GitHub API 定位到失败步骤是 `Set up Python`（命令 `uv python install 3.13.15`），其后的静态检查与测试步骤全部 `skipped`。根因：uv 0.9.26 内置 Python 下载索引最高只到 3.13.11（`uv python list --all-versions` 实测），而项目固定 3.13.15（2026-08-05 发布的 3.13 维护版），裸装必然找不到该补丁；本地此前是靠 `--python-downloads-json-url` 绕过，CI 没有这个参数。修复：在 `.github/workflows/ci.yml` 的 job 级 `env` 增加 `UV_PYTHON_DOWNLOADS_JSON_URL`，指向固定提交 `dbda4fbf…`（2026-09-09，含 3.13.15）的下载元数据，使 `uv python install` 与 `uv sync --locked` 都能解析该补丁。已排除的方案：在仓库根新增 `uv.toml`——uv 实测警告它会忽略 `pyproject.toml` 的 `[tool.uv]` 字段（含 `required-version`），会静默破坏构建约束。验证：默认索引下 3.13 最高 3.13.11、固定元数据下出现 `cpython-3.13.15-…`；YAML 经 PyYAML 解析确认 `env` 位于 job 级并同时覆盖 `Set up Python` 与 `Install locked dependencies`。本地等价检查（Ruff/format/严格 mypy 含 `evals`/真实 PostgreSQL 全量 `362 passed`）此前已通过，说明失败与业务代码无关。推送后 GitHub Actions run #44（提交 `b0ddffe`）已完成且为 `success`，因此该 CI 解析问题已验证修复；A1-04 仍因 Linux/容器启动和完整重启矩阵未验收而保持 IN PROGRESS。运行页面：[run #44](https://github.com/yushui2022/Aftercare-Agent/actions/runs/34805186170)。本机访问 `raw.githubusercontent.com` 存在抖动（实测一次 `connection reset`、一次超时、重试后成功），CI 侧未复现。

- 2026-09-13 / A3-03-b 工作台实时订阅：工作台新增 SSE 实时订阅（`web/src/sse.ts`：`SseDecoder` 增量帧解析、`parseCaseEvent`、`appendEvent` 去重封顶、`nextBackoffMs` 退避、`subscribeCaseEvents` 游标续订），请求参数与 `PostgresEventTail.validate` 上限一致（`limit=200`、`wait_seconds=60`），流正常结束后立即按最后 `case_seq` 续订，`401/403` 终止不重试；因 `EventSource` 无法附加自定义 Header，改用 `fetch` + `ReadableStream`，不把合成身份降级为 URL 凭据。新增 `web/src/sse.test.ts` 13 项（跨 chunk 分帧、CRLF、注释行、无 id 消息、非法游标/JSON 拒绝、乱序插入、去重与 500 上限、退避封顶）通过；`tsc --noEmit` 与 `vite build` 通过（产物 234 KB JS + 6.2 KB CSS）。对运行中的真实后端验证 `follow=true&limit=200&wait_seconds=60` 返回 `200 text/event-stream`，`limit=900` 返回 `400`。未验证：真实 OIDC 下的订阅授权、浏览器端交互回归、高吞吐 broker tail。

- 2026-09-13 / A3-03-a 工单发现与工作台：新增 `CaseRepository.get_case`/`list_accessible`/`list_for_tenant`、`RunRepository.list_for_case`、`ReviewRepository.list_for_case`、`ApprovalRepository.list_for_case`，以及四个发现端点（`GET /v1/cases`、`GET /v1/cases/{case_id}`、`.../reviews`、`.../approvals`）；Run 投影剔除 tenant/lease/fence，工单不存在或跨租户统一 `403`。新增 `tests/persistence/test_case_queue.py`（6 项：授权收紧与撤销隐藏、过期隐藏、token `case_ids` 收窄、keyset 翻页覆盖、跨租户隔离、非法分页参数）与 `tests/test_operator_workqueue_integration.py`（5 项：列表/详情/子资源、内含字段不泄露、跨租户 `403`、禁用合成身份 `401`）。临时 PostgreSQL 17 全量回归 `362 passed`；Ruff、format、严格 mypy（94 文件）通过。前端 `web/`（React 19.3 + Vite 8 + TypeScript 7）`tsc --noEmit` 与 `vite build` 通过，产物 `dist/index.html` + 226 KB JS + 5.6 KB CSS；合成身份端到端冒烟走 Vite 代理返回 `200`，工单 Run 属性集合不含 `tenant_id`/`lease_owner`/`fencing_token`。未验证：真实 OIDC、live broker tail、生产压测；`web/` 未做浏览器交互与可访问性验收。

- 2026-09-12 / A2-03 durable scheduler：新增 008 Run 状态同步触发器和队列回填；`run_next(tenant_id=None)` 使用持久 tenant cursor 轮转，跳过租户准入已满的队列，并按 Run 过期 lease 回收 IN_FLIGHT。临时 PostgreSQL 17 全量回归 `285 passed`；新增跨租户轮转、租户限额跳过和失联回收测试；Ruff、格式、严格 mypy、wheel 构建和 `git diff --check` 通过。测试容器已移除，未连接生产数据库。

- 2026-09-12 / A3-03 tail 增量：新增 broker-neutral `PostgresEventTail` 和 `follow=true` SSE 参数；每轮查询使用短事务，最长等待 60 秒，保留 `Last-Event-ID`/after 游标；真实 PostgreSQL SSE/tail 测试 `4 passed`，完整 PG 回归 `288 passed`。Kafka/NATS/Redis live adapter、React 工作台、真实 OIDC 与生产压测仍未实现。

- 2026-09-12 / A3-02 观察账本增量：新增迁移 009 与 `InvestigationObservationRepository`，实现工单范围、来源事件/evidence ID 双重幂等、完整 JSON 保存、撤回持久化和重启后完整快照评估；真实 PostgreSQL 全量回归 `290 passed`，静态检查通过。真实来源认证和 EGM 调查 schema 仍未完成。

- 2026-09-12 / C-01 沙箱边界修复：`confirm_destroy()` 现在必须先经过 `DESTROY_REQUESTED`，重复确认保持幂等；4 项沙箱离线测试和静态检查通过。Fake 仍不是安全隔离，也未接入 E2B/Kubernetes。

- 2026-09-12 / B-02-01 审批门禁增量：新增 `010_approvals.sql`、`ApprovalRequest/ApprovalRecord` 和 `ApprovalRepository`；Action 默认 `approval_required=true`，派发按 Case → Run → Action → Approval 锁序重查 APPROVED、摘要、当前策略和数据库有效期，缺失审批 fail closed。临时 PostgreSQL 审批/Action 回归 `8 passed`，离线全量 `243 passed`；审批等待唤醒、真实认证和供应商仍未接入。

- 2026-09-12 / B-02-01 完整回归：审批门禁加入显式低风险豁免路径、过期结算和打包迁移资源；临时 PostgreSQL 全量回归 `296 passed`，Ruff、格式、严格 mypy、`git diff --check` 和 wheel/sdist 构建通过。临时 PostgreSQL 容器已移除；远端 CI、真实认证和供应商仍未验证。

- 2026-09-13 / B-02-02 审批等待联动：新增 `011_approval_wait_binding.sql` 和 `WaitRepository.resolve_locked()`；绑定 `run_id/wait_id/generation` 的批准/拒绝在 Case → Run → Wait → Action → Approval 锁序下写 Inbox 并原子结算 Wait/Run，重复决定不重复唤醒，超时代次不复活，宿主事务回滚同时回滚审批和唤醒。临时 PostgreSQL 全量回归 `299 passed`，静态检查通过；审批 API、真实身份和供应商仍未接入。

- 2026-09-13 / A2 纵向恢复切片：新增 `runtime.vertical_slice.SyntheticAftercareFlow` 和 PostgreSQL 集成验收，覆盖受理、事务外 Fake Harness、`WAITING_INPUT` 停止、Inbox 原子唤醒、重复投递和另一 Worker 按显式恢复阶段继续；专测与 Wait/Worker 回归 `13 passed`。这不是 A3-04 完整业务评测：尚未生成有来源调查建议、REVIEW 分支或真实模型效果/成本报告。

- 2026-09-13 / A2 纵向证据增量：纵向切片加入受信订单快照、物流状态和买家陈述的持久观察，`assess()` 对完整账本生成带引用的 `RECOMMENDATION_READY`，并保留冲突/错配时转 `HUMAN_REVIEW` 的确定性规则；相关 PostgreSQL 回归 `6 passed`。这仍不是真实 EGM provider 或真实模型评测。

- 2026-09-13 / A2 评估快照：新增迁移 `012_investigation_assessments.sql`、`InvestigationAssessmentRepository` 和不可变 assessment digest；调查结果按 tenant/case/run 保存，重启后可读取最近快照，重复写入按完整结果幂等校验。临时 PostgreSQL 全量回归 `302 passed`，静态检查通过；快照仍不是外部动作授权。

- 2026-09-13 / B-02 合成动作切片：纵向流程新增可信 `ActionIntent`、绑定 `WAITING_APPROVAL`、审批原子唤醒、批准后 `mark_requested` 复核和 fake provider `CONFIRMED`；金额/币种/供应商键均不来自模型。相关 PostgreSQL 审批、Action、纵向回归 `13 passed`；真实审批身份、支付聚合、供应商未知回执和拒绝后 REVIEW 仍待实现。

- 2026-09-13 / Review 与门控事件收口：修复已决定 Review 的旧决定/请求重放不能误路由后续 Run；新增 `approval.requested/decided/expired`、`review.requested/decided` 事件、事务内 Case 序号分配和不可变请求/决定快照。待本轮 PostgreSQL 17 全量回归后记录实际结果；人工工作台、真实身份和供应商仍未接入。
- 2026-09-13 / Review 与门控事件验收：临时 PostgreSQL 17 全量 `308 passed`、16 warnings；Ruff、format、严格 mypy 和 `git diff --check` 通过。事件决策与 Review 回归证明首次写入产生明确类型、Case 序号连续、快照可重放，重复请求/决定不重复产生事件；临时容器已移除。远端 CI、真实身份、供应商和生产压测仍未验证。
- 2026-09-13 / 可复制演示入口：新增 `aftercare-demo`/`python -m aftercare_agent.demo`，串联 admit、等待/Inbox 唤醒、检查点恢复、可信观察评估、审批和 fake provider；Compose 为 PostgreSQL 增加可覆盖宿主端口并使用 `up --wait`。临时 PostgreSQL 17 + Compose CLI smoke 成功，输出 `recommendation_ready`、`APPROVED`、`CONFIRMED` 及事件 seq=1/2；PostgreSQL 17 全量回归 `313 passed, 16 warnings`，无数据库的离线全量回归 `249 passed, 64 skipped`，远端 CI、真实模型/连接器/沙箱仍未验证。
- 2026-09-13 / Operator 控制面：新增 Review/Approval 读取与决定 API，决定身份仅来自认证 `subject_id`，输入模型拒绝 authority 字段，响应过滤租户/证据哈希/策略/等待绑定等内部字段；新增显式权限 scope 和 synthetic 仅测试授权。全新 PostgreSQL 17 全量回归 `319 passed, 22 warnings`，API 集成覆盖首次决定、同键重放、跨工单、伪造 actor 和 synthetic 关闭；真实 OIDC/JWKS、支付聚合和供应商仍未接入。
- 2026-09-13 / D-01-01/02 认证切片：新增 `JwtJwksVerifier` 和 FastAPI `Authorization: Bearer` 入口，固定 HTTPS JWKS、RS/ES 算法与 typ 白名单、短期缓存、未知 `kid` 单次冷却刷新、过期缓存失败关闭、exp/iat/nbf/最大寿命校验及 scope/tenant/Case 映射；默认由数据库 CaseGrant 做资源授权，token case_ids 仅作收窄；新增 RSA + `httpx.MockTransport` JWT 专测 `19 passed`，API/auth seam 测试一并通过。真实 IdP、撤销/introspection、轮换演练和生产密钥配置仍未完成。
- 2026-09-13 / Admission lease safety：slot renewal 现在要求已持有的 reservation 实际续租成功；缺失、过期或被新 fencing token 接管的 slot 会让 Heartbeat fail-closed，不再只延长 Run lease。slot replay 返回数据库写入后的新 expiry，并补充离线 Heartbeat 与 PostgreSQL 缺失/过期/接管回归；专测 `1 passed, 3 skipped`（无 DATABASE_URL），临时 PostgreSQL admission/worker 回归 `12 passed`。真实配额 API、生产压测与动态配置仍未完成。

- 2026-09-12 / A1-04 Compose 启动边界：API 增加 `/readyz` healthcheck，Worker profile 等待 API readiness（从而等待迁移完成），补充 daemon/跨租户队列环境参数；`docker compose config` 已静态核对，未做生产部署。

### A1-02：最小 API 与认证边界

2026-09-12，临时 PostgreSQL 17 Docker 容器和 FastAPI TestClient；合成身份仅在测试 App 显式开启，容器测试后已移除。

| 验证 | 实际结果 |
|---|---|
| 离线边界 | 合成身份默认关闭；OpenCaseInput 拒绝 authority 字段；默认 App 无数据库配置仍可安全导入 |
| API 端到端 | 受理、同键重放、同键改参 `409`、跨租户读取 `403`、生产模式合成身份 `401`：7 项测试通过 |
| 静态与全量回归 | Ruff、严格 mypy；A1-02 阶段为 `215 passed, 7 skipped`（无 DATABASE_URL 的集成测试跳过） |

当前 API 已支持配置静态 JWKS 的 Bearer 验证，同时保留仅显式开启的合成身份测试入口；PostgreSQL CaseGrant 已提供最小数据库资源授权，真实 IdP 权限映射、撤销/introspection、live 消息接入和真实业务动作仍未实现。SSE 目前仅提供有界持久回放。FastAPI 的 TestClient 对当前 Starlette 版本产生弃用警告，不影响测试结果，后续可在依赖升级时切换 `httpx2`。

### A1-03：Fake Harness 与检查点恢复

2026-09-12，Windows / CPython 3.13.15；新增 `aftercare_agent/runtime/harness.py`。该实现不调用模型、网络、数据库或供应商，仅用于把执行循环的边界先固定下来。

| 验证 | 实际结果 |
|---|---|
| 固定计划 | `lookup_order → lookup_tracking → request_material_draft`，工具请求经过 v1 schema 校验；不允许模型填写租户、工单或订单身份 |
| 预算 | 1 次模型步、3 次工具步、固定截止时间；超过 `max_steps` 返回可恢复的中间检查点 |
| 恢复 | 从 `Checkpoint` 继续推进至 `complete`；工具结果只保存确定性 artifact 引用，不伪造业务事实 |
| 隔离 | 租户/工单/Run scope 不匹配返回 `FORBIDDEN`；检查点 JSON round-trip 保持等价 |
| 回归 | `pytest -q tests/test_fake_harness.py`：3 passed；Ruff 与严格 mypy 全通过 |

Fake Harness 不是完整 Worker，也不持有数据库租约；下一步 A1-04 才会把它接到最小 Worker/Compose 和 CI 的真实 PostgreSQL 服务中。详细边界见 [Harness 运行说明](harness.md)。

### A1-01：PostgreSQL 持久化骨架

2026-09-09，使用临时 PostgreSQL 17 Docker 容器（`127.0.0.1:55433`，测试后已移除）运行真实数据库验收；所有记录为合成测试数据，未连接现有业务数据库。

| 验证 | 实际结果 |
|---|---|
| 迁移 | `001_initial.sql` 在空数据库创建 admission、Case、Session、Run、Step、Attempt、Checkpoint 表；修复并验证 Step 复合外键唯一约束 |
| Run 执行权 | 领取使用 `FOR UPDATE` + `clock_timestamp()`；过期接管 fencing token 单调递增；旧 owner 续租被映射为 `LEASE_LOST` |
| 并发 | 两个独立 psycopg 连接同时领取同一 Run：1 个成功、1 个 `CONFLICT` |
| 受理与版本 | 相同幂等键/摘要重放原对象；改参返回 `CONFLICT`；Case version compare-and-swap 拒绝旧版本 |
| 检查点与子对象 | 当前 claim 才能写 Checkpoint；Session/Step/Attempt 读写和 Attempt 终态保护通过 |
| 测试 | `pytest -q`（设置 `DATABASE_URL`）：`215 passed`；无数据库时持久化测试安全跳过 |
| 静态检查 | Ruff check、format --check、严格 mypy、`uv lock --check` 和 whitespace 检查通过 |

这证明的是 A1-01 的数据库骨架和基础竞争不变量，不包含完整事件发布、gap buffer、崩溃恢复、API 认证、真实模型、供应商动作或生产 HA/备份。

### A0-03：合成案件与确定性评测

2026-09-09，Windows / CPython 3.13.15 / uv 0.9.26；新增 `evals/` 离线评测基线，全部输入为合成数据，不调用模型、网络、数据库、供应商或沙箱。

| 验证 | 实际结果 |
|---|---|
| 合成案件 | 12 个：正常、缺材料、承运商/买家冲突、陈旧、跨订单、跨租户、不确定状态、重复证据 ID、重复来源事件、未来时间、撤回、提示注入 |
| 期望与决策核对 | `v1.json` 固定 disposition、缺失材料、冲突、不可用原因、claim 接受/拒绝、引用和拒绝原因；所有 assessment 均断言不授权外部动作且不关单 |
| 离线 runner | `python -m evals.runner`：12/12 PASS；digest `80ddf7f04955ab45130bf1aa97aa04a7e499480b92a978d72a3e135e82744dd7` |
| 稳定性 | 同进程和独立子进程 digest 比较通过；digest 纳入评测器版本、案例集合和期望文件哈希 |
| 回归 | `pytest tests/evals`：4 passed；完整 `pytest -q --tb=short`：210 passed |
| 静态检查 | Ruff check、format --check、严格 mypy 全通过 |

此验收只证明纯契约和合成输入的确定性行为，不证明 PostgreSQL 事务、Worker 租约/fencing、模型质量、连接器认证、沙箱恢复或生产 SLA。评测说明见 [A0-03 合成案件与确定性评测](evals.md)。

### 本次提交前复验

2026-09-07，现有 .venv / Python 3.13.15：Ruff check、format --check 与严格 mypy 全通过（16 个 Python 文件）；pytest -q --tb=short 为 206 passed，39.33 秒。未重复构建安装包，沿用下方 A0-02 的相同源码产物验收；本次只更新交接记录。独立只读范围复核通过；34 个候选文件的常见凭证模式扫描未命中，没有大文件/缓存/构建产物。基础模式扫描不是完整安全审计。文档与暂存区 whitespace 在提交前再次核对。

### A0-02：运行时与调查契约

2026-09-07，Windows / CPython 3.13.15 / uv 0.9.26；依赖保持 A0-01 的 uv.lock。产物定义见[运行时契约](contracts/runtime-v1.md)和[调查契约](contracts/investigation-v1.md)。

| 验证 | 实际结果 |
|---|---|
| .venv Python -m ruff check . / format --check . | 全通过，16 个 Python 文件格式检查 |
| .venv Python -m mypy | 严格检查 16 个文件，无问题；未新增全局忽略 |
| .venv Python -m pytest -q --tb=short | 206 passed，5.63 秒；197 项契约正反例 + 既有 9 项回归 |
| JSON Schema / 兼容检查 | 39 个唯一契约模型可生成 schema 且 extra 禁止；退款适配器与 A0-01 wheel 内容相同 |
| 源码分发 / wheel | 显式固定构建环境中 sdist→wheel 成功；sdist 包含 5 个嵌套测试模块与锁/解释器记录；wheel 含 7 个 domain 模块，共 14 个成员 |
| 仓库外安装 | 在原锁定 23 包的独立环境中 --no-deps --reinstall wheel，pip check 通过；实际模块路径均在 site-packages |
| 从 sdist 取测试复验 wheel | 仓库外 python -I -m pytest --import-mode=importlib，206 passed，49.18 秒；同一套测试，不累计成 412 项，也不作为性能基准 |
| 独立复核 | 运行时/调查分工实现与运行时独立评审；修复 Inbox 缺正文哈希的契约缺口，补非有限 JSON 数值反例 |

测试覆盖：授权/对象层级、规范幂等摘要、到期与旧 Run token、终态与状态字段、等待代次/早到/迟到、同键改正文、事件缺口/重放冲突、检查点版本/输入/范围、部分或越权工具调用、原始观察时间、来源能力、撤回/过期、跨订单以及签收与未收货陈述矛盾。此处均是纯函数与类型验证，不能证明数据库执行权或业务来源真实。

环境异常如实保留：本轮遇到解释器与标准扩展 DLL 间歇 WinError 32，未确认占用者/根因；没有关闭安全软件、终止其他项目或改系统 Python。默认 uv 隔离构建也因此失败，改在 G 盘临时独立环境安装并核验 setuptools=84.0.0、wheel=0.48.0、packaging=26.3，以 uv build --no-build-isolation --python <该环境> 完成两步构建。最终普通 .venv 的检查/回归及独立 wheel 回归均重跑通过。超长参数用例 ID 曾造成测试 setup/teardown 错误，改用短 ID 后复跑全量，不删减测试输入。

临时记录与安装环境在 G:\DevCache\Temp\aftercare-a002：python、isolated-venv（固定构建工具）、verify-venv（最终安装 wheel，23 包）、dist、wheel-check（从 sdist 解出的测试）和测试临时目录。未清理；不是生产服务。A0-01 项目 dist/ 的历史产物未被覆盖。A0-02 验收产物 SHA-256：

- dist/aftercare_agent-0.1.0a0.tar.gz：4688a0f8610628f2feba8e04f8a89b9a8fc7953ed790f0bc3dbca0fac06fb8b3。
- dist/aftercare_agent-0.1.0a0-py3-none-any.whl：6814c72cab88c710931884993903bb15133164d2c67b23c1a44a12527b34b7d9。

文档最终复跑通过：13 份文档、105 个本地链接、25 个任务定义、4 行状态与 7 张 Mermaid 语法；已跟踪修改和 26 个未跟踪文件的 whitespace 检查通过。检查器仍在 G:\DevCache\Temp\aftercare-design-qa\check.mjs，不是项目运行依赖。

未验证/未实现：真实 IdP/CaseGrant 与撤销演练、选定 broker 的 live tail、配置版本仓库、模型效果、真实调查 EGM schema/来源认证、E2B/Kubernetes 后端、Linux/容器试点、远端 CI 结果、真实供应商和生产压测。A1/A2/A3 必须继续补足，不能用本轮测试替代。

### A0-01：Python 工程基线

2026-09-07，Windows / PowerShell 7.6.5 / 常规 CPython 3.13.15 / uv 0.9.26，在本轮未提交工作区执行：

| 检查 | 实际命令/范围 | 结果 |
|---|---|---|
| 开发环境 | uv sync --locked；全部直接/传递依赖按 uv.lock | 23 个包含开发工具的包，安装成功 |
| 静态检查 | uv run --locked ruff check .；ruff format --check .；mypy | 全通过；格式与严格类型范围均为 4 个 Python 文件 |
| 开发环境回归 | uv run --locked pytest -q | 9 passed，0.54 秒；4 项适配器回归 + 5 项安装包契约 |
| 冷缓存安装 | 独立 UV_CACHE_DIR / UV_PROJECT_ENVIRONMENT；uv sync --locked --no-editable | 从远端获取固定 EGM SHA；23 包安装成功，uv pip check 通过 |
| 源码分发构建 | uv build --sdist --out-dir dist | 成功 |
| sdist 构建 wheel | uv build --wheel --out-dir dist dist/aftercare_agent-0.1.0a0.tar.gz | 成功；不是只从源码目录构建 wheel |
| 安装包独立运行 | 在冷环境以 --no-deps --reinstall 安装该 wheel，保留锁定依赖；仓库外 python -I -m pytest --import-mode=importlib | 9 passed，2.33 秒；不是 18 个不同测试 |
| 来源与文件核对 | 两包 __file__ 都在冷环境 site-packages；EGM direct_url 的 Git SHA；非 editable；wheel 目录/元数据 | 通过；Aftercare wheel 7 个成员，含 py.typed；EGM schema/SQL 可读 |
| 独立只读复核 | 固定依赖、打包缺失风险、HEAD 对比、局部类型例外、测试保证范围 | 未发现阻断项；不把资源检查说成 PG 行为测试 |

构建产物在 dist/，不提交 Git。本次验收产物 SHA-256（重新构建可能改变，不承诺字节级可复现）：

- aftercare_agent-0.1.0a0.tar.gz：1ee81509c7fd6a68057afee678f28c09764da6fea603e52b4c6a507726fed6ad。
- aftercare_agent-0.1.0a0-py3-none-any.whl：8b1753fedd6d80d5d660f9ac22b787d4864afbfaa88cffe7fa9762a9d74ea5a2。

G 盘开始时约 454 GB 空闲；项目 .venv/dist、G:\DevCache\uv 和 G:\DevCache\Python 保存环境与产物。独立验收材料在 G:\DevCache\Temp\aftercare-a001 下的 cold-cache、cold-venv、wheel-check，未清理。可复跑的参数与命令见[开发指南](development.md)，其他机器不要求这些个人绝对路径。

已解决的中间问题：旧 uv 的内置解释器目录不认识 3.13.15，改用官方下载元数据成功；补齐本项目类型与格式，未整体关闭错误；Ruff 限定 Python/TOML，不为通过检查改写历史文章片段。EGM 未注解返回值只在适配器边界 cast，两处测试构造器局部忽略 no-untyped-call；这不是新增运行时校验。

文档验收：本机 node G:\DevCache\Temp\aftercare-design-qa\check.mjs 检查 11 份文档、80 个本地链接、25 个任务定义、3 行状态和 7 张 Mermaid 语法；Git 已跟踪变化与 11 个未跟踪文件均通过 whitespace 检查。检查器仍在本机临时目录，不是项目运行依赖；没有做浏览器排版验收。

未覆盖：Linux/容器、EGM 全量回归、真实 PostgreSQL 行为、远端 CI、模型效果、真实连接器、安全漏洞扫描、生产部署与压测。依赖兼容和安装成功不等于这些能力已经验收。

### DOC-001 历史工程文档检查

2026-09-07，在当前未提交文档工作区执行以下检查：

- 本机命令：node G:\DevCache\Temp\aftercare-design-qa\check.mjs。检查器复用已有 mermaid 11.12.0/jsdom 26.1.0；这不是项目运行时依赖。
- 结果：9 份文档、62 个本地链接、25 个唯一任务定义、2 行任务状态、7 张 Mermaid 均通过相应文件存在/引用/状态值/语法检查；文档没有尾随空白，代码围栏配对。
- Git 检查：git -c core.safecrlf=false diff --check；对 5 个未跟踪 Markdown 文件分别使用 diff --no-index --check -- NUL，并核对无诊断输出。已跟踪/未跟踪文件 whitespace 检查通过。
- 人工/独立只读复核：恢复协议、唯一状态源、历史验证标注、阶段依赖与当前 EGM 代码边界通过。已修正“无沙箱试点也强制依赖真实沙箱”的任务依赖。
- 未覆盖：浏览器排版验收、Python 回归、真实 PostgreSQL 新测试、依赖兼容安装、模型效果和任何部署。没有修改 Python 业务代码，不重报历史 pytest 数量为当前结果。

临时检查器位于本机 G 盘缓存，未纳入项目依赖；它的可用性需在复跑前核验。任务交付依据是仓库文档与上述实际结果，不应假定其他机器有这个绝对路径。

### 历史组件验收（本轮未重跑）

2026-09-06 的 [EGM 接入验收](integrations/egm-embedded.md)记录：EGM 303 passed、2 skipped、5 warnings；Aftercare 4 passed；Windows、Python 3.13.11、隔离 PostgreSQL 17.11。对应上方源码基线；不是完整售后系统的验收。

上次架构文档记录 7 张 Mermaid 与 28 个本地链接通过检查，也不是本轮新增文件已经通过的证明。历史 PostgreSQL 测试专用服务据原交接已停止，本轮没有探测其当前进程状态。测试数据/依赖曾保存在 G:\DevCache\Temp\egm-embedded，不是生产数据库；接手使用前重新核验。

## 7. 下一步与未决项

当前推进 D 生产准入。D-04 已落地有界连接池（[ADR-0006](decisions/0006-bounded-connection-pool.md)）：`Database` 默认带池、借用超时 fail-closed、`startup()` 预热、心跳改用 `open()`/`release()`，Broker、暖池、ACP、长期记忆与额外供应商明确保留为候选并各自写下触发条件。验签（D-01-01/02）、数据库 CaseGrant（D-01-03）、撤销判定（D-01-04）、授权管理面（D-01-05）、工作台单工单访问管理（D-01-06）与租户工单发现（D-01-07）已实现，D-01 剩下真实 IdP 演练与权限映射、RLS 与租户级授权总览（“谁对这个租户的哪些工单有权限”，D-01-07 只解决“有哪些工单”）；A2-02 的心跳续期延迟预算已修正。仍未实现：固定调查 EGM schema、真实沙箱后端、完整业务 Harness 与生产连接器；模型端已经接上真实 provider 并存下实测，剩下的不是“接线”而是 `route_reason` 的 operator 投影与预算/放行的运维入口。D-03 已交付定标方法与第一轮实测（[ADR-0007](decisions/0007-pool-metrics-and-capacity.md)、[容量报告](capacity/README.md)）：`aftercare-capacity` 按 steady/burst/wake 扫描尺寸并以 p95 预算判定达标，池指标 `aftercare.db.pool.*` 默认每 10 s 发布到进程日志。本机结论是突发先撞握手与懒增长而不是上限（懒增长时 1–32 没有尺寸达标；预热到 16 后 `max_size=16` 达标且 32 无差别），因此 min 1 / max 8 / 超时 5 s 仍然是**有界默认值**而不是容量结论。D-02 已交付一次性 dump + 清单 + 恢复演练 + 保留计划 + 恢复后核对（[ADR-0008](decisions/0008-backup-and-restore-drills.md)、[运维手册](operations/backup-restore.md)），第二刀交付了演练记录与新鲜度判定（[ADR-0009](decisions/0009-backup-freshness-and-drill-records.md)），第三刀交付了 WAL 归档检查（[ADR-0010](decisions/0010-wal-archive-checks.md)）：判据是一场真跑过的演练，而不是作业退出码；`status` 现在能按预算回答“最新恢复点在 RPO 内吗、最近一次成功演练够新吗”，但**预算没有默认值**，所以真实 RPO/RTO **数值仍然未定**；第三刀让“恢复点之后的 WAL 在不在”也可判定（连续性、归档器是否在推进、是否覆盖最新 dump，滞后预算同样没有默认值）；它没做的事同样明确——没有按时间点的 PITR 演练本身、没有对象存储与异地副本、没有备份加密与密钥托管，演练仍要人工或外部调度触发（仓库不内置调度器，文档里的 cron/systemd/K8s 接线**未在目标环境执行过**）。下一步优先顺序：先补 D-02 剩下的两件最紧的事——按部署目标确定 RPO/RTO **数值**（由此才能决定异地副本与保留期限是否必须）与把演练接进定期执行和告警；然后回到 D-03 剩余（容器/CI 复跑同一条 sweep、把面向业务的队列年龄接进观测）；再往后才是 C-03 导出器与 D-01 剩余（真实 IdP 演练与权限映射、RLS、租户级授权总览）。用户本轮给出的代码推进顺序是：持久路由（已落地）→ 把连接器答案桥进 EGM 变成可审计证据（A3-02）→ 才谈真实沙箱后端（C-02）；分层调度仍缺 class 生产者（`aftercare_execution_queue.priority` 只被读、从未被写），所以“按层生效”目前只在沙箱规格一侧成立，队列排序与审批阈值还没有。

尚待决定但不阻塞离线骨架：真实模型 ID/预算、商家渠道和身份提供者、沙箱/对象存储后端与地域、RPO/RTO 和生产负载目标。每项的决策阶段已列在技术栈和执行计划中。无业务凭证不阻塞 Fake 流程；真实接入缺授权时必须停止该分支。

关键风险：D-02 之后新增三条同源风险：(1) 演练通过只说明“这份 dump 能恢复”，不说明“恢复后业务状态正确”——恢复数据库会把外部世界已经发生的事回滚掉，所以恢复后要跑 `reconcile`，而不是只看行数一致；(2) `reconcile` 的 60 s 安全边界是经验值，cut 离恢复点太近会漏掉“更新过、但外部动作已经生效”的记录，调小它必须同时接受更多噪音；(3) 保留策略只看本地目录，`--apply` 删掉的副本目前没有异地兜底，所以在异地副本与备份加密落地之前不要把 `--apply` 放进定时任务；备份文件按定义含全部业务数据与审计记录，`drill` 的临时库和本机演练产物同样如此，不得把 dump 提交进仓库或留在共享目录。D-02 第三刀之后又多一条同源风险：`wal` 读到的是**目录里现在有什么**，它不会发现一个从来没被拷进去、但未来会被拷进去的段，也不会发现存储层自己把旧段删了——没有 `--dsn` 时它答不了“归档器还在推进吗”，读到那行 `archiver not read` 就必须去问服务器，不要把它当成“没问题”。此前风险：本轮包依赖升级尚未重跑 EGM PostgreSQL 全量验收，A1 必须补足；新旧路线图的阶段名称必须一致；不能把 fencing 延后为性能优化；A 阶段通知不得真实外发；同进程不等于同事务；未提交图稿和其他仓库改动不属于本任务；`grant:admin` 在真实 IdP 里必须只发给管理角色，否则等于交出 ACL 写入权。D-01-07 之后新增一条同源风险：`grant:read` 持有者能看到本租户**全部**工单编号、订单号与状态，它不再是“无害的只读 scope”，真实 IdP 映射时必须和管理角色同等对待；同时“控制面清单 ≠ 数据面授权”这条边界不能在下一次迭代里为了“顺手少一次请求”而被合进 `list_accessible`。D-04 之后新增一条同源风险：池的默认规模与超时是**有界默认**而不是压测结论，D-03 定标前不得当作容量承诺；池规模要按并发而不是队列长度估，并且池化连接上不得留下 `SET`、`LISTEN` 或 `search_path` 这类会话状态（池只回滚归还时未结束的事务，不重置会话），否则会跨借用者泄漏。D-03 之后新增两条同源风险：(1) 容量数字不能跨环境外推——探针的池峰值是采样值（默认 5 ms 一次，报告里带 `watcher_samples`），而本机 16 线程本身就是测量下限，跨主机只能比相对差异；(2) `min_size`（预热）与 `max_size`（上限）不是同一个旋钮，把 `max_size` 当成扛突发的手段会白花预算——本机实测它从 8 提到 32 对 p95 没有帮助。本切片新增一条同源风险：放行一个预算耗尽的 `REVIEW` Run 是**空操作**——花掉的预算随检查点一起持久化，重新进入 model 阶段会立刻再落同一个 `model_budget_exhausted` 路由（不花钱，但也不会前进），在“提额/换策略”的运维入口落地前不能把它当成“重试一下就好了”；同理，`route_reason` 还没进 operator 投影，接手者只能读检查点负载才知道 Run 为什么停下来。

第二刀之后新增两条同源风险：(1) `status` 判的是“目录里的记录与预算的关系”——它读 dump 的字节数与 SHA-256 字段，但不重算 dump 的内容，所以记录可读而 dump 已被替换或损坏时 `status` 仍可能报通过，**`verify` 才是完整性判据，两者不能互相替代**；(2) 演练记录与 dump 写在同一目录、同一个可写身份下，能改 dump 的人也能改记录，因此它防的是“忘记演练”和“演练静默失败”，不防内部人伪造演练；异地副本与备份加密落地前不要把 `status` 的通过当成合规证据。另外“未配置预算 ≠ 已满足”必须保持——任何为了“让面板变绿”而给出默认 RPO/RTO 的改动，都会把一个未决的部署决策伪装成通过。D-02 原有三条风险仍然成立。
工具执行面之后新增的未决项与风险：(1) **已解决（本切片）**：模型在工具预算耗尽后仍提议工具、以及预算耗尽、provider 拒绝、意图被拒这些失败，都不再抛异常、也不再让 Run 悬在 RUNNING 直到租约过期；它们落成持久路由（`RouteReason` + `REVIEW`/`RETRY_AT`）并由 Worker 按检查点决定 Run 状态，且 3 项真实 PostgreSQL 故障测试真跑了。剩下的问题是“放行一个预算耗尽的 Run 仍是空操作”与“`route_reason` 还没进 operator 投影”，已记进关键风险段；(2) 沙箱 tier 目前只按工具名映射，`aftercare_execution_queue.priority` 仍从未被写入，所以“按层次调度”只在沙箱规格上成立，队列排序与审批阈值还没接；(3) 连接器答案尚未转成 `OrderSnapshot`/`LogisticsObservation` 并入 EGM，`evidence.py` 仍只覆盖退款证据，订单/物流结论暂不进证据账本；(4) `SandboxedToolExecutor` 的 `request_key` 含 worker 身份，换 owner 的恢复会开新沙箱——这是为了让崩溃恢复可行，代价是一次重放的沙箱开销，不是副作用重放。

## 8. 最近交接记录
- 2026-09-16 / A3-01 持久路由（完成并推送，提交号见 Git 日志；推送后 CI 结论并入下一次真实改动）：本切片只做用户指定的第一段——把模型驱动的失败路径做成持久路由。判断依据是上一段实测暴露的现象：模型预算耗尽、provider 拒绝、工具意图被拒都还没有“落点”。做法与实测见第 6 节同一条目；3 项真实 PostgreSQL 故障测试是真跑的，不是离线替身。**给下一位接手者**：（1）`REVIEW` 与 `RETRY_AT` 现在是真会出现的状态，运维视角在此之前从未见过；（2）放行一个预算耗尽的 `REVIEW` Run 仍然是空操作，需要“提额/换策略”的入口才谈得上恢复；（3）`route_reason` 只在检查点负载里，operator 投影还没暴露它；（4）本机那 3 项间歇失败（SSE tail 2 项、池借用 1 项）已在未改动的 `bdda584` worktree 上复现，属于这台临时集群的时序抖动，不要误判为新回归。下一段按用户给的顺序：把连接器答案桥进 EGM 变成可审计证据（A3-02），之后才是真实沙箱后端（C-02）。

- 2026-09-15 / D-02 WAL 归档检查（完成并推送，提交 `30d3c27` + `130352f` + `e8b8b9b`，CI run [34976339343](https://github.com/yushui2022/Aftercare-Agent/actions/runs/34976339343) 为 `success`，`632 passed` 且零跳过）：起因是前两刀只能回答“最新那份 dump 多旧、它被证明能恢复过吗”，而一份 dump 只是某一刻的完整拷贝：这个瞬间到“现在”之间写入的数据一个字都没被保护。做法：新增 `wal` 子命令回答三个问题并拒绝第四个——归档段是否成链（只读段名，不需要段大小；有洞时洞之后的一切都不能用于恢复，所以这一问必须有确定答案）、归档器还在不在推进（读 `pg_stat_archiver` 判断“最后一次失败是否晚于最后一次成功”，这是唯一能发现 `archive_command` 静默失败、PostgreSQL 无限重试同一段的信号，只有服务器能答）、归档是否覆盖最新那份 dump（清单 v2 记下与恢复点同一快照读出的 `wal_lsn`）；四是“滞后多少算超标”，`--archive-lag-seconds` **没有默认值**，未配置只报告不判定。`--dsn` 可选，因为服务器没了时正是最需要看归档的时候；给了 `--dsn` 则服务端段大小优先，与 `--segment-size` 矛盾即报错。**本机实测**（真实 `archive_mode = on` 临时集群）三幕：正常时退出 0；破坏 `archive_command` 后**目录看起来仍完好**、只有问服务器才看得出退出 1；删掉中间段且不问服务器时退出 1。**CI 曝出两个真问题**：首次推送的 run 结论是 `success` 但 3 项归档用例实际跳过了（`629 passed, 3 skipped`）——工具安装步骤在 `initdb --version` 上以 127 失败却被 `continue-on-error` 吃掉（`$GITHUB_PATH` 只对后续步骤生效），而临时集群在 Debian 上默认把 socket 往 `/var/run/postgresql` 写、那里属于发行版自己的 `postgres` 用户。修复后三项真跑、零跳过，并加了一道闸门：`AFTERCARE_REQUIRE_DRILLS=1` 下演练模块把跳过当失败。**仍缺**：按时间点的 PITR 演练本身、对象存储与异地副本、备份加密与密钥托管、RPO/RTO 数值、调度落地。
- 2026-09-15 / D-02 演练记录与新鲜度判定（完成并推送，提交 `59e5945`，CI run [34970075358](https://github.com/yushui2022/Aftercare-Agent/actions/runs/34970075358) 为 `success`，`599 passed` 且零跳过）：起因是上一刀的遗留问题——“有没有备份、这份备份能不能恢复”已经可验证，但没人能回答“最近一次真跑过的恢复演练是什么时候、最新的恢复点还新不新”，而运维里最容易发生的失败不是 dump 坏了，而是演练悄悄停了没人发现。做法：`drill` 无论成功失败都在 dump 旁写 `<name>.drill.json`（恢复点、RTO、逐项检查、失败原因截断到 400 字符；不含 dump 内容、租户数据或路径），因此目录能区分“演练过但失败”与“从来没试过”，而 `drill --json` 仍是可归档的完整报告。新增 `status` 子命令，把两个不同的问题分开回答：最新恢复点年龄是否在 RPO 内、最近一次**成功**演练年龄是否在间隔内（给了 `--drill-interval-seconds` 就隐含要求“必须存在一次演练”）。**预算没有默认值**：未配置只报告不判定并在输出里写明 `budgets none stated`，因为未配置不等于已满足。策略问题与完整性问题同样分开——空目录、dump 缺失、记录不可读无论预算如何都失败（1）或报错（2），不把“读不出来”当成“没有”。退出码 0/1/2，`--emit-metrics` 走 D-03 已有的 Metrics 接缝（唯一标签 `component`），仓库不内置调度器，cron/systemd/K8s 只在文档里且**未在目标环境执行过**。`apply_retention` 现在连同 `.drill.json` 一起删，避免记录比 dump 活得更久。名字边界只留一份定义：`BACKUP_NAME_PATTERN`/`BackupName`/`validate_backup_name()` 移到 `ops/tooling.py`。本机实测（临时 PostgreSQL 16.13）：`create` 后按 24 h 演练间隔查询退出 1、`drill --upgrade` 退出 0（RTO 1.147 s）、随后双预算查询退出 0；重建库后全量 `599 passed`，远端 CI 同样 `599 passed`。仍缺：RPO/RTO 数值、WAL 归档与 PITR、异地副本与备份加密、调度落地、演练失败后的自动升级路径、多租户选择性恢复。
- 2026-09-15 / D-02 备份与恢复演练（完成并推送，提交 `1afe587` + `1e9d9ef`，CI run [34967559546](https://github.com/yushui2022/Aftercare-Agent/actions/runs/34967559546) 为 `success`，`580 passed` 且零跳过）：起因是“备份作业退出码 0”并不能说明这份备份能恢复。做法：`aftercare-backup` 的 `create` 在一个 `REPEATABLE READ` 事务里 `pg_export_snapshot()`，把同一快照同时喂给清单读取和 `pg_dump --snapshot`，于是清单里的每张表行数描述的就是这份 dump，而不是对它的近似；清单记恢复点、库名与服务端/客户端版本、schema 版本、每条迁移的 SHA-256、dump 的字节数与 SHA-256。`drill` 才是判据：建 `aftercare_drill_<时间戳>` 副本、`pg_restore --single-transaction --exit-on-error`、比对迁移摘要/表集合/每张表行数，可选 `--upgrade` 在副本上跑一次 `migrate()`，最后删副本；清理失败不掩盖原异常。`retention` 先出计划、`--apply` 才删，永不删空。`reconcile` 只读地列出恢复点无法描述的 Action（全部 `UNKNOWN`，加上 `updated_at` 晚于“恢复点 − 60 s 安全边界”的），因为恢复一个数据库只会回滚外部动作的记录，不会回滚外部动作本身。客户端 major 低于服务端在写任何文件前就失败；密码只经 `PGPASSWORD`，不进命令行与日志。**本机实测**（临时 PostgreSQL 16.13、客户端 16.13）：111,950 字节 dump、31 张 `aftercare_` 表、迁移 16，`restore 0.703 s` / `verify 0.158 s` / `upgrade 0.151 s`，RTO 1.012 s，6 项检查全 ok；本机全量 `580 passed`（55.88 s），离线 `459 passed, 121 skipped`，Ruff、严格 mypy（126 文件）与 `docker compose config --quiet` 通过。**CI 上真跑了**：第一次 run [34967406421](https://github.com/yushui2022/Aftercare-Agent/actions/runs/34967406421) 的 7 项演练用例因 PATH 里的 `pg_dump` 是客户端 16 而带原因跳过（`573 passed, 7 skipped`），`1e9d9ef` 把 `/usr/lib/postgresql/17/bin` 写进 `GITHUB_PATH` 后第二次 run 为 `580 passed`、零跳过。**未做**：WAL 归档与 PITR、对象存储与异地副本、备份加密与密钥托管、演练的定期执行与告警、按部署目标确定的 RPO/RTO、多租户选择性恢复。交接注意：`aftercare-backup` 要求 PATH 里有 major 不低的 `pg_dump`/`pg_restore`；恢复后要跑 `reconcile`，行数一致不等于业务状态正确；保留策略目前没有异地兜底，`--apply` 不要进定时任务。
- 2026-09-15 / D-03 池指标与容量定标（完成并推送，提交 `e3bedbc`，CI run [34965278041](https://github.com/yushui2022/Aftercare-Agent/actions/runs/34965278041) 为 `success`）：起因是 ADR-0006 留下的两条——池有界但看不见、`max_size` 是默认值而不是结论。做法：`Database.stats()` 返回只含整数的 `PoolStats` 冻结快照（有一条测试逐字段断言是整数，因此快照里装不下租户/工单/语句/DSN）；`observability.py` 加 `Metrics` 接缝（`InMemoryMetrics`、默认的 `LoggingMetrics` 一行 JSON、可选 OTel bridge，可选导入改用 `importlib`），`persistence/pool_metrics.py` 的 `PoolStatsSampler` 在 API 与 Worker 里每 10 s 发布 6 个 gauge 与 7 个 counter，唯一标签 `component`，**采样失败只停线程、不失败进程**（与租约心跳相反，理由写在 ADR-0007）；新增 `aftercare-capacity` 探针（三种并发形状、`--service-time-ms` 建模占槽时长、`--min-size` 建模预热、p95 预算判据、退出码 0/1、`--json` 报告）。**实测**（本机、临时 PostgreSQL 16.13、16 并发、10 ms 单元、50 ms p95 预算）：懒增长时 1–32 没有尺寸达标（steady p95 124–262 ms，上限 8→32 只把 in_use 峰值从 8 推到 10）；预热 16 后 `max_size=16` 达标（p95 31–39 ms、池排队 0 ms）且 32 无差别；纯 `SELECT 1` 对照下 1/8/16 都达标 25 ms。验证：真实 PostgreSQL 全量 `541 passed`（34.06 s）、离线 `427 passed, 114 skipped`、ruff/严格 mypy（118 文件）/compose config 通过；CI `541 passed, 98 warnings in 16.50s`。边界与未完成：只有一台开发机、没有容器或 CI 容量结论；池峰值是采样值；OTel 导出器仍未配置（C-03）；面向业务的队列年龄还没进观测。下一步见第 7 节。本轮未部署、未做真实业务动作。
- 2026-09-15 / D-04 有界连接池（完成并推送，提交 `ea8280a` 与修复提交 `116f904`，CI 见第 5、6 节）：起因是每个工作单元都付一次建连——本机 `psycopg.connect` p50 115.2 ms / 最大 215.3 ms，而同一测量里续期事务 p50 0.8 ms；同一批用例本机 79.0 s、CI Linux 14.7 s。A2-02 已经因为把建连放进租约截止预算而失败过（200 ms 租约下单 tick 只剩约 133 ms，单跑 20 次失败 11 次），`PostgresEventTail` 每轮轮询同样走一次 `transaction()`，300 ms 尾部窗口在慢建连主机上会漏事件（观察到 1 次、未稳定复现）。**决策：只启用有界连接池，其余候选（Broker、暖池、ACP、长期记忆、额外供应商）明确保留并写下触发条件**，见 ADR-0006；这才是 D-04 要求的“每个新增组件有需要、方案、代价、迁移和验收”。做法：`Database` 默认带池（min 1 / max 8 / 借用超时 5 s，环境变量可覆盖，`direct()` 保留无池对照），池只在首次借用或 `startup()` 时打开（构造不产生线程与连接），借用超时映射 `ContractViolation(RETRYABLE)`，借用前做一次 `check_connection`（实测 +0.09 ms）以便数据库重启时丢弃坏连接；心跳从“自己建、自己关”改为“从池借、用完归还（`open()`/`release()`）”，仍占一个槽，所以 `max_size` 默认高于 `min_size`；API 的 startup 先 `database.startup()` 再迁移，`create_default_app` 关闭自己建的池，CLI 与 demo 在 `try/finally` 关池。**实测**：25 个连续事务本机只落到 1 条物理连接（上限设 1 的形态已固化为回归用例；默认上限下紧循环里归还与借用竞争仍可能多开一条，CI 上观察到过）、本机稳态 0.47 ms/事务（对照 114.8 ms/事务）；同一批 509 个既有用例本机 78.98 s → 30.35 s；`PostgresEventTail` 每轮轮询复用同一后端并有回归用例。验证：`ruff format --check .`、`ruff check .`、严格 `mypy`（113 文件）通过，离线全量 `407 passed, 110 skipped`，真实 PostgreSQL 16.13 重建库后 `517 passed in 33.20s`。边界与未完成：`max_size` 未按压测定标、池指标未接入观测、池不隔离会话状态（因此禁止在池化连接上 `SET`/`LISTEN`）、每个进程多一个池与其维护线程；下一步 D-03。本轮未部署、未做真实业务动作。

- 2026-09-15 / D-01-07 完成并推送：起因是 D-01-06 的已知边界——工单队列是数据面入口，每行都来自活动 `CaseGrant`，所以一个不参与任何工单的访问管理员打开工作台看到空队列，手上有 `grant:admin` 却无法移交任何工单。**决策：只放宽控制面，不放宽数据面。** 把 `grant:read` 加进 `list_accessible` 的判定也能让管理员看到全部工单，但那会让同一个 scope 同时打开 Case 内容、Run、Review、Approval 与事件流，等价于“租户管理员对所有客户数据的常驻读权”，与逐工单 grant 收敛权限的立场相反，因此改为新增只返回标识与进度的 `GET /v1/administration/cases`。实现：清单投影用新闭集 `ADMINISTRATION_PERMISSIONS = {grant:read, grant:admin}`，只报调用者自己的管理 scope，绝不复用逐 Case 投影（不复用是必须的——报出 `case:read` 会让工作台去调用下一个请求必然拒绝的内容路由）；`CaseGrantListResponse` 新增 `delegable` 与 `can_administer` 描述**调用者**而不是 Case，因为 Case 投影是与 grant 的交集，在该 Case 上没有 grant 的租户管理员在投影里根本看不到 `grant:admin`，客户端无从判断表单该不该出现；`CaseRepository.list_for_tenant()` 的合法调用者扩为两类并把理由写进 docstring，仍接受只收窄的 `case_ids` 上界；决策与代价写入 [ADR-0005](decisions/0005-access-administration-discovery.md)。工作台拆成“工单队列”（数据面）与“本租户其他工单”（控制面）：控制面行标注“仅可管理访问”，选中时只渲染说明 + 授权面板、不调用任何内容路由，读不到清单（`403`）时该区块静默消失；两侧游标独立，同一工单重复出现时以队列行为准（只有它携带真正的 Case 权限）。**验证：** 离线 `407 passed, 102 skipped`；重建后的临时 PostgreSQL 16.13 全量 `509 passed`（含新增 5 项集成测试，覆盖发现他人未参与的工单、非 `grant:read` 者 `403`、租户边界、token `case_ids` 只收窄、keyset 分页）；`ruff format --check`/`ruff check`/严格 `mypy` 通过；前端 43 项单测 + `tsc --noEmit` + 生产构建通过。真实浏览器端到端见本记录末尾（两轮 Playwright 冒烟）。**已知边界（勿谎报）：** 控制面清单是元数据暴露面，`grant:read` 必须只发给真正的访问管理员；租户级授权总览（谁有哪些工单的权限）仍未实现；RLS 与真实 IdP 演练仍未做。**真实浏览器验证抓出并修掉两个缺陷（提交 `95384c3`）：** 一是选中控制面行时工作台仍订阅了该工单的事件流——事件是内容路由，于是每次选中都发一个必然 `403` 的请求（改动本意是"不调用任何内容路由"，实际只做到了不渲染面板）；二是清单区块在"已加载行都在队列里、但还有下一页"时复用了队列的空态文案。验证方式：进程内构造 app（verifier 用 seam double、合成身份关闭）跑真实 HTTP + 真实 PostgreSQL，用 Playwright 的 `page.route` 注入 `Authorization`（工作台只认合成身份输入框，所以只能在浏览器层注入）——真实 Bearer 管理员 10 项断言全过、合成身份回归 9 项全过，脚本在 `G:\DevCache\Temp\aftercare-agent`（`serve_admin_smoke.py`、`ui_smoke_admin.py`、`ui_smoke_d0107.py`，未提交进仓库）。**已知边界（勿谎报）：** 合成身份的数据面队列本身就是租户级（`list_cases` 对它走 `list_for_tenant`），因此本地合成身份下控制面清单与队列重合、工作台里"本租户其他工单"区块不会出现——这个区块只有在真实 Bearer 身份下才可见，本地演示看不到它并非缺陷。交付提交 `f04cc09` 与修复提交 `95384c3` 已推送，CI run [34958850865](https://github.com/yushui2022/Aftercare-Agent/actions/runs/34958850865) 与 [34959917168](https://github.com/yushui2022/Aftercare-Agent/actions/runs/34959917168) 均为 `success`（`postgres:17` 上各 `509 passed`）。**会话内仍运行、接手前需核验或清理的进程：** 临时 PostgreSQL 16.13 集群（`G:\DevCache\Temp\aftercare-pg\data`，端口 55450，PID 155740）、合成身份 API（`127.0.0.1:8000`，PID 119856，pid 文件不保证有效）、Vite 开发服务器（`127.0.0.1:5173`，PID 156096）；本机 5432 的既有 Windows 服务未被触碰。本轮未部署、未做真实业务动作。

- 2026-09-15 / A2-02 心跳续期延迟预算修正（完成并推送）：`main` 在 `01ed4f6` 之后变红，失败项是 `test_worker_heartbeat_keeps_long_slice_lease_alive`，而该提交只改了 `web/` 与文档。本轮先量化再动手：`psycopg.connect` p50 115.2 ms / max 215.3 ms，而续期事务 p50 0.8 ms；200 ms 租约 + 30 ms 间隔下单 tick 只剩约 133 ms 预算，本机连续单跑 20 次失败 11 次——根因是把建连开销放进了租约的截止预算，不是负载抖动，也不是测试写法细节。修复：心跳线程独占一条连接（新接缝 `Database.open()`，跨 tick 复用、线程退出前关闭、线程外不共享）、首个续期立即执行、按绝对截止时间排程；异常仍立即 stop 并 fail-closed（不重连，连接断开与租约被抢占同样处理）。测试侧把假 Database 收紧为只认 `open()` 并新增 2 项回归，长切片用例改为 1 s 租约 / 100 ms 间隔 / 1.5 s 切片，理由写进 docstring。修复后该用例 20/20 通过，重建后的干净临时 PostgreSQL 全量 `504 passed`。**已知边界：`PostgresEventTail` 每轮轮询仍付一次建连，300 ms 尾部窗口在慢建连主机上会漏事件（本轮观察到 1 次、隔离对照两次各 8/8 通过，故不改），正确修法是 D-04 连接池，已写入计划 D-04 行。** 提交 `230e518` 已推送到 `origin/main`，CI run [34957131285](https://github.com/yushui2022/Aftercare-Agent/actions/runs/34957131285) 为 `success`（`postgres:17` 上 `504 passed, 74 warnings in 14.72s`，与本机干净临时库的 `504 passed` 一致），失败的 run 34955596978 因此被覆盖，`main` 恢复绿色。下一步按第 7 节推进（首选 D-04）。本轮未部署、未做真实业务动作。
- 2026-09-15 / D-01-06 完成（待推送）：把上一轮的授权管理面接进 `web/` 工作台——新增 `web/src/grants.ts`（服务端可授予闭集的镜像、委派上限、有效期与 revision 校验）与 `GrantPanel.tsx`（列出/授予/替换/撤销，含"当前身份没有 X，无法授出"提示），`App.tsx` 在服务端拒绝时静默隐藏面板（真实身份的 Case 投影里永远不含 `grant:read`，不能用投影判断能否管理）；顺带修复工作台实时时间线被新事件清空的既有缺陷（`reduce` 把下标当 `cap`）。前端 38 项单测 + 构建通过，并用 Playwright 对真实 API + 真实 PostgreSQL 走完授予→撤销→替换全流程（8 项断言）。**已知边界：工单队列只列出已有授权的工单，真实访问管理员暂时只能移交自己参与处理的工单；跨工单发现需要服务端新入口，列为下一项候选。** 下一步：推送并核对 CI，然后按第 7 节推进。本轮未部署、未做真实业务动作。
- 2026-09-15 / D-01-05 完成并推送：开放 Case 授权管理面（`POST /v1/cases/{case_id}/grants` 与 `.../grants/{subject_id}/revoke` 需租户级 `grant:admin`，`GET .../grants` 需 `grant:read`；可授予权限闭集排除 `case:create`/`grant:*`，不能授出自己没有的权限，替换/撤销需 `expected_revision`，每次变更同事务写 `case_grant.granted`/`case_grant.revoked` 与完整快照），新增 25 项离线回归与 10 项 PostgreSQL 回归；新增 [ADR-0004](decisions/0004-case-grant-administration.md) 显式替代 ADR-0003 中“本轮不开放管理 HTTP”那一句。本机首次用独立临时 PostgreSQL 16 集群（端口 55450，不碰既有 5432）跑通完整套件：`501 passed, 1 failed`，唯一失败是既有的心跳负载抖动，第 6 节给出干净基线复现证据；跑法已写入 `docs/development.md`。提交 `f7818d5` 已推送到 `origin/main`，CI run [34953807687](https://github.com/yushui2022/Aftercare-Agent/actions/runs/34953807687) 为 `success`（`postgres:17` 上 `502 passed`）。下一步按第 7 节推进真实 IdP 演练/权限映射与 RLS。本轮未部署、未做真实业务动作。
- 2026-09-15 / D-01-04 完成并推送：新增 RFC 7662 撤销判定（`auth/introspection.py`、`auth/guard.py`、`TokenVerifier` Protocol、`create_app`/`create_default_app` 接线与关机钩子）与 72 项离线回归；提交 `cd6ba9b`、`e3a7586` 已推送到 `origin/main`，CI run [34951354092](https://github.com/yushui2022/Aftercare-Agent/actions/runs/34951354092) 为 `success`，真实 PostgreSQL 17 上 `467 passed, 52 warnings`。本轮未部署、未发布包、未连接生产数据库、未调用真实 IdP。**本机 Docker 守护进程仍未就绪，本轮本机没有执行过任何集成测试（87 项全部跳过）**，数据库侧结论完全来自 CI；下一位接手者若在本机改动认证/数据库边界，必须预期"本机全绿但 CI 可能失败"。下一项候选：D-01 真实 IdP 演练/RLS/权限管理面、A3-04 把 Harness 等待与审批分支纳入评测、B-02-03 供应商回执核对、C-02 真实沙箱后端。

- 2026-09-14 / 远端 CI 修复：定位到 `main` 上连续失败的根因是 `uv python install 3.13.15` 在 uv 0.9.26 内置索引（止于 3.13.11）中找不到该补丁，改为 CI job 级 `UV_PYTHON_DOWNLOADS_JSON_URL` 指向固定提交元数据，并同步 `docs/development.md`。本轮未提交、未推送，**没有绿色 run 证据**，远端 CI 仍不能视为已修复完成。下一步：提交并推送后在 GitHub 确认转绿，之后才考虑加 CI 徽章；LICENSE/SECURITY.md/Issue 模板等开源前置项仍未处理。

- 2026-09-13 / A3-03-b 完成：工作台接入 SSE 实时订阅（游标续订、去重封顶、指数退避、`401/403` 终止），新增 13 项前端单测；`pnpm test`/`tsc --noEmit`/`vite build` 通过，真实后端 `follow` 参数验收通过。本轮未提交、未推送、未部署。前端新增开发依赖 `vitest@5`（仅测试，不进生产包）。下一项候选：D-01 真实 IdP/撤销演练、A3-04 完整业务评测、B-02-03 供应商回执核对、C-01 真实沙箱后端。

- 2026-09-13 / A3-03-a 完成：补齐操作员工单发现 API（可访问工单列表、工单详情、Review/Approval 列表）与 `web/` 最小 React 工作台。临时 PostgreSQL 17 全量 `362 passed`、无数据库离线 `279 passed, 83 skipped`，Ruff/format/严格 mypy 与前端 `tsc --noEmit`/`vite build` 通过，合成身份经 Vite 代理端到端冒烟成功。本轮未提交、未推送、未部署、未连接生产数据库。会话内仍保留临时 PostgreSQL 容器 `aftercare-queue-pg`（宿主机 55440）、本地 API（127.0.0.1:8000）与 Vite 开发服务器（127.0.0.1:5173）用于预览，接手前需核验或清理。下一项候选：A3-03 高吞吐 live tail、D-01 真实 IdP/撤销演练、B-02-03 供应商回执核对、C-01 真实沙箱后端。

- 2026-09-07 / 本地交付提交：用户明确授权固化 DOC-001/A0-01/A0-02；提交前 206 项回归、类型/格式与范围复核通过，34 个项目文件随本页一起保存。实际提交编号和工作区状态以 Git 日志核验；不推送，不包含 EGM 未提交材料，下一项仍为 A0-03。

- 2026-09-09 / A0-03 完成：新增 12 个合成案件、固定期望、离线确定性 runner、跨进程 digest 回归与评测说明；完整回归 210 passed。尚未提交、推送或部署；下一项为 A1-01。

- 2026-09-09 / A1-01 开始：新增显式 PostgreSQL 迁移、Run 租约/fencing 和检查点执行权校验；初次静态检查通过，未配置 `DATABASE_URL` 时集成测试跳过。

- 2026-09-09 / A1-01 扩展：补齐受理幂等重放/改参冲突、Case version compare-and-swap、Session/Step/Attempt Repository 与受保护状态转换；静态检查通过，完整回归 210 passed、4 项 PG 集成测试因未配置 `DATABASE_URL` 跳过。尚未提交、推送或部署。

- 2026-09-09 / A1-01 验收：使用临时 PostgreSQL 17 Docker 容器（端口 55433，测试后已移除）运行完整仓库回归，`215 passed`；其中 5 项真实 PG 测试覆盖迁移、幂等重放/改参冲突、Case version CAS、检查点执行权、过期接管和双连接竞争。容器数据为合成测试数据，未连接现有业务数据库。

- 2026-09-12 / A1-02 完成：新增 FastAPI `POST /v1/cases`、`GET /v1/cases/{case_id}/runs/{run_id}`、显式合成身份和 API 错误映射；临时 PostgreSQL 端到端 7 项测试通过。完整离线回归 `215 passed, 7 skipped`；尚未提交、推送或部署，下一项为 A1-03 Fake Harness。

- 2026-09-12 / A1-03 完成：新增数据库无关 FakePlanner/Harness，固定只读调查链，执行模型/工具预算和检查点恢复；3 项专测、Ruff 与严格 mypy 通过。尚未提交、推送或部署，下一项为 A1-04 Compose/CI。

- 2026-09-12 / A1-03 回归复验：在无 `DATABASE_URL` 的环境执行完整 `pytest -q`，`218 passed, 10 skipped`；`ruff format --check`、`ruff check`、严格 `mypy` 全通过。10 项跳过均为需要显式 PostgreSQL 的集成测试。

- 2026-09-12 / A1-04 开始：新增 `deploy/Dockerfile`、PostgreSQL + API 开发 Compose、Compose 说明和 GitHub Actions 真实 PostgreSQL CI；`docker compose config`、镜像构建和容器内 API smoke（OpenAPI 返回 200）通过。第一次构建的 Debian `502` 已通过 APT 重试配置恢复；宿主默认 `8000` 被占用时改用容器内网络验收，临时容器/卷已清理。

- 2026-09-12 / A1-04 Worker 增量：新增 `runtime.worker.run_once()`、`run_next()` 和 `runtime.worker_cli`，实现 `SKIP LOCKED` 领取 READY Run、claim → checkpoint 恢复 → 事务外 Fake Harness → fencing 校验保存 → `READY/COMPLETED` 转换；真实 PostgreSQL 下 Worker 专测 3 项、既有持久化专测 5 项全部通过。长运行调度、远端 CI、提交或推送仍未完成。

- 2026-09-12 / A1-04 最终复验：Worker profile 镜像重新构建成功（包含 CLI）；全量离线回归 `218 passed, 10 skipped`，`git diff --check` 通过。默认与 `worker` profile 的 Compose 配置均可解析，容器内 API smoke 已验证；长运行调度和远端 CI 仍待完成。

- 2026-09-12 / A2-01 事件增量：新增 `aftercare_outbox`、`aftercare_inbox`、消费者应用记录及 `EventRepository`；真实 PostgreSQL 下事件专测 2 项与持久化既有 8 项共 10 项通过。随后新增 Wait/wakeup 迁移、`WaitRepository` 与 3 项真实 PostgreSQL 生命周期测试；外部 Broker、发布器和 gap buffer 尚未实现。

- 2026-09-12 / A2-01 Wait 复验：临时 PostgreSQL 17（127.0.0.1:55437）执行 `pytest -q tests/persistence/test_waits.py`，`3 passed`；覆盖 PENDING 早到回执在激活时消费、ACTIVE 回复与超时的单一 wakeup、旧代次不推进新等待。测试后容器已移除，未连接业务数据库。离线全量回归 `214 passed, 2 skipped`，Ruff、格式检查和严格 mypy 通过。
- 2026-09-12 / A2-02 Worker/Outbox 增量：新增独立连接租约心跳、可停止常驻轮询、CLI daemon 配置，以及 Outbox `PENDING→CLAIMED→ACKED` 投递租约、attempt fencing、退避重试和 FakePublisher。临时 PostgreSQL 全量回归 `246 passed`，Ruff/格式/严格 mypy/`uv lock --check` 全通过。待补双 Worker 故障注入、gap buffer 和远端 CI。

- 2026-09-12 / 数据库启动安全增量：`migrate()` 增加事务级 advisory lock 与历史迁移 SHA-256 校验，新增回归拒绝 SQL 漂移。临时 PostgreSQL 17 全量回归 `246 passed`、Ruff、格式、严格 mypy 和 `uv lock --check` 全通过；测试容器已移除。

- 2026-09-12 / A2-02 双 Worker 验收：新增两个独立数据库连接并发执行同一 Run 的集成测试，结果严格为 1 个完成、1 个被拒绝；PostgreSQL Worker 专测 `6 passed`，证明 Run fencing 不仅存在于单独 claim 测试。

- 2026-09-12 / A2-01/B-01 增量复验：新增 `ProjectionRepository` 的乱序 gap buffer/连续 drain 与 Action Ledger 的跨 Case business key 幂等、UNKNOWN 对账和 fencing。临时 PostgreSQL 全量回归 `255 passed`；新增 projection 4 项、Action 4 项均通过。SSE、支付聚合审批和真实供应商仍未接入。

- 2026-09-07 / A0-02 完成：运行时/调查 v1 契约落到纯代码与正反例；206 项本地及独立 wheel 回归、类型/格式和 schema 验证通过。补正文摘要幂等、JSON 非有限值与嵌套测试打包问题。保留 WinError 32 的失败和替代构建记录，未提交/推送/部署；下一项候选 A0-03。

- 2026-09-07 / A0-02 开始：核对 HEAD 与未提交工作区，保留 A0-01 和设计文档；按公共类型/运行时/调查分文件实施及独立复核，不提交、推送或部署。

- 2026-09-07 / A0-01 完成：固定 CPython 3.13.15、EGM 完整 Git 提交与 uv.lock；类型/格式、9 项回归、冷安装、sdist→wheel、仓库外 9 项回归、文件来源与文档检查通过。适配器业务语义保持，未改 EGM 源码。本轮未提交、推送或部署；下一项候选 A0-02。

- 2026-09-07 / A0-01 开始：用户授权执行并维护文档；主 Agent 限定本轮为 Python 包与回归基础，独立只读复核打包/依赖边界。尚未安装或完成验收，不提交/推送。

- 2026-09-07 / DOC-001 完成：建立持久工程记录，明确 Python/TypeScript/SQL 分工、A0–A3/B/C/D 依赖和恢复协议；文档检查与只读复核通过。本轮未实现业务代码、提交、推送或部署；下一项候选为 A0-01。

只保留近期有用记录，不粘贴聊天全文。较长历史依靠 Git 和版本化验收文件。压缩/突发中断后的第一动作是核对文件与事实，不是无条件重做上一条任务。
