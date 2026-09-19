/**
 * T6.6 — quarantine and its decisions (design §7.4; J1, J2).
 *
 * The criterion: "a storekeeper moves a faulty unit **from quarantine to a
 * client return** and records the acknowledgement." So this screen's job is to
 * make the decision the easy thing to do, because J2 exists for the opposite
 * reason — quarantine becomes a graveyard when deciding is harder than ignoring.
 *
 * Two things follow from that:
 *
 * **Age leads.** Every row says how long it has been sitting there, oldest
 * first. A list sorted by item name would let a six-week-old radio hide behind
 * yesterday's arrivals, which is exactly how the graveyard forms.
 *
 * **The four decisions are on the row.** Not behind a "new disposition" form
 * that asks which location and which lines — the storekeeper is looking at the
 * thing; the only real question is what happens to it. What each choice *means*
 * for the stock is written next to it, because "restore" and "scrap" differ in
 * ways an operator audit will ask about.
 */

import { useState } from 'react';
import { Link } from 'react-router-dom';

import { errorMessage, useAction, useList, useResource } from '../../api/hooks';
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
import { EmptyState, PageHeader, Sheet, Stat, StatusBadge } from '../../components/ui/data';
import { SearchField } from '../../components/ui/SearchField';
import type {
  Disposition,
  DispositionDecision,
  QuarantineItem,
} from './types';

/** J2's four outcomes, in the words a storekeeper would use, with consequences. */
const DECISIONS: {
  value: DispositionDecision;
  label: string;
  effect: string;
}[] = [
  {
    value: 'RESTORE_TO_SERVICEABLE',
    label: 'It is fine — back to stock',
    effect: 'Goes back into the yard as serviceable and can be issued again.',
  },
  {
    value: 'REPAIR',
    label: 'Send it for repair',
    effect: 'Leaves the yard to the vendor. Still ours, still on the books.',
  },
  {
    value: 'RETURN_TO_CLIENT',
    label: 'Send it back to the client',
    effect: 'Records the decision. It goes back on a gate pass they sign for.',
  },
  {
    value: 'SCRAP',
    label: 'Scrap it',
    effect: 'Records the decision. Destroying it is a separate approved disposal.',
  },
];

interface LocationRow {
  id: number;
  name: string;
  type: string;
}

export default function QuarantinePage() {
  const [search, setSearch] = useState('');
  const quarantine = useResource<{ count: number; items: QuarantineItem[] }>(
    'quarantine',
    search ? { search } : undefined,
  );
  const [deciding, setDeciding] = useState<QuarantineItem | null>(null);

  const items = quarantine.data?.items ?? [];
  const oldest = items[0]?.days_in_quarantine ?? 0;
  const clientOwned = items.filter((item) => item.owner_client_id).length;

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Quarantine"
        subtitle="Faulty, damaged and scrap material. None of it is issuable."
        actions={
          <Link
            to="/quarantine/decisions"
            className="flex min-h-[44px] items-center rounded-lg border border-slate-300 px-4 text-sm font-medium text-slate-800"
          >
            Decisions
          </Link>
        }
      />

      <SearchField
        value={search}
        onChange={setSearch}
        label="Search quarantine"
        placeholder="Item, client or bay"
      />

      {quarantine.isError ? (
        <Banner tone="error">{errorMessage(quarantine.error)}</Banner>
      ) : null}

      <div className="grid grid-cols-3 gap-2">
        <Stat label="Lines" value={quarantine.data?.count ?? '—'} />
        <Stat
          label="Oldest"
          value={oldest ? `${oldest}d` : '—'}
          tone={oldest > 30 ? 'bad' : oldest > 14 ? 'warn' : 'neutral'}
          hint="Nothing should sit here indefinitely."
        />
        <Stat
          label="Client-owned"
          value={clientOwned}
          tone={clientOwned > 0 ? 'warn' : 'neutral'}
          hint="Theirs to decide about, not ours."
        />
      </div>

      {quarantine.isLoading ? (
        <Spinner className="text-slate-400" />
      ) : items.length === 0 ? (
        <EmptyState
          title="Quarantine is empty."
          hint="Faulty and damaged material lands here on receipt."
        />
      ) : (
        <ul className="flex flex-col gap-2">
          {items.map((item) => (
            <li key={`${item.balance_id}`}>
              <Card className="flex flex-col gap-2">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div className="min-w-0">
                    <p className="text-sm font-semibold text-slate-900">{item.item_type}</p>
                    <p className="text-sm text-slate-600">
                      {item.quantity} {item.uom} ·{' '}
                      {item.condition.toLowerCase().replaceAll('_', ' ')} · {item.location}
                    </p>
                  </div>
                  <div className="flex shrink-0 flex-col items-end gap-1">
                    <OwnershipBadge client={item.owner_client || null} />
                    <span
                      className={
                        (item.days_in_quarantine ?? 0) > 30
                          ? 'text-xs font-semibold text-red-700'
                          : 'text-xs text-slate-500'
                      }
                    >
                      {item.days_in_quarantine ?? 0} days here
                    </span>
                  </div>
                </div>

                <Button variant="secondary" block onClick={() => setDeciding(item)}>
                  Decide what happens to it
                </Button>
              </Card>
            </li>
          ))}
        </ul>
      )}

      <DecideSheet
        item={deciding}
        onClose={() => setDeciding(null)}
        onDone={() => void quarantine.refetch()}
      />
    </div>
  );
}

/** Raise, submit and (where it auto-approves) post a disposition in one go (J2). */
function DecideSheet({
  item,
  onClose,
  onDone,
}: {
  item: QuarantineItem | null;
  onClose: () => void;
  onDone: () => void;
}) {
  const [decision, setDecision] = useState<DispositionDecision>('RESTORE_TO_SERVICEABLE');
  const [reason, setReason] = useState('');
  const [quantity, setQuantity] = useState('');
  const [toLocation, setToLocation] = useState('');
  const [vendor, setVendor] = useState('');
  const [banner, setBanner] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<string | null>(null);

  const locations = useList<LocationRow>('locations', { page_size: 200 });
  const create = useAction<Record<string, unknown>, Disposition>({
    resource: 'dispositions',
    invalidates: ['dispositions', 'quarantine'],
  });
  const submit = useAction<{ id: number }, Disposition>({
    resource: 'dispositions',
    path: (body) => `${body.id}/submit`,
    invalidates: ['dispositions', 'quarantine'],
  });
  const post = useAction<{ id: number }, Disposition>({
    resource: 'dispositions',
    path: (body) => `${body.id}/post`,
    // Posting moves material, so every stock screen is stale.
    invalidates: ['dispositions', 'quarantine', 'stock', 'stock/custody', 'serials', 'drums'],
  });

  const chosen = DECISIONS.find((option) => option.value === decision);
  const yards = (locations.data?.results ?? []).filter((row) => row.type === 'YARD');

  async function go() {
    if (!item) return;
    setBanner(null);
    setOutcome(null);
    try {
      const created = await create.mutateAsync({
        decision,
        reason,
        from_location: item.location_id,
        to_location: decision === 'RESTORE_TO_SERVICEABLE' ? Number(toLocation) : null,
        vendor_name: decision === 'REPAIR' ? vendor : '',
        lines: [
          {
            item_type: item.item_type_id,
            quantity: quantity || item.quantity,
            uom: item.uom,
            condition: item.condition,
            // J2: restoring is the one decision that changes condition, and that
            // change is what makes the material issuable again.
            to_condition:
              decision === 'RESTORE_TO_SERVICEABLE' ? 'USED_SERVICEABLE' : '',
            owner_type: item.owner_client_id ? 'CLIENT' : 'OWN',
            owner_client: item.owner_client_id,
          },
        ],
      });

      const submitted = await submit.mutateAsync({ id: created.id });

      if (submitted.status === 'APPROVED') {
        const posted = await post.mutateAsync({ id: created.id });
        setOutcome(
          decision === 'RESTORE_TO_SERVICEABLE'
            ? `${posted.number} posted — it is back in stock.`
            : decision === 'REPAIR'
              ? `${posted.number} posted — it is with ${vendor}.`
              : decision === 'RETURN_TO_CLIENT'
                ? `${posted.number} recorded. Raise the return gate pass next.`
                : `${posted.number} recorded. Raise a disposal to destroy it.`,
        );
      } else {
        // §5.2: client-owned material always escalates, whatever the rules say.
        setOutcome(
          `${submitted.number} is waiting on approval` +
            (submitted.pending_approval?.role
              ? ` from ${submitted.pending_approval.role}.`
              : '.'),
        );
      }
      onDone();
    } catch (error) {
      setBanner(errorMessage(error));
    }
  }

  const busy = create.isPending || submit.isPending || post.isPending;
  const ready =
    Boolean(reason.trim()) &&
    (decision !== 'RESTORE_TO_SERVICEABLE' || Boolean(toLocation)) &&
    (decision !== 'REPAIR' || Boolean(vendor.trim()));

  return (
    <Sheet
      open={Boolean(item)}
      title={item ? item.item_type : ''}
      onClose={() => {
        setOutcome(null);
        onClose();
      }}
      footer={
        outcome ? (
          <Button
            block
            onClick={() => {
              setOutcome(null);
              onClose();
            }}
          >
            Done
          </Button>
        ) : (
          <>
            <Button variant="secondary" block onClick={onClose}>
              Cancel
            </Button>
            <Button block disabled={!ready} loading={busy} onClick={() => void go()}>
              Record the decision
            </Button>
          </>
        )
      }
    >
      <div className="flex flex-col gap-3">
        {banner ? <Banner tone="error">{banner}</Banner> : null}
        {outcome ? <Banner tone="success">{outcome}</Banner> : null}

        {!outcome ? (
          <>
            {item?.owner_client_id ? (
              <Banner tone="warning">
                This is {item.owner_client}&rsquo;s property. Whatever is decided
                needs their sign-off as well as ours.
              </Banner>
            ) : null}

            <Field label="What happens to it" htmlFor="decision" hint={chosen?.effect}>
              <Select
                id="decision"
                value={decision}
                onChange={(event) =>
                  setDecision(event.target.value as DispositionDecision)
                }
              >
                {DECISIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </Select>
            </Field>

            <Field
              label={`How much (${item?.uom ?? ''})`}
              htmlFor="qty"
              hint={`${item?.quantity ?? ''} ${item?.uom ?? ''} in quarantine. Leave blank for all of it.`}
            >
              <Input
                id="qty"
                type="number"
                inputMode="decimal"
                min="0"
                step="0.001"
                max={item?.quantity}
                value={quantity}
                onChange={(event) => setQuantity(event.target.value)}
              />
            </Field>

            {decision === 'RESTORE_TO_SERVICEABLE' ? (
              <Field label="Back to which yard" htmlFor="to-location">
                <Select
                  id="to-location"
                  value={toLocation}
                  onChange={(event) => setToLocation(event.target.value)}
                >
                  <option value="">Choose…</option>
                  {yards.map((row) => (
                    <option key={row.id} value={row.id}>
                      {row.name}
                    </option>
                  ))}
                </Select>
              </Field>
            ) : null}

            {decision === 'REPAIR' ? (
              <Field
                label="Who is repairing it"
                htmlFor="vendor"
                hint="Named, so somebody can chase it."
              >
                <Input
                  id="vendor"
                  value={vendor}
                  onChange={(event) => setVendor(event.target.value)}
                />
              </Field>
            ) : null}

            <Field
              label="Why"
              htmlFor="reason"
              hint="The reason is part of the decision. “Bench-tested and passed” is an answer; “ok” is not."
            >
              <Textarea
                id="reason"
                value={reason}
                onChange={(event) => setReason(event.target.value)}
              />
            </Field>
          </>
        ) : null}
      </div>
    </Sheet>
  );
}

/** The decisions already taken, and what is waiting on approval (J2, §4.11). */
export function DispositionsPage() {
  const dispositions = useList<Disposition>('dispositions', { page_size: 50 });
  const post = useAction<{ id: number }>({
    resource: 'dispositions',
    path: (body) => `${body.id}/post`,
    invalidates: ['dispositions', 'quarantine', 'stock'],
  });
  const [banner, setBanner] = useState<string | null>(null);

  const rows = dispositions.data?.results ?? [];

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Quarantine decisions"
        subtitle="What has been decided, and what is still waiting on a sign-off."
        actions={
          <Link
            to="/quarantine"
            className="flex min-h-[44px] items-center px-2 text-sm text-slate-700"
          >
            Quarantine
          </Link>
        }
      />

      {banner ? <Banner tone="error">{banner}</Banner> : null}
      {dispositions.isError ? (
        <Banner tone="error">{errorMessage(dispositions.error)}</Banner>
      ) : null}

      {dispositions.isLoading ? (
        <Spinner className="text-slate-400" />
      ) : rows.length === 0 ? (
        <EmptyState title="Nothing decided yet." />
      ) : (
        <ul className="flex flex-col gap-2">
          {rows.map((row) => (
            <li key={row.id}>
              <Card className="flex flex-col gap-2">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div className="min-w-0">
                    <p className="text-sm font-semibold text-slate-900">
                      {row.number || 'Draft'} ·{' '}
                      {DECISIONS.find((option) => option.value === row.decision)?.label ??
                        row.decision}
                    </p>
                    <p className="text-sm text-slate-600">{row.reason}</p>
                    <p className="text-xs text-slate-500">
                      {row.lines.length} line{row.lines.length === 1 ? '' : 's'} from{' '}
                      {row.from_location_name}
                      {row.vendor_name ? ` · ${row.vendor_name}` : ''}
                    </p>
                  </div>
                  <StatusBadge status={row.status} />
                </div>

                {row.involves_client_owned_material ? (
                  <p className="text-sm text-amber-700">
                    Client-owned material — approval is not optional here.
                  </p>
                ) : null}

                {row.status === 'APPROVED' ? (
                  <Button
                    loading={post.isPending}
                    onClick={async () => {
                      setBanner(null);
                      try {
                        await post.mutateAsync({ id: row.id });
                      } catch (error) {
                        setBanner(errorMessage(error));
                      }
                    }}
                  >
                    Action it
                  </Button>
                ) : null}

                {row.status === 'PENDING_APPROVAL' && row.pending_approval?.role ? (
                  <p className="text-sm text-slate-600">
                    Waiting on {row.pending_approval.role}.
                  </p>
                ) : null}

                {row.reject_reason ? (
                  <Banner tone="warning">Rejected: {row.reject_reason}</Banner>
                ) : null}
              </Card>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
