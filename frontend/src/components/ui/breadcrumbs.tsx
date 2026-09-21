/**
 * Breadcrumbs (design §7.6; Epic P).
 *
 * P2: the trail is derived from the URL in one place rather than passed by each
 * screen. Twenty-six screens render a `PageHeader`, and a trail threaded through
 * all of them drifts the first time a route is renamed — a crumb that lies about
 * where you are is worse than no crumb at all.
 *
 * What it cannot derive is the *name* of a record. `/projects/42` should read
 * "Projects › PRJ-000042", and only the screen that loaded the project knows
 * that. So a detail screen publishes its own label through `useCrumb`, and until
 * it does the crumb holds a placeholder rather than flashing `42` and then
 * correcting itself (P3).
 *
 * Parents are declared, not chopped off the end of the URL. `/expenses/new` has
 * no `/expenses` above it and `/jobs/custody` is not a job; a trail built by
 * splitting on slashes would invent both.
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { Link, useLocation, useSearchParams } from 'react-router-dom';
import type { ReactNode } from 'react';

import { PERM, type Permission } from '../../auth/permissions';
import { useSession } from '../../auth/session';

interface Step {
  /** What the crumb says, or `null` when only the screen can know. */
  label: string | null;
  /** The pattern of the step above. `null` marks the top of a trail. */
  parent: string | null;
  /** What it takes to open it. A crumb you cannot open is not a link (P6). */
  anyOf?: Permission[];
}

/**
 * The trail, by route pattern.
 *
 * A route absent from here has no trail, which is how P5 is met: the top-level
 * screens are simply not listed, because a crumb reading `Home › Projects`
 * under a sidebar entry reading *Projects* is noise.
 */
const TRAIL: Record<string, Step> = {
  '/gate-in': { label: 'Gate-in', parent: null, anyOf: [PERM.GATE_IN_POST] },
  '/gate-in/new': { label: 'New delivery', parent: '/gate-in' },
  '/gate-in/:id': { label: null, parent: '/gate-in' },
  '/gate-in/:id/edit': { label: 'Correct it', parent: '/gate-in/:id' },

  '/gate-out': {
    label: 'Gate-out',
    parent: null,
    anyOf: [PERM.GATE_OUT_REQUEST, PERM.GATE_OUT_RELEASE],
  },
  '/gate-out/new': { label: 'New request', parent: '/gate-out' },
  '/gate-out/offline': { label: 'Offline release', parent: '/gate-out' },
  '/gate-out/:id': { label: null, parent: '/gate-out' },
  '/gate-out/:id/edit': { label: 'Amend it', parent: '/gate-out/:id' },

  '/approvals': {
    label: 'Approvals',
    parent: null,
    anyOf: [PERM.GATE_OUT_APPROVE, PERM.DISPOSAL_APPROVE, PERM.PROJECT_VIEW_COST],
  },
  '/approvals/:id': { label: null, parent: '/approvals' },

  '/projects': {
    label: 'Projects',
    parent: null,
    anyOf: [PERM.PROJECT_VIEW_COST, PERM.CATALOGUE_MANAGE],
  },
  '/projects/:id': { label: null, parent: '/projects' },
  // No `/expenses` list exists above this one. It belongs to the project it was
  // opened from, which `Breadcrumbs` reads off the query string.
  '/expenses/new': { label: 'Record an expense', parent: '/projects' },

  '/stock': { label: 'Stock', parent: null },
  '/stock/drums': { label: 'Drums', parent: '/stock' },
  '/stock/drums/:drumNumber': { label: null, parent: '/stock/drums' },
  '/stock/serials/:serialNumber': { label: null, parent: '/stock' },
  '/stock/transfers': { label: 'Transfers', parent: '/stock' },
  '/stock/counts': { label: 'Counts', parent: '/stock' },
  '/stock/counts/:id': { label: null, parent: '/stock/counts' },

  '/jobs': { label: 'Jobs', parent: null, anyOf: [PERM.JOB_CLOSEOUT, PERM.REPORT_VIEW_ALL] },
  '/jobs/custody': { label: 'What I hold', parent: '/jobs' },
  '/jobs/reconciliation': { label: 'Reconciliation', parent: '/jobs' },
  '/jobs/:id': { label: null, parent: '/jobs' },

  '/quarantine': {
    label: 'Quarantine',
    parent: null,
    anyOf: [PERM.STOCK_ADJUST, PERM.GATE_IN_POST],
  },
  '/quarantine/decisions': { label: 'Decisions', parent: '/quarantine' },

  '/reports': { label: 'Reports', parent: null, anyOf: [PERM.REPORT_VIEW_ALL] },
  '/reports/retention': { label: 'Retention', parent: '/reports' },
  '/reports/:slug': { label: null, parent: '/reports' },
};

/** Longest first, so `/stock/drums/:n` is tried before `/stock/:n` would be. */
const PATTERNS = Object.keys(TRAIL).sort(
  (a, b) => b.split('/').length - a.split('/').length || b.length - a.length,
);

export interface Crumb {
  pattern: string;
  /** Where it points, with the current parameters filled in. */
  to: string;
  label: string | null;
  anyOf?: Permission[];
}

/**
 * Match a path against the table.
 *
 * Done by hand rather than with `useMatch`, which is a hook and so cannot be
 * called in a loop. Exported for its own test.
 */
export function matchPattern(pathname: string): string | null {
  const path = pathname.replace(/\/+$/, '') || '/';
  const pathParts = path.split('/');

  for (const pattern of PATTERNS) {
    const patternParts = pattern.split('/');
    if (patternParts.length !== pathParts.length) continue;
    const ok = patternParts.every(
      (part, index) => part.startsWith(':') || part === pathParts[index],
    );
    if (ok) return pattern;
  }
  return null;
}

/**
 * Walk `parent` from the current pattern up to a top-level screen.
 *
 * Exported for its own test: this is all of the logic and none of the
 * rendering, so it can be checked without a router or a session.
 */
export function buildTrail(
  pattern: string | null,
  fill: (pattern: string) => string,
): Crumb[] {
  const trail: Crumb[] = [];
  const seen = new Set<string>();
  let at = pattern;

  while (at && TRAIL[at]) {
    // A cycle in the table would hang the render. Refuse to loop rather than
    // trust the table to stay acyclic forever.
    if (seen.has(at)) break;
    seen.add(at);

    const step = TRAIL[at];
    trail.unshift({ pattern: at, to: fill(at), label: step.label, anyOf: step.anyOf });
    at = step.parent;
  }

  return trail;
}

/** Fill a pattern's parameters from the path currently showing. */
export function fillFrom(pathname: string, currentPattern: string) {
  const pathParts = pathname.replace(/\/+$/, '').split('/');
  const values: Record<string, string> = {};
  currentPattern.split('/').forEach((part, index) => {
    if (part.startsWith(':')) values[part] = pathParts[index] ?? '';
  });

  return (pattern: string) =>
    pattern
      .split('/')
      .map((part) => (part.startsWith(':') ? (values[part] ?? part) : part))
      .join('/') || '/';
}

/**
 * Labels published by the screens themselves, keyed by pattern.
 *
 * Keyed rather than a single value so a screen cannot overwrite the label of a
 * crumb above it.
 */
type Labels = Record<string, string>;

const CrumbContext = createContext<{
  labels: Labels;
  publish: (pattern: string, label: string | undefined) => void;
} | null>(null);

export function CrumbProvider({ children }: { children: ReactNode }) {
  const [labels, setLabels] = useState<Labels>({});

  // `publish` must keep one identity for the life of the provider.
  //
  // It used to be rebuilt whenever `labels` changed, and `useCrumb` lists it as
  // an effect dependency. So: the effect published a label, which changed
  // `labels`, which rebuilt `publish`, which re-ran the effect — whose cleanup
  // deleted the label, which changed `labels`, which rebuilt `publish`, which
  // re-ran the effect, which published the label again. Forever. And this
  // provider sits above the router's outlet, so that stream of updates starved
  // every navigation transition: the URL changed and the old screen stayed.
  // Reported as "the app gets stuck when you leave a report".
  //
  // `setLabels` is stable, so a callback over it with no dependencies is too.
  const publish = useCallback((pattern: string, label: string | undefined) => {
    setLabels((current) => {
      if (label === undefined) {
        if (!(pattern in current)) return current;
        const next = { ...current };
        delete next[pattern];
        return next;
      }
      // Same value: return the same object, or publishing on every render of
      // a detail screen would re-render the whole shell each time.
      if (current[pattern] === label) return current;
      return { ...current, [pattern]: label };
    });
  }, []);

  const value = useMemo(() => ({ labels, publish }), [labels, publish]);

  return <CrumbContext.Provider value={value}>{children}</CrumbContext.Provider>;
}

/** Which pattern the current URL matches, if any. */
function useCurrentPattern(): string | null {
  const location = useLocation();
  return useMemo(() => matchPattern(location.pathname), [location.pathname]);
}

/**
 * Name a crumb — `useCrumb(project.data?.reference)`.
 *
 * Called with `undefined` while the record loads, which leaves the placeholder
 * in place rather than showing a raw id and then correcting it (P3).
 *
 * `forPattern` names a crumb **above** this screen, which one case needs: the
 * expense form sits under a project it never renders, so nothing else is in a
 * position to say what that project is called. Without it the crumb would hold
 * its placeholder forever, which reads as a screen still loading.
 */
export function useCrumb(label: string | undefined, forPattern?: string): void {
  const context = useContext(CrumbContext);
  const current = useCurrentPattern();
  const pattern = forPattern ?? current;
  const publish = context?.publish;

  useEffect(() => {
    if (!publish || !pattern) return;
    publish(pattern, label);
    // Cleared on the way out, so a stale name cannot sit over the next record.
    return () => publish(pattern, undefined);
  }, [publish, pattern, label]);
}

export function Breadcrumbs() {
  const location = useLocation();
  const [params] = useSearchParams();
  const { has } = useSession();
  const context = useContext(CrumbContext);
  const pattern = useCurrentPattern();
  const project = params.get('project');

  const trail = useMemo(() => {
    if (!pattern) return [];
    const built = buildTrail(pattern, fillFrom(location.pathname, pattern));

    // `/expenses/new?project=42` sits under that project rather than under the
    // list: the screen it was opened from is what a person means by "back".
    if (pattern === '/expenses/new' && project) {
      built.splice(built.length - 1, 0, {
        pattern: '/projects/:id',
        to: `/projects/${project}`,
        label: null,
      });
    }
    return built;
  }, [pattern, location.pathname, project]);

  // P5: a trail of one is the screen you are already on.
  if (trail.length < 2) return null;

  const labelled = trail.map((crumb) => ({
    ...crumb,
    text: crumb.label ?? context?.labels[crumb.pattern] ?? null,
  }));
  const parent = labelled[labelled.length - 2];

  return (
    <nav aria-label="Breadcrumb" className="mb-3 text-sm">
      {/*
        P4: on a phone the trail collapses to the one link people actually use,
        the level above. A CSS breakpoint rather than a width measured in
        JavaScript, so it is right on first paint instead of after a measure.
      */}
      <div className="md:hidden">
        <CrumbLink crumb={parent} has={has} className="text-slate-600">
          ‹ {parent.text ?? <Placeholder />}
        </CrumbLink>
      </div>

      <ol className="hidden flex-wrap items-center gap-1 md:flex">
        {labelled.map((crumb, index) => (
          <li key={crumb.pattern} className="flex items-center gap-1">
            {index > 0 ? (
              <span aria-hidden="true" className="text-slate-400">
                ›
              </span>
            ) : null}
            {index === labelled.length - 1 ? (
              // P4: you are here, so it is not a link.
              <span aria-current="page" className="font-medium text-slate-900">
                {crumb.text ?? <Placeholder />}
              </span>
            ) : (
              <CrumbLink
                crumb={crumb}
                has={has}
                className="text-slate-600 hover:text-slate-900"
              >
                {crumb.text ?? <Placeholder />}
              </CrumbLink>
            )}
          </li>
        ))}
      </ol>
    </nav>
  );
}

function CrumbLink({
  crumb,
  has,
  className,
  children,
}: {
  crumb: Crumb & { text?: string | null };
  has: (permission: Permission) => boolean;
  className?: string;
  children: ReactNode;
}) {
  // P6: a crumb pointing at a screen this person cannot open is plain text.
  // Offering the link and bouncing them off it is a worse answer than showing
  // where they are and leaving it there.
  const allowed = !crumb.anyOf || crumb.anyOf.some(has);
  if (!allowed) return <span className={className}>{children}</span>;

  return (
    <Link to={crumb.to} className={className}>
      {children}
    </Link>
  );
}

/** A record whose name has not arrived yet. Never the raw id. */
function Placeholder() {
  return (
    <span
      aria-label="loading"
      className="inline-block h-3 w-16 animate-pulse rounded bg-slate-200 align-middle"
    />
  );
}
