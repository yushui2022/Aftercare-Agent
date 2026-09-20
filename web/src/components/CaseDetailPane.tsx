import { useState } from "react";

import type { GrantBody, RevokeBody } from "../grants";
import { APPROVAL_STATE_LABEL, CASE_STATUS_LABEL, RUN_STATE_LABEL, formatTime } from "../labels";
import type { StreamStatus } from "../sse";
import type {
  Approval,
  CaseDetail,
  CaseEvent,
  CaseGrant,
  CaseSummary,
  Review,
  ReviewDecision,
} from "../types";
import { EventTimeline } from "./EventTimeline";
import { GrantPanel } from "./GrantPanel";

const STREAM_LABEL: Record<StreamStatus, string> = {
  connecting: "连接中",
  live: "实时",
  reconnecting: "重连中",
  stopped: "已暂停",
};

interface DecisionOption {
  value: string;
  label: string;
  tone: "positive" | "negative";
}

function DecisionForm({
  options,
  disabled,
  onSubmit,
}: {
  options: ReadonlyArray<DecisionOption>;
  disabled: boolean;
  onSubmit: (value: string, reason: string) => void;
}) {
  const [reason, setReason] = useState("");
  return (
    <div className="decision-form">
      <input
        placeholder="决定理由（可选）"
        value={reason}
        maxLength={2000}
        onChange={(event) => {
          setReason(event.target.value);
        }}
      />
      <div className="decision-actions">
        {options.map((option) => (
          <button
            key={option.value}
            type="button"
            className={option.tone}
            disabled={disabled}
            onClick={() => {
              onSubmit(option.value, reason.trim());
            }}
          >
            {option.label}
          </button>
        ))}
      </div>
    </div>
  );
}

function CaseHeader({ summary }: { summary: CaseSummary }) {
  return (
    <header className="detail-head">
      <div>
        <h2>{summary.order_id}</h2>
        <p className="detail-sub">
          {summary.case_id} · v{summary.version} · {formatTime(summary.created_at)}
        </p>
      </div>
      <div className="detail-badges">
        <span className={`badge status-${summary.status.toLowerCase()}`}>
          {CASE_STATUS_LABEL[summary.status]}
        </span>
        {summary.permissions.map((permission) => (
          <span key={permission} className="badge scope">
            {permission}
          </span>
        ))}
      </div>
    </header>
  );
}

interface Props {
  /** Header fields, present even when this row may not be opened. */
  summary: CaseSummary;
  /** null when the row is administrable only, so no content route was called. */
  detail: CaseDetail | null;
  reviews: Review[];
  approvals: Approval[];
  events: CaseEvent[];
  streamStatus: StreamStatus;
  live: boolean;
  busy: boolean;
  pendingDecisionIds: ReadonlySet<string>;
  /** null when the identity may not read grants, so no request is sent. */
  grants: CaseGrant[] | null;
  /** Closed-set permissions the server says this identity may hand out. */
  delegable: string[];
  canAdminister: boolean;
  pendingGrantSubjects: ReadonlySet<string>;
  onGrant: (body: GrantBody) => Promise<boolean>;
  onRevoke: (subjectId: string, body: RevokeBody) => Promise<boolean>;
  onToggleLive: () => void;
  onReviewDecision: (reviewId: string, decision: ReviewDecision, reason: string) => void;
  onApprovalDecision: (
    approvalId: string,
    decision: "APPROVED" | "REJECTED",
    reason: string,
  ) => void;
}

export function CaseDetailPane({
  summary,
  detail,
  reviews,
  approvals,
  events,
  streamStatus,
  live,
  busy,
  pendingDecisionIds,
  grants,
  delegable,
  canAdminister,
  pendingGrantSubjects,
  onGrant,
  onRevoke,
  onToggleLive,
  onReviewDecision,
  onApprovalDecision,
}: Props) {
  const header = detail ?? summary;
  const canDecideReview = detail !== null && detail.permissions.includes("review:decide");
  const canDecideApproval = detail !== null && detail.permissions.includes("approval:decide");

  if (detail === null) {
    return (
      <section className="detail">
        <CaseHeader summary={header} />
        <div className="panel">
          <h3>访问管理</h3>
          <p className="empty">
            当前身份对该工单只有访问管理权：可以查看并移交访问授权，但工单内容（Run、审核、审批与事件）仍由逐工单授权决定，因此在这里不可见。若需要处理内容，请让管理员为你的主体授予该工单的对应权限。
          </p>
        </div>
        {grants === null ? null : (
          <GrantPanel
            grants={grants}
            delegable={delegable}
            canAdminister={canAdminister}
            busy={busy}
            pendingSubjects={pendingGrantSubjects}
            onGrant={onGrant}
            onRevoke={onRevoke}
          />
        )}
      </section>
    );
  }

  return (
    <section className="detail">
      <CaseHeader summary={header} />

      <div className="panel">
        <h3>执行 Run</h3>
        {detail.runs.length === 0 ? (
          <p className="empty">该工单暂无 Run。</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Run</th>
                <th>状态</th>
                <th>输入版本</th>
                <th>路由原因</th>
                <th>等待</th>
                <th>租约到期</th>
              </tr>
            </thead>
            <tbody>
              {detail.runs.map((run) => (
                <tr key={run.run_id}>
                  <td className="mono">{run.run_id}</td>
                  <td>
                    <span className={`badge run-${run.state.toLowerCase()}`}>
                      {RUN_STATE_LABEL[run.state]}
                    </span>
                  </td>
                  <td>{run.input_version}</td>
                  <td className="mono">{run.route_reason ?? "—"}</td>
                  <td className="mono">
                    {run.wait_id === null
                      ? "—"
                      : `${run.wait_id}#${String(run.wait_generation)}`}
                  </td>
                  <td>{formatTime(run.lease_until)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="panel">
        <h3>人工审核 Review</h3>
        {reviews.length === 0 ? (
          <p className="empty">暂无人工审核请求。</p>
        ) : (
          <ul className="gate-list">
            {reviews.map((review) => (
              <li key={review.review_id}>
                <div className="gate-head">
                  <span className="mono">{review.review_id}</span>
                  <span className={review.decision === null ? "badge pending" : "badge done"}>
                    {review.decision === null ? "待决定" : review.decision}
                  </span>
                </div>
                <p className="gate-meta">
                  原因 {review.reason_code} · Run {review.run_id} · {formatTime(review.created_at)}
                </p>
                {review.decision === null ? (
                  canDecideReview ? (
                    <DecisionForm
                      disabled={busy || pendingDecisionIds.has(review.review_id)}
                      options={[
                        { value: "CONTINUE", label: "继续执行", tone: "positive" },
                        { value: "CANCEL", label: "取消", tone: "negative" },
                      ]}
                      onSubmit={(value, reason) => {
                        onReviewDecision(review.review_id, value as ReviewDecision, reason);
                      }}
                    />
                  ) : (
                    <p className="empty">当前身份缺少 review:decide 权限。</p>
                  )
                ) : (
                  <p className="gate-meta">
                    由 {review.actor ?? "—"} 于 {formatTime(review.decided_at)} 决定
                    {review.decision_reason === null ? "" : ` · ${review.decision_reason}`}
                  </p>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="panel">
        <h3>审批 Approval</h3>
        {approvals.length === 0 ? (
          <p className="empty">暂无审批请求。</p>
        ) : (
          <ul className="gate-list">
            {approvals.map((approval) => (
              <li key={approval.approval_id}>
                <div className="gate-head">
                  <span className="mono">{approval.approval_id}</span>
                  <span
                    className={
                      approval.decision === "PENDING" ? "badge pending" : "badge done"
                    }
                  >
                    {APPROVAL_STATE_LABEL[approval.decision]}
                  </span>
                </div>
                <p className="gate-meta">
                  动作 {approval.action_id}
                  {approval.run_id === null ? "" : ` · Run ${approval.run_id}`} · 有效期至{" "}
                  {formatTime(approval.expires_at)}
                </p>
                {approval.decision === "PENDING" ? (
                  canDecideApproval ? (
                    <DecisionForm
                      disabled={busy || pendingDecisionIds.has(approval.approval_id)}
                      options={[
                        { value: "APPROVED", label: "批准", tone: "positive" },
                        { value: "REJECTED", label: "拒绝", tone: "negative" },
                      ]}
                      onSubmit={(value, reason) => {
                        onApprovalDecision(
                          approval.approval_id,
                          value as "APPROVED" | "REJECTED",
                          reason,
                        );
                      }}
                    />
                  ) : (
                    <p className="empty">当前身份缺少 approval:decide 权限。</p>
                  )
                ) : (
                  <p className="gate-meta">
                    由 {approval.actor ?? "—"} 于 {formatTime(approval.decided_at)} 决定
                    {approval.decision_reason === null ? "" : ` · ${approval.decision_reason}`}
                  </p>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>

      {grants === null ? null : (
        <GrantPanel
          grants={grants}
          delegable={delegable}
          canAdminister={canAdminister}
          busy={busy}
          pendingSubjects={pendingGrantSubjects}
          onGrant={onGrant}
          onRevoke={onRevoke}
        />
      )}

      <div className="panel">
        <div className="panel-head">
          <h3>事件时间线</h3>
          <div className="stream-controls">
            <span className={`badge stream-${streamStatus}`}>{STREAM_LABEL[streamStatus]}</span>
            <button type="button" onClick={onToggleLive}>
              {live ? "暂停实时" : "开启实时"}
            </button>
          </div>
        </div>
        <EventTimeline events={events} />
      </div>
    </section>
  );
}
