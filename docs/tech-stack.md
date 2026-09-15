# 技术栈与工程约定

更新日期：2026-09-07。本文固定首版实施基线；Python 包依赖已落盘，其他服务与前端仍是待实现选择。任务与当前进度分别见 [执行计划](engineering-plan.md)、[状态台账](project-status.md)。

## 1. 语言与运行时

| 部分 | 基线选择 | 用途与理由 |
|---|---|---|
| API、Harness、Worker、连接器 | Python 3.13 常规 CPython | 复用当前 EGM 与 Python 适配器，保持一个后端语言和清楚的事务边界 |
| 客服工作台 | TypeScript，开启 strict；React + Vite SPA | 管理 Case、证据、等待和进度；浏览器不承担业务执行 |
| 前端构建环境 | Node.js 24 LTS + pnpm | Node 主要是构建/测试工具，不新增第二套业务后端 |
| 持久数据与迁移 | PostgreSQL 17、参数化 SQL | 业务约束、事务、锁和任务领取需要显式可审查 |
| 部署与开发脚本 | Dockerfile/Compose YAML；必要的 PowerShell/Bash | 本地 Windows 开发，Linux 容器为集成/部署目标；脚本不承载业务规则 |

Python 3.13 是兼容性起点，不是“最新版”声明。历史 EGM 验收使用 3.13.11；当前 Python 包使用常规 CPython 3.13.15，已写入 [.python-version](../.python-version)，包范围为 >=3.13.15,<3.14。Linux 镜像仍需在 A1 选定并验证，不把 Windows 验收外推到所有平台。后续安全补丁更新需重新锁定和回归。Python 3.13 的维护阶段见 [PEP 719](https://peps.python.org/pep-0719/)。

Node 24 在本次核查的官方计划中属于 LTS；前端落地时再次检查维护与安全补丁，锁定具体版本。Vite 模板的 Node 要求应在安装时一起验证，不把“电脑已有 node”当兼容证明。[Node 发布计划](https://github.com/nodejs/Release)、[Vite 指南](https://vite.dev/guide/)

第一版不引入 Go/Rust/Java 后端，不本地训练模型，不为每个 Agent 部署模型权重。若未来有实测 CPU 热点或组织约束，再通过 ADR 改变语言边界。

## 2. Python 后端与依赖

| 类别 | 选择 | 首次任务 |
|---|---|---|
| 包与环境 | pyproject.toml + uv.lock + 项目 .venv；setuptools 构建 | A0-01 |
| 类型与数据校验 | Python 类型注解、Pydantic 2；公共接口不传播无约束 Any | A0-01 / A0-02 |
| Web 服务 | FastAPI + Uvicorn | A1-02 |
| 数据库 | psycopg 3 + psycopg_pool 有界池（D-04 起默认启用，见 [ADR-0006](decisions/0006-bounded-connection-pool.md)），同步短事务函数 | A1-01 |
| 外部 HTTP | httpx；统一超时、重试与错误分类；JWKS 与 introspection 使用固定 HTTPS URL | D-01 / A3-01 / B-03 |
| 身份验证 | PyJWT + cryptography；provider-neutral JWKS 验签适配器与 RFC 7662 撤销判定 | D-01；具体 IdP 与 CaseGrant 服务待选 |
| 模型接口 | FakeModelAdapter 先行；官方 OpenAI Python SDK 的 Responses 适配器随后 | A1-03 / A3-01 |
| 质量检查 | Ruff、mypy、pytest、pytest-asyncio；依任务加入测试依赖 | A0-01 |
| 观测 | 起步结构化日志与关联 ID；随后 OpenTelemetry SDK/Collector | A1-03 / C-03 |

依赖在引入时锁定经过测试的精确版本。当前 [pyproject.toml](../pyproject.toml) / [uv.lock](../uv.lock) 已包含 Aftercare 0.1.0a0、EGM、FastAPI、httpx、PyJWT/cryptography、直接使用的 Pydantic 和质量工具；模型 SDK、前端或整套观测平台仍未加入。Pydantic 必须直接声明，不能因为 EGM 间接安装就漏掉 Aftercare 自己的依赖。

当前锁定的主要版本：Pydantic 2.13.5、psycopg/psycopg-binary 3.3.5、psycopg-pool 3.3.1、PyYAML 6.0.3、Ruff 0.16.6、mypy 1.20.2、pytest 9.1.1、pytest-asyncio 1.4.0；构建采用 setuptools 84.0.0、wheel 0.48.0、packaging 26.3。完整版本与来源以锁文件/构建配置为准；安装、质量与产物检查见[开发指南](development.md)。这份清单不表示已有 PostgreSQL 服务或生产镜像。

EGM 源码基线固定为 9c7c5d196f8e703fdc7c70546cff0dc94cc78dcd，包版本 0.6.0。普通构建从确切 Git 提交解析/构建并进入锁文件；发布镜像可使用该提交构建的内部 wheel。开发用相邻仓库 editable 覆盖只能显式开启，必须记录源码差异，不能以此声称是可复现发布。只需要核心和 postgres 能力时，不把 EGM 的 dev/server extras 带进生产镜像。

日常采用 uv sync --locked 校验元数据与锁文件一致；不能把 --frozen 当成“已检查依赖声明没有漂移”，它跳过锁文件时效检查。EGM Git 来源也写入标准包依赖元数据，不只藏在 uv 专属覆盖配置里；单独安装 wheel 仍须沿用锁定环境或审核后的依赖约束。[uv 锁定与同步](https://docs.astral.sh/uv/concepts/projects/sync/)

## 3. 并发与数据库编码方式

Worker 的模型/HTTP 等待使用 asyncio 和有界并发。数据库与同步 EGM 调用包装成短小、完整的同步事务函数，在有限线程/执行槽中运行；连接在同一函数中借出、使用、提交/回滚并归还。调用取消时不能假定后台线程中的事务已停止，必须通过操作键和数据库状态核对结果。

不在事件循环线程直接执行阻塞 SQL/EGM，不在多 Run 之间共享一条正在使用的连接，不跨进程继承连接池。psycopg 同连接查询串行，多个游标还共享事务状态，这也是按工作单元借连接的理由。工作单元仍按需借还，但连接来自每进程一个的有界池：等待中的工作不占连接，拿不到连接在超时后 fail-closed，池化连接上不得留下 `SET`、`LISTEN` 或 `search_path` 这类会话状态（池只回滚归还时未结束的事务，不重置会话）。[psycopg 并发说明](https://www.psycopg.org/psycopg3/docs/advanced/async.html)

Worker 的租约心跳是唯一的长持有者：它从池里借一条连接（`Database.open()`）、只由该线程使用、跨多个短事务复用、在返回前归还（`release()`），因为续期必须落在由租约推出的窗口里，按工作单元借连接等于每次续期都先付一次建连（本机实测建连 p50 115 ms / 最大 215 ms，而续期事务 p50 0.8 ms）。它仍占一个连接槽，池的 max_size 要为此留位置；这条例外不改变“不在线程之间共享正在使用的连接”的边界。

首版不引入 ORM；使用 Repository 和显式 Unit of Work 组织参数化 SQL。既有 EGM join 接受同步 psycopg 连接，Aftercare 的同事务操作必须沿用相同连接及外层事务，不通过另一个池“看似同库”地写入。

池健康按 [ADR-0007](decisions/0007-pool-metrics-and-capacity.md) 发布：`Database.stats()` 是唯一读取口（只含整数，装不下租户/工单/语句/DSN），每进程一个采样器按 `aftercare.db.pool.*` 发 gauge 与 counter，唯一标签是 `component`。默认每 10 s 一次并写标准库日志一行 JSON，`AFTERCARE_POOL_METRICS=0` 关闭，`AFTERCARE_POOL_METRICS_INTERVAL_SECONDS` 改间隔；OTel bridge 是可选类，导出器/采样/留存仍属 C-03。`max_size` 是“按并发有界”的默认值而不是容量结论：定标要用 `aftercare-capacity` 的实测 sweep，版本、工作负载、失败率、延迟与资源成本一起公布，样例参数不当 SLA，方法与三份本机报告见[容量报告](capacity/README.md)。

业务迁移使用按版本编号的 SQL 文件和一个受限迁移命令：校验已应用文件摘要、互斥执行迁移、记录版本、默认事务执行，失败不假报成功。初版不支持在普通事务迁移中偷偷执行必须非事务运行的操作；需要时单独设计运维步骤。Aftercare 与 EGM 保留各自迁移版本，不合并成一个不透明 schema_version。

首版关键列与约束使用关系字段，协议检查点和扩展载荷可用带版本的 JSONB；不是所有内容都塞进一个 state JSON。金额用最小货币单位整数，禁止浮点数参与金额比较；跨前端或 EGM 边界需要字符串表达时必须规范化且往返不丢精度。时间使用带时区 UTC，租约使用数据库时间，原始观察时间不因重放更新。

## 4. 前端、接口和文件

前端位于 web/，采用 React + TypeScript + Vite，先做 Case 列表、详情、证据引用、等待原因、通知草稿和事件时间线。不需要 SSR/SEO，因此首版不引入 Next.js 服务端运行时。React 与 TypeScript 的集成依据见 [React 官方指南](https://react.dev/learn/typescript)。

Python/Pydantic 定义 HTTP 边界，导出版本化 OpenAPI；前端从已检查的 OpenAPI 生成类型，并测试兼容性。SSE 事件使用单独明确的版本化信封，不能靠字符串拼接随意扩展。前端类型正确不代表后端已做授权，所有写命令仍由后端验证。

同源反向代理承载 UI 和 /api，生产身份接企业 OIDC 等经确认的认证方案；具体身份提供者列为上线前待选项。A 阶段允许只在测试/显式本地开发模式使用合成身份，不能暴露为公网匿名多租户 API。SSE 游标不作为认证凭证，恢复要处理权限范围变化。

前端依赖使用 pnpm-lock.yaml，精确 pnpm 版本写入 packageManager，并在 CI 固定安装方式；不要假定任意 Node 安装都已附带可用的 pnpm。单测计划用 Vitest/Testing Library，关键交互用 Playwright。首次前端任务再锁定兼容版本，不提前生成未使用的大量组件。

阶段 A 的通知只生成草稿，附件先用合成结构化输入，不执行任意用户上传代码。引入真实文件后使用 S3 兼容对象存储接口，具体供应商与驻留地域在 C 阶段决定。文件原文不依赖 Pod 临时磁盘持久保存。

## 5. 模型、消息、沙箱和记忆选择

| 能力 | 首版决策 | 明确延后 |
|---|---|---|
| 模型 | Fake 驱动恢复测试，A3 首接 Responses；模型 ID 由任务集评测和账户可用性决定 | 自动多模型路由、跨供应商自动切换 |
| 消息 | PostgreSQL Task/Inbox/Event/Outbox；HTTPS 命令 + SSE 展示 | Kafka、NATS JetStream、Redis Streams、ACP 客户端 |
| 记忆 | Worker 内嵌 EGM；业务状态与检查点独立；长期记忆默认关闭 | Letta、TencentDB Agent Memory、向量库 |
| 沙箱 | 服务端 Harness + 窄 SandboxProvider；先 Fake，后选一个真实后端 | 同时部署 E2B 和 K8s、把完整业务控制面搬入沙箱 |
| 部署 | A1 最小 Compose，后续 Linux 试点；K8s 按运维与隔离需要引入 | 未经验证的生产 Helm/YAML 和自动扩容承诺 |

认证实现边界：`auth.oidc` 现在包含 provider-neutral 的 `JwtJwksVerifier`，API 可在显式
配置 `AFTERCARE_OIDC_ISSUER`、`AFTERCARE_OIDC_AUDIENCE`、`AFTERCARE_OIDC_JWKS_URL`
后验签 Bearer。PyJWT + cryptography 负责签名验证，httpx 仅访问静态 HTTPS JWKS；密钥短期
缓存、未知 `kid` 一次刷新和缓存失效 fail-closed 已有离线测试。`auth.introspection` 与
`auth.guard` 在同一验签之后追加可选 RFC 7662 撤销判定（`AFTERCARE_OIDC_INTROSPECTION_URL`
与 `_CLIENT_ID`/`_CLIENT_SECRET`），凭据只存注入的 httpx 客户端、判别结果有界缓存且统一
fail-closed。这仍不等于企业授权：数据库 CaseGrant、真实 IdP 权限映射、密钥轮换演练和
生产部署验收属于 D-01 后续。CaseGrant 的权威仍是每个业务事务内的短锁判定；授权管理
HTTP 由租户级 `grant:read`/`grant:admin` 承载，可授予权限是排除 `case:create` 与
`grant:*` 的闭集，且不能授出管理员自己没有的权限（见
[ADR-0004](decisions/0004-case-grant-administration.md)）。

Responses 适配器保存完整工具请求、call_id 和恢复所需协议项，再执行经过校验的本地工具；应用自管业务状态。供应商提供会话关联不等于已经提供我们的持久业务运行时。该约束来自本项目设计，并与 [OpenAI Function Calling](https://developers.openai.com/api/docs/guides/function-calling)、[Conversation State](https://developers.openai.com/api/docs/guides/conversation-state) 的能力边界一致。

当前不固化模型价格、账户限额或“最强模型”排名。A3 真实调用前确认凭证配置、预算、数据授权、日志与保留设置；store=false 不应被解释成没有任何数据保留。无真实凭证时 Fake 路径仍可进行工程验收，真实效果验收另记状态。

## 6. 目录边界与运行配置

以下大部分业务模块仍是目标目录；当前已有 aftercare_agent/evidence.py、包与类型标记、domain 中的 v1 纯契约、适配器/打包/契约测试、Python 打包配置和文档。其他路径在相应任务中创建，不先批量生成空文件。

```text
aftercare_agent/
  evidence.py                 已有退款证据适配器，保持兼容
  domain/                     已有 v1 纯类型与规则；不是数据库运行时
  persistence/                Repository、事务、迁移 SQL
  api/                        HTTP/SSE 和输入契约
  auth/                       可信身份与 Case 授权
  runtime/                    Harness、租约、检查点、等待
  connectors/                 可信来源与模拟/真实连接器
  model_adapters/             Fake / Responses
  investigation/              调查证据契约与 EGM 适配
  events/                     Inbox、Outbox、展示投影
  actions/                    B 阶段操作台账、审批与核对
  sandbox/                    C 阶段资源生命周期
  observability/              诊断与 OTel
  ops/                        备份、恢复演练、演练记录与新鲜度、保留删除、恢复后核对（运维工具，不在请求路径）
web/                          A3 工作台
tests/                        单测、PG 集成、故障和契约测试
evals/                        合成案件、期望与评测报告
deploy/compose/               A1 开发环境
deploy/k8s/                   按需要引入，不是当前可用配置
```

配置分为运行环境、数据库/池、Run 预算、模型、连接器、沙箱、对象存储和观测。敏感项从环境/Secret Manager 读取；.env.example 只有无效示例。租约、超时、并发、成本不能因文档示例就成为未经测试的生产默认值；每项要有单位、范围、默认理由和启动时验证。

本机 G 盘存储规则沿用仓库 AGENTS.md。Python 解释器位于 G:\DevCache\Python，项目环境为 .venv，uv 缓存位于 G:\DevCache\uv，临时打包验收位于 G:\DevCache\Temp\aftercare-a001。Docker/WSL 的虚拟磁盘位置不受项目在 G 盘自动保证；拉镜像或创建数据库卷前必须核验空间和实际落盘位置，不擅自迁移现有缓存。

## 7. 后续变更规则

新增依赖需说明用途、替代方案、许可、支持版本、安全影响与验证命令。语言、数据库主权、Harness 位置、EGM 默认调用方式和任务执行语义变化需要 ADR，并同步计划与状态。常规安全补丁更新记录锁文件及回归证据即可。

当前尚待确定：后续新增依赖与镜像补丁版本（A1/A3）、真实模型 ID 与预算（A3）、客户身份提供者及通知渠道（试点前）、沙箱与对象存储后端/地域（C）、备份与恢复的目标 RPO/RTO 和负载目标（D）。D-02 已给出备份、演练、核对、新鲜度预算与 WAL 归档检查的工具和口径（[ADR-0008](decisions/0008-backup-and-restore-drills.md)、[ADR-0009](decisions/0009-backup-freshness-and-drill-records.md)、[ADR-0010](decisions/0010-wal-archive-checks.md)），工具不会自带默认预算，目标值必须在具体部署上按节奏与演练确定，本机数字不能替代。这些是有阶段归属的开放项，不是默许使用任意默认值。
