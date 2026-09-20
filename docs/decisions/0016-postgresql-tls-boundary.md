# ADR-0016：部署 profile 的 PostgreSQL TLS 边界

状态：已接受，2026-09-20

## 背景

Aftercare 的 deployment profile 把 PostgreSQL 连接串放在 migration/runtime 的独立
Secret 文件中。仅检查 Secret 文件存在，无法证明连接会验证服务端身份；`sslmode`
缺失时 libpq 默认可能协商出不适合生产的连接方式。local 和 integration profile
使用临时数据库，不应为了模拟生产而改变开发路径。

## 决策

deployment profile 要求两个 DSN 都设置 `sslmode=verify-full`，并设置
`AFTERCARE_REQUIRE_DATABASE_TLS=1`。部署预检读取 DSN 的非敏感连接参数，只在机器证据
中记录 `sslmode`，不输出 DSN；应用的统一 Secret 读取器在 migration、RLS check、API、
Worker、备份和容量入口读取 `DATABASE_URL` 时再次 fail closed。Kubernetes 清单固定该
环境变量为 `1`，Compose deployment 从外部 env 文件传入它。

`verify-full` 同时要求可信 CA 和主机名匹配；目标环境仍负责分发 CA、选择连接代理参数、
轮换 Secret，并在真实网络中验收。local/integration 继续保留现有开发 DSN 和测试行为。

## 不在本 ADR 中决定的事项

- 云厂商、连接代理、CA 发布系统和网络拓扑；
- 公网入口 TLS、Ingress 或 API 网关策略；
- RPO/RTO、连接池容量和 PostgreSQL 参数调优。

## 验收与回滚

预检、Compose/Kubernetes 配置检查和进程启动测试必须证明弱 TLS、缺失变量和非法 DSN
都会失败。目标环境还需用真实 Secret 和 CA 完成 migration/RLS check、readiness 与
轮换验收。若目标平台暂时无法提供主机名校验，应停留在 local/integration 或 hold，
不能把 `sslmode=require` 当作 deployment 通过条件；回滚使用仍满足该边界的旧镜像和
旧 Secret，不通过关闭 TLS 门禁恢复服务。
