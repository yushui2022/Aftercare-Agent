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

### Case sequence allocation

新写入方使用 `EventRepository.append_event()` 传入 `DomainEventDraft`，由数据库在
同一个事务中分配下一个 `case_seq`。迁移 `014_event_sequences.sql` 的
`aftercare_case_event_sequences` 行在分配时加锁；如果 Case 已存在，先按 Case→sequence
顺序锁定 Case，再锁计数器，和审批、Review 的业务锁顺序一致。分配在事务回滚时也回滚，
因此不会产生可见的提交序号空洞。兼容路径 `append_outbox(DomainEvent)` 仍接受显式序号，
但新事件必须正好是当前高水位的下一个位置；完全相同的历史事件可以幂等重放，跳号写入会
被拒绝。新业务代码不应自行计算序号；真正的历史回填应走单独的审计导入路径。

### Approval and Review events

审批/人工 Review 的成功状态转换会在同一 Case 事务追加以下不可变事件：

| 事件 | 触发点 |
| --- | --- |
| `approval.requested` | 新建 PENDING 审批 |
| `approval.decided` / `approval.expired` | 首次批准、拒绝或过期 |
| `review.requested` | 新建 REVIEW 请求 |
| `review.decided` | 首次 CONTINUE/CANCEL 决定 |

重复请求或相同幂等决定只返回原记录，不再生成第二个事件。事件 payload 是
`aftercare_event_payloads` 中按 SHA-256 寻址的完整记录快照，Outbox 只保存引用，因而
后续审批状态变化不会篡改历史事件。快照仍是受保护的内部审计数据；发布器不能把它或
数据库连接交给模型/沙箱。事件追加失败会让外层业务事务整体回滚，不能出现“审批已生效
但事件缺失”。

`EventRepository` 的方法只负责持久化和幂等，不执行消费者业务逻辑。调用方必须把接收、
状态变更和 Outbox 写入放在同一个短事务中；处理函数不应持有事务等待网络或模型。

## 审批与人工 Review 事件

可信门控路径使用独立、可筛选的事件类型，而不是让消费者从状态快照猜发生了什么：

| 事件 | 产生时机 | 运行标识 |
|---|---|---|
| `approval.requested` | 首次创建审批请求 | 绑定审批 Run 时必有 `run_id` |
| `approval.decided` | 首次批准或拒绝 | 绑定审批 Run 时必有 `run_id` |
| `approval.expired` | 过期回收器首次结算请求 | 绑定审批 Run 时必有 `run_id` |
| `review.requested` | 首次登记人工 Review | 必有 `run_id` |
| `review.decided` | 首次人工继续或取消 | 必有 `run_id` |

这些事件和对应的审批/Review 行在同一个 PostgreSQL 事务中写入。事件 payload 仍然只是
`ArtifactReference`；迁移 `014_event_sequences.sql` 的 payload 表按引用保存不可变的完整
决策/请求快照，便于审计重放而不把正文直接塞进事件信封。`case_seq` 由 Case 行锁保护的
高水位分配器自动递增；同一个事件 ID 的重放返回原事件，不会消耗新序号。历史调用仍可
使用显式 `DomainEvent.case_seq` 的 `append_outbox`，但新事件只能写入下一个连续序号；这
避免 projection 因永久 gap 而卡住。重复事件 ID 仍按原事件幂等返回。

因此，消费者可以按事件类型驱动工作台、审计或通知投影，并用自己的 Inbox/application
记录去重。事件的存在只说明可信状态变更已经提交，不代表外部供应商动作已经成功；外部
动作仍须读取 Action Ledger 的状态和结果未知处理规则。

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
PostgreSQL 集成测试。真实 Broker 连接器应只实现 `EventPublisher.publish(event)`，
不得把数据库连接或高权限凭据交给 Agent/沙箱。

## Case 事件的 gap buffer 与投影水位

迁移 `005_projection.sql` 增加三个按 `(tenant_id, case_id, consumer_id)` 隔离的表：

- `aftercare_projection_positions` 保存每个消费者已经连续应用的 `last_case_seq`；
- `aftercare_projection_applied` 保存完整 `DomainEvent`，并同时约束 event_id 和
  case_seq 唯一，供重放和冲突检测使用；
- `aftercare_projection_buffer` 暂存前方有缺口的事件，约束同一消费者不能有两个
  相同 case_seq。

`ProjectionRepository.ingest()` 在调用方短事务内锁定单个 position 行。下一连续序号
直接写入 applied，并在同一事务中反复清空现在已连续的 buffer；跳过的序号只写入
buffer，返回 `buffer_gap`，不能在这里确认消息已被业务应用。重复的完整事件返回
`replay`；同一 event_id 换序号、同一序号换事件，或事件内容任何字段变化都会返回
`conflict`。因此，先到 seq=42 再到 seq=41 不会倒退水位，也不会丢掉 42。

这是一层持久的顺序账本，不是 SSE 或业务状态投影本身。业务投影更新、消费者应用
标记和该水位应由调用方放在同一事务；SSE 只读取授权后的投影快照，并在后续阶段绑定
租户、订阅范围和授权版本。不同 Case、租户或消费者各自锁定自己的 position，不会
被全局锁串行化。外部 Broker 的分区、保留和重放策略仍由适配器负责。

## 验证

真实 PostgreSQL 测试覆盖：迁移、Outbox replay、序列/身份冲突、Inbox replay、来源内容
冲突、不同消费者独立去重，以及发布器的 SKIP LOCKED 竞争、租约接管、attempt fencing、
退避重试、ACK 幂等、事务回滚和“发布后崩溃”重复交付。没有配置 `DATABASE_URL` 时测试
安全跳过。ProjectionRepository 专测覆盖乱序 seq=2 先到、seq=1 到达后自动 drain、
完整事件 replay、event_id/sequence 内容冲突、租户/消费者隔离和投影事务回滚。

## SSE 回放边界（A3-03）

`GET /v1/cases/{case_id}/events` 提供有界、按 `case_seq` 排序的 SSE 回放页。客户端可用
查询参数 `after` 或 `Last-Event-ID` 续传；服务端取两者较大的游标，因此重连不会倒退。
默认请求是持久回放页。传入 `follow=true` 可启用一个有界的 PostgreSQL polling tail：
每次查询使用短事务，等待期间不持有连接锁，最长等待由 `wait_seconds` 限制在 60 秒以内；
FastAPI 通过异步生成器串接轮询，数据库查询短暂放入线程执行，等待使用异步 sleep，避免
每个空闲连接独占一个同步 worker 线程。该实现是 broker-neutral 的小规模参考，仍应先回放
游标，再由 Kafka/NATS/Redis 等适配器接管高吞吐 live tail；它不是数据库 LISTEN/NOTIFY
或生产消息总线的替代品。
当前 API 支持显式合成身份（仅开发测试）或配置静态 JWKS 的 Bearer 验证；CaseGrant
资源授权已按短事务接入。订阅级授权（按 topic 或消费者再授权）、真实 IdP 演练和高吞吐
broker tail 仍未实现。

### 工作台实时订阅（A3-03-b）

`web/` 的 React 工作台按同一 `case_seq` 游标消费该端点，而不是自己发明进度：

- 请求 `follow=true&limit=200&wait_seconds=60`，与 `PostgresEventTail.validate` 的上限一致；
  后端按 60 秒有界返回，客户端在流正常结束后立刻用最后看到的 `case_seq` 作为 `after`
  续订。因此"常驻"是若干次有界读的串联，不是一个永不关闭的连接。
- 浏览器不能给 `EventSource` 附加自定义 Header，所以工作台用 `fetch` + `ReadableStream`
  读取响应体并增量解析 SSE 帧（`SseDecoder`）。合成身份不会退化成 URL 查询参数里的凭据。
- 时间线按 `case_seq` 去重并封顶 500 条：重连重放不会重复渲染，长会话不会无限增长内存。
- 工单列表使用 `(created_at, case_id)` keyset cursor 加载更多；决策请求为同一业务意图
  保留幂等键，网络丢响应后的重试不会意外生成第二个决定。
- 断线重连使用有上限的指数退避；`401`/`403` 视为终止错误，不做无意义重试。
- 界面显示 连接中/实时/重连中/已暂停，并可暂停实时改为一次性回放。

游标续传、去重和退避是纯函数，由 `web/src/sse.test.ts` 覆盖（13 项，`pnpm test`）。
