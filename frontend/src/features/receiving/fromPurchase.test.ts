import { describe, expect, it } from 'vitest';

import { fromPurchaseNumber } from './fromPurchase';

describe('fromPurchaseNumber', () => {
  it('prefers the API field', () => {
    expect(
      fromPurchaseNumber({ source_type: 'PURCHASE', source_purchase_number: 'SP-000003' }),
    ).toBe('SP-000003');
  });
  it('falls back to the draft note', () => {
    expect(
      fromPurchaseNumber({ source_type: 'PURCHASE', notes: 'From site purchase SP-000007' }),
    ).toBe('SP-000007');
  });
  it('is empty for other sources and plain purchases', () => {
    expect(fromPurchaseNumber({ source_type: 'RECOVERY', notes: 'SP-000007' })).toBe('');
    expect(fromPurchaseNumber({ source_type: 'PURCHASE', notes: 'bought' })).toBe('');
  });
});
