/**
 * Site first, then the project (design §4.17.4; R1).
 *
 * The clerk knows where they were; the project follows. One open project on the
 * site is filled in, two or more are asked, none blocks the save. A project may
 * also be chosen directly, with no site, for costs that belong to the PO rather
 * than a place (a permit).
 *
 * State is derived rather than synchronised: the effective project is computed
 * from the site, its candidates and the pick, so there is no effect to fall out
 * of step with the select.
 */

import { useState, type ChangeEvent } from 'react';

import { useList } from '../../api/hooks';
import { Banner, Field, Select } from '../../components/ui';
import { ControlledReferenceSelect } from '../../components/ui/ReferenceSelect';
import type { Project } from '../projects/types';
import type { Site } from '../settings/types';
import { candidateProjects } from './rules';

export interface SiteProjectState {
  site: string;
  setSite: (value: string) => void;
  /** Choosing the project directly, with no site. */
  direct: boolean;
  setDirect: (value: boolean) => void;
  pick: string;
  setPick: (value: string) => void;
  /** Open projects on the site (empty in direct mode). */
  candidates: Project[];
  /** All open projects, for direct mode. */
  allOpen: Project[];
  loading: boolean;
  /** The project that will be sent, or '' when none can be. */
  project: string;
  /** A site was chosen and has no open project: block the save. */
  blocked: boolean;
  /** The project is fixed from the URL; shown, not chosen. */
  fixed: boolean;
}

export function useSiteProject(fixedProject = ''): SiteProjectState {
  const [site, setSite] = useState('');
  const [direct, setDirect] = useState(Boolean(fixedProject));
  const [pick, setPick] = useState(fixedProject);

  const forSite = useList<Project>(
    'projects',
    { site, status: 'OPEN', page_size: 100 },
    { enabled: Boolean(site) && !direct },
  );
  const open = useList<Project>(
    'projects',
    { status: 'OPEN', page_size: 200 },
    { enabled: direct },
  );

  const candidates =
    site && !direct ? candidateProjects(Number(site), forSite.data?.results ?? []) : [];
  const loading = direct ? open.isLoading : Boolean(site) && forSite.isLoading;

  let project = '';
  if (direct) project = pick;
  else if (candidates.length === 1) project = String(candidates[0].id);
  else if (candidates.length > 1 && candidates.some((c) => String(c.id) === pick)) project = pick;

  const blocked =
    !direct && Boolean(site) && !forSite.isLoading && !forSite.isError && candidates.length === 0;

  return {
    site,
    setSite,
    direct,
    setDirect,
    pick,
    setPick,
    candidates,
    allOpen: open.data?.results ?? [],
    loading,
    project,
    blocked,
    fixed: Boolean(fixedProject),
  };
}

const label = (p: Project) => `${p.po_number || p.reference} ${p.title ?? ''}`.trim();

const LINK = 'min-h-[44px] self-start text-sm font-medium text-slate-700 underline';

export function SiteProjectFields({
  state,
  idPrefix,
  siteError,
  projectError,
  siteOptional = false,
}: {
  state: SiteProjectState;
  idPrefix: string;
  siteError?: string;
  projectError?: string;
  /** Allowance requests may name no site at all. */
  siteOptional?: boolean;
}) {
  const sites = useList<Site>('sites', { page_size: 300 }, { enabled: !state.direct });
  const set = (event: ChangeEvent<HTMLSelectElement>) => {
    state.setSite(event.target.value);
    state.setPick('');
  };

  return (
    <>
      {!state.direct ? (
        <Field label="Site" htmlFor={`${idPrefix}-site`} error={siteError}>
          <ControlledReferenceSelect
            resource="sites"
            id={`${idPrefix}-site`}
            value={state.site}
            onChange={set}
          >
            <option value="">{siteOptional ? 'No site' : 'Choose…'}</option>
            {(sites.data?.results ?? []).map((site) => (
              <option key={site.id} value={site.id}>
                {site.internal_ref} {site.name}
              </option>
            ))}
          </ControlledReferenceSelect>
        </Field>
      ) : null}

      {state.fixed ? null : state.direct ? (
        <button type="button" className={LINK} onClick={() => state.setDirect(false)}>
          Pick a site instead
        </button>
      ) : (
        <button
          type="button"
          className={LINK}
          onClick={() => {
            state.setSite('');
            state.setPick('');
            state.setDirect(true);
          }}
        >
          No site — pick a project
        </button>
      )}

      {state.blocked ? (
        <Banner tone="warning">This site has no open project. It cannot be saved here.</Banner>
      ) : null}

      {state.direct ? (
        <Field label="Project" htmlFor={`${idPrefix}-project`} error={projectError}>
          <Select
            id={`${idPrefix}-project`}
            value={state.pick}
            onChange={(event) => state.setPick(event.target.value)}
          >
            <option value="">Choose…</option>
            {state.allOpen.map((p) => (
              <option key={p.id} value={p.id}>
                {label(p)}
              </option>
            ))}
            {/* A project the URL named that is no longer open still shows its id. */}
            {state.pick && !state.allOpen.some((p) => String(p.id) === state.pick) ? (
              <option value={state.pick}>Project {state.pick}</option>
            ) : null}
          </Select>
        </Field>
      ) : state.candidates.length === 1 ? (
        <p className="text-sm text-slate-700">
          Project: <span className="font-medium text-slate-900">{label(state.candidates[0])}</span>
        </p>
      ) : state.candidates.length > 1 ? (
        <Field
          label="Which project?"
          htmlFor={`${idPrefix}-project`}
          hint="This site is on more than one open project."
          error={projectError}
        >
          <Select
            id={`${idPrefix}-project`}
            value={state.pick}
            onChange={(event) => state.setPick(event.target.value)}
          >
            <option value="">Choose…</option>
            {state.candidates.map((p) => (
              <option key={p.id} value={p.id}>
                {label(p)}
              </option>
            ))}
          </Select>
        </Field>
      ) : null}
    </>
  );
}
