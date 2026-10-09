/**
 * PO milestones, invoices, receipts and attach-PO (R11, R12; design §4.19.7,
 * §4.19.8, endpoint table §4.19.10).
 *
 * The design fixes the endpoints and the model columns, not the JSON keys, so
 * the shapes below are the frontend's reading of them. Money is a decimal
 * string. `state`, `met_on`, `invoiced` and `received` are server-derived.
 */

import { useAction, useList } from '../../api/hooks';

export type ShareType = 'PERCENT' | 'AMOUNT';
export type MilestoneCondition = 'NONE' | 'ALL_SITES_ACCEPTED' | 'DATE';
export type MilestoneState = 'NOT_DUE' | 'DUE' | 'OVERDUE' | 'INVOICED' | 'PART_PAID' | 'PAID';

export interface MilestoneInvoice {
  id: number;
  invoice_number: string;
  invoice_date: string;
  amount: string;
  voided_at?: string | null;
  void_reason?: string;
}

export interface MilestoneReceipt {
  id: number;
  received_on: string;
  amount: string;
  reference: string;
  voided_at?: string | null;
  void_reason?: string;
}

export interface Milestone {
  id: number;
  project: number;
  sequence: number;
  name: string;
  share_type: ShareType;
  share_value: string | null;
  condition: MilestoneCondition;
  condition_date: string | null;
  /** Share of `current_contract_value`; omitted for a viewer without margin rights (O14). */
  amount?: string | null;
  state: MilestoneState;
  met_on?: string | null;
  invoiced?: string;
  received?: string;
  invoices?: MilestoneInvoice[];
  receipts?: MilestoneReceipt[];
}

export interface MilestoneInput {
  name: string;
  share_type: ShareType;
  share_value: string | null;
  condition: MilestoneCondition;
  condition_date: string | null;
}

export interface InvoiceInput {
  milestone: number;
  invoice_number: string;
  invoice_date: string;
  amount: string;
}

export interface ReceiptInput {
  milestone: number;
  received_on: string;
  amount: string;
  reference: string;
}

export interface AttachPoInput {
  project: number;
  po_number: string;
  po_issue_date: string;
  contract_value: string;
  cost_budget: string;
  payment_terms: string;
  payment_terms_days: number | null;
  manager?: number | null;
}

/** A row of `GET /projects?po=none&status=OPEN` (§4.19.8). */
export interface ProjectWithoutPo {
  id: number;
  reference: string;
  title: string;
  client_name?: string;
  opened_at: string | null;
  days_without_po?: number;
}

const listKey = (projectId: number) => `projects/${projectId}/milestones`;

/** `GET /projects/{id}/milestones`. */
export const useMilestones = (projectId: number) =>
  useList<Milestone>(listKey(projectId), { page_size: 100 });

export const useAddMilestone = (projectId: number) =>
  useAction<MilestoneInput, Milestone>({
    resource: listKey(projectId),
    invalidates: [listKey(projectId)],
  });

export const useUpdateMilestone = (projectId: number) =>
  useAction<MilestoneInput & { id: number }, Milestone>({
    resource: listKey(projectId),
    method: 'patch',
    path: (b) => String(b.id),
    invalidates: [listKey(projectId)],
  });

export const useDeleteMilestone = (projectId: number) =>
  useAction<{ id: number }>({
    resource: listKey(projectId),
    method: 'delete',
    path: (b) => String(b.id),
    invalidates: [listKey(projectId)],
  });

/** "Add default milestones": M1 Deposit, M2 Conditional acceptance, M3 Final acceptance. */
export const useAddDefaultMilestones = (projectId: number) =>
  useAction<Record<string, never>>({
    resource: `${listKey(projectId)}/defaults`,
    invalidates: [listKey(projectId)],
  });

/** `POST /milestones/{id}/invoices`. */
export const useRecordInvoice = (projectId: number) =>
  useAction<InvoiceInput, MilestoneInvoice>({
    resource: 'milestones',
    path: (b) => `${b.milestone}/invoices`,
    invalidates: [listKey(projectId)],
  });

/** `POST /milestones/{id}/receipts`. */
export const useRecordReceipt = (projectId: number) =>
  useAction<ReceiptInput, MilestoneReceipt>({
    resource: 'milestones',
    path: (b) => `${b.milestone}/receipts`,
    invalidates: [listKey(projectId)],
  });

/** `POST /projects/{id}/attach-po`: sets the PO on the same row and seeds milestones. */
export const useAttachPo = () =>
  useAction<AttachPoInput>({
    resource: 'projects',
    path: (b) => `${b.project}/attach-po`,
    invalidates: ['projects'],
  });

export const useProjectsWithoutPo = (enabled: boolean) =>
  useList<ProjectWithoutPo>('projects', { po: 'none', status: 'OPEN', page_size: 20 }, { enabled });
