/**
 * The seam for offline entries (design §4.17.8, R6; task T15.10).
 *
 * List items may carry `queued: true`, which the cards show as "Waiting to
 * send". T15.10 replaces this hook with one that reads the outbox; until then
 * nothing is queued, so the lists show only what the server has.
 */

import type { AllowanceRequest, ProjectExpense } from './types';

/** A server row, or one still on the phone. */
export type MaybeQueued<T> = T & { queued?: boolean };

export function useQueuedMoney(): {
  expenses: MaybeQueued<ProjectExpense>[];
  requests: MaybeQueued<AllowanceRequest>[];
} {
  return { expenses: [], requests: [] };
}
