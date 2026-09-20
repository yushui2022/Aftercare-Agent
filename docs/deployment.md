# Deployment profile：平台无关运行契约

这份配置把已经验证过的容器边界固化为一条可执行部署路线。它适合在选定云或编排平台前验证镜像、PostgreSQL 身份、迁移顺序和进程探针；它不宣称某个生产环境已经通过容量、灾备或供应商联调。

仓库还提供 [integration Compose profile](../deploy/compose/README.md#integration-profile)，
在临时 PostgreSQL 上真实验证迁移身份、runtime DDL 拒绝、API readiness 和合成业务闭环；
下面的 deployment profile 再把同一身份边界映射到目标环境的 Secret、TLS、备份和回滚验收。

## 身份和进程边界

一个部署至少使用两个 PostgreSQL 登录角色：

| 身份 | 生命周期 | 权限 |
|---|---|---|
| migration role | 每次发布的短期 Job | 数据库 owner 或等价 DDL 权限；执行 Aftercare 迁移与 EGM schema v1 安装 |
| runtime role | API 与 Worker 常驻进程 | schema `USAGE`、业务表 DML、sequence 使用权、版本表只读；没有 `CREATE`，不能修改迁移台账 |

`aftercare-migrate` 在一个事务边界内应用 23 个 Aftercare 迁移与 EGM schema v1，随后只读复核两个版本。第 23 个迁移为带 `tenant_id` 表启用 RLS 策略；它依赖 runtime 事务先设置 `aftercare.tenant_id`，没有上下文时 fail closed。`aftercare-migrate --check` 只校验，不写数据库。`aftercare-api` 是镜像和安装包提供的 API 进程入口，默认转发到 `aftercare_agent.api.app:app`；它支持 `AFTERCARE_API_HOST`、`AFTERCARE_API_PORT`、`AFTERCARE_API_WORKERS` 和 `AFTERCARE_API_LOG_LEVEL`，不负责迁移或修改业务权限。API 与 Worker 设置 `AFTERCARE_AUTO_MIGRATE=0` 后不会执行 DDL；它们在启动时校验 Aftercare schema，API 的 `/readyz` 每次也校验。启用模型 Harness 的 Worker 还会校验 EGM schema。

启用 RLS 的 deployment profile 目前要求 Worker 设置 `AFTERCARE_TENANT_ID` 并按租户固定运行；参考 Compose 用 `AFTERCARE_WORKER_REQUIRE_TENANT=1` 缺失即启动失败。跨租户公平队列仍保留在代码层，但尚未有能在 `NOBYPASSRLS` 下安全选取任务的独立 dispatcher，因此暂按租户部署 Worker 副本，直到该 dispatcher 经过单独权限与恢复验收。

本地 profile 继续默认 `AFTERCARE_AUTO_MIGRATE=1`，用于贡献者快速启动。deployment profile 显式设为 `0`，因此空库、缺迁移、未知版本或历史 SQL checksum 变化都会拒绝启动。

## 首次建立数据库

先由平台管理员创建一个专用数据库和两个 `LOGIN` role。密码、证书和 DSN 由目标环境的 secret manager 以单行 UTF-8 文件注入，不写进镜像、仓库或容器环境。migration role 应拥有该专用数据库；runtime role 必须是独立账号，不能继承 migration role。

所有进程都接受 `DATABASE_URL` 或 `DATABASE_URL_FILE`，两者同时存在会拒绝启动。文件输入限制为 64 KiB、只允许一个非空文本行；空文件、多行、NUL、非 UTF-8、不可读或超限文件都会 fail closed。模型密钥同样支持 `AFTERCARE_MODEL_API_KEY_FILE`，OIDC introspection 密钥支持 `AFTERCARE_OIDC_INTROSPECTION_CLIENT_SECRET_FILE`。

模型策略通过 `AFTERCARE_MODEL_STRATEGY_ID`、`AFTERCARE_MODEL_CONFIG_VERSION`、
`AFTERCARE_MODEL_POLICY_VERSION` 和 `AFTERCARE_MODEL_TOOL_SCHEMA_VERSION` 形成一组
可审计身份，并写入新 Run 的 checkpoint。滚动模型或策略配置时必须保持
`AFTERCARE_MODEL_CONFIG_VERSION` 不变，或者先完成独立迁移；已有 Run 遇到版本不一致会
在模型调用前进入 `REVIEW`，不能用预算 override 绕过；持有 `strategy:migrate` Case 权限的操作员可通过独立策略迁移 API 提交旧/新身份和 checkpoint 版本，迁移后仍需普通 Review 放行。

用 migration role 运行：

```bash
aftercare-migrate
aftercare-migrate --check
```

再由数据库 owner 执行运行权限模板。三个变量都以 psql identifier 传入，避免把角色名拼成 SQL：

```bash
psql "$AFTERCARE_MIGRATION_DATABASE_URL" \
  -v database_name=aftercare \
  -v migration_role=aftercare_migrator \
  -v runtime_role=aftercare_runtime \
  -f deploy/postgres/runtime-grants.sql
```

模板会撤销 `public` schema 对 `PUBLIC` 的建表权，因此要求专用数据库。它为当前表和 sequence 授权，并为 migration role 创建的后续对象设置默认权限；runtime role 同时被强制设为 `NOSUPERUSER NOBYPASSRLS`；两个版本台账最后被收紧为只读。迁移完成、runtime role 存在后，必须用同一不可变镜像运行 `aftercare-rls harden`，让表 owner 也受 RLS 约束；随后用 runtime DSN 运行 `aftercare-rls verify`，在回滚事务内检查空上下文、租户可见性以及跨租户写入/更新/删除。部署 Compose profile 已把这两个一次性步骤放在 API/Worker 之前。

验收 runtime role：

```bash
DATABASE_URL="$AFTERCARE_RUNTIME_DATABASE_URL" aftercare-migrate --check
```

随后应验证 runtime role 无法 `CREATE TABLE`，API 的 `/readyz` 返回 200，能受理一条测试租户 Case，Worker 能领取或返回 `idle`。测试租户和测试 IdP 必须与生产租户隔离。

## 容器启动顺序

[`docker-compose.deployment.yml`](../deploy/compose/docker-compose.deployment.yml) 是平台无关参考，不内置 PostgreSQL、TLS 终止或真实业务连接器。复制 `deployment.env.example` 到仓库外，填入不可变镜像 digest、两个 DSN secret 文件的宿主路径与目标环境地址后：

两个 DSN Secret 都必须包含 `sslmode=verify-full`，并由目标平台提供可信 CA 链；`AFTERCARE_REQUIRE_DATABASE_TLS=1` 会同时传给 migration、RLS check、API 和 Worker，进程在读取 `DATABASE_URL` 时会拒绝未开启主机名与证书校验的连接串。设计理由和边界见 [ADR-0016](decisions/0016-postgresql-tls-boundary.md)。

```bash
docker compose --env-file /secure/path/aftercare.env \
  -f deploy/compose/docker-compose.deployment.yml run --rm migrate

docker compose --env-file /secure/path/aftercare.env \
  -f deploy/compose/docker-compose.deployment.yml up -d api

docker compose --env-file /secure/path/aftercare.env \
  -f deploy/compose/docker-compose.deployment.yml --profile worker up -d worker
```

Compose 只表达进程和权限边界。migration 容器只挂载 migration DSN；API/Worker 只挂载 runtime DSN，容器环境中仅有 `/run/secrets/...` 路径，因此 `docker inspect` 不会暴露 DSN。API 必须配置真实 OIDC 三元组，合成身份固定关闭。公网 TLS、入口限流、网络策略、secret 轮换和日志收集由选定平台提供；当前进程在启动时读取 secret，轮换后需要受控重启。指标后端默认是 `logging`；若设置 `AFTERCARE_METRICS_BACKEND=otel`，目标镜像必须使用 `AFTERCARE_EXTRAS=observability` 构建以带入锁定的 OTel SDK/OTLP exporter，并在平台配置 provider、endpoint、采样和留存。该 extra 不会自动建立 provider；缺失依赖时进程会 fail closed，不会静默使用日志，构建选择会写入 `io.aftercare.build.extras` OCI label。

在启动 Compose 或目标平台 Job 前，先在部署控制机运行 [`deploy/deployment_preflight.py`](../deploy/deployment_preflight.py)：

```bash
python deploy/deployment_preflight.py \
  --env-file /secure/path/aftercare.env \
  --output /secure/path/aftercare-preflight.json
```

它只读取配置形状和 Secret 文件元信息，不连接数据库、IdP 或镜像仓库，也不会输出 DSN 内容。它会 fail closed 检查不可变镜像 digest、迁移/运行 Secret 是否分离且为单行 UTF-8、两个 DSN 的 `sslmode=verify-full`、OIDC HTTPS 地址和连接池上下界；目标平台仍需在此之后验证权限、readiness、迁移和回滚。

`preflight` 的 JSON 输出是部署证据，不要只把终端截图写进验收记录。它带有
`verifier=aftercare-preflight`、规范化镜像 digest、TLS 模式摘要和空错误列表；后面用
`bind_deployment_evidence.py` 绑定时，验收器会检查它确实对应当前镜像：

```bash
python deploy/bind_deployment_evidence.py \
  --record deployment-acceptance.json \
  --preflight-evidence /secure/path/aftercare-preflight.json \
  --preflight-evidence-ref artifact://deployment/preflight.json \
  --tenant-isolation-evidence /secure/path/rls-evidence.json \
  --evidence-ref artifact://deployment/rls-evidence.json \
  --output deployment-acceptance-bound.json
```

绑定器会先验证 preflight，再绑定租户隔离证据；任一证据的镜像 digest 不同、来源字段缺失
或原始检查不是全通过，命令都会失败并保持验收记录未关闭。`preflight`、`tenant_isolation`
和 `pitr` 三项现在都必须有对应的机器证据才能标为 `pass`。

普通 Docker Compose 的 file secret 实际是 bind mount，不支持在 Compose 文件里强制 `uid`/`gid`/`mode`；宿主 secret 文件必须由部署工具创建并限制读取权限。Linux 控制机运行 preflight 时要求这些文件为 owner-only（`0600` 或更严格），权限过宽会 fail closed；Windows 本地开发不套用 Unix mode 检查。Kubernetes、Swarm 或云 secret projection 的属主和 mode 应在对应平台清单中固定并实测。

Secret 更新、受控重启、PostgreSQL 新 runtime role、readiness 验收和回滚顺序见[Secret rotation runbook](operations/secret-rotation.md)；指标后端、队列年龄告警和 OTel 接入见[运行观测接入手册](operations/observability.md)；目标环境 PITR 的物理基线、WAL、恢复器和证据验收见[PITR 实施与验收](operations/pitr-deployment.md)。进程在启动时读取 Secret，不在长生命周期连接池中热加载；不要在没有回滚窗口的情况下覆盖唯一的旧凭据。

选定目标环境后，用 [`deploy/deployment_acceptance.py`](../deploy/deployment_acceptance.py) 建立验收记录：

```bash
python deploy/deployment_acceptance.py \
  --template \
  --environment staging \
  --image-digest sha256:... \
  --output deployment-acceptance.json
```

记录固定包含 preflight、migration、runtime 权限、租户隔离、readiness、Worker 恢复、Secret rotation、rollback 和 PITR 九项检查。PITR 必须引用目标环境实际生成的物理基线清单、恢复报告和 `aftercare-backup pitr` 校验结果，不能用本机 Docker profile 或静态模板替代。把 `pitr` 标为 `pass` 时，记录还必须保存验证器输出中的 `verifier=aftercare-pitr`、`decision=pass`，以及 `manifest_sha256`、`evidence_sha256`、`base_backup_sha256` 三个摘要；这让发布检查可以识别手工写的通过状态，并把同一组 `pitr_evidence` 摘要复制到 `release-evidence.json` 的部署门中。每项必须附外部证据引用才可标为 `pass`；模板和未完成项只能得到 `hold`，使用 `--fail-on-hold` 可阻止部署流水线晋级。`tenant_isolation` 现在也由验收器结构化校验：通过项必须包含同一镜像 digest 的 `verifier=aftercare-rls`、`decision=pass`、`hardened=true`、被绑定的归一化证据自身 `evidence_sha256`/`evidence_bytes`、harden/verify 原始报告的非空 SHA-256/字节数、runtime role、至少一张租户表、空上下文零行，以及跨租户写入被拒绝、更新/删除不可见的探针结果。目标环境建议先用 [`deploy/verify_rls_evidence.py`](../deploy/verify_rls_evidence.py) 将 harden/verify 两份原始 JSON 绑定成一份摘要，再用 [`deploy/bind_deployment_evidence.py`](../deploy/bind_deployment_evidence.py) 写回验收记录；绑定器会按规范化 JSON 计算该摘要，要求显式外部证据引用、保留其他八项检查，并拒绝覆盖已有失败状态。单独一条配置截图不足以关闭该门。证据引用应指向平台日志、命令输出或演练报告，不应复制 Secret、完整 token 或客户数据。

## 发布和回滚

发布顺序固定为：备份与恢复点检查 → migration Job → `aftercare-migrate --check` → API readiness → 少量 Worker → 全量 Worker。应用镜像和 Dockerfile 的 Python/uv 基础输入都使用 digest，发布记录应同时保存镜像 digest、基础镜像 digest、Aftercare 版本、源码 revision、EGM Git SHA、Aftercare migration 版本和 EGM schema 版本。运行镜像把这些身份写入 OCI labels：`org.opencontainers.image.source`、`org.opencontainers.image.version`、`org.opencontainers.image.revision`、`org.opencontainers.image.licenses`、`io.aftercare.egm.git-sha`、`io.aftercare.schema.aftercare-migration` 和 `io.aftercare.schema.egm`。发布前应从实际镜像读取并与构建提交、迁移目录和 EGM schema 版本核对，例如：

```bash
docker inspect --format '{{index .Config.Labels "org.opencontainers.image.source"}}' "$IMAGE"
docker inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$IMAGE"
docker inspect --format '{{index .Config.Labels "io.aftercare.egm.git-sha"}}' "$IMAGE"
docker inspect --format '{{index .Config.Labels "io.aftercare.schema.aftercare-migration"}}' "$IMAGE"
docker inspect --format '{{index .Config.Labels "io.aftercare.schema.egm"}}' "$IMAGE"
```

仓库 CI 还会用 BuildKit 的 `docker-container` builder 导出 OCI 产物，打开 SPDX SBOM 和 SLSA provenance，并运行 [`deploy/verify_oci_attestations.py`](../deploy/verify_oci_attestations.py)。验证器检查 attestation manifest 的 subject 确实指向镜像 manifest、镜像 config 中的身份 labels、每个 blob 的 SHA-256、SPDX predicate 和 SLSA provenance predicate；`release-evidence` 会再把 OCI config labels 与 `docker inspect` 的镜像 labels 逐项比对。普通 Docker `docker` driver 不支持 attestation，发布流水线必须使用支持 attestation 的 BuildKit builder。

CI 同时固定 `pip-audit==2.10.1` 审计锁定 Python 环境，用固定 digest 的 Trivy 扫描加载的运行镜像，并用固定 digest 的 Gitleaks 扫描完整 Git 历史；工作流依赖的 GitHub Actions 也全部固定到不可变提交，上传三份报告。当前镜像门禁阻断可修复的 HIGH/CRITICAL OS 与 Python/library 漏洞，允许暂时没有上游修复的条目继续生成报告。目标镜像仓库仍应在发布时复扫，不能用一次 CI 结果替代持续扫描；平台级 secret protection 也应保持开启。

目标 registry 配置签名后，先由 cosign 完成密码学验证，再把验证输出规范化并绑定到 OCI manifest digest：

```bash
cosign verify \
  --certificate-oidc-issuer "$COSIGN_OIDC_ISSUER" \
  --certificate-identity-regexp "$COSIGN_IDENTITY_REGEXP" \
  --output=json "$IMAGE" > cosign-verify.json
python deploy/verify_cosign_evidence.py \
  --input cosign-verify.json \
  --image-digest "$OCI_IMAGE_DIGEST" \
  --certificate-oidc-issuer "$COSIGN_OIDC_ISSUER" \
  --certificate-identity-regexp "$COSIGN_IDENTITY_REGEXP" \
  --output signature-evidence.json
```

`verify_cosign_evidence.py` 不实现密码学本身；它校验 cosign 已验证输出中的 manifest digest、OIDC issuer 和工作流身份，并记录原始输出的 SHA-256。生成 release manifest 时同时传入 `--signature-evidence` 和 `--signature-source`，`image_signature` 门才会变成 `passed`；只伪造规范化 JSON、漏传原始输出或签名对应其他 digest 都会 fail closed。

目标 registry 的复扫也必须先绑定不可变引用。用目标仓库对同一个 `IMAGE@sha256:...` 运行 Trivy 后，执行 [`deploy/verify_registry_rescan.py`](../deploy/verify_registry_rescan.py)；它要求报告是 container image、引用或 RepoDigest 与目标完全一致，并拒绝任何漏洞。生成 release manifest 时成对传入 `--registry-rescan-evidence` 和 `--registry-rescan-source`，否则 `target_registry_rescan` 仍保持 `open`。最后传入由 [`deploy/deployment_acceptance.py`](../deploy/deployment_acceptance.py) 评估为 `pass`、且使用同一镜像 digest 的目标环境记录，`target_environment_deployment` 才会关闭。三个 gate 都来自证据时，才允许 release policy 返回 `promote`。

CI 的 `release-evidence` Job 会下载镜像身份、OCI attestation、Python 依赖、镜像漏洞、Git 历史扫描和本机 PITR profile 报告，使用 [`deploy/release_evidence.py`](../deploy/release_evidence.py) 生成一份机器可读的 `release-evidence.json`。Integration Compose 还会单独归档 `aftercare-rls harden/verify` 的 JSON 结果，便于把本机 RLS 回归与目标环境 `tenant_isolation` 证据区分开。使用 Kubernetes reference profile 的发布流水线可以额外传入 `--kubernetes-profile-evidence`；清单会验证该报告的固定 verifier、六项检查、空错误列表、三个 YAML 输入文件的字节数/SHA-256 和 immutable image digest，并记录报告原始文件摘要。这个附件只证明清单结构与镜像配置一致，不关闭目标环境部署门，也不证明已经连接 Kubernetes 集群。主清单把本次构建的镜像 ID、源码/EGM/schema 身份、OCI predicates、安全报告计数、五份基础输入文件的 SHA-256，以及三份 PITR 文件的摘要绑定放在同一个可归档产物中，并列出仍未完成的发布门（镜像签名、目标仓库复扫、目标环境部署）；它是发布审计入口，不代表签名或生产验收已经完成。

真正的晋级流水线应在目标环境补齐这些门后运行 [`deploy/release_policy.py`](../deploy/release_policy.py)。CI 当前也会运行它并归档 `release-decision.json`；由于签名和目标环境门尚未完成，结果应为 `hold`。清单包含固定的 `promotion_gates` 集合，每个门有 `open`/`passed` 状态，策略会校验它与 `unresolved_gates` 一致，删掉列表项不能绕过门禁。脚本只接受 schema v1 且安全状态为 `pass` 的清单；仍有未完成门时返回 `hold`，使用 `--fail-on-hold` 会以非零状态退出。当前仓库不会把本地 CI 证据自动晋级为生产发布。

当前 Dockerfile digest 针对 linux/amd64；增加 ARM 或其他架构时必须分别解析、构建和验收，不能复用这一摘要。

当前迁移只有向前路径，没有自动 down migration。应用回滚只允许回到仍兼容当前 schema 的镜像；不要在事故中删除表或倒改迁移台账。未来出现破坏性 schema 变更前，需要先补 expand/contract 两阶段策略和跨版本兼容矩阵。

进程存活看 `/healthz`，接流量看 `/readyz`。readiness 失败表示数据库不可达或 Aftercare schema 与镜像不一致。Worker 在 SIGTERM 后停止领取并受 `stop_grace_period` 约束；目标平台的租约、终止宽限和最大外部调用时长仍需一起测量。

## 尚需目标环境决定的事项

- PostgreSQL TLS、连接代理/RDS 参数、目标环境实际角色创建方式，以及 migration Job/`aftercare-rls` harden/verify 的运行证据；
- IdP claims、CaseGrant 运营、introspection 凭据轮换；
- 真实订单/物流/客服来源的原生验签与网络出口；
- 资源 request/limit、Worker 数量、连接池和队列年龄告警；
- OTel exporter、日志留存、目标环境 PITR、异地副本和经过演练的 RPO/RTO；
- 外部动作 provider 的幂等、UNKNOWN 对账和支付额度策略。

这些是目标环境验收项，不阻塞仓库提供稳定的容器、迁移身份和运行身份契约。
