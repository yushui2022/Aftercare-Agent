# Fake Harness 运行说明

`aftercare_agent.runtime` 是 A1-03 的最小执行控制器，用来先验证售后 Agent 最容易出错的几个边界：恢复位置、工具参数、预算和 scope。它不是生产 Worker，也不执行真实业务动作。

## 固定执行链

默认 `FakePlanner` 只产生三步：

1. `lookup_order`：只读订单查询；
2. `lookup_tracking`：只读物流查询；
3. `request_material_draft`：提出一项补充材料草稿。

每个请求都先经过 `validate_tool_request`。租户、工单、订单和高信任来源不从模型参数读取，而由可信运行时绑定。第三步使用固定的 `confirm_address` 问题，防止 Fake 流程把自由文本误当成业务事实。

## 检查点与恢复

`run_fake_harness()` 每推进一步就生成新的 `Checkpoint`：

- `next_step` 表示下一次应执行模型、工具或结束；
- `remaining_budget` 保存模型步、工具步和截止时间；
- `tool_results` 只记录确定性 artifact 引用；
- `checkpoint_version` 单调递增；
- `tenant_id/case_id/run_id` 必须与恢复请求完全一致。

调用方可以用较小的 `max_steps` 主动让出执行权，再把返回的 checkpoint 交给下一次调用。这个过程没有长数据库事务，也没有租约；生产版本必须由 A1-01 的 Run claim/fencing 和持久化 Worker 包住同一段推进逻辑。当前 `runtime.worker.run_once()` 已提供这个最小接线：领取后读取最新检查点，执行 Harness，在新的短事务中按 fencing token 保存，并把未完成切片转回 `READY`。

## 当前明确不做的事情

- 不调用 OpenAI/Anthropic 或其他真实模型；
- 不访问订单、物流、支付、邮件或沙箱；
- 不写 PostgreSQL，不模拟外部动作成功；
- 不替代 Wait/Inbox/Outbox、调度器和 Action Ledger。

因此 Fake Harness 的价值是把“模型循环能够安全暂停并恢复”的纯规则固定下来。A1-04 已提供一次性 Worker CLI、`run_next()` 的 `SKIP LOCKED` 领取原语和 `worker` Compose profile；A2 再加入长运行调度、等待、消息去重和跨实例唤醒。

## 验证命令

在仓库根目录执行：

```powershell
.venv\Scripts\python.exe -m pytest -q tests/test_fake_harness.py
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy aftercare_agent tests evals
```
