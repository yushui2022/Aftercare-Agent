# 当前工程状态与接手点

格式版本：1。最后核验日期：2026-09-12（Asia/Shanghai）。记录者：本轮主 Agent。

本文件是进度与交接的唯一台账，不是实际代码/测试的替代证据，也不是自动执行授权。先读根 [AGENTS.md](../AGENTS.md)，任务定义见 [工程执行计划](engineering-plan.md)。

## 1. 当前工作位置

| 字段 | 值 |
|---|---|
| 本轮请求范围 | 用户授权继续完善项目；本轮把 A2-03 durable queue 接入 Worker，并完成跨租户公平、槽压力与失联回收验收；当前已授权推送已验证提交 |
| 当前任务 | A3/C/D 运行控制面与生产边界进行中 |
| 当前阶段 | A1-03 DONE；A1-04 最小 Worker/Compose 已有；A2-01 Wait/Inbox/Outbox 与 gap buffer 基础已落地；A2-02 心跳/常驻轮询已落地；B-01 Action Ledger 最小闭环已落地 |
| 下一项代码候选 | A3-03：broker-neutral live tail/工作台；A3-02：调查 EGM schema 与真实模型接线；C-02：选定真实沙箱后端 |
| 活跃实现任务 | broker-neutral live tail/工作台、调查 EGM schema/真实模型接线和真实沙箱后端选型；后续接入业务 Harness 与真实 provider |
| 本轮外部行为 | 本轮新增适配器仅做离线解析，不调用模型、真实业务动作或生产部署；提交推送状态以 Git 日志为准 |

## 2. 核验过的源码基线

| 仓库 | 已核验源码 HEAD | 用途 |
|---|---|---|
| Aftercare-Agent | d4a20c905343c4a84cd92b9687f9f5bb1c298d71 | DOC-001/A0 实施前的历史基线；工程增量由包含本页的交付提交固化，实际编号以 Git 日志为准 |
| Evidence-Gated-Memory | 9c7c5d196f8e703fdc7c70546cff0dc94cc78dcd | 源码 0.6.0、嵌入式应用层与 PostgreSQL；已固定在 Aftercare 依赖中 |

这些是核验时的源码基线，不要求文档为了指向包含自身的提交而无限修改。本轮没有检查远端分支或远端 CI；不要把旧交接中“远端与本地一致”当作当前核验。

本机位置：G:\Projects\Aftercare-Agent；EGM 相邻仓库 G:\Projects\Evidence-Gated-Memory。脚本应使用仓库相对路径或显式配置，不把本机布局当其他贡献者的强制前提。

## 3. 已有与没有的东西

已存在：aftercare_agent/evidence.py 及其回归；EGM 的公共应用层、PostgreSQL 后端与 join；架构、ADR 和接入资料。工作区新增可安装的 Aftercare 0.1.0a0：pyproject.toml、uv.lock、.python-version、py.typed，以及 tests/test_package_contract.py 和[开发指南](development.md)。A0-03 新增 `evals/` 合成案件目录、固定期望、确定性 runner、12 案件回归和[评测说明](evals.md)。

尚未存在：完整业务 Harness、支付聚合/审批、真实供应商连接器、前端、沙箱接线、live SSE tail 和生产调度体系。调查 EGM 适配器和 PostgreSQL 观察账本已建立代码边界，但真实 EGM 调查 schema 尚未固定。当前已有 Fake Harness、一次性/常驻 Worker、Wait/Inbox/Outbox publisher、gap buffer、Action Ledger 和跨实例 admission 最小闭环，但仍不是完整执行服务。A1-02 已有最小 API，但仅支持显式合成身份测试模式；OIDC claims 转换边界已实现，真实认证仍未接入。不要输出不存在的完整服务启动命令。

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
| A1-02 | DONE | 新增 FastAPI 受理/读取接口、显式合成身份边界、API 单测；临时 PostgreSQL 端到端 7 项测试通过 |
| A1-03 | DONE | 新增数据库无关 FakePlanner/Harness；固定只读工具链、预算消耗、检查点 JSON round-trip 与跨 scope 拒绝通过 |
| A1-04 | IN PROGRESS | 最小 Worker/CLI、`SKIP LOCKED` READY Run 领取、`deploy/Dockerfile`、开发 Compose 和 GitHub Actions 已新增；长运行基础已移入 A2-02，仍缺远端 CI 和完整启动验收 |
| A2-01 | IN PROGRESS | 新增 Outbox、Inbox、消费者应用记录、Wait/wakeup、gap buffer 与 `ProjectionRepository`；真实 PostgreSQL 下事件/投影相关测试通过；SSE 已有有界持久回放，live tail/工作台待补 |
| A2-02 | IN PROGRESS | `LeaseHeartbeat`、可停止 `run_daemon()`、Outbox publisher 租约/重试与故障注入已实现；锁超时、失联接管和旧 Worker fencing 已在真实 PostgreSQL 验证；完整重启矩阵和远端 CI 仍待补齐 |
| A2-03 | DONE | PostgreSQL 全局/租户执行槽、slot 租约心跳、按 Run 幂等重试预算，以及 007/008 durable queue、Run 状态同步触发器、tenant cursor 轮转和过期 IN_FLIGHT 回收已接入；真实 PostgreSQL 通过跨租户/回收/槽竞争测试；这是基础骨架，权重校准与生产压测仍未完成 |
| B-01 | IN PROGRESS | `ActionIntent`、Action Ledger、跨 Case business key 幂等、UNKNOWN/CONFIRMED/FAILED 与 claim fencing 已实现；真实 PostgreSQL Action 测试通过；支付聚合、审批和真实供应商对账仍待实现 |
| A3-01 | IN PROGRESS | 新增严格 Responses wire parser、`ResponsesAdapter` 和整数 token/cost budget：校验原生响应、usage、函数参数、工具白名单、托管工具事件、provider 错误脱敏与超预算拒绝；离线回归通过；尚未发起真实 provider 请求 |
| A3-02 | IN PROGRESS | 新增 `InvestigationEvidenceAdapter` 与 PostgreSQL 观察账本：可信连接器规范化写入、来源事件去重/撤回/重载、模型仅提交 `InvestigationProposal`、完整授权观察集确定性评估与默认禁用长期记忆；真实调查 EGM schema、来源认证和模型接线仍待实现 |
| A3-03 | IN PROGRESS | 新增 case-scoped SSE replay、`Last-Event-ID`/after 游标、持久事件分页和有界 PostgreSQL polling tail；高吞吐 live broker tail、React 工作台和生产认证仍待实现 |
| C-01 | IN PROGRESS | 新增非安全边界 `FakeSandboxProvider`：allocation 幂等、fencing lease、资源/产物预算、过期回收和销毁确认前保留容量；3 项离线测试通过；真实 E2B/Kubernetes 后端未接入 |
| D-01 | IN PROGRESS | 新增 provider-neutral OIDC claims→`AuthContext` 边界：issuer/audience/时间/tenant/Case scope 校验；9 项离线认证测试通过；JWT/JWKS 验签与实际 IdP 尚未接入 |
| C-03 | IN PROGRESS | 新增 provider-neutral Tracer、InMemoryTracer、可选 OTel bridge，并为 Outbox publish 埋点；离线回归通过；Exporter、采样/留存和生产监控尚未配置 |

## 5. 工作区与提交边界

本次待提交范围为 A2-03：PostgreSQL durable execution queue、跨租户 cursor 调度、过期 IN_FLIGHT 回收、Worker/CLI 接线、测试和文档；未包含 .venv、缓存、dist、临时数据或 EGM 仓库改动。

EGM 本轮观察到 README.md 修改，assets/egm-roman-banner.png、assets/egm-roman-banner.prompt.md、docs/benchmark-history.md 未跟踪。这些不是本轮工程文件任务的产物，未修改、暂存或回滚。不能使用“清理工作区”删除它们，也不能把它们默默打入固定提交依赖。

提交授权：本轮用户明确要求提交并推送；本轮 A2-03 增量在本地 PostgreSQL 验收后提交并核验远端状态。部署：本轮没有。生产数据/真实业务操作：本轮没有。远端 CI 尚未核验；没有发布 Python 包；项目许可证仍待用户确定。

本地提交成功后，记录可随该提交进入本仓库的新 worktree；尚未推送时，另一台机器或 GitHub 不能自动取得它。跨机器交接需另行授权推送或明确的提交传递方式，不把本地提交等同远端同步。

## 6. 验证台账


- 2026-09-12 / A2-03 durable scheduler：新增 008 Run 状态同步触发器和队列回填；`run_next(tenant_id=None)` 使用持久 tenant cursor 轮转，跳过租户准入已满的队列，并按 Run 过期 lease 回收 IN_FLIGHT。临时 PostgreSQL 17 全量回归 `285 passed`；新增跨租户轮转、租户限额跳过和失联回收测试；Ruff、格式、严格 mypy、wheel 构建和 `git diff --check` 通过。测试容器已移除，未连接生产数据库。

- 2026-09-12 / A3-03 tail 增量：新增 broker-neutral `PostgresEventTail` 和 `follow=true` SSE 参数；每轮查询使用短事务，最长等待 60 秒，保留 `Last-Event-ID`/after 游标；真实 PostgreSQL SSE/tail 测试 `4 passed`，完整 PG 回归 `288 passed`。Kafka/NATS/Redis live adapter、React 工作台、真实 OIDC 与生产压测仍未实现。

- 2026-09-12 / A3-02 观察账本增量：新增迁移 009 与 `InvestigationObservationRepository`，实现工单范围、来源事件/evidence ID 双重幂等、完整 JSON 保存、撤回持久化和重启后完整快照评估；真实 PostgreSQL 全量回归 `290 passed`，静态检查通过。真实来源认证和 EGM 调查 schema 仍未完成。

- 2026-09-12 / A1-04 Compose 启动边界：API 增加 `/readyz` healthcheck，Worker profile 等待 API readiness（从而等待迁移完成），补充 daemon/跨租户队列环境参数；`docker compose config` 已静态核对，未做生产部署。

### A1-02：最小 API 与认证边界

2026-09-12，临时 PostgreSQL 17 Docker 容器和 FastAPI TestClient；合成身份仅在测试 App 显式开启，容器测试后已移除。

| 验证 | 实际结果 |
|---|---|
| 离线边界 | 合成身份默认关闭；OpenCaseInput 拒绝 authority 字段；默认 App 无数据库配置仍可安全导入 |
| API 端到端 | 受理、同键重放、同键改参 `409`、跨租户读取 `403`、生产模式合成身份 `401`：7 项测试通过 |
| 静态与全量回归 | Ruff、严格 mypy；A1-02 阶段为 `215 passed, 7 skipped`（无 DATABASE_URL 的集成测试跳过） |

当前 API 仍只通过显式合成身份入口做请求认证；OIDC claims 转换边界已实现但尚未接入真实 JWKS/IdP；CaseGrant 数据库授权、live 消息接入和真实业务动作仍未实现。SSE 目前仅提供有界持久回放。FastAPI 的 TestClient 对当前 Starlette 版本产生弃用警告，不影响测试结果，后续可在依赖升级时切换 `httpx2`。

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

未验证/未实现：真实 OIDC/JWKS 接入、选定 broker 的 live tail、配置版本仓库、模型效果、真实调查 EGM schema/来源认证、E2B/Kubernetes 后端、Linux/容器试点、远端 CI 结果、真实供应商和生产压测。A1/A2/A3 必须继续补足，不能用本轮测试替代。

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

当前推进 A3/C/D 运行控制面：先将有界 SSE tail 替换/接入选定 broker 与工作台，再固定调查 EGM schema/真实模型接线，并选择真实沙箱后端；OIDC claims 边界已实现但真实 JWKS/IdP、完整业务 Harness 和生产连接器仍未实现。

尚待决定但不阻塞离线骨架：真实模型 ID/预算、商家渠道和身份提供者、沙箱/对象存储后端与地域、RPO/RTO 和生产负载目标。每项的决策阶段已列在技术栈和执行计划中。无业务凭证不阻塞 Fake 流程；真实接入缺授权时必须停止该分支。

关键风险：本轮包依赖升级尚未重跑 EGM PostgreSQL 全量验收，A1 必须补足；新旧路线图的阶段名称必须一致；不能把 fencing 延后为性能优化；A 阶段通知不得真实外发；同进程不等于同事务；未提交图稿和其他仓库改动不属于本任务。

## 8. 最近交接记录

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
