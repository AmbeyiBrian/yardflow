/**
 * Offline capture for clock-in and clock-out (design §4.18.9; R6, R13).
 *
 * The card calls these when there is no signal or the server could not be
 * reached; the queue and the replay live in `offline/`, in the pattern of
 * `features/money/offline.ts`. Approving and correcting a day are never
 * offline (§8.3), so there is deliberately no function for them.
 *
 * Each capture's `client_uuid` is minted by the card once and travels twice:
 * as the queue row's idempotency key (N2) and inside the payload, where it
 * becomes the session's own `in_client_uuid` / `out_client_uuid`. A clock-in's
 * uuid is also the `session_client_uuid` the later clock-out names, which is
 * how a clock-out captured before its clock-in has landed still finds it.
 *
 * The open session is kept as a local reference row (`open_session`), so the
 * card says "clocked in" and Clock out works with no network. The capture time
 * is the phone's, in `captured_at`; the server compares it with `area_history`
 * and flags `AREA_CHANGED` and `SENT_LATE` itself.
 */

import { liveQuery } from 'dexie';
import { useEffect, useState } from 'react';

import { db, enqueue, newUuid, readReference, saveReference } from '../../offline/db';
import {
  DEFAULT_ACCURACY_CAP_M,
  attendanceEntries,
  clockInPayload,
  clockOutPayload,
  pendingAttendance,
  sessionFromClockIn,
  type LocalSession,
  type QueuedAttendanceEntry,
} from './queued';
import type { ClockPlace, Fix } from './types';

export type { LocalSession, QueuedAttendanceEntry } from './queued';

const NO_STORAGE =
  'This browser cannot store anything offline, and there is no connection. ' +
  'Nothing has been saved.';

const OPEN_SESSION = 'open_session';

/** Keep what the phone knows about the open session (null clears it). */
export async function saveOpenSession(session: LocalSession | null): Promise<void> {
  await saveReference(OPEN_SESSION, session ? [session] : []);
}

export async function readOpenSession(): Promise<LocalSession | null> {
  const { rows } = await readReference<LocalSession>(OPEN_SESSION);
  return rows[0] ?? null;
}

/**
 * Queue a clock-in. The caller has already checked the area with `earlyRefusal`.
 * Returns its `client_uuid`.
 */
export async function queueClockIn(
  args: {
    place: ClockPlace;
    project?: number | null;
    project_name?: string | null;
    fix: Fix;
  },
  client_uuid: string = newUuid(),
): Promise<string> {
  const payload = clockInPayload({ ...args, client_uuid });
  const saved = await enqueue('CLOCK_IN', payload, client_uuid);
  // No IndexedDB: fail loudly rather than pretend something was saved.
  if (!saved) throw new Error(NO_STORAGE);
  await saveOpenSession(
    sessionFromClockIn({
      place: args.place,
      project_name: args.project_name,
      client_uuid,
      clock_in_at: new Date().toISOString(),
    }),
  );
  return saved;
}

/**
 * Queue a clock-out. Never refused on position (R13): `fix` may be null.
 * Returns its `client_uuid`.
 */
export async function queueClockOut(
  args: { session: LocalSession; fix: Fix | null },
  client_uuid: string = newUuid(),
): Promise<string> {
  const saved = await enqueue('CLOCK_OUT', clockOutPayload({ ...args, client_uuid }), client_uuid);
  if (!saved) throw new Error(NO_STORAGE);
  await saveOpenSession(null);
  return saved;
}

export interface QueuedAttendance {
  /** Waiting to send or refused, oldest first. */
  entries: QueuedAttendanceEntry[];
  /** Something is waiting to send, so the local session is the truth. */
  waiting: boolean;
  /** The open session as the phone holds it. */
  local: LocalSession | null;
  /** False until the first read of IndexedDB, so the card does not flash "clocked out". */
  ready: boolean;
}

/** Attendance entries and the local open session, live. Empty where IndexedDB is unavailable. */
export function useQueuedAttendance(): QueuedAttendance {
  const [value, setValue] = useState<QueuedAttendance>(() => ({
    entries: [],
    waiting: false,
    local: null,
    // Nothing to wait for where IndexedDB is unavailable.
    ready: db() === null,
  }));

  useEffect(() => {
    const store = db();
    if (!store) return;
    const subscription = liveQuery(async () => {
      const rows = await store.queue.toArray();
      const reference = await store.reference.get(OPEN_SESSION);
      return {
        entries: attendanceEntries(rows),
        waiting: pendingAttendance(rows).length > 0,
        local: ((reference?.rows as LocalSession[] | undefined) ?? [])[0] ?? null,
        ready: true,
      };
    }).subscribe({
      next: setValue,
      // A blocked store is "nothing to show", not a broken page.
      error: () => setValue({ entries: [], waiting: false, local: null, ready: true }),
    });
    return () => subscription.unsubscribe();
  }, []);

  return value;
}

/** The cached area-check settings from the last bundle (§4.18.3), or the defaults. */
export async function readAttendanceSettings(): Promise<{ accuracy_cap_m: number; auto_close_hour: number }> {
  const { rows } = await readReference<{ accuracy_cap_m?: number; auto_close_hour?: number }>('attendance');
  const row = rows[0] ?? {};
  return {
    accuracy_cap_m: row.accuracy_cap_m ?? DEFAULT_ACCURACY_CAP_M,
    auto_close_hour: row.auto_close_hour ?? 18,
  };
}
