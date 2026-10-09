/**
 * Pure supplier helpers (design §4.20.3, §4.20.10; R15).
 */

import { ApiError } from '../../api/client';

export type SupplierStatus = 'PENDING' | 'APPROVED' | 'REJECTED';

/** What the chip shows: inactive beats the approval status (§4.20.2 "usable"). */
export type SupplierChip = 'Pending' | 'Approved' | 'Rejected' | 'Inactive';

export function supplierChip(s: { status: SupplierStatus; is_active: boolean }): SupplierChip {
  if (!s.is_active) return 'Inactive';
  if (s.status === 'APPROVED') return 'Approved';
  if (s.status === 'REJECTED') return 'Rejected';
  return 'Pending';
}

/** Upper-cased, whitespace stripped: the server's `kra_pin_key` rule (§4.20.2). */
export function normalisePin(pin: string): string {
  return pin.replace(/\s+/g, '').toUpperCase();
}

export interface ExistingSupplier {
  id: number;
  name: string;
  status?: string;
}

/**
 * The supplier a duplicate refusal names (`SUPPLIER_PIN_DUPLICATE`,
 * `SUPPLIER_NAME_DUPLICATE`, §4.20.3), so the form can offer "Use <existing>
 * instead". Null for any other error.
 */
export function duplicateOf(
  error: unknown,
): { kind: 'pin' | 'name'; existing: ExistingSupplier } | null {
  if (!(error instanceof ApiError)) return null;
  const kind =
    error.code === 'SUPPLIER_PIN_DUPLICATE'
      ? 'pin'
      : error.code === 'SUPPLIER_NAME_DUPLICATE'
        ? 'name'
        : null;
  if (!kind) return null;
  const raw = (error.details.existing ?? error.details.supplier ?? error.details) as Record<
    string,
    unknown
  >;
  const id = Number(raw.id ?? raw.supplier_id);
  if (!Number.isFinite(id) || id <= 0) return null;
  return {
    kind,
    existing: {
      id,
      name: String(raw.name ?? 'an existing supplier'),
      status: raw.status as string | undefined,
    },
  };
}

/** Document kinds, carried in the attachment caption (§4.20.2). */
export const SUPPLIER_DOCUMENT_KINDS = [
  'KRA certificate',
  'Certificate of incorporation',
  'Other',
] as const;
