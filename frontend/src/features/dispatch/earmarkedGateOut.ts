/**
 * Site-first gate-out — the pure parts (T13.8, §4.16.5, §4.16.8a; Q3, Q6).
 *
 * Choosing a site asks the server what is earmarked for it at the chosen store
 * (`GET /stock/earmarked`). This turns that answer into ticked rows, ticked rows
 * into draft lines (without repeating what is already on the request), works
 * out the job question, and words a diversion for the approver. No DOM, no
 * network, so the rules are testable.
 */

import { trimQuantity } from './gateOutBoxes';
import type { GateOutDiversion, GateOutLine } from './types';

export interface EarmarkedLine {
  item_type: number;
  item_name: string;
  tracking_mode: 'BULK' | 'SERIALIZED' | 'REEL';
  uom: string;
  owner_type: 'OWN' | 'CLIENT';
  owner_client: number | null;
  condition: string;
  requested_qty: string | number;
  units: { serial_unit: number; serial_number: string }[];
  reels: { reel: number; drum_number: string; length: string | number }[];
}

export interface EarmarkedExclusion {
  kind: 'unit' | 'drum';
  item_name: string;
  serial_number?: string;
  drum_number?: string;
  reason?: string;
  message: string;
}

export interface EarmarkedJob {
  id: number;
  reference: string;
  description?: string;
  project: number | null;
  project_name: string;
}

export interface EarmarkedResult {
  lines: EarmarkedLine[];
  excluded: EarmarkedExclusion[];
  jobs: EarmarkedJob[];
}

/** One tickable row: a unit, a drum, or a bulk quantity. */
export interface EarmarkedRow {
  key: string;
  kind: 'unit' | 'drum' | 'bulk';
  lineIndex: number;
  /** "RRU-1", "DRM-3 (500 m)", or "Jumper". */
  label: string;
  /** Bulk only: what is earmarked, as a string. */
  quantity?: string;
}

export function earmarkedRows(lines: EarmarkedLine[]): EarmarkedRow[] {
  const rows: EarmarkedRow[] = [];
  lines.forEach((line, lineIndex) => {
    if (line.tracking_mode === 'SERIALIZED') {
      for (const unit of line.units) {
        rows.push({
          key: `u:${unit.serial_unit}`,
          kind: 'unit',
          lineIndex,
          label: unit.serial_number,
        });
      }
    } else if (line.tracking_mode === 'REEL') {
      for (const reel of line.reels) {
        rows.push({
          key: `d:${reel.reel}`,
          kind: 'drum',
          lineIndex,
          label: `${reel.drum_number} (${trimQuantity(reel.length)} ${line.uom})`,
        });
      }
    } else {
      rows.push({
        key: `b:${lineIndex}`,
        kind: 'bulk',
        lineIndex,
        label: line.item_name,
        quantity: trimQuantity(line.requested_qty),
      });
    }
  });
  return rows;
}

/** Everything is ticked to begin with (Q6). */
export function allTicked(lines: EarmarkedLine[]): Set<string> {
  return new Set(earmarkedRows(lines).map((row) => row.key));
}

/** A bulk quantity may be lowered to take part of it, never raised. */
export function earmarkedBulkProblem(value: string, earmarked: string, uom: string): string | null {
  const wanted = Number(value);
  if (!value.trim() || !Number.isFinite(wanted) || wanted <= 0) return 'Say how much is going.';
  if (wanted > Number(earmarked)) {
    return `Only ${trimQuantity(earmarked)} ${uom} is earmarked. Take that much or less.`;
  }
  return null;
}

/** Same lot as an existing bulk line: same item, owner and condition. */
function sameLot(a: GateOutLine, b: EarmarkedLine): boolean {
  return (
    a.tracking_mode === 'BULK' &&
    a.item_type === b.item_type &&
    (a.owner_client ?? null) === (b.owner_client ?? null) &&
    (a.condition ?? 'NEW') === b.condition
  );
}

/**
 * Ticked rows → draft lines. A unit already on the request (by serial unit), a
 * drum already on it (by reel) and a bulk lot already on it (by item, owner and
 * condition) are not added again; `skipped` counts them.
 *
 * `quantities` holds what was typed for bulk rows, by row key.
 */
export function earmarkedToLines(
  earmarked: EarmarkedLine[],
  ticked: ReadonlySet<string>,
  quantities: Record<string, string>,
  existing: GateOutLine[],
): { lines: GateOutLine[]; skipped: number } {
  const haveUnits = new Set<number>();
  const haveReels = new Set<number>();
  for (const line of existing) {
    for (const serial of line.serials ?? []) haveUnits.add(serial.serial_unit);
    for (const reel of line.reels ?? []) haveReels.add(reel.reel);
  }
  const lines: GateOutLine[] = [];
  let skipped = 0;

  const base = (line: EarmarkedLine) => ({
    item_type: line.item_type,
    item_name: line.item_name,
    uom: line.uom,
    owner_type: line.owner_type,
    owner_client: line.owner_client,
    condition: line.condition,
    no_serial_reason: '',
    is_returnable: false,
    expected_return_date: null,
    divert_reason: '',
    box: null,
    box_code: null,
    box_path: [],
  });

  earmarked.forEach((line, lineIndex) => {
    if (line.tracking_mode === 'SERIALIZED') {
      const serials = [];
      for (const unit of line.units) {
        if (!ticked.has(`u:${unit.serial_unit}`)) continue;
        if (haveUnits.has(unit.serial_unit)) {
          skipped += 1;
          continue;
        }
        serials.push({ serial_unit: unit.serial_unit, serial_number: unit.serial_number });
      }
      if (serials.length) {
        lines.push({
          ...base(line),
          tracking_mode: 'SERIALIZED',
          requested_qty: String(serials.length),
          serials,
          reels: [],
        });
      }
    } else if (line.tracking_mode === 'REEL') {
      const reels = [];
      let total = 0;
      for (const reel of line.reels) {
        if (!ticked.has(`d:${reel.reel}`)) continue;
        if (haveReels.has(reel.reel)) {
          skipped += 1;
          continue;
        }
        total += Number(reel.length);
        reels.push({
          reel: reel.reel,
          drum_number: reel.drum_number,
          length_requested: trimQuantity(reel.length),
        });
      }
      if (reels.length) {
        lines.push({
          ...base(line),
          tracking_mode: 'REEL',
          requested_qty: trimQuantity(String(total)),
          serials: [],
          reels,
        });
      }
    } else {
      const key = `b:${lineIndex}`;
      if (!ticked.has(key)) return;
      if (existing.some((have) => sameLot(have, line))) {
        skipped += 1;
        return;
      }
      lines.push({
        ...base(line),
        tracking_mode: 'BULK',
        requested_qty: trimQuantity(quantities[key] ?? line.requested_qty),
        serials: [],
        reels: [],
      });
    }
  });
  return { lines, skipped };
}

/* ----------------------------------------------------------------------------
 * The job question (Q6)
 * -------------------------------------------------------------------------- */

export interface JobGroup {
  project: number | null;
  label: string;
  jobs: EarmarkedJob[];
}

/** Jobs grouped by project, in order of first appearance; no project last. */
export function groupJobsByProject(jobs: EarmarkedJob[]): JobGroup[] {
  const groups = new Map<number | null, JobGroup>();
  for (const job of jobs) {
    const group = groups.get(job.project) ?? {
      project: job.project,
      label: job.project === null ? 'No project' : job.project_name || `Project ${job.project}`,
      jobs: [],
    };
    group.jobs.push(job);
    groups.set(job.project, group);
  }
  const ordered = [...groups.values()];
  return [...ordered.filter((g) => g.project !== null), ...ordered.filter((g) => g.project === null)];
}

export type JobRule =
  | { kind: 'none' }
  | { kind: 'single'; job: EarmarkedJob }
  /** More than one project: the pass has to say which. */
  | { kind: 'required' }
  /** Several jobs, one project: naming one is optional, as it always was. */
  | { kind: 'optional' };

export function jobRule(jobs: EarmarkedJob[]): JobRule {
  if (jobs.length === 0) return { kind: 'none' };
  if (jobs.length === 1) return { kind: 'single', job: jobs[0] };
  const projects = new Set(jobs.map((job) => job.project));
  return projects.size > 1 ? { kind: 'required' } : { kind: 'optional' };
}

/** Whether the pass is missing a job it must have. */
export function jobMissing(rule: JobRule, chosen: string): boolean {
  return rule.kind === 'required' && !chosen;
}

/* ----------------------------------------------------------------------------
 * Diversions (Q3)
 * -------------------------------------------------------------------------- */

/** "Diverted from Site A: 1 (RRU-1) — plan changed". */
export function diversionText(diversion: GateOutDiversion, reason: string | undefined): string {
  const names = diversion.serials?.length
    ? diversion.serials
    : diversion.drums?.length
      ? diversion.drums
      : [];
  const what = `${trimQuantity(diversion.quantity)}${names.length ? ` (${names.join(', ')})` : ''}`;
  const why = reason?.trim() ? reason.trim() : 'no reason given';
  return `Diverted from ${diversion.site_name}: ${what} — ${why}`;
}

/** `lines.{i}.divert_reason` field errors → message by line index. */
export function divertErrors(
  fieldErrors: Record<string, string[]> | undefined,
): Record<number, string> {
  const found: Record<number, string> = {};
  for (const [key, messages] of Object.entries(fieldErrors ?? {})) {
    const match = /^lines\.(\d+)\.divert_reason$/.exec(key);
    if (match && messages.length) found[Number(match[1])] = messages.join(' ');
  }
  return found;
}
