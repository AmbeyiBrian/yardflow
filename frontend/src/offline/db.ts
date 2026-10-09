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

/**
 * What the queue can hold. §8 keeps offline capture to a short list, and
 * §4.17.8 widens it by exactly three finance entries. Approving, paying and
 * closing a float are never queued (§8.3).
 */
export type QueuedOperation =
  | 'GATE_IN'
  | 'GATE_OUT_REQUEST'
  | 'GATE_OUT_RELEASE'
  | 'EXPENSE'
  | 'ALLOWANCE_REQUEST'
  | 'CASUAL';

/** The operations that carry photos, and so have a second step after they land. */
export const FINANCE_OPERATIONS: readonly QueuedOperation[] = [
  'EXPENSE',
  'ALLOWANCE_REQUEST',
  'CASUAL',
];

export type PhotoStatus =
  /** Waiting on its entry to be applied, or on a connection. */
  | 'QUEUED'
  | 'UPLOADED'
  /** The server refused the file itself; retrying would fail forever. */
  | 'FAILED';

/**
 * A photo taken offline for a queued entry (§4.17.8, R6).
 *
 * Held in its own table, not inside the queue row, because the blob is large
 * and the queue is read every few seconds for its count. Its own `client_uuid`
 * is minted at capture and reused on every retry, so a lost response replays to
 * the same attachment instead of a second one.
 */
export interface QueuedPhoto {
  id?: number;
  /** The queue entry this belongs to. */
  queue_client_uuid: string;
  /** Receipt / Fuel pump / Work done / ID / Other. */
  caption: string;
  blob: Blob;
  filename: string;
  client_uuid: string;
  status: PhotoStatus;
  attempts: number;
  last_error?: string;
}

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
  photos!: Table<QueuedPhoto, number>;

  constructor() {
    super('yardflow');
    this.version(1).stores({
      // `client_uuid` unique: capturing the same thing twice locally would
      // otherwise become two documents, which is the failure N2 exists for.
      queue: '++id, &client_uuid, status, operation, captured_at',
      reference: 'key',
      releasable: 'id, number',
    });
    // §4.17.8: version 2 adds only the photos table. Every v1 table is restated
    // unchanged and there is no upgrade callback, so rows already queued on a
    // phone — a delivery captured yesterday and not yet sent — survive it.
    this.version(2).stores({
      queue: '++id, &client_uuid, status, operation, captured_at',
      reference: 'key',
      releasable: 'id, number',
      photos: '++id, queue_client_uuid, &client_uuid, status',
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
  /**
   * Finance entries carry their uuid inside the payload as well (the entity has
   * its own unique `client_uuid`, §4.17.8), so the caller mints it first and
   * passes the same one here. Never regenerated on retry (N2).
   */
  client_uuid: string = newUuid(),
): Promise<string | null> {
  const store = db();
  if (!store) return null;

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

/**
 * Clear what has landed. Rejected rows stay until somebody has seen them.
 *
 * An applied entry whose photos are still queued stays too: the photos are
 * addressed through its `document_id`, so clearing it would strand them on the
 * phone with nowhere to go (§4.17.8).
 */
export async function clearApplied(): Promise<void> {
  const store = db();
  if (!store) return;
  try {
    const applied = await store.queue.where('status').equals('APPLIED').toArray();
    const waiting = new Set(
      (await store.photos.where('status').equals('QUEUED').toArray()).map(
        (photo) => photo.queue_client_uuid,
      ),
    );
    const clear = applied.filter((row) => !waiting.has(row.client_uuid));
    const uuids = clear.map((row) => row.client_uuid);
    await store.queue.where('client_uuid').anyOf(uuids).delete();
    // Uploaded blobs are dead weight once their entry is gone.
    await store.photos.where('queue_client_uuid').anyOf(uuids).delete();
  } catch {
    /* nothing to clear */
  }
}

/* -------------------------------------------------------------------------- */
/* Photos for queued entries (§4.17.8)                                         */
/* -------------------------------------------------------------------------- */

export async function addPhotos(
  queue_client_uuid: string,
  photos: { caption: string; blob: Blob; filename: string }[],
): Promise<void> {
  const store = db();
  if (!store || photos.length === 0) return;
  await store.photos.bulkAdd(
    photos.map((photo) => ({
      ...photo,
      queue_client_uuid,
      client_uuid: newUuid(),
      status: 'QUEUED' as const,
      attempts: 0,
    })),
  );
}

export async function queuedPhotos(): Promise<QueuedPhoto[]> {
  const store = db();
  if (!store) return [];
  try {
    return await store.photos.where('status').equals('QUEUED').toArray();
  } catch {
    return [];
  }
}

export async function markPhoto(
  client_uuid: string,
  change: { status?: PhotoStatus; error?: string },
): Promise<void> {
  const store = db();
  if (!store) return;
  await store.photos.where('client_uuid').equals(client_uuid).modify((row) => {
    if (change.status) row.status = change.status;
    if (change.error !== undefined) {
      row.last_error = change.error;
      row.attempts += 1;
    }
  });
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
