/**
 * Pure rules behind the Purchases approvals and To pay screens
 * (design §4.19.3, §4.19.10, §4.19.13; R7, R8, R15).
 */

import type { SitePurchase, SupplierChoice } from './purchasesApi';

/** R15: a PENDING supplier can be bought from and approved, but not paid. */
export function supplierApproved(status: SupplierChoice['status'] | undefined): boolean {
  return status === 'APPROVED';
}

/** The "awaiting approval" note beside a supplier's name; empty when nothing is awaited. */
export function supplierNote(status: SupplierChoice['status'] | undefined): string {
  if (status === 'PENDING') return 'awaiting approval';
  if (status === 'REJECTED') return 'rejected';
  return '';
}

/** §4.19.10: Mark paid is refused (`SUPPLIER_NOT_APPROVED`) until the supplier is approved. */
export function payBlockedReason(purchase: Pick<SitePurchase, 'supplier_status'>): string {
  return supplierApproved(purchase.supplier_status)
    ? ''
    : "Supplier awaiting approval — can't pay yet";
}

/** §4.19.3: only an INTO_YARD purchase makes a draft delivery on approval. */
export function willCreateDelivery(purchase: Pick<SitePurchase, 'destination'>): boolean {
  return purchase.destination === 'INTO_YARD';
}

/** R8: a subcontract payment has one level, the PM's. */
export function paymentLevelLabel(status: string): string {
  if (status === 'PENDING_PM') return 'Waiting for the project manager';
  if (status === 'APPROVED') return 'Approved';
  if (status === 'REJECTED') return 'Rejected';
  return status;
}
