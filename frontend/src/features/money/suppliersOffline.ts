/**
 * The pure parts of offline suppliers (design §4.20.8; R15, R6).
 *
 * Offline, the supplier pickers on gate-in and on a purchase read the bundle's
 * `suppliers` (active, not REJECTED: id, name, status). A supplier added with no
 * signal is a queued `SUPPLIER` operation; until it lands, a gate-in or a
 * purchase names it by `supplier_client_uuid`, the way an expense names a queued
 * casual. The queue replays in capture order, so the supplier reaches the server
 * first.
 */

import type { QueuedFinanceEntry } from '../../offline/financeQueue';

/** A supplier as the bundle stores it. No PIN, no payment data. */
export interface BundleSupplier {
  id: number;
  name: string;
  status: 'PENDING' | 'APPROVED' | 'REJECTED';
}

export interface SupplierOption {
  /** A server id, or `q:<client_uuid>` for a supplier still on this phone. */
  value: string;
  label: string;
  /** The bare name, kept on a gate-in as `supplier_name` for display (§4.20.5). */
  name: string;
  queued: boolean;
}

const QUEUED_PREFIX = 'q:';

const awaiting = (s: Pick<BundleSupplier, 'name' | 'status'>) =>
  s.status === 'PENDING' ? `${s.name} (awaiting approval)` : s.name;

/** Server or bundle rows as options. A REJECTED one is never offered (§4.20.5). */
export function supplierOptions(rows: readonly BundleSupplier[]): SupplierOption[] {
  return rows
    .filter((s) => s.status !== 'REJECTED')
    .map((s) => ({ value: String(s.id), label: awaiting(s), name: s.name, queued: false }));
}

/** Suppliers captured on this phone and not yet refused. */
export function queuedSupplierOptions(entries: readonly QueuedFinanceEntry[]): SupplierOption[] {
  return entries
    .filter((e) => e.kind === 'SUPPLIER' && e.state === 'WAITING')
    .map((e) => {
      const name = String(e.payload.name ?? '');
      return {
        value: `${QUEUED_PREFIX}${e.client_uuid}`,
        label: `${name} (waiting to send)`,
        name,
        queued: true,
      };
    });
}

/** Server rows first, then queued ones, without repeating a value. */
export function mergeSupplierOptions(
  ...groups: readonly (readonly SupplierOption[])[]
): SupplierOption[] {
  const seen = new Map<string, SupplierOption>();
  for (const option of groups.flat()) if (!seen.has(option.value)) seen.set(option.value, option);
  return [...seen.values()];
}

export const isQueuedSupplier = (value: string): boolean => value.startsWith(QUEUED_PREFIX);

/** What a picked option sends: a server id, or the queued supplier's uuid. */
export function supplierRef(value: string): {
  supplier: number | null;
  supplier_client_uuid?: string;
} {
  if (!value) return { supplier: null };
  return isQueuedSupplier(value)
    ? { supplier: null, supplier_client_uuid: value.slice(QUEUED_PREFIX.length) }
    : { supplier: Number(value) };
}

/** The reverse, for filling a picker from a queued payload ("Fix and resend"). */
export function supplierValue(payload: {
  supplier?: unknown;
  supplier_client_uuid?: unknown;
}): string {
  if (payload.supplier_client_uuid) return `${QUEUED_PREFIX}${String(payload.supplier_client_uuid)}`;
  return payload.supplier ? String(payload.supplier) : '';
}

export interface SupplierDraft {
  name: string;
  contact_name?: string;
  phone?: string;
  kra_pin?: string;
}

/**
 * The `SUPPLIER` payload (without `client_uuid`, which `queueSupplier` adds).
 * Trimmed, blank keys dropped, the PIN upper-cased; the server's `add_supplier`
 * runs the duplicate-name and duplicate-PIN checks on replay (§4.20.8).
 */
export function supplierBody(draft: SupplierDraft): Record<string, string> {
  const body: Record<string, string> = { name: draft.name.trim() };
  const contact = draft.contact_name?.trim();
  const phone = draft.phone?.trim();
  const pin = draft.kra_pin?.trim().toUpperCase();
  if (contact) body.contact_name = contact;
  if (phone) body.phone = phone;
  if (pin) body.kra_pin = pin;
  return body;
}

/** The supplier part of a gate-in payload (§4.20.5, §4.20.8). */
export function gateInSupplierFields(
  value: string,
  name: string,
): { supplier: number | null; supplier_client_uuid?: string; supplier_name: string } {
  return { ...supplierRef(value), supplier_name: name };
}
