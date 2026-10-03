import { describe, expect, it } from 'vitest';

import {
  buildTakeOutBody,
  bulkCountText,
  bulkKey,
  bulkProblem,
  documentLink,
  isEmptyTakeOut,
  parentText,
  pathText,
  takeOutSummary,
  trimQuantity,
  unitsCountText,
  type BoxBulk,
} from './boxHelpers';

const line = (over: Partial<BoxBulk> = {}): BoxBulk => ({
  item_type: 5,
  item_name: 'Cable ties',
  owner_client: null,
  owner_name: '',
  condition: 'NEW',
  quantity: '100.000',
  uom: 'pcs',
  ...over,
});

describe('box helpers', () => {
  it('writes the path outermost first', () => {
    expect(pathText(['PAL-7', 'CTN-1'])).toBe('PAL-7 › CTN-1');
  });

  it('says how many units are still in', () => {
    const c = (r: number, n: number) => ({
      received: { units: r, bulk: 0 },
      now: { units: n, bulk: 0 },
    });
    expect(unitsCountText(c(12, 10))).toBe('10 of 12 units still in it');
    expect(unitsCountText(c(12, 12))).toBe('All 12 units still in it');
    expect(unitsCountText(c(1, 1))).toBe('All 1 unit still in it');
    expect(unitsCountText(c(0, 0))).toBe('No serialized units');
  });

  it('says how much bulk is still in', () => {
    const c = (r: string, n: string) => ({
      received: { units: 0, bulk: r },
      now: { units: 0, bulk: n },
    });
    expect(bulkCountText(c('100.000', '40.500'))).toBe('40.5 of 100 of bulk still in it');
    expect(bulkCountText(c('100.000', '100.000'))).toBe('All 100 of bulk still in it');
    expect(bulkCountText(c('0', '0'))).toBe('No bulk stock');
  });

  it('trims decimals and names the parent', () => {
    expect(trimQuantity('12.000')).toBe('12');
    expect(trimQuantity('12.500')).toBe('12.5');
    expect(trimQuantity(7)).toBe('7');
    expect(parentText('PAL-7')).toBe('in PAL-7');
    expect(parentText(null)).toBe('');
  });

  it('builds a take-out body from the selection, leaving blanks out', () => {
    const own = line();
    const client = line({ item_type: 6, owner_client: 9, condition: 'USED' });
    const body = buildTakeOutBody(
      { bulk: [own, client] },
      {
        unitIds: [3, 4],
        bulkQuantities: { [bulkKey(own)]: '', [bulkKey(client)]: '2.5' },
        boxCodes: ['CTN-1'],
      },
    );
    expect(body).toEqual({
      units: [3, 4],
      bulk: [{ item_type: 6, owner_client: 9, condition: 'USED', quantity: '2.5' }],
      boxes: ['CTN-1'],
    });
    expect(isEmptyTakeOut(body)).toBe(false);
    expect(takeOutSummary(body)).toBe('2 units, 1 bulk line, 1 box');
  });

  it('treats an empty selection as nothing to take out', () => {
    const l = line();
    const body = buildTakeOutBody(
      { bulk: [l] },
      { unitIds: [], bulkQuantities: { [bulkKey(l)]: '0' }, boxCodes: [] },
    );
    expect(isEmptyTakeOut(body)).toBe(true);
  });

  it('refuses a bulk quantity bigger than what is there', () => {
    const l = line();
    expect(bulkProblem({ bulk: [l] }, { [bulkKey(l)]: '101' })).toMatch(/Only 100 pcs/);
    expect(bulkProblem({ bulk: [l] }, { [bulkKey(l)]: 'x' })).toMatch(/not a quantity/);
    expect(bulkProblem({ bulk: [l] }, { [bulkKey(l)]: '100' })).toBeNull();
  });

  it('links documents only when there is a number', () => {
    expect(
      documentLink({ document_type: 'GATE_IN', document_id: 4, document_number: 'GI-4' }),
    ).toBe('/gate-in/4');
    expect(
      documentLink({ document_type: 'GATE_OUT', document_id: 8, document_number: 'GO-8' }),
    ).toBe('/gate-out/8');
    expect(
      documentLink({ document_type: 'GATE_IN', document_id: 4, document_number: '' }),
    ).toBeNull();
  });
});
