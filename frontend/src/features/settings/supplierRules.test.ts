import { describe, expect, it } from 'vitest';

import { ApiError } from '../../api/client';
import { duplicateOf, normalisePin, paymentRoutes, supplierChip } from './supplierRules';

describe('paymentRoutes', () => {
  it('lists bank and till routes, and nothing when none', () => {
    expect(paymentRoutes({})).toEqual([]);
    expect(
      paymentRoutes({
        bank_name: 'KCB',
        account_number: '123',
        mpesa_type: 'TILL',
        mpesa_number: '55',
      }),
    ).toEqual(['Bank: KCB · 123', 'Till: 55']);
  });
});

describe('normalisePin', () => {
  it('upper-cases and strips spaces', () => {
    expect(normalisePin(' p05 1234 567a ')).toBe('P051234567A');
  });
});

describe('supplierChip', () => {
  it('shows inactive over any status', () => {
    expect(supplierChip({ status: 'APPROVED', is_active: false })).toBe('Inactive');
  });
  it('maps statuses', () => {
    expect(supplierChip({ status: 'PENDING', is_active: true })).toBe('Pending');
    expect(supplierChip({ status: 'REJECTED', is_active: true })).toBe('Rejected');
    expect(supplierChip({ status: 'APPROVED', is_active: true })).toBe('Approved');
  });
});

describe('duplicateOf', () => {
  it('reads the named supplier', () => {
    const e = new ApiError(409, {
      code: 'SUPPLIER_PIN_DUPLICATE',
      message: 'dup',
      details: { existing: { id: 4, name: 'Kenya Cable', status: 'APPROVED' } },
    } as never);
    expect(duplicateOf(e)).toEqual({
      kind: 'pin',
      existing: { id: 4, name: 'Kenya Cable', status: 'APPROVED' },
    });
  });
  it('is null for other errors', () => {
    expect(duplicateOf(new Error('x'))).toBeNull();
  });
});
