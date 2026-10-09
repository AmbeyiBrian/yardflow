/**
 * Clock-in shapes (design §4.18.2, §4.18.7; R13).
 *
 * Distances are metres, times are ISO strings. Flags are derived on the server
 * (never stored) and arrive as codes; `FLAG_LABELS` in `rules.ts` words them.
 * The design fixes the endpoints and field meanings, not every serializer
 * name, so the read shapes here are the assumed ones (see T16.13 notes).
 */

export type DayStatus = 'OPEN' | 'PENDING' | 'APPROVED' | 'REJECTED';

export type SessionFlag =
  | 'OUTSIDE_AT_CLOCK_OUT'
  | 'NO_POSITION_AT_CLOCK_OUT'
  | 'CLOSED_AUTOMATICALLY'
  | 'SENT_LATE'
  | 'AREA_CHANGED'
  | 'CORRECTED'
  | 'ADDED_BY_DIRECTOR';

/** The fix sent with a clock-in or clock-out. */
export interface Fix {
  lat: number;
  lng: number;
  accuracy_m: number;
}

export interface WorkSessionCorrection {
  id: number;
  kind: 'EDIT' | 'ADD';
  original_in_at: string | null;
  original_out_at: string | null;
  corrected_in_at: string | null;
  corrected_out_at: string | null;
  reason: string;
  made_by_name?: string;
  made_at: string;
}

export interface WorkSession {
  id: number;
  site: number | null;
  location: number | null;
  /** The place's name, whichever kind it is. */
  place_name: string;
  project: number | null;
  project_name?: string | null;
  work_day: number;
  local_date: string;
  clock_in_at: string;
  clock_out_at: string | null;
  in_distance_m: number | null;
  out_distance_m: number | null;
  /** The place's limit, so "640 m (limit 200 m)" can be shown. */
  radius_m?: number | null;
  closed_by: 'PERSON' | 'NEXT_CLOCK_IN' | 'AUTO' | null;
  flags: SessionFlag[];
  /** Hours as counted (corrected times where a correction applies). */
  hours?: string | null;
  /** The slice's status; a REJECTED slice can be corrected. */
  slice_status?: DayStatus | null;
  can_correct?: boolean;
  added_reason?: string;
  corrections?: WorkSessionCorrection[];
}

/**
 * One approver's part of a day (§4.18.5): sessions on a PM's project go to
 * that PM, the rest to the Director role. Assumed read shape.
 */
export interface WorkDaySlice {
  id: number;
  approver: number | null;
  approver_name?: string;
  /** The project the slice covers; null for the Director-routed remainder. */
  project: number | null;
  project_name?: string | null;
  status: DayStatus;
  /** True when this slice is the caller's to decide. */
  is_mine?: boolean;
  reason?: string;
  decided_at?: string | null;
}

export interface WorkDay {
  id: number;
  person: number;
  person_name?: string;
  date: string;
  status: DayStatus;
  hours: string;
  rejection_reason?: string;
  session_count?: number;
  sessions?: WorkSession[];
  slices?: WorkDaySlice[];
}

export type PlaceKind = 'site' | 'location';

/** Anything one can clock in at, with its checked area (R13). */
export interface ClockPlace {
  kind: PlaceKind;
  id: number;
  name: string;
  /** "Site" / "Yard" / "Office". */
  label: string;
  lat: number;
  lng: number;
  radius_m: number;
  /** From the offline bundle only: the site's open projects, so the choice works with no signal (R13). */
  open_projects?: { id: number; reference: string; title: string }[];
}

export interface ClockInBody {
  site?: number;
  location?: number;
  project?: number;
  fix: Fix | null;
  client_uuid: string;
}

export interface ClockOutBody {
  fix?: Fix | null;
  session_client_uuid?: string;
  client_uuid: string;
}

export interface CorrectionBody {
  /** The session being corrected. */
  id: number | string;
  kind: 'EDIT' | 'ADD';
  corrected_in_at?: string;
  corrected_out_at?: string;
  reason: string;
}

export interface DecideDayBody {
  id: number | string;
  approved: boolean;
  reason: string;
}

/** §4.18.6a: the Director adds a day for someone. */
export interface AddDayBody {
  person: number;
  date: string;
  place: { site: number } | { location: number };
  project?: number;
  start: string;
  end: string;
  reason: string;
}
