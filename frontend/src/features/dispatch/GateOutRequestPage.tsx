/**
 * T4.21 — raising a gate-out request (design §7.3, §7.4; F1, F2, C7).
 *
 * The criterion is a stopwatch: "a technician raises a request from a phone in
 * **under a minute**." Three decisions follow from that and nothing else.
 *
 * *The destination is one choice, not four.* F1 allows a site, a project, a
 * client or a location, and the model enforces exactly one (§4.7). Four separate
 * pickers would mean reading all four to find the one that applies, so this asks
 * "where is it going?" once and then asks for the right thing.
 *
 * *Defaults do the work.* The requester is the custody holder, because a
 * technician taking material for their own job is the ordinary case — and it is
 * still a field, because a storekeeper raising it for someone else is the other
 * ordinary case (Q2).
 *
 * *Lines go in by scanning.* A serialized line is a specific unit (§4.7:
 * "these two RRUs", not "two RRUs"), which is what lets the gate check the load
 * against the pass (G1). Scanning is the fast path; searching is always there.
 */

import { useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';

import { api } from '../../api/client';
import { errorMessage, useAction, useDetail, useList } from '../../api/hooks';
import { useSession } from '../../auth/session';
import { newUuid } from '../../offline/db';
import { BarcodeScanner } from '../../components/BarcodeScanner';
import {
  ActionBar,
  Banner,
  Button,
  Card,
  Field,
  Input,
  OwnershipBadge,
  Select,
  Textarea,
} from '../../components/ui';
import { EmptyState, PageHeader, Sheet } from '../../components/ui/data';
import type { Reel, SerialUnit, StockBalance } from '../receiving/types';
import type { Client, ItemType, Location, Site, Project } from '../settings/types';
import type { GateOut, GateOutLine, GateOutPurpose } from './types';

const DRAFT_KEY = 'yardflow.gate-out.draft';

const PURPOSES: { value: GateOutPurpose; label: string; hint: string }[] = [
  { value: 'INSTALLATION', label: 'Installation at a site', hint: '' },
  { value: 'MAINTENANCE', label: 'Maintenance', hint: '' },
  { value: 'TRANSFER', label: 'Transfer to another location', hint: '' },
  {
    value: 'RETURN_TO_CLIENT',
    label: 'Return to the client',
    hint: 'Leaves available stock but stays our exposure until they acknowledge it.',
  },
  { value: 'DISPOSAL', label: 'Disposal', hint: 'Always needs an approval.' },
  { value: 'LOAN', label: 'Loan', hint: 'Expected back, so it goes on somebody’s record.' },
];

type DestinationKind = 'site' | 'project' | 'client' | 'to_location';

interface Draft {
  purpose_type: GateOutPurpose;
  destination_kind: DestinationKind;
  destination_id: string;
  from_location: string;
  custody_holder: string;
  notes: string;
  lines: GateOutLine[];
  /**
   * This request's identity, sent with it so a second press of the button
   * cannot become a second pass (N2, §8.2). The same reasoning as the gate-in
   * draft: disabling the control while a request is in flight leaves a window
   * the client cannot close, and the server settles it instead.
   */
  client_uuid: string;
}

function emptyDraft(holderId: string): Draft {
  return {
    purpose_type: 'INSTALLATION',
    destination_kind: 'site',
    destination_id: '',
    from_location: '',
    custody_holder: holderId,
    notes: '',
    lines: [],
    client_uuid: newUuid(),
  };
}

export default function GateOutRequestPage() {
  const navigate = useNavigate();
  const { user } = useSession();
  const holderId = user ? String(user.id) : '';
  // Present when an existing draft is being corrected. The same screen, because
  // a correction is the same work as the entry.
  const { id: editingId } = useParams();
  const existing = useDetail<GateOut>('gate-outs', editingId);
  const [loaded, setLoaded] = useState(false);

  const [draft, setDraft] = useState<Draft>(() => {
    if (editingId) return emptyDraft(holderId);
    try {
      const stored = localStorage.getItem(DRAFT_KEY);
      if (stored) {
        const parsed = JSON.parse(stored) as Draft;
        if (parsed?.lines && Array.isArray(parsed.lines)) {
          // A draft stored before this field existed still deserves one.
          return parsed.client_uuid ? parsed : { ...parsed, client_uuid: newUuid() };
        }
      }
    } catch {
      /* a stored shape from an older build must not break the screen */
    }
    return emptyDraft(holderId);
  });
  const [lineSheet, setLineSheet] = useState(false);
  /** Which line is open for correction, or null when a new one is being added. */
  const [editingLine, setEditingLine] = useState<number | null>(null);
  const [banner, setBanner] = useState<string | null>(null);

  // Skipped while editing: that draft lives on the server, and writing it here
  // would overwrite an unsaved request the same person may have in progress.
  useEffect(() => {
    if (editingId) return;
    try {
      localStorage.setItem(DRAFT_KEY, JSON.stringify(draft));
    } catch {
      /* private mode, or the quota is full */
    }
  }, [draft, editingId]);

  // Fill the form from the saved draft, once.
  useEffect(() => {
    if (!editingId || loaded || !existing.data) return;
    const document = existing.data;
    // Exactly one destination is set on the record; which one it is decides
    // what the picker shows.
    const kind: DestinationKind = document.site
      ? 'site'
      : document.project
        ? 'project'
        : document.client
          ? 'client'
          : 'to_location';
    const destinationId =
      document.site ?? document.project ?? document.client ?? document.to_location;

    setDraft({
      purpose_type: document.purpose_type,
      destination_kind: kind,
      destination_id: destinationId ? String(destinationId) : '',
      from_location: document.from_location ? String(document.from_location) : '',
      custody_holder: document.custody_holder ? String(document.custody_holder) : holderId,
      notes: document.notes ?? '',
      lines: document.lines ?? [],
      client_uuid: document.client_uuid ?? newUuid(),
    });
    setLoaded(true);
  }, [editingId, loaded, existing.data, holderId]);

  const locations = useList<Location>('locations', { page_size: 200 });
  const sites = useList<Site>('sites', { page_size: 300 });
  const projects = useList<Project>('projects', { page_size: 200 });
  const clients = useList<Client>('clients', { page_size: 200 });
  const people = useList<{ id: number; full_name: string }>('users', { page_size: 200 });

  const create = useAction<Record<string, unknown>, { id: number }>({
    resource: 'gate-outs',
  });
  const update = useAction<Record<string, unknown>, { id: number }>({
    resource: 'gate-outs',
    method: 'patch',
    path: () => String(editingId),
    invalidates: ['gate-outs'],
  });
  const submit = useAction<{ id: number }, { id: number; number: string; status: string }>({
    resource: 'gate-outs',
    path: (body) => `${body.id}/submit`,
    invalidates: ['gate-outs', 'approvals'],
  });

  const yards = useMemo(
    () => (locations.data?.results ?? []).filter((row) => row.type !== 'QUARANTINE'),
    [locations.data],
  );

  function set(patch: Partial<Draft>) {
    setDraft((current) => ({ ...current, ...patch }));
  }

  /** "Client owned" without the client is half a sentence. */
  function clientName(id: number): string {
    return (clients.data?.results ?? []).find((row) => row.id === id)?.name ?? 'A client';
  }

  function clearDraft() {
    if (editingId) return;
    setDraft(emptyDraft(holderId));
    try {
      localStorage.removeItem(DRAFT_KEY);
    } catch {
      /* nothing to clear */
    }
  }

  function payload() {
    return {
      purpose_type: draft.purpose_type,
      // Exactly one destination — the model refuses more, and refuses none
      // (§4.7). Sending only the chosen one is what makes that easy to satisfy.
      site: draft.destination_kind === 'site' ? Number(draft.destination_id) : null,
      project:
        draft.destination_kind === 'project' ? Number(draft.destination_id) : null,
      client: draft.destination_kind === 'client' ? Number(draft.destination_id) : null,
      to_location:
        draft.destination_kind === 'to_location' ? Number(draft.destination_id) : null,
      from_location: Number(draft.from_location),
      custody_holder: Number(draft.custody_holder),
      notes: draft.notes,
      client_uuid: draft.client_uuid,
      lines: draft.lines,
    };
  }

  async function saveThenSubmit(alsoSubmit: boolean) {
    setBanner(null);
    try {
      const created = editingId
        ? await update.mutateAsync(payload())
        : await create.mutateAsync(payload());
      if (alsoSubmit) {
        const result = await submit.mutateAsync({ id: created.id });
        clearDraft();
        navigate(`/gate-out/${result.id}`);
        return;
      }
      clearDraft();
      navigate(`/gate-out/${created.id}`);
    } catch (error) {
      setBanner(errorMessage(error));
    }
  }

  const destinations: { kind: DestinationKind; label: string; options: { id: number; label: string }[] }[] = [
    {
      kind: 'site',
      label: 'A site',
      options: (sites.data?.results ?? []).map((site) => ({
        id: site.id,
        label: `${site.internal_ref} · ${site.name}`,
      })),
    },
    {
      kind: 'project',
      label: 'A project',
      options: (projects.data?.results ?? []).map((project) => ({
        id: project.id,
        label: project.reference,
      })),
    },
    {
      kind: 'client',
      label: 'Back to a client',
      options: (clients.data?.results ?? []).map((client) => ({
        id: client.id,
        label: client.name,
      })),
    },
    {
      kind: 'to_location',
      label: 'Another of our locations',
      options: yards.map((location) => ({ id: location.id, label: location.name })),
    },
  ];

  const chosen = destinations.find((entry) => entry.kind === draft.destination_kind)!;
  const purpose = PURPOSES.find((entry) => entry.value === draft.purpose_type);
  const canSubmit =
    Boolean(draft.from_location) &&
    Boolean(draft.destination_id) &&
    Boolean(draft.custody_holder) &&
    draft.lines.length > 0;

  return (
    <div className="flex flex-col gap-4 pb-24">
      <PageHeader
        title={editingId ? 'Correct this request' : 'Request material'}
        subtitle={
          editingId
            ? 'It is still a draft, so it authorises nothing yet.'
            : 'What is leaving the yard, where it is going, and who is taking it.'
        }
        actions={
          // "Discard" throws away what is on this device, which a saved draft
          // is not — offering it while editing was a button that did nothing.
          editingId ? (
            <Button variant="ghost" onClick={() => navigate(`/gate-out/${editingId}`)}>
              Cancel
            </Button>
          ) : draft.lines.length > 0 ? (
            <Button variant="ghost" onClick={clearDraft}>
              Discard
            </Button>
          ) : undefined
        }
      />

      {banner ? <Banner tone="error">{banner}</Banner> : null}

      <Card className="flex flex-col gap-3">
        <Field label="What for" htmlFor="go-purpose" hint={purpose?.hint}>
          <Select
            id="go-purpose"
            value={draft.purpose_type}
            onChange={(event) => set({ purpose_type: event.target.value as GateOutPurpose })}
          >
            {PURPOSES.map((entry) => (
              <option key={entry.value} value={entry.value}>
                {entry.label}
              </option>
            ))}
          </Select>
        </Field>

        <Field
          label="Where it is going"
          htmlFor="go-destination-kind"
          hint="A project is optional — a site on its own is fine."
        >
          <Select
            id="go-destination-kind"
            value={draft.destination_kind}
            onChange={(event) =>
              set({
                destination_kind: event.target.value as DestinationKind,
                destination_id: '',
              })
            }
          >
            {destinations.map((entry) => (
              <option key={entry.kind} value={entry.kind}>
                {entry.label}
              </option>
            ))}
          </Select>
        </Field>

        <Field label={chosen.label} htmlFor="go-destination">
          <Select
            id="go-destination"
            value={draft.destination_id}
            onChange={(event) => set({ destination_id: event.target.value })}
          >
            <option value="">Choose…</option>
            {chosen.options.map((option) => (
              <option key={option.id} value={option.id}>
                {option.label}
              </option>
            ))}
          </Select>
        </Field>

        <Field label="Out of" htmlFor="go-from">
          <Select
            id="go-from"
            value={draft.from_location}
            onChange={(event) => set({ from_location: event.target.value })}
          >
            <option value="">Choose…</option>
            {yards.map((location) => (
              <option key={location.id} value={location.id}>
                {location.name}
              </option>
            ))}
          </Select>
        </Field>

        <Field
          label="Who is taking it"
          htmlFor="go-holder"
          hint="It goes on their custody record when it leaves the gate."
        >
          <Select
            id="go-holder"
            value={draft.custody_holder}
            onChange={(event) => set({ custody_holder: event.target.value })}
          >
            <option value="">Choose…</option>
            {(people.data?.results ?? []).map((person) => (
              <option key={person.id} value={person.id}>
                {person.full_name}
              </option>
            ))}
          </Select>
        </Field>
      </Card>

      <Card className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-sm font-semibold text-slate-900">
            What is going {draft.lines.length ? `(${draft.lines.length})` : ''}
          </h2>
          <Button onClick={() => setLineSheet(true)}>Add</Button>
        </div>

        {draft.lines.length === 0 ? (
          <EmptyState
            title="Nothing on the request yet."
            hint="Scan a serial or a drum, or search the catalogue."
          />
        ) : (
          <ul className="flex flex-col gap-2">
            {draft.lines.map((line, index) => (
              <li
                key={`${line.item_type}-${index}`}
                className="flex items-start justify-between gap-3 rounded-lg border border-slate-200 p-3 text-sm"
              >
                <div>
                  <p className="font-medium text-slate-900">
                    {line.requested_qty} {line.uom} · {line.item_name}
                  </p>
                  <p className="text-slate-600">
                    {line.serials?.length
                      ? line.serials.map((serial) => serial.serial_number).join(', ')
                      : line.reels?.length
                        ? line.reels
                            .map((reel) => `${reel.drum_number} (${reel.length_requested})`)
                            .join(', ')
                        : 'bulk'}
                    {' · '}
                    {/* Which lot this takes. Two lines that look identical can
                        be asking for two different piles of the same item, and
                        only one of them may exist. */}
                    {line.owner_client
                      ? `${clientName(line.owner_client)}’s stock`
                      : 'your own stock'}
                  </p>
                  {line.is_returnable ? (
                    <p className="text-amber-800">
                      Expected back{line.expected_return_date ? ` by ${line.expected_return_date}` : ''}.
                    </p>
                  ) : null}
                </div>
                <div className="flex shrink-0 items-center gap-1">
                <Button
                  variant="ghost"
                  className="min-h-0 px-2 py-1 text-sm"
                  onClick={() => setEditingLine(index)}
                >
                  Change
                </Button>
                <Button
                  variant="ghost"
                  className="min-h-0 px-2 py-1 text-sm text-red-700"
                  onClick={() =>
                    setDraft((current) => ({
                      ...current,
                      lines: current.lines.filter((_line, position) => position !== index),
                    }))
                  }
                >
                  Remove
                </Button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card>
        <Field label="Notes" htmlFor="go-notes">
          <Textarea
            id="go-notes"
            value={draft.notes}
            onChange={(event) => set({ notes: event.target.value })}
          />
        </Field>
      </Card>

      {/* `ActionBar`, not a hand-rolled fixed bar. A `fixed bottom-0` div sits
          *under* the shell's phone tab bar, which is also fixed at the bottom —
          so on the device this screen was designed for, its primary button was
          covered and unreachable. Found by running T8.13 on a phone viewport.
          `ActionBar` is sticky, so it stacks above the content and below the
          tabs. */}
      <ActionBar>
        <Button
          variant="secondary"
          block
          disabled={!canSubmit}
          loading={create.isPending && !submit.isPending}
          onClick={() => saveThenSubmit(false)}
        >
          Save as draft
        </Button>
        <Button
          block
          disabled={!canSubmit}
          loading={submit.isPending}
          onClick={() => saveThenSubmit(true)}
        >
          Send for approval
        </Button>
      </ActionBar>

      <LineSheet
        open={lineSheet || editingLine !== null}
        initial={editingLine !== null ? draft.lines[editingLine] : undefined}
        onClose={() => {
          setLineSheet(false);
          setEditingLine(null);
        }}
        fromLocation={draft.from_location}
        fromLocationName={
          (locations.data?.results ?? []).find(
            (row) => String(row.id) === draft.from_location,
          )?.name ?? 'that location'
        }
        fromNodeId={
          (locations.data?.results ?? []).find(
            (row) => String(row.id) === draft.from_location,
          )?.node_id ?? null
        }
        onAdd={(line) => {
          setDraft((current) => ({
            ...current,
            lines:
              editingLine === null
                ? [...current.lines, line]
                : current.lines.map((existing, position) =>
                    position === editingLine ? line : existing,
                  ),
          }));
          setLineSheet(false);
          setEditingLine(null);
        }}
      />
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* One line                                                                   */
/* -------------------------------------------------------------------------- */

/** One lot on the shelf: whose it is, and in what condition. */
function keyOf(row: StockBalance): string {
  return `${row.owner_client ?? 'own'}:${row.condition}`;
}

/** A number a person would say out loud: 5, not 5.000. */
function trim(quantity: string): string {
  const text = String(quantity);
  return text.includes('.') ? text.replace(/0+$/, '').replace(/\.$/, '') : text;
}

function describe(row: StockBalance): string {
  const owner = row.owner_client_name ? `${row.owner_client_name}’s` : 'your own';
  return `${owner} stock (${row.condition.replaceAll('_', ' ').toLowerCase()})`;
}

function LineSheet({
  open,
  initial,
  onClose,
  onAdd,
  fromLocation,
  fromLocationName,
  fromNodeId,
}: {
  open: boolean;
  /** A line being corrected. Absent when a new one is being added. */
  initial?: GateOutLine;
  onClose: () => void;
  onAdd: (line: GateOutLine) => void;
  fromLocation: string;
  fromLocationName: string;
  fromNodeId: number | null;
}) {
  const [itemId, setItemId] = useState('');
  const [quantity, setQuantity] = useState('');
  const [returnable, setReturnable] = useState(false);
  const [returnDate, setReturnDate] = useState('');
  const [scanned, setScanned] = useState<{ unit?: SerialUnit; reel?: Reel } | null>(null);
  // D3: a serialized item can only go out as a quantity if the yard holds
  // untagged units, and then the reason is part of the record. The server
  // refuses the line without it, so the reason is asked for here rather than
  // left as a 400 nobody can act on.
  const [noSerialReason, setNoSerialReason] = useState('');
  const [error, setError] = useState<string | null>(null);

  const items = useList<ItemType>('item-types', { page_size: 500 });
  void fromLocation;

  const item = (items.data?.results ?? []).find((row) => String(row.id) === itemId);

  /**
   * What this location actually holds of this item, by owner and condition.
   *
   * From a real refusal. A technician asked for three vests, the stock screen
   * showed five at the yard, and "Send for approval" said some lines ask for
   * more than is in stock. Both were right: the five belonged to Safaricom and
   * the line asked for the company's own, because the line had no way to say
   * whose stock it wanted. The choice was never offered, so it was always
   * "ours" — and the mismatch only surfaced at the end, to the approver's
   * screen rather than the requester's.
   */
  const holdings = useList<StockBalance>(
    'stock',
    { item_type: itemId || undefined, node: fromNodeId ?? undefined },
    { enabled: Boolean(itemId && fromNodeId) },
  );
  const available = (holdings.data?.results ?? []).filter(
    (row) => Number(row.quantity) > 0,
  );
  const [holdingKey, setHoldingKey] = useState('');
  // A key seeded from a line whose lot has since gone (or never existed at this
  // location) must not quietly fall back to "ours" — it has to read as unset.
  const chosenKey = available.some((row) => keyOf(row) === holdingKey) ? holdingKey : '';
  const holding =
    available.find((row) => keyOf(row) === chosenKey) ??
    (available.length === 1 ? available[0] : undefined);

  function reset() {
    setItemId('');
    setQuantity('');
    setReturnable(false);
    setReturnDate('');
    setScanned(null);
    setNoSerialReason('');
    setHoldingKey('');
    setError(null);
  }

  /** D7, G1: a scanned identifier names the exact unit that is going. */
  async function lookup(value: string) {
    setError(null);
    try {
      const found = await api.get<{ kind: string; object: SerialUnit | Reel }>(
        `/stock/lookup?q=${encodeURIComponent(value)}`,
      );
      if (found.kind === 'serial_unit') {
        const unit = found.object as SerialUnit;
        setScanned({ unit });
        setItemId(String(unit.item_type));
        setQuantity('1');
      } else {
        const reel = found.object as Reel;
        setScanned({ reel });
        setItemId(String(reel.item_type));
      }
    } catch {
      setError(`Nothing here matches "${value}". It may be another organization's.`);
    }
  }

  function add() {
    setError(null);
    if (!item) {
      setError('Choose an item, or scan one.');
      return;
    }
    if (!quantity || Number(quantity) <= 0) {
      setError('How much is going?');
      return;
    }

    // Refuse here, where it can be fixed, rather than at "Send for approval"
    // where the only thing left to do is go back and start again.
    if (!scanned && fromNodeId && !holdings.isLoading) {
      if (available.length === 0) {
        setError(`${fromLocationName} holds no ${item.name}.`);
        return;
      }
      if (available.length > 1 && !holding) {
        setError(`${fromLocationName} holds more than one lot of ${item.name}. Choose which one is going.`);
        return;
      }
      if (holding && Number(quantity) > Number(holding.quantity)) {
        setError(
          `${fromLocationName} holds ${trim(holding.quantity)} ${holding.uom} of ` +
            `${describe(holding)}. Ask for that much or less.`,
        );
        return;
      }
    }

    // F1: a serialized item is requested by identity. Without a scan there is
    // nothing to identify, so the line either names a unit or explains why it
    // cannot — the same rule the server enforces.
    const serializedWithoutAScan =
      !scanned?.unit && item.default_tracking_mode === 'SERIALIZED';
    if (serializedWithoutAScan && !noSerialReason.trim()) {
      setError(
        `${item.name} is tracked by serial number. Scan the unit that is going, ` +
          `or say why there is no serial.`,
      );
      return;
    }

    const line: GateOutLine = {
      item_type: item.id,
      item_name: item.name,
      tracking_mode: scanned?.unit
        ? 'SERIALIZED'
        : scanned?.reel
          ? 'REEL'
          : serializedWithoutAScan
            // Untagged units are a quantity, with the reason recorded (D3).
            ? 'BULK'
            : item.default_tracking_mode,
      no_serial_reason: serializedWithoutAScan ? noSerialReason.trim() : '',
      requested_qty: quantity,
      uom: item.uom,
      // Whose stock, and in what condition — taken from the lot that was
      // chosen, so the request matches something that is really there.
      owner_type: holding?.owner_client ? 'CLIENT' : 'OWN',
      owner_client: holding?.owner_client ?? null,
      condition: holding?.condition ?? 'NEW',
      // C3: a returnable item defaults to returnable, and the date comes from
      // the item type — so nobody has to remember which tools come back.
      is_returnable: returnable || item.is_returnable,
      expected_return_date: returnDate || null,
      serials: scanned?.unit
        ? [{ serial_unit: scanned.unit.id, serial_number: scanned.unit.serial_number }]
        : [],
      reels: scanned?.reel
        ? [
            {
              reel: scanned.reel.id,
              drum_number: scanned.reel.drum_number,
              length_requested: quantity,
            },
          ]
        : [],
    };

    onAdd(line);
    reset();
  }

  useEffect(() => {
    if (item?.is_returnable) setReturnable(true);
  }, [item]);

  // Fill the controls from the line being corrected. Keyed on the sheet
  // opening, so typing into it afterwards is not overwritten.
  useEffect(() => {
    if (!open) return;
    if (!initial) return;
    setItemId(String(initial.item_type));
    setQuantity(String(initial.requested_qty));
    setReturnable(Boolean(initial.is_returnable));
    setReturnDate(initial.expected_return_date ?? '');
    setNoSerialReason(initial.no_serial_reason ?? '');
    setHoldingKey(`${initial.owner_client ?? 'own'}:${initial.condition ?? 'NEW'}`);
  }, [open, initial]);

  return (
    <Sheet
      open={open}
      title={initial ? 'Change this line' : 'Add to the request'}
      onClose={() => {
        reset();
        onClose();
      }}
      footer={
        <>
          <Button
            variant="secondary"
            block
            onClick={() => {
              reset();
              onClose();
            }}
          >
            Cancel
          </Button>
          <Button block onClick={add}>
            {initial ? 'Save the line' : 'Add'}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        {error ? <Banner tone="error">{error}</Banner> : null}

        <BarcodeScanner
          label="Scan a serial or a drum"
          hint="Fastest route. Serialized material goes out by identity, not by count."
          onScan={lookup}
        />

        {scanned?.unit ? (
          <Banner tone="info">
            <span className="flex flex-wrap items-center gap-2">
              {scanned.unit.serial_number} · {scanned.unit.item_name} · at{' '}
              {scanned.unit.node_label}
              <OwnershipBadge client={scanned.unit.owner_client_name || null} />
            </span>
          </Banner>
        ) : null}

        {scanned?.reel ? (
          <Banner tone="info">
            {scanned.reel.drum_number} · {scanned.reel.remaining_length}{' '}
            {scanned.reel.uom} left · at {scanned.reel.node_label}
          </Banner>
        ) : null}

        {!scanned ? (
          <Field label="Or choose an item" htmlFor="gol-item">
            <Select
              id="gol-item"
              value={itemId}
              onChange={(event) => setItemId(event.target.value)}
            >
              <option value="">Choose…</option>
              {(items.data?.results ?? [])
                .filter((row) => !row.is_archived)
                .map((row) => (
                  <option key={row.id} value={row.id}>
                    {row.name}
                  </option>
                ))}
            </Select>
          </Field>
        ) : null}

        {!scanned && item && fromNodeId ? (
          holdings.isLoading ? (
            <p className="text-sm text-slate-500">
              Checking what {fromLocationName} holds…
            </p>
          ) : available.length === 0 ? (
            <Banner tone="warning">
              {fromLocationName} holds no {item.name}. Nothing can go out of it
              that is not there.
            </Banner>
          ) : available.length === 1 ? (
            <p className="text-sm text-slate-600">
              {fromLocationName} holds {trim(available[0].quantity)}{' '}
              {available[0].uom} of {describe(available[0])}. That is what this
              line takes.
            </p>
          ) : (
            <Field
              label="Whose stock is going"
              htmlFor="gol-holding"
              hint="The same item can sit in the yard under more than one owner or condition. They are counted separately, and a pass has to name one."
            >
              <Select
                id="gol-holding"
                value={chosenKey}
                onChange={(event) => setHoldingKey(event.target.value)}
              >
                <option value="">Choose…</option>
                {available.map((row) => (
                  <option key={keyOf(row)} value={keyOf(row)}>
                    {trim(row.quantity)} {row.uom} · {describe(row)}
                  </option>
                ))}
              </Select>
            </Field>
          )
        ) : null}

        {!scanned?.unit && item?.default_tracking_mode === 'SERIALIZED' ? (
          <Field
            label="No serial available — why?"
            htmlFor="gol-no-serial"
            hint="This item is normally tracked by serial number. Scanning it above is the better route; this is for units the yard holds untagged."
          >
            <Input
              id="gol-no-serial"
              value={noSerialReason}
              onChange={(event) => setNoSerialReason(event.target.value)}
            />
          </Field>
        ) : null}

        <Field
          label={`How much${item ? ` (${item.uom})` : ''}`}
          htmlFor="gol-quantity"
          hint={
            scanned?.reel
              ? 'Less than the drum holds is a cut; all of it means the drum goes with them.'
              : undefined
          }
        >
          <Input
            id="gol-quantity"
            inputMode="decimal"
            value={quantity}
            disabled={Boolean(scanned?.unit)}
            onChange={(event) => setQuantity(event.target.value)}
          />
        </Field>

        {returnable ? (
          <Field
            label="Expected back"
            htmlFor="gol-return"
            hint="It stays on their record until it comes back, and gets chased if it does not."
          >
            <Input
              id="gol-return"
              type="date"
              value={returnDate}
              onChange={(event) => setReturnDate(event.target.value)}
            />
          </Field>
        ) : null}
      </div>
    </Sheet>
  );
}
