/**
 * Money rules the phone can check before sending (design §4.17.5, R5).
 *
 * Pure and I/O-free. The server (`commercials/finance_rules.py`) stays the
 * authority and enforces the same rules on replay; these only let the form
 * warn early, so a clerk is not told "no" from a queue an hour later.
 */

import type {
  AllowanceLimitKey,
  AllowanceLimits,
  AllowanceRequest,
  AllowanceType,
  ExpenseStatus,
  ProjectExpense,
  TransportScope,
} from './types';

/** Parse `YYYY-MM-DD` to a UTC day number. No time-of-day, so no DST or zone drift. */
function dayNumber(iso: string): number {
  const [y, m, d] = iso.slice(0, 10).split('-').map(Number);
  return Date.UTC(y, m - 1, d) / 86_400_000;
}

/** Inclusive: the same day twice is one day (`days = to − from + 1`, §4.17.2). */
export function daysBetween(from: string, to: string): number {
  return dayNumber(to) - dayNumber(from) + 1;
}

/** A decimal string as integer cents, so comparisons carry no float error. NaN if unparseable. */
export function toCents(value: string | number): number {
  const text = String(value).trim().replace(/,/g, '');
  const match = /^(-?)(\d*)(?:\.(\d*))?$/.exec(text);
  if (!match || (match[2] === '' && !match[3])) return NaN;
  const frac = (match[3] ?? '').padEnd(3, '0');
  const cents = Number(match[2] || '0') * 100 + Number(frac.slice(0, 2));
  // Beyond two places, round half up on the third digit.
  const rounded = Number(frac[2]) >= 5 ? cents + 1 : cents;
  return match[1] ? -rounded : rounded;
}

export function fromCents(cents: number): string {
  const sign = cents < 0 ? '-' : '';
  const abs = Math.abs(cents);
  return `${sign}${Math.floor(abs / 100)}.${String(abs % 100).padStart(2, '0')}`;
}

/** The key into `allowance_limits`; null for FLOAT and OTHER, which are unlimited. */
export function limitKey(
  type: AllowanceType,
  scope: TransportScope | null | undefined,
): AllowanceLimitKey | null {
  if (type === 'TRANSPORT') {
    if (scope === 'WITHIN_NAIROBI') return 'TRANSPORT_WITHIN_NAIROBI';
    if (scope === 'OUTSIDE_NAIROBI') return 'TRANSPORT_OUTSIDE_NAIROBI';
    return null; // The server answers TRANSPORT_SCOPE_REQUIRED; the form asks for it.
  }
  if (type === 'NIGHT_OUT' || type === 'TEAM_ALLOWANCE') return type;
  return null;
}

export interface LimitBreach {
  /** Amount per day, 2 places. */
  daily: string;
  min: string | null;
  max: string | null;
  message: string;
}

/**
 * Amount against `min × days` and `max × days` (R5), with no rounding of the
 * total; both sides are integer cents. Null when within limits or unlimited.
 */
export function checkLimit(
  type: AllowanceType,
  scope: TransportScope | null | undefined,
  amount: string,
  days: number,
  limits: AllowanceLimits | null | undefined,
): LimitBreach | null {
  const key = limitKey(type, scope);
  const bounds = key && limits ? limits[key] : null;
  const total = toCents(amount);
  if (!bounds || !(days > 0) || Number.isNaN(total)) return null;

  const daily = fromCents(Math.round(total / days));
  const max = bounds.max === null ? NaN : toCents(bounds.max);
  const min = bounds.min === null ? NaN : toCents(bounds.min);

  if (!Number.isNaN(max) && total > max * days) {
    return {
      daily,
      min: bounds.min,
      max: bounds.max,
      message: `${daily} a day is above the limit of ${bounds.max} a day.`,
    };
  }
  if (!Number.isNaN(min) && total < min * days) {
    return {
      daily,
      min: bounds.min,
      max: bounds.max,
      message: `${daily} a day is below the minimum of ${bounds.min} a day.`,
    };
  }
  return null;
}

const OVERLAP_TYPES: AllowanceType[] = ['TRANSPORT', 'NIGHT_OUT', 'TEAM_ALLOWANCE'];
const LIVE: ExpenseStatus[] = ['PENDING_PM', 'PENDING_FINANCE', 'APPROVED', 'PAID'];

type Dated = Pick<AllowanceRequest, 'type' | 'from_date' | 'to_date'>;
type Existing = Dated & Pick<AllowanceRequest, 'status'> & { id?: number };

/**
 * The earlier request of the same type whose dates touch the candidate's
 * (R5). Pass only the caller's own requests: the server scopes by recorder.
 * FLOAT and OTHER are exempt; R2 allows a second float. A shared boundary day
 * overlaps, matching the inclusive `days`.
 */
export function findOverlap<E extends Existing>(
  candidate: Dated & { id?: number },
  existing: E[],
): E | null {
  if (!OVERLAP_TYPES.includes(candidate.type)) return null;
  const from = dayNumber(candidate.from_date);
  const to = dayNumber(candidate.to_date);
  let earliest: E | null = null;
  for (const other of existing) {
    if (other.type !== candidate.type || !LIVE.includes(other.status)) continue;
    if (candidate.id !== undefined && other.id === candidate.id) continue;
    if (dayNumber(other.from_date) > to || dayNumber(other.to_date) < from) continue;
    if (!earliest || dayNumber(other.from_date) < dayNumber(earliest.from_date)) {
      earliest = other;
    }
  }
  return earliest;
}

/**
 * `amount − Σ expenses not REJECTED − returned` (§4.17.2). Pending expenses
 * count. May be negative, which reads "owed to you".
 */
export function floatBalance(
  amount: string,
  expenses: Pick<ProjectExpense, 'amount' | 'status'>[],
  returned: string | null | undefined,
): string {
  let cents = toCents(amount);
  for (const expense of expenses) {
    if (expense.status !== 'REJECTED') cents -= toCents(expense.amount);
  }
  if (returned) cents -= toCents(returned);
  return fromCents(cents);
}

/** Open projects on a site (R1). One: fill it in; two or more: ask. */
export function candidateProjects<P extends { status: string; sites: number[] }>(
  siteId: number,
  projects: P[],
): P[] {
  return projects.filter((p) => p.status === 'OPEN' && p.sites.includes(siteId));
}

/** Same key as the server's `id_number_key`: upper-case, no spaces or dashes (R3). */
export function normaliseIdNumber(value: string): string {
  return value.toUpperCase().replace(/[\s-]/g, '');
}

const STATUS_LABELS: Record<ExpenseStatus, string> = {
  PENDING_PM: 'Waiting for PM',
  PENDING_FINANCE: 'Waiting for Finance',
  APPROVED: 'Approved',
  PAID: 'Paid',
  REJECTED: 'Rejected',
};

export function statusLabel(status: ExpenseStatus): string {
  return STATUS_LABELS[status] ?? status;
}
