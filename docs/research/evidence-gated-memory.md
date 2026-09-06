# Evidence-Gated-Memory：代码评估与本地验证

评估日期：2026-09-06。结论对应固定提交 [a16e3de7ca51727d6f16b8d335bb75ce70e9b4f6](https://github.com/yushui2022/Evidence-Gated-Memory/tree/a16e3de7ca51727d6f16b8d335bb75ce70e9b4f6)，不自动适用于后续 main 或 PyPI 发布。

本页正文是修复前的历史评估：当时只读取上游代码并运行合成数据检查，没有修改 EGM 源码、调用真实模型或售后 API。接入决策见 [ADR-0001](../decisions/0001-evidence-gated-memory.md)。

## 后续状态：源码问题已修复，历史结果保留

2026-09-06 随后的 EGM 0.5.0 本地源码升级已修复以下路径：失败回执与错误订单被 JSON 契约拒绝；task_id 限制 Facts 和 L1/L2/L3 来源链；增加受认证的租户/工单 HTTP 边界、原子事务、幂等回执与 revision 检查。原始探针与 True/True 结果保留，**仅代表 a16e3de 修复前行为，不代表新源码**。

0.5 修复记录见[服务接入记录](../integrations/egm-service.md)；后续 0.6 已实现共享应用层、PostgreSQL 后端和 Aftercare 证据 Adapter，见[嵌入式接入](../integrations/egm-embedded.md)。EGM 仍不验证任意自然语言真值、外部签名、审批或退款幂等，完整 Aftercare 运行时未完成。下文的“当前”“本次”均指上面的固定历史提交。

## 1. 总结：有用，但需要准确定位

EGM 已有可执行的证据类型与来源门控、时效检查、事实依赖、任务图、候选记忆及上下文构建。它不是只有 README 或几个未接入的公式。

最值得 Aftercare 利用的，是“不能只凭模型一句话就宣布完成”的证据准入结构，以及来源下钻与失效传播。

当前实现仍把调用方视为可信库使用者。它不验证原始回执的完整业务含义，不具备租户鉴权或多 Worker 工作流语义。因此不能因为 GateResult.accepted 为 true，就直接宣称退款成功或批准执行退款。

## 2. 本次实际运行了什么

环境：Python 3.11.1、Pydantic 2.12.5、PyYAML 6.0.3、pytest 8.4.2。直接使用源码路径，不安装或替换 EGM 包。临时 workspace 与 pytest 数据位于 G 盘任务临时目录。

选取以下 13 个上游测试文件：

~~~text
tests/test_gates.py
tests/test_freshness.py
tests/test_source_allowlist.py
tests/test_state_gates.py
tests/test_transition_node.py
tests/test_cascade.py
tests/test_critical_expiry.py
tests/test_task_graph.py
tests/test_context_task_map.py
tests/test_memory_candidate_gate.py
tests/test_sqlite_connection.py
tests/test_adapter_metadata.py
tests/test_fact_dependency_storage.py
~~~

结果：**65 passed，1 warning，7.90 秒**。这是一次本地测试输出，不是延迟基准。

运行时通过 PYTHONPATH 指向 EGM 的 src，关闭字节码生成、pytest 自动插件加载与 cacheprovider，并指定独立 basetemp。警告是禁用自动插件加载后，上游配置项 asyncio_mode 未被注册；这批所选测试完成并通过。

此结果不表示跑过全量测试，也不覆盖真实 API、多租户攻击、跨进程竞争、磁盘损坏、负载与生产恢复。

## 3. 额外构造的三个边界探针

均使用内置 REFUND Schema、临时本地 workspace 与合成数据，未调用外部接口。

### 3.1 失败响应也能支持“已退款”

创建 order_id=ORD-123 的 refund_completion 节点，记录：

~~~json
{"order_id": "ORD-123", "status": "failed", "amount": 0}
~~~

来源标签为 refund_api，证据类型为 refund_api_response，随后请求“Refund for ORD-123 completed successfully”的 refund_completed Fact，并请求节点转 DONE。

实测：

~~~json
{"claim_accepted": true, "done_accepted": true}
~~~

原因：内置规则检查所需证据类型与时效，门控没有解析 status 并验证成功语义。这是业务适配层必须补足的条件，不应被解释成类型与时效门控没有执行。

### 3.2 另一订单的响应也能支持当前订单结论

节点与结论仍属于 ORD-123，证据正文与 metadata 则属于 ORD-999：

~~~json
{"order_id": "ORD-999", "status": "success", "amount": 1}
~~~

实测同样为 claim_accepted=true、done_accepted=true。

原因：TaskNode 的 anchors 与 Evidence / Claim 的业务字段没有在该门控中自动建立强制相等关系。挂到节点上不等于已校验租户与订单归属。

下面的最小合成示例可以在有依赖的 EGM 源码环境中复核前两个问题；它不执行真实退款：

~~~python
import json
from tempfile import TemporaryDirectory
from evidence_gated_memory import EvidenceGatedMemory, TaskNodeStatus
from evidence_gated_memory.schemas.builtin import REFUND

payload = {"order_id": "ORD-999", "status": "success", "amount": 1}
# 将 payload 换成 ORD-123 / failed / 0，可复核失败回执的情况。
with TemporaryDirectory() as workspace:
    m = EvidenceGatedMemory(workspace, REFUND)
    try:
        node = m.create_task_node(
            "case:ORD-123", "refund_completion", "Confirm ORD-123",
            anchors={"order_id": "ORD-123"},
        )
        ref = m.record_evidence(
            "refund_api_response", "refund_api", json.dumps(payload),
            source_system="refund_api", metadata={"order_id": payload["order_id"]},
        )
        result = m.assert_fact(
            "Refund for ORD-123 completed successfully",
            claim_type="refund_completed", evidence=[ref],
            metadata={"order_id": "ORD-123"},
        )
        done = m.transition_node(node.id, TaskNodeStatus.DONE, evidence=[ref])
        print(result.accepted, done.accepted)  # 此提交：True True
    finally:
        m.close()
~~~

执行时将系统临时目录指向允许使用的测试目录；不要使用真实客户数据或生产 workspace。

### 3.3 task_id 并非长期记忆访问控制

在同一 workspace 中创建 Case A 与 Case B。为 A 保存一条标记为 SYNTHETIC_CASE_A_ONLY_PREFERENCE 的对话与 persona atom，其 metadata 标明 Case A。

随后不给 query，只调用 build_context(task_id="case:B")。

实测：

~~~json
{
  "other_case_memory_in_default_context": true,
  "other_case_memory_with_long_term_disabled": false
}
~~~

默认长期记忆选择函数没有使用 task_id 或访问权限过滤，include_long_term=False 时不注入这些记忆。该结果证明 workspace 共享范围必须由宿主明确决定，不能将任务聚焦能力描述成多租户隔离。

关闭长期记忆只是初期减少暴露面的配置，不代替事实查询、read_ref 和文件访问的权限检查。

## 4. 源码支持的实现与边界

| 核查项 | 当前结论 | 固定提交源码 |
|---|---|---|
| 来源校验 | 检查声明的来源标签，不认证真实来源 | [gates.py](https://github.com/yushui2022/Evidence-Gated-Memory/blob/a16e3de7ca51727d6f16b8d335bb75ce70e9b4f6/src/evidence_gated_memory/core/gates.py#L92) |
| 原始响应语义 | 不校验金额、状态或订单相等关系 | [gates.py](https://github.com/yushui2022/Evidence-Gated-Memory/blob/a16e3de7ca51727d6f16b8d335bb75ce70e9b4f6/src/evidence_gated_memory/core/gates.py#L141) |
| 时间与 TTL | 调用方可传入时间及覆盖值；UNKNOWN 可以满足 fresh 的可用性判断 | [freshness.py](https://github.com/yushui2022/Evidence-Gated-Memory/blob/a16e3de7ca51727d6f16b8d335bb75ce70e9b4f6/src/evidence_gated_memory/core/freshness.py#L31) |
| 节点完成 | 只对匹配的 state_gates 执行要求；没有规则不自动拒绝 | [gates.py](https://github.com/yushui2022/Evidence-Gated-Memory/blob/a16e3de7ca51727d6f16b8d335bb75ce70e9b4f6/src/evidence_gated_memory/core/gates.py#L269) |
| 低层修改 | 存在手工状态、Fact 提交和记忆写入入口；不是防篡改服务边界 | [memory.py](https://github.com/yushui2022/Evidence-Gated-Memory/blob/a16e3de7ca51727d6f16b8d335bb75ce70e9b4f6/src/evidence_gated_memory/core/memory.py#L1280) |
| 长期记忆范围 | 长期选择按 query / limit，不由 task_id 提供 ACL | [context.py](https://github.com/yushui2022/Evidence-Gated-Memory/blob/a16e3de7ca51727d6f16b8d335bb75ce70e9b4f6/src/evidence_gated_memory/core/context.py#L70) |
| 存储 | SQLite WAL、5 秒 busy_timeout；无内建 tenant 权限体系 | [sqlite.py](https://github.com/yushui2022/Evidence-Gated-Memory/blob/a16e3de7ca51727d6f16b8d335bb75ce70e9b4f6/src/evidence_gated_memory/storage/sqlite.py#L320) |
| 复合提交 | ref 文件、索引、状态、审计与级联操作不在统一高层事务中 | [memory.py](https://github.com/yushui2022/Evidence-Gated-Memory/blob/a16e3de7ca51727d6f16b8d335bb75ce70e9b4f6/src/evidence_gated_memory/core/memory.py#L651) |
| 无环约束 | 显式 TaskEdge 已拒绝多节点环；不能泛化为全图、并发强制 DAG | [memory.py](https://github.com/yushui2022/Evidence-Gated-Memory/blob/a16e3de7ca51727d6f16b8d335bb75ce70e9b4f6/src/evidence_gated_memory/core/memory.py#L1173) |
| 候选记忆 | source span 校验与 CandidateAtom 晋升已存在，L2/L3 不能宣传为完整自动提炼 | [memory.py](https://github.com/yushui2022/Evidence-Gated-Memory/blob/a16e3de7ca51727d6f16b8d335bb75ce70e9b4f6/src/evidence_gated_memory/core/memory.py#L137) |
| 撤销传播 | 影响 Evidence / Fact，不自动回滚节点 DONE 或外部动作 | [memory.py](https://github.com/yushui2022/Evidence-Gated-Memory/blob/a16e3de7ca51727d6f16b8d335bb75ce70e9b4f6/src/evidence_gated_memory/core/memory.py#L1334) |

## 5. 哪些宣传与指标本次不采用

没有复跑需要模型 API 的 tau-bench / tau2-bench，没有复核其胜率因果归因，不将小样本结果或上下文块压缩比直接转换成 Aftercare 的准确率提升或整单成本下降。

65 个测试通过意味着其覆盖的行为得到复核，不意味着不存在其他错误放行。三项额外探针正好说明：测试结果与业务安全边界必须一起报告。

本次结论是“适合有边界地进入 PoC”，不是“已经证明可直接部署为企业记忆服务”。
