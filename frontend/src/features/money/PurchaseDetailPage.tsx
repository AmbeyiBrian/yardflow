/**
 * One site purchase (Epic R, R7, R9; design §4.19.3, §4.19.13; T18.16).
 *
 * Read-only: supplier, lines, destination, where it stands and who decided it,
 * the over-budget reason the recorder gave (the figures are the approvers', R9),
 * and the receipt photos. A yard purchase links to the delivery its approval made.
 */

import { Link, useLocation, useParams } from 'react-router-dom';

import { PhotoCapture } from '../../components/PhotoCapture';
import { Banner, Button, Card } from '../../components/ui';
import { PageHeader } from '../../components/ui/data';
import { errorMessage } from '../../api/hooks';
import { Money } from '../../components/ui/money';
import type { UploadItem } from './drafts';
import { Loading, Photos, Rows } from './DetailPages';
import { StatusPill } from './MoneyHomePage';
import { useResubmitSitePurchase, useSitePurchase } from './purchasesApi';

export default function PurchaseDetailPage() {
  const { id } = useParams();
  const query = useSitePurchase(id);
  const resubmit = useResubmitSitePurchase();
  const failedPhotos = (useLocation().state as { failedPhotos?: UploadItem[] } | null)
    ?.failedPhotos;

  return (
    <Loading query={query}>
      {(p) => (
        <div className="flex flex-col gap-4">
          <PageHeader
            title={`Purchase ${p.number} ${p.project_reference ?? ''}`.trim()}
            subtitle={<StatusPill status={p.status} />}
          />
          {failedPhotos?.length ? (
            <Banner tone="error">
              {failedPhotos.length} {failedPhotos.length === 1 ? 'photo' : 'photos'} did not send.
              The purchase is saved; add them again below.
            </Banner>
          ) : null}
          <Card className="flex flex-col gap-3">
            <Rows
              rows={[
                ['Total', <Money key="a" value={p.amount} />],
                ['Supplier', p.supplier_name],
                ['Date', p.purchase_date],
                ['Site', p.site_name],
                ['Project', p.project_reference],
                ['Goes', p.destination === 'INTO_YARD' ? 'Into the yard' : 'Used at the site'],
                ['Received into', p.receive_into_name],
                ['Recorded by', p.recorded_by_name],
                ['Why over budget', p.over_budget_reason],
                ['Decided', p.decided_at?.slice(0, 10)],
                ['Paid', p.paid_at?.slice(0, 10)],
                ['Payment reference', p.payment_reference],
                [
                  'Delivery',
                  p.gate_in ? (
                    <Link key="g" to={`/gate-in/${p.gate_in}`} className="underline">
                      Open the delivery
                    </Link>
                  ) : null,
                ],
              ]}
            />
            {p.status === 'REJECTED' && p.decision_reason ? (
              <Banner tone="error">Rejected: {p.decision_reason}</Banner>
            ) : null}
            <div>
              <p className="mb-1 text-sm font-semibold text-slate-900">Lines</p>
              <ul className="flex flex-col gap-1 text-sm">
                {p.lines.map((line, index) => (
                  <li key={line.id ?? index} className="flex justify-between gap-3">
                    <span>
                      {line.item_type_name || line.description} · {line.quantity}
                      {line.uom ? ` ${line.uom}` : ''} × {line.unit_price}
                    </span>
                    {line.line_total ? <Money value={line.line_total} /> : null}
                  </li>
                ))}
              </ul>
            </div>
            {p.status === 'REJECTED' ? (
              <div className="flex flex-col gap-2">
                {resubmit.isError ? (
                  <Banner tone="error">{errorMessage(resubmit.error)}</Banner>
                ) : null}
                <Button
                  variant="secondary"
                  loading={resubmit.isPending}
                  onClick={() => resubmit.mutate({ id: p.id })}
                >
                  Resubmit
                </Button>
              </div>
            ) : null}
          </Card>
          {p.status === 'PENDING_PM' || p.status === 'PENDING_FINANCE' ? (
            <PhotoCapture
              targetType="commercials.SitePurchase"
              targetId={p.id}
              label="Photos"
              caption="Receipt"
            />
          ) : (
            <Photos targetType="commercials.SitePurchase" targetId={p.id} />
          )}
        </div>
      )}
    </Loading>
  );
}
