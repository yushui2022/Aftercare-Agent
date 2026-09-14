import { CASE_STATUS_LABEL, formatTime } from "../labels";
import type { CaseStatus, CaseSummary } from "../types";

interface Props {
  cases: CaseSummary[];
  selectedId: string | null;
  statusFilter: CaseStatus | "ALL";
  onSelect: (caseId: string) => void;
  onStatusFilter: (status: CaseStatus | "ALL") => void;
  hasMore: boolean;
  loadingMore: boolean;
  onLoadMore: () => void;
}

const FILTERS: Array<{ value: CaseStatus | "ALL"; label: string }> = [
  { value: "ALL", label: "全部" },
  { value: "OPEN", label: "进行中" },
  { value: "IN_REVIEW", label: "人审中" },
  { value: "CLOSED", label: "已关闭" },
];

export function CaseList({
  cases,
  selectedId,
  statusFilter,
  onSelect,
  onStatusFilter,
  hasMore,
  loadingMore,
  onLoadMore,
}: Props) {
  return (
    <aside className="case-list">
      <div className="case-list-head">
        <h2>工单队列</h2>
        <span className="count">{cases.length}</span>
      </div>
      <div className="filters">
        {FILTERS.map((filter) => (
          <button
            key={filter.value}
            type="button"
            className={statusFilter === filter.value ? "chip active" : "chip"}
            onClick={() => {
              onStatusFilter(filter.value);
            }}
          >
            {filter.label}
          </button>
        ))}
      </div>
      {cases.length === 0 ? (
        <p className="empty">该租户当前没有可见工单。</p>
      ) : (
        <ul>
          {cases.map((item) => (
            <li key={item.case_id}>
              <button
                type="button"
                className={item.case_id === selectedId ? "case-row selected" : "case-row"}
                onClick={() => {
                  onSelect(item.case_id);
                }}
              >
                <span className="case-row-top">
                  <strong>{item.order_id}</strong>
                  <span className={`badge status-${item.status.toLowerCase()}`}>
                    {CASE_STATUS_LABEL[item.status]}
                  </span>
                </span>
                <span className="case-row-meta">
                  {item.case_id} · v{item.version}
                </span>
                <span className="case-row-meta">{formatTime(item.created_at)}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
      {hasMore ? (
        <button type="button" className="load-more" disabled={loadingMore} onClick={onLoadMore}>
          {loadingMore ? "加载中…" : "加载更多"}
        </button>
      ) : null}
    </aside>
  );
}
