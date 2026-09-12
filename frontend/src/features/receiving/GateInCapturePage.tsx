/**
 * T3.19 — the gate-in capture flow (design §7.3, §7.4; D1–D8, J1).
 *
 * The criterion is specific: "a storekeeper enters a ten-line mixed delivery on a
 * phone one-handed, and **drafts survive a page reload**."
 *
 * Both halves shape this screen.
 *
 * *One-handed* means the line-entry loop is the whole screen: scan or search,
 * confirm, and the form resets ready for the next line with the previous one
 * listed above it. No dialog to dismiss between lines, and the primary action is
 * bottom-anchored where a thumb reaches (§7.3).
 *
 * *Surviving a reload* means the draft is written to `localStorage` on every
 * change, not only on submit. A phone in a yard loses its browser tab to a
 * backgrounded process or a dropped call, and losing nine of ten lines to that is
 * how a system gets abandoned. The full offline queue is T8.3; this is the part
 * that cannot wait for it.
 *
 * The screen also edits a draft that is already on the server (`/gate-in/:id/
 * edit`). A draft has posted nothing, so a mis-keyed quantity in one is just a
 * mistake to correct — but until this existed the only way to correct it was to
 * abandon it and key the whole delivery again. When editing, the browser's own
 * stored draft is left alone: it belongs to a different, unsaved delivery.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';

import { ApiError } from '../../api/client';
import { errorMessage, useAction, useDetail, useList } from '../../api/hooks';
import { useOffline } from '../../offline/OfflineProvider';
import { newUuid } from '../../offline/db';
import { submitOrQueue } from '../../offline/sync';
import { BarcodeScanner } from '../../components/BarcodeScanner';
import {
  ActionBar,
  Banner,
  Button,
  Card,
  Field,
  Input,
  Select,
  Spinner,
  Textarea,
} from '../../components/ui';
import { EmptyState, PageHeader, Sheet } from '../../components/ui/data';
import type { Client, ItemType, Location, Site } from '../settings/types';
import type { Condition, GateIn, GateInLineInput, SourceType } from './types';

const DRAFT_KEY = 'yardflow.gate-in.draft';

const SOURCES: { value: SourceType; label: string; hint: string }[] = [
  { value: 'PURCHASE', label: 'Purchase', hint: 'Bought by us. Our stock.' },
  {
    value: 'CLIENT_ISSUE',
    label: 'Client issue',
    hint: 'Consignment material. Stays theirs, wherever it goes.',
  },
  {
    value: 'RECOVERY',
    label: 'Recovery from a site',
    hint: 'Off a decommissioned or demolished site. Records which one.',
  },
  {
    value: 'RETURN_FROM_SITE',
    label: 'Return from site',
    hint: 'Unused material coming back. Comes off the holder’s record.',
  },
  { value: 'WARRANTY_RETURN', label: 'Warranty or faulty return', hint: '' },
  { value: 'TRANSFER', label: 'Transfer from another location', hint: '' },
];

const CONDITIONS: { value: Condition; label: string; quarantines: boolean }[] = [
  { value: 'NEW', label: 'New', quarantines: false },
  { value: 'USED_SERVICEABLE', label: 'Used, serviceable', quarantines: false },
  { value: 'FAULTY', label: 'Faulty', quarantines: true },
  { value: 'DAMAGED', label: 'Damaged', quarantines: true },
  { value: 'SCRAP', label: 'Scrap', quarantines: true },
];

interface DraftHeader {
  source_type: SourceType;
  supplier_name: string;
  client: string;
  returned_by: string;
  origin_site: string;
  to_location: string;
  client_delivery_note_ref: string;
  notes: string;
}

interface Draft {
  header: DraftHeader;
  lines: GateInLineInput[];
  /**
   * This delivery's identity, decided here and sent with it (N2, §8.2).
   *
   * A storekeeper pressed "Save as draft" three times on a slow connection and
   * got three drafts. Disabling the button while the request is in flight does
   * not close the window — the second press leaves before the first reply
   * arrives. The server matches on this instead and hands back the document it
   * already made. It is generated once per delivery and kept with the draft, so
   * a reload or a retry is still the same delivery; only clearing the form
   * starts a new one.
   */
  client_uuid: string;
  /**
   * When the delivery actually arrived.
   *
   * Set only when correcting a saved draft. A new capture stamps the moment it
   * is saved — but a correction must not, or fixing a typo on Friday would
   * record Tuesday's delivery as having arrived on Friday.
   */
  received_at?: string;
}

function emptyDraft(): Draft {
  return {
    header: {
      source_type: 'PURCHASE',
      supplier_name: '',
      client: '',
      returned_by: '',
      origin_site: '',
      to_location: '',
      client_delivery_note_ref: '',
      notes: '',
    },
    lines: [],
    client_uuid: newUuid(),
  };
}

function readDraft(): Draft {
  try {
    const stored = localStorage.getItem(DRAFT_KEY);
    if (!stored) return emptyDraft();
    const parsed = JSON.parse(stored) as Draft;
    // A stored shape from an older build must not break the screen: a
    // storekeeper cannot fix a bad draft, they can only give up on the app.
    if (!parsed?.header || !Array.isArray(parsed.lines)) return emptyDraft();
    // A draft stored before this field existed still deserves one.
    return parsed.client_uuid ? parsed : { ...parsed, client_uuid: newUuid() };
  } catch {
    return emptyDraft();
  }
}

export default function GateInCapturePage() {
  const navigate = useNavigate();
  // Present when an existing draft is being corrected, absent when a new
  // delivery is being keyed. Everything below reads off this one fact.
  const { id: editingId } = useParams();
  const existing = useDetail<GateIn>('gate-ins', editingId);

  const [draft, setDraft] = useState<Draft>(() => (editingId ? emptyDraft() : readDraft()));
  const [lineSheet, setLineSheet] = useState(false);
  const [banner, setBanner] = useState<string | null>(null);
  const [restored, setRestored] = useState(() =>
    editingId ? false : readDraft().lines.length > 0,
  );
  const [loaded, setLoaded] = useState(false);

  const locations = useList<Location>('locations', { page_size: 200 });
  const clients = useList<Client>('clients', { page_size: 200 });
  const sites = useList<Site>('sites', { page_size: 200 });
  const items = useList<ItemType>('item-types', { page_size: 500 });
  const people = useList<{ id: number; full_name: string }>('users', { page_size: 200 });

  // Every change, not only on submit. See the module comment. Skipped while
  // editing: that draft lives on the server, and writing it here would
  // overwrite an unsaved delivery the same person may have in progress.
  useEffect(() => {
    if (editingId) return;
    try {
      localStorage.setItem(DRAFT_KEY, JSON.stringify(draft));
    } catch {
      // Private mode or a full quota. The entry still works; it just will not
      // survive a reload, and saying nothing is better than blocking the flow.
    }
  }, [draft, editingId]);

  // Fill the form from the saved draft, once.
  useEffect(() => {
    if (!editingId || loaded || !existing.data) return;
    const document = existing.data;
    setDraft({
      header: {
        source_type: document.source_type,
        supplier_name: document.supplier_name ?? '',
        client: document.client ? String(document.client) : '',
        returned_by: document.returned_by ? String(document.returned_by) : '',
        origin_site: document.origin_site ? String(document.origin_site) : '',
        to_location: document.to_location ? String(document.to_location) : '',
        client_delivery_note_ref: document.client_delivery_note_ref ?? '',
        notes: document.notes ?? '',
      },
      lines: document.lines ?? [],
      // Its identity is its id now; the uuid guards a *create*.
      client_uuid: document.client_uuid ?? newUuid(),
      received_at: document.received_at,
    });
    setLoaded(true);
  }, [editingId, loaded, existing.data]);

  const { online, refresh } = useOffline();
  const create = useAction<Record<string, unknown>, { id: number }>({
    resource: 'gate-ins',
  });
  const post = useAction<{ id: number }, { id: number; number: string }>({
    resource: 'gate-ins',
    path: (body) => `${body.id}/post`,
    // Posting moves stock, so every stock view is stale the moment it lands.
    invalidates: ['gate-ins', 'stock', 'movements', 'serials', 'drums'],
  });
  const save = useAction<Record<string, unknown>, { id: number }>({
    resource: 'gate-ins',
    method: 'patch',
    path: () => String(editingId),
    invalidates: ['gate-ins'],
  });

  /** Create, or update the draft being corrected. One name for both. */
  async function store(): Promise<{ id: number }> {
    return editingId ? save.mutateAsync(payload()) : create.mutateAsync(payload());
  }

  const yards = useMemo(
    () => (locations.data?.results ?? []).filter((row) => row.type !== 'QUARANTINE'),
    [locations.data],
  );

  const loadingExisting = Boolean(editingId) && existing.isLoading;
  /** "Client owned" without the client is half a sentence. */
  function clientName(id: number): string {
    return (
      (clients.data?.results ?? []).find((row) => row.id === id)?.name ?? 'A client'
    );
  }

  const source = SOURCES.find((entry) => entry.value === draft.header.source_type);
  const needsClient = draft.header.source_type === 'CLIENT_ISSUE';
  const needsOriginSite = draft.header.source_type === 'RECOVERY';
  const needsReturner = draft.header.source_type === 'RETURN_FROM_SITE';

  const setHeader = useCallback(
    (patch: Partial<DraftHeader>) =>
      setDraft((current) => ({ ...current, header: { ...current.header, ...patch } })),
    [],
  );

  function addLine(line: GateInLineInput) {
    setDraft((current) => ({ ...current, lines: [...current.lines, line] }));
    setLineSheet(false);
  }

  function removeLine(index: number) {
    setDraft((current) => ({
      ...current,
      lines: current.lines.filter((_line, position) => position !== index),
    }));
  }

  function clearDraft() {
    if (editingId) return;
    // A fresh identity with the fresh form: the next delivery is a different
    // delivery, whatever the last one was.
    setDraft(emptyDraft());
    setRestored(false);
    try {
      localStorage.removeItem(DRAFT_KEY);
    } catch {
      /* nothing to clear */
    }
  }

  function payload() {
    const header = draft.header;
    return {
      source_type: header.source_type,
      supplier_name: header.supplier_name,
      client: header.client ? Number(header.client) : null,
      returned_by: header.returned_by ? Number(header.returned_by) : null,
      origin_site: header.origin_site ? Number(header.origin_site) : null,
      to_location: Number(header.to_location),
      received_at: draft.received_at ?? new Date().toISOString(),
      client_delivery_note_ref: header.client_delivery_note_ref,
      notes: header.notes,
      client_uuid: draft.client_uuid,
      lines: draft.lines,
    };
  }

  async function saveDraft() {
    setBanner(null);
    try {
      const saved = await store();
      clearDraft();
      navigate(`/gate-in/${saved.id}`);
    } catch (error) {
      setBanner(errorMessage(error));
    }
  }

  async function saveAndPost() {
    setBanner(null);
    try {
      // Correcting a draft that is already on the server is an online job: the
      // queue can only replay a *create*, so queueing an edit would quietly
      // drop the correction and post the old lines.
      if (editingId) {
        const saved = await store();
        const posted = await post.mutateAsync({ id: saved.id });
        navigate(`/gate-in/${posted.id}`);
        return;
      }

      // N1: with no signal this goes to the device's queue instead, carrying a
      // client_uuid so a later retry cannot double-post it (N2, §8.2). The
      // storekeeper's flow does not change — which is the point: a gate-in they
      // cannot capture is a delivery that goes unrecorded.
      const outcome = await submitOrQueue('GATE_IN', payload(), async () => {
        const saved = await store();
        return post.mutateAsync({ id: saved.id });
      });

      clearDraft();
      if (outcome.queued) {
        await refresh();
        setBanner(
          'No connection — this delivery is saved on the device and will be ' +
            'sent as soon as you have signal. It is listed under Sync until then.',
        );
        return;
      }
      navigate(`/gate-in/${outcome.result!.id}`);
    } catch (error) {
      // A validation failure on posting (a missing custom field, a duplicate
      // serial) leaves the draft saved on the server, which is the right
      // outcome — nothing is lost and it can be fixed and posted again.
      setBanner(
        error instanceof ApiError
          ? `${error.message} The delivery has been saved as a draft.`
          : errorMessage(error),
      );
    }
  }

  const canSubmit = Boolean(draft.header.to_location) && draft.lines.length > 0;
  // The button says what is actually going to happen. "Receive it" when there is
  // no connection would be a small lie, and the storekeeper finds out later.
  const submitLabel = online ? 'Receive it' : 'Save on this device';

  if (loadingExisting) return <Spinner className="text-slate-400" />;
  if (editingId && existing.isError) {
    return <Banner tone="error">{errorMessage(existing.error)}</Banner>;
  }

  return (
    <div className="flex flex-col gap-4 pb-24">
      <PageHeader
        title={editingId ? 'Correct this delivery' : 'Receive a delivery'}
        subtitle={
          editingId
            ? 'It is still a draft, so nothing has moved yet.'
            : 'Everything arriving at the yard, in one document.'
        }
        actions={
          editingId ? (
            <Button variant="ghost" onClick={() => navigate(`/gate-in/${editingId}`)}>
              Cancel
            </Button>
          ) : draft.lines.length > 0 ? (
            <Button variant="ghost" onClick={clearDraft}>
              Discard
            </Button>
          ) : undefined
        }
      />

      {restored && draft.lines.length > 0 ? (
        <Banner tone="info">
          Picked up where you left off — {draft.lines.length}{' '}
          {draft.lines.length === 1 ? 'line' : 'lines'} were still unsaved on this
          device.
        </Banner>
      ) : null}

      {banner ? <Banner tone="error">{banner}</Banner> : null}

      <Card className="flex flex-col gap-3">
        <h2 className="text-sm font-semibold text-slate-900">Where it came from</h2>

        <Field label="Source" htmlFor="gi-source" hint={source?.hint}>
          <Select
            id="gi-source"
            value={draft.header.source_type}
            onChange={(event) => setHeader({ source_type: event.target.value as SourceType })}
          >
            {SOURCES.map((entry) => (
              <option key={entry.value} value={entry.value}>
                {entry.label}
              </option>
            ))}
          </Select>
        </Field>

        {needsClient ? (
          <Field
            label="Client"
            htmlFor="gi-client"
            hint="Consignment stock stays theirs wherever it goes."
          >
            <Select
              id="gi-client"
              value={draft.header.client}
              onChange={(event) => setHeader({ client: event.target.value })}
            >
              <option value="">Choose…</option>
              {(clients.data?.results ?? []).map((client) => (
                <option key={client.id} value={client.id}>
                  {client.name}
                </option>
              ))}
            </Select>
          </Field>
        ) : (
          <Field label="Supplier" htmlFor="gi-supplier">
            <Input
              id="gi-supplier"
              value={draft.header.supplier_name}
              onChange={(event) => setHeader({ supplier_name: event.target.value })}
            />
          </Field>
        )}

        {needsOriginSite ? (
          <Field
            label="Recovered from"
            htmlFor="gi-origin"
            hint="Recorded on the document and on each unit, so recoveries can be reported by site."
          >
            <Select
              id="gi-origin"
              value={draft.header.origin_site}
              onChange={(event) => setHeader({ origin_site: event.target.value })}
            >
              <option value="">Choose…</option>
              {(sites.data?.results ?? []).map((site) => (
                <option key={site.id} value={site.id}>
                  {site.internal_ref} · {site.name}
                </option>
              ))}
            </Select>
          </Field>
        ) : null}

        {needsReturner ? (
          <>
            <Field
              label="Handed back by"
              htmlFor="gi-returner"
              hint="This is whose custody record it comes off. Required for a return."
            >
              <Select
                id="gi-returner"
                value={draft.header.returned_by}
                onChange={(event) => setHeader({ returned_by: event.target.value })}
              >
                <option value="">Choose…</option>
                {(people.data?.results ?? []).map((person) => (
                  <option key={person.id} value={person.id}>
                    {person.full_name}
                  </option>
                ))}
              </Select>
            </Field>
            <Field
              label="Coming back from"
              htmlFor="gi-return-site"
              hint="Lets the site's reconciliation see the return."
            >
              <Select
                id="gi-return-site"
                value={draft.header.origin_site}
                onChange={(event) => setHeader({ origin_site: event.target.value })}
              >
                <option value="">Choose…</option>
                {(sites.data?.results ?? []).map((site) => (
                  <option key={site.id} value={site.id}>
                    {site.internal_ref} · {site.name}
                  </option>
                ))}
              </Select>
            </Field>
          </>
        ) : null}

        <Field label="Received into" htmlFor="gi-location">
          <Select
            id="gi-location"
            value={draft.header.to_location}
            onChange={(event) => setHeader({ to_location: event.target.value })}
          >
            <option value="">Choose…</option>
            {yards.map((location) => (
              <option key={location.id} value={location.id}>
                {location.name}
              </option>
            ))}
          </Select>
        </Field>

        <Field label="Their delivery note" htmlFor="gi-note-ref" hint="Keeps the paper trail.">
          <Input
            id="gi-note-ref"
            value={draft.header.client_delivery_note_ref}
            onChange={(event) => setHeader({ client_delivery_note_ref: event.target.value })}
          />
        </Field>
      </Card>

      <Card className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-sm font-semibold text-slate-900">
            Lines {draft.lines.length ? `(${draft.lines.length})` : ''}
          </h2>
          <Button onClick={() => setLineSheet(true)}>Add a line</Button>
        </div>

        {draft.lines.length === 0 ? (
          <EmptyState
            title="Nothing on this delivery yet."
            hint="Serialized, bulk and reel lines can all go on the same document."
          />
        ) : (
          <ul className="flex flex-col gap-2">
            {draft.lines.map((line, index) => {
              const quarantines = CONDITIONS.find(
                (entry) => entry.value === line.condition,
              )?.quarantines;
              return (
                <li
                  key={`${line.item_type}-${index}`}
                  className="flex items-start justify-between gap-3 rounded-lg border border-slate-200 p-3"
                >
                  <div className="text-sm">
                    <p className="font-medium text-slate-900">
                      {line.quantity} {line.uom} · {line.item_name}
                    </p>
                    <p className="text-slate-600">
                      {line.condition.replaceAll('_', ' ').toLowerCase()}
                      {line.owner_client ? ` · ${clientName(line.owner_client)} owns this` : ''}
                      {line.serials?.length ? ` · ${line.serials.length} serials` : ''}
                      {line.reels?.length ? ` · ${line.reels.length} drums` : ''}
                    </p>
                    {quarantines ? (
                      // J1: said here, at entry, because this is the decision
                      // point — not discovered later when it cannot be issued.
                      <p className="text-amber-800">Goes to quarantine, not to free stock.</p>
                    ) : null}
                  </div>
                  <Button
                    variant="ghost"
                    className="min-h-0 px-2 py-1 text-sm text-red-700"
                    onClick={() => removeLine(index)}
                  >
                    Remove
                  </Button>
                </li>
              );
            })}
          </ul>
        )}
      </Card>

      <Card>
        <Field label="Notes" htmlFor="gi-notes">
          <Textarea
            id="gi-notes"
            value={draft.header.notes}
            onChange={(event) => setHeader({ notes: event.target.value })}
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
          loading={(create.isPending || save.isPending) && !post.isPending}
          onClick={saveDraft}
        >
          Save as draft
        </Button>
        <Button block disabled={!canSubmit} loading={post.isPending} onClick={saveAndPost}>
          {submitLabel}
        </Button>
      </ActionBar>

      <LineSheet
        open={lineSheet}
        onClose={() => setLineSheet(false)}
        onAdd={addLine}
        items={items.data?.results ?? []}
        clients={clients.data?.results ?? []}
        defaultClient={needsClient ? draft.header.client : ''}
      />
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* One line                                                                   */
/* -------------------------------------------------------------------------- */

function LineSheet({
  open,
  onClose,
  onAdd,
  items,
  clients,
  defaultClient,
}: {
  open: boolean;
  onClose: () => void;
  onAdd: (line: GateInLineInput) => void;
  items: ItemType[];
  clients: Client[];
  defaultClient: string;
}) {
  const [itemId, setItemId] = useState('');
  const [quantity, setQuantity] = useState('');
  const [condition, setCondition] = useState<Condition>('NEW');
  const [ownerClient, setOwnerClient] = useState(defaultClient);
  const [serials, setSerials] = useState<string[]>([]);
  const [drums, setDrums] = useState<{ drum_number: string; length: string }[]>([]);
  const [noSerialReason, setNoSerialReason] = useState('');
  const [notes, setNotes] = useState('');
  const [error, setError] = useState<string | null>(null);
  /**
   * What has been typed into the serial and drum fields but not yet added.
   *
   * Reported from those controls so that "Add line" can finish the entry rather
   * than refuse it. A storekeeper who typed a drum number and its length has
   * said everything the line needs; telling them to "add at least one drum"
   * reads as a bug, because from where they are standing it is one.
   */
  const [pendingSerial, setPendingSerial] = useState('');
  const [pendingDrum, setPendingDrum] = useState('');
  const [pendingLength, setPendingLength] = useState('');

  const item = items.find((row) => String(row.id) === itemId);
  const mode = item?.default_tracking_mode ?? 'BULK';

  useEffect(() => {
    if (open) setOwnerClient(defaultClient);
  }, [open, defaultClient]);

  function reset() {
    setItemId('');
    setQuantity('');
    setCondition('NEW');
    setSerials([]);
    setDrums([]);
    setPendingSerial('');
    setPendingDrum('');
    setPendingLength('');
    setNoSerialReason('');
    setNotes('');
    setError(null);
  }

  function add() {
    setError(null);
    if (!item) {
      setError('Choose an item type.');
      return;
    }

    const trackingMode = mode === 'SERIALIZED' && noSerialReason ? 'BULK' : mode;

    // Anything typed and not yet added still counts: pressing this button is
    // as clear a statement of intent as pressing the one beside the field.
    const typedSerial = pendingSerial.trim();
    const allSerials =
      typedSerial && !serials.includes(typedSerial) ? [...serials, typedSerial] : serials;

    const typedDrum = pendingDrum.trim();
    const typedLength = pendingLength.trim();
    if (trackingMode === 'REEL' && typedDrum && !typedLength) {
      setError(`How many ${item.uom} are on drum ${typedDrum}? Fill in its length.`);
      return;
    }
    if (trackingMode === 'REEL' && typedLength && !typedDrum) {
      setError('Which drum is that length on? Fill in its number.');
      return;
    }
    const allDrums =
      typedDrum && typedLength
        ? [...drums, { drum_number: typedDrum, length: typedLength }]
        : drums;

    if (trackingMode === 'SERIALIZED' && allSerials.length === 0) {
      setError(
        'Scan or type at least one serial. If the label is unreadable, say so below and it will be received as bulk.',
      );
      return;
    }
    if (trackingMode === 'REEL' && allDrums.length === 0) {
      setError('Add at least one drum, with its length.');
      return;
    }

    const total =
      trackingMode === 'SERIALIZED'
        ? String(allSerials.length)
        : trackingMode === 'REEL'
          ? String(allDrums.reduce((sum, drum) => sum + Number(drum.length || 0), 0))
          : quantity;

    if (!total || Number(total) <= 0) {
      setError('A quantity is needed.');
      return;
    }

    onAdd({
      item_type: item.id,
      item_name: item.name,
      tracking_mode: trackingMode,
      quantity: total,
      uom: item.uom,
      condition,
      owner_type: ownerClient ? 'CLIENT' : 'OWN',
      owner_client: ownerClient ? Number(ownerClient) : null,
      no_serial_reason: noSerialReason,
      notes,
      serials: allSerials.map((serial_number) => ({ serial_number })),
      reels: allDrums,
    });
    reset();
  }

  return (
    <Sheet
      open={open}
      title="Add a line"
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
            Add line
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        {error ? <Banner tone="error">{error}</Banner> : null}

        <Field label="Item" htmlFor="line-item">
          <Select id="line-item" value={itemId} onChange={(event) => setItemId(event.target.value)}>
            <option value="">Choose…</option>
            {items
              .filter((row) => !row.is_archived)
              .map((row) => (
                <option key={row.id} value={row.id}>
                  {row.name}
                  {row.code ? ` (${row.code})` : ''}
                </option>
              ))}
          </Select>
        </Field>

        {mode === 'SERIALIZED' && !noSerialReason ? (
          <>
            <BarcodeScanner
              label="Serial number"
              hint="One per unit. A duplicate is refused, naming where the existing one sits."
              onDraft={setPendingSerial}
              onScan={(value) => {
                setSerials((current) =>
                  current.includes(value) ? current : [...current, value],
                );
                setPendingSerial('');
              }}
            />
            {serials.length > 0 ? (
              <ul className="flex flex-wrap gap-2">
                {serials.map((serial) => (
                  <li
                    key={serial}
                    className="flex items-center gap-2 rounded-full bg-slate-100 px-3 py-1 text-sm"
                  >
                    <span className="font-mono">{serial}</span>
                    <button
                      type="button"
                      aria-label={`Remove ${serial}`}
                      className="text-slate-500"
                      onClick={() =>
                        setSerials((current) => current.filter((entry) => entry !== serial))
                      }
                    >
                      ×
                    </button>
                  </li>
                ))}
              </ul>
            ) : null}
          </>
        ) : null}

        {mode === 'SERIALIZED' ? (
          <Field
            label="No serial available"
            htmlFor="line-no-serial"
            hint="Received as bulk instead, and the reason is recorded — otherwise it is indistinguishable from something that was never serialized."
          >
            <Input
              id="line-no-serial"
              value={noSerialReason}
              placeholder="Label torn off; unit recovered from a demolished site"
              onChange={(event) => setNoSerialReason(event.target.value)}
            />
          </Field>
        ) : null}

        {mode === 'REEL' ? (
          <DrumEntry
            drums={drums}
            onChange={setDrums}
            uom={item?.uom ?? 'm'}
            drumNumber={pendingDrum}
            onDrumNumberChange={setPendingDrum}
            length={pendingLength}
            onLengthChange={setPendingLength}
          />
        ) : null}

        {mode === 'BULK' || noSerialReason ? (
          <Field label={`Quantity${item ? ` (${item.uom})` : ''}`} htmlFor="line-quantity">
            <Input
              id="line-quantity"
              inputMode="decimal"
              value={quantity}
              onChange={(event) => setQuantity(event.target.value)}
            />
          </Field>
        ) : null}

        <Field
          label="Condition"
          htmlFor="line-condition"
          hint={
            CONDITIONS.find((entry) => entry.value === condition)?.quarantines
              ? 'This lands in quarantine and never shows as available.'
              : undefined
          }
        >
          <Select
            id="line-condition"
            value={condition}
            onChange={(event) => setCondition(event.target.value as Condition)}
          >
            {CONDITIONS.map((entry) => (
              <option key={entry.value} value={entry.value}>
                {entry.label}
              </option>
            ))}
          </Select>
        </Field>

        <Field
          label="Owner"
          htmlFor="line-owner"
          hint="Set here and carried on the stock permanently."
        >
          <Select
            id="line-owner"
            value={ownerClient}
            onChange={(event) => setOwnerClient(event.target.value)}
          >
            <option value="">Our own stock</option>
            {clients.map((client) => (
              <option key={client.id} value={client.id}>
                {client.name}
              </option>
            ))}
          </Select>
        </Field>

        <Field label="Notes" htmlFor="line-notes">
          <Input id="line-notes" value={notes} onChange={(event) => setNotes(event.target.value)} />
        </Field>
      </div>
    </Sheet>
  );
}

/**
 * Drums, one row at a time.
 *
 * The entry row is controlled from the sheet above rather than held here, so
 * that pressing "Add line" with a number and a length still in the boxes adds
 * that drum instead of refusing the line. This screen used to show two fields
 * both labelled "Drum number" — the scanner's, and the row's — which made it
 * look as though typing into either one was enough. The scanner now says what
 * it does: it fills the row, and the row is the only place a drum is entered.
 */
function DrumEntry({
  drums,
  onChange,
  uom,
  drumNumber,
  onDrumNumberChange,
  length,
  onLengthChange,
}: {
  drums: { drum_number: string; length: string }[];
  onChange: (drums: { drum_number: string; length: string }[]) => void;
  uom: string;
  drumNumber: string;
  onDrumNumberChange: (value: string) => void;
  length: string;
  onLengthChange: (value: string) => void;
}) {
  function addPending() {
    onChange([...drums, { drum_number: drumNumber.trim(), length: length.trim() }]);
    onDrumNumberChange('');
    onLengthChange('');
  }

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-slate-200 p-3">
      <p className="text-sm font-medium text-slate-700">Drums</p>
      <p className="text-sm text-slate-600">
        Each drum is its own record with a starting length, so issuing metres
        off it later is possible.
      </p>

      <BarcodeScanner
        label="Scan a drum tag"
        onScan={(value) => onDrumNumberChange(value)}
        hint="It fills the drum number below. You can also type it there."
      />

      <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_minmax(0,8rem)_auto]">
        <Input
          aria-label="Drum number"
          value={drumNumber}
          placeholder="Drum number"
          onChange={(event) => onDrumNumberChange(event.target.value)}
        />
        <Input
          aria-label={`Length in ${uom}`}
          inputMode="decimal"
          value={length}
          placeholder={`Length in ${uom}`}
          onChange={(event) => onLengthChange(event.target.value)}
          onKeyDown={(event) => {
            // Enter is how a keyboard-and-scanner pair works: the gun types the
            // number and the operator types the length, then Enter.
            if (event.key === 'Enter' && drumNumber.trim() && length.trim()) {
              event.preventDefault();
              addPending();
            }
          }}
        />
        <Button
          variant="secondary"
          disabled={!drumNumber.trim() || !length.trim()}
          onClick={addPending}
        >
          Add drum
        </Button>
      </div>

      <p className="text-xs text-slate-500">
        Add each drum, or leave the last one filled in — it is added with the
        line.
      </p>

      {drums.length > 0 ? (
        <ul className="flex flex-col gap-1 text-sm">
          {drums.map((drum, index) => (
            <li key={drum.drum_number} className="flex justify-between">
              <span className="font-mono">{drum.drum_number}</span>
              <span className="flex items-center gap-2">
                {drum.length} {uom}
                <button
                  type="button"
                  aria-label={`Remove ${drum.drum_number}`}
                  className="text-slate-500"
                  onClick={() =>
                    onChange(drums.filter((_entry, position) => position !== index))
                  }
                >
                  ×
                </button>
              </span>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
