# ACP 与 Agent Memory 核实笔记

核实日期：2026-09-06。用于跨境电商售后 Agent 的设计参考，非已上线系统或实测报告。以下项目事实与架构建议分开陈述。

选型补充：当前优先评估 EGM 承担案件证据准入，Letta 与 TencentDB Agent Memory 保留为长期上下文与经验管理候选。见 [EGM 接入决策](../decisions/0001-evidence-gated-memory.md)及[代码验证记录](evidence-gated-memory.md)；并非计划把三套记忆运行时同时接入。

## 1. ACP：交互协议，不是任务队列

此处 ACP 专指 **Agent Client Protocol**。官方定位是编辑器／客户端与 Agent 的互操作协议，不是 Agent Communication Protocol，也不是消息中间件。自建售后平台只有在需要接入 ACP 兼容客户端／运行时时才有采用价值，并非企业级 Agent 必备组件。后一句为结合协议范围的架构判断。[官方介绍](https://agentclientprotocol.com/get-started/introduction)

按稳定 **v1** 描述：客户端发送 `session/prompt`；Agent 用 `session/update` notification 输出消息片段、计划等；一次 prompt 可以包含多次模型与工具交互。[Prompt Turn](https://agentclientprotocol.com/protocol/v1/prompt-turn)

`session/update` 内的 `tool_call` 和 `tool_call_update` 报告工具进度；需要用户许可时，Agent 发出带请求 ID 的 `session/request_permission`，客户端回传选择或取消。它不是“写一条日志就获得授权”。[Tool Calls](https://agentclientprotocol.com/protocol/v1/tool-calls)

v1 使用 UTF-8 JSON-RPC；stdio 中客户端启动 Agent 子进程，消息以换行分隔，stdout 只能包含协议消息，诊断日志可写 stderr。该页把 Streamable HTTP 标为草案，额外双向 transport 可以自定义，不能直接宣称远程传输已经全部标准化。[Transports](https://agentclientprotocol.com/protocol/v1/transports)

版本警告：官方已有 v2 Draft，prompt 生命周期和权限对象存在变化；本文不能把 v2 字段套入 v1 示例。[v2 草案公告](https://agentclientprotocol.com/announcements/acp-v2-draft)

协议支持具备相应能力的 Agent 用 `session/load` 加载并回放对话；所以不能说 ACP 完全不能恢复会话。[Session Setup](https://agentclientprotocol.com/protocol/v1/session-setup)

**售后系统设计判断：** 会话回放仍不等于业务恢复。另行持久化 `case_id/run_id/action_id/event_id`、业务状态转换和外部回执；审计记录保存谁批准了什么操作、参数摘要与政策版本。UI 收到“工具完成”不能直接证明退款成功，浏览器断连也不能撤销已经发生的外部操作。ACP 事件可供展示或转存，但协议不替代事务、幂等和业务审计设计；工具许可也不替代后端身份鉴权与退款额度校验。

## 2. Letta 与腾讯项目：记忆的实现并不只有一种

Letta 的 stateful agent 会持久保存记忆、消息和工具调用，重要记忆进入模型上下文；这说明“状态可保存”，不代表模型权重自动更新。[Stateful Agents，V1 SDK](https://docs.letta.com/v1-sdk/concepts/stateful-agents)

在其 **V1 SDK（官网已标 legacy）** 中，memory blocks 是持久、可编辑的上下文片段，附着后直接放入 prompt，无需检索；支持共享和只读块。[Memory blocks](https://docs.letta.com/v1-sdk/memory/memory-blocks)

Archival memory 是按需检索的长期存储；Agent 可以通过工具插入、语义搜索，开发者可管理和删除，不是整库常驻上下文。[Archival memory](https://docs.letta.com/v1-sdk/memory/archival-memory)

当前 Agent SDK 另有 Git 支撑的 MemFS：`system/` 文件每轮进入上下文，其他文件按需读取，不能把它与 legacy blocks／archival API 混写成同一套接口。[当前 SDK Memory](https://docs.letta.com/agent-sdk/memory)

本文“腾讯 Agent Memory”指 **TencentDB Agent Memory**，当前官方仓库位于 `TencentCloud/TencentDB-Agent-Memory`，旧 `Tencent/...` 地址仍出现在安装说明。其当前团队记忆方案组织 Chat Memory、Skill、Wiki、CodeGraph；引用时应写明仓库名称，避免与其他腾讯记忆或知识产品混淆。[官方仓库](https://github.com/TencentCloud/TencentDB-Agent-Memory)

当前 MemoryCore 的 L0→L3 对应原始对话、原子记忆、场景记忆、核心画像；它管理记忆及元信息，不负责运行／调度 Agent。当前 standalone 的抽取与归纳需要 LLM API，不能沿用旧搜索摘要“零外部 API”概括新版。[MemoryCore](https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/feat/server_team/MemoryCore/README_CN.md)

WeKnora 是另一套知识平台，不能因为同属腾讯，就当成上述 Agent Memory 项目。[WeKnora](https://github.com/Tencent/WeKnora)

## 3. EGM 的位置不同于一般偏好记忆

EGM 已实现证据引用、门控、时效与事实血缘，可用于限制当前案件中缺乏支持的结论；其 Fact 是通过特定规则的数据对象，不等于完整业务真值证明。

修复前 a16e3de 的退款门控没有验证回执正文成功状态或订单相等关系，task_id 也未过滤长期记忆。0.5 修复这些路径；0.6 进一步把授权、校验与重试协议抽成嵌入式应用层，并加入共享 PostgreSQL 存储。来源认证仍依赖可信连接器；task_id 过滤不是 ACL，可信宿主必须从认证结果构造 Principal。新旧版本边界见[嵌入式接入](../integrations/egm-embedded.md)，HTTP 不再是必选部署。

Aftercare 的适配层应优先实现来源认证、对象绑定、响应语义与访问范围校验，再将核验后的证据导入 EGM。EGM 只作受控、可重建的案件解释投影；审批、操作台账、租约与持久调度仍由 Aftercare 负责。

## 4. 售后系统的数据边界（设计建议）

| 数据 | 示例与责任 |
|---|---|
| 业务事实库 | 订单状态、退款金额、承运商回执；由权威系统与事务记录确认 |
| 执行 checkpoint | 当前步骤、待审核动作、幂等键、已完成调用；用于决定从哪里继续 |
| 证据准入与解释图 | EGM 的 Evidence、Claim、Fact、TaskGraph；用于检查声明的证据条件，不替代业务事实与执行权 |
| Agent memory | 客户语言偏好、审核过的处理经验、历史案件摘要；用于减少重复调查 |
| 本轮上下文 | 当前 prompt 中实际装入的事实、消息和检索片段；受 Token 上限约束 |

记忆是可修正的派生信息，不是退款账本。建议每条记忆保留租户／主体范围、来源引用、生成时间、有效期或失效规则、版本和审核状态；事实变化后使派生记忆失效。删除应覆盖原始记忆、向量、衍生摘要及缓存，审计记录采用独立保留策略。

隔离由服务端实施：租户身份来自已认证请求，不让模型自行提供可信 `tenant_id`；检索与共享块都执行 ACL。退款政策等权威规则保持受控只读，跨租户共享只使用经审核且脱敏的经验。客户邮件中的指令不能通过“写入记忆”升级成系统规则；记住“经理以前同意过退款”不等于本次获得授权，提交时仍需后端重新校验。
