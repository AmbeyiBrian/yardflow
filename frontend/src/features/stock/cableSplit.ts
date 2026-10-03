/** Cable on drums and cable loose, said under the quantity (D10; design §7.3c). */

import { trimQuantity } from './boxHelpers';

function grouped(value: string | number): string {
  const text = trimQuantity(value);
  const [whole, fraction] = text.split('.');
  const spaced = Number(whole).toLocaleString('en-US');
  return fraction ? `${spaced}.${fraction}` : spaced;
}

/**
 * "1,000 m on drums · 240 m loose", or null for a row that has no split
 * (any item that is not tracked by drum, or an older server).
 */
export function cableSplitText(row: {
  on_drums?: string | number | null;
  loose?: string | number | null;
  uom: string;
}): string | null {
  if (row.on_drums == null || row.loose == null) return null;
  return `${grouped(row.on_drums)} ${row.uom} on drums · ${grouped(row.loose)} ${row.uom} loose`;
}
