/**
 * One expense, one request (Epic R, R1–R4; design §4.17.2, §4.17.10).
 *
 * Read-only: what was asked, where it stands, who decided and why, the photos
 * that came with it, and — for a float — what has been spent and what is left.
 * Approving, paying and closing are the approvers' screens (T15.9). The
 * decision shown is the latest one on the row; a per-level history is not
 * carried by the list endpoints.
 */

import { useState, type ReactNode } from 'react';
import { Link, useLocation, useParams } from 'react-router-dom';

import { useQueryClient } from '@tanstack/react-query';

import { useResource } from '../../api/hooks';
import { PhotoCapture, type Attachment } from '../../components/PhotoCapture';
import { Banner, Button, Card, Spinner } from '../../components/ui';
import { EmptyState, ListState, PageHeader } from '../../components/ui/data';
import { Money } from '../../components/ui/money';
import { useAllowanceRequest, useExpense, useExpenses } from './api';
import { sendTo, uploadItems, type UploadItem } from './drafts';
import { ResubmitButton, StatusPill } from './MoneyHomePage';
import { TYPE_LABELS } from './RequestAllowancePage';
import type { AllowanceRequest, ProjectExpense } from './types';

function Rows({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="flex flex-col gap-2">
      {rows
        .filter(([, value]) => value !== null && value !== undefined && value !== '')
        .map(([label, value]) => (
          <div key={label} className="flex justify-between gap-3 text-sm">
            <dt className="text-slate-500">{label}</dt>
            <dd className="text-right font-medium text-slate-900">{value}</dd>
          </div>
        ))}
    </dl>
  );
}

function Photos({ targetType, targetId }: { targetType: string; targetId: number }) {
  const query = useResource<Attachment[] | { results: Attachment[] }>('attachments', {
    target_type: targetType,
    target_id: String(targetId),
    page_size: 100,
  });
  const items = Array.isArray(query.data) ? query.data : (query.data?.results ?? []);

  return (
    <Card className="flex flex-col gap-2">
      <p className="text-sm font-semibold text-slate-900">Photos</p>
      <ListState query={query}>
        {items.length === 0 ? (
          <p className="text-sm text-slate-500">No photos.</p>
        ) : (
          <ul className="grid grid-cols-3 gap-2">
            {items.map((a) => (
              <li key={a.id} className="flex flex-col gap-1">
                {a.content_type.startsWith('image/') ? (
                  <img
                    src={a.download_url}
                    alt={a.caption || a.filename}
                    className="aspect-square w-full rounded-lg object-cover"
                  />
                ) : (
                  <a
                    href={a.download_url}
                    target="_blank"
                    rel="noreferrer"
                    className="flex aspect-square w-full items-center justify-center rounded-lg border border-slate-200 bg-slate-50 p-1 text-center text-xs break-all text-slate-600"
                  >
                    {a.filename}
                  </a>
                )}
                {a.caption ? (
                  <span className="text-center text-xs text-slate-500">{a.caption}</span>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </ListState>
    </Card>
  );
}

/**
 * Photos taken in the form that did not go up (R1). The expense is saved either
 * way; this says how many are left and retries them with the same `client_uuid`s,
 * so one that did land on a lost response is not attached twice.
 */
function FailedPhotos({ targetId, items }: { targetId: number; items: UploadItem[] }) {
  const [left, setLeft] = useState(items);
  const [busy, setBusy] = useState(false);
  const client = useQueryClient();

  if (left.length === 0) return null;

  async function retry() {
    setBusy(true);
    setLeft(await uploadItems(left, sendTo('commercials.ProjectExpense', targetId)));
    setBusy(false);
    await client.invalidateQueries({ queryKey: ['attachments'] });
  }

  return (
    <Banner tone="error">
      <span className="flex flex-col items-start gap-2">
        <span>
          {left.length} {left.length === 1 ? 'photo' : 'photos'} did not send:{' '}
          {left.map((item) => `${item.caption} (${item.filename})`).join(', ')}. The expense is
          saved.
        </span>
        <Button variant="secondary" loading={busy} onClick={() => void retry()}>
          Send again
        </Button>
      </span>
    </Banner>
  );
}

function Decision({
  row,
}: {
  row: Pick<
    ProjectExpense,
    'status' | 'decided_at' | 'decision_reason' | 'paid_at' | 'payment_reference'
  >;
}) {
  return (
    <>
      {row.status === 'REJECTED' && row.decision_reason ? (
        <Banner tone="error">Rejected: {row.decision_reason}</Banner>
      ) : null}
      <Rows
        rows={[
          ['Decided', row.decided_at?.slice(0, 10)],
          ['Paid', row.paid_at?.slice(0, 10)],
          ['Payment reference', row.payment_reference],
        ]}
      />
    </>
  );
}

function Loading<T>({
  query,
  children,
}: {
  query: { isLoading: boolean; isError: boolean; error?: unknown; refetch?: () => unknown; data?: T };
  children: (data: T) => ReactNode;
}) {
  if (query.isLoading) return <Spinner className="text-slate-400" />;
  return (
    <ListState query={query}>
      {query.data ? children(query.data) : <EmptyState title="Not found." />}
    </ListState>
  );
}

export function ExpenseDetailPage() {
  const { id } = useParams();
  const query = useExpense(id);
  const failedPhotos = (useLocation().state as { failedPhotos?: UploadItem[] } | null)
    ?.failedPhotos;

  return (
    <Loading query={query}>
      {(e) => (
        <div className="flex flex-col gap-4">
          <PageHeader
            title={`${e.category_name ?? 'Expense'} ${e.project_reference ?? ''}`.trim()}
            subtitle={<StatusPill status={e.status} />}
          />
          <Card className="flex flex-col gap-3">
            <Rows
              rows={[
                ['Amount', <Money key="a" value={e.amount} />],
                ['Date', e.incurred_on],
                ['Site', e.site_name],
                ['Project', e.project_reference],
                ['Scope of work', e.scope_of_work],
                ['What for', e.description],
                ['Vehicle', e.vehicle_reg],
                ['Litres', e.litres],
                ['Recorded by', e.recorded_by_name],
                [
                  'Paid from float',
                  e.float_request ? (
                    <Link key="f" to={`/money/requests/${e.float_request}`} className="underline">
                      Open the float
                    </Link>
                  ) : null,
                ],
                [
                  'Evidence',
                  e.evidence_state === 'none'
                    ? 'No photo'
                    : e.evidence_state === 'arriving'
                      ? 'Photos on the way'
                      : 'Photos attached',
                ],
              ]}
            />
            {e.casual_lines.length > 0 ? (
              <div>
                <p className="mb-1 text-sm font-semibold text-slate-900">Casuals</p>
                <ul className="flex flex-col gap-1 text-sm">
                  {e.casual_lines.map((line, index) => (
                    <li key={line.id ?? index} className="flex justify-between gap-3">
                      <span>{line.casual_name ?? `Casual ${line.casual}`}</span>
                      <span className="text-slate-600">
                        {line.days} {line.days === 1 ? 'day' : 'days'}
                        {line.amount ? ` · ${line.amount}` : ''}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
            <Decision row={e} />
            {e.status === 'REJECTED' ? <ResubmitButton id={e.id} kind="expense" /> : null}
          </Card>
          {failedPhotos?.length ? <FailedPhotos targetId={e.id} items={failedPhotos} /> : null}
          {/* While it is still pending more photos can be added (R1); after that, read-only. */}
          {e.status === 'PENDING_PM' || e.status === 'PENDING_FINANCE' ? (
            <PhotoCapture
              targetType="commercials.ProjectExpense"
              targetId={e.id}
              label="Photos"
              caption="Other"
            />
          ) : (
            <Photos targetType="commercials.ProjectExpense" targetId={e.id} />
          )}
        </div>
      )}
    </Loading>
  );
}

function FloatLedger({ request }: { request: AllowanceRequest }) {
  const query = useExpenses({ float_request: request.id, page_size: 100 });
  const rows = query.data?.results ?? [];
  return (
    <Card className="flex flex-col gap-2">
      <p className="text-sm font-semibold text-slate-900">Float</p>
      <Rows
        rows={[
          ['Given', <Money key="g" value={request.amount} />],
          ['Spent', request.spent !== undefined ? <Money key="s" value={request.spent} /> : null],
          [
            'Returned',
            request.returned_amount ? <Money key="r" value={request.returned_amount} /> : null,
          ],
          [
            'Balance',
            request.balance !== undefined ? <Money key="b" value={request.balance} /> : null,
          ],
        ]}
      />
      {request.balance !== undefined && Number(request.balance) < 0 ? (
        <p className="text-sm text-amber-700">A negative balance means it is owed to you.</p>
      ) : null}
      <ListState query={query}>
        {rows.length > 0 ? (
          <ul className="mt-1 flex flex-col gap-1 text-sm">
            {rows.map((row) => (
              <li key={row.id} className="flex justify-between gap-3">
                <Link to={`/money/expenses/${row.id}`} className="underline">
                  {row.category_name ?? 'Expense'} {row.incurred_on}
                </Link>
                <Money value={row.amount} />
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-slate-500">Nothing recorded against it yet.</p>
        )}
      </ListState>
    </Card>
  );
}

export function RequestDetailPage() {
  const { id } = useParams();
  const query = useAllowanceRequest(id);

  return (
    <Loading query={query}>
      {(r) => (
        <div className="flex flex-col gap-4">
          <PageHeader
            title={`${TYPE_LABELS[r.type]} ${r.number ?? ''}`.trim()}
            subtitle={<StatusPill status={r.status} />}
          />
          <Card className="flex flex-col gap-3">
            <Rows
              rows={[
                ['Amount', <Money key="a" value={r.amount} />],
                [
                  'Where to',
                  r.transport_scope === 'WITHIN_NAIROBI'
                    ? 'Within Nairobi'
                    : r.transport_scope === 'OUTSIDE_NAIROBI'
                      ? 'Outside Nairobi'
                      : null,
                ],
                ['From', r.from_date],
                ['To', r.to_date],
                ['Days', r.days],
                ['Per day', <Money key="d" value={r.daily_amount} />],
                ['Site', r.site_name],
                ['Project', r.project_reference],
                ['Reason', r.reason],
                ['Recorded by', r.recorded_by_name],
              ]}
            />
            <Decision row={{ ...r, status: r.status }} />
            {r.closed_at ? <p className="text-sm text-slate-600">Closed {r.closed_at.slice(0, 10)}.</p> : null}
            {r.status === 'REJECTED' ? <ResubmitButton id={r.id} kind="request" /> : null}
          </Card>
          {r.type === 'FLOAT' && (r.status === 'PAID' || r.closed_at) ? (
            <FloatLedger request={r} />
          ) : null}
        </div>
      )}
    </Loading>
  );
}
