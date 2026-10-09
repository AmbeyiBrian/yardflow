/**
 * Site-purchase endpoints (design §4.19.10, §4.20.6; R7, R9), as thin hooks.
 * Resources carry no `/api/v1` prefix; the client adds it.
 */

import { useAction, useDetail, useList, type QueryParams } from '../../api/hooks';
import type { PurchaseDestination } from './purchaseRules';
import type { ExpenseStatus } from './types';

const PURCHASES = 'site-purchases';
const PENDING = ['approvals/pending'];

export interface SupplierChoice {
  id: number;
  name: string;
  status: 'PENDING' | 'APPROVED' | 'REJECTED';
  is_active: boolean;
  kra_pin?: string;
}

export interface SitePurchaseLine {
  id?: number;
  item_type: number | null;
  item_type_name?: string;
  description: string;
  quantity: string;
  uom?: string;
  unit_price: string;
  line_total?: string;
}

export interface SitePurchase {
  id: number;
  /** Series SP. */
  number: string;
  project: number;
  project_reference?: string;
  site: number;
  site_name?: string;
  supplier: number;
  supplier_name?: string;
  supplier_status?: SupplierChoice['status'];
  purchase_date: string;
  destination: PurchaseDestination;
  receive_into: number | null;
  receive_into_name?: string;
  lines: SitePurchaseLine[];
  amount: string;
  status: ExpenseStatus;
  decision_reason: string;
  decided_at: string | null;
  paid_at: string | null;
  payment_reference: string;
  gate_in: number | null;
  /** R9: the recorder sees their own reason, not the figures. */
  is_over_budget?: boolean;
  over_budget_reason?: string;
  over_budget_by?: string | null;
  photos_expected: number;
  client_uuid: string | null;
  recorded_by_name?: string;
  created_at?: string;
}

export interface SitePurchaseInput {
  project: number;
  site: number;
  supplier: number;
  purchase_date: string;
  destination: PurchaseDestination;
  receive_into?: number | null;
  lines: { item_type: number | null; description: string; quantity: string; unit_price: string }[];
  over_budget_reason?: string;
  photos_expected?: number;
  client_uuid?: string;
}

export const useSitePurchases = (params?: QueryParams & { mine?: boolean }) =>
  useList<SitePurchase>(PURCHASES, params);

export const useSitePurchase = (id: string | number | undefined) =>
  useDetail<SitePurchase>(PURCHASES, id);

/** `queuePurchase` (T18.17) slots in beside this call, as `queueExpense` does. */
export const useCreateSitePurchase = () =>
  useAction<SitePurchaseInput, SitePurchase>({
    resource: PURCHASES,
    invalidates: [PURCHASES, ...PENDING],
  });

export const useResubmitSitePurchase = () =>
  useAction<{ id: number | string }, SitePurchase>({
    resource: PURCHASES,
    path: (b) => `${b.id}/resubmit`,
    invalidates: [PURCHASES, ...PENDING],
  });

/** Suppliers a purchase may name: active and not rejected (a PENDING one is fine to buy from, not to pay). */
export function useSuppliers(enabled = true) {
  return useList<SupplierChoice>('suppliers', { is_active: true, page_size: 200 }, { enabled });
}

/** R9's early warning: `{amount}` gives `{over, over_by?}`. Members get the boolean only. */
export const useBudgetCheck = () =>
  useAction<{ project: number | string; amount: string }, { over: boolean; over_by?: string }>({
    resource: 'projects',
    path: (b) => `${b.project}/budget-check`,
    invalidates: [],
  });
