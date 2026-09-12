/** Job, closeout, custody and reconciliation shapes (design §6, §4.9, §4.10). */

export type JobStatus =
  | 'OPEN'
  | 'IN_PROGRESS'
  /** Material reported, waiting on the yard to receive what is coming back. */
  | 'AWAITING_CLOSEOUT'
  | 'CLOSED'
  | 'CANCELLED';

/**
 * H2, §4.9: the split matters.
 *
 * `INSTALLED` and `CONSUMED` post movements immediately — that material is gone.
 * `RETURNING` and `RECOVERED` create an *expectation*: the technician says it is
 * coming back, and the yard confirms when it arrives. A closeout that posted
 * returns straight away would show stock in the yard that is still in a van.
 */
export type CloseoutAction = 'INSTALLED' | 'CONSUMED' | 'RETURNING' | 'RECOVERED';

export interface Job {
  id: number;
  reference: string;
  client: number | null;
  client_name: string;
  site: number | null;
  site_name: string;
  site_ref: string;
  work_order: number | null;
  assignee: number | null;
  assignee_name: string;
  description: string;
  status: JobStatus;
  closed_at: string | null;
  closed_by: number | null;
  closed_with_variance: boolean;
  close_reason: string;
  is_closed: boolean;
  created_at: string;
}

export interface CloseoutLineInput {
  id?: number;
  action: CloseoutAction;
  item_type: number;
  item_name?: string;
  serial_unit?: number | null;
  serial_number?: string;
  reel?: number | null;
  drum_number?: string;
  quantity: string;
  uom: string;
  condition?: string;
  notes?: string;
}

export interface JobCloseout {
  id: number;
  job: number;
  job_reference: string;
  submitted_by: number | null;
  submitted_by_name: string;
  on_behalf_of: number | null;
  status: string;
  submitted_at: string | null;
  confirmed_at: string | null;
  confirmed_by: number | null;
  notes: string;
  lines: CloseoutLineInput[];
  created_at: string;
}

/** One row of a person's custody, read from the ledger's PERSON node (§4.10). */
export interface CustodyBalance {
  id: number;
  item_type: number;
  item_name: string;
  item_code: string;
  tracking_mode: 'BULK' | 'SERIALIZED' | 'REEL';
  node: number;
  holder_id: number | null;
  site_id: number | null;
  node_label: string;
  node_type: string;
  owner_client: number | null;
  owner_client_name: string;
  condition: string;
  quantity: string;
  uom: string;
}

export interface SerialUnit {
  id: number;
  serial_number: string;
  asset_tag: string;
  item_type: number;
  item_name: string;
  status: string;
  condition: string;
  current_node: number | null;
  node_label: string;
  owner_type: string;
  owner_client: number | null;
  owner_client_name: string;
}

export interface Reel {
  id: number;
  drum_number: string;
  item_type: number;
  item_name: string;
  status: string;
  condition: string;
  initial_length: string;
  remaining_length: string;
  uom: string;
  current_node: number | null;
  node_label: string;
  owner_client: number | null;
  owner_client_name: string;
}

export interface CustodyTransferLine {
  id?: number;
  item_type: number;
  item_name?: string;
  serial_unit?: number | null;
  reel?: number | null;
  quantity: string;
  uom: string;
  condition?: string;
}

export interface CustodyTransfer {
  id: number;
  number: string;
  status: 'PENDING' | 'ACKNOWLEDGED' | 'DECLINED' | 'CANCELLED';
  from_holder: number;
  from_holder_name: string;
  to_holder: number;
  to_holder_name: string;
  requested_by: number | null;
  acknowledged_at: string | null;
  declined_reason: string;
  notes: string;
  lines: CustodyTransferLine[];
  created_at: string;
}

export interface CustodyExpectation {
  id: number;
  holder: number;
  holder_name: string;
  item_type: number;
  item_name: string;
  serial_unit: number | null;
  reel: number | null;
  quantity: string;
  returned_quantity: string;
  outstanding_quantity: string;
  expected_return_date: string | null;
  status: string;
  is_open: boolean;
  returned_at: string | null;
  written_off_reason: string;
  created_at: string;
}

/** H4's four figures for one item, plus the fifth that is derived from them. */
export interface ReconciliationItem {
  item_type_id: number;
  item_type: string;
  uom: string;
  issued: string;
  installed: string;
  consumed: string;
  returned: string;
  accounted: string;
  unaccounted: string;
  is_reconciled: boolean;
}

export interface Reconciliation {
  scope: string;
  scope_id: number | string;
  label: string;
  items: ReconciliationItem[];
  totals: {
    issued: string;
    installed: string;
    consumed: string;
    returned: string;
    unaccounted: string;
  };
  is_reconciled: boolean;
}

export interface OverdueRow {
  /** Set on the by-person grouping. */
  holder?: string;
  holder_id?: number;
  items?: number;
  days_overdue?: number;
  /** Set on the by-item grouping. */
  item?: string;
  item_type_id?: number;
  holders?: number;
  uom?: string;
  quantity: string;
}

export interface OverdueReport {
  as_at: string;
  total: number;
  by_person: OverdueRow[];
  by_item: OverdueRow[];
}
