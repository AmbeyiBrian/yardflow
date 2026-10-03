/**
 * Pure helpers for the box screens (T11.14; P4, P7, P8). No DOM, no hooks, so
 * they are tested directly.
 */

export interface BoxUnit {
  id: number;
  serial_number: string;
  asset_tag: string;
  item_type: number;
  item_name: string;
  status: string;
  condition: string;
}

export interface BoxBulk {
  item_type: number;
  item_name: string;
  owner_client: number | null;
  owner_name: string;
  condition: string;
  /** A decimal string (or number) from the server. */
  quantity: string | number;
  uom: string;
}

export interface BoxCounts {
  received: { units: number; bulk: string | number };
  now: { units: number; bulk: string | number };
}

export interface BoxNode {
  id: number;
  code: string;
  status: string;
  node: string;
  depth: number;
  units: BoxUnit[];
  bulk: BoxBulk[];
  children: BoxNode[];
  counts: BoxCounts;
}

export interface BoxDetail extends BoxNode {
  source: string;
  parent_code: string | null;
  path: string[];
  node_id: number;
  node_label: string;
  gate_in: number | null;
  gate_in_number: string | null;
  created_at: string;
  closed_at: string | null;
}

export interface BoxRow {
  id: number;
  code: string;
  status: string;
  source: string;
  depth: number;
  parent_code: string | null;
  node: number;
  node_label: string;
  units_now: number;
  bulk_lines_now: number;
  created_at: string;
  closed_at: string | null;
}

export interface BoxEvent {
  id: number;
  occurred_at: string;
  action: string;
  action_label: string;
  box_code: string;
  actor: string;
  serial_number: string;
  child_box_code: string;
  item_name: string;
  owner_client: string;
  condition: string;
  quantity: string | null;
  document_type: string;
  document_id: number | null;
  document_number: string;
  note: string;
}

/** "PAL-7 › CTN-1": outermost box first, ending in this one. */
export function pathText(path: readonly string[]): string {
  return path.join(' › ');
}

/** Trailing zeros off a decimal string: "12.000" -> "12". */
export function trimQuantity(value: string | number): string {
  const text = String(value);
  if (!text.includes('.')) return text;
  return text.replace(/0+$/, '').replace(/\.$/, '');
}

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

/**
 * "10 of 12 units still in it" — received against now (P4). When nothing has
 * gone out it says so rather than repeating the figure twice.
 */
export function unitsCountText(counts: BoxCounts): string {
  const { received, now } = counts;
  if (received.units === 0 && now.units === 0) return 'No serialized units';
  if (now.units === received.units) {
    return `All ${plural(received.units, 'unit', 'units')} still in it`;
  }
  return `${now.units} of ${plural(received.units, 'unit', 'units')} still in it`;
}

/** The same for bulk, which is a quantity rather than a count of things. */
export function bulkCountText(counts: BoxCounts): string {
  const received = Number(counts.received.bulk);
  const now = Number(counts.now.bulk);
  if (received === 0 && now === 0) return 'No bulk stock';
  if (now === received) return `All ${trimQuantity(counts.received.bulk)} of bulk still in it`;
  return `${trimQuantity(counts.now.bulk)} of ${trimQuantity(counts.received.bulk)} of bulk still in it`;
}

/** "in PAL-7" for a list row; empty when the box stands alone. */
export function parentText(parentCode: string | null): string {
  return parentCode ? `in ${parentCode}` : '';
}

/** A bulk line's identity inside one box: the server keys it on these three. */
export function bulkKey(
  line: Pick<BoxBulk, 'item_type' | 'owner_client' | 'condition'>,
): string {
  return `${line.item_type}:${line.owner_client ?? 'own'}:${line.condition}`;
}

export interface TakeOutSelection {
  unitIds: readonly number[];
  /** Quantity typed per bulk line, by `bulkKey`. Blank or zero means none. */
  bulkQuantities: Readonly<Record<string, string>>;
  boxCodes: readonly string[];
}

export interface TakeOutBody {
  units: number[];
  bulk: {
    item_type: number;
    owner_client: number | null;
    condition: string;
    quantity: string;
  }[];
  boxes: string[];
}

/**
 * The request body for take-out (P7), from what was ticked and typed.
 * Blank, zero and non-numeric quantities are left out; `bulkProblem` reports
 * the ones that are too big or malformed before anything is sent.
 */
export function buildTakeOutBody(box: Pick<BoxNode, 'bulk'>, sel: TakeOutSelection): TakeOutBody {
  const bulk: TakeOutBody['bulk'] = [];
  for (const line of box.bulk) {
    const raw = (sel.bulkQuantities[bulkKey(line)] ?? '').trim();
    const qty = Number(raw);
    if (raw === '' || !Number.isFinite(qty) || qty <= 0) continue;
    bulk.push({
      item_type: line.item_type,
      owner_client: line.owner_client,
      condition: line.condition,
      quantity: raw,
    });
  }
  return { units: [...sel.unitIds], bulk, boxes: [...sel.boxCodes] };
}

/** True when the body would take nothing out. */
export function isEmptyTakeOut(body: TakeOutBody): boolean {
  return body.units.length === 0 && body.bulk.length === 0 && body.boxes.length === 0;
}

/** A message for the first bulk quantity that is malformed or more than is there. */
export function bulkProblem(
  box: Pick<BoxNode, 'bulk'>,
  quantities: Readonly<Record<string, string>>,
): string | null {
  for (const line of box.bulk) {
    const raw = (quantities[bulkKey(line)] ?? '').trim();
    if (raw === '') continue;
    const qty = Number(raw);
    if (!Number.isFinite(qty) || qty < 0) {
      return `"${raw}" is not a quantity for ${line.item_name}.`;
    }
    if (qty > Number(line.quantity)) {
      return `Only ${trimQuantity(line.quantity)} ${line.uom} of ${line.item_name} is in the box.`;
    }
  }
  return null;
}

/** One line summarising what a take-out will do, for the confirm step. */
export function takeOutSummary(body: TakeOutBody): string {
  const parts: string[] = [];
  if (body.units.length) parts.push(plural(body.units.length, 'unit', 'units'));
  if (body.bulk.length) parts.push(plural(body.bulk.length, 'bulk line', 'bulk lines'));
  if (body.boxes.length) parts.push(plural(body.boxes.length, 'box', 'boxes'));
  return parts.join(', ');
}

/** Where an event's document lives, when it has a number. */
export function documentLink(
  event: Pick<BoxEvent, 'document_type' | 'document_id' | 'document_number'>,
): string | null {
  if (!event.document_number || event.document_id == null) return null;
  const type = event.document_type.toLowerCase().replace(/[\s_-]/g, '');
  if (type.includes('gatein')) return `/gate-in/${event.document_id}`;
  if (type.includes('gateout')) return `/gate-out/${event.document_id}`;
  return null;
}

/** What an event was about, in one phrase: the unit, the box, or the bulk line. */
export function eventSubject(event: BoxEvent): string {
  if (event.serial_number) return event.serial_number;
  if (event.child_box_code) return `box ${event.child_box_code}`;
  if (event.item_name) {
    const qty = event.quantity != null ? `${trimQuantity(event.quantity)} × ` : '';
    return `${qty}${event.item_name}`;
  }
  return '';
}
