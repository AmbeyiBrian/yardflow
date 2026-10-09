/** Pure figures for the subcontract panel and the job sheet (R8; §4.19.4). Cents keep sums exact. */

import type { SubcontractJob, SubcontractPayment } from './subcontractsApi';

export const toCents = (v: string | number | null | undefined): number =>
  Math.round(Number(v ?? 0) * 100);

export const fromCents = (c: number): string => (c / 100).toFixed(2);

/** Work done: sum of agreed price of CLOSED jobs, the O11 rule (§4.19.4). */
export function workDoneCents(jobs: Pick<SubcontractJob, 'status' | 'agreed_price'>[]): number {
  return jobs
    .filter((j) => j.status === 'CLOSED')
    .reduce((sum, j) => sum + toCents(j.agreed_price), 0);
}

/** Committed: agreed price of non-cancelled jobs, open or closed (§4.19.4). */
export function committedCents(jobs: Pick<SubcontractJob, 'status' | 'agreed_price'>[]): number {
  return jobs
    .filter((j) => j.status !== 'CANCELLED')
    .reduce((sum, j) => sum + toCents(j.agreed_price), 0);
}

/** Paid: signed APPROVED payments; reversals are negative; pending is excluded. */
export function paidCents(payments: Pick<SubcontractPayment, 'status' | 'amount'>[]): number {
  return payments
    .filter((p) => p.status === 'APPROVED')
    .reduce((sum, p) => sum + toCents(p.amount), 0);
}

/** Owed = work done - paid. Negative is an advance. */
export function owedCents(workDone: number, paid: number): number {
  return workDone - paid;
}

/** Would awarding `price` more push the committed jobs past the contract value? Warns, never blocks. */
export function wouldExceedContract(
  contractValue: string,
  committed: number,
  price: string | null | undefined,
): boolean {
  return committed + toCents(price) > toCents(contractValue);
}

/** "Advance paid" wording for a negative owed figure. */
export function owedLabel(owed: number): string {
  return owed < 0 ? 'Advance paid' : 'Owed';
}
