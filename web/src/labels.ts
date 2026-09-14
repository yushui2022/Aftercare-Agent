import type { ApprovalState, CaseStatus, RunState } from "./types";

export const CASE_STATUS_LABEL: Record<CaseStatus, string> = {
  OPEN: "进行中",
  IN_REVIEW: "人审中",
  CLOSED: "已关闭",
};

export const RUN_STATE_LABEL: Record<RunState, string> = {
  READY: "待执行",
  RUNNING: "执行中",
  WAITING_INPUT: "等待买家",
  WAITING_APPROVAL: "等待审批",
  RETRY_AT: "等待重试",
  REVIEW: "等待人审",
  COMPLETED: "已完成",
  CANCELLED: "已取消",
};

export const APPROVAL_STATE_LABEL: Record<ApprovalState, string> = {
  PENDING: "待审批",
  APPROVED: "已批准",
  REJECTED: "已拒绝",
  EXPIRED: "已过期",
  CANCELLED: "已取消",
};

export function formatTime(value: string | null): string {
  if (value === null) {
    return "—";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString("zh-CN", { hour12: false });
}
