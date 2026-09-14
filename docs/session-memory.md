# Session transcript references

Aftercare 的 `SessionRecord` 描述一次持久会话，但不会把模型供应商的会话 ID
当作我们的恢复状态。`aftercare_session_messages` 现在保存每个会话的不可变消息
引用：`tenant_id/case_id/session_id`、单调 `message_seq`、角色、外部
`message_ref` 和 SHA-256 摘要。原文仍由经过授权的 artifact/object store 管理，
不进入运行时数据库。

`SessionMessageRepository.append()` 会在会话行上取得短行锁，分配连续序号；同一
`message_id` 的完全相同重投返回原记录，改内容或跳号会失败。`list_for_session()`
按会话和工单过滤，并支持 `after_seq` 游标，避免把一个租户或会话的 transcript
带入另一个 Case。它是恢复和审计的持久边界，不是长期记忆：EGM 的长期记忆仍
默认不注入模型上下文，必须由可信 Worker 按 Case 明确选择。

下一步的 Responses transcript adapter 应先读取当前 session 的消息引用，再从
授权 artifact store 解析内容，并把模型输出以 `assistant`/`tool` 消息追加；不能
接受模型自行提交 tenant、case、session 或消息序号。消息原文的留存、加密、删除
和跨地域策略属于部署阶段的对象存储审查。
