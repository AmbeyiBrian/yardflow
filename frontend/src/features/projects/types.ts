/**
 * Project shapes (Epic O).
 *
 * Every money field is optional on purpose. O14 has the server **omit** a
 * figure the caller may not see rather than sending null, so `undefined` here
 * means "you were not told" and `null` means "there is no figure". Screens have
 * to tell those apart: one is a blank, the other is a reason to hide the tile.
 */

export interface Project {
  id: number;
  client: number;
  client_name?: string;
  reference: string;
  po_number: string;
  title: string;
  description: string;
  manager: number | null;
  manager_name?: string;
  /** Withheld without `project.view_margin`. */
  contract_value?: string | null;
  current_contract_value?: string | null;
  /** Withheld without `project.view_cost` on a project you manage. */
  cost_budget?: string | null;
  current_cost_budget?: string | null;
  starts_on: string | null;
  target_completion_on: string | null;
  sites: number[];
  site_count?: number;
  status: 'OPEN' | 'CLOSED' | 'CANCELLED';
  opened_at: string | null;
  closed_at: string | null;
  closed_with_unreconciled: boolean;
  close_reason: string;
}

export interface ProjectPerformance {
  project: number;
  reference: string;
  status: string;
  manager: number | null;

  /** Quantities, so everyone who can see the project sees these. */
  jobs_total: number;
  jobs_closed: number;
  progress_percent: string | null;

  material?: string;
  material_loss?: string;
  subcontractor?: string;
  labour?: string;
  expenses?: string;
  cost_to_date?: string;
  exposure?: string;
  cost_budget?: string | null;
  budget_variance?: string | null;
  is_over_budget?: boolean;

  /** False when some input carried no valuation — the figure is incomplete. */
  is_fully_valued?: boolean;
  unvalued_movements?: number;
  uncosted_labour_entries?: number;
  jobs_closed_without_labour?: number;

  contract_value?: string | null;
  margin?: string | null;
  margin_percent?: string | null;
}

export interface ProjectVariation {
  id: number;
  project: number;
  project_reference?: string;
  reference: string;
  description: string;
  value_delta: string;
  budget_delta: string;
  effective_on: string;
  raised_by: number;
  raised_by_name?: string;
  status: 'PENDING' | 'APPROVED' | 'REJECTED';
  decided_by: number | null;
  decided_at: string | null;
  decision_reason: string;
}

export interface Subcontractor {
  id: number;
  name: string;
  code: string;
  contact_name: string;
  contact_email: string;
  contact_phone: string;
  notes: string;
  is_active: boolean;
}

export interface ExpenseCategory {
  id: number;
  name: string;
  code: string;
  is_active: boolean;
}

export interface ProjectExpense {
  id: number;
  project: number;
  project_reference?: string;
  job: number | null;
  category: number;
  category_name?: string;
  amount: string;
  incurred_on: string;
  description: string;
  recorded_by: number;
  recorded_by_name?: string;
  status: 'SUBMITTED' | 'APPROVED' | 'REJECTED';
  decided_by: number | null;
  decided_at: string | null;
  decision_reason: string;
  reverses: number | null;
  is_reversal?: boolean;
  created_at?: string;
}
