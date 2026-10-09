/** Pure display maths for the budget and site panels (R9, R10; §4.19.5, §4.19.6). */

import type { BudgetKind, BudgetPosition } from './stage2Api';

export const KIND_LABELS: Record<BudgetKind, string> = {
  COST: 'Cost (material, labour, expenses)',
  ALLOWANCE: 'Allowances',
  FLOAT: 'Floats',
  PURCHASE: 'Yard purchases',
  SUBCONTRACT: 'Subcontracts',
};

/** Decimal strings in cents, so sums are exact. */
const toCents = (v: string | null | undefined): number => Math.round(Number(v ?? 0) * 100);

/** `committed + spent`: the figure that matters for "will we overspend" (§4.19.5). */
export function committedPlusSpent(p: Pick<BudgetPosition, 'spent' | 'committed'>): string {
  return ((toCents(p.spent) + toCents(p.committed)) / 100).toFixed(2);
}

/** Share of the budget used (spent + committed), or null with no budget. */
export function percentUsed(p: BudgetPosition): number | null {
  const budget = toCents(p.budget);
  if (p.budget === null || budget <= 0) return null;
  return Math.round(((toCents(p.spent) + toCents(p.committed)) / budget) * 100);
}

/** Over budget when remaining is negative; no budget is never "over" (R12). */
export function isOverBudget(p: BudgetPosition): boolean {
  return p.remaining !== null && toCents(p.remaining) < 0;
}

/** A recorded overrun with no reason: replayed from a phone that could not know (§4.19.5). */
export function reasonText(reason: string): string {
  return reason.trim() || 'Over budget, no reason given';
}

/** R10: accepted needs an acceptance date AND a certificate. */
export function isAcceptedSite(acceptedOn: string | null, certificates: number): boolean {
  return Boolean(acceptedOn) && certificates > 0;
}

/** `2026-03-04T10:00:00Z` or `2026-03-04` as `2026-03-04`; null as an em dash. */
export function dateOnly(value: string | null | undefined): string {
  return value ? value.slice(0, 10) : '—';
}
