/**
 * T5.11 — custody and reconciliation (design §7.4; H4, I1, I4).
 *
 * The criterion: "an owner sees **who is holding what and what is overdue on one
 * screen**."
 *
 * One screen, deliberately, because these are one question. An owner asking
 * "who has our stuff?" is nearly always asking because something has not come
 * back — putting custody and overdue on separate screens means the answer to the
 * real question is always one navigation away, and the overdue tab is the one
 * nobody opens.
 *
 * So: overdue first, holdings under it, and a holder's row opens what they are
 * carrying. Overdue rows carry the holder's id for exactly that (I4).
 *
 * Reconciliation is a sibling screen rather than a section here: H4's four
 * figures are per *site or work order*, not per person, and the operator
 * conversation it exists for is a different conversation.
 */

import { useState } from 'react';
import { Link } from 'react-router-dom';

import { errorMessage, useList, useResource } from '../../api/hooks';
import { Banner, Button, Card, OwnershipBadge, Select, Spinner } from '../../components/ui';
import {
  DataList,
  EmptyState,
  PageHeader,
  Sheet,
  Stat,
} from '../../components/ui/data';
import type {
  CustodyBalance,
  CustodyExpectation,
  OverdueReport,
  Reconciliation,
} from './types';

export default function CustodyOverviewPage() {
  const overdue = useResource<OverdueReport>('custody/overdue');
  // Everyone's custody: PERSON-node balances across the tenant (§4.10).
  const holdings = useList<CustodyBalance>('stock/custody', { page_size: 200 });
  const [openHolder, setOpenHolder] = useState<{ id: number; name: string } | null>(null);

  const rows = holdings.data?.results ?? [];

  // Grouped by person, because "who is holding what" is a question about people.
  const byHolder = new Map<number, { name: string; rows: CustodyBalance[] }>();
  for (const row of rows) {
    if (!row.holder_id) continue;
    const entry = byHolder.get(row.holder_id) ?? { name: row.node_label, rows: [] };
    entry.rows.push(row);
    byHolder.set(row.holder_id, entry);
  }
  const holders = [...byHolder.entries()].sort((left, right) =>
    left[1].name.localeCompare(right[1].name),
  );

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Custody"
        subtitle="Who is holding what, and what is late. Read straight from the ledger."
        actions={
          <Link
            to="/jobs/reconciliation"
            className="flex min-h-[44px] items-center rounded-lg border border-slate-300 px-4 text-sm font-medium text-slate-800"
          >
            Reconciliation
          </Link>
        }
      />

      {overdue.isError ? <Banner tone="error">{errorMessage(overdue.error)}</Banner> : null}

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
        <Stat
          label="Overdue lines"
          value={overdue.data?.total ?? '—'}
          tone={(overdue.data?.total ?? 0) > 0 ? 'bad' : 'good'}
          hint="Past the date it was due back."
        />
        <Stat label="People holding stock" value={holders.length} />
        <Stat label="Custody lines" value={rows.length} />
      </div>

      {/* I4: "who keeps doing this" and "what do we keep losing" — both, because
          they lead to different conversations. */}
      {(overdue.data?.by_person.length ?? 0) > 0 ? (
        <Card className="flex flex-col gap-3">
          <h2 className="text-sm font-semibold text-slate-900">Overdue by person</h2>
          <DataList
            rows={overdue.data?.by_person ?? []}
            rowKey={(row) => row.holder_id ?? row.holder ?? ''}
            onRowClick={(row) =>
              row.holder_id
                ? setOpenHolder({ id: row.holder_id, name: row.holder ?? '' })
                : undefined
            }
            columns={[
              { header: 'Person', cell: (row) => row.holder ?? '' },
              { header: 'Lines', cell: (row) => row.items ?? '' },
              { header: 'Quantity', cell: (row) => row.quantity },
              {
                header: 'Worst',
                cell: (row) =>
                  row.days_overdue ? `${row.days_overdue} days late` : '',
              },
            ]}
          />
        </Card>
      ) : null}

      {(overdue.data?.by_item.length ?? 0) > 0 ? (
        <Card className="flex flex-col gap-3">
          <h2 className="text-sm font-semibold text-slate-900">Overdue by item</h2>
          <DataList
            rows={overdue.data?.by_item ?? []}
            rowKey={(row) => row.item_type_id ?? row.item ?? ''}
            columns={[
              { header: 'Item', cell: (row) => row.item ?? '' },
              { header: 'Holders', cell: (row) => row.holders ?? '' },
              { header: 'Quantity', cell: (row) => `${row.quantity} ${row.uom ?? ''}` },
            ]}
          />
        </Card>
      ) : null}

      <Card className="flex flex-col gap-3">
        <h2 className="text-sm font-semibold text-slate-900">Who is holding what</h2>
        {holdings.isLoading ? (
          <Spinner className="text-slate-400" />
        ) : holders.length === 0 ? (
          <EmptyState
            title="Nobody is holding anything."
            hint="Everything is in the yard or installed."
          />
        ) : (
          <ul className="flex flex-col gap-2">
            {holders.map(([holderId, entry]) => (
              <li key={holderId}>
                <button
                  type="button"
                  onClick={() => setOpenHolder({ id: holderId, name: entry.name })}
                  className="flex w-full items-center justify-between gap-3 rounded-lg border border-slate-200 p-3 text-left active:bg-slate-50"
                >
                  <span className="min-w-0">
                    <span className="block truncate text-sm font-medium text-slate-900">
                      {entry.name}
                    </span>
                    <span className="block text-xs text-slate-500">
                      {entry.rows.length} line{entry.rows.length === 1 ? '' : 's'}
                    </span>
                  </span>
                  <span className="text-sm text-slate-500">View</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <HolderSheet
        holder={openHolder}
        rows={openHolder ? (byHolder.get(openHolder.id)?.rows ?? []) : []}
        onClose={() => setOpenHolder(null)}
      />
    </div>
  );
}

/** One person's holdings and what of it is late (I1, I3). */
function HolderSheet({
  holder,
  rows,
  onClose,
}: {
  holder: { id: number; name: string } | null;
  rows: CustodyBalance[];
  onClose: () => void;
}) {
  const expectations = useList<CustodyExpectation>(
    'custody-expectations',
    { holder: holder?.id, page_size: 100 },
    { enabled: Boolean(holder) },
  );

  const open = (expectations.data?.results ?? []).filter((row) => row.is_open);

  return (
    <Sheet open={Boolean(holder)} title={holder?.name ?? ''} onClose={onClose}>
      <div className="flex flex-col gap-4">
        <div className="flex flex-col gap-2">
          <h3 className="text-sm font-semibold text-slate-900">Holding now</h3>
          {rows.length === 0 ? (
            <p className="text-sm text-slate-500">Nothing.</p>
          ) : (
            <ul className="flex flex-col gap-1">
              {rows.map((row) => (
                <li key={row.id} className="flex items-center justify-between gap-3 text-sm">
                  <span className="min-w-0 truncate text-slate-700">{row.item_name}</span>
                  <span className="flex shrink-0 items-center gap-2">
                    <OwnershipBadge client={row.owner_client_name || null} />
                    <span className="font-medium text-slate-900">
                      {row.quantity} {row.uom}
                    </span>
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="flex flex-col gap-2">
          <h3 className="text-sm font-semibold text-slate-900">Due back</h3>
          {open.length === 0 ? (
            <p className="text-sm text-slate-500">Nothing outstanding.</p>
          ) : (
            <ul className="flex flex-col gap-1 text-sm">
              {open.map((row) => (
                <li key={row.id} className="flex items-center justify-between gap-3">
                  <span className="min-w-0 truncate text-slate-700">
                    {row.item_name}
                    {row.expected_return_date ? ` · due ${row.expected_return_date}` : ''}
                  </span>
                  <span
                    className={
                      row.status === 'OVERDUE'
                        ? 'shrink-0 font-medium text-red-700'
                        : 'shrink-0 font-medium text-slate-900'
                    }
                  >
                    {row.outstanding_quantity}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </Sheet>
  );
}

/**
 * H4 — issued versus installed versus consumed versus returned, per site or work
 * order (T5.11, T5.7).
 *
 * The screen an operator's question is answered from, so it shows the five
 * figures per item and says plainly when they do not add up. The fifth is
 * derived from the other four (§10), which is why the reader never has to wonder
 * which number is wrong.
 */
export function ReconciliationPage() {
  const [scope, setScope] = useState<'site' | 'work_order'>('site');
  const [target, setTarget] = useState('');

  const sites = useList<{ id: number; name: string; internal_ref: string }>('sites', {
    page_size: 200,
  });
  const workOrders = useList<{ id: number; reference: string; title: string }>(
    'work-orders',
    { page_size: 200 },
  );

  const result = useResource<Reconciliation>(
    'reconciliation',
    scope === 'site' ? { site: target } : { work_order: target },
    { enabled: Boolean(target) },
  );

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Reconciliation"
        subtitle="Issued, installed, consumed, returned — and what that leaves unexplained."
        actions={
          <Link
            to="/jobs/custody"
            className="flex min-h-[44px] items-center px-2 text-sm text-slate-700"
          >
            Custody
          </Link>
        }
      />

      <Card className="flex flex-col gap-3 sm:flex-row sm:items-end">
        <div className="flex-1">
          <label htmlFor="scope" className="text-sm font-medium text-slate-700">
            Reconcile a
          </label>
          <Select
            id="scope"
            value={scope}
            onChange={(event) => {
              setScope(event.target.value as 'site' | 'work_order');
              setTarget('');
            }}
          >
            <option value="site">Site</option>
            <option value="work_order">Work order</option>
          </Select>
        </div>
        <div className="flex-1">
          <label htmlFor="target" className="text-sm font-medium text-slate-700">
            Which one
          </label>
          <Select
            id="target"
            value={target}
            onChange={(event) => setTarget(event.target.value)}
          >
            <option value="">Choose…</option>
            {scope === 'site'
              ? (sites.data?.results ?? []).map((site) => (
                  <option key={site.id} value={site.id}>
                    {site.internal_ref ? `${site.internal_ref} — ` : ''}
                    {site.name}
                  </option>
                ))
              : (workOrders.data?.results ?? []).map((order) => (
                  <option key={order.id} value={order.id}>
                    {order.reference} {order.title}
                  </option>
                ))}
          </Select>
        </div>
      </Card>

      {result.isError ? <Banner tone="error">{errorMessage(result.error)}</Banner> : null}

      {!target ? (
        <EmptyState
          title="Pick a site or a work order."
          hint="This is the screen the operator's question gets answered from."
        />
      ) : result.isLoading ? (
        <Spinner className="text-slate-400" />
      ) : result.data ? (
        <>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
            <Stat label="Issued" value={result.data.totals.issued} />
            <Stat label="Installed" value={result.data.totals.installed} tone="good" />
            <Stat label="Consumed" value={result.data.totals.consumed} />
            <Stat label="Returned" value={result.data.totals.returned} />
            <Stat
              label="Unaccounted"
              value={result.data.totals.unaccounted}
              tone={result.data.is_reconciled ? 'good' : 'bad'}
            />
          </div>

          {result.data.is_reconciled ? (
            <Banner tone="success">
              Everything issued for {result.data.label} is accounted for.
            </Banner>
          ) : (
            <Banner tone="warning">
              {result.data.label} has material nobody has explained. Each row below
              shows where the gap is — H5 blocks closing a job while it stands.
            </Banner>
          )}

          <Card className="flex flex-col gap-3">
            <DataList
              rows={result.data.items}
              rowKey={(row) => row.item_type_id}
              columns={[
                { header: 'Item', cell: (row) => row.item_type },
                { header: 'Issued', cell: (row) => `${row.issued} ${row.uom}` },
                { header: 'Installed', cell: (row) => row.installed, wideOnly: true },
                { header: 'Consumed', cell: (row) => row.consumed, wideOnly: true },
                { header: 'Returned', cell: (row) => row.returned, wideOnly: true },
                {
                  header: 'Unaccounted',
                  cell: (row) => (
                    <span
                      className={
                        row.is_reconciled
                          ? 'font-medium text-emerald-700'
                          : 'font-semibold text-red-700'
                      }
                    >
                      {row.unaccounted}
                    </span>
                  ),
                },
              ]}
            />
          </Card>

          <Button variant="ghost" onClick={() => void result.refetch()}>
            Refresh
          </Button>
        </>
      ) : null}
    </div>
  );
}
