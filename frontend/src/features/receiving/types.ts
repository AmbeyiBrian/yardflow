/** Receiving and stock shapes (design §6). Field names match the committed schema. */

export type SourceType =
  | 'PURCHASE'
  | 'CLIENT_ISSUE'
  | 'RECOVERY'
  | 'RETURN_FROM_SITE'
  | 'WARRANTY_RETURN'
  | 'TRANSFER';

export type Condition =
  | 'NEW'
  | 'USED_SERVICEABLE'
  | 'FAULTY'
  | 'DAMAGED'
  | 'SCRAP';

export interface GateInSerialInput {
  id?: number;
  serial_number: string;
  asset_tag?: string;
  source?: string;
}

export interface GateInReelInput {
  id?: number;
  drum_number: string;
  length: string;
}

export interface GateInLineInput {
  id?: number;
  line_number?: number;
  item_type: number;
  item_name?: string;
  tracking_mode: 'BULK' | 'SERIALIZED' | 'REEL';
  quantity: string;
  uom: string;
  condition: Condition;
  is_unserviceable?: boolean;
  owner_type: 'OWN' | 'CLIENT';
  owner_client: number | null;
  owner_client_name?: string;
  custom_field_values?: Record<string, unknown>;
  no_serial_reason?: string;
  notes?: string;
  serials?: GateInSerialInput[];
  reels?: GateInReelInput[];
}

export interface GateIn {
  id: number;
  number: string;
  status: 'DRAFT' | 'POSTED' | 'VOID';
  source_type: SourceType;
  supplier_name: string;
  client: number | null;
  client_name?: string;
  returned_by: number | null;
  returned_by_name?: string;
  origin_site: number | null;
  origin_site_ref?: string;
  to_location: number;
  to_location_name?: string;
  received_at: string;
  posted_at: string | null;
  client_delivery_note_ref: string;
  client_uuid: string | null;
  void_reason: string;
  voided_at: string | null;
  notes: string;
  lines: GateInLineInput[];
  created_at: string;
}

export interface StockBalance {
  id: number;
  item_type: number;
  item_name: string;
  item_code: string;
  node: number;
  node_label: string;
  node_type: string;
  owner_client: number | null;
  owner_client_name: string;
  condition: string;
  quantity: string;
  uom: string;
}

export interface Movement {
  id: number;
  occurred_at: string;
  movement_type: string;
  item_type: number;
  item_name: string;
  quantity: string;
  uom: string;
  from_node: number;
  from_label: string;
  to_node: number;
  to_label: string;
  condition: string;
  from_condition: string;
  owner_type: string;
  owner_client: number | null;
  owner_client_name: string;
  serial_number: string;
  drum_number: string;
  document_type: string;
  document_number: string;
  document_id: string;
  posted_by: number | null;
  posted_by_name: string;
  note: string;
}

export interface SerialUnit {
  id: number;
  serial_number: string;
  asset_tag: string;
  item_type: number;
  item_name: string;
  status: string;
  condition: string;
  current_node: number;
  node_label: string;
  owner_type: string;
  owner_client: number | null;
  owner_client_name: string;
  origin_site: number | null;
  origin_site_ref: string;
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
  current_node: number;
  node_label: string;
  owner_type: string;
  owner_client: number | null;
  owner_client_name: string;
}

export interface StockCountLine {
  id: number;
  item_type: number;
  item_name: string;
  owner_client: number | null;
  owner_client_name: string;
  condition: string;
  expected_quantity: string;
  counted_quantity: string;
  variance: string;
  uom: string;
  reason: string;
}

export interface StockCount {
  id: number;
  number: string;
  status: string;
  location: number;
  location_name: string;
  counted_at: string;
  posted_at: string | null;
  notes: string;
  lines: StockCountLine[];
}
