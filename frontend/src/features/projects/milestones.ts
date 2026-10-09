/**
 * Pure maths for the milestones panel (R11, R12; design §4.19.7).
 *
 * The server owns the verdict (`state`); these helpers are what the screen
 * shows when it has to total or explain figures itself. Everything is in
 * integer cents so sums are exact.
 */

import type { Milestone, MilestoneState, ShareType } from './milestonesApi';

export const toCents = (v: string | number | null | undefined): number =>
  Math.round(Number(v ?? 0) * 100);

export const fromCents = (cents: number): string => (cents / 100).toFixed(2);

/** A milestone's amount: percent of the contract value, or the fixed amount (§4.19.7). */
export function milestoneAmountCents(
  shareType: ShareType,
  shareValue: string | null | undefined,
  contractValue: string | null | undefined,
): number {
  if (shareValue === null || shareValue === undefined || shareValue === '') return 0;
  if (shareType === 'AMOUNT') return toCents(shareValue);
  return Math.round((toCents(contractValue) * Number(shareValue)) / 100);
}

/** Σ percent shares; the screen warns until this is 100 (a warning, not a block). */
export function percentSharesTotal(
  milestones: Pick<Milestone, 'share_type' | 'share_value'>[],
): number {
  return milestones
    .filter((m) => m.share_type === 'PERCENT')
    .reduce((sum, m) => sum + Number(m.share_value || 0), 0);
}

/** `YYYY-MM-DD` plus whole days, in UTC so a timezone never moves the date. */
export function addDays(date: string, days: number): string {
  const [y, m, d] = date.split('-').map(Number);
  return new Date(Date.UTC(y, m - 1, d + days)).toISOString().slice(0, 10);
}

/**
 * OVERDUE: something invoiced is unpaid and today is past the latest invoice
 * date plus the payment terms. No terms days, never overdue (§4.19.7).
 */
export function isOverdue(
  invoicedCents: number,
  receivedCents: number,
  latestInvoiceDate: string | null,
  termsDays: number | null | undefined,
  today: string,
): boolean {
  if (!termsDays || !latestInvoiceDate) return false;
  if (invoicedCents - receivedCents <= 0) return false;
  return today > addDays(latestInvoiceDate, termsDays);
}

/** Receipts beyond the invoiced amount are refused (RECEIPT_EXCEEDS_INVOICED). */
export function receiptRoomCents(invoicedCents: number, receivedCents: number): number {
  return Math.max(0, invoicedCents - receivedCents);
}

export interface PoTotals {
  value: number;
  invoiced: number;
  received: number;
  /** `value − received` (§4.19.7 report). */
  outstanding: number;
  /** Invoiced and not yet paid. */
  invoicedUnpaid: number;
}

export function poTotals(
  contractValue: string | null | undefined,
  milestones: Pick<Milestone, 'invoiced' | 'received'>[],
): PoTotals {
  const invoiced = milestones.reduce((s, m) => s + toCents(m.invoiced), 0);
  const received = milestones.reduce((s, m) => s + toCents(m.received), 0);
  const value = toCents(contractValue);
  return {
    value,
    invoiced,
    received,
    outstanding: value - received,
    invoicedUnpaid: invoiced - received,
  };
}

export const STATE_LABELS: Record<MilestoneState, string> = {
  NOT_DUE: 'Not due',
  DUE: 'Due',
  OVERDUE: 'Overdue',
  INVOICED: 'Invoiced',
  PART_PAID: 'Part paid',
  PAID: 'Paid',
};

export const CONDITION_LABELS = {
  NONE: 'On PO',
  ALL_SITES_ACCEPTED: 'All sites accepted',
  DATE: 'On a date',
} as const;

/** Whole days between two `YYYY-MM-DD` dates (a - b). */
export function daysBetween(a: string, b: string): number {
  const t = (s: string) => {
    const [y, m, d] = s.split('-').map(Number);
    return Date.UTC(y, m - 1, d);
  };
  return Math.round((t(a) - t(b)) / 86_400_000);
}
