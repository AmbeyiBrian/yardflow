/** What a delivery line's "For site" cell says (Q2). */
export function lineSiteText(
  line: { for_site_name?: string; earmarked_now?: { site: number; name: string }[] },
  deliverySiteName?: string,
): string {
  const said = line.for_site_name || deliverySiteName || '';
  const now = (line.earmarked_now ?? []).map((s) => s.name);
  const nowText = now.join(', ');
  if (!said) return now.length > 0 ? `${nowText} (earmarked later)` : '—';
  if (now.length > 0 && !(now.length === 1 && now[0] === said)) return `${said} · now ${nowText}`;
  return said;
}
