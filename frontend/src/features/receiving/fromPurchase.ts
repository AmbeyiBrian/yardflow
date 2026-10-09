/**
 * The "From purchase SP-…" badge on a draft delivery made by an INTO_YARD
 * site purchase (design §4.19.3, §4.19.13; R7).
 */

/** The draft's notes read "From site purchase SP-…" (§4.19.3) when the API has no number field. */
const NOTE_PATTERN = /\bSP-[A-Za-z0-9-]+/;

export function fromPurchaseNumber(gateIn: {
  source_type: string;
  source_purchase_number?: string | null;
  notes?: string;
}): string {
  if (gateIn.source_type !== 'PURCHASE') return '';
  if (gateIn.source_purchase_number) return gateIn.source_purchase_number;
  return gateIn.notes?.match(NOTE_PATTERN)?.[0] ?? '';
}
