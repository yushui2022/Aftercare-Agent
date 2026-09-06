# 持久运行时、消息与可观测性核查

核查日期：2026-09-06。以下区分官方机制与本文设计推论；不是部署或故障压测报告。

## 1. Kafka：事务边界不是整个业务世界

官方 4.3 设计文档说明：事务生产者可原子提交输出记录与消费位点，结合适当消费隔离构成 Kafka 内部的 read-process-write 语义；其他目的系统的 exactly-once 通常需要目的系统协作。默认不能把“消息被再次消费”解释成“应再次退款”。来源：[Kafka Design：Message Delivery Semantics / Using Transactions](https://kafka.apache.org/43/design/design/)。

**本文推论：**消费者已发出退款、尚未提交位点就崩溃，重放仍可能重复请求外部支付系统。需要稳定的业务 action_id、目的端幂等契约、动作台账与未知结果对账。Kafka 事务不会自动包住任意 HTTP API。分区有序也不等于异步业务完成顺序；按工单分区之外仍需版本检查和状态转换规则。

## 2. NATS JetStream：存储、交付、处理是三个确认点

当前官方文档已经迁移到新路径，优先引用以下页面，不依赖已归档 nats.docs 仓库的旧章节地址。

- 发布返回 PubAck 代表消息被流接收存储，不代表消费者处理完成。Nats-Msg-Id 在流的去重窗口内防止重复发布；发布超时可能是确认丢失，不代表没有写入。来源：[Publishing](https://docs.nats.io/learn/jetstream/publishing)。
- 显式确认消费者在 Ack Wait 到期后可能收到重投；double ack 等待服务端确认已记录消费确认，减少确认丢失导致的重投。它没有将用户的外部支付事务纳入同一个原子提交。来源：[Delivery and acknowledgment](https://docs.nats.io/learn/jetstream/delivery-and-acknowledgment)。
- 需要选存储、复制和故障域配置，而非只写“用了 JetStream 就持久”。来源：[Surviving node loss](https://docs.nats.io/learn/jetstream/surviving-node-loss)。

**本文推论：**外部操作完成到发送 double ack 之间仍有崩溃窗口。因此 double ack 不能替代业务幂等。多 worker 共享消费者适合分工，但重投和多条 in-flight 消息会破坏按到达时间推测业务完成顺序；需要工单状态版本保护。

## 3. Redis Streams：不是 Pub/Sub，持久性也不是无条件保证

Streams 支持消费组、pending entries、XACK，以及通过 XCLAIM / XAUTOCLAIM 转移闲置未确认消息。消费组应用应按至少一次交付处理，而不是假设 handler 只运行一次。来源：[Redis streaming](https://redis.io/docs/latest/develop/use-cases/streaming/)、[XAUTOCLAIM](https://redis.io/docs/latest/commands/xautoclaim/)。

Streams 的数据安全受 Redis 的持久化、复制、故障转移与裁剪策略影响。RDB 和 AOF 的耐久性不同，AOF everysec 在灾难下可能丢失最近约一秒写入；“XADD 成功”不是脱离配置的零丢失承诺。来源：[Redis persistence](https://redis.io/docs/latest/operate/oss_and_stack/management/persistence/)。

版本提示：当前官方 Streams 页面已说明 Redis 8.6 的生产端幂等去重能力，不应绝对写成“Redis Streams 完全没有生产去重”。该功能仍不代表退款、邮件等外部业务效果只发生一次。来源：[Redis Streams](https://redis.io/docs/latest/develop/data-types/streams/)。

**本文推论：**使用 Streams 做任务入口时，应配置并监控 PEL、重投次数、消息保留、内存/磁盘与恢复流程；业务台账不应只存在可随意裁剪的流里。

## 4. PostgreSQL Event Table / Outbox：一种应用设计，不是自带完整 Agent 队列

PostgreSQL 明确说明 SKIP LOCKED 可帮助多个消费者访问 queue-like table 时避免行锁争用，但会产生不一致视图，不适合一般业务查询语义。它只解决领取阶段的行锁争用，不自动提供超时回收、租户公平、重试或外部幂等。来源：[SELECT：The Locking Clause](https://www.postgresql.org/docs/current/sql-select.html)。

LISTEN 注册绑定数据库会话，会话结束自动清除；只通知当前监听者。官方也指出建立监听与初始读取之间的竞态，并建议先提交 LISTEN，再读取数据库状态。来源：[LISTEN](https://www.postgresql.org/docs/current/sql-listen.html)。

NOTIFY 在事务提交后交付，事务内重复的同频道同 payload 可以合并，payload 有尺寸限制。它适合“提醒你去查表”，不应作为断线后可从游标完整重放的任务真相源。来源：[NOTIFY](https://www.postgresql.org/docs/current/sql-notify.html)。

Outbox 将业务更新与待发布事件放在同一数据库事务中，随后由转发器/CDC 发布。Debezium 提供 Outbox Event Router，事件 ID 可用于消费端去重。来源：[Debezium Outbox Event Router](https://debezium.io/documentation/reference/stable/transformations/outbox-event-router.html)。

**本文建议：**本案例先以 PostgreSQL 的 case、run、step、action、event、outbox 表为持久事实；短事务领取并写 lease_owner / lease_until / fencing_token 后提交，绝不在持有行锁的长事务中调用模型或等人工。Outbox 转发成功、记录 sent 状态之前崩溃仍可能重复发布，故消费侧要 inbox 去重与幂等。事件表、可变任务队列表、outbox 各自承担不同职责，不能混称成同一条“事件流”。

## 5. 租约、心跳、fencing 与外部效果的边界

etcd 官方说明：客户端可能仍以为拥有已经过期的租约；租约本身不保证外部资源互斥。针对 etcd 键，版本与租约条件检查提供实际保护；保护外部资源时，外部资源也必须执行相应版本校验。来源：[etcd versus other key-value stores：Distributed locks](https://etcd.io/docs/v3.6/learning/why/)。

Redis 官方分布式锁文档同样建议 fencing tokens，尤其针对长耗时进程，并警告不要因为进程还活着就假设锁仍有效。来源：[Distributed Locks with Redis](https://redis.io/docs/latest/develop/clients/patterns/distributed-locks/)。

**本文设计推论：**

- 心跳只说明最近有续租/通信，不能证明业务正在取得进展。需分别记录 lease heartbeat、step progress、deadline。
- fencing_token 单调增加，数据库条件更新拒绝旧持有者；旧 worker 的外部 HTTP 请求却不会被数据库条件更新自动撤销。
- 工具网关可以校验当前 token，并将具体 action_id 持久化后调度；但“检查 token 后调用外部接口”之间仍存在时间窗口，不能声称凭这一检查彻底解决问题。
- 有副作用的动作最终仍需目标系统幂等、可查询回执、状态对账；无法确认是否成功时记录 UNKNOWN，不盲目用新 action_id 重试。
- 不同 attempt 可以共享同一业务 action_id；否则每次重试生成新幂等键会破坏防重。

## 6. OpenTelemetry：观测运行，不替代业务审计

OpenTelemetry traces 以 span 表示工作单元。Span Links 可表达异步排队后另一次执行与前一次执行的因果关系，不必把跨数天的工单硬塞入一个始终不关闭的 span。来源：[Traces：Span Links](https://opentelemetry.io/docs/concepts/signals/traces/#span-links)。

OTel 官方支持采样；未采样的 trace/span 不会像采样数据那样被处理并导出。因此普通 tracing 管线不能被假定为完整、不可篡改的业务审计台账。来源：[Sampling](https://opentelemetry.io/docs/concepts/sampling/)。

**重要版本事实：**官方语义规范首页已将 GenAI 指向独立仓库；新仓库 docs/gen-ai/README.md 当前明确 Status: Development。不能把基础 OTel 的成熟度直接等同于全部 GenAI 字段已稳定。应固定所采用的规范/SDK版本，升级时核对变更。来源：[OTel semantic conventions](https://opentelemetry.io/docs/specs/semconv/)、[GenAI conventions README](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/README.md)。

**本文建议：**每次活跃执行建立 trace，模型/工具调用建立子 span；消息中传递受信任的 trace context，恢复执行使用合适父子关系或 span link。case_id、run_id、step_id、action_id 建立观测与审计关联，但高基数 ID 不直接当通用 metrics label。批准记录、动作参数、政策版本、外部回执由独立持久审计数据保存；遥测脱敏并限制访问，默认不把客户邮件、密钥、完整 prompt 放入通用日志。

## 7. 本案例选型的简短结论

以下是工程判断，不是官方性能排名：

| 起点/变化 | 合理选择 |
| --- | --- |
| 业务数据库已用 PostgreSQL，规模尚未证明需要独立 broker | 先采用事务性任务表、事件表与 outbox |
| 需要独立持久分发、多个 worker 拉取和消息流能力 | 评估 NATS JetStream |
| 团队已有可靠 Redis 运维与明确的消息保留/耐久契约 | 可评估 Redis Streams |
| 多下游回放、数据平台集成、分区事件流已是核心需求 | 评估 Kafka |

四种方案都不免除任务状态机、审批、重试分类、租户配额、幂等与对账。持续增长的队列表示下游容量/资源不匹配；换 broker 不会自动提升模型配额或第三方 API 吞吐。
