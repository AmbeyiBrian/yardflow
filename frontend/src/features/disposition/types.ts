/** Disposition, disposal and client-return shapes (design §4.11, §4.12). */

/**
 * J2's four outcomes. The split that matters:
 *
 * `RESTORE_TO_SERVICEABLE` and `REPAIR` move material when the disposition is
 * posted. `RETURN_TO_CLIENT` and `SCRAP` move nothing — they record a decision,
 * and the material leaves on its own document (a gate-out, or an approved
 * disposal). That is why deciding something is scrap does not make it disappear.
 */
export type DispositionDecision =
  | 'REPAIR'
  | 'RESTORE_TO_SERVICEABLE'
  | 'RETURN_TO_CLIENT'
  | 'SCRAP';

export type DispositionStatus =
  | 'DRAFT'
  | 'PENDING_APPROVAL'
  | 'APPROVED'
  | 'REJECTED'
  | 'POSTED'
  | 'CANCELLED';

export type DisposalStatus =
  | 'DRAFT'
  | 'PENDING_APPROVAL'
  | 'APPROVED'
  | 'REJECTED'
  | 'DISPOSED'
  | 'CANCELLED';

export type DisposalMethod =
  | 'SCRAP_DEALER'
  | 'LICENSED_HANDLER'
  | 'DESTROYED_ON_SITE'
  | 'RETURNED_TO_SUPPLIER'
  | 'OTHER';

export interface PendingApproval {
  id: number;
  level: number;
  role: string | null;
  status: string;
  due_at: string | null;
}

export interface DispositionLine {
  id?: number;
  line_number?: number;
  item_type: number;
  item_name?: string;
  serial_unit?: number | null;
  serial_number?: string;
  reel?: number | null;
  drum_number?: string;
  quantity: string;
  uom: string;
  condition?: string;
  /** Only RESTORE_TO_SERVICEABLE changes this, and that change is what puts the
   * material back into free stock. */
  to_condition?: string;
  owner_type?: 'OWN' | 'CLIENT';
  owner_client?: number | null;
  owner_client_name?: string;
  notes?: string;
}

export interface Disposition {
  id: number;
  number: string;
  status: DispositionStatus;
  decision: DispositionDecision;
  reason: string;
  from_location: number;
  from_location_name: string;
  to_location: number | null;
  to_location_name: string;
  vendor_name: string;
  expected_return_date: string | null;
  decided_by: number | null;
  posted_at: string | null;
  posted_by: number | null;
  disposal: number | null;
  reject_reason: string;
  cancel_reason: string;
  notes: string;
  lines: DispositionLine[];
  involves_client_owned_material: boolean;
  pending_approval: PendingApproval | null;
  created_at: string;
}

export interface DisposalLine {
  id?: number;
  line_number?: number;
  item_type: number;
  item_name?: string;
  serial_unit?: number | null;
  serial_number?: string;
  reel?: number | null;
  drum_number?: string;
  quantity: string;
  uom: string;
  condition?: string;
  owner_type?: 'OWN' | 'CLIENT';
  owner_client?: number | null;
  owner_client_name?: string;
  written_off_value?: string | null;
  notes?: string;
}

export interface Disposal {
  id: number;
  number: string;
  status: DisposalStatus;
  method: DisposalMethod;
  handler_name: string;
  handler_reference: string;
  from_location: number;
  from_location_name: string;
  requested_by: number | null;
  submitted_at: string | null;
  approved_at: string | null;
  disposed_at: string | null;
  disposed_by: number | null;
  reject_reason: string;
  cancel_reason: string;
  notes: string;
  lines: DisposalLine[];
  involves_client_owned_material: boolean;
  pending_approval: PendingApproval | null;
  /** `null` when the tenant does not track money (C8). */
  written_off_total: string | null;
  created_at: string;
}

/** One thing sitting in quarantine, and how long it has been there (J1, J2). */
export interface QuarantineItem {
  balance_id: number;
  location_id: number;
  location: string;
  item_type_id: number;
  item_type: string;
  tracking_mode: 'BULK' | 'SERIALIZED' | 'REEL';
  quantity: string;
  uom: string;
  condition: string;
  owner_client_id: number | null;
  owner_client: string;
  since: string | null;
  days_in_quarantine: number | null;
}

/** K1, K3: where a client's material stands with us. */
export type ClientMaterialState = 'HELD' | 'IN_TRANSIT' | 'ACKNOWLEDGED';

export interface ClientPositionRow {
  client_id: number;
  client: string;
  item_type_id: number;
  item_type: string;
  uom: string;
  state: ClientMaterialState;
  quantity: string;
  documents: {
    gate_out_id: number;
    number: string;
    released_at: string | null;
    acknowledged_ref: string;
    acknowledged_at: string | null;
  }[];
}

export interface ClientReturnAck {
  id: number;
  gate_out: number;
  gate_out_number: string;
  client_name: string;
  acknowledged_ref: string;
  acknowledged_at: string;
  acknowledged_by_name: string;
  recorded_by: number | null;
  recorded_by_name: string;
  notes: string;
  created_at: string;
}
