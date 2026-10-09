import { describe, expect, it } from 'vitest';

import { dateInFuture, expiryStatus, monthRange, normaliseTag, recentMonths } from './rules';

describe('expiryStatus', () => {
  it('is none for a blank date', () => {
    expect(expiryStatus(null, '2026-10-09').tone).toBe('none');
  });
  it('is ok beyond 30 days', () => {
    expect(expiryStatus('2026-11-09', '2026-10-09').tone).toBe('ok');
  });
  it('is soon on the 30-day edge', () => {
    expect(expiryStatus('2026-11-08', '2026-10-09')).toMatchObject({ tone: 'soon', days: 30 });
  });
  it('says today', () => {
    expect(expiryStatus('2026-10-09', '2026-10-09').label).toBe('Expires today');
  });
  it('is lapsed the day after', () => {
    expect(expiryStatus('2026-10-08', '2026-10-09')).toMatchObject({ tone: 'lapsed', days: -1 });
  });
});

describe('normaliseTag', () => {
  it('strips spaces and dashes', () => {
    expect(normaliseTag('kca 123-a')).toBe('KCA123A');
  });
});

describe('dateInFuture', () => {
  it('refuses tomorrow only', () => {
    expect(dateInFuture('2026-10-10', '2026-10-09')).toBe(true);
    expect(dateInFuture('2026-10-09', '2026-10-09')).toBe(false);
  });
});

describe('monthRange', () => {
  it('handles a leap February', () => {
    expect(monthRange('2028-02')).toEqual({ from: '2028-02-01', to: '2028-02-29' });
  });
});

describe('recentMonths', () => {
  it('crosses a year boundary', () => {
    expect(recentMonths(3, new Date(2026, 0, 15))).toEqual(['2026-01', '2025-12', '2025-11']);
  });
});
