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

## Outbox 发布器

迁移 `004_outbox_delivery.sql` 在 Outbox 行上增加独立的交付状态：
`PENDING → CLAIMED → ACKED`。发布器使用短事务和
`SELECT … FOR UPDATE SKIP LOCKED` 批量领取到期事件，设置
`delivery_owner`、`delivery_lease_until`，并递增 `delivery_attempts`。领取事务
提交后才调用 Kafka/NATS/HTTP 等外部发布器；网络调用绝不能持有数据库事务。

确认和重试必须在新的短事务中提交。确认条件包含 owner、未过期租约和
`delivery_attempts`，所以旧进程即使与新进程使用同一个 owner 名称，也不能覆盖新
一轮结果。发布失败会清除租约、保存经过调用方清理的错误码并按
`next_attempt_at` 延迟重新进入 PENDING；租约过期也会被其他实例回收。发布成功后
ACK 是幂等的。进程在“外部发布成功、ACK 尚未提交”之间崩溃时，事件可能重复发布，
这是有意保留的 **at-least-once** 边界，消费端仍必须使用 Inbox/业务幂等键。

`runtime.publisher.OutboxPublisher` 提供这一循环，`FakePublisher` 用于本地和
PostgreSQL 集成测试。当前实现仍未包含 Broker 特定的分区/保留策略、乱序 gap
buffer、SSE 投影和跨实例长驻调度；这些是后续任务。真实 Broker 连接器应只实现
`EventPublisher.publish(event)`，不得把数据库连接或高权限凭据交给 Agent/沙箱。

## 验证

真实 PostgreSQL 测试覆盖：迁移、Outbox replay、序列/身份冲突、Inbox replay、来源内容
冲突、不同消费者独立去重，以及发布器的 SKIP LOCKED 竞争、租约接管、attempt fencing、
退避重试、ACK 幂等、事务回滚和“发布后崩溃”重复交付。没有配置 `DATABASE_URL` 时测试
安全跳过。
