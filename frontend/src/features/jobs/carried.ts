/**
 * What one person is carrying, merged into one list (design §3.1, §4.10; I1).
 *
 * Shared by the closeout picker (T5.10) and the custody screens (T5.10, T5.11)
 * because getting it wrong is invisible until somebody cannot report something
 * they are holding — which is exactly what happened here.
 *
 * The ledger tracks three kinds of thing and answers "what does this person
 * have?" in two different shapes:
 *
 *  - `/stock/custody` — a *balance* at their PERSON node, for everything
 *  - `/serials` and `/drums` — the *identified* items located at that node
 *
 * Those overlap, and neither alone is right:
 *
 * **Balances alone** lose the serial number and the drum number, which is what a
 * technician reads off the label and what a serialized closeout line needs.
 *
 * **Identified items alone** silently drop the commonest case in the yard. When a
 * storekeeper cuts 120 m off a 500 m drum for a technician, §3.1 keeps the drum
 * in the yard and puts a *quantity* on the technician's node — there is no drum
 * object with their name on it. A picker built from `/drums` shows them nothing,
 * and the cable they are actually carrying cannot be reported at all.
 *
 * So: identified items become their own rows, and a balance becomes a "loose"
 * row for whatever that leaves over — the 120 m of cut cable, the untagged
 * spares. Subtracting is what stops the same radio appearing twice, once as a
 * serial and once as a quantity.
 */

import type { CustodyBalance, Reel, SerialUnit } from './types';

export interface Carried {
  key: string;
  itemType: number;
  itemName: string;
  uom: string;
  /** How much is there. `1` for a serialized unit, the remaining length for a drum. */
  available: string;
  condition: string;
  ownerClient: string;
  /** The serial or drum number, when this row is an identified thing. */
  reference?: string;
  serialUnit?: number;
  reel?: number;
  /** True for a cut length or an untagged quantity — reported as a number. */
  loose: boolean;
}

export function mergeCarried({
  balances,
  serials,
  drums,
}: {
  balances: CustodyBalance[];
  serials: SerialUnit[];
  drums: Reel[];
}): Carried[] {
  const rows: Carried[] = [];
  // What the identified rows already account for, per item type, so the loose
  // remainder can be worked out below.
  const identified = new Map<number, number>();

  for (const unit of serials) {
    rows.push({
      key: `serial-${unit.id}`,
      itemType: unit.item_type,
      itemName: unit.item_name,
      uom: 'ea',
      available: '1',
      condition: unit.condition,
      ownerClient: unit.owner_client_name,
      reference: unit.serial_number,
      serialUnit: unit.id,
      loose: false,
    });
    identified.set(unit.item_type, (identified.get(unit.item_type) ?? 0) + 1);
  }

  for (const reel of drums) {
    rows.push({
      key: `reel-${reel.id}`,
      itemType: reel.item_type,
      itemName: reel.item_name,
      uom: reel.uom,
      available: reel.remaining_length,
      condition: reel.condition,
      ownerClient: reel.owner_client_name,
      reference: reel.drum_number,
      reel: reel.id,
      loose: false,
    });
    identified.set(
      reel.item_type,
      (identified.get(reel.item_type) ?? 0) + Number(reel.remaining_length),
    );
  }

  for (const balance of balances) {
    const accountedFor = identified.get(balance.item_type) ?? 0;
    const loose = Number(balance.quantity) - accountedFor;
    // A rounding tolerance rather than `> 0`: lengths are three decimal places,
    // and a stray thousandth should not put a phantom row in front of somebody.
    if (loose <= 0.0005) continue;
    rows.push({
      key: `loose-${balance.id}`,
      itemType: balance.item_type,
      itemName: balance.item_name,
      uom: balance.uom,
      available: String(Number(loose.toFixed(3))),
      condition: balance.condition,
      ownerClient: balance.owner_client_name,
      loose: true,
    });
  }

  return rows;
}

/** How a row describes itself on screen. */
export function carriedLabel(item: Carried): string {
  if (item.reference) return `${item.itemName} · ${item.reference}`;
  return item.itemName;
}
