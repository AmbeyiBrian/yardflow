/**
 * The pure parts of offline finance capture (design §4.17.8, §8; R6).
 *
 * Kept free of IndexedDB and the network so the two rules that matter can be
 * tested directly: the queue replays in capture order, and a photo goes up only
 * after the entry it belongs to has landed.
 */

import type { QueuedMutation, QueuedOperation, QueuedPhoto } from './db';

/**
 * Oldest capture first, ties broken by insertion order.
 *
 * §4.17.8: an expense may name a casual registered offline in the same queue
 * (`casual_client_uuid`), and the server resolves that reference while it
 * replays. A casual that arrived *after* the expense naming it would be refused
 * as unknown, so order is a correctness property, not a nicety. Dexie's index
 * order is by status then primary key; this makes the intent explicit instead of
 * depending on it.
 */
export function inCaptureOrder<T extends Pick<QueuedMutation, 'captured_at' | 'id'>>(
  rows: readonly T[],
): T[] {
  return [...rows].sort(
    (a, b) => a.captured_at.localeCompare(b.captured_at) || (a.id ?? 0) - (b.id ?? 0),
  );
}

/** The attachment target for an operation's photos, or null if it has none. */
export function photoTarget(operation: QueuedOperation): string | null {
  switch (operation) {
    case 'EXPENSE':
      return 'commercials.ProjectExpense';
    case 'CASUAL':
      return 'commercials.Casual';
    default:
      // An allowance request carries no evidence (§4.17.2).
      return null;
  }
}

export interface PhotoUpload {
  photo: QueuedPhoto;
  targetType: string;
  targetId: string;
}

/**
 * Which queued photos may be sent now.
 *
 * Only those whose entry is APPLIED and has a `document_id`: until then there is
 * nothing for the attachment to hang off, and a REJECTED entry's photos are held
 * on the phone with it (R6) rather than uploaded to nowhere. Photos go in
 * capture order of their entries.
 */
export function photosReadyToUpload(
  queue: readonly QueuedMutation[],
  photos: readonly QueuedPhoto[],
): PhotoUpload[] {
  const byUuid = new Map(queue.map((row) => [row.client_uuid, row]));
  const ready: PhotoUpload[] = [];
  for (const photo of photos) {
    if (photo.status !== 'QUEUED') continue;
    const entry = byUuid.get(photo.queue_client_uuid);
    if (!entry || entry.status !== 'APPLIED' || !entry.document_id) continue;
    const targetType = photoTarget(entry.operation);
    if (!targetType) continue;
    ready.push({ photo, targetType, targetId: entry.document_id });
  }
  const captured = (upload: PhotoUpload) => byUuid.get(upload.photo.queue_client_uuid)?.captured_at ?? '';
  return ready.sort(
    (a, b) => captured(a).localeCompare(captured(b)) || (a.photo.id ?? 0) - (b.photo.id ?? 0),
  );
}

/**
 * A refusal of the file itself, which no number of retries will fix (too large,
 * wrong type). Anything else — a dropped connection, a 5xx, an expired token —
 * stays queued, because losing a receipt photo to a bad minute is worse than
 * retrying one.
 */
export function isPermanentUploadFailure(status: number): boolean {
  return status === 400 || status === 413 || status === 415 || status === 422;
}

/* -------------------------------------------------------------------------- */
/* Plain words for the Sync screen and the money lists                         */
/* -------------------------------------------------------------------------- */

export const ALLOWANCE_WORDS: Record<string, string> = {
  FLOAT: 'Float',
  TRANSPORT: 'Transport allowance',
  NIGHT_OUT: 'Night-out allowance',
  TEAM_ALLOWANCE: 'Team allowance',
  OTHER: 'Allowance',
};

function kes(amount: unknown): string {
  const value = Number(amount);
  if (amount === undefined || amount === null || amount === '' || Number.isNaN(value)) {
    return '';
  }
  return `KES ${value.toLocaleString('en-KE', { maximumFractionDigits: 2 })}`;
}

/** "Expense KES 1,200", "Casual Jane Wanjiru" — what an entry is, before its state. */
export function describeEntry(operation: QueuedOperation, payload: unknown): string {
  const body = (payload ?? {}) as Record<string, unknown>;
  switch (operation) {
    case 'EXPENSE':
      return `Expense ${kes(body.amount)}`.trim();
    case 'ALLOWANCE_REQUEST':
      return `${ALLOWANCE_WORDS[String(body.type)] ?? 'Allowance'} ${kes(body.amount)}`.trim();
    case 'CASUAL':
      return `Casual ${String(body.name ?? '')}`.trim();
    default:
      return operation;
  }
}

export type FinanceKind = 'EXPENSE' | 'ALLOWANCE_REQUEST' | 'CASUAL';

export interface QueuedFinanceEntry {
  client_uuid: string;
  kind: FinanceKind;
  summary: string;
  state: 'WAITING' | 'REFUSED';
  /** The server's code and reason, when refused (R6). */
  reason?: string;
  captured_at: string;
  /** What was captured, so "Fix and resend" can start from it. */
  payload: Record<string, unknown>;
  photos: number;
}

/**
 * The finance entries a person still has to do something about: waiting to go,
 * or refused. APPLIED ones are not listed — they are real records by then. A
 * refused entry that has already been corrected and resent is hidden, so the
 * list does not offer to fix the same mistake twice.
 */
export function financeEntries(
  rows: readonly QueuedMutation[],
  photos: readonly QueuedPhoto[],
): QueuedFinanceEntry[] {
  const superseded = new Set(
    rows
      .map((row) => (row.payload as { supersedes_client_uuid?: string } | null)?.supersedes_client_uuid)
      .filter((uuid): uuid is string => Boolean(uuid)),
  );
  const photoCount = new Map<string, number>();
  for (const photo of photos) {
    photoCount.set(photo.queue_client_uuid, (photoCount.get(photo.queue_client_uuid) ?? 0) + 1);
  }

  return inCaptureOrder(rows)
    .filter(
      (row) =>
        (row.operation === 'EXPENSE' ||
          row.operation === 'ALLOWANCE_REQUEST' ||
          row.operation === 'CASUAL') &&
        (row.status === 'PENDING' || row.status === 'REJECTED') &&
        !superseded.has(row.client_uuid),
    )
    .map((row) => ({
      client_uuid: row.client_uuid,
      kind: row.operation as FinanceKind,
      summary: describeEntry(row.operation, row.payload),
      state: row.status === 'REJECTED' ? ('REFUSED' as const) : ('WAITING' as const),
      reason: row.exception_reason,
      captured_at: row.captured_at,
      payload: (row.payload ?? {}) as Record<string, unknown>,
      photos: photoCount.get(row.client_uuid) ?? 0,
    }));
}
