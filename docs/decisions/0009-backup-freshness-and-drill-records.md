# ADR-0009：备份新鲜度、演练记录与 RPO/演练预算

状态：接受，D-02 实施中。本 ADR 决定"备份多久没人验就该报警"以及"演练结果存在哪里"。它不
替代任何既有决策：[ADR-0008](0008-backup-and-restore-drills.md) 关于"一次备份怎么算数"的十条
决策继续有效，本 ADR 补的是"谁定期问、拿什么问、答不出来时怎么失败"。

## 背景

ADR-0008 把备份的判据从"作业退出码"换成了"一次真实恢复演练"，同时明确留下两件未做的事：
定期演练的自动化与告警、按部署目标给出的 RPO/RTO 结论。缺了它们，一个已经能演练的备份目录
仍然只能靠人记得去看：没人知道最新恢复点多老，也没人知道昨夜那次演练是失败还是压根没跑。
RPO 的具体数值只能由部署给出，但"检查 RPO 这件事"本身可以被工具化。

## 决策

1. **每次演练都在 dump 旁边写一条记录，成功失败都写。** `drill` 写
   `<name>.drill.json`：恢复点、结束时间、是否通过、RTO、每条检查的结果；演练在
   `pg_restore` 阶段就崩掉时也写一条 `ok=false` 加错误原因。理由只有一条：目录必须能区分
   "昨晚演练过、失败了"和"从来没人试过"，而缺失的文件区分不了这两件事。
2. **记录是给调度读的，不是给人读的。** 记录里只有判断新鲜度需要的字段，没有正文、没有
   租户数据、没有路径；`drill --json` 仍然是可以归档的完整报告，两者不互相替代。
3. **预算没有默认值。** `status` 的三个预算（`--rpo-seconds`、`--drill-interval-seconds`、
   `--require-newest-drill`，也可以用 `AFTERCARE_BACKUP_RPO_SECONDS` 与
   `AFTERCARE_BACKUP_DRILL_INTERVAL_SECONDS` 配置）都必须由部署给出。未配置就只报告、不判定，
   输出里写明"budgets none stated"：**未配置不等于已满足**。工具发明一个 RPO，等于替部署
   承诺了一个没人做过的承诺。
4. **RPO 检查最新恢复点，演练检查最近一次成功的恢复。** 两者不是同一个问题：备份每小时
   跑、演练每周跑是完全正常的组合。`status` 因此分别判定、分别报告。
5. **说了演练间隔，就等于要求最新那份备份被演练过。** `--drill-interval-seconds` 隐含
   `--require-newest-drill`：只有旧备份被演练过、最新的没有，不算达标——没演练过的 dump 是
   文件，不是恢复能力。
6. **策略问题与完整性问题分开。** 目录为空、清单对应的 dump 不见了、记录文件读不出来，
   无论预算怎么配都算失败或报错（退出码 1 / 2）。预算是部署说了算，完整性不是。
7. **导出器之前，告警靠退出码。** `status` 退出码：`0` 全部通过、`1` 有判定未通过或目录有
   问题、`2` 参数或环境错误（包括预算不是数字）。`--emit-metrics` 用现有 Metrics 接缝打
   `aftercare.backup.*` 的 JSON 日志行（唯一标签 `component`），导出器（C-03）落地后可以换成
   真正的告警通道。仓库不内置调度器：cron / systemd timer / CronJob 的接线写在
   [运维文档](../operations/backup-restore.md) 里，由部署选一种。
8. **保留删除连同记录一起删。** `retention --apply` 删除一份备份时同时删掉它的 dump、清单与
   演练记录，避免留下指向已删 dump 的记录把新鲜度算错。
9. **名字仍然是边界。** 记录文件名由备份名拼出来，所以备份名模式与校验移到
   `ops/tooling.py`（`BACKUP_NAME_PATTERN`、`validate_backup_name`），备份与演练记录共用一份
   定义：任何一个入口都不能把路径引出备份目录。

## 本机实测（2026-09-15，临时真实 PostgreSQL 16.13，CPython 3.13.15）

一次完整链路（`create` → `status` 未通过 → `drill --upgrade` → `status` 通过）：

```text
$ aftercare-backup create --directory <dir>
backup aftercare-20260915T123301Z written to <dir>
  recovery point  2026-09-15T12:33:01.816766+00:00
  schema          migration 16 of 16
  dump            82556 bytes, sha256 a5193011241a4839...

$ aftercare-backup status --directory <dir> --drill-interval-seconds 86400
backup status <dir>: failed
  backups          1 (0 restored, 1 never restored)
  newest dump      aftercare-20260915T123301Z (2s old)
  last drill       never recorded in this directory
  [FAIL] a restore has been proven inside the drill interval: no drill has ever been recorded in this directory
  [FAIL] the newest backup has been restored: aftercare-20260915T123301Z has never been restored;
         an unrehearsed dump is a file, not recovery capacity
（退出码 1）

$ aftercare-backup drill --directory <dir> --upgrade
  restore            0.609s
  verify             0.228s
  upgrade            0.310s
  RTO                1.147s on this host, for this dump
  [ok  ] restored row counts: every table holds the 31 recorded counts
  recorded         <dir>\aftercare-20260915T123301Z.drill.json
（退出码 0）

$ aftercare-backup status --directory <dir> --rpo-seconds 86400 --drill-interval-seconds 604800
backup status <dir>: ok
  backups          1 (1 restored, 0 never restored)
  newest dump      aftercare-20260915T123301Z (9s old)
  last drill       aftercare-20260915T123301Z ok (1s old)
  [ok  ] the newest recovery point is inside the RPO budget: ... is 9s old, inside 86400s
  [ok  ] a restore has been proven inside the drill interval: ... is 1s old, inside 604800s
  [ok  ] the newest backup has been restored: ... was restored and compared at least once
（退出码 0）
```

**这些数字仍然只说明口径。** 86400 / 604800 秒是示例预算，不是本项目的推荐值：它们必须按
部署的备份节奏与业务容忍度确定。

## 后果

- 备份目录多一类文件 `<name>.drill.json`，它和 dump、清单一起受保留策略管理。
- `aftercare-backup` 多一个子命令 `status`；`drill` 现在总会写记录（`--json` 仍然可选）。
- `ops/tooling.py` 多了备份名模式与校验，`backup.py` 与 `drill_records.py` 共用；路径边界的
  定义只有一份。
- 测试：离线 17 项（`tests/ops/test_freshness.py`）与真实 PostgreSQL 2 项
  （`tests/persistence/test_backup_restore.py`，缺客户端时带原因跳过）。
- 仍未完成：具体 RPO/RTO 数值仍要按部署确定；调度接线只在文档里给出，没有在目标环境执行
  过；WAL 归档与 PITR、对象存储与异地副本、备份加密与密钥托管、演练失败后的自动升级路径、
  多租户级选择性恢复都不在本 ADR 范围内。
