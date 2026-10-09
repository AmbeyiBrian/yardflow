import { describe, expect, it } from 'vitest';

import { nothingMatches, resolveFind, searchable } from './findStockLogic';

describe('resolveFind', () => {
  it('goes to the unit on a hit, whichever way it was entered', () => {
    expect(resolveFind('scan', 'SN1', '/stock/serials/4', 0)).toEqual({
      kind: 'navigate',
      to: '/stock/serials/4',
    });
    expect(resolveFind('enter', 'SN1', '/stock/serials/4', 3)).toEqual({
      kind: 'navigate',
      to: '/stock/serials/4',
    });
  });

  it('says nothing matches on a scan miss, even with items listed', () => {
    expect(resolveFind('scan', 'XYZ', null, 5)).toEqual({
      kind: 'message',
      text: nothingMatches('XYZ'),
    });
  });

  it('keeps the list on an Enter miss when it has items', () => {
    expect(resolveFind('enter', 'rect', null, 2)).toEqual({ kind: 'message', text: null });
  });

  it('says nothing matches on an Enter miss when the list is empty', () => {
    expect(resolveFind('enter', 'zzz', null, 0)).toEqual({
      kind: 'message',
      text: 'Nothing here matches "zzz".',
    });
  });
});

describe('searchable', () => {
  it('needs two characters', () => {
    expect(searchable('a')).toBe(false);
    expect(searchable(' a ')).toBe(false);
    expect(searchable('ab')).toBe(true);
  });
});
