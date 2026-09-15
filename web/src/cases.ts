/**
 * How the workbench tells the two Case lists apart.
 *
 * The operator queue (`GET /v1/cases`) is a data-plane view: every row comes
 * from a Case grant, so its `permissions` are the intersection with that
 * grant.  The administration inventory (`GET /v1/administration/cases`) is a
 * control-plane view: its rows report the caller's tenant-level `grant:*`
 * scopes, never `case:read`, because a row that carries no grant confers no
 * Case permission.  These helpers keep that distinction in one place.
 */

import type { CaseSummary } from "./types";

export interface WorkQueueRow {
  summary: CaseSummary;
  /**
   * True when this row may only be administered: it came from the inventory,
   * so reading its content would be refused by the next request.
   */
  administrationOnly: boolean;
}

/** A row whose content this identity may actually open. */
export function canReadContent(summary: CaseSummary): boolean {
  return summary.permissions.includes("case:read");
}

/** The operator queue: every row here comes from a Case grant. */
export function queueRows(cases: CaseSummary[]): WorkQueueRow[] {
  return cases.map((summary) => ({
    summary,
    administrationOnly: !canReadContent(summary),
  }));
}

/**
 * The administration block: inventory rows the queue cannot see.
 *
 * A Case that appears in both lists stays in the queue, because that row
 * carries the real Case permissions while an inventory row only carries the
 * administration scopes.
 */
export function administrationRows(
  queue: CaseSummary[],
  inventory: CaseSummary[],
): WorkQueueRow[] {
  const known = new Set(queue.map((item) => item.case_id));
  return inventory
    .filter((item) => !known.has(item.case_id))
    .map((summary) => ({
      summary,
      administrationOnly: !canReadContent(summary),
    }));
}

/**
 * Append one page, dropping rows already loaded.
 *
 * The two lists overlap on purpose -- an administrator's own Cases appear in
 * both -- so a page boundary can repeat a row that arrived from the other
 * source or an earlier page.
 */
export function appendPage(current: CaseSummary[], page: CaseSummary[]): CaseSummary[] {
  const known = new Set(current.map((item) => item.case_id));
  return [...current, ...page.filter((item) => !known.has(item.case_id))];
}
