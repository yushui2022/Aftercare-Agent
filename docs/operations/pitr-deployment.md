# 目标环境 PITR 实施与验收

这份 runbook 把目标环境的 PostgreSQL 时间点恢复接到 Aftercare 的固定证据契约。它不假设某一家云厂商，也不把一次 `pg_basebackup` 成功写成灾备已经验收。目标平台需要把物理基线、WAL 归档、恢复实例、业务核对和 Action 对账全部跑完，再将报告交给 `aftercare-backup pitr`。

## 前置条件

目标环境先明确以下值，并写进变更记录：PostgreSQL major 版本、归档位置和保留期、目标 RPO/RTO、恢复实例的网络隔离方式、备份加密密钥、执行身份、对象存储不可变保留策略和人工切换责任人。恢复实例必须使用不高于源端数据版本的兼容镜像；恢复命令不能访问生产凭据或生产业务网络。

归档目录或对象存储至少需要三类权限分离：主库只能追加/写入，恢复器只能读取，清理任务不能删除仍在保留窗口内的对象。所有传输和静态对象都要由目标平台加密；密钥轮换和恢复时使用的密钥版本要留下审计记录。

## 1. 建立物理基线

在低峰或平台规定的备份窗口执行 `pg_basebackup`。使用与服务端相同 major 的客户端，输出 tar 格式到临时目录；同时保存 `base.tar`、`backup_manifest`、备份开始/结束时间和命令日志。清单中的 WAL 起止位置必须来自 PostgreSQL 生成的 `backup_label`/`backup_manifest`，不能由调用方猜测。

一个最小的形状示例（路径和认证由平台 secret manager 提供）：

```bash
pg_basebackup \
  --pgdata=/srv/aftercare/base/$BACKUP_NAME \
  --format=tar \
  --wal-method=none \
  --manifest \
  --progress
```

把 `base.tar` 放在物理清单旁边，并生成 `base.manifest.json`。清单的 `artifact` 只能是文件名，`artifact_bytes` 和 `artifact_sha256` 必须在上传前计算；不要把 DSN、密码、客户数据或完整日志写入清单。

## 2. 持续归档 WAL

`archive_mode=on` 与 `archive_command`/归档库由 DBA 配置。归档命令必须原子地写入临时对象后再 rename，重复执行同一段不能破坏已有对象。备份完成后至少等待基线停止位置之后的 WAL 段进入归档，并运行：

```bash
aftercare-backup wal \
  --archive-dir /srv/aftercare/wal \
  --dsn "$DATABASE_URL" \
  --archive-lag-seconds "$ARCHIVE_LAG_BUDGET" \
  --json wal-verified.json
```

`wal` 只检查连续性、归档器是否推进以及是否覆盖最新逻辑备份；它不证明某个业务目标时间已经恢复。归档缺段、`.partial` 文件、归档器失败晚于成功，或归档落后超过部署预算，都必须阻止 PITR 验收。

## 3. 在隔离实例恢复

恢复器先把 `base.tar` 解到全新的数据目录，再挂载只读 WAL 归档。写入 `recovery.signal` 和平台生成的 `restore_command`，设置明确的 `recovery_target_time`；不要在恢复目录中复用生产 `postmaster.pid`、临时 socket 或生产的认证文件。恢复实例应只允许恢复操作员和自动核对任务访问。

恢复完成后必须检查：

- 实例已离开 recovery 状态，且目标时间没有超出可用 WAL；
- `pg_last_wal_replay_lsn()`（或平台记录的等价值）越过物理基线的 `wal_stop_lsn`；
- 目标时间之前的业务不变量存在，目标时间之后的写入不存在；
- 恢复点之后的 `aftercare_actions` 中 `UNKNOWN` 或更新过的 Action 已列出，并交给同一个 Action key 做供应商核对；
- 恢复耗时从恢复器开始到所有核对完成，不能只取 PostgreSQL 进程启动时间。

业务核对必须使用部署实际的订单、物流、买方观察和 Action 表。仓库的 [Docker profile](../../deploy/pitr_docker_drill.py) 只用合成表验证恢复器链路，不能作为生产业务核对证据。

## 4. 生成并校验证据

恢复器生成固定结构的 `pitr-run.json`，其中 `base_backup_name` 和 `base_backup_sha256` 必须与物理清单一致，并为五项检查提供证据引用：`base_backup`、`wal_archive`、`target_reached`、`application_state`、`action_reconciliation`。任何 `not_run` 或 `fail` 都是 `hold`。

```bash
aftercare-backup pitr \
  --base-manifest /srv/aftercare/base/$BACKUP_NAME/base.manifest.json \
  --evidence /srv/aftercare/recovery/$RUN_ID/pitr-run.json \
  --json /srv/aftercare/recovery/$RUN_ID/pitr-verified.json
```

只有退出码为 0 且 `pitr-verified.json` 的 `decision` 为 `pass`，目标环境 acceptance record 才能把 `pitr` 标为 `pass`。记录中的 `pitr` 项还要逐字保存 `verifier=aftercare-pitr`、`decision=pass`，以及验证器输出的 `manifest_sha256`、`evidence_sha256`、`base_backup_sha256`；验收脚本会拒绝缺少这些绑定信息的手工通过状态。`pitr` 的输入、输出、命令日志、对象版本 ID 和密钥版本号应进入同一归档批次；报告只保留引用和摘要，不复制业务正文。

## 5. 失败、保留和回滚

任何步骤失败都保留失败报告和原始命令状态，销毁隔离恢复实例前先确认日志已经归档。失败的恢复不能覆盖最近一次成功的基线，也不能让 release policy 继续晋级。若需要切换流量，先完成人工审批、连接切换演练和外部 Action 对账；PITR 本身不会自动回滚供应商退款、补发或通知。

恢复成功后按平台保留策略清理临时实例和临时密钥，但物理基线、WAL 对象、`pitr-run.json`、`pitr-verified.json`、`wal-verified.json` 和 acceptance record 必须在审计保留期内可读。定期调度应对 `wal`、PITR 和 `status` 的非零退出报警；没有成功演练记录不能被当作“未知但安全”。

## 验收门

目标环境的 `deployment-acceptance.json` 固定包含 PITR 项。它必须引用本目标环境实际执行产生的 `pitr-verified.json`，并使用与发布镜像相同的 digest；本机 Docker profile、静态模板、手工写的 `{"decision":"pass"}` 或另一镜像的报告都会被视为无效证据。签名、目标 registry 复扫、九项目标环境验收三道发布门全部关闭后，`release_policy.py` 才允许 `promote`。
