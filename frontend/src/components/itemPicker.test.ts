import { describe, expect, it } from 'vitest';

import { pushRecent, readRecent, rankItems, shownText } from './itemPicker';

const ITEMS = [
  { id: 1, name: 'Antenna for RRU', code: 'ANT-1', description: '' },
  { id: 2, name: 'RRU 2x40W', code: 'R-2', description: '' },
  { id: 3, name: 'Bracket', code: 'RRU-BRK', description: '' },
  { id: 4, name: 'Mount', code: 'MNT', description: 'fits an rru unit' },
  { id: 5, name: 'Cable', code: 'CBL', description: '' },
  { id: 6, name: 'RRU old', code: 'R-0', description: '', is_archived: true },
];

function fakeStorage(fail = false) {
  const data = new Map<string, string>();
  return {
    getItem: (k: string) => {
      if (fail) throw new Error('blocked');
      return data.get(k) ?? null;
    },
    setItem: (k: string, v: string) => {
      if (fail) throw new Error('blocked');
      data.set(k, v);
    },
  };
}

describe('rankItems', () => {
  it('orders name-starts, name-contains, code, description', () => {
    const { matches, total } = rankItems(ITEMS, 'rru');
    expect(matches.map((m) => m.name)).toEqual([
      'RRU 2x40W',
      'Antenna for RRU',
      'Bracket',
      'Mount',
    ]);
    expect(total).toBe(4);
  });

  it('is case-insensitive and excludes archived', () => {
    expect(rankItems(ITEMS, 'RrU').matches.map((m) => m.id)).not.toContain(6);
  });

  it('returns nothing for empty text', () => {
    expect(rankItems(ITEMS, '  ')).toEqual({ matches: [], total: 0 });
  });

  it('limits matches but reports the total', () => {
    const many = Array.from({ length: 30 }, (_, i) => ({ id: i + 1, name: `Pipe ${i}` }));
    const { matches, total } = rankItems(many, 'pipe');
    expect(matches).toHaveLength(20);
    expect(total).toBe(30);
    expect(rankItems(many, 'pipe', 5).matches).toHaveLength(5);
  });

  it('breaks ties by name then id', () => {
    const items = [
      { id: 2, name: 'Bolt' },
      { id: 1, name: 'Bolt' },
      { id: 3, name: 'Bolt A' },
    ];
    expect(rankItems(items, 'bolt').matches.map((m) => m.id)).toEqual([1, 2, 3]);
  });
});

describe('recent items', () => {
  it('puts newest first and dedupes by id', () => {
    const store = fakeStorage();
    pushRecent('acme', { id: 1, name: 'A' }, store);
    pushRecent('acme', { id: 2, name: 'B' }, store);
    pushRecent('acme', { id: 1, name: 'A' }, store);
    expect(readRecent('acme', store).map((r) => r.id)).toEqual([1, 2]);
  });

  it('keeps the last 8 and separates tenants', () => {
    const store = fakeStorage();
    for (let i = 1; i <= 10; i += 1) pushRecent('acme', { id: i, name: `I${i}` }, store);
    const recent = readRecent('acme', store);
    expect(recent).toHaveLength(8);
    expect(recent[0].id).toBe(10);
    expect(readRecent('other', store)).toEqual([]);
  });

  it('survives blocked storage', () => {
    const store = fakeStorage(true);
    expect(() => pushRecent('acme', { id: 1, name: 'A' }, store)).not.toThrow();
    expect(readRecent('acme', store)).toEqual([]);
    expect(readRecent('acme', null)).toEqual([]);
  });
});

describe('shownText', () => {
  it('is empty when all are shown', () => {
    expect(shownText(5, 5)).toBe('');
  });
  it('says how many of how many', () => {
    expect(shownText(20, 340)).toBe('20 of 340 shown — keep typing');
  });
});
