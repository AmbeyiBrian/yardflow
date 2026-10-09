/**
 * T7.8 — role dashboards (design §7.4).
 *
 * The criterion: "each role lands on a dashboard **answering its first question
 * of the day**." Those questions are different, and the task spells them out:
 *
 * * storekeeper — pending gate-outs, low stock, exceptions
 * * owner — approvals waiting, overdue custody, unaccounted material
 * * technician — my jobs, my custody
 *
 * So this is not one dashboard with everything on it. A screen showing a
 * technician the approval queue teaches them to scroll past the part that
 * concerns them, and by the second week they are not reading any of it.
 *
 * **Composed from permissions, not from a role name.** A user with three roles
 * gets three sections, and a tenant that renames "Storekeeper" to "Stores
 * Controller" needs no change here (B4). Each section asks for what its
 * permission implies the person is responsible for.
 *
 * Every tile is a link. A dashboard that reports a number without offering the
 * screen behind it makes somebody go and find it, and the number stops being
 * worth reading.
 */

import { Link } from 'react-router-dom';

import { useList, useResource } from '../../api/hooks';
import { PERM } from '../../auth/permissions';
import { useSession } from '../../auth/session';
import { Banner, Card, Spinner } from '../../components/ui';
import { Stat } from '../../components/ui/data';
import { useProjectsWithoutPo } from '../projects/milestonesApi';
import type { GateOut } from '../dispatch/types';
import type { Job } from '../jobs/types';
import { ClockInCard } from '../attendance/ClockInCard';
import { FindStock } from './FindStock';

export default function DashboardPage() {
  const { user, has } = useSession();
  if (!user) return null;

  const isTechnician = has(PERM.JOB_CLOSEOUT);
  const isStorekeeper = has(PERM.GATE_IN_POST) || has(PERM.GATE_OUT_RELEASE);
  const isOwner = has(PERM.GATE_OUT_APPROVE) || has(PERM.REPORT_VIEW_ALL);
  const hasRole = isTechnician || isOwner || isStorekeeper;

  return (
    <div className="flex flex-col gap-5">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">
          {greeting()}, {user.full_name || 'there'}
        </h1>
        <p className="text-sm text-slate-600">
          {user.organization?.name ?? 'Platform administration'}
        </p>
      </div>

      {user.organization?.status === 'SUSPENDED' ? (
        // A2: reads work, writes do not. Better said once at the top than
        // discovered on the first failed save.
        <Banner tone="warning">
          This organization is suspended. You can read everything and change
          nothing until it is reinstated.
        </Banner>
      ) : null}

      {/* E8: "do we have it, and where?" comes before the role panels. */}
      {hasRole ? (
        <>
          <FindStock />
          <YardPanel />
        </>
      ) : null}

      {/* R13: every member clocks in, role or not. Below Find stock. */}
      <ClockInCard />

      {/* Ordered by immediacy rather than by seniority: whoever is holding
          material has the most time-sensitive question. */}
      {isTechnician ? <TechnicianPanel /> : null}
      {isOwner ? <OwnerPanel /> : null}
      {isStorekeeper ? <StorekeeperPanel /> : null}

      {!hasRole ? (
        <Card>
          <h2 className="mb-1 text-sm font-semibold text-slate-900">
            Nothing is assigned to you yet
          </h2>
          <p className="text-sm text-slate-600">
            Your account works, but it has no role — so there is nothing for a
            dashboard to answer. An administrator assigns roles in settings.
          </p>
        </Card>
      ) : null}
    </div>
  );
}

/** "What am I doing today, and what am I carrying?" (H1, I1) */
function TechnicianPanel() {
  const { user } = useSession();
  const jobs = useList<Job>('jobs', { assignee: 'me', open: 'true', page_size: 50 });
  const custody = useList<{ id: number; quantity: string }>('stock/custody', {
    holder: user?.id,
  });
  const expectations = useList<{ id: number; status: string }>('custody-expectations', {
    holder: user?.id,
    page_size: 100,
  });
  const transfers = useList<{ id: number; to_holder: number; status: string }>(
    'custody-transfers',
    { status: 'PENDING', page_size: 50 },
  );

  const overdue = (expectations.data?.results ?? []).filter(
    (row) => row.status === 'OVERDUE',
  ).length;
  const waiting = (transfers.data?.results ?? []).filter(
    (row) => row.to_holder === user?.id,
  ).length;

  return (
    <Panel title="Yours" loading={jobs.isLoading}>
      <Tile
        to="/jobs"
        label="Open jobs"
        value={jobs.data?.results.length ?? 0}
        hint="Assigned to you and not closed out."
      />
      <Tile
        to="/jobs/custody"
        label="Items you hold"
        value={custody.data?.results.length ?? 0}
        hint="Read from the ledger, so it matches the yard."
      />
      <Tile
        to="/jobs/custody"
        label="Overdue"
        value={overdue}
        tone={overdue > 0 ? 'bad' : 'good'}
        hint="Past the date it was due back."
      />
      {waiting > 0 ? (
        <Tile
          to="/jobs/custody"
          label="Handovers to accept"
          value={waiting}
          tone="warn"
          hint="Still on the other person's record until you accept."
        />
      ) : null}
    </Panel>
  );
}

/** "What needs me, and what is unaccounted for?" (F4, I4, H4) */
function OwnerPanel() {
  const approvals = useList<{ id: number }>('approvals/pending', { page_size: 50 });
  const overdue = useResource<{ total: number }>('custody/overdue');
  const exceptions = useResource<{ count: number }>('exceptions');
  const disposals = useList<{ id: number; status: string }>('disposals', {
    status: 'PENDING_APPROVAL',
    page_size: 20,
  });

  // R12 (§4.19.8): the owner sees open projects still working without a PO.
  const { has } = useSession();
  const noPo = useProjectsWithoutPo(has(PERM.PROJECT_VIEW_MARGIN));
  const noPoRows = [...(noPo.data?.results ?? [])].sort(
    (a, b) => (b.days_without_po ?? 0) - (a.days_without_po ?? 0),
  );

  return (
    <>
    <Panel title="Waiting on you" loading={approvals.isLoading}>
      <Tile
        to="/approvals"
        label="Approvals"
        value={approvals.data?.results.length ?? 0}
        tone={(approvals.data?.results.length ?? 0) > 0 ? 'warn' : 'good'}
        hint="Nothing leaves the yard until these are answered."
      />
      <Tile
        to="/custody"
        label="Overdue custody"
        value={overdue.data?.total ?? 0}
        tone={(overdue.data?.total ?? 0) > 0 ? 'bad' : 'good'}
        hint="Material somebody should have brought back."
      />
      <Tile
        to="/exceptions"
        label="Open exceptions"
        value={exceptions.data?.count ?? 0}
        tone={(exceptions.data?.count ?? 0) > 0 ? 'warn' : 'good'}
        hint="Variances, short releases, overdue items."
      />
      {(disposals.data?.results.length ?? 0) > 0 ? (
        <Tile
          to="/disposals"
          label="Write-offs to sign"
          value={disposals.data?.results.length ?? 0}
          tone="warn"
          hint="A disposal needs your authority."
        />
      ) : null}
    </Panel>
    {noPoRows.length > 0 ? (
      <Card className="flex flex-col gap-2">
        <h2 className="text-sm font-semibold text-slate-900">Working without a PO</h2>
        {noPoRows.map((project) => (
          <Link
            key={project.id}
            to={`/projects/${project.id}`}
            className="flex items-center justify-between gap-2 text-sm text-sky-700 underline"
          >
            <span>
              {project.reference}
              {project.title ? ` · ${project.title}` : ''}
            </span>
            <span className="text-xs text-slate-500 no-underline">
              {project.days_without_po ?? 0} days
            </span>
          </Link>
        ))}
      </Card>
    ) : null}
    </>
  );
}

/** "What is going out today, and what am I short of?" (F7, E6, M1) */
function StorekeeperPanel() {
  const releasable = useList<GateOut>('gate-outs', {
    status: 'APPROVED',
    page_size: 50,
  });
  const partial = useList<GateOut>('gate-outs', {
    status: 'PARTIALLY_RELEASED',
    page_size: 50,
  });
  const low = useResource<{ count: number; items: unknown[] }>('stock/low');
  const quarantine = useResource<{ count: number }>('quarantine');

  const toRelease =
    (releasable.data?.results.length ?? 0) + (partial.data?.results.length ?? 0);

  return (
    <Panel title="The yard today" loading={releasable.isLoading}>
      <Tile
        to="/gate-out"
        label="Ready to release"
        value={toRelease}
        tone={toRelease > 0 ? 'warn' : 'neutral'}
        hint="Approved and waiting at the gate."
      />
      <Tile
        to="/stock"
        label="Below minimum"
        value={low.data?.count ?? 0}
        tone={(low.data?.count ?? 0) > 0 ? 'warn' : 'good'}
        hint="Reorder before a job stops."
      />
      <Tile
        to="/quarantine"
        label="In quarantine"
        value={quarantine.data?.count ?? 0}
        hint="Waiting on a decision."
      />
      <Tile
        to="/gate-in/new"
        label="Receive a delivery"
        value="→"
        hint="The commonest thing you do."
      />
    </Panel>
  );
}

/** "What is in the yard, and what is spoken for?" (E8) */
function YardPanel() {
  const yard = useResource<{
    items_in_stock: number;
    deliveries_7d: number;
    earmarks: { site: number; name: string; items: number }[];
    earmark_sites_more: number;
  }>('stock/summary');
  const summary = yard.data;

  if (summary && summary.items_in_stock === 0) {
    return (
      <Panel title="In the yard">
        <div className="col-span-2 rounded-xl border border-slate-200 bg-white p-3 sm:col-span-4">
          <p className="text-sm text-slate-700">Nothing in stock yet</p>
          <Link
            to="/gate-in/new"
            className="flex min-h-[44px] items-center text-sm font-medium text-slate-900 underline"
          >
            Receive a delivery
          </Link>
        </div>
      </Panel>
    );
  }

  return (
    <Panel title="In the yard" loading={yard.isLoading}>
      <Tile
        to="/stock"
        label="Items in stock"
        value={summary?.items_in_stock ?? '–'}
        hint="Kinds of item with something on hand."
      />
      <Tile
        to="/gate-in"
        label="Deliveries this week"
        value={summary?.deliveries_7d ?? '–'}
        hint="Received in the last 7 days."
      />
      {summary && summary.earmarks.length > 0 ? (
        <div className="col-span-2 rounded-xl border border-slate-200 bg-white p-3">
          <p className="text-xs font-medium tracking-wide text-slate-500 uppercase">
            Earmarked for sites
          </p>
          <ul className="m-0 list-none p-0">
            {summary.earmarks.map((row) => (
              <li key={row.site}>
                <Link
                  to={`/stock?earmarked_for=${row.site}`}
                  className="flex min-h-[44px] items-center justify-between gap-3 text-sm text-slate-900"
                >
                  <span className="min-w-0 break-words">{row.name}</span>
                  <span className="shrink-0 tabular-nums text-slate-600">
                    {row.items} {row.items === 1 ? 'item' : 'items'}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
          {summary.earmark_sites_more > 0 ? (
            <p className="text-xs text-slate-500">and {summary.earmark_sites_more} more</p>
          ) : null}
        </div>
      ) : null}
    </Panel>
  );
}

function Panel({
  title,
  loading,
  children,
}: {
  title: string;
  loading?: boolean;
  children: React.ReactNode;
}) {
  return (
    <section className="flex flex-col gap-2">
      <div className="flex items-center gap-2">
        <h2 className="text-sm font-semibold tracking-wide text-slate-500 uppercase">
          {title}
        </h2>
        {loading ? <Spinner className="size-3 text-slate-400" /> : null}
      </div>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">{children}</div>
    </section>
  );
}

/** A figure that goes somewhere. A number with no screen behind it gets ignored. */
function Tile({
  to,
  label,
  value,
  hint,
  tone = 'neutral',
}: {
  to: string;
  label: string;
  value: React.ReactNode;
  hint?: string;
  tone?: 'neutral' | 'good' | 'warn' | 'bad';
}) {
  return (
    <Link to={to} className="block">
      <Stat label={label} value={value} tone={tone} hint={hint} />
    </Link>
  );
}

function greeting(): string {
  const hour = new Date().getHours();
  if (hour < 12) return 'Good morning';
  if (hour < 17) return 'Good afternoon';
  return 'Good evening';
}
