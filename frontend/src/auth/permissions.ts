/**
 * Permission codenames (design §4.2, §7.2; requirement B4).
 *
 * These mirror `accounts/permissions_registry.py`. The duplication is
 * deliberate and narrow — the client needs the names to decide what to render —
 * but it is only ever used for **UX**. Every call is re-checked server-side
 * (§7.2), so a stale or tampered value here hides or reveals a button and
 * changes nothing about what the user can actually do.
 *
 * `GET /api/v1/permissions` returns the authoritative catalogue with labels, and
 * `/me` returns the user's resolved set.
 */

export const PERM = {
  GATE_IN_POST: 'gate_in.post',

  GATE_OUT_REQUEST: 'gate_out.request',
  GATE_OUT_APPROVE: 'gate_out.approve',
  GATE_OUT_RELEASE: 'gate_out.release',

  STOCK_ADJUST: 'stock.adjust',
  CUSTODY_TRANSFER: 'custody.transfer',

  JOB_CLOSEOUT: 'job.closeout',
  JOB_CLOSE_WITH_VARIANCE: 'job.close_with_variance',

  DISPOSAL_APPROVE: 'disposal.approve',

  // Epic O. The manager holds view_cost; the owner also holds view_margin and
  // view_rates, which is the split O14 asks for.
  PROJECT_VARIATION_APPROVE: 'project.variation_approve',
  PROJECT_VIEW_COST: 'project.view_cost',
  PROJECT_VIEW_MARGIN: 'project.view_margin',
  PROJECT_VIEW_RATES: 'project.view_rates',

  CATALOGUE_MANAGE: 'catalogue.manage',
  SETTINGS_MANAGE: 'settings.manage',
  USERS_MANAGE: 'users.manage',

  REPORT_VIEW_ALL: 'report.view_all',
} as const;

export type Permission = (typeof PERM)[keyof typeof PERM];
