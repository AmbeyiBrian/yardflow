/**
 * Approvals, Suppliers tab (T17.13; design §4.20.3, §4.20.10; R15).
 *
 * Finance (`finance.approve`) checks a new supplier before it can be paid:
 * KRA PIN, at least one payment route, and the documents. One level, no PM
 * level (a supplier has no project). The registrar is never offered Approve or
 * Reject on their own entry (`FINANCE_SELF_APPROVAL`); the server refuses it too.
 */

import { type ReactNode, useEffect, useState } from 'react';

import { api } from '../../api/client';
import { errorMessage } from '../../api/hooks';
import { useSession } from '../../auth/session';
import { Banner, Button, Card, Field, Spinner, Textarea } from '../../components/ui';
import { DataList, EmptyState, ListState, Sheet } from '../../components/ui/data';
import type { Attachment } from '../../components/PhotoCapture';
import { paymentRoutes } from './supplierRules';
import { type Supplier, useDecideSupplier, useSuppliers } from './suppliersApi';

function Row({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex justify-between gap-3">
      <dt className="text-slate-500">{label}</dt>
      <dd className="text-right font-medium break-words text-slate-900">{value}</dd>
    </div>
  );
}

/** The supplier's documents, as links (pre-signed, expiring URLs, N-7). */
function SupplierDocuments({ id }: { id: number }) {
  const [items, setItems] = useState<Attachment[] | null>(null);
  useEffect(() => {
    let cancelled = false;
    const query = new URLSearchParams({
      target_type: 'network.Supplier',
      target_id: String(id),
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
  }, [id]);

  if (items === null) return <Spinner />;
  if (items.length === 0) return <span className="text-slate-500">None attached</span>;
  return (
    <ul className="flex flex-col items-end gap-1">
      {items.map((item) => (
        <li key={item.id}>
          <a
            href={item.download_url}
            target="_blank"
            rel="noreferrer"
            className="text-sky-700 underline"
          >
            {item.caption ? `${item.caption}: ` : ''}
            {item.filename}
          </a>
        </li>
      ))}
    </ul>
  );
}

export function SupplierApprovalQueue() {
  const { user } = useSession();
  const [deciding, setDeciding] = useState<Supplier | null>(null);
  const [error, setError] = useState('');
  const [reason, setReason] = useState('');
  const [needReason, setNeedReason] = useState(false);
  const pending = useSuppliers({ status: 'PENDING' });
  const decide = useDecideSupplier();

  // R15: the registrar cannot approve their own entry.
  const isOwn = deciding !== null && deciding.registered_by === user?.id;

  function open(supplier: Supplier) {
    setError('');
    setReason('');
    setNeedReason(false);
    setDeciding(supplier);
  }

  async function run(approved: boolean) {
    if (!deciding) return;
    if (!approved && !reason.trim()) {
      setNeedReason(true);
      return;
    }
    setError('');
    try {
      await decide.mutateAsync({
        id: deciding.id,
        approved,
        reason: approved ? '' : reason.trim(),
      });
      setDeciding(null);
    } catch (caught) {
      // SUPPLIER_PIN_REQUIRED (no PIN or no payment route) and the rest (§4.20.11).
      setError(errorMessage(caught));
    }
  }

  const routes = deciding ? paymentRoutes(deciding) : [];

  return (
    <div className="flex flex-col gap-3">
      <ListState query={pending}>
        <DataList<Supplier>
          rows={pending.data?.results ?? []}
          rowKey={(supplier) => supplier.id}
          onRowClick={open}
          empty={<EmptyState title="Nothing waiting." hint="New suppliers appear here to check." />}
          columns={[
            {
              header: 'Supplier',
              cell: (supplier) => (
                <span className="flex flex-col">
                  <span>{supplier.name}</span>
                  <span className="text-xs text-slate-500">{supplier.kra_pin || 'No PIN yet'}</span>
                </span>
              ),
            },
            { header: 'Added by', cell: (supplier) => supplier.registered_by_name ?? '' },
            { header: 'Phone', cell: (supplier) => supplier.phone, wideOnly: true },
          ]}
        />
      </ListState>

      <Sheet
        // A fresh sheet per supplier, so one reason never carries to the next.
        key={deciding?.id ?? 'none'}
        open={deciding !== null}
        title={deciding?.name ?? 'Supplier'}
        onClose={() => setDeciding(null)}
        footer={
          isOwn ? undefined : (
            <>
              <Button
                variant="danger"
                className="flex-1"
                disabled={decide.isPending}
                onClick={() => run(false)}
              >
                Reject
              </Button>
              <Button className="flex-1" disabled={decide.isPending} onClick={() => run(true)}>
                {decide.isPending ? <Spinner /> : 'Approve'}
              </Button>
            </>
          )
        }
      >
        {deciding ? (
          <div className="flex flex-col gap-3">
            <Card>
              <dl className="flex flex-col gap-1 text-sm">
                <Row label="Added by" value={deciding.registered_by_name ?? ''} />
                <Row label="KRA PIN" value={deciding.kra_pin || 'Missing'} />
                <Row label="Contact" value={deciding.contact_name} />
                <Row label="Phone" value={deciding.phone} />
                {deciding.email ? <Row label="Email" value={deciding.email} /> : null}
                {deciding.address ? <Row label="Address" value={deciding.address} /> : null}
                <Row
                  label="Payment"
                  value={
                    routes.length === 0 ? (
                      'None on file'
                    ) : (
                      <span className="flex flex-col">
                        {routes.map((line) => (
                          <span key={line}>{line}</span>
                        ))}
                      </span>
                    )
                  }
                />
                <Row label="Documents" value={<SupplierDocuments id={deciding.id} />} />
              </dl>
            </Card>
            {!deciding.kra_pin || routes.length === 0 ? (
              <Banner tone="warning">
                Approval needs a KRA PIN and at least one payment route.
              </Banner>
            ) : null}
            {isOwn ? (
              <Banner tone="info">You added this, so somebody else has to approve it.</Banner>
            ) : (
              <Field
                label="Reason"
                htmlFor="supplier-reason"
                hint="Required to reject. The person who added it sees it."
                error={needReason && !reason.trim() ? 'Say why you are rejecting it.' : undefined}
              >
                <Textarea
                  id="supplier-reason"
                  value={reason}
                  onChange={(event) => setReason(event.target.value)}
                />
              </Field>
            )}
            {error ? <Banner tone="error">{error}</Banner> : null}
          </div>
        ) : null}
      </Sheet>
    </div>
  );
}
