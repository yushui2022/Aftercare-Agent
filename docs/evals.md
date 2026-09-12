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

当前 v1 案件覆盖：正常建议、材料缺失、承运商签收与买家未收冲突、陈旧证据、跨订单、
跨租户、状态不明确、重复证据和伪造指令。`RECOMMENDATION_READY` 仍只是有来源的建议
入口，不授权退款、补发、关单或任何外部副作用。

该基线用于确定性工程回归，不是业务准确率、模型质量、并发能力或生产 SLA 评测。后续
A3-04 应在此基础上增加 Harness/模型评测，并分别报告模型效果、成本和运行时故障指标。
