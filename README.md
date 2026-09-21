<div align="center">

# Aftercare Agent

**Evidence-gated after-sales operations for durable, reviewable business workflows.**

把订单、物流、客服、买方陈述和人工决定，组织成一条可恢复、可审计、可替换的售后处理链。

<p>
  <a href="https://github.com/yushui2022/Aftercare-Agent">Repository</a> ·
  <a href="docs/adapting-aftercare.md">接入自己的业务</a> ·
  <a href="docs/open-source-v0.1-readiness.md">开源与部署路线</a> ·
  <a href="CONTRIBUTING.md">贡献</a>
</p>

<p>
  <img src="https://img.shields.io/badge/status-v0.1--alpha-2563eb" alt="v0.1-alpha">
  <img src="https://img.shields.io/badge/license-MIT-16a34a" alt="MIT License">
  <img src="https://img.shields.io/badge/python-3.13-3776ab" alt="Python 3.13">
  <img src="https://img.shields.io/badge/postgresql-17-336791" alt="PostgreSQL 17">
</p>

</div>

![Aftercare evidence gate architecture](docs/assets/aftercare-evidence-gate.png)

> **当前定位：`v0.1-alpha`。** 这是一个面向真实跨境售后场景的企业级开源参考实现。Local profile 可以独立运行；真实身份、供应商、目标集群和外部动作仍需按部署路线单独验收。

> **准备拉下来改成自己的系统？** 先读 [改造与接入指南](docs/adapting-aftercare.md)。它说明哪些模块直接复用、哪些适配器需要替换、哪些风险必须先解决，以及何时可以接入真实退款或补发。

## 为什么需要 Aftercare

售后 Agent 的难点不只是“模型能不能回答”。一个真实工单可能要经过订单查询、物流核验、买方陈述、证据新鲜度检查、人工审批和外部动作；中间还可能遇到重复 Webhook、证据冲突、Worker 崩溃、供应商超时和租户权限变化。

Aftercare 把这些责任明确分开：

| 责任 | 谁负责 |
|---|---|
| Case、Run、等待、恢复和执行权 | 持久化业务内核 |
| 来源、版本、新鲜度、撤回和引用 | 证据账本与确定性证据门 |
| 语义分类、解释和处理建议 | 可替换的模型适配层 |
| 冲突、缺证据和高风险决定 | 人工 Review / Approval |
| 退款、补发、通知和支付 | 带幂等、回执和对账的 Action Provider |
| 记忆和上下文投影 | 受控 EGM 适配层 |
| 租户、身份、RLS 和发布证据 | 部署与安全边界 |

模型可以提出建议，但不能制造证据、改变租户边界、绕过证据门或直接执行外部动作。EGM 是证据记忆组件，不是 Aftercare 的业务状态机。

## 一眼看懂

~~~text
客户消息 / Webhook / 客服界面
             │
             ▼
      认证与租户边界
             │
             ▼
      Case / Run / 事件
             │
             ▼
  订单、物流、买方证据接入
             │
             ▼
    确定性证据门 + EGM
        │             │
        │             └── 缺证据 / 冲突 → 人工 Review
        ▼
    模型语义建议
             │
             ▼
 Approval → Action Ledger → 受控 Provider
~~~

核心原则只有一句话：

> **模型负责理解，证据门负责准入，人工负责高风险决定，Action Ledger 负责外部动作的可追溯性。**

## 你可以直接获得什么

- **持久工作流**：Case/Run、租约、fencing、检查点、等待、恢复和事件回放。
- **证据门**：订单、物流、买方陈述和客服事实的来源、时间、新鲜度、冲突和撤回。
- **人工协作**：Review、Approval、有效期、权限绑定和恢复语义。
- **受控动作**：Action Ledger、稳定幂等键、供应商回执和 UNKNOWN 对账边界。
- **EGM 接缝**：Worker 内嵌、租户/Case 绑定的证据记忆适配，不把 EGM 当业务权威。
- **多租户安全**：CaseGrant、撤销判定、PostgreSQL RLS、migration/runtime 身份分离。
- **部署护栏**：不可变镜像、Secret 文件、PostgreSQL TLS、Kubernetes profile、PITR 和发布证据。
- **可替换 provider**：订单、物流、身份、模型、沙箱、动作和通知都通过边界接入。

## 当前成熟度

| 能力 | 状态 | 说明 |
|---|---|---|
| 业务内核 | 已实现，持续加固 | PostgreSQL 持久语义、恢复和并发边界已建立 |
| 调查证据 | 已实现，持续加固 | 有订单/物流/买方事实、来源授权、版本、新鲜度和撤回 |
| EGM 接入 | 已接入 | 使用受控 Adapter，Aftercare 仍拥有业务状态权威 |
| Review / Approval / Action Ledger | 已实现 | 默认无副作用；真实 provider 需要单独联调 |
| Local profile | 可运行 | 合成案例、可控连接器、无客户凭证 |
| Integration profile | 适配就绪 | 需要测试租户、真实 IdP 和来源系统 |
| Deployment profile | 参考实现 | 需要在目标 Kubernetes / PostgreSQL 环境实测 |

本地最近一次全量回归为 **671 passed，144 skipped，2 warnings**。远程 CI 和目标环境证据仍是发布门的一部分；当前版本不宣称生产已验收。

## 5 分钟开始

### 环境

- Python 3.13.15
- [uv](https://docs.astral.sh/uv/)
- Git
- Docker（只在运行 Compose 时需要）

### 安装、测试

~~~bash
git clone https://github.com/yushui2022/Aftercare-Agent.git
cd Aftercare-Agent

uv sync --locked
uv run --locked pytest -q
~~~

测试不需要模型 API Key、客户凭证或真实业务系统。

### 启动 Local profile

~~~powershell
Copy-Item .env.example .env
docker compose --env-file .env -f deploy/compose/docker-compose.yml up --build
~~~

Local profile 使用合成数据和可控 provider，不会执行真实退款、补发、支付或外发通知。完整演示见 [Compose 文档](deploy/compose/README.md)。

## 拉下来之后如何改成自己的业务

推荐按这个顺序，而不是先改模型提示词：

1. **固定基线**：记录上游 commit、`uv.lock`、EGM commit、schema 和 PostgreSQL 版本。
2. **定义业务词汇**：把自己的售后单、调查、订单事实、人工审核和外部动作映射到 Case、Run、Evidence、Review 和 Action。
3. **接入来源**：在 connector / source adapter 中完成验签、租户绑定、规范化和来源幂等。
4. **定义证据门**：明确来源可信度、事件时间、采集时间、新鲜度、撤回和冲突分支。
5. **接入模型**：让模型引用现有证据 ID，只输出契约允许的建议；超时、预算耗尽和格式错误都要能降级。
6. **保留人工路径**：缺证据、证据冲突、策略漂移和高风险动作默认进入 Review。
7. **最后接入 Action Provider**：先用 Synthetic provider，完成 UNKNOWN 对账、幂等和回执后再接真实动作。
8. **按层验收**：领域契约 → PostgreSQL / RLS → 模型与人工 → staging → 小流量真实动作。

详细的文件位置、示例和风险清单见 [改造与接入指南](docs/adapting-aftercare.md)。

## 三种运行配置

| Profile | 适合什么 | 能证明什么 | 不能证明什么 |
|---|---|---|---|
| **Local** | 阅读、开发、CI、故障复现 | 业务契约、证据门、人工流程和基础恢复 | 真实供应商、容量、生产权限 |
| **Integration** | 测试租户和连接器联调 | IdP、来源映射、EGM、模型预算和失败语义 | 目标集群 RPO/RTO、真实动作 |
| **Deployment** | staging / 目标环境 | migration、RLS、Secret、TLS、PITR、回滚和运行观测 | 没有实际证据的生产结论 |

Deployment profile 要求 migration/runtime 数据库身份分离，数据库连接使用 `sslmode=verify-full`，发布证据必须绑定不可变镜像和目标环境检查。见 [Deployment profile](docs/deployment.md) 和 [Kubernetes reference profile](deploy/kubernetes/README.md)。

## 真实部署路线

~~~text
Local
  ↓  契约、恢复和证据门
Integration
  ↓  身份、来源、EGM、模型和沙箱
Staging
  ↓  RLS、Secret、备份、PITR、回滚和观测
受控生产
  ↓  小流量动作、对账、人工接管
~~~

在开启退款、补发、支付或通知前，至少要完成：

- 真实 OIDC、CaseGrant、撤销和 Secret rotation；
- 订单/物流/客服来源的原生验签、重放保护和错误映射；
- PostgreSQL TLS、migration/runtime role、RLS 和跨租户探针；
- Worker 等待、崩溃恢复、旧 fencing 拒绝和重复事件验证；
- Action Provider 的幂等、UNKNOWN 对账、额度和补偿路径；
- PITR、回滚、队列告警、日志/指标/追踪和目标环境证据归档。

## 项目结构

~~~text
aftercare_agent/
├── domain/          # Case、Run、Evidence、Review、Action 等领域契约
├── persistence/     # PostgreSQL、迁移、租约、事件、RLS、队列和台账
├── investigation/   # 订单/物流/买方调查和 EGM 适配
├── connectors/      # 来源连接器、规范化、验签和幂等
├── model_adapters/  # 模型请求、预算、策略版本和传输边界
├── actions/         # Action Ledger、Provider 和回执
├── runtime/         # Worker、Harness、判断和恢复
└── auth/            # 身份、CaseGrant、撤销和租户边界

deploy/              # Compose、Kubernetes、preflight、PITR 和发布证据
docs/                # 契约、决策、运维、接入和开源路线
tests/               # 领域、恢复、PostgreSQL、部署和契约回归
web/                 # 运营工作台
~~~

## 文档入口

| 你要做什么 | 从这里开始 |
|---|---|
| 改造成自己的业务 | [改造与接入指南](docs/adapting-aftercare.md) |
| 了解当前进度 | [项目状态台账](docs/project-status.md) |
| 了解系统设计 | [系统设计](docs/system-design.md) · [架构文章](docs/architecture.md) |
| 理解调查证据和 EGM | [调查契约](docs/contracts/investigation-v1.md) · [EGM embedded](docs/integrations/egm-embedded.md) · [调查 EGM](docs/integrations/egm-investigation.md) |
| 接入来源系统 | [Source Webhook](docs/integrations/source-webhook.md) |
| 接入模型 | [Model adapters](docs/model-adapters.md) · [Provider 边界决策](docs/decisions/0011-model-provider-boundary.md) |
| 接入人工和外部动作 | [Reviews](docs/reviews.md) · [Actions](docs/actions.md) · [Action Provider](docs/integrations/action-provider.md) |
| 部署和运维 | [Deployment](docs/deployment.md) · [Secret rotation](docs/operations/secret-rotation.md) · [PITR](docs/operations/pitr-deployment.md) |
| 开源协作 | [CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md) · [CHANGELOG.md](CHANGELOG.md) |

## 当前边界

- 这是一个售后业务参考实现和企业接入底座，不是开箱即用的 SaaS。
- 默认配置不连接客户系统，也不执行不可逆外部动作。
- 合成数据和模拟连接器用于可复现开发，不代表业务内核只能处理演示案例。
- 模型、沙箱和连接器不能持有数据库连接、裸 EGM 对象、高权限 Principal 或供应商主密钥。
- EGM 负责受控证据记忆；Case、审批、动作台账和外部动作权威属于 Aftercare。
- 本地测试通过不等于目标环境通过容量、RPO/RTO、网络、身份和供应商验收。
- JEV 不属于当前 Aftercare 或 EGM 主线；模型通过 provider-neutral 接缝接入。
- 当前迁移只有向前路径；破坏性 schema 变更需要先完成 expand/contract 和跨版本兼容矩阵。

## 参与、许可和安全

欢迎围绕业务边界、证据语义、恢复故障、适配器契约和部署验收提交 Issue 或 Pull Request。

- 贡献流程：[CONTRIBUTING.md](CONTRIBUTING.md)
- 安全问题：[SECURITY.md](SECURITY.md)
- 版本变更：[CHANGELOG.md](CHANGELOG.md)
- 许可证：[MIT License](LICENSE)
- 依赖许可：[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)

EGM 使用固定、已审核的 Git 提交，并保留独立许可证和版本来源记录。修改锁文件或升级 EGM 时，请重新核对依赖许可和安全报告。
