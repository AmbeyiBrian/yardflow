/**
 * The pure parts of reading the offline bundle for the Money forms (design
 * §4.17.8; R6).
 *
 * Offline, every picker is filled from what the last bundle stored rather than
 * from the network, so a clerk at a site with no signal can still pick the site,
 * the project, the category and the float. Selection and shaping live here, away
 * from React and Dexie, so the rules can be tested directly; `reference.ts`
 * does the reading.
 */

import type { AllowanceLimits } from './types';

/** A project as the pickers need it — the network's `Project` and the bundle's row both fit. */
export interface ProjectChoice {
  id: number;
  reference: string;
  po_number: string;
  title: string;
}

export interface SiteChoice {
  id: number;
  name: string;
  internal_ref: string;
}

/** A site row as the bundle stores it (`sync/views.py`). */
export interface BundleSite extends SiteChoice {
  open_projects?: (Pick<ProjectChoice, 'id' | 'reference'> & Partial<ProjectChoice>)[];
}

export interface CategoryChoice {
  id: number;
  name: string;
  kind: 'GENERAL' | 'FUEL' | 'CASUAL_LABOUR';
}

export interface FloatChoice {
  id: number;
  number: string;
  balance: string;
}

const toProject = (p: NonNullable<BundleSite['open_projects']>[number]): ProjectChoice => ({
  id: p.id,
  reference: p.reference,
  po_number: p.po_number ?? '',
  title: p.title ?? '',
});

/**
 * Open projects on one site, from the bundle (R1). The server already limited
 * the list to OPEN projects, so there is nothing to filter: one is filled in,
 * two or more are asked, none blocks the save.
 */
export function bundleSiteProjects(sites: readonly BundleSite[], siteId: number): ProjectChoice[] {
  return (sites.find((s) => s.id === siteId)?.open_projects ?? []).map(toProject);
}

/**
 * Every open project across all sites, once each and in reference order, for the
 * "no site — pick a project" path. A project on several sites appears under each
 * of them in the bundle, so it is de-duplicated by id.
 */
export function bundleOpenProjects(sites: readonly BundleSite[]): ProjectChoice[] {
  const seen = new Map<number, ProjectChoice>();
  for (const site of sites) {
    for (const p of site.open_projects ?? []) if (!seen.has(p.id)) seen.set(p.id, toProject(p));
  }
  return [...seen.values()].sort((a, b) => a.reference.localeCompare(b.reference));
}

/** The bundle's categories carry only what the form uses: name and kind. */
export function bundleCategories(rows: readonly CategoryChoice[]): CategoryChoice[] {
  return rows.map(({ id, name, kind }) => ({ id, name, kind }));
}

/** Floats arrive PAID and unclosed with their balance already worked out. */
export function bundleFloats(rows: readonly (FloatChoice & { amount?: string })[]): FloatChoice[] {
  return rows.map((f) => ({ id: f.id, number: f.number, balance: f.balance ?? f.amount ?? '' }));
}

/**
 * The limits row. The bundle stores `[limits]` so it fits the table's list shape;
 * an empty object (an older server, or none set) means no early warning, as
 * `checkLimit` already treats a missing key.
 */
export function bundleLimits(
  rows: readonly Record<string, unknown>[],
): AllowanceLimits | undefined {
  const first = rows[0];
  return first && Object.keys(first).length ? (first as unknown as AllowanceLimits) : undefined;
}
