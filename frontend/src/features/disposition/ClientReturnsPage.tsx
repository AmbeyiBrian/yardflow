/**
 * T6.6 — client returns and their acknowledgement (design §7.4, §4.12; K1, K2, K3).
 *
 * The criterion ends with "**and records the acknowledgement**", and that is the
 * part this screen exists for. K3 is a liability requirement: until the client
 * signs, returned material is still the tenant's exposure — so the screen leads
 * with what is outstanding rather than with a list of returns.
 *
 * Three states, never collapsed into one number. "We are holding 40 of yours, 12
 * are on their way back, you signed for 8 last week" is a defensible answer to an
 * operator audit; "52" is not.
 */

import { useState } from 'react';
import { Link } from 'react-router-dom';

import { openDocument } from '../../api/client';
import { errorMessage, useAction, useList, useResource } from '../../api/hooks';
import { Banner, Button, Card, Field, Input, Spinner, Textarea } from '../../components/ui';
import { ControlledReferenceSelect } from '../../components/ui/ReferenceSelect';
import { EmptyState, PageHeader, Sheet, Stat } from '../../components/ui/data';
import type { GateOut } from '../dispatch/types';
import type { ClientPositionRow } from './types';

interface ClientRow {
  id: number;
  name: string;
}

const STATE_LABELS: Record<string, string> = {
  HELD: 'We are holding',
  IN_TRANSIT: 'On its way back',
  ACKNOWLEDGED: 'They have signed for',
};

export default function ClientReturnsPage() {
  const [clientId, setClientId] = useState('');
  const clients = useList<ClientRow>('clients', { page_size: 200 });
  const position = useResource<{ rows: ClientPositionRow[]; exposure: string }>(
    'client-position',
    clientId ? { client: clientId } : undefined,
  );
  // Returns already released. Acknowledgement is recorded against these.
  const returns = useList<GateOut>('gate-outs', {
    purpose_type: 'RETURN_TO_CLIENT',
    page_size: 50,
  });
  const acks = useList<{ id: number; gate_out: number; acknowledged_ref: string }>(
    'client-return-acks',
    { page_size: 200 },
  );

  const [acknowledging, setAcknowledging] = useState<GateOut | null>(null);
  const [banner, setBanner] = useState<string | null>(null);

  const rows = position.data?.rows ?? [];
  const byState = (state: string) =>
    rows
      .filter((row) => row.state === state)
      .reduce((total, row) => total + Number(row.quantity), 0);

  const acknowledgedIds = new Set((acks.data?.results ?? []).map((ack) => ack.gate_out));
  const outstanding = (returns.data?.results ?? []).filter(
    (row) =>
      !acknowledgedIds.has(row.id) &&
      ['PARTIALLY_RELEASED', 'RELEASED', 'CLOSED'].includes(row.status),
  );

  async function print(gateOut: GateOut) {
    setBanner(null);
    try {
      // K2: optional per tenant, so a refusal here is a setting rather than a
      // fault — the message from the server says which.
      await openDocument(`/gate-outs/${gateOut.id}/waybill`);
    } catch (error) {
      setBanner(errorMessage(error));
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Client material"
        subtitle="What we hold, what is going back, and what they have signed for."
        actions={
          <Link
            to="/gate-out/new"
            className="flex min-h-[44px] items-center rounded-lg border border-slate-300 px-4 text-sm font-medium text-slate-800"
          >
            Raise a return
          </Link>
        }
      />

      {banner ? <Banner tone="error">{banner}</Banner> : null}
      {position.isError ? (
        <Banner tone="error">{errorMessage(position.error)}</Banner>
      ) : null}

      <Card>
        <Field label="Client" htmlFor="client">
          <ControlledReferenceSelect resource="clients"
            id="client"
            value={clientId}
            onChange={(event) => setClientId(event.target.value)}
          >
            <option value="">Every client</option>
            {(clients.data?.results ?? []).map((client) => (
              <option key={client.id} value={client.id}>
                {client.name}
              </option>
            ))}
          </ControlledReferenceSelect>
        </Field>
      </Card>

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Stat label="Holding" value={byState('HELD') || '—'} />
        <Stat
          label="In transit"
          value={byState('IN_TRANSIT') || '—'}
          tone={byState('IN_TRANSIT') > 0 ? 'warn' : 'neutral'}
          hint="Gone, but still our exposure."
        />
        <Stat
          label="Acknowledged"
          value={byState('ACKNOWLEDGED') || '—'}
          tone="good"
          hint="Liability ended."
        />
        <Stat
          label="Our exposure"
          value={position.data?.exposure ?? '—'}
          tone={Number(position.data?.exposure ?? 0) > 0 ? 'warn' : 'good'}
          hint="Holding plus in transit."
        />
      </div>

      {outstanding.length > 0 ? (
        <Card className="flex flex-col gap-3 border-amber-300">
          <div>
            <h2 className="text-sm font-semibold text-slate-900">
              {outstanding.length} return{outstanding.length === 1 ? '' : 's'} not
              acknowledged
            </h2>
            <p className="text-sm text-slate-600">
              Until the client confirms receipt, this material is still recorded
              as ours to answer for.
            </p>
          </div>
          {outstanding.map((row) => (
            <div
              key={row.id}
              className="flex flex-wrap items-center justify-between gap-2 border-t border-slate-100 pt-2 first:border-0 first:pt-0"
            >
              <div className="min-w-0">
                <p className="text-sm font-medium text-slate-900">
                  {row.number} · {row.destination_label}
                </p>
                <p className="text-xs text-slate-500">
                  released {row.released_at?.slice(0, 10) ?? '—'} ·{' '}
                  {row.lines.length} line{row.lines.length === 1 ? '' : 's'}
                </p>
              </div>
              <div className="flex shrink-0 gap-2">
                <Button variant="secondary" onClick={() => void print(row)}>
                  Waybill
                </Button>
                <Button onClick={() => setAcknowledging(row)}>Record their signature</Button>
              </div>
            </div>
          ))}
        </Card>
      ) : null}

      {position.isLoading ? (
        <Spinner className="text-slate-400" />
      ) : rows.length === 0 ? (
        <EmptyState
          title="No client-owned material."
          hint="Consignment stock appears here as soon as it is received."
        />
      ) : (
        <Card className="flex flex-col gap-3">
          <h2 className="text-sm font-semibold text-slate-900">Position by item</h2>
          <ul className="flex flex-col gap-2">
            {rows.map((row) => (
              <li
                key={`${row.client_id}-${row.item_type_id}-${row.state}`}
                className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-100 pb-2 last:border-0"
              >
                <div className="min-w-0">
                  <p className="text-sm font-medium text-slate-900">{row.item_type}</p>
                  <p className="text-xs text-slate-500">
                    {row.client} · {STATE_LABELS[row.state] ?? row.state}
                    {row.documents.length > 0
                      ? ` · ${row.documents.map((entry) => entry.number).join(', ')}`
                      : ''}
                  </p>
                  {row.documents.some((entry) => entry.acknowledged_ref) ? (
                    <p className="text-xs text-emerald-700">
                      their ref{' '}
                      {row.documents
                        .filter((entry) => entry.acknowledged_ref)
                        .map((entry) => entry.acknowledged_ref)
                        .join(', ')}
                    </p>
                  ) : null}
                </div>
                <span
                  className={
                    row.state === 'IN_TRANSIT'
                      ? 'text-sm font-semibold text-amber-700'
                      : 'text-sm font-semibold text-slate-900'
                  }
                >
                  {row.quantity} {row.uom}
                </span>
              </li>
            ))}
          </ul>
        </Card>
      )}

      <AcknowledgeSheet
        gateOut={acknowledging}
        onClose={() => setAcknowledging(null)}
        onDone={() => {
          void position.refetch();
          void acks.refetch();
        }}
      />
    </div>
  );
}

/** K3: their reference, or their signed document. Either ends our liability. */
function AcknowledgeSheet({
  gateOut,
  onClose,
  onDone,
}: {
  gateOut: GateOut | null;
  onClose: () => void;
  onDone: () => void;
}) {
  const [reference, setReference] = useState('');
  const [when, setWhen] = useState('');
  const [who, setWho] = useState('');
  const [notes, setNotes] = useState('');
  const [banner, setBanner] = useState<string | null>(null);

  const record = useAction<Record<string, unknown>>({
    resource: 'client-return-acks',
    invalidates: ['client-return-acks', 'client-position', 'gate-outs'],
  });

  async function save() {
    if (!gateOut) return;
    setBanner(null);
    try {
      await record.mutateAsync({
        gate_out: gateOut.id,
        acknowledged_ref: reference,
        acknowledged_at: when ? new Date(when).toISOString() : undefined,
        acknowledged_by_name: who,
        notes,
      });
      setReference('');
      setWhen('');
      setWho('');
      setNotes('');
      onDone();
      onClose();
    } catch (error) {
      setBanner(errorMessage(error));
    }
  }

  return (
    <Sheet
      open={Boolean(gateOut)}
      title={`Acknowledge ${gateOut?.number ?? ''}`}
      onClose={onClose}
      footer={
        <>
          <Button variant="secondary" block onClick={onClose}>
            Cancel
          </Button>
          <Button
            block
            disabled={!reference.trim()}
            loading={record.isPending}
            onClick={() => void save()}
          >
            Record it
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        {banner ? <Banner tone="error">{banner}</Banner> : null}

        <Banner tone="info">
          Recording this ends our liability for that material on the record.
          It cannot be undone, so use their actual reference.
        </Banner>

        <Field
          label="Their reference"
          htmlFor="ack-ref"
          hint="A goods-received number, a delivery-note stamp, an email subject — whatever they gave you."
        >
          <Input
            id="ack-ref"
            value={reference}
            onChange={(event) => setReference(event.target.value)}
          />
        </Field>

        <Field
          label="When"
          htmlFor="ack-when"
          hint="Leave blank for now. Backdate it to when they actually signed."
        >
          <Input
            id="ack-when"
            type="datetime-local"
            value={when}
            onChange={(event) => setWhen(event.target.value)}
          />
        </Field>

        <Field label="Who signed" htmlFor="ack-who">
          <Input id="ack-who" value={who} onChange={(event) => setWho(event.target.value)} />
        </Field>

        <Field label="Note" htmlFor="ack-notes">
          <Textarea
            id="ack-notes"
            value={notes}
            onChange={(event) => setNotes(event.target.value)}
          />
        </Field>
      </div>
    </Sheet>
  );
}
