/**
 * T5.10 — my custody, acknowledge and hand over (design §7.4, §4.10; I1, I5, I3).
 *
 * "What am I holding?" is the question that decides whether a technician trusts
 * the system. If this screen is wrong, nothing else matters — so it reads the
 * ledger's PERSON node (§4.10) rather than a custody table of its own, which is
 * why it and the stock screens cannot disagree.
 *
 * Three things live together here because they are the same conversation:
 *
 *  - **what I hold**, with what is overdue called out (I3)
 *  - **handovers waiting on me**, because I5 makes a transfer real only when the
 *    receiver accepts — an unacknowledged handover that nobody sees is how a
 *    tool goes missing while each of two people believes the other has it
 *  - **handing something over**, from the same list, so the item is picked rather
 *    than typed
 */

import { useMemo, useState } from 'react';

import { errorMessage, useAction, useList, useResource } from '../../api/hooks';
import { PERM } from '../../auth/permissions';
import { useSession } from '../../auth/session';
import {
  Banner,
  Button,
  Card,
  Checkbox,
  Field,
  Input,
  OwnershipBadge,
  Select,
  Spinner,
  Textarea,
} from '../../components/ui';
import { EmptyState, PageHeader, Sheet, StatusBadge } from '../../components/ui/data';
import { type Carried, carriedLabel, mergeCarried } from './carried';
import type {
  CustodyBalance,
  CustodyExpectation,
  CustodyTransfer,
  CustodyTransferLine,
  Reel,
  SerialUnit,
} from './types';

interface UserRow {
  id: number;
  full_name: string;
  email: string | null;
}

export default function MyCustodyPage() {
  const { user, has } = useSession();
  // I5 gates *raising* a handover on custody.transfer, while accepting one is
  // deliberately ungated — the receiver has to be able to answer either way. So
  // the button follows the permission and the accept buttons below do not.
  const mayHandOver = has(PERM.CUSTODY_TRANSFER);

  const balances = useList<CustodyBalance>('stock/custody', { holder: user?.id });
  const serials = useList<SerialUnit>('serials', { holder: 'me', page_size: 100 });
  const drums = useList<Reel>('drums', { holder: 'me', page_size: 100 });
  const expectations = useList<CustodyExpectation>('custody-expectations', {
    holder: user?.id,
    page_size: 100,
  });
  // Handovers in both directions: what is waiting on me to accept, and what I
  // have pushed at somebody else and they have not.
  const transfers = useList<CustodyTransfer>('custody-transfers', {
    status: 'PENDING',
    page_size: 50,
  });

  const [handingOver, setHandingOver] = useState(false);
  const [banner, setBanner] = useState<string | null>(null);

  const acknowledge = useAction<{ id: number }>({
    resource: 'custody-transfers',
    path: (body) => `${body.id}/acknowledge`,
    invalidates: ['custody-transfers', 'stock/custody', 'serials', 'drums', 'notifications'],
  });
  const decline = useAction<{ id: number; reason: string }>({
    resource: 'custody-transfers',
    path: (body) => `${body.id}/decline`,
    invalidates: ['custody-transfers', 'notifications'],
  });

  // One merged list: identified items keep their serial or drum number, and a
  // cut length of cable — which has neither — still appears. See `carried.ts`.
  const rows = mergeCarried({
    balances: balances.data?.results ?? [],
    serials: serials.data?.results ?? [],
    drums: drums.data?.results ?? [],
  });
  const incoming = (transfers.data?.results ?? []).filter(
    (transfer) => transfer.to_holder === user?.id,
  );
  const outgoing = (transfers.data?.results ?? []).filter(
    (transfer) => transfer.from_holder === user?.id,
  );
  const overdue = (expectations.data?.results ?? []).filter(
    (expectation) => expectation.status === 'OVERDUE',
  );

  async function decideOn(transfer: CustodyTransfer, accept: boolean) {
    setBanner(null);
    try {
      if (accept) await acknowledge.mutateAsync({ id: transfer.id });
      else
        await decline.mutateAsync({
          id: transfer.id,
          reason: 'Not received.',
        });
    } catch (error) {
      setBanner(errorMessage(error));
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="What I'm carrying"
        subtitle="Read from the ledger, so it matches what the yard sees."
        actions={
          mayHandOver ? (
            <Button onClick={() => setHandingOver(true)} disabled={rows.length === 0}>
              Hand something over
            </Button>
          ) : undefined
        }
      />

      {banner ? <Banner tone="error">{banner}</Banner> : null}

      {/* I5 first: something is waiting on this person, and until they answer
          the material is still with whoever is giving it away. */}
      {incoming.length > 0 ? (
        <Card className="flex flex-col gap-3 border-sky-300">
          <h2 className="text-sm font-semibold text-slate-900">
            {incoming.length} handover{incoming.length === 1 ? '' : 's'} waiting on you
          </h2>
          {incoming.map((transfer) => (
            <div key={transfer.id} className="flex flex-col gap-2 border-t border-slate-100 pt-2 first:border-0 first:pt-0">
              <p className="text-sm text-slate-700">
                <span className="font-medium text-slate-900">{transfer.from_holder_name}</span>{' '}
                wants to hand you {transfer.lines.length} item
                {transfer.lines.length === 1 ? '' : 's'}
                {/* A pending handover has no number yet: §4.13 allocates one
                    when it is acknowledged, so that gap-free numbering counts
                    real handovers rather than abandoned requests. */}
                {transfer.number ? ` (${transfer.number})` : ''}.
              </p>
              <ul className="text-sm text-slate-600">
                {transfer.lines.map((line, index) => (
                  <li key={line.id ?? index}>
                    {line.quantity} {line.uom} {line.item_name}
                  </li>
                ))}
              </ul>
              {transfer.notes ? (
                <p className="text-sm text-slate-500">“{transfer.notes}”</p>
              ) : null}
              <div className="flex gap-2">
                <Button
                  block
                  loading={acknowledge.isPending}
                  onClick={() => void decideOn(transfer, true)}
                >
                  I have it
                </Button>
                <Button variant="danger" block onClick={() => void decideOn(transfer, false)}>
                  I don't
                </Button>
              </div>
            </div>
          ))}
        </Card>
      ) : null}

      {overdue.length > 0 ? (
        <Banner tone="warning">
          {overdue.length} item{overdue.length === 1 ? ' is' : 's are'} past the date
          it was due back. Bring it to the yard, or hand it to whoever needs
          it next.
        </Banner>
      ) : null}

      {balances.isLoading ? (
        <Spinner className="text-slate-400" />
      ) : rows.length === 0 ? (
        <EmptyState
          title="You are not holding anything."
          hint="Material lands here when a gate pass is released to you."
        />
      ) : (
        <Card className="flex flex-col gap-2">
          <ul className="flex flex-col gap-2">
            {rows.map((item) => (
              <li key={item.key} className="flex items-center justify-between gap-3">
                <span className="min-w-0">
                  <span className="block truncate text-sm font-medium text-slate-900">
                    {item.itemName}
                  </span>
                  <span className="block text-xs text-slate-500">
                    {item.reference ? (
                      <span className="font-mono">{item.reference}</span>
                    ) : (
                      // A cut length has no label to read off, so say what it is
                      // rather than leaving a blank line (§3.1).
                      'loose'
                    )}
                    {item.condition
                      ? ` · ${item.condition.toLowerCase().replaceAll('_', ' ')}`
                      : ''}
                  </span>
                </span>
                <span className="flex shrink-0 items-center gap-2">
                  <OwnershipBadge client={item.ownerClient || null} />
                  <span className="text-sm font-semibold text-slate-900">
                    {item.available} {item.uom}
                  </span>
                </span>
              </li>
            ))}
          </ul>
        </Card>
      )}

      {outgoing.length > 0 ? (
        <Card className="flex flex-col gap-2">
          <h2 className="text-sm font-semibold text-slate-900">Waiting on someone else</h2>
          {outgoing.map((transfer) => (
            <p key={transfer.id} className="text-sm text-slate-600">
              {transfer.number || 'Handover'} to {transfer.to_holder_name} —{' '}
              <StatusBadge status={transfer.status} />
              {' '}
              {/* I5: this is why it still shows above as yours. */}
              still on your record until they accept.
            </p>
          ))}
        </Card>
      ) : null}

      <HandoverSheet
        open={handingOver}
        onClose={() => setHandingOver(false)}
        carried={rows}
      />
    </div>
  );
}

/** I5: a handover is a request, and the receiver's acknowledgement is the move. */
function HandoverSheet({
  open,
  onClose,
  carried,
}: {
  open: boolean;
  onClose: () => void;
  carried: Carried[];
}) {
  const { user } = useSession();
  const people = useResource<{ results: UserRow[] }>('users', { page_size: 200 });
  const [toHolder, setToHolder] = useState('');
  const [notes, setNotes] = useState('');
  const [picked, setPicked] = useState<Record<string, string>>({});
  const [banner, setBanner] = useState<string | null>(null);

  const raise = useAction<Record<string, unknown>>({
    resource: 'custody-transfers',
    invalidates: ['custody-transfers', 'notifications'],
  });

  const options = useMemo(
    () => (people.data?.results ?? []).filter((person) => person.id !== user?.id),
    [people.data, user?.id],
  );

  const lines: CustodyTransferLine[] = carried
    .filter((item) => picked[item.key])
    .map((item) => ({
      item_type: item.itemType,
      serial_unit: item.serialUnit ?? null,
      reel: item.reel ?? null,
      quantity: picked[item.key],
      uom: item.uom,
      condition: item.condition,
    }));

  async function send() {
    setBanner(null);
    try {
      await raise.mutateAsync({ to_holder: Number(toHolder), notes, lines });
      setPicked({});
      setNotes('');
      setToHolder('');
      onClose();
    } catch (error) {
      setBanner(errorMessage(error));
    }
  }

  return (
    <Sheet
      open={open}
      title="Hand something over"
      onClose={onClose}
      footer={
        <>
          <Button variant="secondary" block onClick={onClose}>
            Cancel
          </Button>
          <Button
            block
            disabled={!toHolder || lines.length === 0}
            loading={raise.isPending}
            onClick={() => void send()}
          >
            Ask them to accept
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        {banner ? <Banner tone="error">{banner}</Banner> : null}

        <Banner tone="info">
          It stays on your record until they accept it. They get a
          notification straight away.
        </Banner>

        <Field label="Who is taking it" htmlFor="to-holder">
          <Select
            id="to-holder"
            value={toHolder}
            onChange={(event) => setToHolder(event.target.value)}
          >
            <option value="">Choose a person…</option>
            {options.map((person) => (
              <option key={person.id} value={person.id}>
                {person.full_name}
              </option>
            ))}
          </Select>
        </Field>

        <div className="flex flex-col gap-2">
          <p className="text-sm font-medium text-slate-700">What are you handing over</p>
          {carried.map((item) =>
            // A serialized unit is all or nothing (Q5), so it is a tick. Anything
            // measured takes a quantity, because half a drum is a normal handover.
            item.serialUnit ? (
              <Checkbox
                key={item.key}
                id={`pick-${item.key}`}
                label={carriedLabel(item)}
                checked={Boolean(picked[item.key])}
                onChange={(event) =>
                  setPicked((current) => {
                    const next = { ...current };
                    if (event.target.checked) next[item.key] = '1';
                    else delete next[item.key];
                    return next;
                  })
                }
              />
            ) : (
              <Field
                key={item.key}
                label={`${carriedLabel(item)} — holding ${item.available} ${item.uom}`}
                htmlFor={`pick-${item.key}`}
              >
                <Input
                  id={`pick-${item.key}`}
                  type="number"
                  inputMode="decimal"
                  min="0"
                  max={item.available}
                  step="0.001"
                  value={picked[item.key] ?? ''}
                  onChange={(event) =>
                    setPicked((current) => {
                      const next = { ...current };
                      const quantity = event.target.value;
                      if (quantity && Number(quantity) > 0) next[item.key] = quantity;
                      else delete next[item.key];
                      return next;
                    })
                  }
                />
              </Field>
            ),
          )}
        </div>

        <Field label="Note" htmlFor="handover-notes">
          <Textarea
            id="handover-notes"
            value={notes}
            onChange={(event) => setNotes(event.target.value)}
          />
        </Field>
      </div>
    </Sheet>
  );
}


