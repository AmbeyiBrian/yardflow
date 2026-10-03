import { describe, expect, it } from 'vitest';

import {
  buildBulkChangeBody,
  bulkChangeProblem,
  bulkSourceOptions,
  earmarkLine,
  earmarkSplitText,
} from './earmarkHelpers';

const row = {
  node: 4,
  item_type: 9,
  owner_client: null,
  condition: 'NEW',
  earmarked: [
    { site: 1, name: 'Site Alpha', quantity: '25.000' },
    { site: 2, name: 'Site Bravo', quantity: '10.000' },
  ],
  free: '15.000',
};

describe('earmarkSplitText', () => {
  it('says each site and what is free', () => {
    expect(earmarkSplitText(row)).toBe('25 for Site Alpha · 10 for Site Bravo · 15 free');
  });
  it('omits free when there is none', () => {
    expect(earmarkSplitText({ earmarked: row.earmarked, free: '0.000' })).toBe(
      '25 for Site Alpha · 10 for Site Bravo',
    );
  });
  it('is null with nothing earmarked or an older server', () => {
    expect(earmarkSplitText({ earmarked: [], free: '5' })).toBeNull();
    expect(earmarkSplitText({})).toBeNull();
  });
});

describe('bulk change', () => {
  const options = bulkSourceOptions(row);
  it('offers each earmarked site and free', () => {
    expect(options.map((o) => [o.key, o.max])).toEqual([
      ['1', 25],
      ['2', 10],
      ['free', 15],
    ]);
  });
  it('checks the form', () => {
    const ok = { from: '1', to: '2', quantity: '5', reason: 'Plan changed' };
    expect(bulkChangeProblem(options, ok)).toBeNull();
    expect(bulkChangeProblem(options, { ...ok, quantity: '30' })).toMatch(/Only 25/);
    expect(bulkChangeProblem(options, { ...ok, quantity: '0' })).toMatch(/above zero/);
    expect(bulkChangeProblem(options, { ...ok, reason: ' ' })).toMatch(/why/);
    expect(bulkChangeProblem(options, { ...ok, to: '1' })).toMatch(/already/);
    expect(bulkChangeProblem(options, { ...ok, from: 'free', to: '' })).toMatch(/already/);
  });
  it('builds the body', () => {
    expect(
      buildBulkChangeBody(row, { from: 'free', to: '2', quantity: '5', reason: ' r ' }),
    ).toEqual({
      node: 4,
      item_type: 9,
      owner_client: null,
      condition: 'NEW',
      from_site: null,
      quantity: '5',
      to_site: 2,
      reason: 'r',
    });
    expect(
      buildBulkChangeBody(row, { from: '1', to: '', quantity: '5', reason: 'r' }).to_site,
    ).toBeNull();
  });
});

describe('earmarkLine', () => {
  it('reads both ways', () => {
    expect(earmarkLine('Site X')).toBe('Earmarked for Site X');
    expect(earmarkLine(null)).toBe('Not earmarked');
  });
});
