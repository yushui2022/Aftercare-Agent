# ADR-0002：EGM 模块独立，默认嵌入 Worker

状态：证据集成基础已实现；完整业务运行时和生产准入未完成。

## 决策

EGM 保留独立代码仓库和 Python 包。Aftercare 的可信 Worker 通过
EvidenceApplication 在进程内调用它，多机共享 PostgreSQL。HTTP 是可选传输层，
不是因为代码有独立仓库就必须部署的微服务。

本决策替代 ADR-0001/0.5 指南的 HTTP-first 部署默认值，不撤销来源认证、证据门控、
租户隔离和业务权威的要求。历史探针和测试结果保留其版本语境。

## 实现边界

- EGM application：统一权限、输入验证、工单范围、schema 指纹、幂等、版本和审计。
- EGM storage：SQLite 本地开发；PostgreSQL 为多机 Worker 提供共享事务存储。
- AftercareEvidence：台账绑定的退款意图、可信连接器回执和固定措辞的完成声明。
- Aftercare 业务层：Case/Action 台账、租约 fencing、外部幂等和结果核对仍在实现；Action 的参数绑定审批门禁已落地，审批等待唤醒和真实审批身份仍待实现。
- 沙箱：只运行隔离工具，不获得 PostgreSQL 连接、EGM 存储对象或高权限身份。

## 并发取舍

同一工单的 EGM 操作在短事务中串行，不同工单可并发。模型和第三方 API 调用在事务外。
revision 防并发覆盖，operation_id 支持原样重放；两者都不替代任务租约，也不防止
两个不同工单对同一订单重复退款。后者要靠 Aftercare 的业务唯一约束和供应商幂等。

默认 EGM 自行提交。需要与业务写入原子提交时，显式加入同一个 PostgreSQL 连接的
外层事务。若不能共享事务，继续用 Outbox 投影；不能把“同进程/同数据库”当作原子性。

## 何时再拆为服务

多个不同语言的应用需要统一入口，或 EGM 需要单独的权限域、发布节奏、资源隔离时，
可使用同一个应用层的 HTTP 适配器。拆分后增加网络失败和跨事务一致性成本，应由
实际需求与观测结果决定，而不是用服务数量代表企业级。

验收与剩余风险见[嵌入式接入指南](../integrations/egm-embedded.md)。
