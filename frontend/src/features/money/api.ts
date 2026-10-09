/**
 * Money-out endpoints (design §4.17.6), as thin hooks over `useList` /
 * `useAction`. Resources carry no `/api/v1` prefix; the client adds it.
 *
 * Decisions invalidate both expenses and requests: a float's balance counts
 * its expenses (R2), so one moving can make the other stale.
 */

import { useAction, useDetail, useList, useResource, type QueryParams } from '../../api/hooks';
import type {
  AllowanceRequest,
  Casual,
  CategoryKind,
  ExpenseCasualLine,
  ExpenseCategory,
  FinanceSettings,
  ProjectExpense,
} from './types';

const EXPENSES = 'project-expenses';
const REQUESTS = 'allowance-requests';
const CASUALS = 'casuals';
const SETTINGS = 'finance/settings';
const CATEGORIES = 'expense-categories';
const PENDING = [`${EXPENSES}/pending`, `${REQUESTS}/pending`];
const BOTH = [EXPENSES, REQUESTS, ...PENDING];

export interface ExpenseParams extends QueryParams {
  /** My own entries. */
  mine?: boolean;
  /** To-pay queue: APPROVED, no float, unpaid (`finance.approve`). */
  payable?: boolean;
  project?: number;
  status?: string;
}

export interface AllowanceParams extends QueryParams {
  mine?: boolean;
  /** To-pay queue: APPROVED and unpaid (`finance.approve`). */
  payable?: boolean;
  type?: string;
  status?: string;
}

export interface ExpenseInput {
  project?: number | null;
  site?: number | null;
  job?: number | null;
  category: number;
  amount: string;
  incurred_on: string;
  description: string;
  scope_of_work?: string;
  /** An asset (VEHICLE/GENERATOR); "Not ours" sends `vehicle_reg` instead (4.20.10). */
  vehicle?: number | null;
  vehicle_reg?: string;
  litres?: string | null;
  float_request?: number | null;
  photos_expected?: number;
  client_uuid?: string;
  casual_lines?: ExpenseCasualLine[];
}

export interface AllowanceInput {
  type: AllowanceRequest['type'];
  transport_scope?: AllowanceRequest['transport_scope'];
  amount: string;
  from_date: string;
  to_date: string;
  site?: number | null;
  project?: number | null;
  reason: string;
  client_uuid?: string;
}

export interface CasualInput {
  name: string;
  id_number: string;
  phone?: string;
  client_uuid?: string;
}

export interface DecideBody {
  id: number | string;
  approved: boolean;
  reason?: string;
}

export interface MarkPaidBody {
  id: number | string;
  payment_reference: string;
  paid_at?: string;
}

export interface CloseFloatBody {
  id: number | string;
  returned_amount: string;
}

export const useExpenses = (params?: ExpenseParams) =>
  useList<ProjectExpense>(EXPENSES, params);

export const useExpense = (id: string | number | undefined) =>
  useDetail<ProjectExpense>(EXPENSES, id);

export const useCreateExpense = () =>
  useAction<ExpenseInput, ProjectExpense>({ resource: EXPENSES, invalidates: BOTH });

export const useDecideExpense = () =>
  useAction<DecideBody, ProjectExpense>({
    resource: EXPENSES,
    path: (b) => `${b.id}/decide`,
    invalidates: [...BOTH, 'approvals/pending'],
  });

export const useResubmitExpense = () =>
  useAction<{ id: number | string }, ProjectExpense>({
    resource: EXPENSES,
    path: (b) => `${b.id}/resubmit`,
    invalidates: [...BOTH, 'approvals/pending'],
  });

export const useMarkExpensePaid = () =>
  useAction<MarkPaidBody, ProjectExpense>({
    resource: EXPENSES,
    path: (b) => `${b.id}/mark-paid`,
    invalidates: BOTH,
  });

/** Waiting on the caller at their level (PM or Finance), decided server-side (§4.17.6). */
export const usePendingExpenses = (params?: QueryParams) =>
  useList<ProjectExpense>(`${EXPENSES}/pending`, params);

export const usePendingAllowances = (params?: QueryParams) =>
  useList<AllowanceRequest>(`${REQUESTS}/pending`, params);

export const useAllowanceRequests = (params?: AllowanceParams, enabled = true) =>
  useList<AllowanceRequest>(REQUESTS, params, { enabled });

export const useAllowanceRequest = (id: string | number | undefined) =>
  useDetail<AllowanceRequest>(REQUESTS, id);

export const useCreateAllowanceRequest = () =>
  useAction<AllowanceInput, AllowanceRequest>({ resource: REQUESTS });

export const useDecideAllowance = () =>
  useAction<DecideBody, AllowanceRequest>({
    resource: REQUESTS,
    path: (b) => `${b.id}/decide`,
    invalidates: [...BOTH, 'approvals/pending'],
  });

export const useResubmitAllowance = () =>
  useAction<{ id: number | string }, AllowanceRequest>({
    resource: REQUESTS,
    path: (b) => `${b.id}/resubmit`,
    invalidates: [REQUESTS, 'approvals/pending'],
  });

export const useMarkAllowancePaid = () =>
  useAction<MarkPaidBody, AllowanceRequest>({
    resource: REQUESTS,
    path: (b) => `${b.id}/mark-paid`,
    invalidates: BOTH,
  });

export const useCloseFloat = () =>
  useAction<CloseFloatBody, AllowanceRequest>({
    resource: REQUESTS,
    path: (b) => `${b.id}/close-float`,
    invalidates: BOTH,
  });

export const useCasuals = (search?: string, enabled = true) =>
  useList<Casual>(CASUALS, { search }, { enabled });

export const useCreateCasual = () => useAction<CasualInput, Casual>({ resource: CASUALS });

export const useFinanceSettings = (enabled = true) =>
  useResource<FinanceSettings>(SETTINGS, undefined, { enabled });

export const useUpdateFinanceSettings = () =>
  useAction<Partial<FinanceSettings>, FinanceSettings>({
    resource: SETTINGS,
    method: 'patch',
  });

export const useExpenseCategories = (params?: QueryParams) =>
  useList<ExpenseCategory>(CATEGORIES, { page_size: 100, ...params });

export interface CategoryInput {
  id?: number;
  name: string;
  code: string;
  kind: CategoryKind;
  is_active?: boolean;
}

export const useCreateExpenseCategory = () =>
  useAction<CategoryInput, ExpenseCategory>({ resource: CATEGORIES });

export const useUpdateExpenseCategory = () =>
  useAction<CategoryInput, ExpenseCategory>({
    resource: CATEGORIES,
    method: 'patch',
    path: (b) => String(b.id),
  });
