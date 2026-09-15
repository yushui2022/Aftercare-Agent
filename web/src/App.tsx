import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, api, type Identity } from "./api";
import { CaseDetailPane } from "./components/CaseDetailPane";
import { CaseList } from "./components/CaseList";
import { IdentityBar } from "./components/IdentityBar";
import type { GrantBody, RevokeBody } from "./grants";
import { appendEvents, subscribeCaseEvents, type StreamStatus } from "./sse";
import type {
  Approval,
  CaseDetail,
  CaseEvent,
  CaseGrant,
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
  /** null when this identity may not read access rows. */
  grants: CaseGrant[] | null;
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
  const [listMoreBusy, setListMoreBusy] = useState(false);
  const [nextCursor, setNextCursor] = useState<{
    createdAt: string;
    caseId: string;
  } | null>(null);
  const [detailBusy, setDetailBusy] = useState(false);
  const [pendingDecisionIds, setPendingDecisionIds] = useState<Set<string>>(
    () => new Set(),
  );
  const [pendingGrantSubjects, setPendingGrantSubjects] = useState<Set<string>>(
    () => new Set(),
  );
  const listGeneration = useRef(0);
  // Keep one idempotency key per decision payload so a retry after a dropped
  // response replays the same server operation instead of creating a second one.
  const decisionKeys = useRef(new Map<string, string>());
  // Bumped by the refresh button and after every decision so the case list and
  // its gates re-read from the server instead of trusting local state.
  const [refreshToken, setRefreshToken] = useState(0);

  useEffect(() => {
    let cancelled = false;
    const generation = listGeneration.current + 1;
    listGeneration.current = generation;
    setNextCursor(null);
    setCases([]);
    setListMoreBusy(false);
    setListBusy(true);
    setError(null);
    api
      .listCases(identity, statusFilter === "ALL" ? {} : { status: statusFilter })
      .then((response) => {
        if (cancelled) {
          return;
        }
        setCases(response.cases);
        setNextCursor(
          response.next_created_at !== null && response.next_case_id !== null
            ? { createdAt: response.next_created_at, caseId: response.next_case_id }
            : null,
        );
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

  const loadMoreCases = useCallback(async () => {
    if (nextCursor === null || listMoreBusy || listBusy) {
      return;
    }
    setListMoreBusy(true);
    setError(null);
    const generation = listGeneration.current;
    try {
      const response = await api.listCases(identity, {
        ...(statusFilter === "ALL" ? {} : { status: statusFilter }),
        afterCreatedAt: nextCursor.createdAt,
        afterCaseId: nextCursor.caseId,
      });
      if (generation !== listGeneration.current) {
        return;
      }
      setCases((current) => {
        const known = new Set(current.map((item) => item.case_id));
        return [...current, ...response.cases.filter((item) => !known.has(item.case_id))];
      });
      setNextCursor(
        response.next_created_at !== null && response.next_case_id !== null
          ? { createdAt: response.next_created_at, caseId: response.next_case_id }
          : null,
      );
    } catch (cause: unknown) {
      if (generation === listGeneration.current) {
        setError(describe(cause));
      }
    } finally {
      if (generation === listGeneration.current) {
        setListMoreBusy(false);
      }
    }
  }, [identity, listBusy, listMoreBusy, nextCursor, statusFilter]);

  useEffect(() => {
    if (selectedId === null) {
      setWorkspace(null);
      return;
    }
    let cancelled = false;
    setDetailBusy(true);
    void (async () => {
      try {
        const [detail, reviews, approvals] = await Promise.all([
          api.getCase(identity, selectedId),
          api.listReviews(identity, selectedId),
          api.listApprovals(identity, selectedId),
        ]);
        // Access rows are a tenant-level view.  The Case projection cannot
        // answer whether this identity may read them - a real bearer identity
        // sees only the intersection with its grant, which never contains
        // `grant:read` - so the server decides and a refusal just hides the
        // panel instead of showing an error the operator cannot act on.
        const grants = await api.listCaseGrants(identity, selectedId).then(
          (response) => response.grants,
          (cause: unknown) => {
            if (cause instanceof ApiError && cause.isFatal) {
              return null;
            }
            throw cause;
          },
        );
        if (cancelled) {
          return;
        }
        setWorkspace({
          detail,
          reviews: reviews.reviews,
          approvals: approvals.approvals,
          grants,
        });
        setError(null);
      } catch (cause: unknown) {
        if (cancelled) {
          return;
        }
        setWorkspace(null);
        setError(describe(cause));
      } finally {
        if (!cancelled) {
          setDetailBusy(false);
        }
      }
    })();
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
        setEvents((current) => appendEvents(current, incoming));
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
      const payloadKey = `review:${identity.tenantId}:${identity.subjectId}:${selectedId}:${reviewId}:${decision}:${reason.trim()}`;
      const idempotencyKey = decisionKeys.current.get(payloadKey) ?? crypto.randomUUID();
      decisionKeys.current.set(payloadKey, idempotencyKey);
      setPendingDecisionIds((current) => new Set(current).add(reviewId));
      try {
        await api.decideReview(identity, selectedId, reviewId, decision, reason, idempotencyKey);
        decisionKeys.current.delete(payloadKey);
      } catch (cause: unknown) {
        setError(describe(cause));
      } finally {
        setPendingDecisionIds((current) => {
          const next = new Set(current);
          next.delete(reviewId);
          return next;
        });
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
      const payloadKey = `approval:${identity.tenantId}:${identity.subjectId}:${selectedId}:${approvalId}:${decision}:${reason.trim()}`;
      const idempotencyKey = decisionKeys.current.get(payloadKey) ?? crypto.randomUUID();
      decisionKeys.current.set(payloadKey, idempotencyKey);
      setPendingDecisionIds((current) => new Set(current).add(approvalId));
      try {
        await api.decideApproval(identity, selectedId, approvalId, decision, reason, idempotencyKey);
        decisionKeys.current.delete(payloadKey);
      } catch (cause: unknown) {
        setError(describe(cause));
      } finally {
        setPendingDecisionIds((current) => {
          const next = new Set(current);
          next.delete(approvalId);
          return next;
        });
      }
      reload();
    },
    [identity, selectedId, reload],
  );

  const busy = listBusy || listMoreBusy || detailBusy;

  const handleGrant = useCallback(
    async (body: GrantBody): Promise<boolean> => {
      if (selectedId === null) {
        return false;
      }
      setError(null);
      try {
        await api.grantCaseAccess(identity, selectedId, body);
      } catch (cause: unknown) {
        setError(describe(cause));
        return false;
      }
      reload();
      return true;
    },
    [identity, selectedId, reload],
  );

  const handleRevoke = useCallback(
    async (subjectId: string, body: RevokeBody): Promise<boolean> => {
      if (selectedId === null) {
        return false;
      }
      setError(null);
      setPendingGrantSubjects((current) => new Set(current).add(subjectId));
      try {
        await api.revokeCaseAccess(identity, selectedId, subjectId, body);
      } catch (cause: unknown) {
        setError(describe(cause));
        return false;
      } finally {
        setPendingGrantSubjects((current) => {
          const next = new Set(current);
          next.delete(subjectId);
          return next;
        });
      }
      reload();
      return true;
    },
    [identity, selectedId, reload],
  );

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
          hasMore={nextCursor !== null}
          loadingMore={listMoreBusy}
          onLoadMore={() => {
            void loadMoreCases();
          }}
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
            grants={workspace.grants}
            events={events}
            streamStatus={streamStatus}
            live={live}
            busy={busy}
            pendingDecisionIds={pendingDecisionIds}
            pendingGrantSubjects={pendingGrantSubjects}
            onGrant={handleGrant}
            onRevoke={handleRevoke}
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
