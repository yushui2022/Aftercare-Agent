# 实现路线图

当前状态：参考架构与可执行 EGM 证据适配层；下列业务阶段尚未整体验收。阶段表示依赖顺序，不代表发布日期或生产能力承诺。

已完成：历史源码评估、0.5 回执契约修复，以及 0.6 嵌入式应用层和 PostgreSQL 存储。Aftercare 的受限证据 Adapter 已有合成回执测试；真实业务运行时仍未实现。EGM 并发存储通过真实数据库测试，不等于 Aftercare 已有任务调度或租约。见[最新接入决策](docs/decisions/0002-embedded-egm.md)与[接入验收](docs/integrations/egm-embedded.md)。

## 执行入口

2026-09-07 起，阶段统一为 A0–A3/B/C/D，替代原来的单 Worker/多 Worker 独立分期。基础 fencing 和开发 Compose 前移；支付 Action Ledger 后移到受控动作阶段。

- [工程执行计划](docs/engineering-plan.md)：任务 ID、依赖、模块、验收与风险。
- [当前状态](docs/project-status.md)：唯一进度台账，含真实验证与下一步。
- [技术栈](docs/tech-stack.md)：语言、版本、依赖、目录与运行约定。
- [AGENTS.md](AGENTS.md)：压缩/换会话后的恢复与收尾协议。

本页只作阶段导航，不维护第二套完成勾选或测试数量。

## A0：工程基线与业务契约

建立可复现 Python 包、固定 EGM 提交、明确运行时/调查契约、合成案件与预期。现有退款证据适配器保持回归，不能冒用为通用订单/物流调查。当前已完成 A0-03：12 个合成案件和确定性离线评测可运行；退出条件是环境、接口和评测输入可复现，不要求真实模型凭证。

## A1：确定性持久运行骨架

实现最小 PostgreSQL 迁移、API/Worker、FakePlanner、检查点与基础 Run 租约/fencing，并交付最小 Compose。当前 A1-01、A1-02、A1-03 已完成，A1-04 已交付 Worker、Compose、CI 解释器修复并在 GitHub Actions run #44 通过；Linux/容器启动和完整重启矩阵仍待验收。租户授权从入口开始设计；生产身份未接通时只允许显式本地测试模式。退出条件是固定步骤可以恢复且过期执行者无法提交。

## A2：等待、可靠事件与跨实例恢复

实现 Wait、Inbox、每消费者去重、Outbox、定时扫描和原子唤醒；当前已完成 Inbox/Outbox 幂等持久化、Wait 注册/激活/早到回复/超时/唯一唤醒、gap 处理、跨实例限额与公平调度基础，并已提供有界 PostgreSQL SSE tail 和工作台消费路径。仍需完成选定 broker 的 live adapter、完整重启矩阵和生产运行验证；等待不得长期占用执行槽或数据库事务。

## A3：调查 Agent、证据与工作台

在确定性恢复基础上接 Responses、调查专用 EGM 适配和最小 React/TypeScript 工作台。当前已交付严格 Responses 边界、调查观察账本、Case-scoped SSE 续传和最小工作台；仍需验证完整工具请求、证据语义、真实认证、成本与质量，并保留 Fake 测试通道。EGM 与简单显式证据规则作对照，区分准入价值、语义校验和上下文成本。

整个 A 阶段只生成处理建议与通知草稿，使用模拟业务连接器；不真实退款、补发或发邮件。模型不填写高信任来源、观察时间、TTL 或高权限身份。

## B：受控外部业务动作

实现 Action Ledger、审批绑定、稳定幂等键、支付聚合约束、额度预留、派发和 UNKNOWN 核对，随后接已授权供应商测试环境。当前已完成审批门禁、绑定 Wait 的 Inbox 原子唤醒、显式合成身份下的 operator 审批决定 API、provider-neutral JWT/JWKS Bearer 验签入口、最小 PostgreSQL CaseGrant、RFC 7662 撤销判定和 Case 授权管理 HTTP（[ADR-0004](docs/decisions/0004-case-grant-administration.md)）和租户工单发现（[ADR-0005](docs/decisions/0005-access-administration-discovery.md)）；真实 IdP 权限映射与演练、RLS、支付聚合和供应商对账仍待实现。通知外发同样需要可靠动作语义。依赖 A2 执行基础，完整业务闭环验收依赖 A3。

## C：按需沙箱与试点部署

已完成 C-01 的 Fake SandboxProvider 生命周期契约；下一步选择一个通过部署/驻留审查的真实后端，补资源租约、隔离/出网、短期能力、对象存储、暖池、孤儿回收与 OTel。可在 A2 后与 B 并行；真实业务动作试点仍需 B 验收。K8s 按实际需要采用，不是默认必须安装。

## D：生产准入、容量和有依据的扩展

依据启用功能完成真实认证/渠道审查、安全检查、备份恢复、保留删除、RPO/RTO、压测、成本与人工接管。备份、恢复演练、保留删除、恢复点之后的外部动作核对，以及按部署预算的新鲜度检查（演练记录写回备份目录）已由 `aftercare-backup` 交付并留下本机实测（[ADR-0008](docs/decisions/0008-backup-and-restore-drills.md)、[ADR-0009](docs/decisions/0009-backup-freshness-and-drill-records.md)、[运维文档](docs/operations/backup-restore.md)）；WAL 归档/时间点恢复、异地副本、备份加密、按部署目标的 RPO/RTO 数值与调度接线仍待完成。真实接入的授权和数据检查必须在调用前完成，不能延后到最终上线日。

Kafka、NATS JetStream、Redis Streams、ACP、Letta/TencentDB Agent Memory、额外模型路由仍是候选能力。只有确认解决了实际问题，并说明迁移、运维成本与验收方式后才引入。

Session transcript references 已补入 A1 持久运行骨架：会话消息使用租户/Case/Session
复合范围、连续序号和幂等重放；消息原文留在授权 artifact store，不能把供应商会话
状态直接当作 Aftercare 的恢复权威。详见 [Session memory](docs/session-memory.md)。
