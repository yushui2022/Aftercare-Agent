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
- `next_step=wait` 时，`resume_next_step` 固定记录结算后的继续阶段；
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

因此 Fake Harness 的价值是把“模型循环能够安全暂停并恢复”的纯规则固定下来。A1-04 已提供一次性 Worker CLI、`run_next()` 的 `SKIP LOCKED` 领取原语和 `worker` Compose profile；A2-02 现在补上了可停止的 `run_daemon()` 轮询和独立连接租约心跳。

## 合成售后纵向切片

`SyntheticAftercareFlow` 把上述边界接成一条可复现的验收路径：

1. `admit()` 在一个短事务内创建 Case、Session 和 READY Run；
2. `pause_for_customer()` 先领取 Run，再在事务外运行两步 Fake Harness，随后用另一短事务保存 checkpoint、注册并激活 `WAITING_INPUT`；
3. `reply()` 以受信任的渠道适配器身份写入 Inbox，按 `wait_id/generation/correlation_key/condition_version` 原子结算 Wait 并唤醒 Run；
4. `resume()` 由另一 Worker 重新 claim；新写入的等待 checkpoint 会持久化 `resume_next_step=tool`，因此通用 Daemon 不需要为所有等待类型配置一个全局恢复阶段，最终保存 fenced checkpoint 并完成 Run；`assess()` 再从完整的可信观察账本计算 `RECOMMENDATION_READY`，每个结论都带有来源引用，并以不可变 assessment snapshot 写入 PostgreSQL。

这条切片证明的是“停机期间输入不丢、恢复不重复、模型预算不被等待重复消耗”，并把订单、物流、买家三类受信观察接入确定性评估。把承运商改为 `DELIVERED` 或提交不匹配引用即可得到 `HUMAN_REVIEW`，结果仍会保留完整引用和策略版本。当前仍未接真实模型、EGM 调查 schema、审批 API 或供应商副作用；完整 A3-04 评测还要报告真实模型效果/成本。旧的 schema-v1 等待 checkpoint 可能没有恢复阶段，Worker 会 fail-closed，只有可信调用方显式传入 `resume_next_step` 才能兼容恢复；新 checkpoint 不再依赖这个部署级参数。

## 常驻 Worker 与心跳

`run_daemon()` 每次只领取一个 READY Run；没有任务时按 `idle_sleep` 退避，收到进程的停止事件或达到测试用 `max_iterations` 后返回计数结果。数据库仍是调度权威，进程内循环和计数器不会替代租约。

Harness 在事务外执行时，`LeaseHeartbeat` 用独立数据库连接按租约约三分之一的间隔调用 `RunRepository.renew()`。心跳只延长当前 `owner/fencing_token` 的租约，不授予新权限；心跳报错后，切片拒绝保存，最终 checkpoint 事务仍会再次执行 fencing 校验。旧 Worker 的外部调用无法被 PostgreSQL 中断，所以连接器仍必须使用幂等键并在结果未知时走核对流程。

本地常驻入口使用：

```powershell
$env:DATABASE_URL = 'postgresql://...'
$env:AFTERCARE_TENANT_ID = 'tenant-a'
$env:AFTERCARE_WORKER_DAEMON = '1'
$env:AFTERCARE_WORKER_ID = 'worker-a'
$env:AFTERCARE_LEASE_SECONDS = '30'
$env:AFTERCARE_HEARTBEAT_SECONDS = '10'
# 可选：测试/批处理运行到固定轮数后退出；生产常驻不设置
# $env:AFTERCARE_MAX_ITERATIONS = '100'
python -m aftercare_agent.runtime.worker_cli
```

按 `SIGINT`/`SIGTERM` 停止会先结束轮询；正在执行的切片仍以租约和 fencing 保护提交。该入口目前使用 Fake Harness，尚不等于生产模型、连接器、消息 Broker 或沙箱服务。

## 验证命令

在仓库根目录执行：

```powershell
.venv\Scripts\python.exe -m pytest -q tests/test_fake_harness.py
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy aftercare_agent tests evals
```
