/**
 * Pure helpers for gate-in's supplier picker (design §4.20.5; R15).
 *
 * A PENDING supplier may be received from (the goods still arrive); a REJECTED
 * or inactive one may not. Paying is gated elsewhere (`assert_payable`).
 */

import type { SupplierStatus } from '../settings/supplierRules';

export interface PickableSupplier {
  id: number;
  name: string;
  status: SupplierStatus;
  is_active: boolean;
}

export function selectableSuppliers<T extends PickableSupplier>(rows: T[]): T[] {
  return rows.filter((s) => s.is_active && s.status !== 'REJECTED');
}

export function supplierOptionLabel(s: Pick<PickableSupplier, 'name' | 'status'>): string {
  return s.status === 'PENDING' ? `${s.name} (awaiting approval)` : s.name;
}

/** `supplier_name` kept for display and back-compat (§4.20.5). */
export function supplierNameFor(rows: PickableSupplier[], id: string, fallback: string): string {
  if (!id) return fallback;
  return rows.find((s) => String(s.id) === id)?.name ?? fallback;
}
