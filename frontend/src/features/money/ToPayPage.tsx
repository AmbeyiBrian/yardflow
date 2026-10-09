/**
 * To pay (Epic R; R2, R4, R7; design §4.17.6, §4.17.10, §4.19.13): approved,
 * unpaid expenses, requests and site purchases (a purchase waits while its
 * supplier is unapproved, R15), with Mark paid, and paid floats still open, with
 * Close float. `finance.approve` only; the server re-checks.
 *
 * YardFlow records the payment; it does not send money, so the reference
 * (an M-Pesa code, say) is what makes "paid" mean something. Errors such as
 * PAYMENT_REFERENCE_REQUIRED and FLOAT_NOT_OPEN are shown as the server words
 * them.
 */

import { useState } from 'react';
import { Link } from 'react-router-dom';

import { errorMessage } from '../../api/hooks';
import { Banner, Button, Card, Field, Input, Spinner } from '../../components/ui';
import { DataList, EmptyState, ListState, PageHeader, Sheet } from '../../components/ui/data';
import { Money, MoneyInput } from '../../components/ui/money';
import { typeLabel } from '../dispatch/FinanceApprovals';
import {
  useAllowanceRequests,
  useCloseFloat,
  useExpenses,
  useMarkAllowancePaid,
  useMarkExpensePaid,
} from './api';
import { useMarkPurchasePaid } from './purchaseApprovalsApi';
import { payBlockedReason, supplierNote } from './purchaseApprovalsRules';
import { useSitePurchases, type SitePurchase } from './purchasesApi';
import type { AllowanceRequest, ProjectExpense } from './types';

/** Today as the phone's calendar reads it, not UTC. */
function today(): string {
  const now = new Date();
  const month = String(now.getMonth() + 1).padStart(2, '0');
  const day = String(now.getDate()).padStart(2, '0');
  return `${now.getFullYear()}-${month}-${day}`;
}

type Payable =
  | { kind: 'expense'; row: ProjectExpense }
  | { kind: 'request'; row: AllowanceRequest }
  | { kind: 'purchase'; row: SitePurchase };

export default function ToPayPage() {
  const expenses = useExpenses({ payable: true, page_size: 100 });
  const requests = useAllowanceRequests({ payable: true, page_size: 100 });
  const purchases = useSitePurchases({ payable: true, page_size: 100 });
  const floats = useAllowanceRequests({ type: 'FLOAT', status: 'PAID', page_size: 100 });
  const [paying, setPaying] = useState<Payable | null>(null);
  const [closing, setClosing] = useState<AllowanceRequest | null>(null);

  const openFloats = (floats.data?.results ?? []).filter((request) => !request.closed_at);

  return (
    <div className="flex flex-1 flex-col gap-6">
      <PageHeader
        title="To pay"
        subtitle="Approved and waiting for payment, and floats still open."
        actions={
          <Link
            to="/money"
            className="flex min-h-[44px] items-center px-2 text-sm text-slate-700"
          >
            Back to Money
          </Link>
        }
      />

      <section className="flex flex-col gap-2" aria-label="Expenses to pay">
        <h2 className="text-base font-semibold text-slate-900">Expenses</h2>
        <ListState query={expenses}>
          <DataList<ProjectExpense>
            rows={expenses.data?.results ?? []}
            rowKey={(expense) => expense.id}
            onRowClick={(expense) => setPaying({ kind: 'expense', row: expense })}
            empty={<EmptyState title="Nothing to pay." hint="Approved expenses appear here." />}
            columns={[
              { header: 'What', cell: (expense) => expense.category_name ?? '' },
              { header: 'Amount', cell: (expense) => <Money value={expense.amount} /> },
              { header: 'Who', cell: (expense) => expense.recorded_by_name ?? '' },
            ]}
          />
        </ListState>
      </section>

      <section className="flex flex-col gap-2" aria-label="Requests to pay">
        <h2 className="text-base font-semibold text-slate-900">Requests</h2>
        <ListState query={requests}>
          <DataList<AllowanceRequest>
            rows={requests.data?.results ?? []}
            rowKey={(request) => request.id}
            onRowClick={(request) => setPaying({ kind: 'request', row: request })}
            empty={<EmptyState title="Nothing to pay." hint="Approved requests appear here." />}
            columns={[
              {
                header: 'Request',
                cell: (request) => `${request.number} · ${typeLabel(request)}`,
              },
              { header: 'Amount', cell: (request) => <Money value={request.amount} /> },
              { header: 'Who', cell: (request) => request.recorded_by_name ?? '' },
            ]}
          />
        </ListState>
      </section>

      <section className="flex flex-col gap-2" aria-label="Purchases to pay">
        <h2 className="text-base font-semibold text-slate-900">Purchases</h2>
        <ListState query={purchases}>
          <DataList<SitePurchase>
            rows={purchases.data?.results ?? []}
            rowKey={(purchase) => purchase.id}
            onRowClick={(purchase) => setPaying({ kind: 'purchase', row: purchase })}
            empty={<EmptyState title="Nothing to pay." hint="Approved purchases appear here." />}
            columns={[
              {
                header: 'Purchase',
                cell: (purchase) => (
                  <span className="flex flex-col">
                    <span>
                      {purchase.number} · {purchase.supplier_name ?? ''}
                    </span>
                    {supplierNote(purchase.supplier_status) ? (
                      <span className="text-xs text-amber-800">
                        Supplier {supplierNote(purchase.supplier_status)}
                      </span>
                    ) : null}
                  </span>
                ),
              },
              { header: 'Amount', cell: (purchase) => <Money value={purchase.amount} /> },
              { header: 'Who', cell: (purchase) => purchase.recorded_by_name ?? '' },
            ]}
          />
        </ListState>
      </section>

      <section className="flex flex-col gap-2" aria-label="Open floats">
        <h2 className="text-base font-semibold text-slate-900">Open floats</h2>
        <ListState query={floats}>
          <DataList<AllowanceRequest>
            rows={openFloats}
            rowKey={(request) => request.id}
            onRowClick={(request) => setClosing(request)}
            empty={<EmptyState title="No open floats." hint="Paid floats stay here until closed." />}
            columns={[
              {
                header: 'Float',
                cell: (request) => `${request.number} · ${request.recorded_by_name ?? ''}`,
              },
              { header: 'Spent', cell: (request) => <Money value={request.spent} /> },
              { header: 'Balance', cell: (request) => <Money value={request.balance} /> },
            ]}
          />
        </ListState>
      </section>

      <MarkPaidSheet target={paying} onClose={() => setPaying(null)} />
      <CloseFloatSheet request={closing} onClose={() => setClosing(null)} />
    </div>
  );
}

function MarkPaidSheet({ target, onClose }: { target: Payable | null; onClose: () => void }) {
  const [reference, setReference] = useState('');
  const [paidAt, setPaidAt] = useState(today);
  const [error, setError] = useState('');
  const payExpense = useMarkExpensePaid();
  const payRequest = useMarkAllowancePaid();
  const payPurchase = useMarkPurchasePaid();
  const busy = payExpense.isPending || payRequest.isPending || payPurchase.isPending;
  // R15 / §4.19.10: a purchase cannot be paid until its supplier is approved.
  const blocked = target?.kind === 'purchase' ? payBlockedReason(target.row) : '';

  function close() {
    setReference('');
    setPaidAt(today());
    setError('');
    onClose();
  }

  async function submit() {
    if (!target) return;
    setError('');
    if (!reference.trim()) {
      setError('A payment reference is required, such as the M-Pesa code.');
      return;
    }
    const body = { id: target.row.id, payment_reference: reference.trim(), paid_at: paidAt };
    try {
      if (target.kind === 'expense') await payExpense.mutateAsync(body);
      else if (target.kind === 'purchase') await payPurchase.mutateAsync(body);
      else await payRequest.mutateAsync(body);
      close();
    } catch (caught) {
      setError(errorMessage(caught));
    }
  }

  const title =
    target?.kind === 'request' || target?.kind === 'purchase'
      ? `Mark ${target.row.number} paid`
      : 'Mark expense paid';

  return (
    <Sheet
      open={target !== null}
      title={title}
      onClose={close}
      footer={
        <>
          <Button variant="secondary" className="flex-1" onClick={close}>
            Back
          </Button>
          <Button className="flex-1" disabled={busy || blocked !== ''} onClick={submit}>
            {busy ? <Spinner /> : 'Mark paid'}
          </Button>
        </>
      }
    >
      {target ? (
        <div className="flex flex-col gap-3">
          <Card>
            <p className="text-sm text-slate-600">
              {target.row.recorded_by_name ?? ''} ·{' '}
              {target.kind === 'expense'
                ? (target.row.category_name ?? '')
                : target.kind === 'purchase'
                  ? (target.row.supplier_name ?? '')
                  : typeLabel(target.row)}
            </p>
            <p className="text-base font-semibold text-slate-900">
              <Money value={target.row.amount} />
            </p>
          </Card>
          {blocked ? <Banner tone="warning">{blocked}</Banner> : null}
          <Field label="Payment reference" htmlFor="pay-ref" hint="For example the M-Pesa code.">
            <Input
              id="pay-ref"
              value={reference}
              onChange={(event) => setReference(event.target.value)}
            />
          </Field>
          <Field label="Paid on" htmlFor="pay-date">
            <Input
              id="pay-date"
              type="date"
              value={paidAt}
              onChange={(event) => setPaidAt(event.target.value)}
            />
          </Field>
          {error ? <Banner tone="error">{error}</Banner> : null}
        </div>
      ) : null}
    </Sheet>
  );
}

function CloseFloatSheet({
  request,
  onClose,
}: {
  request: AllowanceRequest | null;
  onClose: () => void;
}) {
  const [returned, setReturned] = useState('0');
  const [error, setError] = useState('');
  const close = useCloseFloat();

  function dismiss() {
    setReturned('0');
    setError('');
    onClose();
  }

  async function submit() {
    if (!request) return;
    setError('');
    const amount = Number(returned.replace(/,/g, ''));
    if (returned.trim() === '' || Number.isNaN(amount) || amount < 0) {
      setError('Enter what came back, or 0 if nothing did.');
      return;
    }
    try {
      await close.mutateAsync({ id: request.id, returned_amount: returned.replace(/,/g, '') });
      dismiss();
    } catch (caught) {
      setError(errorMessage(caught));
    }
  }

  return (
    <Sheet
      open={request !== null}
      title={request ? `Close ${request.number}` : 'Close float'}
      onClose={dismiss}
      footer={
        <>
          <Button variant="secondary" className="flex-1" onClick={dismiss}>
            Back
          </Button>
          <Button className="flex-1" disabled={close.isPending} onClick={submit}>
            {close.isPending ? <Spinner /> : 'Close float'}
          </Button>
        </>
      }
    >
      {request ? (
        <div className="flex flex-col gap-3">
          <Card>
            <dl className="flex flex-col gap-1 text-sm">
              <div className="flex justify-between gap-3">
                <dt className="text-slate-500">Paid out</dt>
                <dd className="font-medium text-slate-900">
                  <Money value={request.amount} />
                </dd>
              </div>
              <div className="flex justify-between gap-3">
                <dt className="text-slate-500">Spent</dt>
                <dd className="font-medium text-slate-900">
                  <Money value={request.spent} />
                </dd>
              </div>
              <div className="flex justify-between gap-3">
                <dt className="text-slate-500">Balance</dt>
                <dd className="font-medium text-slate-900">
                  <Money value={request.balance} />
                </dd>
              </div>
            </dl>
          </Card>
          <Field
            label="Returned amount"
            htmlFor="float-returned"
            hint="What came back to the company. Closing records it; it cannot be reopened."
          >
            <MoneyInput
              id="float-returned"
              defaultValue={returned}
              onChange={(event) => setReturned(event.target.value)}
            />
          </Field>
          {error ? <Banner tone="error">{error}</Banner> : null}
        </div>
      ) : null}
    </Sheet>
  );
}
