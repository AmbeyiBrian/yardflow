/**
 * Subcontract endpoints (design §4.19.4, §4.19.10; R8).
 *
 * The design fixes the endpoints, the figures and the error codes; the JSON
 * keys below are the frontend's reading of them. Money is a decimal string.
 */

import { useAction, useList, useResource } from '../../api/hooks';

export type SubcontractStatus = 'ACTIVE' | 'COMPLETED' | 'CANCELLED';
export type PaymentStatus = 'PENDING_PM' | 'APPROVED' | 'REJECTED' | 'REVERSED';

/** `contracts.position` (§4.19.4). Withheld (undefined) without `project.view_cost`. */
export interface SubcontractPosition {
  contract_value: string;
  work_done: string;
  paid: string;
  /** Recorded and awaiting the PM; outside `paid`. */
  awaiting_approval?: string;
  /** Work done minus paid. Negative reads "advance paid". */
  owed: string;
  /** Sum of agreed price of non-cancelled jobs (open or closed). */
  committed?: string;
  paid_exceeds_work_done?: boolean;
  paid_exceeds_contract_value?: boolean;
}

export interface SubcontractJob {
  id: number;
  reference?: string;
  description?: string;
  status: string;
  agreed_price: string | null;
  over_contract_reason?: string;
}

export interface SubcontractPayment {
  id: number;
  subcontract: number;
  /** Negative on a reversal. */
  amount: string;
  paid_on: string;
  reference: string;
  status: PaymentStatus;
  rejection_reason?: string;
  reverses?: number | null;
}

export interface Subcontract {
  id: number;
  reference: string;
  project: number;
  subcontractor: number;
  subcontractor_name?: string;
  /** Sites covered; each must belong to the project. */
  sites: number[];
  contract_value: string;
  payment_terms: string;
  status: SubcontractStatus;
  position?: SubcontractPosition;
  jobs?: SubcontractJob[];
  payments?: SubcontractPayment[];
}

export interface SubcontractInput {
  id?: number;
  project?: number;
  subcontractor?: number;
  sites: number[];
  contract_value: string;
  payment_terms: string;
}

export interface SubcontractPaymentInput {
  subcontract: number;
  amount: string;
  paid_on: string;
  reference: string;
}

const KEYS = ['subcontracts', 'subcontract-payments', 'jobs'];

export const useSubcontracts = (project: string | number | undefined, enabled = true) =>
  useList<Subcontract>(
    'subcontracts',
    { project, page_size: 100 },
    { enabled: enabled && project !== undefined && project !== '' },
  );

/** Detail carries the position, jobs and payments (§4.19.10). */
export const useSubcontract = (id: number | undefined) =>
  useResource<Subcontract>(`subcontracts/${id}`, undefined, { enabled: id !== undefined });

export const useCreateSubcontract = () =>
  useAction<SubcontractInput, Subcontract>({
    resource: 'subcontracts',
    invalidates: KEYS,
  });

export const useUpdateSubcontract = () =>
  useAction<SubcontractInput, Subcontract>({
    resource: 'subcontracts',
    method: 'patch',
    path: (b) => String(b.id),
    invalidates: KEYS,
  });

export const useRecordPayment = () =>
  useAction<SubcontractPaymentInput, SubcontractPayment>({
    resource: 'subcontract-payments',
    invalidates: KEYS,
  });
