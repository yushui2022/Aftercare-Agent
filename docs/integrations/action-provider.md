# 外部动作 Provider 回执契约

Action Ledger 先记录一个可信的业务义务，再由 provider 在事务外执行。Provider 不拥有
Case、审批或数据库连接；它只接收已批准的 `ActionRecord`，使用同一个
`provider_idempotency_key` 调用外部系统，并返回规范化 `ProviderReceipt`。

```text
ActionRepository: RESERVED → REQUESTED
        │  当前 Run claim + 审批/策略已核对
        ▼
ActionProvider.request(action)       （事务外）
        │  超时也不能新建 Action
        ├─ CONFIRMED(provider_reference, result_sha256)
        ├─ FAILED(failure_code)
        └─ UNKNOWN
             │
             └─ ActionProvider.lookup(action) 使用原幂等键
```

## ProviderReceipt 规则

- `action_id` 必须指向当前 Action；错 Action 的回执拒绝。
- 如果 Action 有 `provider_idempotency_key`，回执必须带相同值；不同值是冲突。
- `CONFIRMED` 必须同时包含供应商引用和结果摘要，不能带 failure code。
- `FAILED` 必须有稳定 failure code。
- `UNKNOWN` 仍然占用业务额度，不允许通过新 Action 绕过它。
- 终态回执重放必须保持相同字段；改变回执内容进入冲突。

实现应在短数据库事务之外调用供应商，随后重新领取/核对当前 Action 版本与 Run fence，
再调用 `ActionRepository.mark_receipt()`。供应商响应成功不等于 EGM 或本地事务已经提交；
本地写入失败时必须保留供应商引用并按原 Action 查询，不能把本地回滚解释为外部未执行。
当前 PostgreSQL 台账入口是 `ActionRepository.mark_receipt()`；它在同一短事务内重新核对
Action、案例范围、Run fence 和 provider 幂等键，再落状态。

## 供应商适配器需要自行完成的事情

供应商原生认证、请求签名、金额/币种映射、查询接口、状态枚举、证书轮换、限流和重试
策略不属于这个通用契约。没有可靠查询或幂等能力的供应商，在 `UNKNOWN` 状态下必须转
人工核对；队列、数据库事务和模型置信度都不能补出 exactly-once 保证。

仓库目前提供 provider-neutral 的 `ActionProvider`、`ProviderReceipt`、回执匹配校验、
PostgreSQL Action Ledger 和只用于 local/integration profile 的
`SyntheticActionProvider`。真实支付/退款 provider、额度预留和供应商回执联调仍未实现；
本页是接入边界，不是外部付款已经可用的声明。
