/**
 * Pure asset helpers (design §4.20.4, §4.20.10; R14).
 */

export type ExpiryTone = 'ok' | 'soon' | 'lapsed' | 'none';

export interface ExpiryStatus {
  tone: ExpiryTone;
  /** Whole days from `today`; negative once lapsed. */
  days: number | null;
  label: string;
}

/** The sweep alerts at 30 days (§4.20.4); the chip turns amber at the same point. */
export const EXPIRY_WARNING_DAYS = 30;

function dayNumber(iso: string): number {
  const [y, m, d] = iso.slice(0, 10).split('-').map(Number);
  return Date.UTC(y, m - 1, d) / 86_400_000;
}

export function todayISO(now: Date = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

/** Amber within 30 days, red once lapsed; nothing for a blank date. */
export function expiryStatus(
  date: string | null | undefined,
  today: string = todayISO(),
): ExpiryStatus {
  if (!date) return { tone: 'none', days: null, label: '' };
  const days = dayNumber(date) - dayNumber(today);
  if (days < 0) return { tone: 'lapsed', days, label: `Expired ${date}` };
  if (days <= EXPIRY_WARNING_DAYS) {
    return {
      tone: 'soon',
      days,
      label: days === 0 ? 'Expires today' : `Expires in ${days} ${days === 1 ? 'day' : 'days'}`,
    };
  }
  return { tone: 'ok', days, label: `Valid to ${date}` };
}

/** Upper-case, spaces and dashes stripped: the server's `tag_key` (§4.20.2). */
export function normaliseTag(tag: string): string {
  return tag.replace(/[\s-]+/g, '').toUpperCase();
}

/** A handover or closing may be back-dated, never future (`ASSET_DATE_IN_FUTURE`). */
export function dateInFuture(date: string, today: string = todayISO()): boolean {
  return date > today;
}

/** First and last day of a month, for the Fuel panel's selector. */
export function monthRange(month: string): { from: string; to: string } {
  const [y, m] = month.split('-').map(Number);
  const last = new Date(Date.UTC(y, m, 0)).getUTCDate();
  const mm = String(m).padStart(2, '0');
  return { from: `${y}-${mm}-01`, to: `${y}-${mm}-${String(last).padStart(2, '0')}` };
}

/** The last `count` months as `YYYY-MM`, newest first. */
export function recentMonths(count: number, now: Date = new Date()): string[] {
  const out: string[] = [];
  for (let i = 0; i < count; i += 1) {
    const d = new Date(now.getFullYear(), now.getMonth() - i, 1);
    out.push(`${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`);
  }
  return out;
}
