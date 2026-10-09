/**
 * The pure parts of an offline site purchase (design §4.19.11; R6, R7, R9).
 *
 * The queued `SITE_PURCHASE` payload is the online body plus `client_uuid`
 * (added by `queuePurchase`), with the supplier named by server id or by
 * `supplier_client_uuid` when it was added on this same phone. Lines travel
 * inline; receipt photos go up afterwards against `commercials.SitePurchase`.
 * Replay never refuses for over-budget, it only flags (§4.19.5), so the form
 * sends whatever reason was typed and no more.
 */

import { supplierRef } from './suppliersOffline';
import type { SitePurchaseInput } from './purchasesApi';

export type QueuedPurchaseBody = Omit<SitePurchaseInput, 'supplier' | 'client_uuid'> & {
  supplier?: number | null;
  supplier_client_uuid?: string;
};

/** The body for the queue from the form's answers; `supplierValue` is a server id or `q:<uuid>`. */
export function purchaseBody(
  input: Omit<SitePurchaseInput, 'supplier' | 'client_uuid'>,
  supplierValue: string,
): QueuedPurchaseBody {
  return { ...input, ...supplierRef(supplierValue) };
}

/** True when the purchase names a supplier that is itself still queued: it cannot go online. */
export function needsQueue(body: Pick<QueuedPurchaseBody, 'supplier_client_uuid'>): boolean {
  return Boolean(body.supplier_client_uuid);
}
