# ADR-0015：目标部署参考环境与租户 RLS 边界

状态：接受为默认参考路径，尚未代表某个云环境已选定或已部署

日期：2026-09-20

## 背景

Aftercare 已有 local、integration 和 deployment 三种运行配置。local 和
integration 使用合成身份与临时 PostgreSQL，deployment 还缺一个可以持续验收的
真实环境基准。当前 API、Worker 和 Repository 都显式携带 `tenant_id`。迁移
`023_tenant_rls.sql` 已为带租户列的 Aftercare 表启用 RLS，runtime 角色设置为
`NOBYPASSRLS`；镜像现在提供 `aftercare-rls harden/verify`，可在迁移后把 owner
也纳入 `FORCE ROW LEVEL SECURITY` 并用 runtime DSN 做回滚探针。目标环境仍未完成
Kubernetes 实测、外部 IdP 和备份/队列复核，因此本 ADR 不把本机结果写成生产隔离完成。

## 决策

### 1. 默认真实部署参考

将 **Kubernetes + 托管 PostgreSQL** 作为默认 deployment profile 的参考组合：

- Kubernetes 负责 API、Worker、migration Job、Secret 投影、探针、滚动更新和资源边界；
- PostgreSQL 承担业务事务、租约、队列和 RLS；
- local/integration 继续使用 Compose，不为了模拟 Kubernetes 而强制引入 Helm、Broker
  或云厂商 SDK；
- 云厂商、区域、托管 PostgreSQL 产品和真实 IdP 仍保持可替换，必须在试点前单独记录。

这是一条验收基线，不是未经用户选择就执行的生产部署授权。

### 2. 数据库角色分层

生产至少分为三类身份：

1. **migration/owner**：执行迁移和受控结构维护，可以管理 RLS 策略；不被 API 或
   Worker 使用；
2. **runtime**：API、Worker 和发布器使用，`NOBYPASSRLS`，没有 schema DDL 权限，
   只能通过事务内租户上下文访问业务表；
3. **backup/restore**：由备份工具或平台托管，权限和生产运行时分离，恢复后的验证
   不能借用 runtime 凭据。

现有 `aftercare_migrate` / `aftercare_runtime` 分离继续保留。RLS 上线前必须在目标
   PostgreSQL 实测角色属性，而不能只看 SQL 文件。

### 3. 租户上下文只存在于事务边界

每个需要访问租户数据的短事务，在第一条业务 SQL 前设置事务本地上下文，例如：

```sql
SELECT set_config('aftercare.tenant_id', $1, true);
SELECT set_config('aftercare.subject_id', $2, true);
```

上下文由已验证的 `AuthContext`、已锁定的 Run/Case scope 或受控 Worker claim
产生，不能来自请求体、模型输出、浏览器参数或未经核对的连接器答案。使用
`SET LOCAL`/第三参数为 `true`，让上下文在提交、回滚或连接归还时自动消失，避免
有界连接池把前一个租户的身份带给下一个借用者。

缺少上下文、上下文为空、上下文与业务参数不一致时，策略必须拒绝访问或写入。
实现上，事务接缝会把两个自定义 GUC 显式置空；PostgreSQL 对空的自定义 GUC
返回空字符串，因此 RLS 策略应使用 `NULLIF(current_setting('aftercare.tenant_id', true), '')`
来把它解释为“没有租户上下文”。
业务 SQL 继续显式带 `tenant_id`；RLS 是纵深防御，不取代 Repository 的范围检查、
CaseGrant 或 Run fencing。

### 4. RLS 的实施顺序

RLS 按以下顺序实施，当前完成到 integration 验收：

1. 先增加一个统一的事务租户上下文接缝，并让 API、Worker、SSE polling、Outbox、
   备份后的验证路径明确声明上下文来源；
2. 在独立迁移 `023_tenant_rls.sql` 中为所有带 `tenant_id` 的业务表增加
   `ENABLE ROW LEVEL SECURITY`，策略使用 `current_setting(..., true)`，上下文不存在
   时不返回行；迁移完成后由 `aftercare-rls harden` 检查 approved policy、runtime
   角色继承链并启用 `FORCE`；
3. 已在 integration profile 以 runtime 角色验证跨租户 SELECT/INSERT/UPDATE/DELETE、
   空上下文、连接池复用、API readiness 和合成 vertical slice；目标环境仍需以同一
   镜像运行 migration harden 与 runtime verify；
4. 全局调度器、租户公平队列和迁移/备份操作不能偷偷绕过策略。若某个查询必须跨租户，
   设计一个最小化、可审计的数据库函数或独立运维身份，并为它建立单独的权限和回归，
   不把 `BYPASSRLS` 加给 API/Worker。

### 5. 验收门

RLS 切片只有同时满足以下条件才能标记完成：

- 两个租户在真实 PostgreSQL 中各有数据；runtime 角色没有 `BYPASSRLS` 和 DDL 权限；
- 正确上下文只能读写本租户，错误上下文、空上下文和跨租户外键操作均被拒绝或不可见；
- API Bearer 身份、CaseGrant、Worker Run claim 和 SSE 短轮询都使用同一个上下文接缝；
- 连接池借还后不会残留租户上下文，失败事务也不会把上下文泄漏到下一次借用；
- migration/backup 身份与 runtime 身份分离，schema readiness、备份和恢复演练仍可运行；
- integration Compose 已保存可复核的 SQL、CLI 日志和结果；至少一个 Linux/Kubernetes 目标环境仍待保存同等证据，并在备份/队列路径复核后运行同一 harden/verify；
- 失败时 fail closed，并且 release evidence 将 RLS、角色属性和目标环境结果绑定到镜像
  digest，而不是只记录“迁移成功”。

## 不在本 ADR 中决定的事项

- 具体云厂商、托管 PostgreSQL 产品、区域和网络拓扑；
- 具体 OIDC/IdP 供应商与 claim 映射；
- RPO/RTO 数值、容量目标、OTel Collector 和对象存储产品；
- 是否引入真实沙箱、Broker 或支付供应商。

## 风险与回滚

RLS 策略上线前必须保持 expand/contract 兼容，旧版本 API/Worker 在同一 schema
期间不能因为缺上下文而误删或误读数据。若目标环境发现策略与连接池、备份或队列
不兼容，应停止发布并回到仍可验证的旧镜像/策略版本；不能用 `kubectl rollout undo`
假装撤销已执行的 DDL。任何临时绕过都只能在独立运维身份中短时、可审计地完成，不能
通过给 runtime 授予 `BYPASSRLS` 解决。
