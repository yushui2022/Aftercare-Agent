# Aftercare Agent 改造与接入指南

这份指南给准备把 Aftercare Agent 拉下来、改成自己业务系统的人使用。它说明哪些部分可以直接复用，哪些部分必须替换，改造过程中最容易出现什么问题，以及在接入真实数据和外部动作前要完成哪些验证。

Aftercare Agent 是一个可以被企业改造的售后业务内核，不是拿到仓库后填几个 API Key 就能直接上线的 SaaS。你可以复制、Fork、私有化部署和修改代码；项目使用 MIT License，但运行依赖和 EGM 固定版本的许可证仍需按 [第三方声明](../THIRD_PARTY_NOTICES.md) 一并保留。

## 先判断你要改哪一层

通常不需要从头重写整个项目。先把自己的需求分成三层：

| 层 | 你要放入的内容 | 通常改动的位置 |
|---|---|---|
| 业务输入 | 订单、物流、客服、买方陈述、附件、平台回调 | `aftercare_agent/connectors/`、`investigation/source_ingress.py`、来源 Webhook 适配器 |
| 业务判断 | 问题类型、证据新鲜度、冲突规则、人工门槛、处理建议 | `investigation/schemas/`、调查契约、证据评定和自己的策略模块 |
| 外部执行 | 退款、补发、通知、支付、工单更新 | `aftercare_agent/actions/`、`ActionProvider`、供应商回执和对账适配器 |

Case/Run、Worker 租约、fencing、等待/恢复、Inbox/Outbox、事件回放、Review/Approval、Action Ledger、租户边界和部署证据链属于业务内核。除非你的业务模型确实不同，否则先通过适配器扩展，不要直接改这些基础不变量。

## 从 Fork 到第一次本地运行

### 1. 固定自己的基线

先 Fork 或建立内部镜像，记录：

- 上游 commit SHA；
- `pyproject.toml` 和 `uv.lock` 版本；
- EGM Git commit 和 schema 版本；
- 计划使用的 PostgreSQL 版本；
- 目标 Python、Node 和容器平台版本。

不要一开始就把上游依赖改成浮动版本。业务改造和依赖升级分开，遇到问题才能知道是规则变化还是基础设施变化。

### 2. 先运行 Local profile

```bash
uv sync --locked
uv run --locked pytest -q
```

需要 Compose 时：

```powershell
Copy-Item .env.example .env
docker compose --env-file .env -f deploy/compose/docker-compose.yml up --build
```

Local profile 使用合成数据和可控 provider。它的作用是让你先理解 Case、Run、证据、Review、Approval 和 Action Ledger 的关系；它不会替你验证真实平台的字段、权限、签名、网络和供应商回执。

### 3. 建立自己的业务词汇表

在改代码前，先写一张小表，把业务术语映射到 Aftercare 概念：

| 你的系统 | Aftercare 概念 | 必须明确的内容 |
|---|---|---|
| 售后单/工单 | Case | 唯一标识、租户、订单绑定、关闭条件 |
| 一次调查 | Run | 输入版本、恢复方式、重试预算 |
| 订单/物流/客服事实 | Observation / Evidence | 来源、采集时间、事件时间、新鲜度、撤回方式 |
| 风险或缺证据 | Review | 谁可以处理、决定是否可撤回、等待如何恢复 |
| 退款/补发/通知 | Action | 幂等键、审批、回执、UNKNOWN 对账和补偿 |

如果一个字段同时被当成 Case version、EGM revision、Run fence 或外部动作幂等键，先停下来重新拆分。它们解决的是不同问题，不能互相替代。

## 修改业务识别和证据门

### 先定义输入契约，再写识别代码

不要让模型直接读取供应商原始 JSON 并决定业务状态。先把来源响应转换成版本化的领域事实，至少包含：

- `tenant_id`、`case_id`、`order_id`；
- `source_id` 和 `source_event_id`；
- 事实类型，例如订单状态、物流扫描、买方陈述、支付状态；
- `observed_at`、`occurred_at` 和来源时区；
- 原始响应摘要或 artifact 引用；
- 来源授权和验签结果；
- schema 版本、规范化版本和撤回语义。

调查证据的边界见 [investigation-v1](contracts/investigation-v1.md)，来源签名和幂等入口见 [source webhook 指南](integrations/source-webhook.md)。供应商字段变化时，优先在来源适配器中转换，避免把平台字段散落在 Worker、模型提示词和前端里。

### 把“识别”与“准入”分开

建议使用下面的顺序：

```text
原始响应
  → 验签、租户绑定、规范化
  → 写入 Aftercare 观察账本
  → 按来源、时间、新鲜度和冲突规则评定
  → 证据门决定可用事实集合
  → 模型解释、分类或提出建议
  → 人工/策略决定是否进入 Action
```

模型可以识别“买方说未收到货”“物流显示已签收”之间的语义关系，但不能凭模型置信度把冲突变成批准。证据门应能在模型不可用时继续给出 `accepted`、`rejected`、`stale`、`conflict` 或 `needs_review` 等确定性结果。

### EGM 应该放在哪里

EGM 适合作为受控的证据记忆和上下文投影层：

- Aftercare 先保存业务观察、来源授权、版本和撤回记录；
- 经过准入的证据再通过 `InvestigationEvidenceAdapter` 写入 EGM；
- 模型只能读取被授权、被投影的证据上下文；
- EGM revision 不替代 Run fencing、Case version 或 Action 幂等键；
- EGM 写入成功而 Aftercare 确认前进程崩溃时，必须使用 operation/revision 收据恢复。

嵌入式边界见 [EGM embedded 接入](integrations/egm-embedded.md) 和 [调查 EGM 适配](integrations/egm-investigation.md)。如果你的业务不需要证据记忆，仍可以先使用 Aftercare 的观察账本和确定性门，不要为了“使用了 EGM”而把业务状态迁入 EGM。

## 接入自己的模型

模型接入点是 provider-neutral 的。你需要实现或配置的是：

- 请求和响应的版本化 schema；
- 工具白名单和参数校验；
- token、金额、延迟和重试预算；
- provider 错误分类和超时；
- 模型策略 ID、配置版本和迁移规则；
- 降级到确定性规则或人工 Review 的路径。

模型不能：

- 自己填写高信任来源、观察时间、TTL、tenant 或 principal；
- 修改证据准入结果；
- 持有 PostgreSQL 连接、裸 EGM 对象或供应商主密钥；
- 直接调用退款、补发、支付或通知接口；
- 用“高置信度”绕过人工审批和 Action Ledger。

模型适配边界见 [model-adapters](model-adapters.md) 和 [模型 provider 决策](decisions/0011-model-provider-boundary.md)。真实业务上线前，应使用自己的固定案件集做 shadow 回放，至少记录准确性、拒绝率、转人工率、延迟、成本、超时和证据引用完整性。

## 接入真实外部动作

先保留 `SyntheticActionProvider` 跑通完整流程，再实现自己的 `ActionProvider`。真实 provider 至少要回答这些问题：

1. 同一个 `action_id` 或幂等键重试时，供应商会返回同一结果还是创建第二个动作？
2. 请求超时但供应商已经执行时，如何查询和对账？
3. 供应商返回 UNKNOWN 时，系统是否暂停后续动作并转人工？
4. 退款、补发和通知是否需要不同的审批和额度策略？
5. 供应商回执如何绑定订单、Case、租户和原始请求摘要？
6. 撤回、补偿和人工纠错如何留痕？

外部调用必须在数据库短事务之外执行；调用前写入 Action Ledger，调用后用 `mark_receipt()` 保存规范化回执。完整边界见 [Action Provider 回执](integrations/action-provider.md) 和 [Action Ledger](actions.md)。

## 真实部署前要替换的内容

Local profile 的配置不能直接复制到生产。至少需要替换：

| 项目 | Local profile | 真实部署必须提供 |
|---|---|---|
| 身份 | 合成 principal | OIDC/JWT、claims 映射、CaseGrant、撤销和密钥轮换 |
| 数据库 | 本地 Compose PostgreSQL | TLS、独立 migration/runtime role、RLS、备份和恢复策略 |
| 来源 | 合成连接器 | 原生验签、重放保护、网络出口、限流和错误映射 |
| 模型 | Fake 或测试 provider | 预算、密钥托管、版本迁移、审计和降级 |
| 沙箱 | Fake provider | 隔离、出网、资源租约、短期凭证和孤儿回收 |
| 动作 | Synthetic provider | 供应商幂等、UNKNOWN 对账、额度、补偿和人工接管 |
| 观测 | logging | 指标、日志、追踪、队列年龄和告警留存 |

部署身份、Secret、TLS、RLS、PITR 和回滚顺序见 [Deployment profile](deployment.md)；Kubernetes 参考清单见 [Kubernetes profile](../deploy/kubernetes/README.md)。

## 改造过程中最容易出现的问题

| 风险 | 常见错误 | 解决方式 |
|---|---|---|
| 业务语义错配 | 把平台字段直接当作“已退款”或“已签收” | 先定义来源事实和证据评定，再映射到业务状态 |
| 证据污染 | 模型补写来源、时间或 TTL | 来源字段只由可信 adapter 生成，模型只能引用证据 ID |
| 过期证据继续生效 | 只保存事实内容，不保存 `observed_at`/`occurred_at` | 把新鲜度和撤回作为契约字段，并在门控时重新评定 |
| 冲突被自动批准 | 买方陈述和物流状态冲突时仍走退款 | 冲突默认转 Review，除非有明确且可审计的业务规则 |
| 重试重复动作 | 用 Run ID 或 EGM revision 当外部幂等键 | 使用稳定 Action 幂等键，并保存 UNKNOWN 查询路径 |
| 跨租户泄露 | 从请求 body 或模型参数读取 tenant | tenant 和 principal 只来自认证上下文与 CaseGrant/RLS |
| 旧 Worker 写入 | 只检查 Run 是否存在，不检查 lease/fence | 每次可变写入都验证当前租约和 fencing token |
| 等待丢失 | 把等待放在进程内内存或长数据库事务里 | 持久化 Wait、generation、唤醒和消费应用记录 |
| EGM 与业务状态混淆 | 用 EGM revision 推断业务动作已完成 | Aftercare 账本、EGM 收据和供应商回执分别确认 |
| 迁移权限过大 | API/Worker 使用数据库 owner | migration role 与 runtime role 分离，runtime 拒绝 DDL |
| 本地通过误当生产通过 | 只跑 Compose 或静态 YAML 检查 | 在目标 staging 实际运行迁移、RLS、备份、恢复和回滚 |

## 推荐的验收顺序

不要一上来接真实退款。按下面的顺序逐层扩大范围：

### A. 领域契约验收

- 自己的订单、物流、客服和买方陈述能被规范化为版本化事实；
- 重复事件、同 ID 内容变化、旧事件和撤回都有明确结果；
- 证据过期、冲突、缺失和来源不可信会进入预期分支；
- 14 个以上合成案例覆盖低歧义、过期、缺证据和冲突场景。

### B. 持久运行验收

- PostgreSQL 迁移和 RLS 在测试租户上真实运行；
- Worker 在等待、超时、SIGTERM 和崩溃后能恢复；
- 旧 lease/fence 不能提交；
- Inbox、Outbox、观察导入和动作请求重复不会造成第二次业务效果。

### C. 模型和人工验收

- 模型只返回契约允许的结构，并且引用已存在的证据 ID；
- 模型超时、预算耗尽、格式错误会降级到 Review 或重试；
- 人工决定绑定正确的 tenant、Case、Review、策略版本和有效期；
- 模型建议不会直接关闭证据门或执行 Action。

### D. staging 验收

- 真实 OIDC、Secret manager、TLS CA 和 PostgreSQL runtime role 已接通；
- 真实来源 webhook 验签、网络出口和限流已验证；
- migration/RLS Job、readiness、Worker 恢复、Secret rotation、PITR 和回滚都有机器证据；
- 远程 CI 全绿，发布清单中的签名、registry 复扫和目标环境门状态明确。

### E. 真实动作小流量验收

- 先只开启低风险、可对账的动作；
- 退款、补发和通知分别设置审批、额度和暂停条件；
- 任何 UNKNOWN、证据冲突或策略版本漂移都进入人工队列；
- 每次真实动作都有 Action Ledger、供应商回执和可重放对账记录。

## 一个最小改造示例

假设你的平台需要处理“客户说包裹未收到”的售后：

1. 在来源 adapter 中接入订单查询、物流轨迹和客服消息，并把平台响应规范化为 `order_record`、`shipment_event`、`buyer_statement`。
2. 为每条事实保存来源、事件时间、采集时间、授权 scope、schema 版本和撤回条件。
3. 规定“物流已签收 + 买方未收到”是 `conflict`，默认进入人工 Review；不能让模型自行选择其中一条。
4. 让模型只解释冲突、列出需要补充的证据和生成通知草稿。
5. 人工批准后才生成补发 Action；Action Provider 使用稳定幂等键调用物流/仓储系统，并把回执写回台账。
6. 在 staging 反复测试重复 webhook、物流状态延迟、供应商超时、Worker 崩溃和人工撤回。

这类改造通常只需要新增或替换来源 adapter、调查 schema、证据规则、模型 provider 和 Action Provider；Case/Run、持久恢复、租户边界和审计结构应继续复用。

## 发布前检查表

- [ ] 已记录上游 commit、EGM commit、schema 和依赖版本。
- [ ] README、许可证、第三方声明、安全政策和变更记录已同步。
- [ ] 所有真实来源都有授权、验签、幂等和撤回方案。
- [ ] 证据门不依赖模型可用性，冲突和缺证据默认有人工路径。
- [ ] 模型没有数据库、EGM 写权限、业务高权限身份或供应商主密钥。
- [ ] Action Provider 通过 UNKNOWN 对账、幂等和回执契约测试。
- [ ] PostgreSQL migration/runtime 身份分离，TLS 和 RLS 已在 staging 实测。
- [ ] Worker 恢复、等待、租约、fencing 和跨租户访问已验证。
- [ ] Secret rotation、PITR、回滚和目标环境部署记录已经归档。
- [ ] 远程 CI 全绿；未通过的 Job 已修复或明确标记为发布阻塞项。

如果只是研究架构或改造 Local profile，不需要完成最后三项；如果要接入真实数据，就至少完成 A–D；如果要开启退款、补发或支付，则必须完成 E，并由业务负责人单独批准动作范围。
