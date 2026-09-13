# ADR-0003：将 Token 身份与 Case 资源授权分离

状态：接受，D-01-03 实施中。真实 IdP、权限管理后台和 RLS 仍不在本 ADR 范围内。

## 背景

JWT/JWKS 验证只能证明“谁签发了这个主体”和 token 携带的粗粒度 scope，不能替代
业务资源授权。把 `tenant_id` 或一个缺失的 `case_ids` 直接解释为“该租户全部工单”，
会让 operator token 在 Case 撤销后继续可用，也无法表达单个 Case 的过期授权。

## 决策

1. `JwtJwksVerifier` 只负责签名、issuer/audience、时间、tenant、主体和粗粒度权限的
   验证。请求体、路径和模型输出不能修改这些字段。
2. PostgreSQL 的 `CaseGrant` 是 Case 访问的权威来源，按
   `(tenant_id, subject_id, case_id)` 保存授予的 scope、有效期、撤销时间和 revision。
   只有可信服务端 Repository 能写入授权；本轮不开放管理 HTTP。
3. 每个带 Case 的业务操作在自己的短事务中锁定并检查当前 grant。有效 grant 的权限与
   token scope 取交集；token 中的 `case_ids` 只能是额外的上界，不能绕过数据库 grant。
   没有 grant、已撤销或已过期均 fail closed。授权检查和业务读取/写入不能拆成“先检查、
   后执行”的无界窗口。
4. 新建 Case 时，创建者的 grant 与受理记录在同一个事务中写入；受理幂等重放不能为
   不同主体自动补授予。合成身份仍只在显式本地/测试开关下可用，真实 verifier 存在时
   强制关闭。

## 后果

这会增加一张授权表和每次 Case 操作的一次短查询，但撤销、最小权限和跨主体隔离有了
可审计的权威状态。JWT 无需携带大 Case 列表；后续可加入版本化缓存或 RLS，但不能把
缓存命中当作永久授权。当前实现仍未提供权限管理 UI、实时撤销/introspection、真实
IdP 演练或生产 RLS，不能据此宣称生产就绪。
