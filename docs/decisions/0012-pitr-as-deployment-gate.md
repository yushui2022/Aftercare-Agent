# ADR-0012：把物理 PITR 作为目标部署验收门

- 状态：Accepted
- 日期：2026-09-20
- 关联：ADR-0008、ADR-0009、ADR-0010、`aftercare_agent/ops/pitr.py`

## 背景

Aftercare 已经能验证逻辑 `pg_dump` 的完整性、恢复演练、新鲜度和 WAL 归档状态，但这些检查不能证明 PostgreSQL 能从物理基线沿 WAL 恢复到指定时间。逻辑 dump 的摘要和表行数也不能替代物理 `pg_basebackup` 的 WAL 起止位置。若发布只要求服务 readiness、迁移和回滚，灾备可能一直没有在目标环境真正执行过。

## 决策

目标环境验收固定包含 `pitr` 项，与 preflight、migration、runtime 权限、租户隔离、readiness、Worker 恢复、Secret rotation 和 rollback 一起组成九项门禁。

PITR 验收必须：

1. 从物理 `pg_basebackup` 文件生成 `PhysicalBackupManifest`，记录文件摘要、字节数、服务端版本和 WAL 起止位置；
2. 在隔离实例使用目标环境实际的 `restore_command` 和 `recovery_target_time`；
3. 核对恢复后的业务不变量和恢复点之后的 Action 对账；
4. 生成固定的五项 `PitrEvidence`，由 `aftercare-backup pitr` 绑定并校验；
5. 把物理清单、恢复报告、校验结果、对象版本和密钥版本纳入同一审计批次；
6. 使用与发布镜像相同的 image digest 写入 `deployment-acceptance.json`，否则发布保持 `hold`。

仓库提供 `deploy/pitr_docker_drill.py` 作为本机和 CI 的可复现 profile。它验证恢复器和证据契约，但使用合成业务表；它不能关闭目标环境的 `pitr` 门。

## 未采用的方案

- 只检查 `pg_dump`：无法证明 WAL 前滚和时间点目标；
- 只检查 WAL 目录连续：无法证明恢复实例能启动或业务状态正确；
- 只接受人工填写的 `decision: pass`：无法把报告绑定到物理文件和发布镜像；
- 把 PITR 放到上线之后：第一次灾难才会发现归档权限、密钥、网络或 Action 对账缺失。

## 后果

发布验收多一个必须在目标环境实际执行的步骤，部署会多消耗一次隔离恢复实例和对象存储读取成本。好处是“服务已运行”和“灾备可恢复”不会混为一个状态，RPO/RTO、归档权限、恢复网络和外部 Action 核对都能在上线前暴露。目标平台仍需自行确定调度、加密、异地复制、流量切换和告警实现；这些不能由仓库的本机 profile 推断。
