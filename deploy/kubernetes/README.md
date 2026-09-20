# Kubernetes reference profile

这是一套平台参考清单，不是某个云厂商的生产发布包。它把已经在
`deploy/compose/docker-compose.deployment.yml` 固化的边界映射到 Kubernetes：
独立 migration Job、无 DDL runtime、文件 Secret、PostgreSQL `sslmode=verify-full`、非 root、只读 rootfs、探针、
资源边界和滚动更新。它不创建真实 Secret、不配置 Ingress/TLS、不提供 RLS、
对象存储或供应商连接器；迁移 023 已启用 runtime RLS，迁移 Job 会继续运行同一镜像的
`aftercare-rls harden`，随后单独的 [`rls-check-job.yaml`](rls-check-job.yaml) 使用 runtime
DSN 做可回滚跨租户探针。目标环境的 Secret projection、备份/队列复核和对应验收记录仍由
目标平台提供。

## 应用前替换的内容

在 [`reference.yaml`](reference.yaml) 和 [`migration-job.yaml`](migration-job.yaml)
中把两个 `registry.example/...@sha256:replace-me` 换成同一个已签名、不可变的
Aftercare 镜像引用，把 `aftercare-runtime-config` 中的 OIDC 三元组和
`AFTERCARE_TENANT_ID` 换成目标 IdP 与实际租户。Worker 当前按租户固定运行，不能
保留 `replace-with-tenant-id` 占位值。不要把 DSN、OIDC client secret 或模型 key 写进 YAML。

## 创建 Secret

Secret 文件由目标 secret manager 或受控部署机写入，Linux 文件权限至少限制为
owner-only。下面的命令不把 DSN 内容放进 shell 参数或仓库：

```bash
kubectl create namespace aftercare --dry-run=client -o yaml | kubectl apply -f -
kubectl -n aftercare create secret generic aftercare-migration-database-url \
  --from-file=database_url=/secure/aftercare/migration-database-url \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n aftercare create secret generic aftercare-runtime-database-url \
  --from-file=database_url=/secure/aftercare/runtime-database-url \
  --dry-run=client -o yaml | kubectl apply -f -
```

清单中的 `defaultMode: 256` 是八进制 `0400`。Secret projection 的实际属主、
mode、轮换和审计仍要在目标集群实测；清单中的 `AFTERCARE_REQUIRE_DATABASE_TLS=1`
会让进程拒绝不做主机名与证书校验的 DSN，`deployment_preflight.py` 只检查控制机
上的输入文件。

## 发布顺序

替换目标值后，先运行仓库验证器。它不连接集群，但会拒绝镜像漂移、占位租户、
OIDC/ConfigMap 不一致，以及 API、Worker、migration、RLS check 任一工作负载失去
非 root、只读根文件系统、禁止提权、capability drop 和 0400 Secret projection 基线：

```bash
uv run python deploy/verify_kubernetes_profile.py \
  --directory deploy/kubernetes \
  --image 'registry.example/aftercare-agent@sha256:<64 lowercase hex characters>' \
  --issuer 'https://idp.example/' \
  --audience 'aftercare-api' \
  --jwks-url 'https://idp.example/.well-known/jwks.json' \
  --tenant-id 'tenant-staging' \
  --output kubernetes-profile-evidence.json
```

验证器通过后再做客户端解析，不连接集群：

```bash
kubectl apply --dry-run=client -f deploy/kubernetes/reference.yaml
kubectl apply --dry-run=client -f deploy/kubernetes/migration-job.yaml
kubectl apply --dry-run=client -f deploy/kubernetes/rls-check-job.yaml
```

每次发布先删除旧 migration Job，再应用同一镜像 digest 的 Job，等待成功后才
更新 API/Worker：

```bash
kubectl -n aftercare delete job aftercare-migrate --ignore-not-found
kubectl apply -f deploy/kubernetes/migration-job.yaml
kubectl -n aftercare wait --for=condition=complete --timeout=10m job/aftercare-migrate
kubectl -n aftercare delete job aftercare-rls-check --ignore-not-found
kubectl apply -f deploy/kubernetes/rls-check-job.yaml
kubectl -n aftercare wait --for=condition=complete --timeout=10m job/aftercare-rls-check
kubectl apply -f deploy/kubernetes/reference.yaml
kubectl -n aftercare rollout status deployment/aftercare-api --timeout=10m
kubectl -n aftercare rollout status deployment/aftercare-worker --timeout=10m
```

### 保存 RLS 验收证据

两个 Job 的命令都会把机器可读 JSON 写到 stdout：migration Job 还会先输出
`aftercare-migrate` 的 schema 摘要，因此只提取带有 `verifier=aftercare-rls` 的那一行。
在受控验收目录中保存原始结果，再用同一个镜像 digest 归一化；不要把完整 Job 日志、DSN
或 Secret 内容复制进验收记录：

```bash
evidence_dir="${RUNNER_TEMP:-/tmp}/aftercare-rls-evidence"
mkdir -p "$evidence_dir"
kubectl -n aftercare logs job/aftercare-migrate \
  | jq -c 'select(.verifier == "aftercare-rls")' \
  > "$evidence_dir/rls-harden.json"
kubectl -n aftercare logs job/aftercare-rls-check \
  | jq -c 'select(.verifier == "aftercare-rls")' \
  > "$evidence_dir/rls-verify.json"
uv run python deploy/verify_rls_evidence.py \
  --harden "$evidence_dir/rls-harden.json" \
  --verify "$evidence_dir/rls-verify.json" \
  --image-digest 'sha256:<64 lowercase hex characters>' \
  --output "$evidence_dir/rls-evidence.json"
```

`rls-evidence.json` 的 `source` 字段保留两份原始报告的字节数和 SHA-256；将它与目标
环境的 Job 日志引用一起写入 `tenant_isolation` 验收项。命令返回非零时，目标环境验收保持
`hold`，不能用截图或手工填写的 `decision=pass` 替代。

要把归一化结果写回验收记录，可使用绑定工具。它要求显式提供外部证据引用，保留其他八项
检查原状，并拒绝覆盖已有的失败状态：

```bash
uv run python deploy/bind_deployment_evidence.py \
  --record deployment-acceptance.json \
  --tenant-isolation-evidence "$evidence_dir/rls-evidence.json" \
  --evidence-ref "artifact://deployment/rls-evidence.json" \
  --output deployment-acceptance-bound.json
```

API 使用 `/healthz` 存活探针和 `/readyz` 就绪探针；Worker 没有 HTTP 入口，
平台应根据进程退出、日志和队列年龄指标报警。`replicas` 和 resources 是起始
形状，必须用 `aftercare-capacity` 和目标环境观测重新定标。启用 RLS 时必须为每个
Worker 副本补 `AFTERCARE_TENANT_ID`；当前清单强制 `AFTERCARE_WORKER_REQUIRE_TENANT=1`，
跨租户 dispatcher 仍是单独的后续验收项。

## 回滚和边界

命名空间默认标记为 Kubernetes Pod Security restricted，API 还使用
readyz startupProbe 保护冷启动阶段，避免 liveness 在数据库和 schema 尚未就绪时
提前重启进程。
目标集群仍应确认实际启用的准入策略与组织级网络策略兼容。

应用镜像回滚只允许回到仍兼容当前数据库 schema 的已验收 digest；迁移只有向前
路径，不能用 `kubectl rollout undo` 假装撤销 DDL。破坏性 schema 变更前必须先
补 expand/contract 和跨版本兼容矩阵。公网 TLS、入口限流、网络策略、Pod 安全
策略、备份/PITR、真实 IdP 和 deployment acceptance 仍按
[`docs/deployment.md`](../../docs/deployment.md) 与目标平台 runbook 验收。

仓库回归会先用 PyYAML 检查清单的文档结构和关键安全字段；配置了目标集群后，
再运行上面的 `kubectl --dry-run=client`。这些检查只能证明 YAML 形状和字段可
解析，不证明集群权限、Secret projection、镜像签名、RPO/RTO 或业务恢复正确。
