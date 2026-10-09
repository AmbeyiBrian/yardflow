/**
 * The pure parts of taking photos in the form, before saving (design §4.17.8;
 * R1, R3, R6).
 *
 * The expense form collects photos first, so that offline there is something to
 * queue and online the count sent as `photos_expected` is what was actually
 * taken. What goes where lives here, away from React and the network, so the
 * rules can be tested directly.
 */

import { ApiError } from '../../api/client';
import type { QueuedFinanceEntry } from '../../offline/financeQueue';
import { postAttachment } from '../../offline/photos';
import type { PhotoDraft, QueuedCasualLine } from './offline';

export const SAVED_ON_PHONE = 'Saved on this phone — it will send when there is network';

/** A draft with the `client_uuid` its upload will carry, minted once so a retry lands once. */
export type UploadItem = PhotoDraft & { client_uuid: string };

export function toUploadItems(drafts: readonly PhotoDraft[], newId: () => string): UploadItem[] {
  return drafts.map((draft) => ({ ...draft, client_uuid: newId() }));
}

/** The send function for `uploadItems`: each item to `/attachments`, on this record. */
export function sendTo(targetType: string, targetId: string | number) {
  return (item: UploadItem) => postAttachment({ targetType, targetId, ...item });
}

/**
 * Send every item, one at a time, and return those that did not go.
 *
 * It carries on past a failure: one bad photo should not strand the rest, and
 * the caller shows exactly which are left (R1).
 */
export async function uploadItems(
  items: readonly UploadItem[],
  send: (item: UploadItem) => Promise<unknown>,
): Promise<UploadItem[]> {
  const failed: UploadItem[] = [];
  for (const item of items) {
    try {
      await send(item);
    } catch {
      failed.push(item);
    }
  }
  return failed;
}

/**
 * A request that never reached the server (no signal, DNS, a dropped
 * connection) — `fetch` throws rather than answering. An `ApiError` is the
 * server speaking, and it is shown, not queued.
 */
export function isNetworkError(error: unknown): boolean {
  // A gateway answering for a server it cannot reach is no answer from the
  // server, and on a site's signal that is the usual failure. Queueing is safe
  // because the entry keeps its client_uuid: if the server did save it, the
  // replay finds that row rather than making a second (§4.17.8).
  if (error instanceof ApiError) return [502, 503, 504].includes(error.status);
  return true;
}

/**
 * Whether the save goes to the phone's queue rather than the server.
 *
 * Also when a casual line names a casual that is itself still queued: the
 * server cannot know it yet, and the queue replays in capture order so the
 * casual lands first (§4.17.8).
 */
export function shouldQueue(online: boolean, lines: readonly QueuedCasualLine[] = []): boolean {
  return !online || lines.some((line) => Boolean(line.casual_client_uuid));
}

/* -------------------------------------------------------------------------- */
/* Casual picker: server casuals plus those queued on this phone               */
/* -------------------------------------------------------------------------- */

export interface CasualOption {
  /** A server id, or `q:<client_uuid>` for a casual still on this phone. */
  value: string;
  label: string;
}

const QUEUED_PREFIX = 'q:';

export function queuedCasualOptions(entries: readonly QueuedFinanceEntry[]): CasualOption[] {
  return entries
    .filter((entry) => entry.kind === 'CASUAL' && entry.state === 'WAITING')
    .map((entry) => ({
      value: `${QUEUED_PREFIX}${entry.client_uuid}`,
      label: `${String(entry.payload.name ?? '')} ${String(entry.payload.id_number ?? '')} (waiting to send)`.trim(),
    }));
}

/** What a picked option sends: a server id, or the queued casual's uuid. */
export function casualRef(value: string): Pick<QueuedCasualLine, 'casual' | 'casual_client_uuid'> {
  return value.startsWith(QUEUED_PREFIX)
    ? { casual_client_uuid: value.slice(QUEUED_PREFIX.length) }
    : { casual: Number(value) };
}

/** The reverse, for filling the picker from a queued payload. */
export function casualValue(line: QueuedCasualLine): string {
  return line.casual_client_uuid
    ? `${QUEUED_PREFIX}${line.casual_client_uuid}`
    : String(line.casual ?? '');
}

/** A casual as the bundle stores it; the ID number is already masked. */
export interface BundleCasual {
  id: number;
  name: string;
  phone?: string;
  id_number: string;
}

/**
 * The bundle's casuals as picker options, narrowed by what was typed (R6).
 *
 * Offline there is no server search, so the same "name or ID" match runs over
 * the stored list. The ID is masked, so only its visible tail can match.
 */
export function bundleCasualOptions(
  rows: readonly BundleCasual[],
  search: string,
): CasualOption[] {
  const needle = search.trim().toLowerCase();
  return rows
    .filter(
      (c) =>
        !needle ||
        c.name.toLowerCase().includes(needle) ||
        c.id_number.toLowerCase().includes(needle) ||
        (c.phone ?? '').includes(needle),
    )
    .map((c) => ({ value: String(c.id), label: `${c.name} ${c.id_number}` }));
}

/** Server rows first, then queued ones, without repeating a value. */
export function mergeCasualOptions(
  ...groups: readonly (readonly CasualOption[])[]
): CasualOption[] {
  const seen = new Map<string, CasualOption>();
  for (const option of groups.flat()) if (!seen.has(option.value)) seen.set(option.value, option);
  return [...seen.values()];
}

/* -------------------------------------------------------------------------- */
/* "Fix and resend": the form, filled from what was queued                     */
/* -------------------------------------------------------------------------- */

const text = (value: unknown, fallback = ''): string =>
  value === undefined || value === null ? fallback : String(value);

export interface ExpensePrefill {
  values: {
    job: string;
    category: string;
    amount: string;
    incurred_on: string;
    description: string;
    scope_of_work: string;
    vehicle: string;
    not_ours: boolean;
    vehicle_reg: string;
    litres: string;
    float_request: string;
  };
  site: string;
  project: string;
  lines: { casual: string; days: string; amount: string }[];
  photos_expected: number;
}

export function expensePrefill(payload: Record<string, unknown>): ExpensePrefill {
  const lines = Array.isArray(payload.casual_lines)
    ? (payload.casual_lines as QueuedCasualLine[])
    : [];
  return {
    values: {
      job: text(payload.job),
      category: text(payload.category),
      amount: text(payload.amount),
      incurred_on: text(payload.incurred_on),
      description: text(payload.description),
      scope_of_work: text(payload.scope_of_work),
      vehicle: text(payload.vehicle),
      not_ours: !payload.vehicle && Boolean(text(payload.vehicle_reg)),
      vehicle_reg: text(payload.vehicle_reg),
      litres: text(payload.litres),
      float_request: text(payload.float_request),
    },
    site: text(payload.site),
    project: text(payload.project),
    lines: lines.map((line) => ({
      casual: casualValue(line),
      days: text(line.days, '1'),
      amount: text(line.amount),
    })),
    photos_expected: Number(payload.photos_expected) || 0,
  };
}

export function allowancePrefill(payload: Record<string, unknown>) {
  return {
    values: {
      type: text(payload.type, 'FLOAT'),
      transport_scope: text(payload.transport_scope),
      from_date: text(payload.from_date),
      to_date: text(payload.to_date),
      amount: text(payload.amount),
      reason: text(payload.reason),
    },
    site: text(payload.site),
    project: text(payload.project),
  };
}
