/**
 * The offline store (design §8.1; N1, N2, T8.2, T8.3).
 *
 * §8.1: "Dexie holds: reference data (item types, locations, clients, sites,
 * users), a **mutation queue**, and downloaded approved gate passes eligible for
 * release."
 *
 * Three tables, and the third is the one with a security property attached:
 *
 * `reference` — enough to build a gate-in or gate-out line with no signal.
 * Refreshed whenever the app is online, and stamped with when, so a screen can
 * say "as at 08:12" rather than implying it is current.
 *
 * `queue` — one row per captured mutation, each carrying its own
 * `client_uuid`. That uuid is generated **once, at capture**, and never
 * regenerated on retry: it is what makes a replay idempotent server-side (§8.2,
 * N2). Regenerating it on retry would be the bug that turns one delivery into
 * three.
 *
 * `releasable` — approved gate passes downloaded for offline release. §8.3
 * permits offline release *only* of these, because they are already authorised.
 * Nothing writes to this table except a bundle fetch from the server, so a
 * device cannot invent a pass to release.
 *
 * IndexedDB can be unavailable — a private window, a browser with storage
 * blocked. Every function here degrades to "no offline support" rather than
 * throwing, because a storekeeper with a locked-down browser must still be able
 * to work online.
 */

import Dexie, { type Table } from 'dexie';

/** What the queue can hold. §8 keeps offline capture to these three. */
export type QueuedOperation = 'GATE_IN' | 'GATE_OUT_REQUEST' | 'GATE_OUT_RELEASE';

export type QueueStatus =
  /** Captured, not yet sent. */
  | 'PENDING'
  /** Sent and accepted. Kept briefly so the screen can show what landed. */
  | 'APPLIED'
  /** The server refused it — a conflict for somebody to resolve (§8.4). */
  | 'REJECTED';

export interface QueuedMutation {
  id?: number;
  /** Generated once at capture. Never regenerated (N2). */
  client_uuid: string;
  operation: QueuedOperation;
  payload: unknown;
  /** When the phone captured it, not when it synced. */
  captured_at: string;
  status: QueueStatus;
  attempts: number;
  last_error?: string;
  /** What it became, once the server said. */
  document_number?: string;
  document_id?: string;
  /** Set when the server refused it, so the screen can link to the exception. */
  exception_reason?: string;
}

export interface ReferenceRow {
  /** `item_types`, `locations`, `clients`, `sites`, `people`. */
  key: string;
  rows: unknown[];
  fetched_at: string;
}

export interface ReleasablePass {
  id: number;
  number: string;
  status: string;
  destination: string;
  custody_holder: string;
  expires_at: string | null;
  lines: unknown[];
  fetched_at: string;
}

class YardFlowDatabase extends Dexie {
  queue!: Table<QueuedMutation, number>;
  reference!: Table<ReferenceRow, string>;
  releasable!: Table<ReleasablePass, number>;

  constructor() {
    super('yardflow');
    this.version(1).stores({
      // `client_uuid` unique: capturing the same thing twice locally would
      // otherwise become two documents, which is the failure N2 exists for.
      queue: '++id, &client_uuid, status, operation, captured_at',
      reference: 'key',
      releasable: 'id, number',
    });
  }
}

let database: YardFlowDatabase | null = null;
let unavailable = false;

/**
 * The database, or `null` where IndexedDB is unavailable.
 *
 * Callers treat `null` as "no offline support" and carry on online. A private
 * window is not an error state.
 */
export function db(): YardFlowDatabase | null {
  if (unavailable) return null;
  if (database) return database;
  try {
    database = new YardFlowDatabase();
    return database;
  } catch {
    unavailable = true;
    return null;
  }
}

export function offlineSupported(): boolean {
  return db() !== null && typeof indexedDB !== 'undefined';
}

/* -------------------------------------------------------------------------- */
/* Reference data (T8.2)                                                      */
/* -------------------------------------------------------------------------- */

export async function saveReference(key: string, rows: unknown[]): Promise<void> {
  const store = db();
  if (!store) return;
  try {
    await store.reference.put({ key, rows, fetched_at: new Date().toISOString() });
  } catch {
    // A full or blocked store is not worth failing a page render over.
  }
}

export async function readReference<T>(key: string): Promise<{ rows: T[]; fetchedAt: string | null }> {
  const store = db();
  if (!store) return { rows: [], fetchedAt: null };
  try {
    const row = await store.reference.get(key);
    return { rows: (row?.rows as T[]) ?? [], fetchedAt: row?.fetched_at ?? null };
  } catch {
    return { rows: [], fetchedAt: null };
  }
}

/** The oldest thing in the cache, so a screen can say how stale it is (T8.2). */
export async function referenceAge(): Promise<string | null> {
  const store = db();
  if (!store) return null;
  try {
    const rows = await store.reference.toArray();
    if (rows.length === 0) return null;
    return rows.reduce(
      (oldest, row) => (row.fetched_at < oldest ? row.fetched_at : oldest),
      rows[0].fetched_at,
    );
  } catch {
    return null;
  }
}

/* -------------------------------------------------------------------------- */
/* The mutation queue (T8.3)                                                  */
/* -------------------------------------------------------------------------- */

/**
 * Queue one mutation. Returns its `client_uuid`.
 *
 * The uuid is minted here and stored with the payload, so every later retry
 * sends the same one — which is what makes the server's idempotency work (N2).
 */
export async function enqueue(
  operation: QueuedOperation,
  payload: unknown,
): Promise<string | null> {
  const store = db();
  if (!store) return null;

  const client_uuid = newUuid();
  await store.queue.add({
    client_uuid,
    operation,
    payload,
    captured_at: new Date().toISOString(),
    status: 'PENDING',
    attempts: 0,
  });
  return client_uuid;
}

export async function pending(): Promise<QueuedMutation[]> {
  const store = db();
  if (!store) return [];
  try {
    return await store.queue.where('status').equals('PENDING').toArray();
  } catch {
    return [];
  }
}

export async function pendingCount(): Promise<number> {
  const store = db();
  if (!store) return 0;
  try {
    return await store.queue.where('status').equals('PENDING').count();
  } catch {
    return 0;
  }
}

export async function allQueued(): Promise<QueuedMutation[]> {
  const store = db();
  if (!store) return [];
  try {
    return await store.queue.orderBy('captured_at').reverse().toArray();
  } catch {
    return [];
  }
}

export async function markApplied(
  client_uuid: string,
  document: { number?: string; id?: string },
): Promise<void> {
  const store = db();
  if (!store) return;
  await store.queue.where('client_uuid').equals(client_uuid).modify({
    status: 'APPLIED',
    document_number: document.number,
    document_id: document.id,
  });
}

export async function markRejected(client_uuid: string, reason: string): Promise<void> {
  const store = db();
  if (!store) return;
  await store.queue.where('client_uuid').equals(client_uuid).modify((row) => {
    row.status = 'REJECTED';
    row.exception_reason = reason;
    row.attempts += 1;
  });
}

export async function markAttempted(client_uuid: string, error: string): Promise<void> {
  const store = db();
  if (!store) return;
  // Still PENDING: a network failure is not a refusal, and the row has to be
  // retried rather than shown to somebody as a conflict.
  await store.queue.where('client_uuid').equals(client_uuid).modify((row) => {
    row.attempts += 1;
    row.last_error = error;
  });
}

/** Clear what has landed. Rejected rows stay until somebody has seen them. */
export async function clearApplied(): Promise<void> {
  const store = db();
  if (!store) return;
  try {
    await store.queue.where('status').equals('APPLIED').delete();
  } catch {
    /* nothing to clear */
  }
}

/* -------------------------------------------------------------------------- */
/* Downloaded approved passes (T8.5, §8.3)                                    */
/* -------------------------------------------------------------------------- */

export async function saveReleasable(passes: Omit<ReleasablePass, 'fetched_at'>[]): Promise<void> {
  const store = db();
  if (!store) return;
  const fetched_at = new Date().toISOString();
  try {
    // Replaced wholesale rather than merged: a pass that has since been
    // released, cancelled or expired must *disappear* from the device, and
    // merging would leave it there to be released again.
    await store.releasable.clear();
    await store.releasable.bulkPut(passes.map((pass) => ({ ...pass, fetched_at })));
  } catch {
    /* see above */
  }
}

export async function readReleasable(): Promise<ReleasablePass[]> {
  const store = db();
  if (!store) return [];
  try {
    return await store.releasable.toArray();
  } catch {
    return [];
  }
}

export async function findReleasable(id: number): Promise<ReleasablePass | undefined> {
  const store = db();
  if (!store) return undefined;
  try {
    return await store.releasable.get(id);
  } catch {
    return undefined;
  }
}

/* -------------------------------------------------------------------------- */

/** A uuid, from the platform where it exists and by hand where it does not. */
export function newUuid(): string {
  const source = globalThis.crypto;
  if (source && 'randomUUID' in source) {
    return source.randomUUID();
  }
  // Android WebView on older devices has `crypto` but not `randomUUID`, and
  // this is exactly the device the offline flow exists for.
  const bytes = new Uint8Array(16);
  source.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((byte) => byte.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
