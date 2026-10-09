/**
 * The pure parts of offline clock-in (design §4.18.9; R6, R13).
 *
 * No IndexedDB and no network here, so the rules that matter can be tested
 * directly: what the queued payload carries, when the phone refuses early, and
 * which session the card shows while captures are still waiting to send.
 */

import type { QueuedMutation } from '../../offline/db';
import { checkArea } from './area';
import { formatDistance } from './nearby';
import { PLAIN } from './rules';
import type { ClockInBody, ClockOutBody, ClockPlace, Fix, WorkSession } from './types';

/** The cap used when no bundle has ever been fetched (the server default, §4.18.3). */
export const DEFAULT_ACCURACY_CAP_M = 100;

/** The area the phone checked against, sent so the server can tell AREA_CHANGED (§4.18.9). */
export interface PlaceArea {
  lat: number;
  lng: number;
  radius: number;
}

export function placeArea(place: Pick<ClockPlace, 'lat' | 'lng' | 'radius_m'>): PlaceArea {
  return { lat: place.lat, lng: place.lng, radius: place.radius_m };
}

export type QueuedClockInBody = ClockInBody & { place_area: PlaceArea };
export type QueuedClockOutBody = ClockOutBody & { place_area?: PlaceArea };

export function clockInPayload(args: {
  place: ClockPlace;
  project?: number | null;
  fix: Fix;
  client_uuid: string;
}): QueuedClockInBody {
  const { place, project, fix, client_uuid } = args;
  return {
    ...(place.kind === 'site' ? { site: place.id } : { location: place.id }),
    ...(project ? { project } : {}),
    fix,
    client_uuid,
    place_area: placeArea(place),
  };
}

/**
 * A clock-out is never refused on position (R13), so `fix` may be null and
 * `place_area` is only there when the phone knows where the session is.
 */
export function clockOutPayload(args: {
  session: LocalSession;
  fix: Fix | null;
  client_uuid: string;
}): QueuedClockOutBody {
  const { session, fix, client_uuid } = args;
  return {
    fix,
    session_client_uuid: session.session_client_uuid,
    client_uuid,
    ...(session.place_area ? { place_area: session.place_area } : {}),
  };
}

/**
 * The phone's own area check before it queues a clock-in (§4.18.4, §4.18.9).
 * Returns the sentence to show, in the server's words, or null when it passes.
 * Not a gate on the server: the replay checks again against the place as it
 * was at `captured_at`.
 */
export function earlyRefusal(
  fix: Fix | null,
  place: ClockPlace,
  capM: number = DEFAULT_ACCURACY_CAP_M,
): string | null {
  const result = checkArea(fix, { lat: place.lat, lng: place.lng, radius_m: place.radius_m }, capM);
  if (result.problem === 'NO_FIX') return PLAIN.CLOCK_LOCATION_REQUIRED;
  if (result.problem === 'TOO_VAGUE') return PLAIN.CLOCK_LOCATION_TOO_VAGUE;
  if (!result.inside && result.distance_m !== null) {
    return `You are ${formatDistance(result.distance_m)} from ${place.name} (limit ${formatDistance(place.radius_m)}).`;
  }
  return null;
}

/* -------------------------------------------------------------------------- */
/* The open session kept on the phone                                          */
/* -------------------------------------------------------------------------- */

/**
 * The open session as the phone knows it: the reference row that lets the card
 * say "clocked in" and Clock out work with no signal (§4.18.9). Its
 * `session_client_uuid` is what a clock-out names.
 */
export interface LocalSession {
  session_client_uuid: string;
  place_name: string;
  clock_in_at: string;
  project_name?: string | null;
  place_area: PlaceArea | null;
  /** Captured on the phone and not yet on the server. */
  queued: boolean;
}

export function sessionFromClockIn(args: {
  place: ClockPlace;
  project_name?: string | null;
  client_uuid: string;
  clock_in_at: string;
}): LocalSession {
  return {
    session_client_uuid: args.client_uuid,
    place_name: args.place.name,
    clock_in_at: args.clock_in_at,
    project_name: args.project_name ?? null,
    place_area: placeArea(args.place),
    queued: true,
  };
}

/**
 * A session the server reported, in the phone's shape. The server names it by
 * id until it exposes its `in_client_uuid`; the area is looked up among the
 * places the phone holds, and is null when that place is not in the cache.
 */
export function sessionFromServer(
  session: WorkSession & { in_client_uuid?: string },
  places: readonly ClockPlace[] = [],
): LocalSession {
  const place = places.find((p) =>
    session.site != null ? p.kind === 'site' && p.id === session.site : p.kind === 'location' && p.id === session.location,
  );
  return {
    session_client_uuid: session.in_client_uuid ?? String(session.id),
    place_name: session.place_name,
    clock_in_at: session.clock_in_at,
    project_name: session.project_name ?? null,
    place_area: place ? placeArea(place) : null,
    queued: false,
  };
}

/** Attendance rows the phone still has to send. */
export function pendingAttendance(rows: readonly QueuedMutation[]): QueuedMutation[] {
  return rows.filter(
    (row) => (row.operation === 'CLOCK_IN' || row.operation === 'CLOCK_OUT') && row.status === 'PENDING',
  );
}

/**
 * Which session the card shows.
 *
 * While anything is waiting to send, the local row wins: it already reflects
 * every queued clock-in and clock-out, and the server cannot yet. Otherwise the
 * server's answer wins, and `undefined` (no answer: offline or paused) falls
 * back to the local row. `null` from the server means "not clocked in".
 */
export function resolveSession(args: {
  server: LocalSession | null | undefined;
  local: LocalSession | null;
  waiting: boolean;
}): LocalSession | null {
  const { server, local, waiting } = args;
  if (waiting) return local;
  if (server === undefined) return local;
  return server;
}

/* -------------------------------------------------------------------------- */
/* Plain words for the Sync screen                                             */
/* -------------------------------------------------------------------------- */

function timeOfDay(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

/** "Clock-in 08:12" — what an entry is, before its state. */
export function describeAttendance(row: Pick<QueuedMutation, 'operation' | 'captured_at'>): string {
  const what = row.operation === 'CLOCK_IN' ? 'Clock-in' : 'Clock-out';
  return `${what} ${timeOfDay(row.captured_at)}`;
}

export interface QueuedAttendanceEntry {
  client_uuid: string;
  kind: 'CLOCK_IN' | 'CLOCK_OUT';
  summary: string;
  state: 'WAITING' | 'REFUSED';
  /** The server's code and reason, when refused (R6). */
  reason?: string;
  captured_at: string;
}

/** Entries a person still has to do something about: waiting, or refused (R6). */
export function attendanceEntries(rows: readonly QueuedMutation[]): QueuedAttendanceEntry[] {
  return [...rows]
    .sort((a, b) => a.captured_at.localeCompare(b.captured_at) || (a.id ?? 0) - (b.id ?? 0))
    .filter(
      (row) =>
        (row.operation === 'CLOCK_IN' || row.operation === 'CLOCK_OUT') &&
        (row.status === 'PENDING' || row.status === 'REJECTED'),
    )
    .map((row) => ({
      client_uuid: row.client_uuid,
      kind: row.operation as 'CLOCK_IN' | 'CLOCK_OUT',
      summary: describeAttendance(row),
      state: row.status === 'REJECTED' ? ('REFUSED' as const) : ('WAITING' as const),
      reason: row.exception_reason,
      captured_at: row.captured_at,
    }));
}
