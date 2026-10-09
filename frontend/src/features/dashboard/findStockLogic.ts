/**
 * What Enter and a scan do on Home's Find stock card (E8, design §7.3d).
 *
 * Both ask `/stock/lookup` first, since a serial, tag, drum or box code goes
 * straight to its page. They differ only when nothing matches: a scan was an
 * identifier, so it says so; Enter may have been a name half-typed, so the item
 * list underneath keeps answering and no error is shown unless that is empty.
 */

export const MIN_CHARS = 2;

export type FindVia = 'scan' | 'enter';

export type FindOutcome =
  | { kind: 'navigate'; to: string }
  /** `text: null` leaves the item list as the answer. */
  | { kind: 'message'; text: string | null };

export function nothingMatches(value: string): string {
  return `Nothing here matches "${value}".`;
}

/** Whether typed text is long enough to search the item list. */
export function searchable(text: string): boolean {
  return text.trim().length >= MIN_CHARS;
}

/**
 * `resource` is the lookup's answer, or null on a 404. `listCount` is how many
 * items the list is showing for the typed text.
 */
export function resolveFind(
  via: FindVia,
  value: string,
  resource: string | null,
  listCount: number,
): FindOutcome {
  if (resource) return { kind: 'navigate', to: resource };
  if (via === 'scan') return { kind: 'message', text: nothingMatches(value) };
  return { kind: 'message', text: listCount > 0 ? null : nothingMatches(value) };
}
