import { useState } from "react";

import { formatTime } from "../labels";
import {
  GRANT_ADMIN_PERMISSION,
  buildGrantRequest,
  buildRevokeRequest,
  delegablePermissions,
  grantStatus,
  toLocalInput,
  undelegablePermissions,
  type GrantBody,
  type GrantDraft,
  type GrantStatus,
  type RevokeBody,
} from "../grants";
import type { CaseGrant } from "../types";

const STATUS_LABEL: Record<GrantStatus, string> = {
  active: "生效中",
  expired: "已过期",
  revoked: "已撤销",
};

interface Props {
  grants: CaseGrant[];
  actorPermissions: string[];
  busy: boolean;
  pendingSubjects: ReadonlySet<string>;
  onGrant: (body: GrantBody) => Promise<boolean>;
  onRevoke: (subjectId: string, body: RevokeBody) => Promise<boolean>;
}

export function GrantPanel({
  grants,
  actorPermissions,
  busy,
  pendingSubjects,
  onGrant,
  onRevoke,
}: Props) {
  const [subjectId, setSubjectId] = useState("");
  const [permissions, setPermissions] = useState<ReadonlySet<string>>(() => new Set());
  const [expiresAt, setExpiresAt] = useState("");
  const [expectedRevision, setExpectedRevision] = useState<number | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  const canAdminister = actorPermissions.includes(GRANT_ADMIN_PERMISSION);
  const options = delegablePermissions(actorPermissions);
  const withheld = undelegablePermissions(actorPermissions);

  const resetForm = (): void => {
    setSubjectId("");
    setPermissions(new Set());
    setExpiresAt("");
    setExpectedRevision(null);
    setFormError(null);
  };

  const applyDraft = (grant: CaseGrant): void => {
    setSubjectId(grant.subject_id);
    setPermissions(new Set(grant.permissions));
    setExpiresAt(toLocalInput(grant.expires_at));
    // Replacing or re-granting after a revoke needs the revision we just read.
    setExpectedRevision(grant.revision);
    setFormError(null);
  };

  const submit = async (): Promise<void> => {
    const draft: GrantDraft = { subjectId, permissions: [...permissions], expiresAt, expectedRevision };
    const result = buildGrantRequest(draft);
    if (!result.ok) {
      setFormError(result.message);
      return;
    }
    setFormError(null);
    if (await onGrant(result.body)) {
      resetForm();
    }
  };

  const revoke = async (grant: CaseGrant): Promise<void> => {
    const result = buildRevokeRequest(grant.revision);
    if (!result.ok) {
      setFormError(result.message);
      return;
    }
    setFormError(null);
    await onRevoke(grant.subject_id, result.body);
  };

  return (
    <div className="panel">
      <div className="panel-head">
        <h3>访问管理</h3>
        <span className="count">{grants.length}</span>
      </div>
      <p className="gate-meta">
        列表包含已撤销与已过期的历史行；每次授予、替换和撤销都由服务端在同一个事务里写入事件流。
      </p>
      {grants.length === 0 ? (
        <p className="empty">该工单尚无额外授权。</p>
      ) : (
        <ul className="gate-list">
          {grants.map((grant) => {
            const status = grantStatus(grant, new Date());
            return (
              <li key={grant.subject_id}>
                <div className="gate-head">
                  <span className="mono">{grant.subject_id}</span>
                  <span className={`badge grant-${status}`}>{STATUS_LABEL[status]}</span>
                </div>
                <p className="gate-meta">
                  {grant.permissions.map((permission) => (
                    <span key={permission} className="badge scope">
                      {permission}
                    </span>
                  ))}
                </p>
                <p className="gate-meta">
                  r{grant.revision} · 由 {grant.granted_by} 于 {formatTime(grant.granted_at)}
                  {grant.expires_at === null
                    ? ""
                    : ` · 有效期至 ${formatTime(grant.expires_at)}`}
                  {grant.revoked_at === null
                    ? ""
                    : ` · 由 ${grant.revoked_by ?? "—"} 于 ${formatTime(grant.revoked_at)} 撤销`}
                </p>
                {canAdminister ? (
                  <div className="decision-actions">
                    <button type="button" disabled={busy} onClick={() => { applyDraft(grant); }}>
                      替换 / 重新授权
                    </button>
                    <button
                      type="button"
                      className="negative"
                      disabled={busy || status === "revoked" || pendingSubjects.has(grant.subject_id)}
                      onClick={() => {
                        void revoke(grant);
                      }}
                    >
                      {pendingSubjects.has(grant.subject_id) ? "撤销中…" : "撤销"}
                    </button>
                  </div>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}

      {canAdminister ? (
        <div className="grant-form">
          <label>
            <span>被授权主体</span>
            <input
              placeholder="例如 operator-7"
              value={subjectId}
              maxLength={160}
              onChange={(event) => {
                setSubjectId(event.target.value);
              }}
            />
          </label>
          <fieldset className="grant-perms">
            <legend>授予权限</legend>
            {options.map((permission) => (
              <label key={permission} className="permission-choice">
                <input
                  type="checkbox"
                  checked={permissions.has(permission)}
                  onChange={(event) => {
                    setPermissions((current) => {
                      const next = new Set(current);
                      if (event.target.checked) {
                        next.add(permission);
                      } else {
                        next.delete(permission);
                      }
                      return next;
                    });
                  }}
                />
                <span className="mono">{permission}</span>
              </label>
            ))}
          </fieldset>
          {withheld.length === 0 ? null : (
            <p className="grant-hint">
              当前身份没有 {withheld.join("、")}，无法授出这些权限。
            </p>
          )}
          <label>
            <span>有效期（可留空表示不过期）</span>
            <input
              type="datetime-local"
              value={expiresAt}
              onChange={(event) => {
                setExpiresAt(event.target.value);
              }}
            />
          </label>
          <p className="grant-hint">
            {expectedRevision === null
              ? "首次授权不需要 revision；若该主体已有授权行，请先点“替换 / 重新授权”。"
              : `将替换当前 r${String(expectedRevision)}（重放会返回 409）。`}
          </p>
          {formError === null ? null : <p className="grant-error">{formError}</p>}
          <div className="decision-actions">
            <button
              type="button"
              className="primary"
              disabled={busy}
              onClick={() => {
                void submit();
              }}
            >
              {expectedRevision === null ? "创建授权" : "提交替换"}
            </button>
            <button type="button" disabled={busy} onClick={resetForm}>
              清空
            </button>
          </div>
        </div>
      ) : (
        <p className="empty">当前身份缺少 grant:admin 权限，只能查看。</p>
      )}
    </div>
  );
}
