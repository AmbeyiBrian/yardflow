import { describe, expect, it } from 'vitest';

import {
  candidateProjects,
  checkLimit,
  daysBetween,
  findOverlap,
  floatBalance,
  limitKey,
  normaliseIdNumber,
  statusLabel,
  toCents,
} from './rules';
import type { AllowanceLimits } from './types';

const limits: AllowanceLimits = {
  TRANSPORT_WITHIN_NAIROBI: { min: null, max: '500' },
  TRANSPORT_OUTSIDE_NAIROBI: { min: null, max: null },
  NIGHT_OUT: { min: '1500', max: '10000' },
  TEAM_ALLOWANCE: { min: '1500', max: '10000' },
};

describe('daysBetween', () => {
  it('counts a single day as 1', () => {
    expect(daysBetween('2026-03-01', '2026-03-01')).toBe(1);
  });
  it('is inclusive of both ends', () => {
    expect(daysBetween('2026-03-01', '2026-03-03')).toBe(3);
  });
  it('crosses a leap day without drift', () => {
    expect(daysBetween('2028-02-28', '2028-03-01')).toBe(3);
  });
  it('is not thrown by a DST change month', () => {
    expect(daysBetween('2026-03-28', '2026-03-30')).toBe(3);
  });
});

describe('limitKey', () => {
  it('splits transport by scope', () => {
    expect(limitKey('TRANSPORT', 'WITHIN_NAIROBI')).toBe('TRANSPORT_WITHIN_NAIROBI');
    expect(limitKey('TRANSPORT', 'OUTSIDE_NAIROBI')).toBe('TRANSPORT_OUTSIDE_NAIROBI');
  });
  it('is null for transport without a scope', () => {
    expect(limitKey('TRANSPORT', null)).toBeNull();
  });
  it('uses the type for night out and team allowance', () => {
    expect(limitKey('NIGHT_OUT', null)).toBe('NIGHT_OUT');
    expect(limitKey('TEAM_ALLOWANCE', null)).toBe('TEAM_ALLOWANCE');
  });
  it('is null for FLOAT and OTHER', () => {
    expect(limitKey('FLOAT', null)).toBeNull();
    expect(limitKey('OTHER', null)).toBeNull();
  });
});

describe('checkLimit', () => {
  it('allows exactly max x days (500 x 3 = 1500.00)', () => {
    expect(checkLimit('TRANSPORT', 'WITHIN_NAIROBI', '1500.00', 3, limits)).toBeNull();
  });
  it('flags a cent over', () => {
    const hit = checkLimit('TRANSPORT', 'WITHIN_NAIROBI', '1500.01', 3, limits);
    expect(hit?.daily).toBe('500.00');
    expect(hit?.max).toBe('500');
    expect(hit?.message).toContain('above');
  });
  it('flags below the minimum', () => {
    const hit = checkLimit('NIGHT_OUT', null, '2999.99', 2, limits);
    expect(hit?.min).toBe('1500');
  });
  it('allows exactly min x days', () => {
    expect(checkLimit('NIGHT_OUT', null, '3000', 2, limits)).toBeNull();
  });
  it('treats null bounds as no bound', () => {
    expect(checkLimit('TRANSPORT', 'OUTSIDE_NAIROBI', '999999', 1, limits)).toBeNull();
  });
  it('never limits FLOAT or OTHER', () => {
    expect(checkLimit('FLOAT', null, '999999', 1, limits)).toBeNull();
  });
  it('does nothing without limits or a usable amount', () => {
    expect(checkLimit('NIGHT_OUT', null, '1', 1, null)).toBeNull();
    expect(checkLimit('NIGHT_OUT', null, '', 1, limits)).toBeNull();
  });
});

describe('toCents', () => {
  it('avoids float error', () => {
    expect(toCents('0.1') + toCents('0.2')).toBe(30);
    expect(toCents('1500.00')).toBe(150000);
    expect(toCents('-12.5')).toBe(-1250);
  });
});

describe('findOverlap', () => {
  const base = { type: 'NIGHT_OUT' as const, status: 'PENDING_PM' as const };
  const existing = [
    { ...base, id: 1, number: 'AR-1', from_date: '2026-03-01', to_date: '2026-03-03' },
    { ...base, id: 2, number: 'AR-2', from_date: '2026-03-02', to_date: '2026-03-05' },
  ];
  it('finds the earlier of several overlaps', () => {
    const hit = findOverlap(
      { type: 'NIGHT_OUT', from_date: '2026-03-03', to_date: '2026-03-04' },
      [existing[1], existing[0]],
    );
    expect(hit?.number).toBe('AR-1');
  });
  it('counts the boundary day', () => {
    const hit = findOverlap(
      { type: 'NIGHT_OUT', from_date: '2026-03-05', to_date: '2026-03-06' },
      existing,
    );
    expect(hit?.number).toBe('AR-2');
  });
  it('lets the next day through', () => {
    expect(
      findOverlap({ type: 'NIGHT_OUT', from_date: '2026-03-06', to_date: '2026-03-06' }, existing),
    ).toBeNull();
  });
  it('ignores other types and rejected requests', () => {
    const list = [
      { ...existing[0], type: 'TEAM_ALLOWANCE' as const },
      { ...existing[1], status: 'REJECTED' as const },
    ];
    expect(
      findOverlap({ type: 'NIGHT_OUT', from_date: '2026-03-01', to_date: '2026-03-05' }, list),
    ).toBeNull();
  });
  it('exempts FLOAT and OTHER', () => {
    const list = [{ ...existing[0], type: 'FLOAT' as const }];
    expect(
      findOverlap({ type: 'FLOAT', from_date: '2026-03-01', to_date: '2026-03-03' }, list),
    ).toBeNull();
  });
  it('does not clash with itself when editing', () => {
    expect(
      findOverlap(
        { id: 1, type: 'NIGHT_OUT', from_date: '2026-03-01', to_date: '2026-03-03' },
        [existing[0]],
      ),
    ).toBeNull();
  });
  it('counts PAID requests', () => {
    const list = [{ ...existing[0], status: 'PAID' as const }];
    expect(
      findOverlap({ type: 'NIGHT_OUT', from_date: '2026-03-01', to_date: '2026-03-01' }, list),
    ).not.toBeNull();
  });
});

describe('floatBalance', () => {
  it('subtracts live expenses and the returned amount', () => {
    const spent = [
      { amount: '300.50', status: 'PENDING_PM' as const },
      { amount: '200', status: 'PAID' as const },
      { amount: '999', status: 'REJECTED' as const },
    ];
    expect(floatBalance('1000', spent, '100')).toBe('399.50');
  });
  it('goes negative on overspend', () => {
    expect(floatBalance('100', [{ amount: '150.25', status: 'APPROVED' }], null)).toBe('-50.25');
  });
  it('is the amount when nothing was spent', () => {
    expect(floatBalance('1500', [], undefined)).toBe('1500.00');
  });
});

describe('candidateProjects', () => {
  const projects = [
    { id: 1, status: 'OPEN', sites: [7] },
    { id: 2, status: 'OPEN', sites: [7, 8] },
    { id: 3, status: 'CLOSED', sites: [7] },
    { id: 4, status: 'OPEN', sites: [9] },
  ];
  it('returns every open project on an ambiguous site', () => {
    expect(candidateProjects(7, projects).map((p) => p.id)).toEqual([1, 2]);
  });
  it('returns one when the site is unambiguous', () => {
    expect(candidateProjects(9, projects).map((p) => p.id)).toEqual([4]);
  });
  it('returns none for an unknown site', () => {
    expect(candidateProjects(99, projects)).toEqual([]);
  });
});

describe('normaliseIdNumber', () => {
  it('upper-cases and strips spaces and dashes', () => {
    expect(normaliseIdNumber(' ab-12 34 c ')).toBe('AB1234C');
  });
});

describe('statusLabel', () => {
  it('speaks plainly', () => {
    expect(statusLabel('PENDING_PM')).toBe('Waiting for PM');
    expect(statusLabel('PENDING_FINANCE')).toBe('Waiting for Finance');
    expect(statusLabel('APPROVED')).toBe('Approved');
    expect(statusLabel('PAID')).toBe('Paid');
    expect(statusLabel('REJECTED')).toBe('Rejected');
  });
});
