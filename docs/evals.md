# A0-03 合成案件与确定性评测

`evals/` 是不调用模型、网络、数据库、供应商或沙箱的离线回归基线。案件全部由
`evals/cases/catalog.py` 在运行时构造，原文是合成占位文本；预期结果锁定在
`evals/expectations/v1.json`。

运行：

```powershell
.venv\Scripts\python.exe -m evals.runner
.venv\Scripts\python.exe -m pytest -q tests/evals
```

评测器把模型提案先经过 `InvestigationProposal` 边界校验，再将完整授权观察集交给
`assess_investigation`。每个结果记录 disposition、缺失材料、冲突、不可用证据或
结构化错误码，并输出稳定 SHA-256 digest，便于发现契约行为变化。`prompt-injection`
案例验证自由事实/指令字段会在模型输入边界被拒绝；它不代表真实模型的抗提示注入能力。

当前 v1.1 案件覆盖：正常建议、材料缺失、承运商签收与买家未收冲突、买家前后陈述冲突、
订单/买家陈述陈旧、跨订单、跨租户、状态不明确、重复证据和伪造指令。`RECOMMENDATION_READY` 仍只是有来源的建议
入口，不授权退款、补发、关单或任何外部副作用。

该基线用于确定性工程回归，不是业务准确率、模型质量、并发能力或生产 SLA 评测。后续
A3-04 的离线部分见下节；真实模型效果与真实账单仍需在有凭证的环境单独报告。

## A3-04 闭环评测（离线部分）

`evals/loop.py` 在 A0-03 的评估之上补上模型边界与运行闭环：脚本化 Responses client
返回一个 provider 形状的 `function_call`，`ResponsesAdapter` 在工具白名单下归一化并保持
`store=false`，`ModelUsageBudget`/`ModelPricing` 按整数 token 结算，`parse_investigation_proposal`
再把模型 JSON 解析成唯一允许的 `InvestigationProposal`，最后交给同一套确定性评估。

运行：

```powershell
.venv\Scripts\python.exe -m evals.loop
.venv\Scripts\python.exe -m pytest -q tests/evals
```

每个案件记录 disposition、引用来源、未被引用的接受项、未知引用、错误码和 token/成本；
报告另外给出闭环步数、工具调用数、是否从检查点恢复、以及稳定 SHA-256 digest。当前 14 个
案件的结果是：1 个 `recommendation_ready`（引用 `carrier`）、6 个转人工复核或补材料、
3 个结构化错误（跨订单、跨租户、重复证据）、1 个模型边界拒绝、1 个冲突。

限制必须同时读：

- 报价是**合成单价**（3/15 micro-USD per token），只证明计费路径和回归稳定性，不是
  provider 账单，也不是模型效果；真实单价必须按账单校准后才能用于成本结论。
- 脚本化模型返回的是案件目录中已有的提案，因此不代表真实模型的准确率或抗提示注入能力。
- 等待/唤醒/审批的持久语义仍在 PostgreSQL 集成测试中验证；本评测不覆盖租约、并发、
  故障恢复或生产 SLA。
