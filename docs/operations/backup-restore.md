# 备份、恢复演练、保留删除与恢复后核对

`aftercare-backup` 是运维工具，不在请求路径上。它把"备份文件存在"变成"恢复被演练过"：
一次备份产出一份 dump 与一份清单，演练把这份 dump 恢复到临时库并逐表比对，核对清单列出
恢复点之后必须人工复核的外部动作。决策、口径与不变量见
[ADR-0008](../decisions/0008-backup-and-restore-drills.md)。

## 1. 前置条件

- `pg_dump` 与 `pg_restore` 在 PATH 上，且 **major 不低于服务端**：`pg_dump` 拒绝读取比
  自己新的服务端，工具会在写任何文件之前先失败。不在 PATH 时用 `AFTERCARE_PG_DUMP`、
  `AFTERCARE_PG_RESTORE` 指定绝对路径。
- 一个可以 `CREATE DATABASE` / `DROP DATABASE` 的账号（演练要建临时库），以及对目标库的
  普通读权限。演练不需要超级用户。
- 备份目录要有容量与保留策略；工具不做异地复制、对象存储、加密与密钥托管。

CI 使用 `postgres:17` 服务，而 GitHub runner 自带的客户端可能是旧 major，因此工作流在
测试前会尝试安装 `postgresql-client-17`；装不上时 `tests/persistence/test_backup_restore.py`
会带着原因跳过，而不是把整条流水线判失败。

## 2. 命令

```powershell
aftercare-backup create   --directory <dir> [--dsn <url>] [--name <name>]
aftercare-backup verify   --directory <dir> [--name <name> | --all]
aftercare-backup drill    --directory <dir> [--dsn <url>] [--upgrade] [--keep-scratch]
aftercare-backup retention --directory <dir> [--keep-last N] [--keep-daily N] [--apply]
aftercare-backup reconcile --dsn <url> (--recovery-point <iso> | --directory <dir>)
```

DSN 缺省取 `DATABASE_URL`。退出码：`0` 通过、`1` 检查未通过、`2` 缺参数或环境不允许
（例如客户端缺失、库已存在、DSN 未配置）。`--json <path>` 把同一份结果写成 JSON，
便于归档与比对。

## 3. 备份目录里的东西

| 文件 | 内容 |
|---|---|
| `<name>.dump` | `pg_dump --format=custom` 的归档 |
| `<name>.manifest.json` | 恢复点、库名、服务端版本、schema 版本与每条迁移的 SHA-256、每张 `aftercare_` 表的行数、dump 的字节数与 SHA-256、客户端版本 |

两者必须成对保留：清单是"恢复出来应该长什么样"的答案，缺了它只剩一个文件。清单从与 dump
相同的快照读出，所以行数一字不差地描述这份 dump。

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

## 6. 恢复点之后的外部动作核对

```powershell
aftercare-backup reconcile --directory <dir> --json review.json
```

从 `aftercare_actions` 列出两类 Action：任何 `UNKNOWN`（持久结局，与恢复点无关，必须按**同一个
Action** 与供应商核对，不能换幂等键重发），以及 `updated_at` 晚于分界线的行。分界线默认比恢复
点再提前 60 s（`--safety-margin-seconds`），把与 dump 赛跑的那个提交留在清单里。工具不调用
供应商、不解析回执、不自动改状态；`--fail-on-open` 让清单非空时退出码为 1，可用于发布门禁。

## 7. 保留删除

```powershell
aftercare-backup retention --directory <dir> --keep-last 7 --keep-daily 30   # 先看计划
aftercare-backup retention --directory <dir> --keep-last 7 --keep-daily 30 --apply
```

规则：最新 `keep_last` 份永久保留；其余按 UTC 自然日每份保留一天，最多 `keep_daily` 天；
其余删除。任何会删空的计划都被拒绝，名字必须匹配受限模式（防止把删除引到目录外）。先看
计划再 `--apply`，不要直接自动化执行未经阅读的计划。

## 8. 本机实测（2026-09-15）

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

## 9. 尚未覆盖

WAL 归档与时间点恢复（PITR）、对象存储/异地副本、备份加密与密钥托管、定期演练的自动化与
告警、按部署目标给出的 RPO/RTO 结论、多租户级选择性恢复、恢复过程中的流量切换脚本。
