/**
 * Changing a gate-in line, as pure functions (D9; design §7.3).
 *
 * The line sheet holds its fields as separate pieces of state; a line is one
 * object. Everything that maps between the two lives here, so "Change" reopens
 * the sheet exactly as the line was keyed and "Save changes" writes back the
 * same shape "Add line" would have — tested without a DOM.
 */

import type { PickedItem } from '../../components/ItemPicker';
import type {
  Condition,
  GateInLineInput,
  GateInReelInput,
  GateInSerialInput,
} from './types';

/** What the sheet shows for a line it is reopening. */
export interface SheetState {
  item: PickedItem;
  quantity: string;
  condition: Condition;
  /** '' is our own stock. */
  ownerClient: string;
  noSerialReason: string;
  notes: string;
  /** Each unit with its own box; '' is loose. */
  serials: GateInSerialInput[];
  drums: GateInReelInput[];
  /** The "Into a box" selector: the line's box when bulk, otherwise loose. */
  intoKey: string;
}

/**
 * The item as the line itself knows it.
 *
 * Seeded from the line's own fields so the tracking mode is known before any
 * fetch: a sheet that had to wait for the item list would briefly treat a
 * serialized line as bulk. A line received without serials is BULK on the line
 * but its item is serialized, so the mode is put back — otherwise the "no serial
 * available" reason could never be cleared.
 */
export function itemOfLine(line: GateInLineInput): PickedItem {
  const mode = line.no_serial_reason ? 'SERIALIZED' : line.tracking_mode;
  return {
    id: line.item_type,
    name: line.item_name ?? '',
    code: '',
    uom: line.uom,
    tracking_mode: mode,
    default_tracking_mode: mode,
    is_returnable: false,
    category_name: '',
    description: '',
  };
}

/** Line → the sheet's starting state. */
export function lineToSheet(line: GateInLineInput): SheetState {
  return {
    item: itemOfLine(line),
    quantity: line.tracking_mode === 'BULK' ? String(line.quantity) : '',
    condition: line.condition,
    ownerClient: line.owner_client ? String(line.owner_client) : '',
    noSerialReason: line.no_serial_reason ?? '',
    notes: line.notes ?? '',
    serials: (line.serials ?? []).map((serial) => ({
      serial_number: serial.serial_number,
      box_key: serial.box_key ?? '',
    })),
    drums: (line.reels ?? []).map((drum) => ({ ...drum })),
    intoKey: line.tracking_mode === 'BULK' ? (line.box_key ?? '') : '',
  };
}

/** What the sheet has once its entry has been checked. */
export interface SheetResult {
  item: Pick<PickedItem, 'id' | 'name' | 'uom'>;
  /** The item's own mode; the line's may become BULK when no serial is given. */
  mode: 'BULK' | 'SERIALIZED' | 'REEL';
  /** Bulk quantity as typed. */
  quantity: string;
  condition: Condition;
  ownerClient: string;
  noSerialReason: string;
  notes: string;
  serials: GateInSerialInput[];
  drums: GateInReelInput[];
  /** The selected box, already checked against the delivery's boxes. */
  activeKey: string;
}

/**
 * The sheet → a line.
 *
 * `base` is the line being changed, if any: what the sheet does not edit
 * (custom field values, a unit's asset tag) is carried over rather than lost.
 * Its server ids are dropped — the line is saved as a whole.
 */
export function sheetToLine(state: SheetResult, base?: GateInLineInput): GateInLineInput {
  const trackingMode = state.mode === 'SERIALIZED' && state.noSerialReason ? 'BULK' : state.mode;
  const total =
    trackingMode === 'SERIALIZED'
      ? String(state.serials.length)
      : trackingMode === 'REEL'
        ? String(state.drums.reduce((sum, drum) => sum + Number(drum.length || 0), 0))
        : state.quantity;
  const known = new Map((base?.serials ?? []).map((unit) => [unit.serial_number, unit]));
  const serials = state.serials.map((unit) => {
    const { id: _id, ...carried } = known.get(unit.serial_number) ?? { serial_number: '' };
    void _id;
    return { ...carried, serial_number: unit.serial_number, box_key: unit.box_key ?? '' };
  });
  const line: GateInLineInput = {
    item_type: state.item.id,
    item_name: state.item.name,
    tracking_mode: trackingMode,
    quantity: total,
    uom: state.item.uom,
    condition: state.condition,
    owner_type: state.ownerClient ? 'CLIENT' : 'OWN',
    owner_client: state.ownerClient ? Number(state.ownerClient) : null,
    no_serial_reason: state.noSerialReason,
    notes: state.notes,
    serials,
    reels: state.drums,
    // Bulk only: the whole quantity is in that box. Serialized units carry
    // their own box_key, and drums take none (4.15.5).
    box_key: trackingMode === 'BULK' ? state.activeKey : '',
  };
  if (base?.custom_field_values && base.item_type === state.item.id) {
    line.custom_field_values = base.custom_field_values;
  }
  return line;
}

/** Replace the line at `index`; same position, same count. Out of range changes nothing. */
export function replaceLineAt(
  lines: GateInLineInput[],
  index: number,
  line: GateInLineInput,
): GateInLineInput[] {
  if (index < 0 || index >= lines.length) return lines;
  return lines.map((existing, position) => (position === index ? line : existing));
}

/** Move one unit to another box ('' is loose). Other units are untouched. */
export function moveSerial(
  serials: GateInSerialInput[],
  serialNumber: string,
  boxKey: string,
): GateInSerialInput[] {
  return serials.map((unit) =>
    unit.serial_number === serialNumber ? { ...unit, box_key: boxKey } : unit,
  );
}
