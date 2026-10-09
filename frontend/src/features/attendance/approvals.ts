/**
 * Pure helpers for approving days and the Director's add (design §4.18.5,
 * §4.18.6a, §4.18.8, §4.18.11; R13). No React, so Vitest can reach them.
 */

import type { AddDayBody, WorkDay, WorkDaySlice, WorkSessionCorrection } from './types';

/** §4.18.6a: the Director is whoever holds the role named by `finance_director_role`. */
export function holdsRole(
  roles: readonly { id: number }[] | undefined,
  roleId: number | null | undefined,
): boolean {
  return roleId != null && (roles ?? []).some((r) => r.id === roleId);
}

/**
 * §4.18.11: Team needs a project to manage (or view-all); Everyone needs
 * view-all or the Director role. `all` implies `team`.
 */
export function timeScopes(opts: {
  managesProject: boolean;
  viewAll: boolean;
  isDirector: boolean;
}): ('mine' | 'team' | 'all')[] {
  const all = opts.viewAll || opts.isDirector;
  return [
    'mine',
    ...(opts.managesProject || all ? (['team'] as const) : []),
    ...(all ? (['all'] as const) : []),
  ];
}

/** The Days tab shows for an open request on me, or an OPEN project I manage (§4.18.11). */
export const showDaysTab = (awaitingMe: number, managesOpenProject: boolean) =>
  awaitingMe > 0 || managesOpenProject;

/** My slice is the one I may decide; everyone else's is "with <name>" (§4.18.5). */
export function splitSlices(slices: readonly WorkDaySlice[] | undefined) {
  const all = slices ?? [];
  return {
    mine: all.filter((s) => s.is_mine),
    others: all.filter((s) => !s.is_mine),
  };
}

/** I can decide only while one of my slices is still pending. */
export const canDecide = (day: WorkDay) =>
  splitSlices(day.slices).mine.some((s) => s.status === 'PENDING');

export function sliceLabel(slice: WorkDaySlice): string {
  const scope = slice.project_name ?? 'No project';
  if (slice.is_mine) return `${scope}: yours`;
  const who = slice.approver_name ?? 'someone else';
  if (slice.status === 'PENDING') return `${scope}: waiting on ${who}`;
  return `${scope}: ${slice.status.toLowerCase()} by ${who}`;
}

/** Original beside corrected for one correction (§4.18.6). */
export function correctionPair(
  c: Pick<
    WorkSessionCorrection,
    'original_in_at' | 'original_out_at' | 'corrected_in_at' | 'corrected_out_at'
  >,
  fmt: (iso: string | null) => string,
) {
  return {
    original: `${fmt(c.original_in_at)} – ${fmt(c.original_out_at)}`,
    corrected: `${fmt(c.corrected_in_at)} – ${fmt(c.corrected_out_at)}`,
  };
}

export interface AddDayDraft {
  person: string;
  date: string;
  /** "site:12" or "location:3". */
  place: string;
  project: string;
  start: string;
  end: string;
  reason: string;
}

/** Validates the Director's form; returns the body or a sentence for the banner (§4.18.6a). */
export function buildAddDay(
  d: AddDayDraft,
  selfId: number | undefined,
): { body: AddDayBody } | { error: string } {
  if (!d.person) return { error: 'Choose who the day is for.' };
  if (selfId !== undefined && Number(d.person) === selfId)
    return { error: 'You cannot add a day for yourself.' };
  if (!d.date) return { error: 'Choose the date.' };
  const [kind, id] = d.place.split(':');
  if ((kind !== 'site' && kind !== 'location') || !Number(id))
    return { error: 'Choose where they were.' };
  if (!d.start || !d.end) return { error: 'Give both the start and the end.' };
  if (d.end <= d.start) return { error: 'The end must be after the start.' };
  if (!d.reason.trim()) return { error: 'Say why the day is being added.' };
  const at = (t: string) => new Date(`${d.date}T${t}`).toISOString();
  return {
    body: {
      person: Number(d.person),
      date: d.date,
      place: kind === 'site' ? { site: Number(id) } : { location: Number(id) },
      ...(d.project ? { project: Number(d.project) } : {}),
      start: at(d.start),
      end: at(d.end),
      reason: d.reason.trim(),
    },
  };
}
