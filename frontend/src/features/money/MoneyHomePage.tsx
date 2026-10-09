/**
 * Money home (Epic R, R1–R3; design §4.17.10).
 *
 * Three lists in tabs: my expenses, my requests and floats, and the casual
 * register. A rejected entry shows its reason and a Resubmit action, because a
 * rejection returns the entry to whoever recorded it (R4). Cards take an
 * optional `queued` flag so entries still on the phone read "Waiting to send"
 * (R6, T15.10); a refused one shows the server's reason and "Fix and resend",
 * which reopens the form from what was captured (§4.17.8).
 */

import { useState, type ReactNode } from 'react';
import { Link, useLocation, useSearchParams } from 'react-router-dom';

import { errorMessage } from '../../api/hooks';
import { PERM } from '../../auth/permissions';
import { usePermission } from '../../auth/session';
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
import { useResubmitSitePurchase, useSitePurchases } from './purchasesApi';
import { TYPE_LABELS } from './RequestAllowancePage';
import { useQueuedMoney, type QueuedCard } from './queued';
import { statusLabel } from './rules';
import type { ExpenseStatus } from './types';

type Tab = 'expenses' | 'purchases' | 'requests' | 'casuals';

const TABS: readonly TabItem<Tab>[] = [
  { key: 'expenses', label: 'My expenses' },
  { key: 'purchases', label: 'Purchases' },
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

/** An entry still on this phone: waiting to send, or refused with a way to fix it (R6). */
export function QueuedEntryCard({ card }: { card: QueuedCard }) {
  return (
    <EntryCard
      title={card.title}
      meta={card.meta}
      amount={card.amount}
      // A refusal reads as one; a waiting entry has no server status yet.
      status={card.refused ? 'REJECTED' : 'PENDING_PM'}
      queued={!card.refused}
      reason={card.reason}
    >
      {card.refused ? (
        <Link
          to={card.fixTo}
          className="mt-2 inline-flex min-h-[44px] items-center text-sm font-medium underline"
        >
          Fix and resend
        </Link>
      ) : null}
    </EntryCard>
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
  const rows = query.data?.results ?? [];

  return (
    <div className="flex flex-col gap-2">
      {queued.length > 0 ? (
        <ul className="flex flex-col gap-2">
          {queued.map((card) => (
            <QueuedEntryCard key={`q${card.client_uuid}`} card={card} />
          ))}
        </ul>
      ) : null}
      <ListState query={query}>
        {rows.length === 0 && queued.length === 0 ? (
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
                key={row.id}
                to={`/money/expenses/${row.id}`}
                title={`${row.category_name ?? 'Expense'} · ${row.project_reference ?? ''}`.trim()}
                meta={[row.incurred_on, row.site_name].filter(Boolean).join(' · ')}
                amount={row.amount}
                status={row.status}
                reason={row.decision_reason}
              >
                {row.status === 'REJECTED' ? <ResubmitButton id={row.id} kind="expense" /> : null}
              </EntryCard>
            ))}
          </ul>
        )}
      </ListState>
    </div>
  );
}

/** R7: my site purchases (T18.16). */
function PurchasesTab() {
  const query = useSitePurchases({ mine: true, page_size: 50 });
  const resubmit = useResubmitSitePurchase();
  const rows = query.data?.results ?? [];
  return (
    <ListState query={query}>
      {rows.length === 0 ? (
        <EmptyState
          title="No purchases yet."
          hint="Record one when you buy goods for a site."
          action={
            <Link to="/money/purchases/new" className="text-sm font-medium underline">
              Record a purchase
            </Link>
          }
        />
      ) : (
        <ul className="flex flex-col gap-2">
          {rows.map((row) => (
            <EntryCard
              key={row.id}
              to={`/money/purchases/${row.id}`}
              title={`${row.number} · ${row.supplier_name ?? 'Supplier'}`}
              meta={[
                row.purchase_date,
                row.site_name,
                row.destination === 'INTO_YARD' ? 'into the yard' : 'used at site',
              ]
                .filter(Boolean)
                .join(' · ')}
              amount={row.amount}
              status={row.status}
              reason={row.decision_reason}
            >
              {row.status === 'REJECTED' ? (
                <div className="mt-2">
                  <Button
                    variant="secondary"
                    loading={resubmit.isPending}
                    onClick={() => resubmit.mutate({ id: row.id })}
                  >
                    Resubmit
                  </Button>
                </div>
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
  const rows = query.data?.results ?? [];

  return (
    <div className="flex flex-col gap-2">
      {queued.length > 0 ? (
        <ul className="flex flex-col gap-2">
          {queued.map((card) => (
            <QueuedEntryCard key={`q${card.client_uuid}`} card={card} />
          ))}
        </ul>
      ) : null}
      <ListState query={query}>
        {rows.length === 0 && queued.length === 0 ? (
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
                  key={row.id}
                  to={`/money/requests/${row.id}`}
                  title={`${TYPE_LABELS[row.type]} ${row.number ?? ''}`.trim()}
                  meta={`${row.from_date} to ${row.to_date}`}
                  amount={row.amount}
                  status={row.status}
                  reason={row.decision_reason}
                  extra={
                    isFloat && row.status === 'PAID' && !row.closed_at && row.balance !== undefined ? (
                      <p className="mt-2 text-sm text-slate-700">
                        Balance <Money value={row.balance} />
                      </p>
                    ) : null
                  }
                >
                  {row.status === 'REJECTED' ? <ResubmitButton id={row.id} kind="request" /> : null}
                </EntryCard>
              );
            })}
          </ul>
        )}
      </ListState>
    </div>
  );
}

function CasualsTab() {
  const [search, setSearch] = useState('');
  const query = useCasuals(search);
  const queued = useQueuedMoney().casuals;
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
      {queued.length > 0 ? (
        <ul className="flex flex-col gap-2">
          {queued.map((card) => (
            <li key={`q${card.client_uuid}`} className="rounded-xl border border-slate-200 bg-white p-3">
              <p className="text-sm font-medium text-slate-900">{card.title}</p>
              <p className="text-sm text-slate-500">{card.meta}</p>
              {card.refused ? (
                <>
                  <p className="mt-2 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-900">
                    Refused: {card.reason}
                  </p>
                  <Link
                    to={card.fixTo}
                    className="mt-2 inline-flex min-h-[44px] items-center text-sm font-medium underline"
                  >
                    Fix and resend
                  </Link>
                </>
              ) : (
                <p className="mt-1 text-xs font-medium text-slate-700">Waiting to send</p>
              )}
            </li>
          ))}
        </ul>
      ) : null}
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
  const canApprove = usePermission(PERM.FINANCE_APPROVE);
  // Set by a form that saved to the phone (R6), so the person is told it is safe.
  const notice = (useLocation().state as { notice?: string } | null)?.notice;

  return (
    <div className="flex flex-col gap-4">
      <PageHeader title="Money" subtitle="What you have spent, asked for and been given." />

      {notice ? <Banner tone="success">{notice}</Banner> : null}

      <div className="flex flex-wrap gap-2">
        <Link
          to="/money/expenses/new"
          className="inline-flex min-h-[44px] items-center rounded-lg bg-slate-900 px-4 text-base font-medium text-white"
        >
          Record expense
        </Link>
        <Link
          to="/money/purchases/new"
          className="inline-flex min-h-[44px] items-center rounded-lg border border-slate-300 bg-white px-4 text-base font-medium text-slate-900"
        >
          Record purchase
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
        {canApprove ? (
          <Link
            to="/money/to-pay"
            className="inline-flex min-h-[44px] items-center rounded-lg border border-slate-300 bg-white px-4 text-base font-medium text-slate-900"
          >
            To pay
          </Link>
        ) : null}
      </div>

      <TabStrip<Tab>
        tabs={TABS}
        current={tab}
        onSelect={(key) => setParams({ tab: key }, { replace: true })}
        aria-label="Money sections"
      />

      {tab === 'expenses' ? (
        <ExpensesTab />
      ) : tab === 'purchases' ? (
        <PurchasesTab />
      ) : tab === 'requests' ? (
        <RequestsTab />
      ) : (
        <CasualsTab />
      )}
    </div>
  );
}
