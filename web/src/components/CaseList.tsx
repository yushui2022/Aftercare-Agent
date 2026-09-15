import { CASE_STATUS_LABEL, formatTime } from "../labels";
import type { WorkQueueRow } from "../cases";
import type { CaseStatus } from "../types";

/** One paginated list: its rows plus its own keyset cursor. */
export interface CaseBlock {
  rows: WorkQueueRow[];
  hasMore: boolean;
  loadingMore: boolean;
  onLoadMore: () => void;
}

interface Props {
  queue: CaseBlock;
  /** Inventory rows the queue cannot see; empty for a non-administrator. */
  administration: CaseBlock;
  selectedId: string | null;
  statusFilter: CaseStatus | "ALL";
  onSelect: (row: WorkQueueRow) => void;
  onStatusFilter: (status: CaseStatus | "ALL") => void;
}

const FILTERS: Array<{ value: CaseStatus | "ALL"; label: string }> = [
  { value: "ALL", label: "全部" },
  { value: "OPEN", label: "进行中" },
  { value: "IN_REVIEW", label: "人审中" },
  { value: "CLOSED", label: "已关闭" },
];

function CaseRow({
  row,
  selected,
  onSelect,
}: {
  row: WorkQueueRow;
  selected: boolean;
  onSelect: (row: WorkQueueRow) => void;
}) {
  const { summary } = row;
  const classes = ["case-row"];
  if (selected) {
    classes.push("selected");
  }
  if (row.administrationOnly) {
    classes.push("admin-only");
  }
  return (
    <li>
      <button
        type="button"
        className={classes.join(" ")}
        onClick={() => {
          onSelect(row);
        }}
      >
        <span className="case-row-top">
          <strong>{summary.order_id}</strong>
          <span className={`badge status-${summary.status.toLowerCase()}`}>
            {CASE_STATUS_LABEL[summary.status]}
          </span>
        </span>
        <span className="case-row-meta">
          {summary.case_id} · v{summary.version}
          {row.administrationOnly ? " · 仅可管理访问" : ""}
        </span>
        <span className="case-row-meta">{formatTime(summary.created_at)}</span>
      </button>
    </li>
  );
}

function CaseRows({
  block,
  emptyText,
  selectedId,
  onSelect,
}: {
  block: CaseBlock;
  emptyText: string;
  selectedId: string | null;
  onSelect: (row: WorkQueueRow) => void;
}) {
  return (
    <>
      {block.rows.length === 0 ? (
        <p className="empty">{emptyText}</p>
      ) : (
        <ul>
          {block.rows.map((row) => (
            <CaseRow
              key={row.summary.case_id}
              row={row}
              selected={row.summary.case_id === selectedId}
              onSelect={onSelect}
            />
          ))}
        </ul>
      )}
      {block.hasMore ? (
        <button
          type="button"
          className="load-more"
          disabled={block.loadingMore}
          onClick={block.onLoadMore}
        >
          {block.loadingMore ? "加载中…" : "加载更多"}
        </button>
      ) : null}
    </>
  );
}

export function CaseList({
  queue,
  administration,
  selectedId,
  statusFilter,
  onSelect,
  onStatusFilter,
}: Props) {
  const showAdministration = administration.rows.length > 0 || administration.hasMore;
  return (
    <aside className="case-list">
      <div className="case-list-head">
        <h2>工单队列</h2>
        <span className="count">{queue.rows.length}</span>
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
      <CaseRows
        block={queue}
        emptyText="该租户当前没有可见工单。"
        selectedId={selectedId}
        onSelect={onSelect}
      />
      {showAdministration ? (
        <section className="administration-block">
          <div className="case-list-head">
            <h2>本租户其他工单</h2>
            <span className="count">{administration.rows.length}</span>
          </div>
          <p className="hint">
            这些工单你没有内容访问权，只能查看访问授权并移交给其他主体。
          </p>
          <CaseRows
            block={administration}
            emptyText="已加载的工单都在队列里；继续加载可查看更早的工单。"
            selectedId={selectedId}
            onSelect={onSelect}
          />
        </section>
      ) : null}
    </aside>
  );
}
