import { useCallback, useEffect, useState } from "react";

import { ApiError, api, type Identity } from "./api";
import { CaseDetailPane } from "./components/CaseDetailPane";
import { CaseList } from "./components/CaseList";
import { IdentityBar } from "./components/IdentityBar";
import { appendEvent, subscribeCaseEvents, type StreamStatus } from "./sse";
import type {
  Approval,
  CaseDetail,
  CaseEvent,
  CaseStatus,
  CaseSummary,
  Review,
  ReviewDecision,
} from "./types";

const DEFAULT_IDENTITY: Identity = { tenantId: "synthetic-demo", subjectId: "ops-1" };

interface Workspace {
  detail: CaseDetail;
  reviews: Review[];
  approvals: Approval[];
}

function describe(cause: unknown): string {
  if (cause instanceof ApiError) {
    return cause.message;
  }
  return cause instanceof Error ? cause.message : String(cause);
}

export default function App() {
  const [identity, setIdentity] = useState<Identity>(DEFAULT_IDENTITY);
  const [statusFilter, setStatusFilter] = useState<CaseStatus | "ALL">("ALL");
  const [cases, setCases] = useState<CaseSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [events, setEvents] = useState<CaseEvent[]>([]);
  const [live, setLive] = useState(true);
  const [streamStatus, setStreamStatus] = useState<StreamStatus>("stopped");
  const [error, setError] = useState<string | null>(null);
  const [listBusy, setListBusy] = useState(false);
  const [detailBusy, setDetailBusy] = useState(false);
  // Bumped by the refresh button and after every decision so the case list and
  // its gates re-read from the server instead of trusting local state.
  const [refreshToken, setRefreshToken] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setListBusy(true);
    setError(null);
    api
      .listCases(identity, statusFilter === "ALL" ? {} : { status: statusFilter })
      .then((response) => {
        if (cancelled) {
          return;
        }
        setCases(response.cases);
        setSelectedId((current) =>
          current !== null && response.cases.some((item) => item.case_id === current)
            ? current
            : (response.cases[0]?.case_id ?? null),
        );
      })
      .catch((cause: unknown) => {
        if (cancelled) {
          return;
        }
        setCases([]);
        setSelectedId(null);
        setError(describe(cause));
      })
      .finally(() => {
        if (!cancelled) {
          setListBusy(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [identity, statusFilter, refreshToken]);

  useEffect(() => {
    if (selectedId === null) {
      setWorkspace(null);
      return;
    }
    let cancelled = false;
    setDetailBusy(true);
    Promise.all([
      api.getCase(identity, selectedId),
      api.listReviews(identity, selectedId),
      api.listApprovals(identity, selectedId),
    ])
      .then(([detail, reviews, approvals]) => {
        if (cancelled) {
          return;
        }
        setWorkspace({ detail, reviews: reviews.reviews, approvals: approvals.approvals });
        setError(null);
      })
      .catch((cause: unknown) => {
        if (cancelled) {
          return;
        }
        setWorkspace(null);
        setError(describe(cause));
      })
      .finally(() => {
        if (!cancelled) {
          setDetailBusy(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [identity, selectedId, refreshToken]);

  // Events are owned by the subscription, not by the case fetch, so a decision
  // refresh never truncates the running timeline.
  useEffect(() => {
    setEvents([]);
    if (selectedId === null) {
      setStreamStatus("stopped");
      return;
    }
    if (!live) {
      let cancelled = false;
      setStreamStatus("stopped");
      api
        .listEvents(identity, selectedId)
        .then((replay) => {
          if (!cancelled) {
            setEvents(replay);
          }
        })
        .catch((cause: unknown) => {
          if (!cancelled) {
            setError(describe(cause));
          }
        });
      return () => {
        cancelled = true;
      };
    }
    return subscribeCaseEvents(identity, selectedId, {
      onEvents: (incoming) => {
        setEvents((current) => incoming.reduce(appendEvent, current));
      },
      onStatus: (status, detail) => {
        setStreamStatus(status);
        if (status === "stopped" && detail !== undefined) {
          setError(detail);
        }
      },
    });
  }, [identity, selectedId, live]);

  const reload = useCallback(() => {
    setRefreshToken((token) => token + 1);
  }, []);

  const handleReviewDecision = useCallback(
    async (reviewId: string, decision: ReviewDecision, reason: string) => {
      if (selectedId === null) {
        return;
      }
      setError(null);
      try {
        await api.decideReview(identity, selectedId, reviewId, decision, reason);
      } catch (cause: unknown) {
        setError(describe(cause));
      }
      reload();
    },
    [identity, selectedId, reload],
  );

  const handleApprovalDecision = useCallback(
    async (approvalId: string, decision: "APPROVED" | "REJECTED", reason: string) => {
      if (selectedId === null) {
        return;
      }
      setError(null);
      try {
        await api.decideApproval(identity, selectedId, approvalId, decision, reason);
      } catch (cause: unknown) {
        setError(describe(cause));
      }
      reload();
    },
    [identity, selectedId, reload],
  );

  const busy = listBusy || detailBusy;

  return (
    <div className="app">
      <IdentityBar
        identity={identity}
        busy={busy}
        onChange={setIdentity}
        onRefresh={reload}
      />
      {error === null ? null : (
        <div className="error-banner">
          <span>{error}</span>
          <button type="button" onClick={reload}>
            重试
          </button>
        </div>
      )}
      <main className="layout">
        <CaseList
          cases={cases}
          selectedId={selectedId}
          statusFilter={statusFilter}
          onSelect={setSelectedId}
          onStatusFilter={setStatusFilter}
        />
        {workspace === null ? (
          <section className="detail placeholder">
            <p className="empty">选择左侧工单以查看 Run、审核、审批与事件。</p>
          </section>
        ) : (
          <CaseDetailPane
            detail={workspace.detail}
            reviews={workspace.reviews}
            approvals={workspace.approvals}
            events={events}
            streamStatus={streamStatus}
            live={live}
            busy={busy}
            onToggleLive={() => {
              setLive((current) => !current);
            }}
            onReviewDecision={(reviewId, decision, reason) => {
              void handleReviewDecision(reviewId, decision, reason);
            }}
            onApprovalDecision={(approvalId, decision, reason) => {
              void handleApprovalDecision(approvalId, decision, reason);
            }}
          />
        )}
      </main>
    </div>
  );
}
