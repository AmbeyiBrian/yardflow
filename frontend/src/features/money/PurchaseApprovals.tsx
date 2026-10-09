/**
 * Approvals: the Purchases tab (PM then Finance) and the Subcontract payments
 * tab (PM only) (design §4.19.3, §4.19.13; R7, R8).
 *
 * Both lists come from the server already level-aware. The recorder is never
 * offered Approve on their own entry (R4); the server refuses it as well.
 * A purchase from a supplier still awaiting approval may be approved, but not
 * paid (R15), so the sheet says so up front.
 */

import { useState } from 'react';

import { errorMessage } from '../../api/hooks';
import { useSession } from '../../auth/session';
import { Banner, Card } from '../../components/ui';
import { DataList, EmptyState, ListState } from '../../components/ui/data';
import { Money } from '../../components/ui/money';
import { AttachedPhotos, DecideSheet, Row, ToPayLink } from '../dispatch/FinanceApprovals';
import {
  useDecidePurchase,
  useDecideSubcontractPayment,
  usePendingPurchases,
  usePendingSubcontractPayments,
  type SubcontractPayment,
} from './purchaseApprovalsApi';
import { paymentLevelLabel, supplierNote, willCreateDelivery } from './purchaseApprovalsRules';
import type { SitePurchase } from './purchasesApi';
import { statusLabel } from './rules';

export function PurchaseApprovalQueue() {
  const { user } = useSession();
  const [deciding, setDeciding] = useState<SitePurchase | null>(null);
  const [error, setError] = useState('');
  const pending = usePendingPurchases({ page_size: 100 });
  const decide = useDecidePurchase();

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
        <DataList<SitePurchase>
          rows={pending.data?.results ?? []}
          rowKey={(purchase) => purchase.id}
          onRowClick={(purchase) => {
            setError('');
            setDeciding(purchase);
          }}
          empty={<EmptyState title="Nothing waiting." hint="Purchases appear here for approval." />}
          columns={[
            {
              header: 'Purchase',
              cell: (purchase) => (
                <span className="flex flex-col">
                  <span>
                    {purchase.number} · {purchase.supplier_name ?? ''}
                  </span>
                  <span className="text-xs text-slate-500">
                    {statusLabel(purchase.status)}
                    {purchase.is_over_budget ? ' · over budget' : ''}
                  </span>
                </span>
              ),
            },
            { header: 'Amount', cell: (purchase) => <Money value={purchase.amount} /> },
            { header: 'Who', cell: (purchase) => purchase.recorded_by_name ?? '' },
          ]}
        />
      </ListState>

      <DecideSheet
        key={deciding?.id ?? 'none'}
        title={deciding ? `Purchase ${deciding.number}` : 'Purchase'}
        open={deciding !== null}
        isOwn={deciding?.recorded_by !== undefined && deciding.recorded_by === user?.id}
        busy={decide.isPending}
        error={error}
        onClose={() => setDeciding(null)}
        onDecide={run}
      >
        {deciding ? <PurchaseBody purchase={deciding} /> : null}
      </DecideSheet>
    </div>
  );
}

function PurchaseBody({ purchase }: { purchase: SitePurchase }) {
  const note = supplierNote(purchase.supplier_status);
  return (
    <>
      <Card>
        <dl className="flex flex-col gap-1 text-sm">
          <Row label="Waiting for" value={statusLabel(purchase.status)} />
          <Row label="Recorded by" value={purchase.recorded_by_name ?? ''} />
          <Row
            label="Supplier"
            value={note ? `${purchase.supplier_name ?? ''} (${note})` : (purchase.supplier_name ?? '')}
          />
          <Row label="Site" value={purchase.site_name ?? ''} />
          <Row label="Project" value={purchase.project_reference ?? ''} />
          <Row label="Bought" value={purchase.purchase_date} />
          <Row
            label="Goes"
            value={
              purchase.destination === 'INTO_YARD'
                ? `Into ${purchase.receive_into_name ?? 'the yard'}`
                : 'Used at site'
            }
          />
          <Row label="Total" value={<Money value={purchase.amount} />} />
        </dl>
        <ul className="mt-2 flex flex-col gap-1 border-t border-slate-100 pt-2 text-sm">
          {purchase.lines.map((line, index) => (
            <li key={line.id ?? index} className="flex justify-between gap-3">
              <span className="text-slate-700">
                {line.quantity} {line.uom ?? ''} {line.item_type_name || line.description}
              </span>
              <span className="text-slate-900">
                <Money value={line.line_total ?? line.unit_price} />
              </span>
            </li>
          ))}
        </ul>
      </Card>
      {purchase.is_over_budget ? (
        <Banner tone="warning">
          Over budget
          {purchase.over_budget_by ? (
            <>
              {' '}
              by <Money value={purchase.over_budget_by} />
            </>
          ) : null}
          . {purchase.over_budget_reason ? `Reason: ${purchase.over_budget_reason}` : 'No reason given.'}
        </Banner>
      ) : null}
      {willCreateDelivery(purchase) ? (
        <Banner tone="info">Approving will create a delivery for the storekeeper to receive.</Banner>
      ) : null}
      {note ? (
        <Banner tone="info">
          The supplier is {note}. You can approve this, but it can't be paid until they are approved.
        </Banner>
      ) : null}
      <AttachedPhotos targetType="commercials.SitePurchase" targetId={purchase.id} />
    </>
  );
}

/** R8: the PM approves a subcontract payment Finance entered; one level. */
export function SubcontractPaymentQueue() {
  const { user } = useSession();
  const [deciding, setDeciding] = useState<SubcontractPayment | null>(null);
  const [error, setError] = useState('');
  const pending = usePendingSubcontractPayments({ page_size: 100 });
  const decide = useDecideSubcontractPayment();

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
      <ListState query={pending}>
        <DataList<SubcontractPayment>
          rows={pending.data?.results ?? []}
          rowKey={(payment) => payment.id}
          onRowClick={(payment) => {
            setError('');
            setDeciding(payment);
          }}
          empty={
            <EmptyState
              title="Nothing waiting."
              hint="Subcontract payments appear here for approval."
            />
          }
          columns={[
            {
              header: 'Payment',
              cell: (payment) => (
                <span className="flex flex-col">
                  <span>
                    {payment.subcontract_number ?? ''} · {payment.subcontractor_name ?? ''}
                  </span>
                  <span className="text-xs text-slate-500">{paymentLevelLabel(payment.status)}</span>
                </span>
              ),
            },
            { header: 'Amount', cell: (payment) => <Money value={payment.amount} /> },
            { header: 'Who', cell: (payment) => payment.recorded_by_name ?? '' },
          ]}
        />
      </ListState>

      <DecideSheet
        key={deciding?.id ?? 'none'}
        title="Subcontract payment"
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
                <Row label="Waiting for" value={paymentLevelLabel(deciding.status)} />
                <Row label="Entered by" value={deciding.recorded_by_name ?? ''} />
                <Row label="Subcontractor" value={deciding.subcontractor_name ?? ''} />
                <Row label="Contract" value={deciding.subcontract_number ?? ''} />
                <Row label="Project" value={deciding.project_reference ?? ''} />
                <Row label="Amount" value={<Money value={deciding.amount} />} />
                <Row label="Paid on" value={deciding.paid_on} />
                <Row label="Reference" value={deciding.reference} />
              </dl>
            </Card>
            <AttachedPhotos targetType="commercials.SubcontractPayment" targetId={deciding.id} />
          </>
        ) : null}
      </DecideSheet>
    </div>
  );
}
