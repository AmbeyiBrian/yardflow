/** Dispatch, approval and notification shapes (design §6). */

export type GateOutStatus =
  | 'DRAFT'
  | 'PENDING_APPROVAL'
  | 'APPROVED'
  | 'REJECTED'
  | 'PARTIALLY_RELEASED'
  | 'RELEASED'
  | 'CANCELLED'
  | 'EXPIRED'
  | 'CLOSED';

export type GateOutPurpose =
  | 'INSTALLATION'
  | 'MAINTENANCE'
  | 'TRANSFER'
  | 'RETURN_TO_CLIENT'
  | 'DISPOSAL'
  | 'LOAN';

export interface GateOutLineSerial {
  id?: number;
  serial_unit: number;
  serial_number?: string;
  released?: boolean;
}

export interface GateOutLineReel {
  id?: number;
  reel: number;
  drum_number?: string;
  length_requested: string;
  length_released?: string;
}

export interface GateOutLine {
  id?: number;
  line_number?: number;
  item_type: number;
  item_name?: string;
  /** C1: what routes the approval (§5.1). Shown on the approval screen. */
  criticality?: 'LOW' | 'MEDIUM' | 'HIGH';
  tracking_mode: 'BULK' | 'SERIALIZED' | 'REEL';
  requested_qty: string;
  released_qty?: string;
  outstanding_qty?: string;
  uom: string;
  condition?: string;
  owner_type?: 'OWN' | 'CLIENT';
  owner_client?: number | null;
  is_returnable?: boolean;
  expected_return_date?: string | null;
  notes?: string;
  /**
   * D3: why a serialized item is going out as a plain quantity.
   *
   * Required in that one case, because otherwise the ledger holds a unit nobody
   * can identify — see the gate-out line's own validation.
   */
  no_serial_reason?: string;
  serials?: GateOutLineSerial[];
  reels?: GateOutLineReel[];
}

export interface PendingApproval {
  id: number;
  level: number;
  role: string | null;
  status: string;
  due_at: string | null;
}

export interface GateOut {
  id: number;
  number: string;
  status: GateOutStatus;
  purpose_type: GateOutPurpose;
  site: number | null;
  project: number | null;
  /** O5: which job this is for. An attribution, not a destination. */
  job?: number | null;
  client: number | null;
  to_location: number | null;
  from_location: number;
  custody_holder: number;
  custody_holder_name: string;
  requested_by: number | null;
  destination_label: string;
  submitted_at: string | null;
  approved_at: string | null;
  expires_at: string | null;
  vehicle_reg: string;
  driver_name: string;
  released_by: number | null;
  released_at: string | null;
  version: number;
  reject_reason: string;
  cancel_reason: string;
  close_reason: string;
  notes: string;
  /** N2: the identity the requester gave it, so a repeat create is not a second pass. */
  client_uuid?: string | null;
  lines: GateOutLine[];
  is_releasable: boolean;
  is_expired: boolean;
  pending_approval: PendingApproval | null;
}

export interface ApprovalAction {
  id: number;
  decision: string;
  reason: string;
  actor: number | null;
  actor_name: string;
  on_behalf_of: number | null;
  /** §4.2: "X on behalf of Y", never just Y. */
  attribution: string;
  auth_method: string;
  decided_at: string;
}

/**
 * What an approval request carries about its document (F4).
 *
 * A purpose-built summary, not the whole gate pass: the pending list has to
 * carry enough to judge on a phone without a second request, and no more.
 */
export interface ApprovalDocumentSummary {
  id?: number;
  number?: string;
  label?: string;
  purpose?: string;
  destination?: string;
  custody_holder?: string;
  requested_by?: string;
  requested_by_id?: number | null;
  notes?: string;
  lines?: {
    item: string;
    quantity: string;
    uom: string;
    owner: string;
    is_client_owned: boolean;
    criticality: 'LOW' | 'MEDIUM' | 'HIGH';
  }[];
}

export interface ApprovalRequest {
  id: number;
  document_type: string;
  document_id: string;
  document_number: string;
  document: ApprovalDocumentSummary | null;
  level: number;
  required_role: number | null;
  role_name: string;
  status: string;
  due_at: string | null;
  escalated_at: string | null;
  resolved_at: string | null;
  actions: ApprovalAction[];
}

export interface ReleaseVariance {
  id: number;
  gate_out_line: number;
  gate_out_number: string;
  item_name: string;
  approved_qty: string;
  released_qty: string;
  difference: string;
  reason: string;
  acknowledged_at: string | null;
  is_open: boolean;
}

export interface Notification {
  id: number;
  event_key: string;
  subject: string;
  body: string;
  target_label: string;
  target_type: string;
  target_id: string;
  resource: string;
  payload: Record<string, unknown>;
  occurred_at: string;
  read_at: string | null;
  is_unread: boolean;
}

export interface ExceptionEntry {
  kind: string;
  id: number;
  reference: string;
  summary: string;
  expected: string;
  actual: string;
  uom: string;
  raised_at: string | null;
  detail_url: string;
}
