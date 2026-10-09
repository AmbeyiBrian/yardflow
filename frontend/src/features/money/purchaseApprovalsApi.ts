/**
 * Approval and payment calls for site purchases and subcontract payments
 * (design §4.19.10; R7, R8). Resources carry no `/api/v1` prefix.
 */

import { useAction, useList, type QueryParams } from '../../api/hooks';
import type { DecideBody, MarkPaidBody } from './api';
import type { SitePurchase } from './purchasesApi';

const PURCHASES = 'site-purchases';
const PAYMENTS = 'subcontract-payments';
const AFTER = [PURCHASES, PAYMENTS, 'approvals/pending'];

export interface SubcontractPayment {
  id: number;
  subcontract: number;
  subcontract_number?: string;
  subcontractor_name?: string;
  project_reference?: string;
  amount: string;
  paid_on: string;
  reference: string;
  status: 'PENDING_PM' | 'APPROVED' | 'REJECTED';
  recorded_by: number;
  recorded_by_name?: string;
  decision_reason: string;
}

/** Waiting on the caller at their level (PM, then Finance), decided server-side. */
export const usePendingPurchases = (params?: QueryParams) =>
  useList<SitePurchase>(`${PURCHASES}/pending`, params);

export const useDecidePurchase = () =>
  useAction<DecideBody, SitePurchase>({
    resource: PURCHASES,
    path: (b) => `${b.id}/decide`,
    invalidates: AFTER,
  });

export const useMarkPurchasePaid = () =>
  useAction<MarkPaidBody, SitePurchase>({
    resource: PURCHASES,
    path: (b) => `${b.id}/mark-paid`,
    invalidates: AFTER,
  });

/** R8: the PM's list; Finance enters the payments. */
export const usePendingSubcontractPayments = (params?: QueryParams) =>
  useList<SubcontractPayment>(PAYMENTS, { ...params, pending: true });

export const useDecideSubcontractPayment = () =>
  useAction<DecideBody, SubcontractPayment>({
    resource: PAYMENTS,
    path: (b) => `${b.id}/decide`,
    invalidates: AFTER,
  });
