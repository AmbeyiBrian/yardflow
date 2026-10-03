/**
 * The pure parts of the item picker (design §7.3b, C9): ranking, the
 * recently-used list and the "N of M shown" text. No React, no DOM, so they
 * are unit-tested on their own and mirror the server's search order exactly.
 */

export interface SearchableItem {
  id: number;
  name: string;
  code?: string | null;
  description?: string | null;
  is_archived?: boolean;
}

export const PAGE_SIZE = 20;
export const RECENT_LIMIT = 8;

function rank(item: SearchableItem, needle: string): number | null {
  const name = (item.name ?? '').toLowerCase();
  const code = (item.code ?? '').toLowerCase();
  const description = (item.description ?? '').toLowerCase();
  if (name.startsWith(needle)) return 0;
  if (name.includes(needle)) return 1;
  if (code.includes(needle)) return 2; // includes covers startsWith
  if (description.includes(needle)) return 3;
  return null;
}

/**
 * Same order as the server: name starts with the text, then name contains it,
 * then code, then description; alphabetical by name within each, then id.
 */
export function rankItems<T extends SearchableItem>(
  items: T[],
  text: string,
  limit = PAGE_SIZE,
): { matches: T[]; total: number } {
  const needle = text.trim().toLowerCase();
  if (!needle) return { matches: [], total: 0 };

  const ranked: { item: T; rank: number }[] = [];
  for (const item of items) {
    if (item.is_archived) continue;
    const r = rank(item, needle);
    if (r !== null) ranked.push({ item, rank: r });
  }
  ranked.sort(
    (a, b) =>
      a.rank - b.rank ||
      (a.item.name ?? '').localeCompare(b.item.name ?? '') ||
      a.item.id - b.item.id,
  );
  return { matches: ranked.slice(0, limit).map((r) => r.item), total: ranked.length };
}

/** "20 of 340 shown — keep typing", or nothing when everything is shown. */
export function shownText(shown: number, total: number): string {
  return total > shown ? `${shown} of ${total} shown — keep typing` : '';
}

export interface RecentItem {
  id: number;
  name: string;
  code: string;
  uom: string;
  tracking_mode: string;
  category_name: string;
}

export const recentKey = (orgSlug: string) => `yardflow.${orgSlug}.recent-items`;

type StorageLike = Pick<Storage, 'getItem' | 'setItem'>;

function storage(): StorageLike | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage;
  } catch {
    return null;
  }
}

export function readRecent(orgSlug: string, store: StorageLike | null = storage()): RecentItem[] {
  try {
    const raw = store?.getItem(recentKey(orgSlug));
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? (parsed as RecentItem[]).slice(0, RECENT_LIMIT) : [];
  } catch {
    return [];
  }
}

/** Newest first, one entry per item, at most 8. Returns the new list. */
export function pushRecent(
  orgSlug: string,
  item: {
    id: number;
    name: string;
    code?: string | null;
    uom?: string | null;
    tracking_mode?: string | null;
    category_name?: string | null;
  },
  store: StorageLike | null = storage(),
): RecentItem[] {
  const entry: RecentItem = {
    id: item.id,
    name: item.name,
    code: item.code ?? '',
    uom: item.uom ?? '',
    tracking_mode: item.tracking_mode ?? '',
    category_name: item.category_name ?? '',
  };
  const next = [entry, ...readRecent(orgSlug, store).filter((r) => r.id !== entry.id)].slice(
    0,
    RECENT_LIMIT,
  );
  try {
    store?.setItem(recentKey(orgSlug), JSON.stringify(next));
  } catch {
    // Blocked storage only loses the convenience.
  }
  return next;
}
