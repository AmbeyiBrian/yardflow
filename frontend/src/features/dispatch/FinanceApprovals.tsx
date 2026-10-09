/**
 * Finance approvals: the Expenses and Requests tabs (Epic R; R2, R4;
 * design §4.17.3, §4.17.6, §4.17.10).
 *
 * Both lists come from `/pending`, which the server makes level-aware: a PM
 * sees what waits on them, Finance what waits on Finance. The screen only
 * names the level and shows what the approver needs to decide: who, how much,
 * where, evidence, and the open-float warning (R2). The recorder is never
 * offered Approve on their own entry (R4); the server refuses it as well.
 */

import { type ReactNode, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';

import { api } from '../../api/client';
import { errorMessage } from '../../api/hooks';
import { PERM } from '../../auth/permissions';
import { useSession } from '../../auth/session';
import { Banner, Button, Card, Field, Spinner, Textarea } from '../../components/ui';
import { DataList, EmptyState, ListState, Sheet } from '../../components/ui/data';
import { Money } from '../../components/ui/money';
import type { Attachment } from '../../components/PhotoCapture';
import {
  useDecideAllowance,
  useDecideExpense,
  usePendingAllowances,
  usePendingExpenses,
} from '../money/api';
import { statusLabel } from '../money/rules';
import type { AllowanceRequest, EvidenceState, ProjectExpense } from '../money/types';

const EVIDENCE_TEXT: Record<EvidenceState, string> = {
  ok: 'Photos attached',
  arriving: 'Photos on the way',
  none: 'No evidence',
};

const EVIDENCE_TONE: Record<EvidenceState, string> = {
  ok: 'bg-emerald-100 text-emerald-900',
  arriving: 'bg-amber-100 text-amber-900',
  none: 'bg-red-100 text-red-900',
};

export function EvidenceNote({ state }: { state: EvidenceState }) {
  const known = state in EVIDENCE_TEXT ? state : 'none';
  return (
    <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${EVIDENCE_TONE[known]}`}>
      {EVIDENCE_TEXT[known]}
    </span>
  );
}

function Row({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex justify-between gap-3">
      <dt className="text-slate-500">{label}</dt>
      <dd className="text-right font-medium text-slate-900">{value}</dd>
    </div>
  );
}

/** Read-only thumbnails of what is attached (signed, expiring URLs, N-7). */
function AttachedPhotos({ targetType, targetId }: { targetType: string; targetId: number }) {
  const [items, setItems] = useState<Attachment[]>([]);
  useEffect(() => {
    let cancelled = false;
    const query = new URLSearchParams({
      target_type: targetType,
      target_id: String(targetId),
      page_size: '100',
    });
    api
      .get<Attachment[] | { results: Attachment[] }>(`/attachments?${query}`)
      .then((body) => {
        if (!cancelled) setItems(Array.isArray(body) ? body : (body.results ?? []));
      })
      .catch(() => {
        if (!cancelled) setItems([]);
      });
    return () => {
      cancelled = true;
    };
  }, [targetType, targetId]);

  if (items.length === 0) return null;
  return (
    <ul className="grid grid-cols-3 gap-2">
      {items.map((item) => (
        <li key={item.id}>
          {item.content_type.startsWith('image/') ? (
            <a href={item.download_url} target="_blank" rel="noreferrer">
              <img
                src={item.download_url}
                alt={item.filename}
                className="aspect-square w-full rounded-lg object-cover"
              />
            </a>
          ) : (
            <a
              href={item.download_url}
              target="_blank"
              rel="noreferrer"
              className="flex aspect-square w-full items-center justify-center rounded-lg border border-slate-200 bg-slate-50 p-1 text-center text-xs break-all text-slate-600"
            >
              {item.filename}
            </a>
          )}
        </li>
      ))}
    </ul>
  );
}

/** Finance-only shortcut to the payment queue, from Approvals. */
function ToPayLink() {
  const { has } = useSession();
  if (!has(PERM.FINANCE_APPROVE)) return null;
  return (
    <Link
      to="/money/to-pay"
      className="flex min-h-[44px] items-center self-start rounded-lg border border-slate-300 px-4 text-sm font-medium text-slate-800"
    >
      To pay
    </Link>
  );
}

/** A PM's or Director's own entry skips the PM level; say so (R4). */
function wentStraightToFinance(item: { status: string; decided_by: number | null }) {
  return item.status === 'PENDING_FINANCE' && item.decided_by === null;
}

const TYPE_LABELS: Record<AllowanceRequest['type'], string> = {
  FLOAT: 'Float',
  TRANSPORT: 'Transport',
  NIGHT_OUT: 'Night-out',
  TEAM_ALLOWANCE: 'Team allowance',
  OTHER: 'Other',
};

export function typeLabel(request: Pick<AllowanceRequest, 'type' | 'transport_scope'>): string {
  const base = TYPE_LABELS[request.type] ?? request.type;
  if (request.type !== 'TRANSPORT' || !request.transport_scope) return base;
  return `${base} ${request.transport_scope === 'WITHIN_NAIROBI' ? 'within' : 'outside'} Nairobi`;
}

interface DecideProps {
  title: string;
  open: boolean;
  isOwn: boolean;
  busy: boolean;
  error: string;
  onClose: () => void;
  onDecide: (approved: boolean, reason: string) => void;
  children: ReactNode;
}

function DecideSheet({ title, open, isOwn, busy, error, onClose, onDecide, children }: DecideProps) {
  const [reason, setReason] = useState('');
  const [needReason, setNeedReason] = useState(false);

  function reject() {
    if (!reason.trim()) {
      setNeedReason(true);
      return;
    }
    onDecide(false, reason.trim());
  }

  return (
    <Sheet
      open={open}
      title={title}
      onClose={onClose}
      footer={
        isOwn ? undefined : (
          <>
            <Button variant="danger" className="flex-1" disabled={busy} onClick={reject}>
              Reject
            </Button>
            <Button className="flex-1" disabled={busy} onClick={() => onDecide(true, '')}>
              {busy ? <Spinner /> : 'Approve'}
            </Button>
          </>
        )
      }
    >
      <div className="flex flex-col gap-3">
        {children}
        {isOwn ? (
          <Banner tone="info">You recorded this, so somebody else has to approve it.</Banner>
        ) : (
          <Field
            label="Reason"
            htmlFor="fin-reason"
            hint="Required to reject. The person who recorded it sees it."
            error={needReason && !reason.trim() ? 'Say why you are rejecting it.' : undefined}
          >
            <Textarea
              id="fin-reason"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
          </Field>
        )}
        {error ? <Banner tone="error">{error}</Banner> : null}
      </div>
    </Sheet>
  );
}

export function FinanceExpenseQueue() {
  const { user } = useSession();
  const [deciding, setDeciding] = useState<ProjectExpense | null>(null);
  const [error, setError] = useState('');
  const pending = usePendingExpenses({ page_size: 100 });
  const decide = useDecideExpense();

  async function run(approved: boolean, reason: string) {
    if (!deciding) return;
    setError('');
    try {
      await decide.mutateAsync({ id: deciding.id, approved, reason });
      setDeciding(null);
    } catch (caught) {
      setError(errorMessage(caught));
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <ToPayLink />
      <ListState query={pending}>
        <DataList<ProjectExpense>
          rows={pending.data?.results ?? []}
          rowKey={(expense) => expense.id}
          onRowClick={(expense) => {
            setError('');
            setDeciding(expense);
          }}
          empty={<EmptyState title="Nothing waiting." hint="Expenses appear here for approval." />}
          columns={[
            {
              header: 'What',
              cell: (expense) => (
                <span className="flex flex-col">
                  <span>{expense.category_name ?? ''}</span>
                  <span className="text-xs text-slate-500">{statusLabel(expense.status)}</span>
                </span>
              ),
            },
            { header: 'Amount', cell: (expense) => <Money value={expense.amount} /> },
            { header: 'Who', cell: (expense) => expense.recorded_by_name ?? '' },
            {
              header: 'Evidence',
              cell: (expense) => <EvidenceNote state={expense.evidence_state} />,
              wideOnly: true,
            },
          ]}
        />
      </ListState>

      <DecideSheet
        // A fresh sheet per entry, so one rejection reason never carries to the next.
        key={deciding?.id ?? 'none'}
        title="Expense"
        open={deciding !== null}
        isOwn={deciding?.recorded_by === user?.id}
        busy={decide.isPending}
        error={error}
        onClose={() => setDeciding(null)}
        onDecide={run}
      >
        {deciding ? (
          <>
            <Card>
              <dl className="flex flex-col gap-1 text-sm">
                <Row label="Waiting for" value={statusLabel(deciding.status)} />
                <Row label="Recorded by" value={deciding.recorded_by_name ?? ''} />
                <Row label="Amount" value={<Money value={deciding.amount} />} />
                <Row label="Category" value={deciding.category_name ?? ''} />
                <Row label="Site" value={deciding.site_name ?? ''} />
                <Row label="Project" value={deciding.project_reference ?? ''} />
                <Row label="Incurred" value={deciding.incurred_on} />
                {deciding.vehicle_reg ? <Row label="Vehicle" value={deciding.vehicle_reg} /> : null}
                {deciding.litres ? <Row label="Litres" value={deciding.litres} /> : null}
              </dl>
              {deciding.scope_of_work ? (
                <p className="mt-2 text-sm text-slate-600">{deciding.scope_of_work}</p>
              ) : null}
              {deciding.description ? (
                <p className="mt-1 text-sm text-slate-600">{deciding.description}</p>
              ) : null}
            </Card>
            {wentStraightToFinance(deciding) ? (
              <Banner tone="info">Went straight to Finance.</Banner>
            ) : null}
            <div>
              <EvidenceNote state={deciding.evidence_state} />
            </div>
            <AttachedPhotos targetType="commercials.ProjectExpense" targetId={deciding.id} />
          </>
        ) : null}
      </DecideSheet>
    </div>
  );
}

export function FinanceRequestQueue() {
  const { user } = useSession();
  const [deciding, setDeciding] = useState<AllowanceRequest | null>(null);
  const [error, setError] = useState('');
  const pending = usePendingAllowances({ page_size: 100 });
  const decide = useDecideAllowance();

  async function run(approved: boolean, reason: string) {
    if (!deciding) return;
    setError('');
    try {
      await decide.mutateAsync({ id: deciding.id, approved, reason });
      setDeciding(null);
    } catch (caught) {
      setError(errorMessage(caught));
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <ToPayLink />
      <ListState query={pending}>
        <DataList<AllowanceRequest>
          rows={pending.data?.results ?? []}
          rowKey={(request) => request.id}
          onRowClick={(request) => {
            setError('');
            setDeciding(request);
          }}
          empty={<EmptyState title="Nothing waiting." hint="Requests appear here for approval." />}
          columns={[
            {
              header: 'Request',
              cell: (request) => (
                <span className="flex flex-col">
                  <span>
                    {request.number} · {typeLabel(request)}
                  </span>
                  <span className="text-xs text-slate-500">{statusLabel(request.status)}</span>
                </span>
              ),
            },
            { header: 'Amount', cell: (request) => <Money value={request.amount} /> },
            { header: 'Who', cell: (request) => request.recorded_by_name ?? '' },
            {
              header: 'Days',
              cell: (request) => <span className="tabular-nums">{request.days}</span>,
              wideOnly: true,
            },
          ]}
        />
      </ListState>

      <DecideSheet
        key={deciding?.id ?? 'none'}
        title={deciding ? `Request ${deciding.number}` : 'Request'}
        open={deciding !== null}
        isOwn={deciding?.recorded_by === user?.id}
        busy={decide.isPending}
        error={error}
        onClose={() => setDeciding(null)}
        onDecide={run}
      >
        {deciding ? (
          <>
            <Card>
              <dl className="flex flex-col gap-1 text-sm">
                <Row label="Waiting for" value={statusLabel(deciding.status)} />
                <Row label="Recorded by" value={deciding.recorded_by_name ?? ''} />
                <Row label="Type" value={typeLabel(deciding)} />
                <Row label="Amount" value={<Money value={deciding.amount} />} />
                <Row
                  label="Days"
                  value={`${deciding.days} (${deciding.from_date} to ${deciding.to_date})`}
                />
                <Row label="A day" value={<Money value={deciding.daily_amount} />} />
                <Row label="Site" value={deciding.site_name ?? ''} />
                <Row label="Project" value={deciding.project_reference ?? ''} />
              </dl>
              {deciding.reason ? (
                <p className="mt-2 text-sm text-slate-600">{deciding.reason}</p>
              ) : null}
            </Card>
            {deciding.open_float_warning ? (
              <Banner tone="warning">
                Has an open float {deciding.open_float_warning.number} with{' '}
                <Money value={deciding.open_float_warning.balance} /> left.
              </Banner>
            ) : null}
            {wentStraightToFinance(deciding) ? (
              <Banner tone="info">Went straight to Finance.</Banner>
            ) : null}
          </>
        ) : null}
      </DecideSheet>
    </div>
  );
}
