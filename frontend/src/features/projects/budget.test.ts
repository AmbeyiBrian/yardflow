import { describe, expect, it } from 'vitest';

import {
  committedPlusSpent,
  dateOnly,
  isAcceptedSite,
  isOverBudget,
  percentUsed,
  reasonText,
} from './budget';
import type { BudgetPosition } from './stage2Api';

const base: BudgetPosition = {
  budget: '1000.00',
  spent: '400.10',
  committed: '200.20',
  pending: '0.00',
  remaining: '399.70',
};

describe('budget display', () => {
  it('adds spent and committed exactly', () => {
    expect(committedPlusSpent(base)).toBe('600.30');
  });
  it('computes percent used', () => {
    expect(percentUsed(base)).toBe(60);
    expect(percentUsed({ ...base, budget: null, remaining: null })).toBeNull();
  });
  it('flags a negative remaining only', () => {
    expect(isOverBudget(base)).toBe(false);
    expect(isOverBudget({ ...base, remaining: '-5.00' })).toBe(true);
    expect(isOverBudget({ ...base, budget: null, remaining: null })).toBe(false);
  });
  it('states a missing reason', () => {
    expect(reasonText('  ')).toBe('Over budget, no reason given');
    expect(reasonText('Rain delay')).toBe('Rain delay');
  });
});

describe('site helpers', () => {
  it('accepts only with a date and a certificate', () => {
    expect(isAcceptedSite('2026-01-01', 1)).toBe(true);
    expect(isAcceptedSite('2026-01-01', 0)).toBe(false);
    expect(isAcceptedSite(null, 2)).toBe(false);
  });
  it('trims timestamps to dates', () => {
    expect(dateOnly('2026-03-04T10:00:00Z')).toBe('2026-03-04');
    expect(dateOnly(null)).toBe('—');
  });
});
