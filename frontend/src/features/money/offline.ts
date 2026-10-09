/**
 * Offline capture for money-out entries (design §4.17.8, §8; R6).
 *
 * The screens call these when there is no signal; the queue, the photo step and
 * the replay live in `offline/`. Nothing here approves, pays or closes a float
 * — those are never offline (§8.3), so there is deliberately no function for
 * them.
 *
 * Each entry's `client_uuid` is minted here, once, and travels twice: as the
 * queue row's idempotency key (N2) and inside the payload, because the entity
 * has its own unique `client_uuid` on the server (§4.17.8). A retry reuses it.
 */

import { liveQuery } from 'dexie';
import { useEffect, useState } from 'react';

import {
  FINANCE_OPERATIONS,
  addPhotos,
  db,
  enqueue,
  newUuid,
  type QueuedOperation,
} from '../../offline/db';
import { financeEntries, type QueuedFinanceEntry } from '../../offline/financeQueue';
import type { AllowanceInput, CasualInput, ExpenseInput } from './api';

export type { QueuedFinanceEntry } from '../../offline/financeQueue';

/** A photo taken on the phone, caption chosen from Receipt / Fuel pump / Work done / ID / Other. */
export interface PhotoDraft {
  caption: string;
  blob: Blob;
  filename: string;
}

/**
 * A casual line on a queued expense. Names the casual by server id, or — for
 * one registered earlier in this same queue — by `casual_client_uuid`, which
 * the server resolves on replay because the queue replays in capture order.
 */
export interface QueuedCasualLine {
  casual?: number;
  casual_client_uuid?: string;
  days: number;
  amount?: string | null;
}

export type QueuedExpenseBody = Omit<ExpenseInput, 'casual_lines' | 'client_uuid'> & {
  casual_lines?: QueuedCasualLine[];
};

export type QueuedAllowanceBody = Omit<AllowanceInput, 'client_uuid'>;
export type QueuedCasualBody = Omit<CasualInput, 'client_uuid'>;

const NO_STORAGE =
  'This browser cannot store anything offline, and there is no connection. ' +
  'Nothing has been saved.';

async function queueFinance(
  operation: QueuedOperation,
  body: object,
  client_uuid: string,
  photos: PhotoDraft[],
): Promise<string> {
  const saved = await enqueue(operation, { ...body, client_uuid }, client_uuid);
  // No IndexedDB: fail loudly rather than pretend something was saved.
  if (!saved) throw new Error(NO_STORAGE);
  await addPhotos(client_uuid, photos);
  return saved;
}

/** Queue an expense with its photos. Returns its `client_uuid`. */
export function queueExpense(body: QueuedExpenseBody, photos: PhotoDraft[] = []): Promise<string> {
  const client_uuid = newUuid();
  // "arriving" vs "no evidence" on the approver's screen (§4.17.8) depends on
  // this count, so it is set from what was actually captured unless the screen
  // says otherwise.
  const photos_expected = body.photos_expected ?? photos.length;
  return queueFinance('EXPENSE', { ...body, photos_expected }, client_uuid, photos);
}

/** Queue an allowance or float request. Returns its `client_uuid`. */
export function queueAllowanceRequest(body: QueuedAllowanceBody): Promise<string> {
  return queueFinance('ALLOWANCE_REQUEST', body, newUuid(), []);
}

/**
 * Queue a casual registration, with the ID photo. Returns the casual's
 * `client_uuid`, which a later expense in the same queue names as
 * `casual_client_uuid`.
 */
export function queueCasual(body: QueuedCasualBody, idPhoto?: PhotoDraft): Promise<string> {
  return queueFinance('CASUAL', body, newUuid(), idPhoto ? [idPhoto] : []);
}

/**
 * "Fix and resend" on a refused entry (R6): a new `client_uuid` with
 * `supersedes_client_uuid`, which tells the server to resolve the old exception
 * when the new one lands. The old uuid cannot be reused — the server already
 * answered it, and replaying it would return the same refusal.
 *
 * The photos move with the entry, so nothing has to be taken again, and the old
 * refused row is dropped from the device since the new one replaces it.
 */
export async function resendCorrected(
  oldClientUuid: string,
  newBody: Record<string, unknown>,
): Promise<string> {
  const store = db();
  if (!store) throw new Error(NO_STORAGE);

  const old = await store.queue.where('client_uuid').equals(oldClientUuid).first();
  if (!old || !FINANCE_OPERATIONS.includes(old.operation)) {
    throw new Error('That entry is no longer on this device.');
  }

  const client_uuid = newUuid();
  const saved = await enqueue(
    old.operation,
    { ...newBody, client_uuid, supersedes_client_uuid: oldClientUuid },
    client_uuid,
  );
  if (!saved) throw new Error(NO_STORAGE);

  await store.photos
    .where('queue_client_uuid')
    .equals(oldClientUuid)
    .modify({ queue_client_uuid: client_uuid });
  await store.queue.where('client_uuid').equals(oldClientUuid).delete();
  return client_uuid;
}

/**
 * Finance entries waiting to send or refused, live — a refusal arriving during a
 * sync shows without a reload. Empty where IndexedDB is unavailable.
 */
export function useQueuedFinance(): QueuedFinanceEntry[] {
  const [entries, setEntries] = useState<QueuedFinanceEntry[]>([]);

  useEffect(() => {
    const store = db();
    if (!store) return;
    const subscription = liveQuery(async () =>
      financeEntries(await store.queue.toArray(), await store.photos.toArray()),
    ).subscribe({
      next: setEntries,
      // A blocked store is "nothing to show", not a broken page.
      error: () => setEntries([]),
    });
    return () => subscription.unsubscribe();
  }, []);

  return entries;
}
