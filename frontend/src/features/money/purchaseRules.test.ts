import { describe, expect, it } from 'vitest';

import {
  lineTotal,
  purchaseTotal,
  validatePurchase,
  type PurchaseLineDraft,
} from './purchaseRules';

const line = (p: Partial<PurchaseLineDraft> = {}): PurchaseLineDraft => ({
  item_type: '',
  description: 'Cable ties',
  quantity: '2',
  unit_price: '10.50',
  ...p,
});

describe('lineTotal', () => {
  it('multiplies in cents', () => expect(lineTotal('2', '10.50')).toBe(2100));
  it('rounds fractional quantities to whole cents', () =>
    expect(lineTotal('0.333', '10.00')).toBe(333));
  it('is NaN for blanks', () => {
    expect(lineTotal('', '5')).toBeNaN();
    expect(lineTotal('2', '')).toBeNaN();
  });
});

describe('purchaseTotal', () => {
  it('sums parseable lines and skips the rest', () =>
    expect(
      purchaseTotal([line(), line({ quantity: '1', unit_price: '3' }), line({ quantity: '' })]),
    ).toBe(2400));
});

describe('validatePurchase', () => {
  it('accepts free text at the site', () =>
    expect(
      validatePurchase({ destination: 'USED_AT_SITE', receiveInto: '', lines: [line()] }),
    ).toEqual({}));
  it('needs a line', () =>
    expect(
      validatePurchase({ destination: 'USED_AT_SITE', receiveInto: '', lines: [] }).lines,
    ).toBeTruthy());
  it('refuses zero quantity', () =>
    expect(
      validatePurchase({
        destination: 'USED_AT_SITE',
        receiveInto: '',
        lines: [line({ quantity: '0' })],
      }).lines,
    ).toBeTruthy());
  it('yard needs a location and catalogue items', () => {
    const e = validatePurchase({ destination: 'INTO_YARD', receiveInto: '', lines: [line()] });
    expect(e.receive_into).toBeTruthy();
    expect(e.lines).toMatch(/catalogue/);
  });
  it('yard passes with both', () =>
    expect(
      validatePurchase({
        destination: 'INTO_YARD',
        receiveInto: '4',
        lines: [line({ item_type: '7' })],
      }),
    ).toEqual({}));
});
