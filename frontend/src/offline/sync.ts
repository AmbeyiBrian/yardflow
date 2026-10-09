/**
 * Draining the queue, and the state the UI shows about it (design §8; N1–N3).
 *
 * Three things live here:
 *
 * **The drain.** Everything pending goes in one batch, because a phone that has
 * been offline for a shift has ten things to send and ten round trips over a bad
 * connection is how a sync gets abandoned half way. The server answers per item,
 * so what landed is cleared and what was refused is kept and shown (§8.4).
 *
 * **The bundle fetch.** Reference data and *already approved* gate passes, taken
 * while there is still signal. §8.3 allows offline release only of these.
 *
 * **The online/offline signal.** `navigator.onLine` is famously optimistic — it
 * reports true on a phone connected to a wifi access point with no internet
 * behind it. So a failed request also marks the app offline, and a successful one
 * marks it back: what matters is whether the API answers, not what the OS thinks
 * about the radio.
 */

import { ApiError, api } from '../api/client';
import {
  ATTENDANCE_OPERATIONS,
  allQueued,
  FINANCE_OPERATIONS,
  type QueuedOperation,
  clearApplied,
  markApplied,
  markAttempted,
  markRejected,
  pending,
  saveReference,
  saveReleasable,
} from './db';
import { heldBehindRefusedSupplier, inCaptureOrder } from './financeQueue';
import { drainPhotos } from './photos';

export interface SyncOutcome {
  applied: number;
  rejected: number;
  replayed: number;
  /** Nothing was sent because there was nothing to send. */
  idle: boolean;
  error?: string;
  /** Finance photos (§4.17.8) still waiting on a connection or on their entry. */
  photosWaiting?: number;
}

interface SubmissionResult {
  client_uuid: string;
  status?: string;
  document_id?: string;
  document_number?: string;
  replayed?: boolean;
  code?: string;
  reason?: string;
  exception?: { code: string; reason: string } | null;
}

/**
 * Drain the queue, then the photos that hang off what landed (§4.17.8).
 *
 * Photos run even when no entry was pending: an earlier cycle may have applied
 * an entry and then lost the connection before its photos went up. They are
 * skipped only when the entries themselves just failed to reach the server,
 * since a second request would only wait out the same timeout.
 */
export async function drainQueue(): Promise<SyncOutcome> {
  const outcome = await drainEntries();
  if (outcome.error) return outcome;
  const photos = await drainPhotos();
  return photos.waiting > 0 ? { ...outcome, photosWaiting: photos.waiting } : outcome;
}

async function drainEntries(): Promise<SyncOutcome> {
  // §4.17.8: capture order, so a casual registered offline reaches the server
  // before the expense that names it.
  // §4.20.8: a gate-in or purchase naming a refused supplier waits behind it,
  // until the supplier is fixed and resent.
  const waiting = inCaptureOrder(await pending());
  const held = heldBehindRefusedSupplier(waiting, await allQueued());
  const rows = waiting.filter((row) => !held.has(row));
  if (rows.length === 0) {
    await clearApplied();
    return { applied: 0, rejected: 0, replayed: 0, idle: true };
  }

  try {
    const response = await api.post<{
      applied: number;
      rejected: number;
      replayed: number;
      results: SubmissionResult[];
    }>('/sync/submissions', {
      submissions: rows.map((row) => ({
        client_uuid: row.client_uuid,
        operation: row.operation,
        payload: row.payload,
        captured_at: row.captured_at,
      })),
    });

    for (const result of response.results) {
      const refusal = result.exception ?? (result.code ? { code: result.code, reason: result.reason ?? '' } : null);
      if (refusal) {
        // §8.4: kept, with the reason, until somebody has dealt with it. The
        // conflict itself lives on the server as a SyncException; this is the
        // device's copy of "your capture did not land, and here is why".
        // R6: a refused finance entry stays on the phone with the server's code
        // and reason, so "ALLOWANCE_OVERLAP: overlaps AR-000012" is what the
        // clerk reads when deciding how to fix it.
        // R13, §4.18.9: the same for a refused clock-in or clock-out, whose
        // CLOCK_OUTSIDE_AREA reason is never turned into hours by resending.
        const operation = rows.find((row) => row.client_uuid === result.client_uuid)
          ?.operation as QueuedOperation;
        const isFinance =
          FINANCE_OPERATIONS.includes(operation) || ATTENDANCE_OPERATIONS.includes(operation);
        await markRejected(
          result.client_uuid,
          isFinance && refusal.code && refusal.reason
            ? `${refusal.code}: ${refusal.reason}`
            : refusal.reason || refusal.code,
        );
      } else {
        await markApplied(result.client_uuid, {
          id: result.document_id,
          number: result.document_number,
        });
      }
    }

    setOnline(true);
    return {
      applied: response.applied,
      rejected: response.rejected,
      replayed: response.replayed,
      idle: false,
    };
  } catch (error) {
    // A transport failure is not a refusal. Every row stays PENDING and is
    // retried — losing that distinction is how a flaky connection turns into
    // lost captures.
    const message = error instanceof ApiError ? error.message : 'No connection.';
    for (const row of rows) {
      await markAttempted(row.client_uuid, message);
    }
    if (!(error instanceof ApiError)) setOnline(false);
    return { applied: 0, rejected: 0, replayed: 0, idle: false, error: message };
  }
}

/**
 * Fill the device's cache for offline work (§8.1, §8.3, T8.2, T8.5).
 *
 * One request. This is the last thing a phone does before losing signal, and
 * each extra round trip is a chance to lose half of it.
 */
export async function fetchBundle(): Promise<{ ok: boolean; passes: number }> {
  try {
    const bundle = await api.get<{
      item_types: unknown[];
      locations: unknown[];
      clients: unknown[];
      sites: unknown[];
      people: unknown[];
      // R6, §4.17.8: what the money forms need with no signal. Optional so a
      // device that fetched from an older server still stores what it got.
      expense_categories?: unknown[];
      casuals?: unknown[];
      my_floats?: unknown[];
      finance_limits?: Record<string, unknown>;
      // R15, §4.20.8: active, not REJECTED suppliers (id, name, status) and
      // ACTIVE vehicles and generators (id, tag, name, type). Optional for an
      // older server.
      suppliers?: unknown[];
      // R9, §4.19.11: `{id, headroom}` per open project, only for a caller who may see
      // its cost. Optional for an older server.
      project_headroom?: unknown[];
      vehicles?: unknown[];
      // R13, §4.18.9: the area the phone checks a clock-in against. `sites` and
      // `locations` carry latitude/longitude/radius_m; `locations` leaves out
      // OFFICE, so those arrive here. Optional for an older server.
      offices?: unknown[];
      attendance?: { accuracy_cap_m: number; auto_close_hour: number };
      releasable_gate_outs: {
        id: number;
        number: string;
        status: string;
        destination: string;
        custody_holder: string;
        expires_at: string | null;
        lines: unknown[];
      }[];
    }>('/sync/bundle');

    await Promise.all([
      saveReference('item_types', bundle.item_types),
      saveReference('locations', bundle.locations),
      saveReference('clients', bundle.clients),
      saveReference('sites', bundle.sites),
      saveReference('people', bundle.people),
      saveReference('expense_categories', bundle.expense_categories ?? []),
      saveReference('casuals', bundle.casuals ?? []),
      saveReference('my_floats', bundle.my_floats ?? []),
      saveReference('finance_limits', [bundle.finance_limits ?? {}]),
      saveReference('suppliers', bundle.suppliers ?? []),
      saveReference('vehicles', bundle.vehicles ?? []),
      saveReference('project_headroom', bundle.project_headroom ?? []),
      saveReference('offices', bundle.offices ?? []),
      saveReference('attendance', bundle.attendance ? [bundle.attendance] : []),
    ]);
    // Only what the server says is approved. The device never adds to this.
    await saveReleasable(bundle.releasable_gate_outs);

    setOnline(true);
    return { ok: true, passes: bundle.releasable_gate_outs.length };
  } catch {
    setOnline(false);
    return { ok: false, passes: 0 };
  }
}

/* -------------------------------------------------------------------------- */
/* Online state                                                               */
/* -------------------------------------------------------------------------- */

type Listener = (online: boolean) => void;

const listeners = new Set<Listener>();
let online = typeof navigator === 'undefined' ? true : navigator.onLine;

export function isOnline(): boolean {
  return online;
}

/**
 * Record what we actually observed.
 *
 * Called by the sync functions above rather than only by browser events, because
 * `navigator.onLine` reports true on a phone attached to an access point with no
 * internet behind it — which in a yard is a common situation, not an edge case.
 */
export function setOnline(value: boolean): void {
  if (online === value) return;
  online = value;
  for (const listener of listeners) listener(value);
}

export function watchOnline(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Wire the browser's own events. Called once, from the provider. */
export function startOnlineWatch(): () => void {
  if (typeof window === 'undefined') return () => {};

  const goOnline = () => {
    setOnline(true);
    // Drain immediately: the storekeeper who just walked back into signal
    // should not have to find a button.
    void drainQueue();
  };
  const goOffline = () => setOnline(false);

  window.addEventListener('online', goOnline);
  window.addEventListener('offline', goOffline);
  return () => {
    window.removeEventListener('online', goOnline);
    window.removeEventListener('offline', goOffline);
  };
}

/** Queue a mutation, or send it now if there is a connection (N1). */
export async function submitOrQueue<T>(
  operation: QueuedOperation,
  payload: unknown,
  online_path: () => Promise<T>,
): Promise<{ queued: boolean; result?: T; client_uuid?: string }> {
  const { enqueue } = await import('./db');

  if (isOnline()) {
    try {
      return { queued: false, result: await online_path() };
    } catch (error) {
      // A validation failure must not be queued — it would fail again forever.
      // Only a transport failure falls through to the queue.
      if (error instanceof ApiError) throw error;
      setOnline(false);
    }
  }

  const client_uuid = await enqueue(operation, payload);
  if (!client_uuid) {
    // No IndexedDB: better to fail loudly than to pretend something was saved.
    throw new Error(
      'This browser cannot store anything offline, and there is no connection. ' +
        'Nothing has been saved.',
    );
  }
  return { queued: true, client_uuid };
}

/** Queue every pending row, drain it, and report — used by the banner's button. */
export async function syncNow(): Promise<SyncOutcome> {
  const bundle = await fetchBundle();
  const outcome = await drainQueue();
  return bundle.ok ? outcome : { ...outcome, error: outcome.error ?? 'No connection.' };
}
