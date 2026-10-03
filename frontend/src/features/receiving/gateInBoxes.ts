/**
 * Boxes on a gate-in, as pure functions (P1, P2, P9, P10; design §4.15.5).
 *
 * The capture screen keeps boxes in its draft as a flat list with `parent_key`
 * pointers, and lines and units point at them by `box_key`. Everything that
 * needs to *understand* that shape lives here and not in the component: the
 * tree the storekeeper sees, the counts beside each box, which boxes may be a
 * parent, whether a box can go, and what the server's refusals mean in words.
 * None of it touches the DOM, so all of it is tested without one.
 */

import type { GateInBoxInput, GateInLineInput } from './types';

/** A pallet holds cartons, a carton holds boxes, a box holds units (§4.15.5). */
export const MAX_BOX_DEPTH = 3;

export type DraftBox = GateInBoxInput;

/* -------------------------------------------------------------------------- */
/* Reading what is stored                                                     */
/* -------------------------------------------------------------------------- */

/**
 * Boxes from whatever was stored or returned.
 *
 * A draft saved before boxes existed has none; a server draft may hold `null`
 * for a blank parent. Neither may break the screen, because a storekeeper
 * cannot repair a stored draft, only abandon it.
 */
export function normaliseBoxes(raw: unknown): DraftBox[] {
  if (!Array.isArray(raw)) return [];
  const seen = new Set<string>();
  const boxes: DraftBox[] = [];
  for (const entry of raw) {
    if (!entry || typeof entry !== 'object') continue;
    const row = entry as Record<string, unknown>;
    const key = typeof row.key === 'string' ? row.key : '';
    if (!key || seen.has(key)) continue;
    seen.add(key);
    boxes.push({
      key,
      code: typeof row.code === 'string' ? row.code : '',
      parent_key: typeof row.parent_key === 'string' ? row.parent_key : '',
      label_text: typeof row.label_text === 'string' ? row.label_text : '',
    });
  }
  return boxes;
}

/* -------------------------------------------------------------------------- */
/* Names                                                                      */
/* -------------------------------------------------------------------------- */

/**
 * What to call a box: its code, or "Unlabelled box N" where N counts the
 * unlabelled boxes in the order they were started. A box with no code yet has
 * no other name, and the storekeeper has to be able to tell two of them apart.
 */
export function boxLabel(boxes: DraftBox[], key: string): string {
  let unlabelled = 0;
  for (const box of boxes) {
    const code = box.code.trim();
    if (!code) unlabelled += 1;
    if (box.key === key) return code || `Unlabelled box ${unlabelled}`;
  }
  return 'A box';
}

/** "PAL-7 › CTN-1": a box with the boxes it sits in, for pickers. */
export function boxPath(boxes: DraftBox[], key: string): string {
  const chain: string[] = [];
  const seen = new Set<string>();
  let cursor: string = key;
  while (cursor && !seen.has(cursor)) {
    seen.add(cursor);
    chain.unshift(boxLabel(boxes, cursor));
    cursor = boxes.find((box) => box.key === cursor)?.parent_key ?? '';
  }
  return chain.join(' › ');
}

/* -------------------------------------------------------------------------- */
/* Depth and the parent picker                                                */
/* -------------------------------------------------------------------------- */

/** 1 for a box on its own; one more for each box it sits inside. */
export function depthOf(boxes: DraftBox[], key: string): number {
  const seen = new Set<string>();
  let depth = 0;
  let cursor: string = key;
  while (cursor && !seen.has(cursor)) {
    const box = boxes.find((entry) => entry.key === cursor);
    if (!box) break;
    seen.add(cursor);
    depth += 1;
    cursor = box.parent_key;
  }
  // A loop is as deep as it gets: report it too deep rather than looping.
  return cursor && seen.has(cursor) ? MAX_BOX_DEPTH + 1 : depth;
}

function childrenOf(boxes: DraftBox[], key: string): DraftBox[] {
  return boxes.filter((box) => box.parent_key === key);
}

/** Boxes in and below `key`, `key` included. Visits each once, so a loop ends. */
function subtreeKeys(boxes: DraftBox[], key: string): Set<string> {
  const found = new Set<string>();
  const queue = [key];
  while (queue.length) {
    const next = queue.pop()!;
    if (found.has(next)) continue;
    found.add(next);
    for (const child of childrenOf(boxes, next)) queue.push(child.key);
  }
  return found;
}

/** Levels in the tree under `key`, itself included: 1 for a box with no boxes in it. */
function heightOf(boxes: DraftBox[], key: string, seen = new Set<string>()): number {
  if (seen.has(key)) return 0;
  const next = new Set(seen).add(key);
  let tallest = 0;
  for (const child of childrenOf(boxes, key)) {
    tallest = Math.max(tallest, heightOf(boxes, child.key, next));
  }
  return 1 + tallest;
}

/**
 * The boxes that may hold another box.
 *
 * Only a box with room for one more level qualifies (three deep at most), and a
 * box can never be put inside itself or anything inside it. `selfKey` is the box
 * being placed; for a box that is only now being started there is none, and the
 * answer is just "has room".
 */
export function parentChoices(boxes: DraftBox[], selfKey?: string): DraftBox[] {
  const blocked = selfKey ? subtreeKeys(boxes, selfKey) : new Set<string>();
  const own = selfKey ? heightOf(boxes, selfKey) : 1;
  return boxes.filter(
    (candidate) =>
      !blocked.has(candidate.key) && depthOf(boxes, candidate.key) + own <= MAX_BOX_DEPTH,
  );
}

/** Whether `parentKey` is an acceptable place for `selfKey` (or a new box if absent). */
export function canBeParent(boxes: DraftBox[], parentKey: string, selfKey?: string): boolean {
  return parentChoices(boxes, selfKey).some((box) => box.key === parentKey);
}

/** A box whose code is already on this delivery (any case, ignoring spaces). */
export function findByCode(boxes: DraftBox[], code: string): DraftBox | undefined {
  const wanted = code.trim().toLowerCase();
  if (!wanted) return undefined;
  return boxes.find((box) => box.code.trim().toLowerCase() === wanted);
}

/* -------------------------------------------------------------------------- */
/* What is in which box                                                       */
/* -------------------------------------------------------------------------- */

/** The box a unit was scanned into, or '' when it is loose. */
export function boxOfSerial(lines: GateInLineInput[], serialNumber: string): string {
  for (const line of lines) {
    for (const serial of line.serials ?? []) {
      if (serial.serial_number === serialNumber) return serial.box_key ?? '';
    }
  }
  return '';
}

/** Serial numbers sitting directly in a box, across every line. */
export function serialsInBox(lines: GateInLineInput[], key: string): string[] {
  const found: string[] = [];
  for (const line of lines) {
    for (const serial of line.serials ?? []) {
      if (serial.box_key === key) found.push(serial.serial_number);
    }
  }
  return found;
}

/** One line's share of one box (or of the loose pile, `box === ''`). */
export interface BoxEntry {
  lineIndex: number;
  line: GateInLineInput;
  /** Serials for a serialized line; empty for bulk and reel. */
  serials: string[];
  /** Units for serialized, the line quantity for bulk. */
  units: number;
}

export interface BoxNode {
  box: DraftBox;
  label: string;
  entries: BoxEntry[];
  children: BoxNode[];
  /** Units directly in this box. */
  ownUnits: number;
  /** Units in this box and everything below it. */
  totalUnits: number;
  /** Boxes below this one, at any depth. */
  boxCount: number;
}

export interface GateInTree {
  roots: BoxNode[];
  /** What is on the delivery but in no box. */
  loose: BoxEntry[];
}

function quantityOf(line: GateInLineInput): number {
  const value = Number(line.quantity);
  return Number.isFinite(value) ? value : 0;
}

/**
 * The delivery as the storekeeper thinks of it: boxes inside boxes, with what
 * is in each, and the rest loose.
 *
 * A line or unit that points at a box which is not there (one removed, or a
 * stored draft that lost it) is treated as loose rather than dropped: losing a
 * unit from the screen is worse than showing it in the wrong place.
 */
export function buildTree(boxes: DraftBox[], lines: GateInLineInput[]): GateInTree {
  const known = new Set(boxes.map((box) => box.key));
  const byBox = new Map<string, BoxEntry[]>();
  const loose: BoxEntry[] = [];

  const place = (key: string | undefined, entry: BoxEntry) => {
    if (key && known.has(key)) byBox.set(key, [...(byBox.get(key) ?? []), entry]);
    else loose.push(entry);
  };

  lines.forEach((line, lineIndex) => {
    if (line.tracking_mode === 'SERIALIZED') {
      // Units, not the line, carry the box: one line can span several cartons.
      const groups = new Map<string, string[]>();
      for (const serial of line.serials ?? []) {
        const key = serial.box_key && known.has(serial.box_key) ? serial.box_key : '';
        groups.set(key, [...(groups.get(key) ?? []), serial.serial_number]);
      }
      for (const [key, serials] of groups) {
        place(key, { lineIndex, line, serials, units: serials.length });
      }
      if (groups.size === 0) place('', { lineIndex, line, serials: [], units: 0 });
    } else if (line.tracking_mode === 'BULK') {
      place(line.box_key, { lineIndex, line, serials: [], units: quantityOf(line) });
    } else {
      // Drums are not boxed (§4.15.5).
      place('', { lineIndex, line, serials: [], units: 0 });
    }
  });

  const placed = new Set<string>();
  const make = (box: DraftBox, trail: Set<string>): BoxNode => {
    placed.add(box.key);
    const next = new Set(trail).add(box.key);
    const children = childrenOf(boxes, box.key)
      .filter((child) => !next.has(child.key))
      .map((child) => make(child, next));
    const entries = byBox.get(box.key) ?? [];
    const ownUnits = entries.reduce((sum, entry) => sum + entry.units, 0);
    return {
      box,
      label: boxLabel(boxes, box.key),
      entries,
      children,
      ownUnits,
      totalUnits: ownUnits + children.reduce((sum, child) => sum + child.totalUnits, 0),
      boxCount: children.reduce((sum, child) => sum + 1 + child.boxCount, 0),
    };
  };

  const roots = boxes
    .filter((box) => !box.parent_key || !known.has(box.parent_key))
    .map((box) => make(box, new Set()));
  // Anything caught in a loop was never reached from a root; show it anyway.
  for (const box of boxes) {
    if (!placed.has(box.key)) roots.push(make(box, new Set()));
  }

  return { roots, loose };
}

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

function formatUnits(count: number): string {
  return plural(Number.isInteger(count) ? count : Number(count.toFixed(3)), 'unit', 'units');
}

/** "10 units", "2 boxes, 20 units", or "empty": the counts beside a box's name. */
export function describeCounts(node: BoxNode): string {
  if (node.totalUnits === 0 && node.boxCount === 0) return 'empty';
  const parts: string[] = [];
  if (node.boxCount > 0) parts.push(plural(node.boxCount, 'box', 'boxes'));
  parts.push(formatUnits(node.totalUnits));
  return parts.join(', ');
}

/* -------------------------------------------------------------------------- */
/* Removing a box                                                             */
/* -------------------------------------------------------------------------- */

/**
 * Why a box cannot be removed, or null when it can.
 *
 * Only an empty box with no boxes inside goes. Quietly deleting what is in it
 * would lose scanned units, and quietly re-homing them would put them somewhere
 * the storekeeper did not choose.
 */
export function removalBlock(
  boxes: DraftBox[],
  lines: GateInLineInput[],
  key: string,
): string | null {
  const label = boxLabel(boxes, key);
  const inside = childrenOf(boxes, key).length;
  if (inside > 0) {
    return `${label} has ${plural(inside, 'box', 'boxes')} inside it. Remove ${
      inside === 1 ? 'that box' : 'those boxes'
    } first.`;
  }
  const node = findNode(buildTree(boxes, lines).roots, key);
  const holds = node ? node.ownUnits : 0;
  const held = node ? node.entries.length : 0;
  if (holds > 0 || held > 0) {
    return `${label} has ${formatUnits(holds)} in it. Remove the lines it is in first, then the box.`;
  }
  return null;
}

function findNode(nodes: BoxNode[], key: string): BoxNode | undefined {
  for (const node of nodes) {
    if (node.box.key === key) return node;
    const inner = findNode(node.children, key);
    if (inner) return inner;
  }
  return undefined;
}

/** The boxes without `key`. Check `removalBlock` first. */
export function withoutBox(boxes: DraftBox[], key: string): DraftBox[] {
  return boxes.filter((box) => box.key !== key);
}

/* -------------------------------------------------------------------------- */
/* Sending                                                                    */
/* -------------------------------------------------------------------------- */

/**
 * Lines as the server wants them.
 *
 * Blank `box_key`s are left off, and a serialized line never carries one of its
 * own: the server refuses a line-level box on a serialized line (units carry it)
 * and a drum line takes none. A server draft being edited can bring blanks back
 * in, and this is where they are stopped.
 */
export function linesForPayload(lines: GateInLineInput[]): GateInLineInput[] {
  return lines.map((line) => {
    const { box_key: boxKey, reel_item: _reelItem, ...rest } = line;
    void _reelItem;
    const out: GateInLineInput = { ...rest };
    if (line.tracking_mode === 'BULK' && boxKey) out.box_key = boxKey;
    if (line.serials) {
      out.serials = line.serials.map((serial) => {
        const { box_key: unitBox, ...unit } = serial;
        return unitBox && line.tracking_mode === 'SERIALIZED'
          ? { ...unit, box_key: unitBox }
          : unit;
      });
    }
    return out;
  });
}

/* -------------------------------------------------------------------------- */
/* The server's refusals, in words                                            */
/* -------------------------------------------------------------------------- */

const CODE_PATTERN = /\((BOX_[A-Z_]+)\)/;

function explain(code: string | undefined, name: string, fallback: string): string {
  switch (code) {
    case 'BOX_CODE_IN_USE':
      return `${name}: that code is already taken. Check the label, or clear the code and one will be made.`;
    case 'BOX_EMPTY':
      return `${name} holds nothing. Put something in it or remove it.`;
    case 'BOX_TOO_DEEP':
      return `${name} is nested too deep. Boxes go three levels at most.`;
    case 'BOX_CYCLE':
      return `${name} would sit inside itself. Change which box it is in.`;
    case 'BOX_MIXED_DESTINATIONS':
      return `${name} mixes serviceable and quarantined stock. Receive them in separate boxes.`;
    default:
      return `${name}: ${fallback}`;
  }
}

/**
 * Box-related `field_errors` as sentences that name the box by its code or
 * "Unlabelled box N". The server addresses things by position
 * (`boxes.2.code`, `lines.0.serials.4.box_key`); a storekeeper has never seen
 * "boxes.2". Positions follow the order the draft was sent in.
 *
 * Errors about anything else are left out, for the caller's own wording.
 */
export function boxErrorMessages(
  fieldErrors: Record<string, string[]>,
  boxes: DraftBox[],
  lines: GateInLineInput[],
): string[] {
  const out: string[] = [];
  for (const [path, messages] of Object.entries(fieldErrors)) {
    const boxMatch = /^boxes\.(\d+)\./.exec(path);
    const lineMatch = /^lines\.(\d+)\.(?:serials\.(\d+)\.)?box_key$/.exec(path);
    let name: string;
    if (boxMatch) {
      const box = boxes[Number(boxMatch[1])];
      name = box ? boxLabel(boxes, box.key) : 'A box';
    } else if (lineMatch) {
      const line = lines[Number(lineMatch[1])];
      const serial =
        lineMatch[2] !== undefined ? line?.serials?.[Number(lineMatch[2])] : undefined;
      name = serial
        ? `Unit ${serial.serial_number}`
        : `Line ${Number(lineMatch[1]) + 1}${line?.item_name ? ` (${line.item_name})` : ''}`;
    } else {
      continue;
    }
    for (const message of messages) {
      out.push(explain(CODE_PATTERN.exec(message)?.[1], name, message));
    }
  }
  return [...new Set(out)];
}
