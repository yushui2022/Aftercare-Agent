# EGM 0.6：服务于 Aftercare 的嵌入式证据模块

更新日期：2026-09-20。EGM 0.6 为源码版本，尚未发布 PyPI。本仓库已有可执行证据
适配层，不是完整售后平台；本页说明已实现能力及尚未完成的生产条件。

## 1. 默认架构

```text
认证 / 调度（宿主负责可信身份和工单范围）
                    │
          Aftercare Worker + Harness
             ├─ 业务台账 / Action 审批门禁 / 租约
             ├─ AftercareEvidence 受限适配层
             │       └─ EGM EvidenceApplication（进程内）
             │               └─ PostgreSQL（跨 Worker 共享）
             ├─ 可信业务连接器（真实供应商接入待实现）
             └─ 按需沙箱（不持有 EGM 数据库权限）
```

EGM 是独立依赖包，不是必选独立服务。HTTP v1 仍可用，但它与进程内调用共用
application 层：去掉网络请求，不会去掉授权、校验、版本、幂等或审计。
每个可信 Worker 可以有自己的应用对象，持久状态只存共享数据库，不保存在某个
Worker 的 Python 对象中。SQLite 保留给本地开发，不作为多机共享文件数据库。

## 2. 已落地的代码

本仓库 aftercare_agent/evidence.py 提供 AftercareEvidence：

| 入口 | 调用者与约束 |
|---|---|
| register_refund | 可信编排器，将已登记的订单、action、金额和币种作为预期绑定 |
| ingest_receipt | 可信连接器，保存带原始观察时间的标准化回执；不允许模型充当连接器 |
| propose_completion | 受限模型工具，只选择节点和引用，不能自填金额、订单或自由事实文本 |
| context | 当前授权工单上下文；默认不注入长期记忆 |

RefundIntent 是业务台账快照，不是退款授权。适配层当前只覆盖退款证据这一条垂直切片；
它不调用真实付款 API，也没有实现订单、物流、审批和完整 Agent 循环。

不要把整个适配器对象暴露给沙箱或不可信 Python：它持有宿主能力。宿主只能注册经过
范围绑定的工具函数，将 propose_completion/context 暴露给模型；其他方法留在可信代码。
Principal 的身份来自已认证请求/调度，不来自模型参数。

## 3. 本地安装与运行

默认不再要求并排克隆或 editable 安装 EGM。Aftercare 的 pyproject/uv.lock 固定 EGM
提交 `5d1302e3eb799764c23d8a8e6872abf8547da4ee`，包含 0.6 的 application、来源约束撤回边界和
PostgreSQL 实现，只启用 postgres extra，不带入 EGM 的 dev/server extras。
准备 Python 3.13.15、uv 和 Git，在 Aftercare-Agent 根目录执行：

```sh
uv sync --locked
uv run --locked pytest -q
```

首次安装需要网络，不需要模型 API Key 或真实退款凭证。tests/test_evidence_adapter.py 使用合成回执与
本地 SQLite，验证成功回执准入、失败回执拒绝、固定声明、角色分离与幂等重放。
PostgreSQL 的完整并发/故障测试位于 EGM 仓库，不把适配层回归说成生产压测。

解释器、G 盘缓存、类型检查和非 editable 打包验收见[开发指南](../development.md)。
tests/test_package_contract.py 另外核对固定 Git 来源、类型标记、schema 和迁移资源；
它不会连接数据库。需要联调相邻 EGM 源码时使用单独环境，并记录偏离固定提交的事实；
默认来源断言不接受该环境作为可复现安装验收。

嵌入接线示例（身份仅示意，实际必须从认证层取得）：

```python
from evidence_gated_memory.application import EvidenceApplication, Principal
from evidence_gated_memory.storage.providers import SqliteProvider
from aftercare_agent.evidence import AftercareEvidence

scope = dict(tenant_id="merchant-1", case_ids={"case-1"})
adapter = AftercareEvidence(
    EvidenceApplication(SqliteProvider("./local-egm")), "case-1",
    orchestrator=Principal(subject="orchestrator", permissions={"task:write"}, **scope),
    connector=Principal(subject="connector", permissions={"evidence:write"},
                        source_systems={"refund_api"}, **scope),
    model_tools=Principal(subject="model-tools", permissions={"claim:write", "context:read"}, **scope),
)
```

多机部署时将 provider 换为 PostgresProvider；显式迁移、连接工厂和宿主事务示例见
[EGM 嵌入指南](https://github.com/yushui2022/Evidence-Gated-Memory/blob/5d1302e3eb799764c23d8a8e6872abf8547da4ee/docs/embedded.md)
（本轮锁定安装已从远端取得该提交；这不表示重新核验了远端 main 或 CI）。
迁移凭证与运行凭证分离，配置可靠 search_path、TLS、连接池和超时；这些不是示例自动完成的。

## 4. 并发和恢复的实际含义

每个操作先锁定 tenant_id/case_id 对应的行，在同一个短事务里完成请求重放检查、
expected_revision 校验、证据/事实变更、审计和操作回执。锁不会覆盖整个 Agent 的执行。
当前同工单读取也会串行；不同工单可以并发，但共享数据库仍有总容量限制。

- 原样重投：同一个 operation_id、身份和请求得到已提交响应，不重复创建对象。
- 改参数复用 ID：冲突，不能将重试变成新的业务操作。
- 不同请求同时携带旧 revision：一个提交，其余冲突后重新核对状态。
- 数据库锁超时：普通 provider 返回 busy；按有界退避重试原请求。
- 提交结果不确定：先以原请求重放，不能刷新 observed_at，也不能直接再执行退款。
- accepted=false：是已持久化、已审计的拒绝，会消耗 revision，不是可忽略的网络错误。

EGM 原子写入不等于支付 exactly-once。任务执行权、同订单跨工单竞争、预算和供应商
幂等都属于业务层，不能只凭 EGM revision 推断当前 Worker 有执行权。

## 5. 与业务事务的两种接法

默认 provider 每次调用自行提交；使用 HTTP 同样不共享业务事务。这时先保存权威
来源事件与 Outbox，再可重试导入 EGM，保存对象映射和导入进度。

如需要业务与 EGM 同时提交，显式使用 PostgresProvider.join(connection, tenant, case)，
且必须是业务事务正在使用的同一个连接。EGM 使用保存点，不提交或关闭宿主事务。
返回值在外层 commit 前仅是暂定结果，不能提前 ACK 队列或向外发布成功。
外部付款 API 无法被这个数据库事务包住，仍需 Action Ledger 和核对机制。

## 6. 验证记录与尚未达成的目标

以下 PostgreSQL 与全量 EGM 结果属于 **2026-09-06 的历史组件验收**，本轮打包任务未重跑：

EGM 在隔离 PostgreSQL 17.11 实例上进行了真实多进程竞争、提交前后进程退出、
重放、锁超时、跨工单独立进展、业务表与 EGM 外层事务一起回滚/提交的验证。
两个存储后端均拒绝失败状态及租户、工单、订单、action、金额、币种错配的完成声明。
EGM CI 增加 PostgreSQL 服务测试；本地验收不是远程 CI 通过声明。

该次本地结果：EGM 全量 **303 passed、2 skipped、5 warnings，78.12 秒**；
Aftercare 适配层 **4 passed，1.12 秒**。EGM 的两项跳过是 SQLite 参数下不适用的
PG 专项断言，不是未运行 PG 后端；警告来自测试/打包依赖的弃用提示。
环境为 Windows、Python 3.13.11、隔离 PostgreSQL 17.11，未使用真实业务凭证。

当前 Python 包与安装验收另记于[状态台账](../project-status.md)，不能拿历史 303 项结果
证明当前运行时或新的依赖组合已经通过 PostgreSQL 全量测试。

未完成：真实供应商签名验证、完整 Case/Run/Action 运行时、审批和租约 fencing、
生产部署、数据库 HA/恢复演练、RLS、保留/删除策略、业务压测与沙箱接入。
PostgreSQL 检索当前是工单内 JSONB 字面的子串查询，不保证 FTS5 排名或大规模搜索性能。
因此当前成果是有测试依据的企业级集成基础，而不是已经通过企业生产验收的整个平台。
