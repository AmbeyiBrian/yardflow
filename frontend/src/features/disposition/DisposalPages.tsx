/**
 * T6.6 — disposal, with its approval status (design §7.4, §4.11; J3).
 *
 * J3: "disposal of scrap requires approval and produces a disposal record, **so
 * that write-offs are controlled and auditable**." Both halves are on this
 * screen: the approval state of every disposal, and the certificate.
 *
 * The screen never hides the approval. A "Dispose" button that sometimes needs a
 * sign-off and sometimes does not teaches people that approval is noise; showing
 * what is waiting on whom, on every row, teaches the opposite. And for
 * client-owned material it says outright that no configuration can switch the
 * approval off (§5.2) — because the storekeeper who reads that once will not
 * raise the same request again next week expecting it to slip through.
 */

import { useState } from 'react';

import { openDocument } from '../../api/client';
import { errorMessage, useAction, useList, useResource } from '../../api/hooks';
import { PERM } from '../../auth/permissions';
import { useSession } from '../../auth/session';
import {
  Banner,
  Button,
  Card,
  Field,
  Input,
  OwnershipBadge,
  Select,
  Spinner,
  Textarea,
} from '../../components/ui';
import { EmptyState, PageHeader, Sheet, StatusBadge } from '../../components/ui/data';
import { SearchField } from '../../components/ui/SearchField';
import type { Disposal, DisposalMethod, QuarantineItem } from './types';

const METHODS: { value: DisposalMethod; label: string }[] = [
  { value: 'LICENSED_HANDLER', label: 'Collected by a licensed e-waste handler' },
  { value: 'SCRAP_DEALER', label: 'Sold or given to a scrap dealer' },
  { value: 'DESTROYED_ON_SITE', label: 'Destroyed on our premises' },
  { value: 'RETURNED_TO_SUPPLIER', label: 'Returned to the supplier' },
  { value: 'OTHER', label: 'Other — see the note' },
];

export default function DisposalsPage() {
  const { has } = useSession();
  const mayApprove = has(PERM.DISPOSAL_APPROVE);

  const [search, setSearch] = useState('');
  const disposals = useList<Disposal>('disposals', {
    search: search || undefined,
    page_size: 50,
  });
  const [raising, setRaising] = useState(false);
  const [banner, setBanner] = useState<string | null>(null);
  const [rejecting, setRejecting] = useState<Disposal | null>(null);

  const invalidates = ['disposals', 'quarantine', 'stock', 'notifications'];
  const submit = useAction<{ id: number }>({
    resource: 'disposals',
    path: (body) => `${body.id}/submit`,
    invalidates,
  });
  const approve = useAction<{ id: number }>({
    resource: 'disposals',
    path: (body) => `${body.id}/approve`,
    invalidates,
  });
  const post = useAction<{ id: number }>({
    resource: 'disposals',
    path: (body) => `${body.id}/post`,
    invalidates,
  });

  const rows = disposals.data?.results ?? [];

  async function run(what: () => Promise<unknown>) {
    setBanner(null);
    try {
      await what();
    } catch (error) {
      setBanner(errorMessage(error));
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Disposals"
        subtitle="Material leaving the books for good. Every one is approved and certificated."
        actions={<Button onClick={() => setRaising(true)}>Raise a disposal</Button>}
      />

      <SearchField
        value={search}
        onChange={setSearch}
        label="Search disposals"
        placeholder="Number, handler or note"
      />

      {banner ? <Banner tone="error">{banner}</Banner> : null}
      {disposals.isError ? (
        <Banner tone="error">{errorMessage(disposals.error)}</Banner>
      ) : null}

      {disposals.isLoading ? (
        <Spinner className="text-slate-400" />
      ) : rows.length === 0 ? (
        <EmptyState
          title="Nothing has been written off."
          hint="Scrap reaches here once a quarantine decision says so."
        />
      ) : (
        <ul className="flex flex-col gap-2">
          {rows.map((row) => (
            <li key={row.id}>
              <Card className="flex flex-col gap-2">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div className="min-w-0">
                    <p className="text-sm font-semibold text-slate-900">
                      {row.number || 'Draft'} ·{' '}
                      {METHODS.find((option) => option.value === row.method)?.label ??
                        row.method}
                    </p>
                    <p className="text-sm text-slate-600">
                      {row.lines.length} line{row.lines.length === 1 ? '' : 's'} from{' '}
                      {row.from_location_name}
                      {row.handler_name ? ` · ${row.handler_name}` : ''}
                      {row.handler_reference ? ` (${row.handler_reference})` : ''}
                    </p>
                    {row.written_off_total ? (
                      <p className="text-sm text-slate-600">
                        Value written off: {row.written_off_total}
                      </p>
                    ) : null}
                  </div>
                  <StatusBadge status={row.status} />
                </div>

                <ul className="text-sm text-slate-700">
                  {row.lines.map((line, index) => (
                    <li key={line.id ?? index} className="flex items-center gap-2">
                      <span>
                        {line.quantity} {line.uom} {line.item_name}
                      </span>
                      <OwnershipBadge client={line.owner_client_name || null} />
                    </li>
                  ))}
                </ul>

                {row.involves_client_owned_material ? (
                  <Banner tone="warning">
                    This writes off a client&rsquo;s property. §5.2 requires a
                    sign-off for that and no setting can turn it off.
                  </Banner>
                ) : null}

                {row.status === 'PENDING_APPROVAL' ? (
                  <p className="text-sm text-slate-600">
                    Waiting on {row.pending_approval?.role ?? 'an approver'}.
                  </p>
                ) : null}

                <div className="flex flex-wrap gap-2">
                  {row.status === 'DRAFT' ? (
                    <Button
                      loading={submit.isPending}
                      onClick={() => void run(() => submit.mutateAsync({ id: row.id }))}
                    >
                      Send for approval
                    </Button>
                  ) : null}

                  {row.status === 'PENDING_APPROVAL' && mayApprove ? (
                    <>
                      <Button
                        loading={approve.isPending}
                        onClick={() => void run(() => approve.mutateAsync({ id: row.id }))}
                      >
                        Approve
                      </Button>
                      <Button variant="danger" onClick={() => setRejecting(row)}>
                        Reject
                      </Button>
                    </>
                  ) : null}

                  {row.status === 'APPROVED' ? (
                    <Button
                      loading={post.isPending}
                      onClick={() => void run(() => post.mutateAsync({ id: row.id }))}
                    >
                      Confirm it is gone
                    </Button>
                  ) : null}

                  {/* J3: the certificate is available before completion too —
                      whoever hands material to a licensed handler needs the
                      paperwork when the lorry arrives, not after.

                      Fetched rather than linked, because the token is in memory
                      and a plain href would 401. */}
                  <Button
                    variant="secondary"
                    onClick={() =>
                      void run(() => openDocument(`/disposals/${row.id}/certificate`))
                    }
                  >
                    Certificate
                  </Button>
                </div>

                {row.reject_reason ? (
                  <Banner tone="warning">Rejected: {row.reject_reason}</Banner>
                ) : null}
              </Card>
            </li>
          ))}
        </ul>
      )}

      <RaiseDisposalSheet
        open={raising}
        onClose={() => setRaising(false)}
        onDone={() => void disposals.refetch()}
      />

      <RejectSheet
        disposal={rejecting}
        onClose={() => setRejecting(null)}
        onDone={() => void disposals.refetch()}
      />
    </div>
  );
}

/** Lines are picked from quarantine, because that is where scrap actually is. */
function RaiseDisposalSheet({
  open,
  onClose,
  onDone,
}: {
  open: boolean;
  onClose: () => void;
  onDone: () => void;
}) {
  const quarantine = useResource<{ items: QuarantineItem[] }>('quarantine', undefined, {
    enabled: open,
  });
  const [method, setMethod] = useState<DisposalMethod>('LICENSED_HANDLER');
  const [handler, setHandler] = useState('');
  const [reference, setReference] = useState('');
  const [notes, setNotes] = useState('');
  const [picked, setPicked] = useState<Record<number, string>>({});
  const [banner, setBanner] = useState<string | null>(null);

  const create = useAction<Record<string, unknown>, Disposal>({
    resource: 'disposals',
    invalidates: ['disposals'],
  });

  const items = quarantine.data?.items ?? [];
  const chosen = items.filter((item) => picked[item.balance_id]);

  async function raise() {
    setBanner(null);
    if (chosen.length === 0) {
      setBanner('Pick at least one thing to write off.');
      return;
    }
    // Everything on one disposal has to come out of one place, because the
    // document records where the material left from.
    const from = chosen[0].location_id;
    if (chosen.some((item) => item.location_id !== from)) {
      setBanner('One disposal covers one quarantine location. Raise a second for the other.');
      return;
    }

    try {
      await create.mutateAsync({
        method,
        from_location: from,
        handler_name: handler,
        handler_reference: reference,
        notes,
        lines: chosen.map((item) => ({
          item_type: item.item_type_id,
          quantity: picked[item.balance_id] || item.quantity,
          uom: item.uom,
          condition: item.condition,
          owner_type: item.owner_client_id ? 'CLIENT' : 'OWN',
          owner_client: item.owner_client_id,
        })),
      });
      setPicked({});
      setHandler('');
      setReference('');
      setNotes('');
      onDone();
      onClose();
    } catch (error) {
      setBanner(errorMessage(error));
    }
  }

  return (
    <Sheet
      open={open}
      title="Raise a disposal"
      onClose={onClose}
      footer={
        <>
          <Button variant="secondary" block onClick={onClose}>
            Cancel
          </Button>
          <Button block loading={create.isPending} onClick={() => void raise()}>
            Raise it
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        {banner ? <Banner tone="error">{banner}</Banner> : null}

        <Banner tone="info">
          Nothing moves until this is approved and confirmed. A disposal is the
          one movement that cannot be reversed.
        </Banner>

        <Field label="How it is being got rid of" htmlFor="method">
          <Select
            id="method"
            value={method}
            onChange={(event) => setMethod(event.target.value as DisposalMethod)}
          >
            {METHODS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </Select>
        </Field>

        <Field
          label="Who is taking it"
          htmlFor="handler"
          hint="Their name and reference are what makes the certificate checkable from outside."
        >
          <Input
            id="handler"
            value={handler}
            onChange={(event) => setHandler(event.target.value)}
          />
        </Field>

        <Field label="Their reference" htmlFor="handler-ref">
          <Input
            id="handler-ref"
            value={reference}
            onChange={(event) => setReference(event.target.value)}
          />
        </Field>

        <div className="flex flex-col gap-2">
          <p className="text-sm font-medium text-slate-700">What is being written off</p>
          {quarantine.isLoading ? (
            <Spinner className="text-slate-400" />
          ) : items.length === 0 ? (
            <p className="text-sm text-slate-500">Quarantine is empty.</p>
          ) : (
            items.map((item) => (
              <Field
                key={item.balance_id}
                label={`${item.item_type} — ${item.quantity} ${item.uom} in ${item.location}`}
                htmlFor={`pick-${item.balance_id}`}
                hint={
                  item.owner_client
                    ? `${item.owner_client}'s property — this will need their sign-off.`
                    : undefined
                }
              >
                <Input
                  id={`pick-${item.balance_id}`}
                  type="number"
                  inputMode="decimal"
                  min="0"
                  step="0.001"
                  max={item.quantity}
                  value={picked[item.balance_id] ?? ''}
                  onChange={(event) =>
                    setPicked((current) => {
                      const next = { ...current };
                      if (event.target.value && Number(event.target.value) > 0) {
                        next[item.balance_id] = event.target.value;
                      } else {
                        delete next[item.balance_id];
                      }
                      return next;
                    })
                  }
                />
              </Field>
            ))
          )}
        </div>

        <Field label="Note" htmlFor="disposal-notes">
          <Textarea
            id="disposal-notes"
            value={notes}
            onChange={(event) => setNotes(event.target.value)}
          />
        </Field>
      </div>
    </Sheet>
  );
}

function RejectSheet({
  disposal,
  onClose,
  onDone,
}: {
  disposal: Disposal | null;
  onClose: () => void;
  onDone: () => void;
}) {
  const [reason, setReason] = useState('');
  const [banner, setBanner] = useState<string | null>(null);
  const reject = useAction<{ id: number; reason: string }>({
    resource: 'disposals',
    path: (body) => `${body.id}/reject`,
    invalidates: ['disposals', 'notifications'],
  });

  return (
    <Sheet
      open={Boolean(disposal)}
      title={`Reject ${disposal?.number ?? 'this disposal'}`}
      onClose={onClose}
      footer={
        <>
          <Button variant="secondary" block onClick={onClose}>
            Back
          </Button>
          <Button
            variant="danger"
            block
            disabled={!reason.trim()}
            loading={reject.isPending}
            onClick={async () => {
              if (!disposal) return;
              setBanner(null);
              try {
                await reject.mutateAsync({ id: disposal.id, reason });
                setReason('');
                onDone();
                onClose();
              } catch (error) {
                setBanner(errorMessage(error));
              }
            }}
          >
            Reject
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        {banner ? <Banner tone="error">{banner}</Banner> : null}
        <p className="text-sm text-slate-700">
          The material stays in quarantine and whoever raised this sees the
          reason — which is what lets them fix it rather than ask around.
        </p>
        <Field label="Why" htmlFor="reject-reason">
          <Textarea
            id="reject-reason"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
          />
        </Field>
      </div>
    </Sheet>
  );
}
