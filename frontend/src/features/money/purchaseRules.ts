/**
 * Pure checks for a site purchase (design §4.19.3; R7, R9).
 *
 * The server (`finance.record_site_purchase`) is the authority and repeats every
 * rule; these let the form say "no" before sending, and keep the arithmetic in
 * integer cents so the running total matches the server's to the shilling.
 */

import { fromCents, toCents } from './rules';

export type PurchaseDestination = 'USED_AT_SITE' | 'INTO_YARD';

export interface PurchaseLineDraft {
  /** Catalogue item id, or '' for free text. */
  item_type: string;
  description: string;
  quantity: string;
  unit_price: string;
}

/** `quantity × unit_price` rounded to whole cents (§4.19.2: "rounded to 2 places"). NaN if unparseable. */
export function lineTotal(quantity: string, unitPrice: string): number {
  const qty = Number(String(quantity).replace(/,/g, ''));
  const price = toCents(unitPrice);
  if (String(quantity).trim() === '' || !Number.isFinite(qty) || Number.isNaN(price)) return NaN;
  return Math.round(qty * price);
}

/** The sum of the lines that have a figure, in cents. Lines that do not parse add nothing. */
export function purchaseTotal(lines: readonly PurchaseLineDraft[]): number {
  return lines.reduce((sum, l) => {
    const t = lineTotal(l.quantity, l.unit_price);
    return Number.isNaN(t) ? sum : sum + t;
  }, 0);
}

export function totalText(lines: readonly PurchaseLineDraft[]): string {
  return fromCents(purchaseTotal(lines));
}

/** A line is blank when nothing was entered on it; blank lines are dropped, not errors. */
export function isBlankLine(l: PurchaseLineDraft): boolean {
  return !l.item_type && !l.description.trim() && !l.quantity.trim() && !l.unit_price.trim();
}

/**
 * Why the purchase cannot be sent, keyed by field (§4.19.3). Empty when fine.
 * Mirrors the server: at least one line, quantity > 0, price ≥ 0, an item or a
 * description on each line, and for INTO_YARD a `receive_into` and a catalogue
 * item on every line (`SITE_PURCHASE_YARD_NEEDS_CATALOGUE`).
 */
export function validatePurchase(input: {
  destination: PurchaseDestination;
  receiveInto: string;
  lines: readonly PurchaseLineDraft[];
}): Record<string, string> {
  const errors: Record<string, string> = {};
  const lines = input.lines.filter((l) => !isBlankLine(l));
  if (lines.length === 0) {
    errors.lines = 'Add at least one line.';
  }
  for (const l of lines) {
    if (!l.item_type && !l.description.trim()) {
      errors.lines = 'Each line needs an item or a description.';
      break;
    }
    if (!(Number(l.quantity) > 0)) {
      errors.lines = 'Each line needs a quantity above zero.';
      break;
    }
    const price = toCents(l.unit_price);
    if (Number.isNaN(price) || price < 0) {
      errors.lines = 'Each line needs a unit price of zero or more.';
      break;
    }
  }
  if (input.destination === 'INTO_YARD') {
    if (!input.receiveInto) errors.receive_into = 'Which yard or store will it be received into?';
    if (!errors.lines && lines.some((l) => !l.item_type)) {
      errors.lines =
        'Goods going into the yard must be catalogue items, so they can be received into stock.';
    }
  }
  return errors;
}
