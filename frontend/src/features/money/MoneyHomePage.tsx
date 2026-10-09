/**
 * Money home (Epic R, R1–R3; design §4.17.10).
 *
 * Three lists in tabs: my expenses, my requests and floats, and the casual
 * register. A rejected entry shows its reason and a Resubmit action, because a
 * rejection returns the entry to whoever recorded it (R4). Cards take an
 * optional `queued` flag so entries still on the phone read "Waiting to send"
 * (R6, T15.10) — `useQueuedMoney` is where those will come from.
 */

import { useState, type ReactNode } from 'react';
import { Link, useSearchParams } from 'react-router-dom';

import { errorMessage } from '../../api/hooks';
import { Banner, Button } from '../../components/ui';
import { EmptyState, ListState, PageHeader, StatusBadge } from '../../components/ui/data';
import { TabStrip, type TabItem } from '../../components/ui/TabStrip';
import { Money } from '../../components/ui/money';
import {
  useAllowanceRequests,
  useCasuals,
  useExpenses,
  useResubmitAllowance,
  useResubmitExpense,
} from './api';
import { TYPE_LABELS } from './RequestAllowancePage';
import { useQueuedMoney, type MaybeQueued } from './queued';
import { statusLabel } from './rules';
import type { AllowanceRequest, ExpenseStatus, ProjectExpense } from './types';

type Tab = 'expenses' | 'requests' | 'casuals';

const TABS: readonly TabItem<Tab>[] = [
  { key: 'expenses', label: 'My expenses' },
  { key: 'requests', label: 'Requests and floats' },
  { key: 'casuals', label: 'Casuals' },
];

export function StatusPill({ status, queued }: { status: ExpenseStatus; queued?: boolean }) {
  if (queued) {
    return (
      <span className="inline-block rounded-full bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-700">
        Waiting to send
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1">
      <StatusBadge status={status} />
      <span className="sr-only">{statusLabel(status)}</span>
    </span>
  );
}

/** One entry. A link to its detail unless it is still queued (there is nothing to open yet). */
export function EntryCard({
  to,
  title,
  meta,
  amount,
  status,
  queued,
  reason,
  extra,
  children,
}: {
  to?: string;
  title: string;
  meta?: string;
  amount: string;
  status: ExpenseStatus;
  queued?: boolean;
  /** A rejection's reason, shown on the card. */
  reason?: string;
  extra?: ReactNode;
  /** Actions, outside the link so a button is not nested in an anchor. */
  children?: ReactNode;
}) {
  const body = (
    <div className="flex items-start justify-between gap-3">
      <div className="min-w-0">
        <p className="truncate text-sm font-medium text-slate-900">{title}</p>
        {meta ? <p className="truncate text-sm text-slate-500">{meta}</p> : null}
      </div>
      <div className="flex shrink-0 flex-col items-end gap-1">
        <Money value={amount} />
        <StatusPill status={status} queued={queued} />
      </div>
    </div>
  );

  return (
    <li className="rounded-xl border border-slate-200 bg-white p-3">
      {to && !queued ? (
        <Link to={to} className="block min-h-[44px]">
          {body}
        </Link>
      ) : (
        body
      )}
      {extra}
      {status === 'REJECTED' && reason ? (
        <p className="mt-2 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-900">
          Rejected: {reason}
        </p>
      ) : null}
      {children}
    </li>
  );
}

export function ResubmitButton({
  id,
  kind,
}: {
  id: number;
  kind: 'expense' | 'request';
}) {
  const expense = useResubmitExpense();
  const request = useResubmitAllowance();
  const action = kind === 'expense' ? expense : request;
  return (
    <div className="mt-2 flex flex-col gap-2">
      {action.isError ? <Banner tone="error">{errorMessage(action.error)}</Banner> : null}
      <Button
        variant="secondary"
        loading={action.isPending}
        onClick={() => action.mutate({ id })}
      >
        Resubmit
      </Button>
    </div>
  );
}

function ExpensesTab() {
  const query = useExpenses({ mine: true, page_size: 50 });
  const queued = useQueuedMoney().expenses;
  const rows: MaybeQueued<ProjectExpense>[] = [...queued, ...(query.data?.results ?? [])];

  return (
    <ListState query={query}>
      {rows.length === 0 ? (
        <EmptyState
          title="No expenses yet."
          hint="Record one when you pay for something on a project."
          action={
            <Link to="/money/expenses/new" className="text-sm font-medium underline">
              Record an expense
            </Link>
          }
        />
      ) : (
        <ul className="flex flex-col gap-2">
          {rows.map((row) => (
            <EntryCard
              key={row.queued ? `q${row.client_uuid}` : row.id}
              to={`/money/expenses/${row.id}`}
              title={`${row.category_name ?? 'Expense'} · ${row.project_reference ?? ''}`.trim()}
              meta={[row.incurred_on, row.site_name].filter(Boolean).join(' · ')}
              amount={row.amount}
              status={row.status}
              queued={row.queued}
              reason={row.decision_reason}
            >
              {row.status === 'REJECTED' && !row.queued ? (
                <ResubmitButton id={row.id} kind="expense" />
              ) : null}
            </EntryCard>
          ))}
        </ul>
      )}
    </ListState>
  );
}

function RequestsTab() {
  const query = useAllowanceRequests({ mine: true, page_size: 50 });
  const queued = useQueuedMoney().requests;
  const rows: MaybeQueued<AllowanceRequest>[] = [...queued, ...(query.data?.results ?? [])];

  return (
    <ListState query={query}>
      {rows.length === 0 ? (
        <EmptyState
          title="No requests yet."
          hint="Ask for a float or an allowance before you spend."
          action={
            <Link to="/money/requests/new" className="text-sm font-medium underline">
              Request an allowance
            </Link>
          }
        />
      ) : (
        <ul className="flex flex-col gap-2">
          {rows.map((row) => {
            const isFloat = row.type === 'FLOAT';
            return (
              <EntryCard
                key={row.queued ? `q${row.client_uuid}` : row.id}
                to={`/money/requests/${row.id}`}
                title={`${TYPE_LABELS[row.type]} ${row.number ?? ''}`.trim()}
                meta={`${row.from_date} to ${row.to_date}`}
                amount={row.amount}
                status={row.status}
                queued={row.queued}
                reason={row.decision_reason}
                extra={
                  isFloat && row.status === 'PAID' && !row.closed_at && row.balance !== undefined ? (
                    <p className="mt-2 text-sm text-slate-700">
                      Balance <Money value={row.balance} />
                    </p>
                  ) : null
                }
              >
                {row.status === 'REJECTED' && !row.queued ? (
                  <ResubmitButton id={row.id} kind="request" />
                ) : null}
              </EntryCard>
            );
          })}
        </ul>
      )}
    </ListState>
  );
}

function CasualsTab() {
  const [search, setSearch] = useState('');
  const query = useCasuals(search);
  return (
    <div className="flex flex-col gap-3">
      <input
        type="search"
        aria-label="Search casuals"
        placeholder="Search by name or ID"
        value={search}
        onChange={(event) => setSearch(event.target.value)}
        className="min-h-[44px] rounded-lg border border-slate-300 px-3 text-base"
      />
      <ListState query={query}>
        {(query.data?.results ?? []).length === 0 ? (
          <EmptyState title="No casuals found." hint="Register someone once, then pick them on any expense." />
        ) : (
          <ul className="flex flex-col gap-2">
            {(query.data?.results ?? []).map((casual) => (
              <li key={casual.id} className="rounded-xl border border-slate-200 bg-white p-3">
                <p className="text-sm font-medium text-slate-900">{casual.name}</p>
                <p className="text-sm text-slate-500">
                  {casual.id_number}
                  {casual.phone ? ` · ${casual.phone}` : ''}
                </p>
              </li>
            ))}
          </ul>
        )}
      </ListState>
    </div>
  );
}

export default function MoneyHomePage() {
  const [params, setParams] = useSearchParams();
  const requested = params.get('tab');
  const tab: Tab = TABS.some((t) => t.key === requested) ? (requested as Tab) : 'expenses';

  return (
    <div className="flex flex-col gap-4">
      <PageHeader title="Money" subtitle="What you have spent, asked for and been given." />

      <div className="flex flex-wrap gap-2">
        <Link
          to="/money/expenses/new"
          className="inline-flex min-h-[44px] items-center rounded-lg bg-slate-900 px-4 text-base font-medium text-white"
        >
          Record expense
        </Link>
        <Link
          to="/money/requests/new"
          className="inline-flex min-h-[44px] items-center rounded-lg border border-slate-300 bg-white px-4 text-base font-medium text-slate-900"
        >
          Request allowance
        </Link>
        <Link
          to="/money/casuals/new"
          className="inline-flex min-h-[44px] items-center rounded-lg border border-slate-300 bg-white px-4 text-base font-medium text-slate-900"
        >
          Add casual
        </Link>
      </div>

      <TabStrip<Tab>
        tabs={TABS}
        current={tab}
        onSelect={(key) => setParams({ tab: key }, { replace: true })}
        aria-label="Money sections"
      />

      {tab === 'expenses' ? <ExpensesTab /> : tab === 'requests' ? <RequestsTab /> : <CasualsTab />}
    </div>
  );
}
