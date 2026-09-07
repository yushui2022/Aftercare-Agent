# 当前工程状态与接手点

格式版本：1。最后核验日期：2026-09-07（Asia/Shanghai）。记录者：本轮主 Agent。

本文件是进度与交接的唯一台账，不是实际代码/测试的替代证据，也不是自动执行授权。先读根 [AGENTS.md](../AGENTS.md)，任务定义见 [工程执行计划](engineering-plan.md)。

## 1. 当前工作位置

| 字段 | 值 |
|---|---|
| 本轮请求范围 | 用户授权本地提交已完成的 DOC-001/A0-01/A0-02；不推送、不新增运行时功能 |
| 当前任务 | 封存工程基线与 v1 契约；实现任务仍停在 A0-02 完成 |
| 当前阶段 | A0-02 已实现并验收：纯契约、类型校验与正反例；尚无持久运行时 |
| 下一项代码候选 | A0-03：合成案件与确定性评测基线；A1 持久化另行实施 |
| 活跃实现任务 | 无新增实现；本次仅做提交前复验、范围核对与本地提交 |
| 本轮外部行为 | 仅授权 Aftercare 本地 Git 提交；无推送、模型调用、真实业务动作或部署 |

## 2. 核验过的源码基线

| 仓库 | 已核验源码 HEAD | 用途 |
|---|---|---|
| Aftercare-Agent | d4a20c905343c4a84cd92b9687f9f5bb1c298d71 | DOC-001/A0 实施前的历史基线；工程增量由包含本页的交付提交固化，实际编号以 Git 日志为准 |
| Evidence-Gated-Memory | 9c7c5d196f8e703fdc7c70546cff0dc94cc78dcd | 源码 0.6.0、嵌入式应用层与 PostgreSQL；已固定在 Aftercare 依赖中 |

这些是核验时的源码基线，不要求文档为了指向包含自身的提交而无限修改。本轮没有检查远端分支或远端 CI；不要把旧交接中“远端与本地一致”当作当前核验。

本机位置：G:\Projects\Aftercare-Agent；EGM 相邻仓库 G:\Projects\Evidence-Gated-Memory。脚本应使用仓库相对路径或显式配置，不把本机布局当其他贡献者的强制前提。

## 3. 已有与没有的东西

已存在：aftercare_agent/evidence.py 及其回归；EGM 的公共应用层、PostgreSQL 后端与 join；架构、ADR 和接入资料。工作区新增可安装的 Aftercare 0.1.0a0：pyproject.toml、uv.lock、.python-version、py.typed，以及 tests/test_package_contract.py 和[开发指南](development.md)。

尚未存在：完整 API/Worker/Harness、Case/Run/Wait 表、Action Ledger、真实供应商连接器、调查证据适配器、前端、沙箱接线和可运行部署配置。不要输出不存在的完整服务启动命令。

当前 evidence.py 只负责固定退款完成声明的证据验证。domain 已有运行时、协议、等待、事件与订单/物流/买家材料的独立纯契约；调查 EGM 适配、持久执行与实际来源认证仍未实现，不能视为“EGM 已有所以调查已接通”。

技术选择已写入 [tech-stack.md](tech-stack.md)。当前实际环境是 Windows、CPython 3.13.15、uv 0.9.26；EGM 0.6.0 固定完整 Git SHA，运行依赖与测试工具在 uv.lock 中锁定。FastAPI、PostgreSQL 服务、前端 TypeScript/React/Vite、Node/pnpm、模型和沙箱仍需在各自任务引入，不能把选型表当作已安装清单。

## 4. 任务状态表

未列出的执行计划任务均为 BACKLOG。READY 只是依赖满足的候选，不代表获得了未来会话的实现授权。

| Task ID | 状态 | 证据或剩余事项 |
|---|---|---|
| DOC-001 | DONE | 四份核心记录与导航已写；两项只读复核完成；9 文件、62 本地链接、25 任务定义和 7 图语法检查通过，见第 6 节 |
| A0-01 | DONE | 锁定安装、Ruff/mypy、开发环境 9 项测试、sdist→wheel、仓库外 9 项测试和文档检查通过；见第 6 节 |
| A0-02 | DONE | 两份契约、7 个 domain 模块、5 个契约测试模块；206 项回归、类型/格式、39 类 JSON Schema、sdist→wheel 与仓库外回归通过，见第 6 节 |
| A0-03 | READY | 下一项：将契约正反例组织成可复用的合成案件、期望结果与离线评测；尚未实施 |

## 5. 工作区与提交边界

本次提交范围为累计的 DOC-001/A0-01/A0-02，共 34 个文件，实施前 HEAD 如上。包括 aftercare_agent/domain/ 的 __init__、common、runtime、protocol、waits、events、investigation；tests/contracts/ 五个正反例模块；docs/contracts/ 两份契约；打包配置、依赖锁、MANIFEST.in 和工程交接文档。README、开发指南、技术栈、执行计划、总设计与本台账一起保存。未包含 .venv、缓存、dist、临时数据或 EGM 仓库改动；evidence.py 与 A0-01 wheel 中的内容逐字节一致。实际文件集合以交付提交的 git show --stat 为准。

EGM 本轮观察到 README.md 修改，assets/egm-roman-banner.png、assets/egm-roman-banner.prompt.md、docs/benchmark-history.md 未跟踪。这些不是本轮工程文件任务的产物，未修改、暂存或回滚。不能使用“清理工作区”删除它们，也不能把它们默默打入固定提交依赖。

提交授权：本轮用户明确要求本地提交，本文随该交付提交保存；以 git log 和 git status 核验实际提交结果，不为了在文档里写入自身哈希反复修改提交。推送：本轮没有。部署：本轮没有。生产数据/真实业务操作：本轮没有。没有更新两个工作仓库的远端跟踪分支；此前 uv 取得过 EGM 固定提交，不等于远端 main 与 CI 状态核验。没有发布 Python 包；项目许可证仍待用户确定。

本地提交成功后，记录可随该提交进入本仓库的新 worktree；尚未推送时，另一台机器或 GitHub 不能自动取得它。跨机器交接需另行授权推送或明确的提交传递方式，不把本地提交等同远端同步。

## 6. 验证台账

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

未验证/未实现：真实 PostgreSQL 原子领取、锁顺序/事务/故障恢复、真实认证、事件持久分发、配置版本仓库、模型效果、EGM 调查投影、Linux/容器、CI、真实供应商和生产压测。A1/A2/A3 必须继续补足，不能用本轮 206 项纯测试替代。

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

下一项 A0-03：读取两份 v1 契约及现有 tests/contracts 正反例，在 evals/cases、evals/expectations 组织完整合成案件与确定性评测入口，覆盖正常、缺材料、矛盾、陈旧、跨订单/租户、重复事件和伪造指令。不能把单条声明通过等同建议文本完成，也不使用真实客户材料。随后进入 A1-01 的 PostgreSQL 迁移/Repository/Run 执行权；本轮未启动这些任务。

尚待决定但不阻塞离线骨架：真实模型 ID/预算、商家渠道和身份提供者、沙箱/对象存储后端与地域、RPO/RTO 和生产负载目标。每项的决策阶段已列在技术栈和执行计划中。无业务凭证不阻塞 Fake 流程；真实接入缺授权时必须停止该分支。

关键风险：本轮包依赖升级尚未重跑 EGM PostgreSQL 全量验收，A1 必须补足；新旧路线图的阶段名称必须一致；不能把 fencing 延后为性能优化；A 阶段通知不得真实外发；同进程不等于同事务；未提交图稿和其他仓库改动不属于本任务。

## 8. 最近交接记录

- 2026-09-07 / 本地交付提交：用户明确授权固化 DOC-001/A0-01/A0-02；提交前 206 项回归、类型/格式与范围复核通过，34 个项目文件随本页一起保存。实际提交编号和工作区状态以 Git 日志核验；不推送，不包含 EGM 未提交材料，下一项仍为 A0-03。

- 2026-09-07 / A0-02 完成：运行时/调查 v1 契约落到纯代码与正反例；206 项本地及独立 wheel 回归、类型/格式和 schema 验证通过。补正文摘要幂等、JSON 非有限值与嵌套测试打包问题。保留 WinError 32 的失败和替代构建记录，未提交/推送/部署；下一项候选 A0-03。

- 2026-09-07 / A0-02 开始：核对 HEAD 与未提交工作区，保留 A0-01 和设计文档；按公共类型/运行时/调查分文件实施及独立复核，不提交、推送或部署。

- 2026-09-07 / A0-01 完成：固定 CPython 3.13.15、EGM 完整 Git 提交与 uv.lock；类型/格式、9 项回归、冷安装、sdist→wheel、仓库外 9 项回归、文件来源与文档检查通过。适配器业务语义保持，未改 EGM 源码。本轮未提交、推送或部署；下一项候选 A0-02。

- 2026-09-07 / A0-01 开始：用户授权执行并维护文档；主 Agent 限定本轮为 Python 包与回归基础，独立只读复核打包/依赖边界。尚未安装或完成验收，不提交/推送。

- 2026-09-07 / DOC-001 完成：建立持久工程记录，明确 Python/TypeScript/SQL 分工、A0–A3/B/C/D 依赖和恢复协议；文档检查与只读复核通过。本轮未实现业务代码、提交、推送或部署；下一项候选为 A0-01。

只保留近期有用记录，不粘贴聊天全文。较长历史依靠 Git 和版本化验收文件。压缩/突发中断后的第一动作是核对文件与事实，不是无条件重做上一条任务。
