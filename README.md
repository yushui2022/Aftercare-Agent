# Aftercare Agent

面向跨境电商售后场景的 Agent 参考架构，探索多租户并发、持久工作流、沙箱执行与故障恢复。

An after-sales agent architecture blueprint exploring durable workflows, sandboxed execution, and multi-tenant concurrency.

> 当前阶段：文档与架构设计。仓库尚未包含可运行的 Agent 服务，也没有生产部署或性能压测结果。

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
| [完整技术文章](docs/architecture.md) | 从 Demo 到企业级：跨境电商售后 Agent 的并发、沙箱与故障恢复设计 |
| [实现路线图](ROADMAP.md) | 分阶段目标、交付边界与验收条件 |
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

EGM 的独立源码已升级：原始回执状态和对象绑定、任务上下文隔离、事务幂等及租户/工单级 HTTP 服务已落地；目标版本 0.5.0 尚未发布。Aftercare 仍未安装运行依赖或实现业务 Adapter。接入采用受限 HTTP 契约，同宿主每工单串行，不共享裸 SQLite 给所有 Worker；业务来源认证、审批与外部动作幂等仍由 Aftercare 负责。

详细取舍、API 协议对比及故障处理见[技术文章](docs/architecture.md)。

## 边界

- 这是售后业务参考项目，不是 Coding Agent，也不局限于承运商索赔。
- 模型可以提出行动建议，业务权限、金额、审批与外部动作由确定性规则控制。
- 退款、补发和对外承诺不得绕过必要授权；不确定的外部执行结果需要核对。
- 沙箱降低执行风险，但不能代替租户权限、网络控制和凭证管理。
- 文档中的数量与超时是说明方法的假设，不是推荐生产参数或实测结果。
- EGM 评估记录中的测试来自独立上游库；它们不代表 Aftercare 已有可运行实现或通过生产验收。

## 本地阅读

~~~bash
git clone git@github.com:yushui2022/Aftercare-Agent.git
cd Aftercare-Agent
~~~

当前直接阅读 Markdown 即可，不需要安装运行时依赖，也不需要 API Key。后续实现的启动命令与环境要求会随代码一并提供。

## 参与与许可

欢迎提交针对架构、业务边界、资料准确性和故障场景的 Issue 或 Pull Request。提交前请阅读[贡献说明](CONTRIBUTING.md)。

开源许可证尚待确定。仓库公开不等同于授予某一种开源许可证；确定后会添加 LICENSE。
