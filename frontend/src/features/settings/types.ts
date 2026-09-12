/**
 * Shapes the administration screens read (design §6).
 *
 * Hand-written rather than taken from `schema.d.ts`: the generated file is the
 * whole API surface, and a screen only needs the fields it renders. Field names
 * here match the committed schema — `npm run api:types` regenerates the full
 * version, which is what a client generator or an integration would read (N-10).
 */

export type Criticality = 'LOW' | 'MEDIUM' | 'HIGH';
export type TrackingMode = 'BULK' | 'SERIALIZED' | 'REEL';

export interface ItemCategory {
  id: number;
  parent: number | null;
  name: string;
  code: string;
  criticality: Criticality | null;
  /** C1: inherited down the tree when a child sets none. */
  effective_criticality: Criticality;
  is_archived: boolean;
  custom_fields?: CategoryCustomField[];
  item_type_count?: number;
}

export interface CategoryCustomField {
  id: number;
  category: number;
  label: string;
  key: string;
  field_type: string;
  /** C2: a required field is required *at gate-in*, which is when it is known. */
  required_at_gate_in: boolean;
  options: string[];
  order: number;
  is_archived: boolean;
}

export interface ItemType {
  id: number;
  category: number;
  category_name?: string;
  name: string;
  code: string;
  description: string;
  default_tracking_mode: TrackingMode;
  uom: string;
  is_returnable: boolean;
  default_return_days: number | null;
  min_stock_qty: string | null;
  unit_cost: string | null;
  is_archived: boolean;
  criticality: Criticality;
  attributes: Record<string, unknown>;
}

export interface Client {
  id: number;
  name: string;
  code: string;
  contact_name: string;
  contact_email: string;
  contact_phone: string;
  /** C6: a client's own site-code shape, validated on entry. */
  site_code_pattern: string;
  is_active: boolean;
  site_count?: number;
}

export interface Site {
  id: number;
  client: number;
  client_name?: string;
  internal_ref: string;
  name: string;
  region: string;
  county: string;
  latitude: string | null;
  longitude: string | null;
  site_type: string;
  status: string;
  cell_id: string;
  enodeb_id: string;
  notes: string;
  references?: SiteReference[];
}

export interface SiteReference {
  id: number;
  site: number;
  label: string;
  value: string;
}

export interface WorkOrder {
  id: number;
  client: number;
  client_name?: string;
  reference: string;
  description: string;
  sites: number[];
  site_count?: number;
  status: string;
  opened_at: string | null;
  closed_at: string | null;
  closed_with_unreconciled: boolean;
  close_reason: string;
}

export interface Location {
  id: number;
  parent: number | null;
  name: string;
  code: string;
  type: 'YARD' | 'STORE' | 'VEHICLE' | 'QUARANTINE';
  vehicle_reg: string;
  is_active: boolean;
  is_system: boolean;
  children?: Location[];
  node_id?: number | null;
}

export interface Role {
  id: number;
  name: string;
  description: string;
  is_system: boolean;
  codenames: string[];
  user_count: number;
}

export interface ManagedUser {
  /** False until they follow their invitation and set a password (B1). */
  has_signed_in_yet?: boolean;
  id: number;
  email: string | null;
  phone: string | null;
  full_name: string;
  is_active: boolean;
  roles: { id: number; name: string }[];
  permissions: string[];
  can_be_deleted: boolean;
  last_login: string | null;
}

export interface Delegation {
  id: number;
  from_user: number;
  from_user_name: string;
  to_user: number;
  to_user_name: string;
  role: number | null;
  /** The role's name, when a whole role is delegated. */
  role_name: string | null;
  codenames: string[];
  starts_at: string;
  ends_at: string;
  reason: string;
  is_revoked: boolean;
  is_currently_active: boolean;
}

export interface PermissionGroup {
  group: string;
  permissions: { codename: string; label: string; rationale: string }[];
}

export interface OrganizationSettingsPayload {
  money_tracking_enabled: boolean;
  min_stock_enabled: boolean;
  qr_labels_enabled: boolean;
  asset_tag_enabled: boolean;
  asset_tag_prefix_format: string;
  client_waybill_enabled: boolean;
  attachments_enabled: boolean;
  attachments_required_gate_in: boolean;
  attachments_required_gate_out: boolean;
  signature_required_on_release: boolean;
  gate_pass_expiry_hours: number;
  allow_self_approval: boolean;
  approval_escalation_hours: number;
  allow_document_amendment: boolean;
  retention_months: number;
  timezone: string;
  currency: string;
  /** L2: which channels are live for this tenant, and who hears what. */
  notification_channels?: Record<string, boolean>;
  notification_matrix?: Record<
    string,
    { recipients?: string[]; channels?: string[]; enabled?: boolean }
  >;
}
