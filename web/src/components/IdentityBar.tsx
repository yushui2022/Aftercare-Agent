import type { Identity } from "../api";

interface Props {
  identity: Identity;
  busy: boolean;
  onChange: (identity: Identity) => void;
  onRefresh: () => void;
}

export function IdentityBar({ identity, busy, onChange, onRefresh }: Props) {
  return (
    <header className="identity-bar">
      <div className="brand">
        <span className="brand-mark">AC</span>
        <div>
          <h1>Aftercare 运营工作台</h1>
          <p>本地合成身份 · 仅用于开发与联调，非生产认证</p>
        </div>
      </div>
      <div className="identity-fields">
        <label>
          <span>租户</span>
          <input
            value={identity.tenantId}
            onChange={(event) => {
              onChange({ ...identity, tenantId: event.target.value });
            }}
          />
        </label>
        <label>
          <span>操作员</span>
          <input
            value={identity.subjectId}
            onChange={(event) => {
              onChange({ ...identity, subjectId: event.target.value });
            }}
          />
        </label>
        <button type="button" className="primary" disabled={busy} onClick={onRefresh}>
          {busy ? "加载中…" : "刷新"}
        </button>
      </div>
    </header>
  );
}
