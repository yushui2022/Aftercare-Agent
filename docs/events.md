# PostgreSQL Inbox/Outbox

A2-01 的第一块可靠事件能力使用 PostgreSQL，而不是提前引入 Kafka、NATS 或 Redis。
业务状态和 Outbox 可以在同一个短事务里提交；发布器之后再把 Outbox 转发到外部总线，
因此外部总线暂时不可用不会造成业务状态“已成功但事件不存在”。

## 三个持久化边界

- **Outbox**：按 `(tenant_id, event_id)` 保存版本化 `DomainEvent`，并用
  `(tenant_id, case_id, case_seq)` 防止同一工单出现两个不同的顺序事件。
- **Inbox**：按 `(tenant_id, source_id, source_event_id)` 去重外部来源；相同键但内容、
  身份或原始接收时间变化会报冲突，不会静默覆盖。
- **Consumer application**：每个消费者独立使用 `(tenant_id, case_id, consumer_id, event_id)`
  记录应用标记，所以审计投影和通知投影可以分别消费同一事件。

`EventRepository` 的方法只负责持久化和幂等，不执行消费者业务逻辑。调用方必须把接收、
状态变更和 Outbox 写入放在同一个短事务中；处理函数不应持有事务等待网络或模型。

Wait 持久化现已落地：`WaitRepository` 用 Case→Run→Wait 的行锁顺序实现
PENDING→ACTIVE、早到回执重查、回复优先的超时解析和唯一 wakeup。Inbox 信号只有在
同一事务提交后才能确认；当前 Run 不再绑定该代次时，旧回执只保留审计记录，不会推进
新代次。业务入口应传入 `wait.resolved` `DomainEvent`，使等待结算、Run READY、wakeup
和 Outbox 同事务提交；低层测试可省略 event 以验证纯持久化。

当前仍未实现：外部 Broker 发布器、事件分区/保留策略、乱序 gap buffer、SSE 投影和
跨实例长驻调度。这些属于 A2 后续任务。

## 验证

真实 PostgreSQL 测试覆盖：迁移、Outbox replay、序列/身份冲突、Inbox replay、来源内容
冲突、不同消费者独立去重。没有配置 `DATABASE_URL` 时测试安全跳过。
