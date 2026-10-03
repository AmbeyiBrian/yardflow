import { describe, expect, it } from 'vitest';

import {
  drumNote,
  effectiveSite,
  lineToSheet,
  moveSerial,
  replaceLineAt,
  sheetToLine,
  type SheetResult,
} from './gateInLineEdit';
import type { GateInLineInput } from './types';

const base = {
  uom: 'ea',
  condition: 'NEW' as const,
  owner_type: 'OWN' as const,
  owner_client: null,
  no_serial_reason: '',
  notes: '',
};

function result(state: ReturnType<typeof lineToSheet>, mode: SheetResult['mode']): SheetResult {
  return {
    item: state.item,
    mode,
    quantity: state.quantity,
    condition: state.condition,
    ownerClient: state.ownerClient,
    noSerialReason: state.noSerialReason,
    notes: state.notes,
    serials: state.serials,
    drums: state.drums,
    notOnDrum: state.notOnDrum,
    activeKey: state.intoKey,
    forSite: state.forSite,
  };
}

describe('gate-in line and sheet', () => {
  it('round-trips a bulk line in a box', () => {
    const line: GateInLineInput = {
      ...base,
      item_type: 4,
      item_name: 'Cable clamp',
      tracking_mode: 'BULK',
      quantity: '10',
      notes: 'dented',
      owner_type: 'CLIENT',
      owner_client: 7,
      box_key: 'k1',
      serials: [],
      reels: [],
    };
    const state = lineToSheet(line);
    expect(state.intoKey).toBe('k1');
    expect(state.quantity).toBe('10');
    expect(state.ownerClient).toBe('7');
    expect(state.item.tracking_mode).toBe('BULK');
    expect(sheetToLine(result(state, 'BULK'), line)).toEqual(line);
  });

  it('round-trips a serialized line with units in two boxes', () => {
    const line: GateInLineInput = {
      ...base,
      item_type: 5,
      item_name: 'Baseband board',
      tracking_mode: 'SERIALIZED',
      quantity: '2',
      box_key: '',
      serials: [
        { serial_number: 'A1', box_key: 'a' },
        { serial_number: 'A2', box_key: 'b' },
      ],
      reels: [],
    };
    const state = lineToSheet(line);
    expect(state.intoKey).toBe('');
    expect(state.serials.map((s) => s.box_key)).toEqual(['a', 'b']);
    expect(sheetToLine(result(state, 'SERIALIZED'), line)).toEqual(line);
  });

  it('moves one unit and leaves the rest', () => {
    const moved = moveSerial(
      [
        { serial_number: 'A1', box_key: 'a' },
        { serial_number: 'A2', box_key: 'a' },
      ],
      'A2',
      'b',
    );
    expect(moved.map((s) => s.box_key)).toEqual(['a', 'b']);
  });

  it('round-trips a drum line and totals the lengths', () => {
    const line: GateInLineInput = {
      ...base,
      item_type: 6,
      item_name: 'Power cable',
      tracking_mode: 'REEL',
      uom: 'm',
      quantity: '300',
      box_key: '',
      serials: [],
      reels: [
        { drum_number: 'D1', length: '100' },
        { drum_number: 'D2', length: '200' },
      ],
    };
    const state = lineToSheet(line);
    expect(state.drums).toEqual(line.reels);
    expect(sheetToLine(result(state, 'REEL'), line)).toEqual(line);
  });

  it('receives cable not on a drum as a bulk line and reopens it so', () => {
    const state = lineToSheet({
      ...base,
      item_type: 6,
      item_name: 'Power cable',
      tracking_mode: 'REEL',
      uom: 'm',
      quantity: '0',
      reels: [],
    });
    const line = sheetToLine({ ...result(state, 'REEL'), quantity: '240', notOnDrum: true });
    expect(line.tracking_mode).toBe('BULK');
    expect(line.quantity).toBe('240');
    expect(line.reels).toEqual([]);
    expect(line.no_serial_reason).toBe('');
    expect(line.box_key).toBe('');
    expect(drumNote(line)).toBe(' · not on a drum');

    const reopened = lineToSheet(line);
    expect(reopened.notOnDrum).toBe(true);
    expect(reopened.item.default_tracking_mode).toBe('REEL');
    expect(reopened.quantity).toBe('240');
    expect(sheetToLine(result(reopened, 'REEL'), line)).toEqual(line);
  });

  it('does not mistake a plain bulk line for cable, and counts drums', () => {
    const bulk: GateInLineInput = { ...base, item_type: 4, tracking_mode: 'BULK', quantity: '3' };
    expect(lineToSheet(bulk).notOnDrum).toBe(false);
    expect(drumNote(bulk)).toBe('');
    expect(drumNote({ ...bulk, tracking_mode: 'REEL', reels: [{ drum_number: 'D', length: '1' }] })).toBe(
      ' · 1 drums',
    );
  });

  it('keeps a no-serial line changeable as a serialized item', () => {
    const line: GateInLineInput = {
      ...base,
      item_type: 5,
      item_name: 'Baseband board',
      tracking_mode: 'BULK',
      quantity: '3',
      no_serial_reason: 'label torn',
      box_key: '',
      serials: [],
      reels: [],
    };
    const state = lineToSheet(line);
    expect(state.item.default_tracking_mode).toBe('SERIALIZED');
    expect(sheetToLine(result(state, 'SERIALIZED'), line)).toEqual(line);
  });

  it('replaces at the index without reordering or duplicating', () => {
    const mk = (n: number): GateInLineInput => ({
      ...base,
      item_type: n,
      tracking_mode: 'BULK',
      quantity: String(n),
    });
    const next = replaceLineAt([mk(1), mk(2), mk(3)], 1, mk(9));
    expect(next.map((l) => l.item_type)).toEqual([1, 9, 3]);
    expect(replaceLineAt([mk(1)], 5, mk(9)).map((l) => l.item_type)).toEqual([1]);
  });
});

describe('for site (Q1)', () => {
  const line: GateInLineInput = {
    ...base,
    item_type: 4,
    item_name: 'Cable clamp',
    tracking_mode: 'BULK',
    quantity: '10',
    for_site: 9,
  };

  it("keeps a line's own site through Change", () => {
    const state = lineToSheet(line);
    expect(state.forSite).toBe('9');
    expect(sheetToLine(result(state, 'BULK'), line).for_site).toBe(9);
  });

  it('a line with no site inherits (left out), and reopens blank', () => {
    const plain = { ...line, for_site: undefined };
    const state = lineToSheet(plain);
    expect(state.forSite).toBe('');
    expect(sheetToLine(result(state, 'BULK'), plain).for_site).toBeUndefined();
  });

  it('works out the effective site', () => {
    expect(effectiveSite(line, '3')).toBe(9);
    expect(effectiveSite({ ...line, for_site: null }, '3')).toBe(3);
    expect(effectiveSite({ ...line, for_site: null }, '')).toBeNull();
  });
});
