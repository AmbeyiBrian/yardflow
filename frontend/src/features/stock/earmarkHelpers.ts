/** Pure helpers for earmarks on the stock screens (Q2, Q4; design §4.16.6-7). */

import { trimQuantity } from './boxHelpers';

export interface EarmarkPortion {
  site: number;
  name: string;
  quantity: string | number;
}

/** What a stock row carries once earmarks exist. */
export interface EarmarkRow {
  node: number;
  item_type: number;
  owner_client: number | null;
  condition: string;
  earmarked?: EarmarkPortion[] | null;
  free?: string | number | null;
}

function grouped(value: string | number): string {
  const text = trimQuantity(value);
  const [whole, fraction] = text.split('.');
  const spaced = Number(whole).toLocaleString('en-US');
  return fraction ? `${spaced}.${fraction}` : spaced;
}

/**
 * "25 for Site Alpha · 10 for Site Bravo · 15 free", or null when nothing on
 * the row is earmarked (or an older server sent no split).
 */
export function earmarkSplitText(row: Pick<EarmarkRow, 'earmarked' | 'free'>): string | null {
  const earmarked = (row.earmarked ?? []).filter((e) => Number(e.quantity) > 0);
  if (earmarked.length === 0) return null;
  const parts = earmarked.map((e) => `${grouped(e.quantity)} for ${e.name}`);
  if (row.free != null && Number(row.free) > 0) parts.push(`${grouped(row.free)} free`);
  return parts.join(' · ');
}

/** A portion of a row that an earmark can be taken from: a site, or "free". */
export interface SourceOption {
  /** "free" or the site id as a string. */
  key: string;
  label: string;
  max: number;
}

export function bulkSourceOptions(row: Pick<EarmarkRow, 'earmarked' | 'free'>): SourceOption[] {
  const options: SourceOption[] = (row.earmarked ?? [])
    .filter((e) => Number(e.quantity) > 0)
    .map((e) => ({ key: String(e.site), label: `For ${e.name}`, max: Number(e.quantity) }));
  const free = Number(row.free ?? 0);
  if (free > 0) options.push({ key: 'free', label: 'Free', max: free });
  return options;
}

/** Whether a row has anything to change the earmark of. */
export function canChangeBulk(row: Pick<EarmarkRow, 'earmarked' | 'free'>): boolean {
  return bulkSourceOptions(row).length > 0;
}

export interface BulkChangeInput {
  from: string;
  to: string;
  quantity: string;
  reason: string;
}

/** Why the form cannot be sent yet, or null. */
export function bulkChangeProblem(options: SourceOption[], input: BulkChangeInput): string | null {
  const source = options.find((o) => o.key === input.from);
  if (!source) return 'Choose where the quantity is earmarked now.';
  const quantity = Number(input.quantity);
  if (!input.quantity.trim() || !Number.isFinite(quantity) || quantity <= 0) {
    return 'Enter a quantity above zero.';
  }
  if (quantity > source.max) {
    return `Only ${trimQuantity(source.max)} is ${source.label.toLowerCase()}.`;
  }
  if (input.from === (input.to || 'free')) return 'That is already its earmark.';
  if (!input.reason.trim()) return 'Say why the earmark is changing.';
  return null;
}

export function buildBulkChangeBody(row: EarmarkRow, input: BulkChangeInput) {
  return {
    node: row.node,
    item_type: row.item_type,
    owner_client: row.owner_client,
    condition: row.condition,
    from_site: input.from === 'free' ? null : Number(input.from),
    quantity: input.quantity.trim(),
    to_site: input.to ? Number(input.to) : null,
    reason: input.reason.trim(),
  };
}

export function earmarkLine(site: string | null | undefined): string {
  return site ? `Earmarked for ${site}` : 'Not earmarked';
}
