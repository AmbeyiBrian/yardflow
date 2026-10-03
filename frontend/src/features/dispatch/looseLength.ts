/**
 * Loose cable at gate-out, as pure functions (D10; design §7.3c).
 *
 * A cable line names a drum (scanned), or takes loose length from what is not
 * on a drum. The ledger keeps the two apart, so the choice decides the line's
 * tracking mode.
 */

type Mode = 'BULK' | 'SERIALIZED' | 'REEL';

/** The label of the choice a cable line offers when no drum has been scanned. */
export const LOOSE_LENGTH_LABEL = 'Loose length';

/** The line's tracking mode, given what was scanned and what was chosen. */
export function lineTrackingMode(args: {
  itemMode: Mode;
  scanned: 'unit' | 'reel' | null;
  looseLength: boolean;
  /** A serialized item going out untagged, with the reason recorded (D3). */
  untagged: boolean;
}): Mode {
  if (args.scanned === 'unit') return 'SERIALIZED';
  if (args.scanned === 'reel') return 'REEL';
  if (args.untagged) return 'BULK';
  if (args.itemMode === 'REEL' && args.looseLength) return 'BULK';
  return args.itemMode;
}

/** The label on the length field. */
export function lengthLabel(uom: string | undefined): string {
  return `Length${uom ? ` (${uom})` : ''}`;
}

/** What to say when a cable line has neither a drum nor loose length. */
export function drumOrLooseMessage(itemName: string): string {
  return `${itemName} is cable. Scan the drum that is going, or choose ${LOOSE_LENGTH_LABEL}.`;
}
