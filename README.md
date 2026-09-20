# Aftercare Agent

面向真实跨境电商售后场景的企业级开源参考实现：把持久工作流、证据门、人工协作和受控业务动作组合成一套可审计、可恢复、可替换的业务内核。

Aftercare Agent is an open-source reference implementation for durable, evidence-gated after-sales operations. It separates business authority from model suggestions, memory adapters, external providers, and deployment infrastructure.

![Aftercare evidence gate architecture](docs/assets/aftercare-evidence-gate.png)

> 当前版本定位为 `v0.1-alpha`：业务内核、开源文档和部署契约已经具备，真实供应商、身份系统和目标环境仍需按部署路线单独验收。Local profile 可以独立运行，但不代表某个生产环境已经上线。

## 这个项目解决什么问题

一次售后处理通常要同时面对订单、物流、买方陈述、客服记录、附件和人工决定。真正困难的地方不是“让模型回答一句话”，而是让系统在信息不完整、证据冲突、Worker 崩溃、重复消息和外部接口超时的情况下仍然保持可解释、可恢复和可审计。

Aftercare 把这些责任拆开：

- **业务状态**由 Case、Run、Review、Approval 和 Action Ledger 管理；
- **证据准入**由确定性证据规则和 EGM 适配层管理；
- **模型**只提出受约束的语义建议，不能制造证据、改变租户边界或绕过审批；
- **人工**可以接管冲突、缺证据和高风险动作；
- **外部动作**必须经过授权、幂等、回执和对账；
- **Worker**通过租约、fencing、检查点和持久等待恢复执行。

## 核心处理链

```text
消息 / Webhook / 客服界面
          │
          ▼
认证与租户边界 ──► Case / Run / 持久事件
                              │
                              ▼
                   订单、物流、买方证据
                              │
                              ▼
              确定性证据门 + EGM 调查适配层
                    │                  │
                    │                  └── 证据不足/冲突 → 人工 Review
                    ▼
              模型语义建议（可替换）
                              │
                              ▼
             Approval → Action Ledger → 受控 Provider
```

证据门决定“哪些事实可以进入判断”；模型可以帮助解释和排序，但不能把高置信度输出直接变成退款、补发或通知。EGM 是证据记忆组件，不是 Aftercare 的业务状态机。

## 当前成熟度

| 能力 | 当前状态 | 公开含义 |
|---|---|---|
| Case/Run、租约、fencing、等待/恢复、Inbox/Outbox | 已实现并持续加固 | 已有 PostgreSQL 持久语义和回归验证 |
| 订单/物流/买方证据账本 | 已实现并持续加固 | 有来源、版本、新鲜度、撤回和引用边界 |
| EGM 调查接缝 | 已接入 | Worker 内嵌受控 adapter；Aftercare 保留业务权威 |
| Review/Approval/Action Ledger | 已实现 | 默认使用无副作用 provider；真实动作需另行接入 |
| 模型适配与 Harness | 已有 provider-neutral 接口 | 模型版本、预算、延迟和降级仍需按环境评估 |
| Local profile | 可运行 | 合成案例和可控连接器，用于开发、CI 和问题复现 |
| Integration profile | 适配就绪 | 需要测试租户、真实 IdP/来源和供应商联调 |
| Deployment profile | 参考配置已具备 | 目标 Kubernetes、PostgreSQL、Secret、备份和回滚仍需实测 |

最新测试和未完成项以 [项目状态台账](docs/project-status.md) 为准。仓库不会把本地 Compose、静态 Kubernetes profile 或离线测试写成生产验收结论。

## 三种运行配置

### Local profile

使用合成 Case、可控 provider 和开发 Compose。它用于阅读代码、运行测试、复现故障、检查证据门和验证人工协作，不需要客户凭证，也不会执行真实退款或补发。

### Integration profile

使用 PostgreSQL、测试租户、契约测试 provider 和可选的真实模型端点。它用于验证认证、连接器映射、EGM schema、模型预算、沙箱资源以及外部动作的失败、重试和 UNKNOWN 语义。

### Deployment profile

使用目标环境的 IdP、RLS、真实业务连接器、密钥托管、沙箱、观测和备份策略。迁移身份与运行身份分离，数据库连接要求 `sslmode=verify-full`，发布必须绑定 preflight、租户隔离和 PITR 等机器证据。

详细边界见 [开源版与真实部署路线](docs/open-source-v0.1-readiness.md) 和 [Deployment profile](docs/deployment.md)。

## 快速开始

### 依赖

- Python 3.13.15
- [uv](https://docs.astral.sh/uv/)
- Git
- Docker（仅在运行 Compose 时需要）

### 安装和测试

```bash
git clone https://github.com/yushui2022/Aftercare-Agent.git
cd Aftercare-Agent
uv sync --locked
uv run --locked pytest -q
```

测试不需要模型 API Key、客户凭证或真实业务系统。依赖版本、打包、Ruff、mypy 和仓库外 wheel 验收见 [开发指南](docs/development.md)。

### 启动本地 Compose

```powershell
Copy-Item .env.example .env
docker compose --env-file .env -f deploy/compose/docker-compose.yml up --build
```

`.env.example` 中的身份和密码只适用于开发 Compose。Local profile 的完整演示、Worker profile 和停止方式见 [Compose 文档](deploy/compose/README.md)。

## 从哪里继续阅读

| 目标 | 文档 |
|---|---|
| 了解当前真实进度 | [docs/project-status.md](docs/project-status.md) |
| 了解开源版和生产路线 | [docs/open-source-v0.1-readiness.md](docs/open-source-v0.1-readiness.md) |
| 了解整体设计 | [docs/system-design.md](docs/system-design.md) · [docs/architecture.md](docs/architecture.md) |
| 了解执行计划 | [docs/engineering-plan.md](docs/engineering-plan.md) · [ROADMAP.md](ROADMAP.md) |
| 了解运行时契约 | [docs/contracts/runtime-v1.md](docs/contracts/runtime-v1.md) |
| 了解调查证据 | [docs/contracts/investigation-v1.md](docs/contracts/investigation-v1.md) |
| 了解 EGM 接入 | [docs/integrations/egm-embedded.md](docs/integrations/egm-embedded.md) · [docs/integrations/egm-investigation.md](docs/integrations/egm-investigation.md) |
| 了解模型边界 | [docs/model-adapters.md](docs/model-adapters.md) · [ADR-0011](docs/decisions/0011-model-provider-boundary.md) · [ADR-0013](docs/decisions/0013-versioned-model-strategy.md) |
| 了解人工和动作 | [docs/reviews.md](docs/reviews.md) · [docs/actions.md](docs/actions.md) · [docs/integrations/action-provider.md](docs/integrations/action-provider.md) |
| 了解真实部署 | [docs/deployment.md](docs/deployment.md) · [deploy/kubernetes/README.md](deploy/kubernetes/README.md) |
| 了解备份、观测和密钥 | [PITR 验收](docs/operations/pitr-deployment.md) · [观测接入](docs/operations/observability.md) · [Secret rotation](docs/operations/secret-rotation.md) |
| 贡献和安全 | [CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md) |

## 面向真实部署的路线

开源版和真实部署使用同一套业务契约，但按四个阶段推进：

1. **冻结开源候选版本**：整理依赖、许可证、第三方声明、文档、镜像身份和安全扫描结果，形成可复现的 release candidate。
2. **完成 integration 联调**：使用测试租户接入 IdP、订单/物流来源、EGM、模型和沙箱，验证验签、幂等、撤回、恢复和成本边界。
3. **完成 staging 验收**：在选定的 Kubernetes + 托管 PostgreSQL 环境运行 migration/RLS Job、API、Worker、Secret rotation、PITR、回滚和恢复演练。
4. **最后接入真实动作**：退款、补发、通知和支付动作必须具备稳定幂等键、UNKNOWN 对账、额度策略、人工审批和补偿路径。

模型选择放在证据契约和 Harness 稳定之后。模型只能通过 provider-neutral 接缝进入 shadow 回放或受控任务，不能成为安全批准的唯一依据；JEV 不属于当前 Aftercare 或 EGM 主线。

## 明确边界

- 默认配置不连接客户系统，也不执行真实退款、补发、支付或外发通知。
- 合成数据和模拟连接器是可复现的运行配置，不代表业务内核只能处理演示案例。
- 模型、沙箱和连接器不能持有数据库连接、裸 EGM 对象、高权限 Principal 或供应商主密钥。
- EGM 负责受控证据记忆；Case、Approval、Action Ledger 和外部动作权威属于 Aftercare。
- 本地测试通过不等于目标环境已经通过容量、RPO/RTO、身份、网络和供应商验收。
- 当前迁移只有向前路径；破坏性 schema 变更需要先完成 expand/contract 和跨版本兼容矩阵。

## 贡献、许可和安全

欢迎围绕业务边界、证据语义、恢复故障、适配器契约和部署验收提交 Issue 或 Pull Request。提交前请阅读 [贡献说明](CONTRIBUTING.md)，安全问题请按照 [SECURITY.md](SECURITY.md) 联系维护者。

项目使用 [MIT License](LICENSE)。运行依赖和前端直接依赖的版本化许可清单见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)；EGM 使用固定、已审核的 Git 提交，并保留独立许可证和版本来源记录。

版本变更和已知限制记录在 [CHANGELOG.md](CHANGELOG.md)。
