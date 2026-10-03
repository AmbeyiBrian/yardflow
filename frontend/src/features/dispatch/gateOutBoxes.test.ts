import { describe, expect, it } from 'vitest';

import {
  bulkQuantityProblem,
  exclusionText,
  groupLinesByBox,
  matchProblems,
  normaliseDraftLine,
  proposalSummary,
  proposalToLines,
  removeSerial,
  submitRefusal,
  unitBoxText,
  type IssuableExclusion,
  type IssuableLine,
  type LookedUpBox,
} from './gateOutBoxes';
import type { GateOutLine } from './types';

const box: LookedUpBox = {
  id: 5,
  code: 'CTN-1',
  status: 'OPEN',
  node: 1,
  node_label: 'Yard',
  units_now: 8,
  bulk_lines_now: 1,
  parent_code: 'PAL-7',
};

const radios: IssuableLine = {
  item_type: 3,
  item_name: 'RRU',
  tracking_mode: 'SERIALIZED',
  uom: 'EA',
  owner_type: 'OWN',
  owner_client: null,
  condition: 'NEW',
  requested_qty: '2',
  units: [
    { serial_unit: 11, serial_number: 'RRU-1' },
    { serial_unit: 12, serial_number: 'RRU-2' },
  ],
  box: 5,
  box_code: 'CTN-1',
  box_path: ['PAL-7', 'CTN-1'],
};

const jumpers: IssuableLine = {
  item_type: 4,
  item_name: 'Jumper',
  tracking_mode: 'BULK',
  uom: 'EA',
  owner_type: 'CLIENT',
  owner_client: 9,
  condition: 'GOOD',
  requested_qty: '40.000',
  units: [],
  box: 5,
  box_code: 'CTN-1',
  box_path: ['PAL-7', 'CTN-1'],
};

describe('proposalSummary', () => {
  it('counts units and bulk lines and names the parent', () => {
    expect(proposalSummary(box, { lines: [radios, jumpers], excluded: [] })).toBe(
      'CTN-1 (in PAL-7): 2 units and 1 bulk line can go',
    );
  });

  it('says so when nothing can go', () => {
    expect(proposalSummary({ ...box, parent_code: null }, { lines: [], excluded: [] })).toBe(
      'Nothing in CTN-1 can go from here.',
    );
  });
});

describe('proposalToLines', () => {
  it('names each unit and counts them as the quantity', () => {
    const [line] = proposalToLines([radios]);
    expect(line).toMatchObject({
      item_type: 3,
      tracking_mode: 'SERIALIZED',
      requested_qty: '2',
      uom: 'EA',
      owner_type: 'OWN',
      owner_client: null,
      condition: 'NEW',
      box: 5,
      serials: [
        { serial_unit: 11, serial_number: 'RRU-1' },
        { serial_unit: 12, serial_number: 'RRU-2' },
      ],
      reels: [],
    });
  });

  it('takes an edited quantity for bulk, and ignores one for serialized', () => {
    const lines = proposalToLines([radios, jumpers], { 0: '1', 1: '15' });
    expect(lines[0].requested_qty).toBe('2');
    expect(lines[1].requested_qty).toBe('15');
    expect(lines[1].box).toBe(5);
    expect(lines[1].owner_client).toBe(9);
  });

  it('trims the API decimal for a bulk line left alone', () => {
    expect(proposalToLines([jumpers])[0].requested_qty).toBe('40');
  });
});

describe('bulkQuantityProblem', () => {
  it('accepts up to the claim and refuses beyond or empty', () => {
    expect(bulkQuantityProblem('40', '40.000', 'EA')).toBeNull();
    expect(bulkQuantityProblem('41', '40.000', 'EA')).toMatch(/holds 40 EA/);
    expect(bulkQuantityProblem('0', '40', 'EA')).not.toBeNull();
    expect(bulkQuantityProblem('', '40', 'EA')).not.toBeNull();
  });
});

describe('exclusionText', () => {
  const base: IssuableExclusion = {
    kind: 'unit',
    serial_unit: 1,
    serial_number: 'RRU-3',
    item_type: 3,
    item_name: 'RRU',
    box: 5,
    box_code: 'CTN-1',
    reason: 'ON_ANOTHER_PASS',
    message: 'RRU-3 is on gate pass GP-000041, which is still open.',
  };
  it('uses the server message', () => {
    expect(exclusionText(base)).toBe(base.message);
  });
  it('falls back by reason', () => {
    expect(exclusionText({ ...base, message: '' })).toBe(
      'RRU-3 is on another gate pass that is still open.',
    );
  });
});

describe('groupLinesByBox', () => {
  const line = (over: Partial<GateOutLine>): GateOutLine => ({
    item_type: 1,
    tracking_mode: 'BULK',
    requested_qty: '1',
    uom: 'EA',
    ...over,
  });

  it('groups by box in order of appearance, keeps one box together, loose last', () => {
    const lines = [
      line({ item_name: 'a' }),
      line({ item_name: 'b', box: 5, box_path: ['PAL-7', 'CTN-1'] }),
      line({ item_name: 'c', box: 6, box_path: ['CTN-2'] }),
      line({ item_name: 'd', box: 5, box_path: ['PAL-7', 'CTN-1'] }),
    ];
    const groups = groupLinesByBox(lines);
    expect(groups.map((group) => group.heading)).toEqual(['PAL-7 › CTN-1', 'CTN-2', null]);
    expect(groups[0].lines.map((entry) => entry.index)).toEqual([1, 3]);
    expect(groups[2].lines.map((entry) => entry.index)).toEqual([0]);
  });

  it('falls back to the box code, then to the id', () => {
    const groups = groupLinesByBox([
      line({ box: 5, box_code: 'CTN-1' }),
      line({ box: 8 }),
    ]);
    expect(groups.map((group) => group.heading)).toEqual(['CTN-1', 'Box 8']);
  });

  it('has no groups for no lines', () => {
    expect(groupLinesByBox([])).toEqual([]);
  });
});

describe('unitBoxText', () => {
  it('names the chain, or nothing', () => {
    expect(unitBoxText({ box_path: ['PAL-7', 'CTN-1'] })).toBe('In PAL-7 › CTN-1');
    expect(unitBoxText({ box_path: [] })).toBeNull();
    expect(unitBoxText({})).toBeNull();
  });
});

describe('matchProblems and removeSerial', () => {
  const lines = proposalToLines([radios, { ...radios, units: [{ serial_unit: 20, serial_number: 'RRU-10' }] }]);

  it('matches a serial as a whole token', () => {
    const [found, other] = matchProblems(
      ['RRU-1 is already on gate pass GP-000041, which is still open.', 'Nothing named here.'],
      lines,
    );
    expect(found).toMatchObject({ lineIndex: 0, serial_unit: 11, serial_number: 'RRU-1' });
    expect(other.lineIndex).toBeNull();
  });

  it('does not take RRU-10 for RRU-1', () => {
    const [found] = matchProblems(['RRU-10 is at Other yard.'], lines);
    expect(found).toMatchObject({ lineIndex: 1, serial_unit: 20 });
  });

  it('removes a unit and recounts, dropping an emptied line', () => {
    const afterOne = removeSerial(lines, 0, 11);
    expect(afterOne[0].serials).toHaveLength(1);
    expect(afterOne[0].requested_qty).toBe('1');
    const afterAll = removeSerial(removeSerial(lines, 0, 11), 0, 12);
    expect(afterAll).toHaveLength(1);
    expect(afterAll[0].serials?.[0].serial_unit).toBe(20);
  });
});

describe('submitRefusal', () => {
  it('reads both box refusals and ignores others', () => {
    expect(
      submitRefusal({
        code: 'UNIT_NOT_AVAILABLE',
        message: 'Two units.',
        details: { problems: ['a', 'b'] },
      }),
    ).toEqual({ message: 'Two units.', problems: ['a', 'b'] });
    expect(submitRefusal({ code: 'BOX_CLAIM_SHORT', message: 'Short.' })).toEqual({
      message: 'Short.',
      problems: [],
    });
    expect(submitRefusal({ code: 'OTHER', message: 'x' })).toBeNull();
    expect(submitRefusal(null)).toBeNull();
  });
});

describe('normaliseDraftLine', () => {
  it('lets an old draft line load', () => {
    const old = { item_type: 1, tracking_mode: 'BULK', requested_qty: '2', uom: 'EA' } as GateOutLine;
    expect(normaliseDraftLine(old)).toMatchObject({
      box: null,
      box_code: null,
      box_path: [],
      serials: [],
      reels: [],
    });
  });
});
