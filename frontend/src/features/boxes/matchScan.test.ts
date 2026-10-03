// Matching a scan against fixture passes (P11, §4.15.8).
import { describe, expect, it } from 'vitest';
import { matchScan } from './matchScan';
import type { PassLine, PassSerial, ScanPass, TickState } from './types';

const none: TickState = { serials: new Set(), lines: new Set() };

function unit(
  id: number,
  serial: string,
  boxPath: string[] = [],
  extra: Partial<PassSerial> = {},
): PassSerial {
  return {
    id,
    serial_unit: id + 1000,
    serial_number: serial,
    asset_tag: null,
    released: false,
    box_path: boxPath,
    ...extra,
  };
}

function line(id: number, serials: PassSerial[], boxPath: string[] = []): PassLine {
  return {
    id,
    tracking_mode: 'SERIALIZED',
    outstanding_qty: serials.filter((s) => !s.released).length,
    box_path: boxPath,
    serials,
  };
}

// A loose unit with an asset tag, a carton of two, and a pallet holding two
// cartons (CT-1 above, CT-2 beside), plus a bulk line of cable in CT-3.
const pass: ScanPass = {
  lines: [
    line(1, [unit(1, 'LOOSE-1', [], { asset_tag: 'AT-77' })]),
    line(2, [unit(2, 'C1-A', ['PL-9', 'CT-1']), unit(3, 'C1-B', ['PL-9', 'CT-1'])], ['PL-9', 'CT-1']),
    line(3, [unit(4, 'C2-A', ['PL-9', 'CT-2'])], ['PL-9', 'CT-2']),
    {
      id: 4,
      tracking_mode: 'BULK',
      requested_qty: '50.000',
      released_qty: '10.000',
      box_path: ['CT-3'],
      serials: [],
    },
  ],
};

describe('matchScan', () => {
  it('ticks a loose unit by serial', () => {
    const r = matchScan(pass, 'LOOSE-1', none);
    expect(r.outcome).toBe('ticked');
    expect(r.ticks).toEqual([
      { kind: 'unit', lineId: 1, serialId: 1, serialUnit: 1001, serialNumber: 'LOOSE-1' },
    ]);
    expect(r.refused).toBeNull();
  });

  it('ticks a unit by asset tag', () => {
    const r = matchScan(pass, 'AT-77', none);
    expect(r.ticks.map((t) => t.kind === 'unit' && t.serialNumber)).toEqual(['LOOSE-1']);
  });

  it('is case-insensitive and reads the label first', () => {
    expect(matchScan(pass, '  loose-1\n', none).ticks).toHaveLength(1);
    expect(matchScan(pass, 'at-77', none).ticks).toHaveLength(1);
    expect(matchScan(pass, '{"sn":"loose-1"}', none).ticks).toHaveLength(1);
    expect(matchScan(pass, 'SN: loose-1', none).ticks).toHaveLength(1);
  });

  it('ticks a box code\'s units only', () => {
    const r = matchScan(pass, 'ct-1', none);
    expect(r.ticks.map((t) => t.kind === 'unit' && t.serialNumber)).toEqual(['C1-A', 'C1-B']);
  });

  it('ticks units across two cartons from a pallet code', () => {
    const r = matchScan(pass, 'PL-9', none);
    expect(r.ticks.map((t) => t.kind === 'unit' && t.serialNumber)).toEqual(['C1-A', 'C1-B', 'C2-A']);
  });

  it('reads a box code out of a JSON label', () => {
    expect(matchScan(pass, '{"carton":"CT-2"}', none).ticks).toHaveLength(1);
  });

  it('ticks a bulk line in a box at its full outstanding quantity', () => {
    const r = matchScan(pass, 'CT-3', none);
    expect(r.ticks).toEqual([{ kind: 'line', lineId: 4, qty: 40 }]);
  });

  it('refuses a stranger, naming it', () => {
    const r = matchScan(pass, 'NOPE-1', none);
    expect(r.outcome).toBe('refused');
    expect(r.ticks).toEqual([]);
    expect(r.refused?.scanned).toBe('NOPE-1');
    expect(r.refused?.message).toContain('NOPE-1');
    expect(r.refused?.message).toContain('not on this pass');
  });

  it('refuses an empty scan without calling it a stranger', () => {
    const r = matchScan(pass, '  ', none);
    expect(r.outcome).toBe('refused');
    expect(r.refused?.scanned).toBe('');
  });

  it('reports a repeat as already counted, and does not tick again', () => {
    const ticked: TickState = { serials: new Set([1]), lines: new Set() };
    const r = matchScan(pass, 'LOOSE-1', ticked);
    expect(r.outcome).toBe('already_counted');
    expect(r.ticks).toEqual([]);
    expect(r.alreadyCounted).toHaveLength(1);
    expect(r.refused).toBeNull();
  });

  it('ticks only what is new when a box is partly counted', () => {
    const ticked: TickState = { serials: new Set([2]), lines: new Set() };
    const r = matchScan(pass, 'CT-1', ticked);
    expect(r.outcome).toBe('ticked');
    expect(r.ticks.map((t) => t.kind === 'unit' && t.serialNumber)).toEqual(['C1-B']);
    expect(r.alreadyCounted).toHaveLength(1);
  });

  it('counts a bulk line once', () => {
    const ticked: TickState = { serials: new Set(), lines: new Set([4]) };
    expect(matchScan(pass, 'CT-3', ticked).outcome).toBe('already_counted');
  });

  it('does not tick a unit that was already released', () => {
    const released: ScanPass = {
      lines: [line(1, [unit(1, 'GONE-1', ['CT-5'], { released: true }), unit(2, 'HERE-1', ['CT-5'])])],
    };
    const box = matchScan(released, 'CT-5', none);
    expect(box.ticks.map((t) => t.kind === 'unit' && t.serialNumber)).toEqual(['HERE-1']);
    expect(box.alreadyReleased).toHaveLength(1);

    const single = matchScan(released, 'GONE-1', none);
    expect(single.outcome).toBe('already_released');
    expect(single.ticks).toEqual([]);
    expect(single.refused).toBeNull();
  });

  it('detects a gate-pass QR and neither ticks nor refuses', () => {
    const r = matchScan(pass, 'https://y.example/api/v1/qr/scan?token=abc123', none);
    expect(r.outcome).toBe('gate_pass');
    expect(r.documentToken).toBe('abc123');
    expect(r.ticks).toEqual([]);
    expect(r.refused).toBeNull();
  });

  it('does not change the tick state it is given', () => {
    const ticked: TickState = { serials: new Set([1]), lines: new Set() };
    matchScan(pass, 'PL-9', ticked);
    expect([...ticked.serials]).toEqual([1]);
  });
});
