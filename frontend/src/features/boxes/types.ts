/**
 * Shapes for scan-to-release (P11, design §4.15.8).
 *
 * `ScanPass` is the part of a gate pass that matching needs. Both read sides
 * (the gate-out detail and the offline releasable bundle) carry it, so the
 * matcher works the same online and at a gate with no signal. Box paths run
 * from the outermost box to the one the stock was picked from, and are empty
 * for loose stock. Codes compare case-insensitively, as the database does.
 */
import type { LabelReading } from './readLabel';

export type Id = number | string;

/** A named unit on a pass line. */
export interface PassSerial {
  /** The line-serial row's id. */
  id: Id;
  /** The unit itself: what `released_serials` names. */
  serial_unit: Id;
  serial_number: string;
  asset_tag?: string | null;
  released: boolean;
  /** Where the unit sat when the pass was raised. */
  box_path: string[];
}

export interface PassLine {
  id: Id;
  /** 'SERIALIZED' lines are ticked per unit; anything else by quantity. */
  tracking_mode: string;
  /** Quantities may arrive as decimal strings. */
  requested_qty?: number | string;
  released_qty?: number | string;
  /** Preferred over `requested_qty - released_qty` when present. */
  outstanding_qty?: number | string;
  box_path: string[];
  serials: PassSerial[];
}

export interface ScanPass {
  lines: PassLine[];
}

/** What is already counted on screen. Matching never changes it. */
export interface TickState {
  /** `PassSerial.id` of units already ticked. */
  serials: ReadonlySet<Id>;
  /** `PassLine.id` of non-serialized lines already ticked. */
  lines: ReadonlySet<Id>;
}

export type Tick =
  | {
      kind: 'unit';
      lineId: Id;
      /** `PassSerial.id` */
      serialId: Id;
      serialUnit: Id;
      serialNumber: string;
    }
  | {
      kind: 'line';
      lineId: Id;
      /** The line's whole outstanding quantity. */
      qty: number;
    };

/** Something was scanned that this pass does not cover. */
export interface Refusal {
  /** What was scanned, as the storekeeper should see it. */
  scanned: string;
  message: string;
}

export type ScanOutcome =
  /** At least one new tick. */
  | 'ticked'
  /** It matched, but everything it matches is already counted. */
  | 'already_counted'
  /** It matched only units or lines already released on this pass. */
  | 'already_released'
  /** A gate-pass QR: not goods, and not a refusal of goods. */
  | 'gate_pass'
  | 'refused';

export interface ScanResult {
  outcome: ScanOutcome;
  /** New ticks only, each unit or line at most once. */
  ticks: Tick[];
  /** Matched things that were already ticked. */
  alreadyCounted: Tick[];
  /** Matched units that were released earlier on this pass (never ticked again). */
  alreadyReleased: Tick[];
  /** The gate-pass token, when a gate pass was scanned. */
  documentToken: string | null;
  refused: Refusal | null;
  reading: LabelReading;
}
