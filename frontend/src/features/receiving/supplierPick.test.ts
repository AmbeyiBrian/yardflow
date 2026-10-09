import { describe, expect, it } from 'vitest';

import { selectableSuppliers, supplierNameFor, supplierOptionLabel } from './supplierPick';

const rows = [
  { id: 1, name: 'A', status: 'APPROVED', is_active: true },
  { id: 2, name: 'B', status: 'PENDING', is_active: true },
  { id: 3, name: 'C', status: 'REJECTED', is_active: true },
  { id: 4, name: 'D', status: 'APPROVED', is_active: false },
] as const;

describe('gate-in supplier picker (§4.20.5)', () => {
  it('keeps pending, drops rejected and inactive', () => {
    expect(selectableSuppliers([...rows]).map((s) => s.id)).toEqual([1, 2]);
  });
  it('labels pending as awaiting approval', () => {
    expect(supplierOptionLabel(rows[1])).toBe('B (awaiting approval)');
    expect(supplierOptionLabel(rows[0])).toBe('A');
  });
  it('fills the display name from the pick', () => {
    expect(supplierNameFor([...rows], '2', 'old')).toBe('B');
    expect(supplierNameFor([...rows], '', 'typed')).toBe('typed');
  });
});
