# Aftercare Agent

面向跨境电商售后场景的 Agent 参考架构，探索多租户并发、持久工作流、沙箱执行与故障恢复。

An after-sales agent architecture blueprint exploring durable workflows, sandboxed execution, and multi-tenant concurrency.

> 当前能力：参考架构 + 可安装的 EGM 证据适配层。Python 版本、依赖锁和离线测试入口已建立；完整 Agent 服务、真实售后连接器、生产部署和性能压测仍未完成。

## 要解决什么问题

从买家反馈“未收到货”开始，一个售后任务可能需要查询订单和物流、分析附件证据、请求补充材料、提出处理建议、等待人工审批，并在数小时或数天后继续跟进。

当多个商家的工单同时运行时，系统不仅要决定下一步调用什么工具，还要回答：

- 哪个 Worker 当前有权继续执行？
- 等待买家或人工审批时，是否还占用沙箱？
- 工具已经成功、但 Worker 在保存回执前崩溃，怎样避免重复操作？
- 如何隔离商家数据，并限制模型、工具与沙箱的资源使用？

Aftercare Agent 以这些问题为主线，而不是把聊天循环包装成一个已经成熟的企业平台。

## 从这里开始

| 文档 | 内容 |
|---|---|
| [当前工程状态与接手点](docs/project-status.md) | 压缩/换会话后先读：真实基线、任务状态、验证、未提交变化和下一步 |
| [工程执行计划](docs/engineering-plan.md) | 稳定任务 ID、A0–A3/B/C/D 依赖、目标模块和验收条件 |
| [技术栈与工程约定](docs/tech-stack.md) | Python/TypeScript/SQL、版本锁定、目录、测试和部署策略 |
| [本地开发、测试与打包](docs/development.md) | 当前可执行的 uv 安装、质量检查、sdist/wheel 和仓库外安装验收 |
| [运行时契约 v1](docs/contracts/runtime-v1.md) | 身份、Case/Run、执行权、检查点、工具请求、等待和事件的规则；PostgreSQL 执行实现见 persistence |
| [调查证据契约 v1](docs/contracts/investigation-v1.md) | 订单/物流/买家陈述、可信来源、证据新鲜度、引用和人审边界；EGM 调查 schema 仍待固定 |
| [A0-03 合成案件与确定性评测](docs/evals.md) | 12 个合成售后案件、固定预期和离线评测；不调用模型或真实业务系统 |
| [PostgreSQL 持久化骨架](docs/persistence.md) | 迁移、租约/fencing、调度队列、检查点和调查观察账本；完整运行时仍未完成 |
| [API 与认证边界](docs/api-auth.md) | FastAPI 受理、Run/事件读取与 Review/Approval operator 控制面；真实 OIDC 仍待接入 |
| [SSE 事件回放](docs/events.md) | A3-03 case-scoped SSE replay、`Last-Event-ID` 续传、持久事件游标与有界 PostgreSQL tail |
| [Harness 运行说明](docs/harness.md) | A1-03 固定只读执行链、预算边界与检查点恢复；不调用真实模型或业务动作 |
| [Human Review 边界](docs/reviews.md) | `HUMAN_REVIEW` 的持久请求、人工决定、恢复和取消语义 |
| [Responses 模型适配边界](docs/model-adapters.md) | A3-01 严格解析、工具白名单、托管工具事件、provider 错误脱敏与 token/cost budget |
| [Inbox/Outbox 说明](docs/events.md) | A2-01 PostgreSQL 事务内事件、来源幂等与消费者去重；外部 Broker 尚未接入 |
| [Action Ledger 与审批门禁](docs/actions.md) | B-01 台账与 B-02 审批参数/身份/策略/有效期绑定；真实供应商尚未接入 |
| [跨实例执行准入](docs/admission.md) | A2-03 全局/租户执行槽、数据库租约心跳和按 Run 重试预算 |
| [本地 Compose](deploy/compose/README.md) | A1-04 开发环境：PostgreSQL + API，Worker 可选 profile；不含模型或真实动作 |
| [合成 Aftercare 演示](deploy/compose/README.md#合成-aftercare-演示) | 一条命令运行受理、等待/唤醒、检查点恢复、证据评估和 fake 审批动作；只写合成数据 |
| [Agent 执行与恢复规则](AGENTS.md) | 恢复阅读顺序、状态维护和授权/安全边界 |
| [工程总设计与 Mermaid 架构图](docs/system-design.md) | 下一步怎样建设：部署、Session/Run、并发、持久事件、沙箱、记忆与 API；区分已实现和目标设计 |
| [完整技术文章](docs/architecture.md) | 从 Demo 到企业级：跨境电商售后 Agent 的并发、沙箱与故障恢复设计 |
| [实现路线图](ROADMAP.md) | 分阶段目标、交付边界与验收条件 |
| [EGM 0.6 嵌入式接入](docs/integrations/egm-embedded.md) | 当前默认方案：Worker 内嵌应用层、共享 PostgreSQL、事务和并发验证 |
| [调查 EGM 适配](docs/integrations/egm-investigation.md) | A3-02 可信观察写入、PostgreSQL 观察账本、完整观察集确定性评估与模型字段边界 |
| [沙箱控制面契约](docs/sandbox.md) | C-01 Fake Provider、分配幂等、fencing、产物预算和销毁确认 |
| [可观测性边界](docs/observability.md) | C-03 OTel-friendly tracing、Outbox 发布 span、脱敏与审计边界 |
| [嵌入式架构决策](docs/decisions/0002-embedded-egm.md) | 代码独立不等于服务独立；模块、存储、业务权威各自的边界 |
| [EGM 接入决策](docs/decisions/0001-evidence-gated-memory.md) | 有条件采用 Evidence-Gated-Memory，明确证据门控与业务权威的边界 |
| [EGM 代码评估与验证](docs/research/evidence-gated-memory.md) | 固定提交的源码核查、65 项测试与三个合成边界探针 |
| [EGM 0.5.0 服务接入](docs/integrations/egm-service.md) | 修复后的契约、认证、事务幂等、并发与离线集成验收；本地源码尚未发布 |
| [沙箱与 Harness 研究](docs/research/sandbox-harness-grok.md) | E2B、Kubernetes Agent Sandbox、Harness 放置与 Grok Bot 的公开事实 |
| [持久运行时研究](docs/research/durable-runtime.md) | 消息语义、Outbox、租约、fencing 与 OpenTelemetry |
| [ACP 与记忆研究](docs/research/memory-acp.md) | 协议边界、Letta 与 TencentDB Agent Memory |

## 参考架构的起点

业务状态、审批和操作台账由服务端管理；Harness 在 Worker 中推进任务。受控业务 API 通过工具网关访问，需要浏览器、脚本或不可信附件处理时再申请沙箱。证据与事实准入优先评估 Evidence-Gated-Memory（EGM），通过受限 Adapter 使用，不把 EGM 的任务图当作业务主状态机。

~~~text
消息 / Webhook / 客服界面
            │
      工单接入与认证
            │
  持久状态、事件与待执行任务
            │
    调度器 → Worker + Harness
                ├── 模型适配层
                ├── 受控业务工具 → 核验后的证据
                ├── Evidence Adapter → EGM 证据准入与解释图
                └── 按需沙箱
~~~

初始方案以 PostgreSQL 为持久化起点。E2B 与 Kubernetes Agent Sandbox 是按部署边界评估的候选方案；Kafka、NATS JetStream、Redis Streams、ACP 和完整记忆平台不是必须同时部署的依赖。

EGM 0.6 源码支持 Worker 内嵌 EvidenceApplication：HTTP 只是可选入口，权限、对象绑定、幂等、revision 与审计不再依赖单独部署服务。多机 Worker 共享 PostgreSQL；同工单短事务串行，不同工单可以并发。Aftercare 已实现 aftercare_agent/evidence.py 的受控退款证据适配层及合成回执测试，并补有 Action 审批参数门禁。它不执行真实退款；业务来源认证、审批等待唤醒、租约与外部动作幂等仍需实现。源码版本尚未发布 PyPI。

aftercare_agent/domain 已提供运行时与调查的 v1 类型、输入校验和确定性规则。它们能验证合法状态、引用和候选决策，但不执行数据库事务、创建 Worker 或调用模型；真正的多进程执行权、恢复与 EGM 调查接入仍按后续阶段实现。

详细取舍、API 协议对比及故障处理见[技术文章](docs/architecture.md)。

## 边界

- 这是售后业务参考项目，不是 Coding Agent，也不局限于承运商索赔。
- 模型可以提出行动建议，业务权限、金额、审批与外部动作由确定性规则控制。
- 退款、补发和对外承诺不得绕过必要授权；不确定的外部执行结果需要核对。
- 沙箱降低执行风险，但不能代替租户权限、网络控制和凭证管理。
- 文档中的数量与超时是说明方法的假设，不是推荐生产参数或实测结果。
- EGM 测试和本仓库适配层测试不代表完整 Aftercare 服务已实现或通过生产验收。

## 本地阅读与开发

~~~bash
git clone git@github.com:yushui2022/Aftercare-Agent.git
cd Aftercare-Agent
~~~

阅读文档不需要安装依赖。准备 Python 3.13.15、uv 和 Git 后，在仓库根目录运行：

~~~bash
uv sync --locked
uv run --locked pytest -q
~~~

依赖从 uv.lock 安装，EGM 固定为审核过的 Git 提交，无需并排克隆。首次安装需要网络；测试本身不需要模型 API Key 或真实业务凭证。Windows 的 G 盘缓存配置、解释器安装、Ruff/mypy 与打包验收见[开发指南](docs/development.md)，适配器用法见[嵌入式接入](docs/integrations/egm-embedded.md)。完整业务服务仍未交付；本地可用的合成纵向演示见 [Compose 文档](deploy/compose/README.md#合成-aftercare-演示)。

## 参与与许可

欢迎提交针对架构、业务边界、资料准确性和故障场景的 Issue 或 Pull Request。提交前请阅读[贡献说明](CONTRIBUTING.md)。

开源许可证尚待确定。仓库公开不等同于授予某一种开源许可证；确定后会添加 LICENSE。
