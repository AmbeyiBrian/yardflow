/**
 * Money-out shapes (Epic R, design §4.17.2).
 *
 * Amounts are decimal strings, as everywhere else: the server owns the
 * arithmetic and a float on the phone would only add rounding to a figure the
 * Finance team reads to the cent.
 */

export type ExpenseStatus =
  | 'PENDING_PM'
  | 'PENDING_FINANCE'
  | 'APPROVED'
  | 'PAID'
  | 'REJECTED';

/** What a category demands, so renaming it keeps behaviour (§4.17.2). */
export type CategoryKind = 'GENERAL' | 'FUEL' | 'CASUAL_LABOUR';

/** 4.17.8: photos that arrived, are still queued on the phone, or never existed. */
export type EvidenceState = 'ok' | 'arriving' | 'none';

export interface ExpenseCategory {
  id: number;
  name: string;
  code: string;
  kind: CategoryKind;
  is_active: boolean;
}

export interface ExpenseCasualLine {
  id?: number;
  casual: number;
  casual_name?: string;
  /** Always > 0. */
  days: number;
  /** Optional; the expense total is the authority (§4.17.2). */
  amount: string | null;
}

export interface ProjectExpense {
  id: number;
  project: number;
  project_reference?: string;
  job: number | null;
  category: number;
  category_name?: string;
  category_kind?: CategoryKind;
  amount: string;
  incurred_on: string;
  description: string;
  recorded_by: number;
  recorded_by_name?: string;
  status: ExpenseStatus;
  decided_by: number | null;
  decided_at: string | null;
  decision_reason: string;
  reverses: number | null;
  is_reversal?: boolean;
  created_at?: string;

  /** R1: where the work was. Null only when `project` was given directly. */
  site: number | null;
  site_name?: string;
  scope_of_work: string;
  /** Required by the server when the category is FUEL. */
  vehicle_reg: string;
  litres: string | null;
  /** The float this was spent from; such an expense is never paid separately. */
  float_request: number | null;
  photos_expected: number;
  client_uuid: string | null;
  paid_at: string | null;
  paid_by: number | null;
  payment_reference: string;
  evidence_state: EvidenceState;
  casual_lines: ExpenseCasualLine[];
  /** The recorder was the PM or the Director, so only Finance decides (R4). */
  pm_level_skipped?: boolean;
}

/** R3. The ID number arrives masked; the full value is never sent back. */
export interface Casual {
  id: number;
  name: string;
  id_number: string;
  phone: string;
  registered_by?: number;
  client_uuid?: string | null;
}

export type AllowanceType =
  | 'FLOAT'
  | 'TRANSPORT'
  | 'NIGHT_OUT'
  | 'TEAM_ALLOWANCE'
  | 'OTHER';

export type TransportScope = 'WITHIN_NAIROBI' | 'OUTSIDE_NAIROBI';

export interface AllowanceRequest {
  id: number;
  /** Series AR. */
  number: string;
  type: AllowanceType;
  /** TRANSPORT only. */
  transport_scope: TransportScope | null;
  amount: string;
  from_date: string;
  to_date: string;
  /** Inclusive: `to − from + 1`. */
  days: number;
  daily_amount: string;
  site: number | null;
  site_name?: string;
  project: number | null;
  project_reference?: string;
  reason: string;
  recorded_by: number;
  recorded_by_name?: string;
  status: ExpenseStatus;
  decided_by: number | null;
  decided_at: string | null;
  decision_reason: string;
  paid_at: string | null;
  paid_by: number | null;
  payment_reference: string;
  closed_at: string | null;
  closed_by: number | null;
  returned_amount: string | null;
  client_uuid: string | null;
  /** R2: another PAID, unclosed float of the same person. Never blocks. */
  open_float_warning: { number: string; balance: string } | null;
  pm_level_skipped?: boolean;
  /** Floats only. */
  spent?: string;
  balance?: string;
  created_at?: string;
}

export type AllowanceLimitKey =
  | 'TRANSPORT_WITHIN_NAIROBI'
  | 'TRANSPORT_OUTSIDE_NAIROBI'
  | 'NIGHT_OUT'
  | 'TEAM_ALLOWANCE';

/** A null bound means no bound. FLOAT and OTHER have no entry: unlimited. */
export type AllowanceLimits = Record<
  AllowanceLimitKey,
  { min: string | null; max: string | null }
>;

export interface FinanceSettings {
  allowance_limits: AllowanceLimits;
  finance_director_role: number | null;
}
