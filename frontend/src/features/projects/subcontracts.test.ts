import { describe, expect, it } from 'vitest';

import {
  committedCents,
  fromCents,
  owedCents,
  owedLabel,
  paidCents,
  workDoneCents,
  wouldExceedContract,
} from './subcontracts';

const jobs = [
  { status: 'CLOSED', agreed_price: '100.10' },
  { status: 'OPEN', agreed_price: '50.20' },
  { status: 'CANCELLED', agreed_price: '999.00' },
];

describe('subcontract figures', () => {
  it('counts only closed jobs as work done', () => {
    expect(workDoneCents(jobs)).toBe(10010);
  });
  it('commits open and closed jobs but not cancelled', () => {
    expect(committedCents(jobs)).toBe(15030);
  });
  it('sums approved payments with reversals, ignoring pending', () => {
    const paid = paidCents([
      { status: 'APPROVED', amount: '60.05' },
      { status: 'APPROVED', amount: '-10.00' },
      { status: 'PENDING_PM', amount: '500.00' },
      { status: 'REJECTED', amount: '70.00' },
    ]);
    expect(paid).toBe(5005);
  });
  it('owed is work done less paid, negative is an advance', () => {
    expect(fromCents(owedCents(10010, 5005))).toBe('50.05');
    expect(owedLabel(owedCents(1000, 2000))).toBe('Advance paid');
    expect(owedLabel(0)).toBe('Owed');
  });
  it('detects an award past the contract value', () => {
    expect(wouldExceedContract('200.00', 15030, '49.70')).toBe(false);
    expect(wouldExceedContract('200.00', 15030, '49.71')).toBe(true);
  });
});
