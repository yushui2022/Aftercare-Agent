# 沙箱、Harness 与 Grok Bot：事实核查笔记

核查日期：2026-09-06。以下区分公开事实与本文设计判断；不把产品介绍当作经过本文验证的性能承诺。网页与 main 分支会变化，实施时应固定版本重新核对。

## 1. E2B：不是只有 SDK 开源，也不是任意服务器上一键安装

- 官方公开了 SDK/CLI 仓库以及 e2b-dev/infra 基础设施仓库；infra 标注 Apache-2.0，包含平台后台，并链接自托管指南。README 当前列出 GCP 和 AWS（Beta），Azure、通用 Linux 机器未标为已支持。[官方 infra 仓库](https://github.com/e2b-dev/infra)
- 当前基础设施架构使用 Firecracker microVM；常规模板启动通过恢复预启动快照实现，并使用按需加载内存页、写时复制根文件系统。API 控制面决定放置，节点 orchestrator 管 VM 的执行与网络。不能把 E2B 准确地简化为“启动 Docker 容器的 SDK”。[官方架构说明](https://github.com/e2b-dev/infra/blob/main/docs/ARCHITECTURE.md)
- 自托指南涉及 Terraform、Packer、Nomad/Consul、PostgreSQL、域名和云资源配置。Firecracker 节点要求可用的硬件虚拟化能力，具体云实例需支持裸金属或嵌套虚拟化。此处的运维规模和支持矩阵，应按指南及选定版本核查，不能宣称任意廉价 Linux VPS 都可直接部署。[官方自托指南](https://github.com/e2b-dev/infra/blob/main/self-host.md)
- 官网商业交付还列出 BYOC、on-prem、self-hosted，范围可能不同于公开仓库当前开箱即用支持项。商业方案可联系厂商，但不能拿商业介绍推断开源部署脚本已经完整支持所有环境。[E2B 官网](https://www.e2b.dev/)

本文设计判断：E2B 托管适合希望外包 VM 执行基础设施的团队；E2B 自托意味着团队还需要运维控制面、虚拟化节点、网络、容量与升级。两者都不替业务系统提供“退款只执行一次”的保证，也不替代工单状态机。沙箱快照只覆盖相应执行环境的状态，不自动快照第三方订单、邮件、支付系统。

## 2. Kubernetes Agent Sandbox：生命周期控制器，不是 LLM 调度器

本文“ K8s Agent Sandbox ”明确指 kubernetes-sigs/agent-sandbox，而不是把任意 Kubernetes Deployment 改名叫 Agent Sandbox。

官方项目在 Kubernetes 基础资源之上提供 Sandbox 以及扩展 CRD：SandboxTemplate、SandboxClaim、SandboxWarmPool。Template 定义可复用配置，Claim 请求沙箱，WarmPool 预建可分配环境。[官方文档](https://agent-sandbox.sigs.k8s.io/docs/)

Quickstart 当前使用扩展 API v1beta1。暖池预建 Sandbox 及其 Pod；Claim 可认领已准备好的 Sandbox，控制器再补充池内数量。这个池维护的是待认领的供给，不是售后平台的业务并发上限。[官方 Quickstart](https://agent-sandbox.sigs.k8s.io/docs/use-cases/examples/quickstart/)

隔离必须单独说明：官方 quickstart 默认基础 KIND 示例未配置增强隔离，gVisor 或 Kata Containers 是可选运行时，需要安装并在模板启用对应 runtimeClassName。不能因为资源名叫 Sandbox 就声称已经是独立内核的 VM 安全边界。gVisor 的用户态内核隔离也不能等同于 Kata 的 VM 隔离。[官方 Quickstart 的运行时配置](https://agent-sandbox.sigs.k8s.io/docs/use-cases/examples/quickstart/)

本文设计判断：

- Kubernetes 管 Pod 放置、资源限制和控制器协调；售后平台仍需租户配额、模型 RPM/TPM 配额、任务租约、业务状态和幂等操作账本。
- 现有 Kubernetes 集群不代表已有可运行不可信代码的安全节点池。应核查运行时、网络出站、服务账号、挂载、镜像供应链和节点权限。
- WarmPool 减少环境等待；预热本身不增加机器总 CPU/内存，也不会消除模型或第三方 API 的限流。
- 官方示例中的启动速度是特定实现/场景说明，不可当作本文售后项目实测。压测应记录模板、节点、镜像缓存状态与 p50/p95/p99。

## 3. Harness 放置：架构选择，不是沙箱品牌决定

下面是本文建议采用的概念定义，不是对任何闭源产品内核的断言。

| 放置方式 | 模型循环在哪里 | 沙箱承担什么 | 必须外置的业务控制 |
|---|---|---|---|
| Harness in service | 应用服务/worker 中 | 被授权的代码、浏览器、文件工具执行 | 工单事实、审批、操作账本、预算、恢复状态 |
| Harness in sandbox | 完整 Agent runtime 跑在隔离环境中 | 模型循环及本地工具环境 | 同样需要外部持久状态、资源策略和危险操作网关 |

服务内 Harness 便于本文自定义售后状态机、租约与审批边界；沙箱内 Harness 便于复用依赖本地文件/进程的成熟 runtime。二者并非先进与落后之分，也不意味着前者天然无状态、后者天然不可观测。一个 Agent 可以使用多个沙箱；多个子 Agent 可以在明确的同一信任域内共享环境。这里的关键是租户与任务的数据/凭据边界，而不是 Agent 名称。

## 4. Grok Bot：必须按具体产品与日期讨论

本笔记讨论的是 Grok Bot 产品，不是 Grok 模型 API、X 上的 @grok 账号或另一个代码仓库。

### 可确认的官方事实

详细架构页说明 Grok Bot 在 Cursor 云运行，桌面和移动端主要承担会话、查看与审批；每个用户有一台持久 Firecracker microVM，同一个用户的所有 Bots 共用这台电脑。不同 Bot 的人格/工作区隔离不等于计算隔离，同用户的文件与登录状态应视为其他 Bot 可用。[Grok Bot 企业架构文档](https://docs.x.ai/grok-bot/teams-and-enterprises)

安全文档说明目前仅支持 Cursor 托管电脑，不支持本地机房部署、部署在客户自己的边界内或 BYO image。它也区分电脑内任务与可选本机命令执行，并说明网络、审批、连接器授权是分层控制。不能据其他 Cursor Cloud Agents 文档宣称 Grok Bot 已支持自托。[Grok Bot 安全文档](https://docs.x.ai/grok-bot/security)

### 官方口径需要保留的警告

2026-09-03 的企业发布营销稿出现“每个 Bot 有自己的云电脑”的表述，而详细团队架构文档明确为同用户共享电脑。本文以专门的架构与安全文档解释隔离边界，不用营销句子推导逐 Bot VM 隔离。[企业发布稿](https://x.ai/news/grok-bot-for-enterprise)

公开资料能确认工具工作的执行环境与产品托管边界，但不足以确定其所有规划、模型循环、状态存储组件各自的具体进程位置。因此不要写“Grok Bot 已证实属于纯 Harness in sandbox”，也不要补造其内部 Kafka、租约或恢复算法。

### 与本文售后平台的合理比较

这是设计判断：Grok Bot 的上述设计围绕用户持久云工作环境；本文参考设计围绕租户、工单与受控业务操作，按需要分配可回收的执行环境。区别是默认资源与权限归属单位，不是“个人玩具对企业产品”。Grok Bot 本身已有企业控制，因此不要用“它不支持任何企业审计/治理”这种过时或无根据的对比。
