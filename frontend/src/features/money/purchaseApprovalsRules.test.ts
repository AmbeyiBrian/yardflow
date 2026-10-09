import { describe, expect, it } from 'vitest';

import {
  payBlockedReason,
  paymentLevelLabel,
  supplierApproved,
  supplierNote,
  willCreateDelivery,
} from './purchaseApprovalsRules';

describe('purchase approval rules', () => {
  it('only an APPROVED supplier can be paid', () => {
    expect(supplierApproved('APPROVED')).toBe(true);
    expect(supplierApproved('PENDING')).toBe(false);
    expect(supplierApproved(undefined)).toBe(false);
  });
  it('words the supplier note and the pay block', () => {
    expect(supplierNote('PENDING')).toBe('awaiting approval');
    expect(supplierNote('APPROVED')).toBe('');
    expect(payBlockedReason({ supplier_status: 'PENDING' })).toContain("can't pay yet");
    expect(payBlockedReason({ supplier_status: 'APPROVED' })).toBe('');
  });
  it('only yard purchases create a delivery', () => {
    expect(willCreateDelivery({ destination: 'INTO_YARD' })).toBe(true);
    expect(willCreateDelivery({ destination: 'USED_AT_SITE' })).toBe(false);
  });
  it('labels the one subcontract level', () => {
    expect(paymentLevelLabel('PENDING_PM')).toMatch(/project manager/);
    expect(paymentLevelLabel('X')).toBe('X');
  });
});
