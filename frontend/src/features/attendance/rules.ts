/**
 * Wording for clock-in (design §4.18.4–§4.18.6, §4.18.12; R13).
 */

import { ApiError } from '../../api/client';
import { formatDistance } from './nearby';
import type { SessionFlag } from './types';

export const FLAG_LABELS: Record<SessionFlag, string> = {
  OUTSIDE_AT_CLOCK_OUT: 'Clocked out away from the place',
  NO_POSITION_AT_CLOCK_OUT: 'No position at clock-out',
  CLOSED_AUTOMATICALLY: 'Closed automatically',
  SENT_LATE: 'Sent late',
  AREA_CHANGED: 'Area changed',
  CORRECTED: 'Corrected',
  ADDED_BY_DIRECTOR: 'Added by the Director',
};

const FLAG_HINTS: Partial<Record<SessionFlag, string>> = {
  CLOSED_AUTOMATICALLY: 'Nobody clocked out, so the system closed it at the end of the day.',
  SENT_LATE: 'This reached the server more than an hour after it happened.',
  AREA_CHANGED: 'The place moved while this was waiting to send; your phone used the area it had.',
  ADDED_BY_DIRECTOR: 'Recorded by the Director, with no position.',
};

export const flagLabel = (flag: SessionFlag) => FLAG_LABELS[flag] ?? flag;
export const flagHint = (flag: SessionFlag) => FLAG_HINTS[flag];

export const PLAIN: Record<string, string> = {
  CLOCK_LOCATION_REQUIRED: 'Turn location on to clock in.',
  CLOCK_LOCATION_TOO_VAGUE:
    'Your phone could not fix its position closely enough. Step outside or wait a moment, then try again.',
  CLOCK_PLACE_REQUIRED: 'Choose where you are clocking in.',
  PLACE_NOT_AVAILABLE: 'You cannot clock in at that place any more. Choose another.',
  CLOCK_OVERLAP: 'That overlaps another of your sessions.',
  CLOCK_NOT_CLOCKED_IN: 'You are not clocked in.',
  CLOCK_SESSION_LOCKED: 'That session has been decided and cannot change.',
  CORRECTION_NOT_ALLOWED: 'Only a rejected day can be corrected, and only for 30 days.',
  CORRECTION_REASON_REQUIRED: 'Say why the time is being corrected.',
  PROJECT_AMBIGUOUS: 'This site is on more than one open project. Choose which one this is for.',
};

function num(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
}

/** "You are 640 m from Karen Mast (limit 200 m)" from CLOCK_OUTSIDE_AREA's details. */
export function outsideAreaMessage(error: ApiError): string {
  const d = error.details;
  const distance = num(d.distance_m);
  const radius = num(d.radius_m);
  const place = typeof d.place === 'string' ? d.place : typeof d.place_name === 'string' ? d.place_name : null;
  if (distance === undefined) return error.message;
  return `You are ${formatDistance(distance)} from ${place ?? 'that place'}${
    radius !== undefined ? ` (limit ${formatDistance(radius)})` : ''
  }.`;
}

/** The banner text for a refused clock-in, correction or clock-out. */
export function clockError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === 'CLOCK_OUTSIDE_AREA') return outsideAreaMessage(error);
    if (error.code === 'PLACE_HAS_NO_COORDINATES') return error.message;
    if (PLAIN[error.code]) return PLAIN[error.code];
    return error.message;
  }
  return error instanceof Error ? error.message : 'Something went wrong.';
}

/** Hours as "7.5 h". */
export function hoursLabel(hours: string | number | null | undefined): string {
  if (hours === null || hours === undefined || hours === '') return '—';
  const n = Number(hours);
  return Number.isFinite(n) ? `${Math.round(n * 100) / 100} h` : String(hours);
}

export function dayStatusLabel(status: string): string {
  return status.charAt(0) + status.slice(1).toLowerCase();
}
