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

/**
 * What the delivery's own "For site" says (Q2). What it said at receipt wins;
 * if it said nothing, the sites its lines are earmarked for now, so the header
 * never contradicts the lines under it.
 */
export function deliverySiteText(
  deliverySiteName: string | undefined,
  lines: { for_site_name?: string; earmarked_now?: { site: number; name: string }[] }[],
): string {
  if (deliverySiteName) return deliverySiteName;
  const sites = new Set<string>();
  for (const line of lines) {
    if (line.for_site_name) sites.add(line.for_site_name);
    for (const s of line.earmarked_now ?? []) sites.add(s.name);
  }
  if (sites.size === 0) return 'Not for a particular site';
  if (sites.size === 1) {
    const [only] = [...sites];
    const saidOnLines = lines.some((line) => line.for_site_name === only);
    return saidOnLines ? only : `${only} (earmarked later)`;
  }
  return 'Several sites — see each line';
}

type SiteNamed = { for_site_name?: string; for_site_ref?: string };

function siteLabel(site: SiteNamed): string {
  const name = site.for_site_name ?? '';
  const ref = site.for_site_ref ?? '';
  return ref && name ? `${ref} — ${name}` : ref || name;
}

/**
 * The Gate-in list's "Site" cell (Elias, 2026-10-10): "ID — name" of the
 * delivery's site, else of the sites its lines name, else a dash.
 */
export function listSiteText(delivery: SiteNamed, lines: SiteNamed[]): string {
  const own = siteLabel(delivery);
  if (own) return own;
  const sites = [...new Set(lines.map(siteLabel).filter(Boolean))];
  return sites.length > 0 ? sites.join(', ') : '—';
}
