import { describe, expect, it } from 'vitest';

import {
  applyTicks,
  buildOfflinePayload,
  buildReleaseBody,
  describeScan,
  emptyTicks,
  isShort,
  lineQuantity,
  linesNeedingScans,
  tickedCount,
  toLoadLines,
  toScanPass,
  untickUnit,
  type LoadLineInput,
} from './loadScan';
import { matchScan } from './matchScan';

const lines: LoadLineInput[] = [
  {
    id: 1,
    tracking_mode: 'SERIALIZED',
    requested_qty: '3',
    released_qty: '0',
    box_path: ['PAL-1'],
    serials: [
      { id: 11, serial_unit: 101, serial_number: 'SN-A', released: false },
      { id: 12, serial_unit: 102, serial_number: 'SN-B', asset_tag: 'AT-B', released: false },
      { id: 13, serial_unit: 103, serial_number: 'SN-C', released: false },
    ],
  },
  { id: 2, tracking_mode: 'BULK', requested_qty: '10', released_qty: '4', box_path: ['CTN-9'] },
];
const load = toLoadLines(lines);

function scan(text: string, state = emptyTicks()) {
  const result = matchScan(toScanPass(lines), text, state);
  return { result, state: applyTicks(state, result.ticks) };
}

describe('scanning two of three and a stranger', () => {
  it('refuses the stranger and releases short by one', () => {
    let state = emptyTicks();
    state = scan('SN-A', state).state;
    state = scan('AT-B', state).state;
    const stranger = scan('XYZ', state);
    expect(stranger.result.outcome).toBe('refused');
    expect(describeScan(stranger.result, () => '').tone).toBe('bad');

    expect(lineQuantity(load[0], state, '3')).toBe(2);
    expect(isShort(load[0], state, '3')).toBe(true);
    const body = buildReleaseBody(load, state, {}, { '1': ' one missing ' });
    expect(body.released_lines).toEqual({ '1': '2', '2': '6' });
    expect(body.released_serials).toEqual({ '1': [101, 102] });
    expect(body.variance_reasons).toEqual({ '1': 'one missing' });
  });

  it('unticking returns the line to hand entry', () => {
    let state = scan('SN-A').state;
    state = untickUnit(state, 11);
    expect(lineQuantity(load[0], state, '3')).toBe(3);
    expect(buildReleaseBody(load, state, {}, {}).released_serials).toEqual({});
  });
});

describe('hand confirmation', () => {
  it('sends a serialized line by quantity only', () => {
    const body = buildReleaseBody(load, emptyTicks(), { '1': '2' }, { '1': 'two on the truck' });
    expect(body.released_lines['1']).toBe('2');
    expect(body.released_serials).toEqual({});
  });
  it('a box scan ticks the bulk line at the outstanding quantity, still editable', () => {
    const { state } = scan('CTN-9');
    expect(state.lines.has(2)).toBe(true);
    expect(load[1].outstanding).toBe(6);
    expect(lineQuantity(load[1], state, '5')).toBe(5);
  });
  it('counts ticks for the scanner', () => {
    expect(tickedCount(load, scan('PAL-1').state)).toBe(3);
  });
});

describe('release_scan_required', () => {
  it('flags serialized lines with a quantity and no scans', () => {
    expect(linesNeedingScans(load, emptyTicks(), {}, true).map((l) => l.id)).toEqual([1]);
    expect(linesNeedingScans(load, emptyTicks(), {}, false)).toEqual([]);
  });
  it('a line set to zero, or scanned, no longer needs scans', () => {
    expect(linesNeedingScans(load, emptyTicks(), { '1': '0' }, true)).toEqual([]);
    expect(linesNeedingScans(load, scan('SN-A').state, {}, true)).toEqual([]);
  });
});

describe('offline payload', () => {
  it('uses the keys the replay reads', () => {
    const body = buildReleaseBody(load, scan('SN-A').state, {}, {});
    expect(Object.keys(buildOfflinePayload(7, 'KDA', 'Joe', body)).sort()).toEqual([
      'driver_name',
      'gate_out',
      'lines',
      'released_serials',
      'variance_reasons',
      'vehicle_reg',
    ]);
  });
});

describe('describeScan', () => {
  it('says whose gate pass it was', () => {
    const result = matchScan(toScanPass(lines), 'x', emptyTicks());
    result.outcome = 'gate_pass';
    expect(describeScan(result, () => '', true).message).toContain('this gate pass');
    expect(describeScan(result, () => '', false).message).toContain('different gate pass');
  });
});
