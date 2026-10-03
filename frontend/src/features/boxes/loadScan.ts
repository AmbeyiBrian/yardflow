/**
 * The pure half of "Scan the load" (P11, G1, design §4.15.8).
 *
 * Both release sheets (online and offline) show the same pass in slightly
 * different shapes, so everything that decides a quantity or builds a payload
 * lives here, with no React and no DOM, and is tested on its own.
 *
 * The rule that matters: once any unit of a serialized line is ticked, the
 * released quantity of that line **is** the tick count and the units are named
 * in `released_serials`; otherwise the hand-entered quantity is used and the
 * line is sent the old way. Whatever is below outstanding is short and needs a
 * reason (G1).
 */
import type { Id, PassSerial, ScanPass, ScanResult, Tick, TickState } from './types';

const SERIALIZED = 'SERIALIZED';

/** A serial entry as either read side sends it; most fields may be missing. */
export interface LoadSerialInput {
  id?: Id;
  serial_unit: Id;
  serial_number?: string;
  asset_tag?: string | null;
  released?: boolean;
  box_path?: string[];
}

/** A pass line as either read side sends it (gate-out detail or offline bundle). */
export interface LoadLineInput {
  id?: Id;
  tracking_mode: string;
  requested_qty?: number | string;
  released_qty?: number | string;
  outstanding_qty?: number | string;
  box_path?: string[];
  serials?: LoadSerialInput[];
}

/** The fields the release sheet needs per line, from either shape. */
export interface LoadLine {
  id: Id;
  serialized: boolean;
  outstanding: number;
  units: PassSerial[];
}

export function emptyTicks(): TickState {
  return { serials: new Set(), lines: new Set() };
}

function numberOf(value: number | string | undefined | null): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

export function outstandingOf(line: LoadLineInput): number {
  if (line.outstanding_qty !== undefined && line.outstanding_qty !== null) {
    return numberOf(line.outstanding_qty);
  }
  return numberOf(line.requested_qty) - numberOf(line.released_qty);
}

/** Adapt either kind of pass to what `matchScan` reads. */
export function toScanPass(lines: LoadLineInput[]): ScanPass {
  return {
    lines: lines.map((line) => ({
      id: line.id as Id,
      tracking_mode: line.tracking_mode,
      requested_qty: line.requested_qty,
      released_qty: line.released_qty,
      outstanding_qty: outstandingOf(line),
      box_path: line.box_path ?? [],
      serials: (line.serials ?? []).map((unit) => ({
        id: unit.id ?? unit.serial_unit,
        serial_unit: unit.serial_unit,
        serial_number: unit.serial_number ?? '',
        asset_tag: unit.asset_tag ?? null,
        released: Boolean(unit.released),
        box_path: unit.box_path ?? [],
      })),
    })),
  };
}

export function toLoadLines(lines: LoadLineInput[]): LoadLine[] {
  return toScanPass(lines).lines.map((line) => ({
    id: line.id,
    serialized: line.tracking_mode === SERIALIZED,
    outstanding: outstandingOf(line),
    units: line.serials,
  }));
}

/** Add new ticks; returns a new state and never mutates the old one. */
export function applyTicks(state: TickState, ticks: Tick[]): TickState {
  const serials = new Set(state.serials);
  const lines = new Set(state.lines);
  for (const tick of ticks) {
    if (tick.kind === 'unit') serials.add(tick.serialId);
    else lines.add(tick.lineId);
  }
  return { serials, lines };
}

export function untickUnit(state: TickState, serialId: Id): TickState {
  const serials = new Set(state.serials);
  serials.delete(serialId);
  return { serials, lines: state.lines };
}

export function untickLine(state: TickState, lineId: Id): TickState {
  const lines = new Set(state.lines);
  lines.delete(lineId);
  return { serials: state.serials, lines };
}

/** Units of a line that can be ticked at all: not already released on the pass. */
export function openUnits(line: LoadLine): PassSerial[] {
  return line.units.filter((unit) => !unit.released);
}

/** Ticked units of one line, in the line's own order. */
export function tickedUnits(line: LoadLine, state: TickState): PassSerial[] {
  return openUnits(line).filter((unit) => state.serials.has(unit.id));
}

/** Total ticked items across the pass, for the scanner's counter. */
export function tickedCount(lines: LoadLine[], state: TickState): number {
  return lines.reduce(
    (sum, line) =>
      sum + (line.serialized ? tickedUnits(line, state).length : state.lines.has(line.id) ? 1 : 0),
    0,
  );
}

/** Is the line's quantity set by scans, not by hand? (Serialized, one unit or more ticked.) */
export function isScanned(line: LoadLine, state: TickState): boolean {
  return line.serialized && tickedUnits(line, state).length > 0;
}

/**
 * The quantity this line releases: the tick count when scans set it, otherwise
 * the typed text. `null` when the text is not a number.
 */
export function lineQuantity(
  line: LoadLine,
  state: TickState,
  manual: string | undefined,
): number | null {
  if (isScanned(line, state)) return tickedUnits(line, state).length;
  const text = (manual ?? String(line.outstanding)).trim();
  if (text === '') return null;
  const parsed = Number(text);
  return Number.isFinite(parsed) ? parsed : null;
}

export function isShort(line: LoadLine, state: TickState, manual: string | undefined): boolean {
  const qty = lineQuantity(line, state, manual);
  return qty !== null && qty < line.outstanding;
}

/**
 * Lines that still need their units scanned because the organization requires
 * it: serialized, nothing scanned, and a quantity above zero.
 */
export function linesNeedingScans(
  lines: LoadLine[],
  state: TickState,
  actual: Record<string, string>,
  required: boolean,
): LoadLine[] {
  if (!required) return [];
  return lines.filter((line) => {
    if (!line.serialized || isScanned(line, state)) return false;
    const qty = lineQuantity(line, state, actual[String(line.id)]);
    return qty === null || qty > 0;
  });
}

export interface ReleaseBody {
  /** line id -> quantity, as the release endpoint's `released_lines`. */
  released_lines: Record<string, string>;
  variance_reasons: Record<string, string>;
  /** line id -> unit ids; only for lines whose quantity was set by scans. */
  released_serials: Record<string, Id[]>;
}

/**
 * What goes to the server. Reasons are sent only for lines that are short, so
 * a stale reason typed before units were scanned does not travel.
 */
export function buildReleaseBody(
  lines: LoadLine[],
  state: TickState,
  actual: Record<string, string>,
  reasons: Record<string, string>,
): ReleaseBody {
  const body: ReleaseBody = { released_lines: {}, variance_reasons: {}, released_serials: {} };
  for (const line of lines) {
    const key = String(line.id);
    const qty = lineQuantity(line, state, actual[key]);
    body.released_lines[key] = String(qty ?? actual[key] ?? line.outstanding);
    if (isScanned(line, state)) {
      body.released_serials[key] = tickedUnits(line, state).map((unit) => unit.serial_unit);
    }
    if (isShort(line, state, actual[key]) && reasons[key]?.trim()) {
      body.variance_reasons[key] = reasons[key].trim();
    }
  }
  return body;
}

/** The payload `_apply_gate_out_release` reads: the same body under its own keys. */
export function buildOfflinePayload(gateOutId: Id, vehicle: string, driver: string, body: ReleaseBody) {
  return {
    gate_out: gateOutId,
    vehicle_reg: vehicle,
    driver_name: driver,
    lines: body.released_lines,
    variance_reasons: body.variance_reasons,
    released_serials: body.released_serials,
  };
}

export interface LoadNotice {
  tone: 'good' | 'info' | 'bad';
  message: string;
}

/** Plain words for the last scan. `thisPass` is only known for a gate-pass QR. */
export function describeScan(
  result: ScanResult,
  names: (tick: Tick) => string,
  thisPass?: boolean | null,
): LoadNotice {
  const list = (ticks: Tick[]) => {
    const labels = ticks.map(names);
    return labels.length <= 3
      ? labels.join(', ')
      : `${labels.slice(0, 3).join(', ')} and ${labels.length - 3} more`;
  };
  switch (result.outcome) {
    case 'ticked': {
      const count = result.ticks.length;
      return {
        tone: 'good',
        message:
          count === 1
            ? `Ticked ${list(result.ticks)}.`
            : `Ticked ${count} items: ${list(result.ticks)}.`,
      };
    }
    case 'already_counted':
      return { tone: 'info', message: `${list(result.alreadyCounted)} is already counted.` };
    case 'already_released':
      return {
        tone: 'info',
        message: `${list(result.alreadyReleased)} was already released on this pass.`,
      };
    case 'gate_pass':
      return {
        tone: 'info',
        message:
          thisPass === true
            ? 'That is this gate pass, not goods. Scan the items being loaded.'
            : thisPass === false
              ? 'That is a different gate pass, not this one. Nothing was ticked.'
              : 'That is a gate pass, not goods. Scan the items being loaded.',
      };
    default:
      return { tone: 'bad', message: result.refused?.message ?? 'That scan was refused.' };
  }
}
