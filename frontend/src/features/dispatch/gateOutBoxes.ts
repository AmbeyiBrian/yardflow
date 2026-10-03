/**
 * Gate-out by box — the pure parts (T11.15, §4.15.7; P5, P6, P9, P10).
 *
 * Scanning a box asks the server what could leave from the chosen location
 * (`GET /stock/boxes/{code}/issuable`). This module turns that answer into
 * draft lines, words the exclusions, groups lines under their box, and finds
 * which draft units a refused submit complained about. No DOM, no network, so
 * the rules are testable.
 */

import type { GateOutLine, GateOutLineSerial } from './types';

/** The separator for a box chain, outermost first: "PAL-7 › CTN-1". */
export const BOX_SEPARATOR = ' › ';

/** What `/stock/lookup` returns for a box (a carton or a pallet). */
export interface LookedUpBox {
  id: number;
  code: string;
  status: string;
  node: number;
  node_label: string;
  units_now: number;
  bulk_lines_now: number;
  parent_code: string | null;
}

/** One line the server proposes: one per (innermost box, item, owner, condition). */
export interface IssuableLine {
  item_type: number;
  item_name: string;
  tracking_mode: 'BULK' | 'SERIALIZED' | 'REEL';
  uom: string;
  owner_type: 'OWN' | 'CLIENT';
  owner_client: number | null;
  condition: string;
  /** A decimal as the API sends it (a string), or a number for a unit count. */
  requested_qty: string | number;
  units: { serial_unit: number; serial_number: string }[];
  box: number;
  box_code: string;
  box_path: string[];
}

export interface IssuableExclusion {
  kind: 'unit' | 'bulk';
  serial_unit: number | null;
  serial_number: string;
  item_type: number;
  item_name: string;
  box: number;
  box_code: string;
  reason: string;
  message: string;
}

export interface IssuableResult {
  lines: IssuableLine[];
  excluded: IssuableExclusion[];
}

/** A number a person would say out loud: 5, not 5.000. */
export function trimQuantity(quantity: string | number): string {
  const text = String(quantity);
  return text.includes('.') ? text.replace(/0+$/, '').replace(/\.$/, '') : text;
}

/** "PAL-7 › CTN-1", or the box's own code when the API gave no chain. */
export function boxHeading(
  line: Pick<GateOutLine, 'box' | 'box_code' | 'box_path'>,
): string | null {
  if (line.box_path?.length) return line.box_path.join(BOX_SEPARATOR);
  if (line.box_code) return line.box_code;
  return line.box ? `Box ${line.box}` : null;
}

/**
 * "CTN-1 (in PAL-7): 7 units and 1 bulk line can go" — what the proposal
 * panel says before anything is added (P6).
 */
export function proposalSummary(box: LookedUpBox, result: IssuableResult): string {
  const where = box.parent_code ? `${box.code} (in ${box.parent_code})` : box.code;
  const units = result.lines.reduce((sum, line) => sum + line.units.length, 0);
  const bulk = result.lines.filter((line) => line.tracking_mode !== 'SERIALIZED').length;
  const parts: string[] = [];
  if (units) parts.push(`${units} ${units === 1 ? 'unit' : 'units'}`);
  if (bulk) parts.push(`${bulk} bulk ${bulk === 1 ? 'line' : 'lines'}`);
  if (parts.length === 0) return `Nothing in ${where} can go from here.`;
  return `${where}: ${parts.join(' and ')} can go`;
}

/** One exclusion in words: the server's message, with a fallback by reason. */
export function exclusionText(excluded: IssuableExclusion): string {
  if (excluded.message) return excluded.message;
  const who = excluded.serial_number || excluded.item_name;
  switch (excluded.reason) {
    case 'NOT_HERE':
      return `${who} is not at this location.`;
    case 'QUARANTINED':
      return `${who} is quarantined.`;
    case 'HELD_BY_PERSON':
      return `${who} is held by a person.`;
    case 'ON_ANOTHER_PASS':
      return `${who} is on another gate pass that is still open.`;
    default:
      return `${who} is not in stock.`;
  }
}

/** A bulk proposal line's quantity must stay within what the box holds (P9). */
export function bulkQuantityProblem(
  value: string,
  claim: string | number,
  uom: string,
): string | null {
  const wanted = Number(value);
  if (!value.trim() || !Number.isFinite(wanted) || wanted <= 0) {
    return 'Say how much is going.';
  }
  if (wanted > Number(claim)) {
    return `The box holds ${trimQuantity(claim)} ${uom}. Take that much or less.`;
  }
  return null;
}

/**
 * Proposal → draft lines. `quantities` holds what the person typed for bulk
 * lines, by position in the proposal (P9: lowering it takes part of the box).
 * A serialized line's quantity is always the number of units it names.
 */
export function proposalToLines(
  proposed: IssuableLine[],
  quantities: Record<number, string> = {},
): GateOutLine[] {
  return proposed.map((line, index) => {
    const serialized = line.tracking_mode === 'SERIALIZED';
    const serials: GateOutLineSerial[] = line.units.map((unit) => ({
      serial_unit: unit.serial_unit,
      serial_number: unit.serial_number,
    }));
    return {
      item_type: line.item_type,
      item_name: line.item_name,
      tracking_mode: line.tracking_mode,
      no_serial_reason: '',
      requested_qty: serialized
        ? String(serials.length)
        : trimQuantity(quantities[index] ?? line.requested_qty),
      uom: line.uom,
      owner_type: line.owner_type,
      owner_client: line.owner_client,
      condition: line.condition,
      is_returnable: false,
      expected_return_date: null,
      box: line.box,
      box_code: line.box_code,
      box_path: line.box_path,
      serials,
      reels: [],
    };
  });
}

export interface LineGroup {
  /** Stable key: the box heading, or "loose". */
  key: string;
  /** Null for lines that came from no box. */
  heading: string | null;
  /** Each line with its position in the original list, for change and remove. */
  lines: { line: GateOutLine; index: number }[];
}

/**
 * Lines grouped under their box heading, loose lines last (§4.15.7). Groups
 * are in order of first appearance, and lines from one box stay together even
 * if other lines were added between them.
 */
export function groupLinesByBox(lines: GateOutLine[]): LineGroup[] {
  const groups = new Map<string, LineGroup>();
  const loose: LineGroup = { key: 'loose', heading: null, lines: [] };
  lines.forEach((line, index) => {
    const heading = boxHeading(line);
    if (!heading) {
      loose.lines.push({ line, index });
      return;
    }
    const key = `box:${line.box ?? heading}`;
    const group = groups.get(key) ?? { key, heading, lines: [] };
    group.lines.push({ line, index });
    groups.set(key, group);
  });
  const result = [...groups.values()];
  if (loose.lines.length) result.push(loose);
  return result;
}

/** "In PAL-7 › CTN-1" for a scanned unit, or null when the lookup did not say. */
export function unitBoxText(unit: { box_path?: string[] | null }): string | null {
  return unit.box_path?.length ? `In ${unit.box_path.join(BOX_SEPARATOR)}` : null;
}

export interface SubmitProblem {
  /** The server's sentence about it. */
  message: string;
  /** The draft unit it names, when one could be found. */
  lineIndex: number | null;
  serial_unit: number | null;
  serial_number: string;
}

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/**
 * Match each `UNIT_NOT_AVAILABLE` problem to the unit in the draft it names, so
 * the screen can offer to remove it (P10). The server's problems are sentences
 * that name the serial, so a serial is matched as a whole token: RRU-1 is not
 * RRU-10.
 */
export function matchProblems(problems: string[], lines: GateOutLine[]): SubmitProblem[] {
  return problems.map((message) => {
    for (let lineIndex = 0; lineIndex < lines.length; lineIndex += 1) {
      for (const serial of lines[lineIndex].serials ?? []) {
        if (!serial.serial_number) continue;
        const token = new RegExp(
          `(?<![A-Za-z0-9-])${escapeRegExp(serial.serial_number)}(?![A-Za-z0-9-])`,
          'i',
        );
        if (token.test(message)) {
          return {
            message,
            lineIndex,
            serial_unit: serial.serial_unit,
            serial_number: serial.serial_number,
          };
        }
      }
    }
    return { message, lineIndex: null, serial_unit: null, serial_number: '' };
  });
}

/**
 * Take one named unit off a line. A serialized line's quantity is its unit
 * count, so it follows; a line left with no units goes.
 */
export function removeSerial(
  lines: GateOutLine[],
  lineIndex: number,
  serialUnit: number,
): GateOutLine[] {
  const next: GateOutLine[] = [];
  lines.forEach((line, index) => {
    if (index !== lineIndex) {
      next.push(line);
      return;
    }
    const serials = (line.serials ?? []).filter((serial) => serial.serial_unit !== serialUnit);
    if (serials.length === 0) return;
    next.push({ ...line, serials, requested_qty: String(serials.length) });
  });
  return next;
}

/** What a refused submit says: its message and, for a unit refusal, every problem. */
export function submitRefusal(error: unknown): { message: string; problems: string[] } | null {
  const candidate = error as {
    code?: string;
    message?: string;
    details?: { problems?: unknown };
  } | null;
  if (!candidate) return null;
  if (candidate.code !== 'UNIT_NOT_AVAILABLE' && candidate.code !== 'BOX_CLAIM_SHORT') {
    return null;
  }
  const problems = Array.isArray(candidate.details?.problems)
    ? candidate.details.problems.filter((row): row is string => typeof row === 'string')
    : [];
  return { message: candidate.message ?? '', problems };
}

/**
 * A draft stored before boxes existed has no box fields, and one stored by a
 * build in between may lack `serials` or `reels`. Fill what the screen reads.
 */
export function normaliseDraftLine(line: GateOutLine): GateOutLine {
  return {
    ...line,
    box: line.box ?? null,
    box_code: line.box_code ?? null,
    box_path: Array.isArray(line.box_path) ? line.box_path : [],
    serials: Array.isArray(line.serials) ? line.serials : [],
    reels: Array.isArray(line.reels) ? line.reels : [],
  };
}
