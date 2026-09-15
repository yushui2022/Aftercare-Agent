# 连接池容量报告

本目录保存 `aftercare-capacity` 的实测输出（[ADR-0007](../decisions/0007-pool-metrics-and-capacity.md)
第 5 条）。一份报告固定的是**方法与口径**：版本、工作负载、失败率、延迟和资源成本都在文件里，
读的人可以自己判断它离自己的部署有多远。

**这些数字不是 SLA。** 探针只建模"一个进程的池同时承载多少个短事务"，不包含模型、连接器、
沙箱或真实业务时延，也只在一台开发机上跑过（Windows 11 / 临时 PostgreSQL 16.13 /
CPython 3.13.15）。它的用途是回答"要不要调 `max_size` 或 `min_size`，调了会不会有用"。

## 复现

```bash
export DATABASE_URL=postgresql://user:password@127.0.0.1:5432/aftercare
aftercare-capacity --max-sizes 1,2,4,8,16,32 --concurrency 16 --rounds 15 \
  --wake-cadence 0.02 --service-time-ms 10 --p95-budget-ms 50 --json report.json
```

- `--service-time-ms`：一个工作单元占住连接的时间。`0` 只量池自身开销，`10` 模拟一次 10 ms 的短事务。
- `--min-size`：每个被扫的池预热多少条连接。`--min-size <max_size>` 就是当前进程的懒增长行为，
  `--min-size = max_size` 是"握手已经付过"的稳态。
- `--p95-budget-ms`：达标判据。退出码 `0` 表示扫出了达标尺寸，`1` 表示没有——在 CI 里可以直接用。
- 三种形状：`steady`（不互相同步地持续打）、`burst`（每轮同时借）、`wake`（集中唤醒：所有工作线程
  在同一个 tick 醒来各做一个单元）。

## 本轮三份报告

| 文件 | 配置 | 结论 |
|---|---|---|
| [2026-09-15-lazy-min1.json](2026-09-15-lazy-min1.json) | `min_size=1`，sweep 1–32，16 并发，10 ms 单元，p95 预算 50 ms | **没有尺寸达标**：steady 的 p95 在 1/2/4/8/16/32 上是 262/130/125/168/128/124 ms，队列时间每轮 9.8–53.9 s；上限从 8 提到 32 时 in_use 峰值只从 8 升到 10 |
| [2026-09-15-prewarmed-min16.json](2026-09-15-prewarmed-min16.json) | `min_size=16`，sweep 16/32，其余同上 | **`max_size=16` 达标，`32` 无差别**：p95 31.3–39.1 ms，池排队时间 0 ms，in_use 峰值 16；32 的 p95 31.6–38.6 ms |
| [2026-09-15-overhead-only.json](2026-09-15-overhead-only.json) | 单元 = `SELECT 1`（`--service-time-ms 0`），sweep 1/8/16，16 并发，p95 预算 25 ms | 三个尺寸都达标，p50 2.7–8.6 ms：同一个池换一个更轻的工作单元就"够用"了 |

前两份合起来是结论：**突发并发先撞上握手与懒增长，不是上限。** 第三份是反面参照：
容量问题永远相对于工作负载，脱离工作负载谈"池够不够"没有意义。

## 读报告时要注意

- `in_use` / `wait` 是**采样峰值**（默认 5 ms 一次），比采样间隔短的工作单元可能采不到；
  `watcher_samples` 是这次跑了多少次采样。
- `opened` 是**运行期间**新开的连接数（相对运行开始时的快照差值），所以 `min_size` 已经把池预热的
  配置会显示 0——这是对的，不是丢了数据。
- 拒绝的借用（acquire timeout）记在 `failures`，**不计入**延迟分位：它在超时点结束，量的是队列
  而不是服务时间。
- 探针的工作单元由 Python 线程发起，本机 16 线程 + 10 ms 单元的下限约 29 ms/单元。跨主机比绝对值
  会误导，比同一台机器上的相对差异才有意义。
