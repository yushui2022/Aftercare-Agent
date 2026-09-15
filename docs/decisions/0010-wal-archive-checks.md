# ADR-0010：WAL 归档检查（连续性、归档器推进与覆盖）

状态：接受，D-02 实施中。本 ADR 决定"在一份 dump 之外还丢得起多少"这个问题怎么被问出来。
它不替代任何既有决策：[ADR-0008](0008-backup-and-restore-drills.md) 的十条与
[ADR-0009](0009-backup-freshness-and-drill-records.md) 的九条继续有效。本 ADR 补的是：
dump 能被恢复到某个恢复点，而恢复点**之后**的 WAL 有没有被安全地留在别处，此前没有任何
东西在检查。

## 背景

ADR-0008/0009 之后，"最新那份 dump 多老"和"它被证明能恢复过吗"都有答案了，但两个答案都只
描述 dump 自己。一份 dump 是一个瞬间的完整拷贝，而这个瞬间到"现在"之间写入的数据一个字都没
被保护：没有 WAL 归档，两次 dump 之间发生的一切只能靠重做。ADR-0009 把这件事明确留给后续，
因为它需要 PostgreSQL 的 `archive_mode` 与 `archive_command`——那是部署的配置，不是一个工具
自己能补上的开关。

工具能做的是把三件事变成**可见、可判定**的，同时**不发明**任何部署没说过的承诺：归档目录里
的段有没有洞、归档器还在不在推进、归档有没有覆盖到最新那份 dump 需要的位置。第三件事正是
ADR-0008 里"一次恢复到底恢复了什么"的下半句——dump 把库带到一个恢复点，归档决定它能不能
再往前走。

## 决策

1. **清单 v2 记下 dump 覆盖到的 WAL 位置，且与恢复点取自同一个快照。**
   `pg_current_wal_lsn()` 与行数、迁移摘要一起在同一个 `REPEATABLE READ` 快照里读出，
   所以"这份 dump 覆盖到哪"和"这份 dump 是从哪个瞬间拷的"不可能各说一套。
   `manifest_version` 升到 `2`，`wal_lsn` 在 v2 里必填、在 v1 里必须缺（模型校验保证两种
   形状各自成立）。v1 清单仍然可读，只是没有 `wal_lsn`：`wal` 因此**不能**替它回答覆盖问题，
   会如实写 `no WAL position in the manifest, so coverage cannot be checked from here`，
   而不是当成没问题。
2. **只问能问的问题：三问一拒。** `wal` 回答归档段是否成链、归档器是否还在推进、归档是否
   覆盖最新 dump；它**拒绝**回答第四个问题——滞后多少算超标。滞后预算
   `--archive-lag-seconds`（或 `AFTERCARE_BACKUP_ARCHIVE_LAG_SECONDS`）没有默认值，未配置就
   只报告、不判定，输出写明 `no archive lag stated, so lag cannot fail here`。理由与
   ADR-0009 第 3 条相同：**未配置不等于已满足**，工具不替部署承诺滞后上限。
3. **连续性是名字的性质，不需要段大小；只有"数几个洞"才需要。**
   `0000000100000000000000FF` 的下一个必然是 `000000010000000000000100`，与
   `wal_segment_size` 无关，所以"有没有洞"任何时候都能答。`--segment-size` 只影响"缺了几个段"
   这一句话，没给时输出如实说明。这条边界很重要：运维在灾难现场最先能拿到的往往就是目录，
   而不是一个能回答设置的服务器。
4. **每条 timeline 单独判，不跨时间线接链。** 目录里可能同时留着切换前后的段，把 `FF..FF`
   的下一个当成 `00..00` 会把"切换后的新链"和"旧链的结尾"缝成一条不存在的链。报告逐条
   timeline 判连续，并在多于一条时说明"只有最新的那条不需要恢复目标就能到达"。
5. **只有显式 `--dsn` 才连数据库。** "归档器还在推进吗"只有服务器能答（`pg_stat_archiver`
   的"最后一次失败是否晚于最后一次成功"是唯一能发现 `archive_command` 静默失败、PostgreSQL
   无限重试同一个段的信号），但**服务器没了的时候恰恰最需要这个工具**，所以缺 `--dsn` 不是
   错误：目录检查照做，输出写 `archiver not read; pass --dsn to ask the server`。反过来，
   给了滞后预算却没有服务器可测，是操作错误（退出码 2），不是"跳过"。
6. **给了 `--dsn` 就以服务端的段大小为准，与 `--segment-size` 矛盾即报错。** 段名只有配上
   产生它的那个设置才有意义；两个来源不一致说明有人在用一个不是这台服务器的假设，这种矛盾
   必须在这里停下（退出码 2），不能被记成一个"发现"。
7. **`.partial` 是问题，不是文件。** 被中断的拷贝不是恢复能力，报告把它单列为 problem。
   `.history` 是正常存在的，不计入问题；目录里的其它文件只记一条 note，不判失败——归档目录
   常常与运维自己的东西共处。目录里一个段都没有同样是 problem：只有一份 dump 的数据库，保护
   它的就只有那份 dump。
8. **退出码沿用 0/1/2，指标复用 `aftercare.backup.*` 前缀。** `0` 全部通过、`1` 有判定未通过
   或目录有问题、`2` 参数或环境错误。`--emit-metrics` 打
   `aftercare.backup.wal_ok`/`wal_segments`/`wal_timelines`/`wal_gaps`，在有数据时才加
   `wal_lag_seconds`/`wal_archiver_failures`/`wal_covers_newest_backup`，标签仍然只有
   `component`——与 `status` 同一套派生口径，导出器（C-03）落地后不必区分两个来源。
9. **归档滞后与 dump 间隔是两个不同的 RPO 形状，同名不同物。**
   `status --rpo-seconds` 量的是"最新的完整拷贝多老"，`wal --archive-lag-seconds` 量的是
   "最近的工作有多少根本不可恢复"。两个都有默认值以外的答案，两个都必须由部署给出。本 ADR
   把第二个问题也变成可测的，但**没有**给出任何一个数值。

## 本机实测（2026-09-15，临时真实 PostgreSQL 16.13 + 真实 `archive_mode`，CPython 3.13.15）

场景一：一次真实 dump 被它自己启动的归档覆盖，预算 600 s 之内。

```text
$ aftercare-backup wal --archive-dir <archive> --dsn <url> --directory <backups> --archive-lag-seconds 600
wal archive <archive>: ok
  checked at       2026-09-15T13:24:04.880293+00:00
  segments         1 over 1 timeline(s) (000000010000000000000001 .. 000000010000000000000001)
  segment size     16MB (from the server)
  gaps             none
  archiver         newest segment 1s old (000000010000000000000001), 0 failure(s)
  newest backup    demo-1 covered (needs 000000000000000000000001, archive ends at 000000010000000000000001)
  [ok  ] the archived segments form an unbroken chain: 1 segment(s) on 1 timeline(s) with no hole
  [ok  ] the archiver is completing segments: 1 segment(s) archived, newest 000000010000000000000001 at 2026-09-15T21:24:04.028940+08:00
  [ok  ] the archive reaches the newest backup: the archive ends at 000000010000000000000001, at or past the segment holding 0/1AEB198 (000000000000000000000001), so demo-1 can be rolled forward
  [ok  ] the archive lag is inside the RPO budget: the newest archived segment is 1s old, inside 600s
（退出码 0）
```

这份 dump 的恢复点是 `0/1AEB198`（同一快照读出的 `wal_lsn`），落在
`000000010000000000000001` 里——归档正好覆盖到它。

场景二：把 `archive_command` 改成必然失败（`exit 1`）之后，**同一个目录看起来仍然完好**，
只有问服务器才看得出来。

```text
$ aftercare-backup wal --archive-dir <archive> --dsn <url>
wal archive <archive>: failed
  segments         1 over 1 timeline(s) (000000010000000000000001 .. 000000010000000000000001)
  segment size     16MB (from the server)
  gaps             none
  archiver         newest segment 2s old (000000010000000000000001), 1 failure(s)
  newest backup    not checked against this archive
  budgets          no archive lag stated, so lag cannot fail here
  [ok  ] the archived segments form an unbroken chain: 1 segment(s) on 1 timeline(s) with no hole
  [FAIL] the archiver is completing segments: the newest attempt (000000010000000000000002) failed after the newest success (000000010000000000000001 at 2026-09-15T21:24:04.028940+08:00); PostgreSQL is retrying the same segment, so the archive is not advancing
（退出码 1）
```

连续性这一问仍然是 `ok`——失败是在"下一个段没进来"之前就发现的，这正是这套检查要抓的时刻。
把场景二的 `--dsn` 去掉，同样的目录会报 `archiver not read`，**不会**说它没问题。

场景三：从归档里删掉中间那个段，并且**不问服务器**。

```text
$ aftercare-backup wal --archive-dir <archive>
wal archive <archive>: failed
  segments         3 over 1 timeline(s) (000000010000000000000001 .. 000000010000000000000004)
  segment size     not stated and not readable from a server
  gaps             1
                   timeline 00000001: 000000010000000000000002 then 000000010000000000000004 (the number of missing segments needs the segment size)
  archiver         not read; pass --dsn to ask the server
  newest backup    not checked against this archive
  budgets          no archive lag stated, so lag cannot fail here
  [FAIL] the archived segments form an unbroken chain: 1 hole(s), the first between 000000010000000000000002 and 000000010000000000000004 on timeline 00000001; everything after a hole is unusable for recovery -- pass --segment-size to count what is missing
（退出码 1）
```

洞之后的一切都不能用于恢复，所以这一问必须有确定答案；目录实际内容
（`...001`、`...002`、`...004`，各 16777216 字节）与报告一致。

**这三个场景只证明口径与这台机器上的行为，不是 RPO 数值。** 600 s 是示例预算，不是本项目的
推荐值；它必须由部署按自己的写入量与容忍度确定。

## 后果

- 备份清单多一个必填字段 `wal_lsn`，`manifest_version` 升到 2；v1 清单仍可读，但缺少覆盖
  判定所需的输入。
- `aftercare-backup` 多一个子命令 `wal`（`--archive-dir` 必填，`--dsn`/`--directory`/`--name`/
  `--segment-size`/`--archive-lag-seconds`/`--emit-metrics`/`--json` 可选），退出码与其它子命令
  一致。
- `aftercare_agent/ops/freshness.py` 的 `_age_text()` 改名并公开为 `age_text()`：归档报告与
  新鲜度报告要用同一套年龄文案，两处不能各写一套。
- 测试：离线 30 项（`tests/ops/test_wal_archive.py`）与真实集群 3 项
  （`tests/persistence/test_wal_archive.py`，自己 `initdb` 一个 `archive_mode = on` 的临时集群，
  没有可用二进制时带原因跳过）。真实集群那三项需要一个**同时带 `pg_dump`** 的二进制目录，
  找到的目录会被放进 `PATH`，因为被测代码就是从 `PATH` 找客户端的。
- CI 多装一个 `postgresql-17`（在 `postgresql-client-17` 之外），归档演练要 `initdb` 与
  `pg_ctl`；用 `policy-rc.d` 阻止 apt 启动第二个集群，避免与作业自己的 service container
  抢 5432。
- 仍未完成：**PITR 演练本身**（按时间点到恢复并验证结果）、对象存储与异地副本、备份加密与
  密钥托管、按部署目标确定的 RPO/RTO 数值、把 `wal` 接进目标环境的调度、演练失败后的自动
  升级路径、多租户级选择性恢复，都不在本 ADR 范围内。本 ADR 交付的是判据，不是恢复流程。