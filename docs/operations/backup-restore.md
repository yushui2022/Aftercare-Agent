# 备份、恢复演练、保留删除与恢复后核对

`aftercare-backup` 是运维工具，不在请求路径上。它把"备份文件存在"变成"恢复被演练过"：
一次备份产出一份 dump 与一份清单，演练把这份 dump 恢复到临时库并逐表比对，核对清单列出
恢复点之后必须人工复核的外部动作。决策、口径与不变量见
[ADR-0008](../decisions/0008-backup-and-restore-drills.md)；每次演练的结果会写在 dump 旁边，
`status` 用这些记录回答"最新恢复点多老、最近一次恢复什么时候被证明过"，见
[ADR-0009](../decisions/0009-backup-freshness-and-drill-records.md)；`wal` 检查恢复点**之后**的
WAL 有没有被归档牢牢接住，见 [ADR-0010](../decisions/0010-wal-archive-checks.md)。

## 1. 前置条件

- `pg_dump` 与 `pg_restore` 在 PATH 上，且 **major 不低于服务端**：`pg_dump` 拒绝读取比
  自己新的服务端，工具会在写任何文件之前先失败。不在 PATH 时用 `AFTERCARE_PG_DUMP`、
  `AFTERCARE_PG_RESTORE` 指定绝对路径。
- 一个可以 `CREATE DATABASE` / `DROP DATABASE` 的账号（演练要建临时库），以及对目标库的
  普通读权限。演练不需要超级用户。
- 备份目录要有容量与保留策略；工具不做异地复制、对象存储、加密与密钥托管。
- WAL 归档检查要求数据库已经被部署配成 `archive_mode = on` 并写好了 `archive_command`：
  这是部署的配置，不是工具能打开的开关。没有归档时 `wal` 会如实报出"一个段都没有"，而不是
  假装 dump 已经够了。

CI 使用 `postgres:17` 服务，而 GitHub runner 自带的客户端可能是旧 major，因此工作流在
测试前会安装 `postgresql-client-17` 与 `postgresql-17`（归档演练需要 `initdb`/`pg_ctl`），并用**绝对路径**自证装上了。
演练类用例在本机缺客户端时会带着原因跳过；但 CI 专门设了
`AFTERCARE_REQUIRE_DRILLS=1`，把跳过变成失败——一个没跑的演练不能被一个绿色作业盖住。

## 2. 命令

```powershell
aftercare-backup create   --directory <dir> [--dsn <url>] [--name <name>]
aftercare-backup verify   --directory <dir> [--name <name> | --all]
aftercare-backup drill    --directory <dir> [--dsn <url>] [--upgrade] [--keep-scratch]
aftercare-backup retention --directory <dir> [--keep-last N] [--keep-daily N] [--apply]
aftercare-backup reconcile --dsn <url> (--recovery-point <iso> | --directory <dir>)
aftercare-backup status    --directory <dir> [--rpo-seconds N] [--drill-interval-seconds N]
aftercare-backup wal       --archive-dir <dir> [--dsn <url>] [--directory <dir>]
```

DSN 缺省取 `DATABASE_URL`。退出码：`0` 通过、`1` 检查未通过、`2` 缺参数或环境不允许
（例如客户端缺失、库已存在、DSN 未配置）。`--json <path>` 把同一份结果写成 JSON，
便于归档与比对。

## 3. 备份目录里的东西

| 文件 | 内容 |
|---|---|
| `<name>.dump` | `pg_dump --format=custom` 的归档 |
| `<name>.manifest.json` | 恢复点、dump 覆盖到的 WAL 位置（清单 v2）、库名、服务端版本、schema 版本与每条迁移的 SHA-256、每张 `aftercare_` 表的行数、dump 的字节数与 SHA-256、客户端版本 |
| `<name>.drill.json` | 这份 dump 最近一次演练的结果：恢复点、结束时间、是否通过、RTO 与每条检查的结论；失败时也写，并带错误原因 |

前两者必须成对保留：清单是"恢复出来应该长什么样"的答案，缺了它只剩一个文件。清单从与 dump
相同的快照读出，所以行数一字不差地描述这份 dump。第三条是"这份 dump 被证明能恢复过"的
记录，只由 `drill` 写；`retention --apply` 删除一份备份时会把它一起删掉。

## 4. 校验（离线）

```powershell
aftercare-backup verify --directory <dir> --all
```

检查 dump 是否存在、字节数与 SHA-256 是否与清单一致、清单里的迁移是否都还在本 build 里、
每条迁移的 SHA-256 是否漂移、是否记录了行数。schema 比本 build 旧是**正常**的（备份先于
迁移），会被如实报成"比这份 dump 新 N 个迁移"，不是失败。

## 5. 演练

```powershell
aftercare-backup drill --directory <dir> --upgrade --json drill.json
```

1. 建一个临时库 `aftercare_drill_<UTC 时间戳>`（已存在则拒绝，除非 `--replace-scratch`）；
2. `pg_restore --single-transaction --exit-on-error` 恢复 dump；
3. 比对迁移摘要、`aftercare_` 表集合与每张表的行数；
4. `--upgrade` 时在恢复出来的副本上跑 `migrate()`，再比对一次行数；
5. 删除临时库（`--keep-scratch` 保留，仅用于人工排查）。

报告给出 `restore`/`verify`/`upgrade` 三段耗时与 **RTO = 三者之和**。**RPO 不由演练发明**：
演练只证明这份 dump 代表哪个恢复点（`taken_at`）以及开始演练时它已经多旧；RPO 是备份节奏
的属性，要和节奏一起在有目标的部署上给出。

演练不是灾难恢复流程本身：真实恢复需要一个变更窗口、一个干净的实例、停写与切换决定。
`--upgrade` 演示的是恢复出来的库能否被本 build 的迁移接管。

## 6. 新鲜度与预算检查（给调度用）

```powershell
aftercare-backup status --directory <dir> [--rpo-seconds N] [--drill-interval-seconds N]
                            [--require-newest-drill] [--emit-metrics] [--json status.json]
```

`status` 只读目录，回答两个不同的问题：最新恢复点多老（RPO 问题），以及最近一次**成功**的
演练多老（演练问题）。三个预算**都没有默认值**，必须由部署给出，也可以用
`AFTERCARE_BACKUP_RPO_SECONDS` 与 `AFTERCARE_BACKUP_DRILL_INTERVAL_SECONDS` 配置：

- `--rpo-seconds`：最新 dump 的恢复点比这个年龄更久就算失败；
- `--drill-interval-seconds`：距最近一次成功演练超过这段时长就算失败，并隐含
  `--require-newest-drill`；
- `--require-newest-drill`：最新那份备份必须被成功演练过。

一个预算都不给时，`status` 只报告、不判定，输出里写 `budgets none stated`——**未配置不等于
已满足**。目录为空、清单对应的 dump 缺失、记录文件读不出来，无论预算怎么配都失败或报错：
这些不是策略问题。退出码与其它子命令一致（`0` 通过、`1` 未通过、`2` 参数或环境错误）。

`drill` 无论成功失败都会写 `<name>.drill.json`，`status` 读的就是这些记录，所以"昨晚演练过、
但失败了"和"从来没人试过"是两种不同的输出。

在没有导出器（C-03）之前，告警靠**非零退出码**。下面两段接线只是示例，**没有在本机执行
过**（本机是 Windows，没有 cron/systemd），落地时按目标环境写：

```cron
# 每小时：备份、演练、预算检查串成一条，任何一步非零由 cron 的 MAILTO 报警
0 * * * * /usr/local/bin/aftercare-backup create --directory /srv/backups >>/var/log/aftercare-backup.log 2>&1 && /usr/local/bin/aftercare-backup drill --directory /srv/backups --upgrade >>/var/log/aftercare-backup.log 2>&1 && /usr/local/bin/aftercare-backup status --directory /srv/backups --rpo-seconds 86400 --drill-interval-seconds 604800 >>/var/log/aftercare-backup.log 2>&1
```

systemd 用 `OnFailure=` 接告警单元，Kubernetes 用 CronJob 并按非零退出重试或告警；
`--emit-metrics` 为每次检查打一行一条 JSON 的 `aftercare.backup.*` 日志（唯一标签
`component`），导出器接上之后可以同时走指标通道。示例里的 86400 / 604800 秒只是占位数字，
不是本项目的推荐值。

## 7. WAL 归档检查（PITR 的前置条件）

```powershell
aftercare-backup wal --archive-dir <dir> [--dsn <url>] [--directory <dir>] [--name <name>]
                         [--segment-size 16MB] [--archive-lag-seconds N] [--emit-metrics]
```

`--archive-dir` 是 `archive_command` 写入的目录。这个子命令回答三个问题，并拒绝回答第四个：

1. **归档段是不是成链**（连续性）。只读目录里的段名就能答，不需要段大小；有洞时洞之后的一切
   都不能用于恢复，所以这一问必须有确定答案。`--segment-size` 只影响"缺了几个段"这句话，
   没给就如实说明。每条 timeline 单独判，不跨时间线接链。
2. **归档器还在不在推进**（需要 `--dsn`）。读 `pg_stat_archiver` 判断"最后一次失败是否晚于最后
   一次成功"：`archive_command` 静默失败时 PostgreSQL 会无限重试同一个段，目录看起来完好，
   只有这个信号能发现它。
3. **归档有没有覆盖最新那份 dump**（需要 `--directory`，且清单里有 `wal_lsn`）。清单 v2 记录的
   是 dump 覆盖到的 WAL 位置，它与恢复点出自同一个快照；清单 v1 没有这个字段，因此覆盖问题
   在旧清单上是"答不了"，不是"没问题"。
4. **滞后多少算超标**：`--archive-lag-seconds`（或 `AFTERCARE_BACKUP_ARCHIVE_LAG_SECONDS`）
   **没有默认值**。未配置只报告、不判定，输出写 `no archive lag stated, so lag cannot fail
   here`；给了预算却没有 `--dsn` 可测是参数错误（退出码 2）。

`--dsn` 是**可选**的：服务器不可用时正是最需要看归档的时候，所以不连库也照常检查目录，只是
如实写 `archiver not read`。给了 `--dsn` 时，服务端报告的段大小优先；它与 `--segment-size`
矛盾会直接报错（退出码 2），因为段名只有配上产生它的那个设置才有意义。归档目录里出现
`.partial`（被中断的拷贝）按问题处理，`.history` 正常，其它文件只记一条 note。

退出码与其它子命令一致，`--json <path>` 归档同一份结果，`--emit-metrics` 打
`aftercare.backup.wal_*`（与第 6 节共用前缀和唯一标签 `component`）。

> `wal` 检查的是**归档够不够用来前滚**，不是**前滚演练本身**。按时间点真正恢复一次、并核对
> 恢复出来的业务状态，仍然没有工具化（见第 11 节）。

接进调度仍然只是示例，**没有在本机执行过**（本机是 Windows，没有 cron/systemd）：

```cron
# 每小时：备份、演练、新鲜度与归档检查串成一条，任何一步非零由 cron 的 MAILTO 报警
0 * * * * /usr/local/bin/aftercare-backup create --directory /srv/backups >>/var/log/aftercare-backup.log 2>&1 && /usr/local/bin/aftercare-backup drill --directory /srv/backups --upgrade >>/var/log/aftercare-backup.log 2>&1 && /usr/local/bin/aftercare-backup status --directory /srv/backups --rpo-seconds 86400 --drill-interval-seconds 604800 >>/var/log/aftercare-backup.log 2>&1 && /usr/local/bin/aftercare-backup wal --archive-dir /srv/pgwal --dsn "$DATABASE_URL" --directory /srv/backups --archive-lag-seconds 900 >>/var/log/aftercare-backup.log 2>&1
```

## 8. 恢复点之后的外部动作核对

```powershell
aftercare-backup reconcile --directory <dir> --json review.json
```

从 `aftercare_actions` 列出两类 Action：任何 `UNKNOWN`（持久结局，与恢复点无关，必须按**同一个
Action** 与供应商核对，不能换幂等键重发），以及 `updated_at` 晚于分界线的行。分界线默认比恢复
点再提前 60 s（`--safety-margin-seconds`），把与 dump 赛跑的那个提交留在清单里。工具不调用
供应商、不解析回执、不自动改状态；`--fail-on-open` 让清单非空时退出码为 1，可用于发布门禁。

## 9. 保留删除

```powershell
aftercare-backup retention --directory <dir> --keep-last 7 --keep-daily 30   # 先看计划
aftercare-backup retention --directory <dir> --keep-last 7 --keep-daily 30 --apply
```

规则：最新 `keep_last` 份永久保留；其余按 UTC 自然日每份保留一天，最多 `keep_daily` 天；
其余删除。任何会删空的计划都被拒绝，名字必须匹配受限模式（防止把删除引到目录外）。先看
计划再 `--apply`，不要直接自动化执行未经阅读的计划。

## 10. 本机实测（2026-09-15）

Windows 11 / 临时 PostgreSQL 16.13 / CPython 3.13.15 / 客户端 16.13，数据库含整套回归
留下的合成数据：dump 111,950 字节、31 张 `aftercare_` 表、schema 迁移 16。

```text
restore drill aftercare-20260915T120729Z: ok
  restore            0.703s
  verify             0.158s
  upgrade            0.151s
  RTO                1.012s on this host, for this dump
```

同一库上的核对清单有 7 个 Action：2 个 `UNKNOWN`、5 个在上次恢复点之后被更新过。报告原文见
[`2026-09-15-restore-drill.json`](2026-09-15-restore-drill.json)。这些数字只说明方法与口径，
不是 SLA。

## 11. 尚未覆盖

按时间点恢复（PITR）**演练本身**、对象存储/异地副本、备份加密与密钥托管、按部署目标给出的
RPO/RTO **数值**、多租户级选择性恢复、恢复过程中的流量切换脚本，以及演练失败后的自动升级
路径。归档是否连续、归档器是否在推进、归档是否覆盖最新 dump 已经可以判定（见第 7 节），
新鲜度与演练间隔同样可以判定（见第 6 节），但**两个都还没有数值**：`--rpo-seconds`、
`--drill-interval-seconds` 与 `--archive-lag-seconds` 都没有默认值，必须由部署给出。调度接线
只在文档里给出，没有在目标环境执行过。
