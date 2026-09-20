# Aftercare Agent：开源版与真实部署路线

版本：0.1；更新日期：2026-09-20。

Aftercare 是面向真实跨境售后场景的开源参考实现。它把持久 Case/Run、证据账本、确定性证据门、人工协作和受控动作做成可审查、可替换、可部署的业务内核。仓库同时提供一个可复现的本地运行配置，方便贡献者在不接入客户系统的情况下验证这套真实业务语义。

合成数据和模拟连接器是运行配置，不是产品定义。真实电商、物流、身份、模型、沙箱和动作供应商通过适配器接入；接入后仍使用同一套 Case、证据、Review、Approval、Run 和 Action Ledger 契约。当前仓库对哪些能力已有代码、哪些能力已有适配接口、哪些能力已经在目标环境验收，分别标注如下。

## 1. 三层产品模型

| 层 | 作用 | 当前公开形态 |
|---|---|---|
| 业务内核 | Case/Run 生命周期、租约与 fencing、等待/恢复、证据来源与评定、人工决定、Action Ledger、事件和审计 | 已有较完整实现，仍在补调查闭环 |
| 适配器 | 订单/物流/客服来源、身份、模型、EGM、沙箱、外部动作和通知 | 契约与部分 provider 已有；来源 Webhook 的规范化、验签、幂等和接账指南已补齐，Action Ledger 另有无副作用的 `SyntheticActionProvider`；真实供应商仍需要按环境接入和验收 |
| 运行配置 | 本地可复现、集成验证、目标环境部署的组合 | local profile 可运行；integration/deployment profile 需要继续交付 |

“已实现”表示仓库有代码和验证证据；“适配就绪”表示边界、输入输出和失败语义已固定，但还需要接入方；“环境已验收”才表示在指定 PostgreSQL、身份、供应商和部署环境中跑过。三者不互相替代。

## 2. v0.1 要交付什么

开源版 v0.1 的目标是交付一套真实业务架构的可运行参考实现：

1. 新贡献者可以安装依赖，启动 PostgreSQL 和 API，并运行一条完整的本地调查与人工协作链路；
2. 每个结论都能追溯到授权来源、证据版本、评定快照和人工决定；等待、崩溃、重复消息和 Worker 接管有持久化语义；
3. 订单、物流、买方陈述、模型、EGM 和动作 provider 的接入边界公开，真实系统可以按契约替换本地 provider；
4. 默认运行配置不产生不可逆外部副作用，外部动作必须经过明确的 provider、审批、幂等和对账配置；
5. CI、PostgreSQL 集成、打包、文档、许可证、安全报告和贡献路径足以让别人复现、审查和扩展项目。

因此，v0.1 不是“永久模拟器”，也不是对某个客户环境已经生产验收的承诺。它是可以拿去做真实适配和试点的业务内核与参考部署路径。

## 3. 三种运行配置

### Local profile

使用合成 Case、可控连接器和本地 provider，验证数据库事务、证据门、等待/唤醒、检查点恢复、人工 Review/Approval、Action Ledger 和事件回放。它不需要客户凭证，适合 CI、教程和问题复现。

### Integration profile

使用 PostgreSQL、测试租户、契约测试 provider 和可选的真实模型端点，验证认证、连接器响应映射、EGM schema、模型预算、沙箱资源和外部动作的失败/重试语义。该配置是从本地运行到目标系统的过渡层。

### Deployment profile

使用目标环境的 IdP、RLS、订单/物流/客服连接器、密钥托管、沙箱、观测和备份策略。生产镜像、迁移 Job、TLS、资源限制、滚动升级、RPO/RTO 与容量结论必须在具体环境单独验收；当前仓库尚未声称这些环境已经验收。

## 4. 当前基线与缺口

| 能力 | 当前状态 | 还要补什么 |
|---|---|---|
| Case/Session/Run、租约、fencing、等待、Inbox/Outbox、检查点 | 已实现/持续加固 | 重启矩阵、生产压测和运维入口 |
| 订单/物流/买方证据账本与确定性评定 | 已实现/持续加固 | 历史分页、Worker 导入、持久游标、撤回和标准化来源认证已验收；仍需供应商原生验签、更宽恢复矩阵和目标环境复跑 |
| EGM 嵌入式接缝 | 已接入/持续加固 | Worker node/revision/operation、写入/撤回收据、崩溃窗口重放和并发 revision 已在 PostgreSQL 验收；仍需目标环境验收与上游正式版本发布 |
| 人工 Review/Approval 与 Action Ledger | 已实现/持续加固；provider-neutral `ActionProvider`/`ProviderReceipt` 回执契约、`mark_receipt()` 落账入口和 `SyntheticActionProvider` 已补齐 | 支付聚合、额度预留、真实供应商回执和对账 |
| 模型适配与 Harness | 已有真实 provider 适配，另有 14 案例确定性调查 Harness | 真实模型 shadow 回放、生产预算、成本与延迟观测 |
| 模型判断层 | 有 provider-neutral 接缝，策略身份/漂移门和独立迁移审计已落地，暂不绑定具体模型 | 完整业务 Harness、模型版本/预算/延迟记录、失败和降级策略，以及真实 IdP 下的迁移权限验收 |
| 身份、CaseGrant、撤销和管理面 | 有 provider-neutral 实现；integration 已用 runtime 角色验证 RLS 租户边界，镜像内 `aftercare-rls harden/verify` 可执行 owner 强制与回滚探针 | 真实 IdP 演练、目标环境实际运行 `FORCE RLS`、租户级授权运营 |
| 沙箱与连接器 | 契约和开发 provider | 真实 E2B/Kubernetes、外部凭证生命周期和网络策略 |
| 本地 Compose、Worker、备份工具 | 开发配置可运行；平台无关 deployment profile 已拆分迁移/运行身份，并以文件 Secret 避免 DSN 进入容器环境；`deployment_preflight.py` 可在启动前检查不可变镜像、Secret、PostgreSQL `sslmode=verify-full`、OIDC 和池边界；Secret rotation runbook 已固定替换、重启、验收和回滚顺序；`deployment_acceptance.py` 提供目标环境九项验收记录模板，PITR 与租户隔离都是独立必需门禁 | 目标 secret manager/轮换与 TLS CA、滚动升级矩阵、对象存储、加密与异地副本；目标环境证据仍需实际填写和复跑 |
| 开源仓库卫生 | LICENSE、SECURITY、CONTRIBUTING、CHANGELOG、`.env.example`、`.dockerignore` 已补齐；EGM 固定提交含独立 MIT 许可证，运行依赖已有版本化许可清单 | 在托管平台启用或核对 secret scanning；锁文件变化后重跑依赖许可审计 |

`aftercare-backup pitr` 已提供 PITR 报告的清单绑定和完整性校验；仓库还提供 `deploy/pitr_docker_drill.py`，可在本机用 PostgreSQL 17 实际跑一遍物理基线、WAL 前滚和时间点恢复，生成同一证据格式。目标平台的实施顺序、权限、对象保留、失败处理和九项验收门见 [PITR 实施与验收](operations/pitr-deployment.md)。该 profile 的业务查询是合成探针，生产仍需替换成真实不变量并在目标环境验收；异地加密副本、调度告警和目标环境 RPO/RTO 仍属于部署验收。

以上缺口的含义是“尚未完成或尚未在目标环境验收”，不是“业务内核只能做演示”。实时细节以 [project-status.md](project-status.md) 为准，任务依赖和验收以 [engineering-plan.md](engineering-plan.md) 为准。

## 5. 开源版实施顺序

下面是按依赖和事故风险排出的默认顺序，不是要求所有工作严格串行。互不修改同一契约的适配器、文档和运维切片可以并行；一旦目标环境、真实供应商或试点范围确定，就按实际风险重排，但不能跳过对应的证据、权限、幂等和恢复验收。

### P0：固定公开契约

README、MIT 许可证、安全政策、贡献指南、CHANGELOG、`.env.example`、`.dockerignore` 和状态标签已补齐。当前锁定的 Python 运行依赖、浏览器运行依赖及项目自有样例已记录在 `THIRD_PARTY_NOTICES.md`，sdist/wheel 也携带项目许可证和该清单；EGM 固定提交已有 SPDX 元数据与独立 MIT 许可证。运行镜像还携带 OCI 来源、版本、源码 revision、许可证、EGM SHA 和两个 schema 版本标签，CI 会核对这些标签与构建提交一致，并验证 OCI config labels 与 `docker inspect` 的镜像身份一致，再验证 OCI 导出中的 SPDX SBOM 与 SLSA provenance subject/blob/predicate；同时固定 `pip-audit==2.10.1` 审计 Python 环境，用固定 digest 的 Trivy 扫描运行镜像 OS/library 依赖，并用固定 digest 的 Gitleaks 扫描完整 Git 历史，所有外部 GitHub Actions 也固定到不可变提交，均保留报告。发布前仍需复核最终锁文件、在托管平台启用平台级 secret protection、完成镜像签名，并在目标镜像仓库持续复扫；所有文档使用“已实现 / 适配就绪 / 环境已验收”三种说法，不用一份本地配置推断生产结论。

CI 还会将上述报告合成为 `release-evidence.json`，把单次构建的镜像身份、源码/EGM/schema 版本、SBOM/provenance、安全扫描结果、五份基础输入报告的 SHA-256，以及本机 PITR profile 的三份摘要绑定放进一个可归档的机器可读清单；构建完成后还会把 wheel 安装到仓库外的隔离环境，用 `python -I` 运行包契约，避免源码目录遮蔽发布包。目标发布流水线补入 cosign 签名、registry 复扫和九项目标环境验收时，规范化证据及其原始输出也会绑定到同一 OCI digest。使用 Kubernetes reference profile 时，可额外把 `verify_kubernetes_profile.py` 的干净通过报告作为可选附件传给 `release_evidence.py`；它必须声明同一镜像 digest、固定六项检查全部通过、三个 YAML 输入文件各自带字节数和 SHA-256 且错误列表为空，不能把离线 profile 误当成目标集群验收。清单里的 `promotion_gates` 是固定集合，每个门显式记录 `open` 或 `passed`，并与 `unresolved_gates` 交叉校验。CI 随后生成 `release-decision.json`；当前应为 `hold`。目标发布流水线可用 [`deploy/release_policy.py`](../deploy/release_policy.py) 将清单判为 `hold` 或 `promote`；在三个证据门尚未补齐时，当前仓库不会自动晋级。清单是发布审计入口，不把本地 CI 结果写成生产验收结论。

### P1：完成调查证据闭环

连接器答案到订单、物流和 `BuyerStatement` 的桥接、调查 EGM schema、来源授权、版本、新鲜度、撤回、重复观察、跨租户隔离和恢复已经落地。Worker 会在模型步骤前批量导入历史，并在证据与 EGM 成功后推进持久游标；写入和撤回都保存可重放 operation/revision 收据。PostgreSQL Worker、并发游标、并发 EGM revision 和“EGM 已提交、Aftercare 未确认”的两类崩溃窗口已验收。下一步是供应商原生验签、更宽恢复矩阵和目标环境复跑。模型建议只能引用已存在的证据 ID，不能改变 EGM 的准入结果。

### P2：让适配器可替换、可验证

固定 connector、identity、model、EGM、sandbox、action provider 的 Protocol、错误分类、超时、幂等和审计字段；为每个 provider 保留 local/test 实现和契约测试。来源 Webhook 的规范化边界见[接入指南](integrations/source-webhook.md)。至少给出一个真实系统接入样例或清晰的接入模板，而不是把供应商响应散落在业务状态机中。

### P3：交付 deployment profile

平台无关的容器运行契约已经落地：`aftercare-migrate` 安装 Aftercare+EGM schema，`aftercare-rls harden`/`verify` 在 API/Worker 启动前完成 owner 强制和 runtime 回滚探针，API/Worker 可用无 DDL role 启动并只读校验 schema；integration Compose 已真实跑通迁移身份、RLS harden、runtime DDL 拒绝、跨租户探针、API readiness 和合成 vertical slice，参考部署 Compose 固定迁移 → RLS harden → RLS verify → API → Worker 顺序及真实 OIDC 边界；新增 [Kubernetes reference profile](../deploy/kubernetes/README.md)，把同一契约映射为 migration Job、RLS check Job、API/Worker、文件 Secret、非 root、探针、资源和滚动更新。下一步仍是在选定平台补 Secret/TLS、日志/指标/追踪、备份恢复、资源限制和滚动/回滚矩阵；不把本机 Compose 或 `kubectl --dry-run` 验收写成 Kubernetes 或生产完成度。

目标部署的隔离基线见 [ADR-0015](decisions/0015-target-deployment-and-rls-boundary.md)：默认参考是 Kubernetes + 托管 PostgreSQL，runtime 身份不得拥有 `BYPASSRLS`，所有租户事务先设置事务本地上下文，再由 RLS 做纵深防御。迁移 023、runtime 角色属性、连接池上下文清理、镜像内 `FORCE RLS` harden/verify 和 integration 双租户验收已经落地；目标环境的实际 Job 运行、真实 IdP、备份/队列复核和平台演练仍待完成，因此不把当前结果写成生产环境已验收。

### P4：验证模型判断层

先固定 provider-neutral 的 `JudgmentGate` 接缝和审计字段，再用可替换的测试 provider 验证任务拆分、预算、超时、重试、降级和人工接管。具体模型只在真实业务 Harness、证据投影和安全阈值稳定后进入 shadow 回放；模型只能提供语义建议，不能补造证据、改变租户/订单、绕过硬证据门、替代人工审批或执行外部动作。当前产品路线不绑定具体判断模型。

### P5：外部动作与生产运营

完成真实动作 provider 的幂等、UNKNOWN 对账、撤销/补偿、额度和支付策略，接入真实 IdP/RLS、沙箱、密钥轮换、容量压测、PITR 和异地恢复。在这些验收完成前，公开版默认保持无不可逆副作用。

## 6. v0.1 完成判据

- 新贡献者只按 README 就能安装并运行 local profile；
- local profile 能走完受理、等待、恢复、证据评定、人工决定和事件回放；
- 业务内核、provider 契约、失败语义和权限边界都有可定位文档；
- 重复消息、重复观察、旧 Worker、中途崩溃和跨租户访问有 PostgreSQL 验证；
- 默认配置不会把本地数据或测试动作发送到外部系统；
- CI 覆盖离线检查、PostgreSQL、打包、类型和前端构建，跳过项有实际原因；
- LICENSE、SECURITY.md、CONTRIBUTING.md、CHANGELOG.md、环境模板和仓库清理完成，发布包携带许可证与第三方声明；
- 文档逐项标明哪些已实现、哪些只到适配就绪、哪些已在目标环境验收；
- 发布说明明确当前验证环境、已知限制和从 local/integration 到 deployment 的下一步。

## 7. 真实落地路径

```text
local profile
    ↓  契约与恢复验证
integration profile
    ↓  身份、供应商、EGM、模型和动作联调
deployment profile
    ↓  容量、备份、观测、权限与故障演练
试点/生产环境
```

每一步都保留同一套业务状态和证据契约；变化集中在 provider、配置、密钥和运维策略。这样开源仓库既能独立运行，也能成为真实项目的起点。

## 8. 真实风险

- 本地 profile 运行成功不等于目标环境已验收，发布说明必须附环境和命令证据；
- 真实供应商、EGM schema 和模型 provider 会演进，接入必须经过版本化适配器；
- 外部动作的超时结果不能直接重放，必须使用 Action Ledger、幂等键和对账；
- EGM 是受控证据记忆组件，Aftercare 仍拥有业务状态、审批和动作权威；
- PostgreSQL 集成通过不等于生产 RPO/RTO、容量或成本结论，目标环境需要重新测量；
- 许可证、第三方模型权重和 EGM 的来源与许可在发布前单独核对；当前 EGM 固定提交已有 MIT 元数据与独立 `LICENSE`，后续升级仍需重新审计。
