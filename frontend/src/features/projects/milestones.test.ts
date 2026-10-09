import { describe, expect, it } from 'vitest';

import {
  addDays,
  daysBetween,
  isOverdue,
  milestoneAmountCents,
  percentSharesTotal,
  poTotals,
  receiptRoomCents,
} from './milestones';

describe('milestoneAmountCents', () => {
  it('takes a percent of the contract value', () => {
    expect(milestoneAmountCents('PERCENT', '30', '1000000.00')).toBe(30_000_000);
  });
  it('uses a fixed amount as typed', () => {
    expect(milestoneAmountCents('AMOUNT', '250000.50', '1000000')).toBe(25_000_050);
  });
  it('is zero while the share is empty', () => {
    expect(milestoneAmountCents('PERCENT', '', '1000')).toBe(0);
  });
});

describe('percentSharesTotal', () => {
  it('sums percent shares only', () => {
    expect(
      percentSharesTotal([
        { share_type: 'PERCENT', share_value: '30' },
        { share_type: 'PERCENT', share_value: '70' },
        { share_type: 'AMOUNT', share_value: '5000' },
      ]),
    ).toBe(100);
  });
});

describe('isOverdue', () => {
  it('is overdue past invoice date plus terms with money outstanding', () => {
    expect(isOverdue(100_000, 0, '2026-09-01', 30, '2026-10-02')).toBe(true);
  });
  it('is not overdue on the last day of terms', () => {
    expect(isOverdue(100_000, 0, '2026-09-01', 30, '2026-10-01')).toBe(false);
  });
  it('is never overdue without terms days or once paid', () => {
    expect(isOverdue(100_000, 0, '2026-01-01', null, '2026-10-01')).toBe(false);
    expect(isOverdue(100_000, 100_000, '2026-01-01', 30, '2026-10-01')).toBe(false);
  });
});

describe('dates and totals', () => {
  it('adds days across a month end', () => {
    expect(addDays('2026-01-31', 1)).toBe('2026-02-01');
  });
  it('counts days between dates', () => {
    expect(daysBetween('2026-10-09', '2026-10-01')).toBe(8);
  });
  it('totals invoiced, received and outstanding in cents', () => {
    const t = poTotals('1000.00', [
      { invoiced: '300.10', received: '100.05' },
      { invoiced: '0', received: '0' },
    ]);
    expect(t).toEqual({
      value: 100_000,
      invoiced: 30_010,
      received: 10_005,
      outstanding: 89_995,
      invoicedUnpaid: 20_005,
    });
  });
  it('caps receipts at what is invoiced', () => {
    expect(receiptRoomCents(500, 200)).toBe(300);
    expect(receiptRoomCents(500, 700)).toBe(0);
  });
});
