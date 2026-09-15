import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ApiError, api, type Identity } from "./api";
import {
  administrationRows,
  appendPage,
  canReadContent,
  queueRows,
  type WorkQueueRow,
} from "./cases";
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
  CaseListResponse,
  CaseStatus,
  CaseSummary,
  Review,
  ReviewDecision,
} from "./types";

const DEFAULT_IDENTITY: Identity = { tenantId: "synthetic-demo", subjectId: "ops-1" };

interface Workspace {
  /** null when the row is administrable only; its content routes stay closed. */
  detail: CaseDetail | null;
  reviews: Review[];
  approvals: Approval[];
  /** null when this identity may not read access rows. */
  grants: CaseGrant[] | null;
  /** What the server says this identity may hand out on this Case. */
  delegable: string[];
  canAdminister: boolean;
}

interface Cursor {
  createdAt: string;
  caseId: string;
}

function describe(cause: unknown): string {
  if (cause instanceof ApiError) {
    return cause.message;
  }
  return cause instanceof Error ? cause.message : String(cause);
}

/** Keyset cursor of a page, or null when the last page was reached. */
function cursorOf(response: CaseListResponse): Cursor | null {
  return response.next_created_at !== null && response.next_case_id !== null
    ? { createdAt: response.next_created_at, caseId: response.next_case_id }
    : null;
}

export default function App() {
  const [identity, setIdentity] = useState<Identity>(DEFAULT_IDENTITY);
  const [statusFilter, setStatusFilter] = useState<CaseStatus | "ALL">("ALL");
  const [cases, setCases] = useState<CaseSummary[]>([]);
  const [inventory, setInventory] = useState<CaseSummary[]>([]);
  const [selected, setSelected] = useState<WorkQueueRow | null>(null);
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [events, setEvents] = useState<CaseEvent[]>([]);
  const [live, setLive] = useState(true);
  const [streamStatus, setStreamStatus] = useState<StreamStatus>("stopped");
  const [error, setError] = useState<string | null>(null);
  const [listBusy, setListBusy] = useState(false);
  const [listMoreBusy, setListMoreBusy] = useState(false);
  const [inventoryCursor, setInventoryCursor] = useState<Cursor | null>(null);
  const [inventoryMoreBusy, setInventoryMoreBusy] = useState(false);
  const [nextCursor, setNextCursor] = useState<Cursor | null>(null);
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

  // Both lists share the status filter, so the query object is built once and
  // stays referentially stable across renders.
  const filterQuery = useMemo(
    () => (statusFilter === "ALL" ? {} : { status: statusFilter }),
    [statusFilter],
  );

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
      .listCases(identity, filterQuery)
      .then((response) => {
        if (cancelled) {
          return;
        }
        setCases(response.cases);
        setNextCursor(cursorOf(response));
      })
      .catch((cause: unknown) => {
        if (cancelled) {
          return;
        }
        setCases([]);
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
  }, [identity, filterQuery, refreshToken]);

  // The administration inventory is a control-plane page, and a refusal is an
  // ordinary state rather than an error: most identities are not access
  // administrators, and the workbench must not nag them about it.
  useEffect(() => {
    let cancelled = false;
    setInventory([]);
    setInventoryCursor(null);
    api
      .listAdministrableCases(identity, filterQuery)
      .then(
        (response) => response,
        (cause: unknown) => {
          if (cause instanceof ApiError && cause.isFatal) {
            return null;
          }
          throw cause;
        },
      )
      .then((response) => {
        if (cancelled || response === null) {
          return;
        }
        setInventory(response.cases);
        setInventoryCursor(cursorOf(response));
      })
      .catch((cause: unknown) => {
        if (!cancelled) {
          setError(describe(cause));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [identity, filterQuery, refreshToken]);

  // A new identity or filter invalidates the previous selection.  Refreshing
  // must not: an administrator would be thrown out of the Case it is handing
  // over.  Repairing is a separate effect so that a list which is briefly
  // empty while it reloads keeps the current selection.
  useEffect(() => {
    setSelected(null);
  }, [identity, statusFilter]);

  useEffect(() => {
    if (cases.length === 0 && inventory.length === 0) {
      return;
    }
    setSelected((current) => {
      // Re-read the row from the freshly loaded pages so that a refresh also
      // updates the header, and prefer the queue: a queued Case carries its
      // real Case permissions while an inventory row only carries the
      // administration scopes.  Keeping the old object when nothing moved
      // avoids re-fetching the workspace on every render.
      const caseId = current?.summary.case_id;
      const found =
        cases.find((item) => item.case_id === caseId) ??
        inventory.find((item) => item.case_id === caseId);
      const next = found ?? cases[0];
      if (next === undefined) {
        return null;
      }
      if (current !== null && next === current.summary) {
        return current;
      }
      return { summary: next, administrationOnly: !canReadContent(next) };
    });
  }, [cases, inventory]);

  const loadMoreCases = useCallback(async () => {
    if (nextCursor === null || listMoreBusy || listBusy) {
      return;
    }
    setListMoreBusy(true);
    setError(null);
    const generation = listGeneration.current;
    try {
      const response = await api.listCases(identity, {
        ...filterQuery,
        afterCreatedAt: nextCursor.createdAt,
        afterCaseId: nextCursor.caseId,
      });
      if (generation !== listGeneration.current) {
        return;
      }
      setCases((current) => appendPage(current, response.cases));
      setNextCursor(cursorOf(response));
    } catch (cause: unknown) {
      if (generation === listGeneration.current) {
        setError(describe(cause));
      }
    } finally {
      if (generation === listGeneration.current) {
        setListMoreBusy(false);
      }
    }
  }, [identity, filterQuery, listBusy, listMoreBusy, nextCursor]);

  const loadMoreInventory = useCallback(async () => {
    if (inventoryCursor === null || inventoryMoreBusy) {
      return;
    }
    setInventoryMoreBusy(true);
    const generation = listGeneration.current;
    try {
      const response = await api.listAdministrableCases(identity, {
        ...filterQuery,
        afterCreatedAt: inventoryCursor.createdAt,
        afterCaseId: inventoryCursor.caseId,
      });
      if (generation !== listGeneration.current) {
        return;
      }
      setInventory((current) => appendPage(current, response.cases));
      setInventoryCursor(cursorOf(response));
    } catch (cause: unknown) {
      if (generation === listGeneration.current) {
        setError(describe(cause));
      }
    } finally {
      if (generation === listGeneration.current) {
        setInventoryMoreBusy(false);
      }
    }
  }, [identity, filterQuery, inventoryCursor, inventoryMoreBusy]);

  // One derived id, plus the one row property this fetch branches on, so that
  // the content routes, the event subscription and the grant actions cannot
  // disagree about which Case they are acting on.  Depending on the id instead
  // of the row object also lets a refreshed summary update the header without
  // re-fetching the workspace.
  const selectedId = selected?.summary.case_id ?? null;
  const administrationOnly = selected?.administrationOnly ?? false;

  useEffect(() => {
    if (selectedId === null) {
      setWorkspace(null);
      return;
    }
    const caseId = selectedId;
    let cancelled = false;
    setDetailBusy(true);
    void (async () => {
      try {
        // Access rows are a tenant-level view.  The Case projection cannot
        // answer whether this identity may read them - a real bearer identity
        // sees only the intersection with its grant, which never contains
        // `grant:read` - so the server decides and a refusal just hides the
        // panel instead of showing an error the operator cannot act on.
        const grants = await api.listCaseGrants(identity, caseId).then(
          (response) => response,
          (cause: unknown) => {
            if (cause instanceof ApiError && cause.isFatal) {
              return null;
            }
            throw cause;
          },
        );
        // Only a row that actually carries a Case grant may call the content
        // routes: an inventory row would be refused.  Saying so once in the
        // panel beats a 403 banner on every visit.
        let detail: CaseDetail | null = null;
        let reviews: Review[] = [];
        let approvals: Approval[] = [];
        if (!administrationOnly) {
          const [detailResponse, reviewList, approvalList] = await Promise.all([
            api.getCase(identity, caseId),
            api.listReviews(identity, caseId),
            api.listApprovals(identity, caseId),
          ]);
          detail = detailResponse;
          reviews = reviewList.reviews;
          approvals = approvalList.approvals;
        }
        if (cancelled) {
          return;
        }
        setWorkspace({
          detail,
          reviews,
          approvals,
          grants: grants === null ? null : grants.grants,
          delegable: grants?.delegable ?? [],
          canAdminister: grants?.can_administer ?? false,
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
  }, [identity, selectedId, administrationOnly, refreshToken]);

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
          queue={{
            rows: queueRows(cases),
            hasMore: nextCursor !== null,
            loadingMore: listMoreBusy,
            onLoadMore: () => {
              void loadMoreCases();
            },
          }}
          administration={{
            rows: administrationRows(cases, inventory),
            hasMore: inventoryCursor !== null,
            loadingMore: inventoryMoreBusy,
            onLoadMore: () => {
              void loadMoreInventory();
            },
          }}
          selectedId={selectedId}
          statusFilter={statusFilter}
          onSelect={setSelected}
          onStatusFilter={setStatusFilter}
        />
        {workspace === null || selected === null ? (
          <section className="detail placeholder">
            <p className="empty">选择左侧工单以查看 Run、审核、审批与事件。</p>
          </section>
        ) : (
          <CaseDetailPane
            summary={selected.summary}
            detail={workspace.detail}
            reviews={workspace.reviews}
            approvals={workspace.approvals}
            grants={workspace.grants}
            delegable={workspace.delegable}
            canAdminister={workspace.canAdminister}
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
