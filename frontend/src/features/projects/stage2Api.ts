/**
 * Finance stage 2 project endpoints (design §4.19.10; R9, R10).
 *
 * Field names for the budget position and the derived site dates are the
 * frontend's reading of §4.19.5 and §4.19.6; the design fixes the figures, not
 * the JSON keys. Every money figure is a decimal string.
 */

import { useAction, useList, useResource } from '../../api/hooks';

/** What a budget component is, per the §4.19.5 table. */
export type BudgetKind = 'COST' | 'ALLOWANCE' | 'FLOAT' | 'PURCHASE' | 'SUBCONTRACT';

/** One row of the §4.19.5 table: where a shilling of spent or committed money comes from. */
export interface BudgetComponent {
  kind: BudgetKind;
  spent: string;
  committed: string;
}

/** An entry recorded past the budget, with the reason its recorder gave (R9). */
export interface OverBudgetEntry {
  /** `expense`, `allowance`, `purchase`. */
  document_type: string;
  id: number;
  reference?: string;
  over_budget_by?: string | null;
  /** Blank when it was replayed from a phone that could not know (§4.19.5). */
  over_budget_reason: string;
  recorded_on?: string | null;
}

/** `GET /projects/{id}/budget`. `budget` and `remaining` are null with no PO (R12). */
export interface BudgetPosition {
  budget: string | null;
  spent: string;
  committed: string;
  /** Awaiting approval: outside both spent and committed. */
  pending: string;
  remaining: string | null;
  components?: BudgetComponent[];
  over_budget_entries?: OverBudgetEntry[];
}

/** `GET /project-sites?project=`: the through row, with derived dates (§4.19.6). */
export interface ProjectSite {
  id: number;
  project: number;
  site: number;
  site_ref?: string;
  site_name?: string;
  mobilised_on: string | null;
  accepted_on: string | null;
  /** Server's verdict: `accepted_on` set and a certificate attached. */
  is_accepted?: boolean;
  /** Earliest final-or-only gate-out release to this site, for this project. */
  first_collection_at: string | null;
  /** Latest gate-out release. */
  last_dispatch_at: string | null;
}

export interface ProjectSiteInput {
  id: number;
  mobilised_on?: string | null;
  accepted_on?: string | null;
}

const SITES = 'project-sites';

export const useProjectBudget = (projectId: string | number | undefined) =>
  useResource<BudgetPosition>(`projects/${projectId}/budget`, undefined, {
    enabled: projectId !== undefined && projectId !== '',
  });

export const useProjectSites = (projectId: string | number | undefined) =>
  useList<ProjectSite>(SITES, { project: projectId, page_size: 100 });

export const useUpdateProjectSite = () =>
  useAction<ProjectSiteInput, ProjectSite>({
    resource: SITES,
    method: 'patch',
    path: (b) => String(b.id),
  });
