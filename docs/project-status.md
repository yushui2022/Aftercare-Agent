# 当前工程状态与接手点

格式版本：1。最后核验日期：2026-09-15（Asia/Shanghai）。记录者：本轮主 Agent。

本文件是进度与交接的唯一台账，不是实际代码/测试的替代证据，也不是自动执行授权。先读根 [AGENTS.md](../AGENTS.md)，任务定义见 [工程执行计划](engineering-plan.md)。

## 1. 当前工作位置

| 字段 | 值 |
|---|---|
| 本轮请求范围 | 用户设定目标：按路线图持续把项目推向企业级，并在本轮明确授权提交/推送到 GitHub。本轮领取 D-01 的 Case 授权管理面切片（移交/授权/撤销）；不调用真实 IdP、不连接生产数据库、不做外部业务动作 |
| 当前任务 | D-01-05（Case 授权管理面：租户级 grant scope + 可授予闭集/委派上限 + 同事务审计事件） |
| 当前阶段 | A1-03 DONE；A1-04 最小 Worker/Compose 已有；A2-01 Wait/Inbox/Outbox 与 gap buffer 基础已落地；A2-02 心跳/常驻轮询已落地；B-01 Action Ledger 最小闭环已落地；A3-03-a/b 工作台与 SSE 已完成；D-01-04 撤销判定与 D-01-05 授权管理面已落地 |
| 下一项代码候选 | D-01：真实 IdP 演练与权限映射、RLS；A3-04：Harness 等待与审批分支纳入评测；B-02-03：供应商回执核对；C-02：真实沙箱后端；随后补生产准入与容量验证 |
| 活跃实现任务 | 本轮新增 `auth/grants.py` 的可授予闭集 `GRANTABLE_CASE_PERMISSIONS` 与 `authorize_case_grant()`、`persistence/grant_events.py`（授权审计事件），`persistence/case_grants.py` 增加 `list_for_case()` 并在 grant/revoke 同事务追写事件，`api/app.py` 新增三个管理路由（`_administered_case()` 按租户级 scope 判定，不解析调用者自己的 grant）；真实 IdP 演练、RLS 与管理 UI 仍未实现 |
| 本轮外部行为 | 本轮只新增授权管理面与其离线/PostgreSQL 回归；不调用真实 IdP 或模型、不做真实业务动作或生产部署。本机首次用独立临时 PostgreSQL 16 集群（G:\DevCache\Temp\aftercare-pg，端口 55450，不影响既有 5432 服务）跑通完整套件（`502 passed`），推送后 CI 在 `postgres:17` 上同样 `502 passed`（run 34953807687） |

## 2. 核验过的源码基线

| 仓库 | 已核验源码 HEAD | 用途 |
|---|---|---|
| Aftercare-Agent | f5c673ab7c17a77a9f5608d846f407def1b17b2a | 本轮 D-01-05 授权管理面切片的提交前基线（D-01-04 已推送并可复现）；交付提交编号以 Git 日志为准 |
| Evidence-Gated-Memory | 9c7c5d196f8e703fdc7c70546cff0dc94cc78dcd | 源码 0.6.0、嵌入式应用层与 PostgreSQL；已固定在 Aftercare 依赖中 |

这是本轮改动开始时核验的源码基线。历史验证条目保留原日期和当时边界；本轮交付后的 HEAD 和远端状态以 Git 日志为准。

本机位置：G:\Projects\Aftercare-Agent；EGM 相邻仓库 G:\Projects\Evidence-Gated-Memory。脚本应使用仓库相对路径或显式配置，不把本机布局当其他贡献者的强制前提。

## 3. 已有与没有的东西

已存在：aftercare_agent/evidence.py 及其回归；EGM 的公共应用层、PostgreSQL 后端与 join；架构、ADR 和接入资料。工作区新增可安装的 Aftercare 0.1.0a0：pyproject.toml、uv.lock、.python-version、py.typed，以及 tests/test_package_contract.py 和[开发指南](development.md)。A0-03 新增 `evals/` 合成案件目录、固定期望、确定性 runner、12 案件回归和[评测说明](evals.md)。

尚未存在：完整业务 Harness、支付聚合、真实供应商连接器、沙箱接线、生产调度体系和高吞吐 live broker tail。调查 EGM 适配器和 PostgreSQL 观察账本已建立代码边界，但真实 EGM 调查 schema 尚未固定。当前已有 Fake Harness、合成 Aftercare 纵向切片（含订单/物流/买家观察与来源引用评估）、一次性/常驻 Worker、Wait/Inbox/Outbox publisher、gap buffer、Action Ledger、审批台账/派发门禁、审批 Wait 原子唤醒、跨实例 admission 最小闭环、显式合成身份或静态 JWKS Bearer 的 Review/Approval 控制面 API、CaseGrant 及其授权管理 HTTP（移交/授权/撤销，见 ADR-0004），以及操作员工单发现 API、React 工作台和 case-scoped SSE replay/tail；仍不是完整执行服务。真实 IdP 演练与权限映射、RLS、授权管理 UI、生产认证验收、浏览器可访问性和高吞吐 broker 仍未完成。不要输出不存在的完整服务启动命令。

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
| A2-02 | IN PROGRESS | `LeaseHeartbeat`、可停止 `run_daemon()`、Outbox publisher 租约/重试与故障注入已实现；锁超时、失联接管和旧 Worker fencing 已在真实 PostgreSQL 验证；完整重启矩阵和远端 CI 仍待补齐 |
| A2-03 | DONE | PostgreSQL 全局/租户执行槽、slot 租约心跳、按 Run 幂等重试预算，以及 007/008 durable queue、Run 状态同步触发器、tenant cursor 轮转和过期 IN_FLIGHT 回收已接入；真实 PostgreSQL 通过跨租户/回收/槽竞争测试；这是基础骨架，权重校准与生产压测仍未完成 |
| B-01 | IN PROGRESS | `ActionIntent`、Action Ledger、跨 Case business key 幂等、UNKNOWN/CONFIRMED/FAILED 与 claim fencing 已实现；审批门禁已补入但支付聚合、额度预留和真实供应商对账仍待实现 |
| B-02 | IN PROGRESS | B-02-01/02 已实现 `ApprovalRepository`、010/011 迁移、参数/策略/身份/有效期绑定、重复决定幂等、`RESERVED` 派发 fail-closed 和绑定 Wait 的 Inbox 原子唤醒；Review 013 已实现 `REVIEW→READY/CANCELLED` 决定边界；已补齐 `approval.*`/`review.*` Outbox 事件、Case 序号分配、不可变门控快照和显式 operator API；支付聚合和供应商回执核对仍待实现 |
| A3-01 | IN PROGRESS | 新增严格 Responses wire parser、`ResponsesAdapter`、整数 token/cost budget、`SessionTranscriptLoader` 与 `build_responses_input()`：校验原生响应、usage、函数参数、工具白名单、托管工具事件、provider 错误脱敏、超预算、transcript scope/序号/digest，并把已校验消息映射为 `ResponsesInputItem` 输入项（`tool` 角色 fail-closed）；离线回归通过；尚未发起真实 provider 请求，Worker 内租约/预算接线未完成 |
| A3-02 | IN PROGRESS | 新增 `InvestigationEvidenceAdapter` 与 PostgreSQL 观察账本：可信连接器规范化写入、来源事件去重/撤回/重载、模型仅提交 `InvestigationProposal`、完整授权观察集确定性评估与默认禁用长期记忆；真实调查 EGM schema、来源认证和模型接线仍待实现 |
| A3-03 | IN PROGRESS | 新增 case-scoped SSE replay、`Last-Event-ID`/after 游标、持久事件分页、有界 PostgreSQL polling tail 和 React 工作台；高吞吐 live broker tail、真实认证和生产压测仍待实现 |
| A3-03-a | DONE | 新增操作员工单发现：可访问工单列表（活动 CaseGrant 收紧、`case_ids` 仅收窄、keyset 游标）、工单详情（Run 投影不含 tenant/lease/fence）、工单下 Review/Approval 列表，以及 `web/` 最小 React+TS+Vite 工作台（列表、详情、决定、事件时间线）；真实 PostgreSQL 全量 `362 passed`（含新增 11 项），前端 `tsc --noEmit` 与 `vite build` 通过，合成身份端到端冒烟通过；live broker tail、真实认证与生产压测仍待实现 |
| A3-03-b | DONE | 工作台接入 SSE 实时订阅：`follow=true&limit=200&wait_seconds=60`，按 `case_seq` 游标续订串接有界读，`fetch`+`ReadableStream` 增量解析（不使用无法带 Header 的 `EventSource`），按 `case_seq` 去重并封顶 500 条，指数退避重连、`401/403` 终止不重试，界面显示 连接中/实时/重连中/已暂停 并可暂停改一次性回放；`web/src/sse.test.ts` 13 项、`tsc --noEmit`、`vite build` 通过，真实后端 `follow` 参数返回 `200 text/event-stream`、非法 `limit` 返回 `400`；订阅级授权、真实 IdP 与高吞吐 broker tail 仍未实现 |
| A3-03-c | IN PROGRESS | SSE follow 改为异步生成器，数据库短轮询放入线程、等待异步 sleep；工作台接入 keyset 加载更多、筛选/身份切换请求代际保护和稳定决策幂等键；异步 tail、前端 API 与回归通过，仍需生产连接池/broker 和真实身份验收 |
| A3-04 | IN PROGRESS | 新增离线闭环评测 `evals/loop.py`：脚本化 Responses client → 工具白名单/整数预算/`parse_investigation_proposal()` → 确定性评估，逐案报告 disposition、引用来源、未知引用、token 与合成成本，并给出稳定 digest 和可恢复的 Harness 步进；12 案件全部通过（1 个有来源建议、6 个转复核或补材料、5 个结构化/边界拒绝）；真实模型效果与账单、Harness 的等待/审批分支和生产压测仍未完成 |
| A1-05 | IN PROGRESS | 新增 SessionMessage append-only transcript 引用表/Repository（tenant/case/session 隔离、连续序号、message_id 幂等、游标读取）与 provider-neutral `SandboxProvider` 生命周期契约；已按 2026-09-15 回填进行计划（原只存在于本台账）；真实 artifact store、Kubernetes/E2B 后端仍待实现，transcript→模型输入映射归 A3-01 |
| C-01 | IN PROGRESS | 新增非安全边界 `FakeSandboxProvider`：allocation 幂等、fencing lease、资源/产物预算、过期回收和销毁确认前保留容量；4 项离线测试通过；真实 E2B/Kubernetes 后端未接入 |
| D-01 | IN PROGRESS | 新增 provider-neutral `JwtJwksVerifier`、FastAPI Bearer 入口、`015_case_grants.sql`/`CaseGrantRepository`、D-01-04 的撤销判定与 D-01-05 的授权管理面：静态 HTTPS JWKS、算法/typ 白名单、短期缓存、未知 `kid` 单次刷新、过期缓存 fail-closed、tenant/scope/Case claim 映射；CaseGrant 默认 fail-closed，token `case_ids` 仅可收窄，grant 与 API 操作同短事务锁定；撤销判定在验签后 fail-closed；管理面用租户级 `grant:read`/`grant:admin`、可授予闭集、委派上限、乐观并发与同事务审计事件；真实 IdP 演练、RLS 与管理 UI 仍待实现 |
| D-01-03 | DONE | PostgreSQL CaseGrant 按 `(tenant_id,subject_id,case_id)` 持久化 scope/revision/有效期/撤销审计；AuthContext 非 synthetic 未绑定时 fail-closed，API 在业务短事务锁定 grant；创建者授权与受理原子提交，撤销/过期/跨主体及 token 收窄回归通过；真实 IdP、introspection、RLS 和管理面仍待完成 |
| D-01-04 | DONE | 新增 `auth/introspection.py` 与 `auth/guard.py`：RFC 7662 判定（静态 HTTPS endpoint、凭据仅存注入的 httpx 客户端、`active` 布尔校验、sub/tenant/exp 与已验证 Token 一致性、有界 TTL 缓存且不缓存已过期判定、传输/解析/超大响应 fail-closed），`TokenAccessGuard` 在验签之后用同一时钟读数执行两步检查且失败统一为 `401 unauthenticated`、不回退合成身份，`auth.guard.TokenVerifier` Protocol 固定验签接缝，`create_default_app()` 按 `AFTERCARE_OIDC_INTROSPECTION_*` 接线并在关机时关闭自有客户端；新增 `tests/test_token_revocation.py` 72 项离线回归；真实 IdP 端点、凭据轮换、RLS 与管理面仍属 D-01 其余部分 |
| D-01-05 | DONE | 新增 Case 授权管理面：`GET/POST /v1/cases/{case_id}/grants` 与 `POST /v1/cases/{case_id}/grants/{subject_id}/revoke`，租户级 `grant:read`/`grant:admin`，可授予权限闭集（排除 `case:create` 与 `grant:*`），不能授出自己没有的权限，替换/撤销需 `expected_revision`（重放 `409`），每次变更同事务写入 `case_grant.granted`/`case_grant.revoked` 与完整快照；离线 25 项 + 真实 PostgreSQL 10 项回归通过（整机满载时的心跳用例抖动与本项无关，见第 6 节）；管理 UI、RLS 与真实 IdP 演练属 D-01 其余部分 |
| C-03 | IN PROGRESS | 新增 provider-neutral Tracer、InMemoryTracer、可选 OTel bridge，并为 Outbox publish 埋点；离线回归通过；Exporter、采样/留存和生产监控尚未配置 |

## 5. 工作区与提交边界

本轮提交范围为 D-01-05 授权管理面切片：`auth/grants.py` 新增可授予闭集 `GRANTABLE_CASE_PERMISSIONS`、`grant:read`/`grant:admin` 与 `authorize_case_grant()`；`persistence/grant_events.py`（新）按 `gate_events.py` 的模式追加 `case_grant.granted`/`case_grant.revoked` 事件与不可变快照；`persistence/case_grants.py` 的 `grant()`/`revoke()` 在返回前同事务追写事件并新增只读 `list_for_case()`；`domain/events.py` 扩展两个 `EventType`；`api/app.py` 新增三个管理路由、`_administered_case()` 与请求/响应模型；`auth/context.py` 给合成身份补上管理 scope；新增 `tests/test_grant_policy.py`（25 项离线）与 `tests/test_operator_grants_integration.py`（10 项需 PostgreSQL）；并同步 `docs/api-auth.md`、`docs/engineering-plan.md`、`docs/tech-stack.md`、`docs/events.md`、`docs/development.md`、`README.md`、`ROADMAP.md` 与新增 `docs/decisions/0004-case-grant-administration.md`。未包含 .venv、缓存、dist、临时数据或 EGM 仓库改动。

EGM 仓库仍有 README.md 修改，assets/egm-roman-banner.png、assets/egm-roman-banner.prompt.md、docs/benchmark-history.md 未跟踪。这些不是本轮工程文件任务的产物，未修改、暂存或回滚。不能使用“清理工作区”删除它们，也不能把它们默默打入固定提交依赖。

提交授权：用户在 2026-09-15 明确要求提交并推送到 GitHub；本轮沿用该授权推送 D-01-05，提交 `f7818d5` 已在 `origin/main`，CI run [34953807687](https://github.com/yushui2022/Aftercare-Agent/actions/runs/34953807687) 为 `success`（`502 passed`）。D-01-04 的提交 `cd6ba9b`、`e3a7586` 与 CI run [34951354092](https://github.com/yushui2022/Aftercare-Agent/actions/runs/34951354092) 同样为 `success`。本轮没有部署、生产数据或真实业务操作；没有发布 Python 包；项目许可证仍待用户确定。

本地提交成功后，记录可随该提交进入本仓库的新 worktree；尚未推送时，另一台机器或 GitHub 不能自动取得它。跨机器交接需另行授权推送或明确的提交传递方式，不把本地提交等同远端同步。

## 6. 验证台账


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

当前推进 D 生产准入：验签（D-01-01/02）、数据库 CaseGrant（D-01-03）、撤销判定（D-01-04）与授权管理面（D-01-05）已实现，D-01 剩下的只有真实 IdP 演练与权限映射、RLS 和管理 UI；同时把有界 SSE tail 纳入生产连接池/broker 评估、固定调查 EGM schema/真实模型接线并选择真实沙箱后端。完整业务 Harness 和生产连接器仍未实现。

尚待决定但不阻塞离线骨架：真实模型 ID/预算、商家渠道和身份提供者、沙箱/对象存储后端与地域、RPO/RTO 和生产负载目标。每项的决策阶段已列在技术栈和执行计划中。无业务凭证不阻塞 Fake 流程；真实接入缺授权时必须停止该分支。

关键风险：本轮包依赖升级尚未重跑 EGM PostgreSQL 全量验收，A1 必须补足；新旧路线图的阶段名称必须一致；不能把 fencing 延后为性能优化；A 阶段通知不得真实外发；同进程不等于同事务；未提交图稿和其他仓库改动不属于本任务；`grant:admin` 在真实 IdP 里必须只发给管理角色，否则等于交出 ACL 写入权。

## 8. 最近交接记录

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
