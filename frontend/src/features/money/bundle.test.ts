import { describe, expect, it } from 'vitest';

import {
  bundleCategories,
  bundleFloats,
  bundleLimits,
  bundleOpenProjects,
  bundleSiteProjects,
  overHeadroom,
  type BundleSite,
} from './bundle';

const p = (id: number, reference: string) => ({
  id,
  reference,
  po_number: `PO-${id}`,
  title: `T${id}`,
  label: `${reference} T${id}`,
});

const sites: BundleSite[] = [
  { id: 1, name: 'A', internal_ref: 'S1', open_projects: [p(10, 'P-010')] },
  { id: 2, name: 'B', internal_ref: 'S2', open_projects: [p(11, 'P-011'), p(10, 'P-010')] },
  { id: 3, name: 'C', internal_ref: 'S3', open_projects: [] },
  { id: 4, name: 'D', internal_ref: 'S4' },
];

describe('bundle projects', () => {
  it('lists the open projects of one site', () => {
    expect(bundleSiteProjects(sites, 1).map((x) => x.id)).toEqual([10]);
    expect(bundleSiteProjects(sites, 2).map((x) => x.id)).toEqual([11, 10]);
  });

  it('is empty for a site with none, an older bundle row, or an unknown site', () => {
    expect(bundleSiteProjects(sites, 3)).toEqual([]);
    expect(bundleSiteProjects(sites, 4)).toEqual([]);
    expect(bundleSiteProjects(sites, 99)).toEqual([]);
  });

  it('fills missing PO and title with empty text', () => {
    const [only] = bundleSiteProjects(
      [{ id: 5, name: 'E', internal_ref: 'S5', open_projects: [{ id: 7, reference: 'P-7' }] }],
      5,
    );
    expect(only).toEqual({ id: 7, reference: 'P-7', po_number: '', title: '' });
  });

  it('unions open projects once each, in reference order', () => {
    expect(bundleOpenProjects(sites).map((x) => x.id)).toEqual([10, 11]);
  });
});

describe('bundle lists', () => {
  it('keeps a category to its name and kind', () => {
    const rows = [{ id: 1, name: 'Fuel', kind: 'FUEL' as const, extra: 1 }];
    expect(bundleCategories(rows)).toEqual([{ id: 1, name: 'Fuel', kind: 'FUEL' }]);
  });

  it('carries each float balance, falling back to its amount', () => {
    expect(
      bundleFloats([
        { id: 1, number: 'AR-1', balance: '40.00' },
        { id: 2, number: 'AR-2', balance: undefined as unknown as string, amount: '500.00' },
      ]),
    ).toEqual([
      { id: 1, number: 'AR-1', balance: '40.00' },
      { id: 2, number: 'AR-2', balance: '500.00' },
    ]);
  });

  it('reads the stored limits row, and nothing when it is empty or missing', () => {
    const limits = { NIGHT_OUT: { min: null, max: '3000.00' } };
    expect(bundleLimits([limits])).toEqual(limits);
    expect(bundleLimits([{}])).toBeUndefined();
    expect(bundleLimits([])).toBeUndefined();
  });
});

describe('overHeadroom', () => {
  const rows = [
    { id: 10, headroom: '1000.00' },
    { id: 11, headroom: '-50.00' },
  ];

  it('is over only when the amount passes the headroom', () => {
    expect(overHeadroom(rows, 10, '1000.01')).toBe(true);
    expect(overHeadroom(rows, 10, '1000')).toBe(false);
    expect(overHeadroom(rows, 10, '999.99')).toBe(false);
  });

  it('is over for any positive amount once the budget is already passed', () => {
    expect(overHeadroom(rows, 11, '0.01')).toBe(true);
  });

  it('cannot tell without a figure or a usable amount', () => {
    expect(overHeadroom(rows, 99, '5')).toBeNull();
    expect(overHeadroom([], 10, '5')).toBeNull();
    expect(overHeadroom(rows, 10, '')).toBeNull();
    expect(overHeadroom(rows, 10, 'abc')).toBeNull();
  });
});
