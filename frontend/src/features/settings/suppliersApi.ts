/**
 * Supplier register endpoints (design §4.20.6; R15). Resources carry no
 * `/api/v1` prefix; the client adds it.
 *
 * PIN and payment fields are omitted by the server for anyone but
 * `finance.approve` and the registrar (§4.20.7), so they are optional here.
 */

import { useAction, useDetail, useList, type QueryParams } from '../../api/hooks';
import type { SupplierStatus } from './supplierRules';

const SUPPLIERS = 'suppliers';

export interface Supplier {
  id: number;
  name: string;
  kra_pin?: string;
  contact_name: string;
  phone: string;
  email?: string;
  address?: string;
  bank_name?: string;
  account_number?: string;
  mpesa_type?: 'PAYBILL' | 'TILL' | '';
  mpesa_number?: string;
  mpesa_account?: string;
  status: SupplierStatus;
  is_active: boolean;
  registered_by?: number | null;
  registered_by_name?: string;
  decision_reason?: string;
}

export type SupplierInput = Partial<
  Omit<Supplier, 'id' | 'status' | 'is_active' | 'registered_by' | 'registered_by_name'>
> & { name: string; client_uuid?: string };

export interface SupplierParams extends QueryParams {
  status?: string;
  is_active?: boolean;
  search?: string;
  /** Usable only (§4.20.6). */
  payable?: boolean;
}

export const useSuppliers = (params?: SupplierParams) =>
  useList<Supplier>(SUPPLIERS, { page_size: 100, ...params });

export const useSupplier = (id: number | undefined) => useDetail<Supplier>(SUPPLIERS, id);

export const useCreateSupplier = () =>
  useAction<SupplierInput, Supplier>({ resource: SUPPLIERS });

export const useUpdateSupplier = () =>
  useAction<SupplierInput & { id: number }, Supplier>({
    resource: SUPPLIERS,
    method: 'patch',
    path: (b) => String(b.id),
  });

export const useResubmitSupplier = () =>
  useAction<{ id: number }, Supplier>({
    resource: SUPPLIERS,
    path: (b) => `${b.id}/resubmit`,
    invalidates: [SUPPLIERS, 'approvals/pending'],
  });

export const useSetSupplierActive = () =>
  useAction<{ id: number; active: boolean }, Supplier>({
    resource: SUPPLIERS,
    path: (b) => `${b.id}/${b.active ? 'reactivate' : 'deactivate'}`,
  });

export const useLinkSupplierHistory = () =>
  useAction<{ id: number }, { linked: number }>({
    resource: SUPPLIERS,
    path: (b) => `${b.id}/link-history`,
    invalidates: ['gate-ins'],
  });
