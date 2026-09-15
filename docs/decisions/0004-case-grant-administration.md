# ADR-0004：开放 Case 授权管理面

状态：接受，D-01-05 实施中。真实 IdP 权限映射、RLS、管理 UI 和生产审计留痕仍不在本
ADR 范围内。

本 ADR 显式替代 [ADR-0003](0003-case-grant-authorization.md) 决策第 2 条中“只有可信
服务端 Repository 能写入授权；本轮不开放管理 HTTP”的**后半句**。ADR-0003 的其余决策
（验签只证明签发方、CaseGrant 是访问权威、每事务短锁判定、受理时原子授予）继续有效。

## 背景

ADR-0003 把 `CaseGrant` 定为 Case 访问的权威，但只留下一个写入口：受理新 Case 时给创建者
授 `case:read`。此后没有任何生产入口能改动授权，于是租户内无法完成最基本的运营动作——把
一件工单移交给另一个操作员或主管。这不是“权限不够”而是“没有可调用的管理面”：`_authorized_case()`
对没有 grant 的真实身份一律 `403`，而 `_authorized_case_row()` 让“无权限”和“不存在”
不可区分，因此任何主体都无法为他人建立第一条授权。

反过来，直接提供一个“随便写 ACL”的端点，会把一张 Case 行的写权限变成租户级提权通道。
本 ADR 的两条约束——闭集与委派上限——就是为这条风险而设。

## 决策

1. 新增租户级 scope `grant:read` 与 `grant:admin`，只由身份提供者映射，不能由请求体声明。
   `GET /v1/cases/{case_id}/grants` 需要 `grant:read`；`POST /v1/cases/{case_id}/grants`
   与 `POST /v1/cases/{case_id}/grants/{subject_id}/revoke` 需要 `grant:admin`。
2. 管理权是**租户级角色**，不是 Case 级授权。`_administered_case()` 只校验租户级 scope
   加上“目标 Case 确实存在于该身份自己的租户内”，不解析调用者自己的 `CaseGrant`。若管理
   面也要求调用者先持有被管理 Case 的 grant，第一条授权就永远发不出去。跨租户与未知 Case
   统一 `403`，与既有单资源端点保持一致。
3. 可授予权限是**闭集** `GRANTABLE_CASE_PERMISSIONS`（`case:read`、`review:read`、
   `review:decide`、`approval:read`、`approval:decide`）。刻意排除 `case:create` 与
   `grant:*`：单张 Case 行不能放大成租户级权限。闭集之外的字符串一律 `400` 且不落库——
   否则它今天匹配不到任何东西，却可能在后续版本变成有效的权限。
4. **委派上限**：`authorize_case_grant()` 要求 `requested ⊆ actor.permissions`。管理员不能
   授出自己没有的权限，管理面因此无法自我提权；需要授出 `approval:decide` 的管理员必须
   自己先持有该 scope，角色分配属于 IdP 而不是本 API。空权限集合同样拒绝：撤销应走撤销
   端点，而不是“授予空集合”。
5. 替换与撤销都必须携带 `expected_revision`（乐观并发）。没有它，一次超时重试可能把同一
   行静默改写第二次；带过期 revision 的重放返回 `409`，与既有的幂等/冲突语义一致。
6. 租户、Case 与执行人全部来自认证上下文；请求体禁止 `tenant_id`、`case_id`、
   `granted_by`、`revoked_by`、`revision`，响应投影也不返回 `tenant_id`。
7. 每次 grant/revoke 在**同一个事务**内追加 `case_grant.granted` / `case_grant.revoked`
   不可变事件，并写入按 SHA-256 寻址的完整快照：授权变更必须留下审计轨迹，“授权已生效但
   事件缺失”不能发生。这两类事件没有 `run_id`——授权变更不是 Run 推进的一部分，强加
   Run 绑定反而会伪造不存在的执行关系。
8. 管理面改变的是**权威状态**，不是调用者的即时权限。`CaseGrant` 仍是“使用时”的权威：
   每个业务路由依然在自己的短事务内锁定并判定。管理面写入成功后，下一个业务请求才会看到
   新权限；撤销不会中断已经在途的请求。

## 后果

运营上，移交、临时授权与撤销第一次成为可执行操作，并可在 Case 事件流里审计。代价是新增
一组租户级 scope：如果 IdP 把它们发给普通操作员，等于交出 ACL 写入权，部署方必须把
`grant:admin` 限制在真正的管理角色上。列表接口按 `subject_id` 升序返回全部行（含已撤销），
故意不加 `FOR UPDATE`——管理视图不应长时间持锁，也不需要与业务写入竞争同一行。

仍未完成，因此不能据此宣称生产就绪：管理 UI、RLS、真实 IdP 权限映射与演练、管理动作的
生产审计留存、以及对“谁能成为管理员”的配置管理。
