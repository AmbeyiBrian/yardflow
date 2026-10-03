/**
 * Matching a scan against a gate pass (P11, design §4.15.8).
 *
 * One pure function, run on the device against the pass's own data, so ticking
 * the load works with no signal. It reads the label first (§4.15.6, P2, P3):
 * a scan may be a bare serial, a vendor JSON, a GS1 barcode and so on, and the
 * reading's serials and box code are matched alongside the raw text.
 *
 * Each value is compared case-insensitively (as the database does) with:
 *  - a unit's serial number or asset tag: ticks that unit;
 *  - a code in a box path: ticks every unreleased unit whose path contains it,
 *    and every non-serialized line whose path contains it, at its full
 *    outstanding quantity. A unit's own path wins; a unit with none uses its
 *    line's.
 *
 * Nothing is ever ticked twice: a unit already on screen is reported as already
 * counted, and one already released on the pass is reported and left alone. A
 * gate-pass QR is its own outcome, because it is not goods and not a mistake.
 * Anything else is refused, naming what was scanned. The function never
 * mutates `alreadyTicked`; the caller applies `ticks`.
 */
import { readLabel } from './readLabel';
import type {
  Id,
  PassLine,
  PassSerial,
  ScanPass,
  ScanResult,
  Tick,
  TickState,
} from './types';

const SERIALIZED = 'SERIALIZED';

function norm(value: string | null | undefined): string {
  return (value ?? '').trim().toLowerCase();
}

function inPath(path: string[] | undefined, code: string): boolean {
  return (path ?? []).some((entry) => norm(entry) === code);
}

function outstanding(line: PassLine): number {
  if (line.outstanding_qty !== undefined && line.outstanding_qty !== null) {
    return Number(line.outstanding_qty) || 0;
  }
  return (Number(line.requested_qty) || 0) - (Number(line.released_qty) || 0);
}

function unitTick(line: PassLine, unit: PassSerial): Tick {
  return {
    kind: 'unit',
    lineId: line.id,
    serialId: unit.id,
    serialUnit: unit.serial_unit,
    serialNumber: unit.serial_number,
  };
}

/**
 * Work out what a scan means for this pass.
 *
 * `ticks` are the new things to count; the rest of the result says why there
 * are none, if there are none (`outcome`, `refused`, `alreadyCounted`, ...).
 */
export function matchScan(pass: ScanPass, scannedText: string, alreadyTicked: TickState): ScanResult {
  const reading = readLabel(scannedText);
  const result: ScanResult = {
    outcome: 'refused',
    ticks: [],
    alreadyCounted: [],
    alreadyReleased: [],
    documentToken: reading.documentToken,
    refused: null,
    reading,
  };

  if (reading.documentToken) {
    result.outcome = 'gate_pass';
    return result;
  }

  const trimmed = (scannedText ?? '').trim();
  const codes = new Set<string>();
  for (const value of [trimmed, ...reading.serials, reading.boxCode]) {
    const code = norm(value);
    if (code) codes.add(code);
  }

  // Keyed so a unit or line reached by two codes is only handled once.
  const seenUnits = new Set<Id>();
  const seenLines = new Set<Id>();

  const take = (line: PassLine, unit: PassSerial) => {
    if (seenUnits.has(unit.id)) return;
    seenUnits.add(unit.id);
    const tick = unitTick(line, unit);
    if (unit.released) result.alreadyReleased.push(tick);
    else if (alreadyTicked.serials.has(unit.id)) result.alreadyCounted.push(tick);
    else result.ticks.push(tick);
  };

  const takeLine = (line: PassLine) => {
    if (seenLines.has(line.id)) return;
    seenLines.add(line.id);
    const qty = outstanding(line);
    const tick: Tick = { kind: 'line', lineId: line.id, qty };
    if (qty <= 0) result.alreadyReleased.push(tick);
    else if (alreadyTicked.lines.has(line.id)) result.alreadyCounted.push(tick);
    else result.ticks.push(tick);
  };

  for (const code of codes) {
    for (const line of pass.lines) {
      const serialized = line.tracking_mode === SERIALIZED;
      for (const unit of line.serials) {
        const own = unit.box_path && unit.box_path.length > 0 ? unit.box_path : line.box_path;
        if (norm(unit.serial_number) === code || norm(unit.asset_tag) === code || inPath(own, code)) {
          take(line, unit);
        }
      }
      if (!serialized && inPath(line.box_path, code)) takeLine(line);
    }
  }

  if (result.ticks.length > 0) result.outcome = 'ticked';
  else if (result.alreadyCounted.length > 0) result.outcome = 'already_counted';
  else if (result.alreadyReleased.length > 0) result.outcome = 'already_released';
  else {
    const scanned = trimmed || reading.serials[0] || reading.boxCode || '';
    result.refused = {
      scanned,
      message: scanned
        ? `${scanned} is not on this pass.`
        : 'Nothing was read from that scan.',
    };
  }
  return result;
}
