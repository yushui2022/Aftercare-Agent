# Aftercare 接入 EGM 0.5.0：服务契约与验收

更新日期：2026-09-06。本次实现位于独立的 Evidence-Gated-Memory 本地源码工作树，目标版本 0.5.0，尚未发布或推送。本仓库仍是 Aftercare 架构文档，没有因此自动获得完整售后运行时、PostgreSQL Adapter 或生产部署。

## 1. 修复了哪些问题

修复前记录保留在[代码评估](../research/evidence-gated-memory.md)，对应 a16e3de，不应与新源码结果混为一谈。

| 风险 | 本次实现 | 仍需业务系统负责 |
|---|---|---|
| 失败回执被当作退款完成 | 从存储的原始 JSON 校验 status，完成只接受 success/completed | 可信连接器认证外部响应并正确映射供应商状态 |
| 另一订单回执支持当前订单 | REFUND 绑定 order_id；AFTERCARE 进一步绑定 tenant/case/order/action/amount/currency | 预期 action、金额与订单来自真实操作台账，不能由模型编造 |
| task_id 未隔离长期记忆 | Facts 与 L1/L2/L3 按任务及来源链过滤，不再回退全库；HTTP 按租户＋工单隔离 workspace | 跨案件共享经验的授权、脱敏、保留与删除策略 |
| 半写入、重复导入和并发覆盖 | 高层事务、持久 operation_id 回执、revision CAS、schema fingerprint | PostgreSQL Outbox、投影进度与外部副作用幂等 |
| 只能嵌入本地 Python 进程 | 增加可选 HTTP 服务、认证、角色权限与 egm-server 启动入口 | TLS、服务运维、租户配额、容量验证与多机架构 |

这些是可执行代码与本地回归覆盖，不是仅添加设计字段。它们也不证明任意自然语言结论真实：退款事实的展示文本应由核验过的结构化字段渲染，模型自己的叙述单独保留。

## 2. 运行位置与角色

~~~text
Aftercare Worker / Harness
  ├─ 查 Case / Action Ledger、审批、执行权 → Aftercare PostgreSQL
  ├─ 模型工具代理（claim:write、context:read）→ EGM HTTP
  ├─ 可信编排器（task:write、transition:write）→ EGM HTTP
  └─ 可信连接器（evidence:write、指定 source_system）→ EGM HTTP
                                                │
                                 tenant + case 独立 workspace
                                 SQLite v4 + 可恢复文件投影

按需沙箱：执行浏览器/脚本/附件处理，不持有 EGM 数据目录或管理凭证
~~~

EGM 是独立证据服务，不是 Harness，也不是沙箱。多个 Harness 可经 HTTP 调用它；不用把 SQLite 文件挂载给每个 Worker 或 E2B 环境。Bearer 身份由服务端配置固定租户、角色与 case_ids。模型进程不能拿到连接器或编排器的凭证。

同一宿主可运行多个 EGM Worker，每个请求独立连接。当前同一工单的 HTTP 读写经事务串行，不同工单使用独立数据库。不能用多个 K8s 节点共享网络盘写 SQLite 来宣称高可用。跨机共享事务存储仍是后续工程。

## 3. 本地源码启动与验收

本次源码路径为 G:/Projects/Evidence-Gated-Memory。需在所选 Python 环境安装该源码的可选服务依赖，而不是假设 PyPI 已有 0.5.0：

~~~powershell
Set-Location G:\Projects\Evidence-Gated-Memory
$env:PIP_CACHE_DIR='G:\DevCache\pip'
python -m pip install -e '.[server,dev]'
python -m evidence_gated_memory.server --data-root G:\Data\Aftercare\egm --tokens-file G:\Data\Aftercare\secrets\egm-tokens.json
~~~

默认只监听 127.0.0.1:8787。示例凭证文件路径需要部署者先通过秘密管理创建；仓库没有默认管理员密钥。权限字段、请求示例及返回语义见 EGM 仓库的 docs/service.md。本文不包含可直接拿来上线的真实凭证。

可以在不启动网络监听、不调用模型或退款 API 的情况下验证接入链路：

~~~powershell
python -m pytest tests/test_aftercare_integration.py tests/test_server.py tests/test_evidence_contracts.py -q
~~~

test_aftercare_integration.py 验证同一案件先记录失败回执并拒绝完成，再记录成功回执、准入事实、推进验证节点；重复导入不增加 revision，重新创建服务后仍能重放原操作与读取原文，另一租户不可读取。其他测试覆盖错订单、错金额、错币种、旧观察时间、事务回滚与并发版本冲突。

这是 EGM 的离线集成验收，不是 Aftercare 完整业务联调、性能压测或生产故障演练。

本次源码全量回归：**269 passed，5 warnings，74.18 秒**。测试包括 wheel 包内服务代码/Schema、确定性本地 benchmark、真实多进程竞争及提交前后进程崩溃恢复。5 项警告来自已安装 Starlette/httpx 与 setuptools/wheel 的弃用提示，不是用例失败；密钥扫描与两个仓库的 diff whitespace 检查也通过。没有运行真实模型/退款 API、生产压测或部署。

## 4. 每一步 HTTP 调用的含义

所有写入携带 operation_id、expected_revision；除 /health 外业务路由需要认证。

| 步骤 | 路由 | 谁调用、输入来自哪里 |
|---|---|---|
| 建预期动作节点 | POST /v1/cases/{case_id}/nodes | 编排器从 Action Ledger 取 order_id/action_id/amount_minor/currency；EGM 注入认证租户及工单 |
| 导入回执 | POST /v1/cases/{case_id}/evidence | 可信连接器提交原始观察时间与标准化 JSON，不允许模型代写 source_system 或 TTL |
| 提议结论 | POST /v1/cases/{case_id}/facts/assert | 模型工具提交 node_id、claim_type 与 evidence_refs；服务从既有节点取得绑定字段 |
| 完成验证节点 | POST /v1/cases/{case_id}/nodes/{node_id}/transition | 编排器请求 done，再独立检查节点契约，不依赖模型口头宣布完成 |
| 读取上下文 | GET /v1/cases/{case_id}/context | 固定当前工单；默认 include_long_term=false |
| 下钻原文 | GET /v1/cases/{case_id}/refs/{evidence_id} | 仅返回授权工单中的证据 |

AFTERCARE 的退款回执标准化内容：

~~~json
{
  "tenant_id": "merchant-1",
  "case_id": "case-100",
  "order_id": "order-100",
  "action_id": "action-100",
  "amount_minor": "2500",
  "currency": "USD",
  "receipt_id": "provider-receipt-100",
  "status": "completed"
}
~~~

金额是最小货币单位的正整数字符串，不是浮点数；货币精度与合法币种由连接器核对。receipt_id 是执行后供应商分配的非空标识，不要求执行前已有。失败、超时或待处理原文也可以保存，但不能因“证据导入成功”就当作“退款成功”。

observed_at 必须有时区、不在未来；重放保留第一次观察时间。旧回执不能因为今天重新导入就获得新的时效。真实重新查询供应商可以产生新的核验观察，但不得伪造时间延长 TTL。

HTTP 200 仅说明请求处理完成。对事实/转换必须检查 result.accepted；false 是已审计的业务拒绝，也消耗一个 revision。错误 schema 指纹、复用 ID 改参数、过期 expected_revision 返回 409；SQLite 繁忙返回 503。

## 5. 与 PostgreSQL、租约及消息重投的衔接

Aftercare 先在 PostgreSQL 同一事务中保存业务事件与 Outbox，再投影到 EGM。原始 provider 响应、标准化版本、policy 版本、action_id、event_id 与输入摘要都应留在权威记录中。

operation_id 对应一个稳定的投影步骤，不要与外部支付幂等键混为一谈。请求超时后原样重发同一请求；成功回执丢失也会获得之前的 EGM 对象 ID 与响应。不能重试时顺手更新 observed_at。对明确 409 的新操作，先读取最新版本并重新核对意图，再选择新的操作请求。

EGM revision 只解决当前工作区的提交冲突，不是 Aftercare 的 fencing token。旧 Worker 即使读取了最新 revision，也可能在其权限范围提交一个新操作；业务网关仍必须验证租约代次与权威状态。EGM 的操作去重不保证退款、邮件或补发 exactly-once。

EGM schema v4 将新证据原文、索引、审计、操作回执放进 SQLite 事务；refs/JSONL 为提交后投影。这解决了 EGM 内部的半写入问题，但没有把 PostgreSQL 与 EGM 变成一个跨库原子事务。投影滞后时等待或转人工，不能用陈旧 accepted 结果推进高风险动作。

TTL 过期表示不能继续支持当前判断，不表示历史退款被撤销；EGM 不会替你重发退款，也不会自动撤销已完成节点。复核、补偿与业务关闭条件属于 Aftercare。

## 6. 仍然未完成的部分

- Aftercare 真实连接器、Case/Run 执行流、审批台账与 PostgreSQL Outbox 集成。
- EGM PostgreSQL 后端、跨机服务高可用与在线 schema 迁移。
- 生产压测、故障域验证、数据保留/清理、网关配额与集中身份接入。
- HTTP 长期记忆写入/晋升、证据撤销与完整管理接口；当前有意不暴露这些高权限能力。
- 任意自然语言真实性、真实供应商签名或完整退款资格自动判定。

先完成离线业务 Adapter，再进入影子运行。不要把 EGM 服务可运行与整个 Aftercare 已经企业级上线画等号。
