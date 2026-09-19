/**
 * T3.20, T3.21 — stock, serial, drum, transfer and count screens
 * (design §7.4; E1–E6).
 *
 * T3.20's criterion is the one that shapes the search box: "typing a serial
 * number anywhere in search jumps straight to its history." So the box does not
 * filter a list — it asks the server what the text *is* (`/stock/lookup`), and
 * navigates. A storekeeper holding a unit with a barcode does not know whether it
 * is a serial, an asset tag or a drum number, and should not have to pick a mode
 * before scanning (D7).
 *
 * E1 runs through every list here: client-owned stock is visually distinct
 * wherever it appears, because issuing somebody else's material by mistake is the
 * expensive error.
 */

import { useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { useCrumb } from '../../components/ui/breadcrumbs';

import { api } from '../../api/client';
import { errorMessage, useAction, useList, useResource } from '../../api/hooks';
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
  Spinner,
} from '../../components/ui';
import { DataList, EmptyState, ListState, PageHeader, Sheet, Stat, StatusBadge } from '../../components/ui/data';
import type { Client, ItemType, Location } from '../settings/types';
import type { Movement, Reel, SerialUnit, StockBalance, StockCount } from '../receiving/types';

/* -------------------------------------------------------------------------- */
/* Stock on hand                                                              */
/* -------------------------------------------------------------------------- */

export default function StockPage() {
  const navigate = useNavigate();
  const [lookupError, setLookupError] = useState<string | null>(null);
  const [itemFilter, setItemFilter] = useState('');
  const [nodeFilter, setNodeFilter] = useState('');
  const [includeUnavailable, setIncludeUnavailable] = useState(false);

  const items = useList<ItemType>('item-types', { page_size: 500 });
  const nodes = useList<{ id: number; label: string; type: string }>('stock-nodes', {
    page_size: 200,
  });
  const stock = useResource<{ results: StockBalance[] }>('stock', {
    item_type: itemFilter || undefined,
    node: nodeFilter || undefined,
    available_only: includeUnavailable ? 'false' : undefined,
  });

  /** D7, E2: one identifier, whatever kind it turns out to be. */
  async function lookup(value: string) {
    setLookupError(null);
    try {
      const found = await api.get<{ kind: string; resource: string }>(
        `/stock/lookup?q=${encodeURIComponent(value)}`,
      );
      navigate(found.resource);
    } catch {
      // A 404 here means the identifier is not ours — which reads the same as
      // another tenant's (§2.4). Say so plainly rather than showing an error.
      setLookupError(`Nothing here matches "${value}".`);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Stock"
        subtitle="What we have, where it is, and whose it is."
        actions={
          <>
            <Link
              to="/stock/transfers"
              className="flex min-h-[44px] items-center px-2 text-sm text-slate-700"
            >
              Transfers
            </Link>
            <Link
              to="/stock/counts"
              className="flex min-h-[44px] items-center px-2 text-sm text-slate-700"
            >
              Counts
            </Link>
          </>
        }
      />

      <Card className="flex flex-col gap-2">
        <h2 className="text-sm font-semibold text-slate-900">Find a unit or a drum</h2>
        <p className="text-sm text-slate-600">
          Scan or type a serial number, an asset tag or a drum number. It goes
          straight to that item's history.
        </p>
        <BarcodeScanner label="Serial, asset tag or drum" onScan={lookup} />
        {lookupError ? <Banner tone="warning">{lookupError}</Banner> : null}
      </Card>

      <Card className="grid gap-3 sm:grid-cols-3">
        <Field label="Item" htmlFor="stock-item">
          <Select
            id="stock-item"
            value={itemFilter}
            onChange={(event) => setItemFilter(event.target.value)}
          >
            <option value="">Everything</option>
            {(items.data?.results ?? []).map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </Select>
        </Field>

        <Field label="Where" htmlFor="stock-node">
          <Select
            id="stock-node"
            value={nodeFilter}
            onChange={(event) => setNodeFilter(event.target.value)}
          >
            <option value="">Everywhere</option>
            {(nodes.data?.results ?? []).map((node) => (
              <option key={node.id} value={node.id}>
                {node.label}
              </option>
            ))}
          </Select>
        </Field>

        <Field
          label="Include"
          htmlFor="stock-scope"
          hint="Quarantined stock is never counted as available."
        >
          <Select
            id="stock-scope"
            value={includeUnavailable ? 'all' : 'available'}
            onChange={(event) => setIncludeUnavailable(event.target.value === 'all')}
          >
            <option value="available">Available to issue</option>
            <option value="all">Everything, including quarantine and custody</option>
          </Select>
        </Field>
      </Card>

      <ListState query={stock}>
        <DataList
          rows={stock.data?.results ?? []}
          rowKey={(row) => row.id}
          empty={
            <EmptyState
              title="Nothing in stock matching that."
              hint="Receiving a delivery is what puts stock here."
            />
          }
          columns={[
            { header: 'Item', cell: (row) => row.item_name },
            { header: 'Quantity', cell: (row) => `${row.quantity} ${row.uom}` },
            { header: 'Where', cell: (row) => row.node_label },
            {
              header: 'Owner',
              // E1: distinct wherever it appears, not only on the stock screen.
              cell: (row) => <OwnershipBadge client={row.owner_client_name || null} />,
            },
            {
              header: 'Condition',
              cell: (row) => row.condition.replaceAll('_', ' ').toLowerCase(),
              wideOnly: true,
            },
          ]}
        />
      </ListState>

      <LowStockCard />
    </div>
  );
}

function LowStockCard() {
  const low = useResource<{ results: Record<string, string>[] }>('stock/low');
  const rows = low.data?.results ?? [];

  if (low.isLoading || rows.length === 0) return null;

  return (
    <Card className="flex flex-col gap-2 border-amber-300 bg-amber-50">
      <h2 className="text-sm font-semibold text-amber-900">Below reorder level</h2>
      <ul className="flex flex-col gap-1 text-sm text-amber-900">
        {rows.map((row) => (
          <li key={String(row.item_type_id ?? row.item)}>
            {row.item}: {row.available} {row.uom} on hand, reorder at {row.minimum}
          </li>
        ))}
      </ul>
    </Card>
  );
}

/* -------------------------------------------------------------------------- */
/* Serial history                                                             */
/* -------------------------------------------------------------------------- */

export function SerialHistoryPage() {
  const { serialNumber } = useParams();
  // A serial is its own name, so there is nothing to wait for.
  useCrumb(serialNumber);
  const history = useResource<{ unit: SerialUnit; movements: Movement[] }>(
    `stock/serials/${encodeURIComponent(serialNumber ?? '')}/history`,
  );

  if (history.isLoading) return <Spinner className="text-slate-400" />;
  if (history.isError) {
    return (
      <div className="flex flex-col gap-3">
        <Banner tone="warning">
          No unit here has that serial number. It may belong to another
          organization, in which case it is deliberately invisible.
        </Banner>
        <Link to="/stock" className="text-sm text-slate-700 underline">
          Back to stock
        </Link>
      </div>
    );
  }

  const { unit, movements } = history.data!;

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={unit.serial_number}
        subtitle={
          <span className="flex flex-wrap items-center gap-2">
            {unit.item_name}
            <StatusBadge status={unit.status} />
            <OwnershipBadge client={unit.owner_client_name || null} />
          </span>
        }
        actions={
          <Link to="/stock" className="flex min-h-[44px] items-center px-2 text-sm text-slate-700">
            Back
          </Link>
        }
      />

      <div className="grid gap-3 sm:grid-cols-3">
        <Stat label="Where it is now" value={unit.node_label} />
        <Stat label="Condition" value={unit.condition.replaceAll('_', ' ').toLowerCase()} />
        <Stat label="Asset tag" value={unit.asset_tag || '—'} />
      </div>

      {unit.origin_site_ref ? (
        <Banner tone="info">
          Recovered from {unit.origin_site_ref}.
        </Banner>
      ) : null}

      <Card className="flex flex-col gap-2">
        {/* E2: "the single most likely question from an operator audit." Oldest
            first, because the story reads forwards. */}
        <h2 className="text-sm font-semibold text-slate-900">Everywhere it has been</h2>
        <MovementList movements={movements} />
      </Card>
    </div>
  );
}

export function DrumHistoryPage() {
  const { drumNumber } = useParams();
  useCrumb(drumNumber);
  const history = useResource<{ reel: Reel; movements: Movement[] }>(
    `stock/drums/${encodeURIComponent(drumNumber ?? '')}/history`,
  );

  if (history.isLoading) return <Spinner className="text-slate-400" />;
  if (history.isError) {
    return <Banner tone="warning">No drum here has that number.</Banner>;
  }

  const { reel, movements } = history.data!;
  const used = Number(reel.initial_length) - Number(reel.remaining_length);

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={reel.drum_number}
        subtitle={
          <span className="flex flex-wrap items-center gap-2">
            {reel.item_name}
            <StatusBadge status={reel.status} />
            <OwnershipBadge client={reel.owner_client_name || null} />
          </span>
        }
        actions={
          <Link to="/stock" className="flex min-h-[44px] items-center px-2 text-sm text-slate-700">
            Back
          </Link>
        }
      />

      <div className="grid gap-3 sm:grid-cols-3">
        <Stat
          label="Remaining"
          value={`${reel.remaining_length} ${reel.uom}`}
          tone={Number(reel.remaining_length) === 0 ? 'bad' : 'good'}
        />
        <Stat label="Used so far" value={`${used.toFixed(3)} ${reel.uom}`} />
        <Stat label="Where it is" value={reel.node_label} />
      </div>

      <Card className="flex flex-col gap-2">
        <h2 className="text-sm font-semibold text-slate-900">Every draw off this drum</h2>
        <MovementList movements={movements} />
      </Card>
    </div>
  );
}

function MovementList({ movements }: { movements: Movement[] }) {
  return (
    <DataList
      rows={movements}
      rowKey={(row) => row.id}
      empty={<EmptyState title="No movements recorded." />}
      columns={[
        { header: 'When', cell: (row) => row.occurred_at.slice(0, 16).replace('T', ' ') },
        { header: 'What', cell: (row) => row.movement_type.toLowerCase() },
        { header: 'Quantity', cell: (row) => `${row.quantity} ${row.uom}` },
        { header: 'From', cell: (row) => row.from_label },
        { header: 'To', cell: (row) => row.to_label },
        {
          header: 'Document',
          wideOnly: true,
          cell: (row) => row.document_number || row.document_type || '—',
        },
        { header: 'By', cell: (row) => row.posted_by_name || '—', wideOnly: true },
      ]}
    />
  );
}

/* -------------------------------------------------------------------------- */
/* Drums list                                                                 */
/* -------------------------------------------------------------------------- */

export function DrumsPage() {
  const navigate = useNavigate();
  const [search, setSearch] = useState('');
  const drums = useList<Reel>('drums', { search: search || undefined, page_size: 100 });

  return (
    <div className="flex flex-col gap-4">
      <PageHeader title="Drums" subtitle="Every reel, with what is left on it." />

      <Input
        className="max-w-sm"
        aria-label="Search drums"
        placeholder="Drum number"
        value={search}
        onChange={(event) => setSearch(event.target.value)}
      />

      {drums.isLoading ? (
        <Spinner className="text-slate-400" />
      ) : (
        <DataList
          rows={drums.data?.results ?? []}
          rowKey={(row) => row.id}
          onRowClick={(row) => navigate(`/stock/drums/${row.drum_number}`)}
          empty={<EmptyState title="No drums yet." />}
          columns={[
            { header: 'Drum', cell: (row) => row.drum_number },
            { header: 'Cable', cell: (row) => row.item_name },
            {
              header: 'Remaining',
              cell: (row) => (
                <span className={Number(row.remaining_length) === 0 ? 'text-slate-400' : ''}>
                  {row.remaining_length} {row.uom}
                </span>
              ),
            },
            { header: 'Started at', cell: (row) => `${row.initial_length} ${row.uom}`, wideOnly: true },
            { header: 'Where', cell: (row) => row.node_label },
            { header: 'Status', cell: (row) => <StatusBadge status={row.status} /> },
            {
              header: 'Owner',
              cell: (row) => <OwnershipBadge client={row.owner_client_name || null} />,
              wideOnly: true,
            },
          ]}
        />
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Transfers (E4)                                                             */
/* -------------------------------------------------------------------------- */

export function TransfersPage() {
  const [banner, setBanner] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const [form, setForm] = useState({
    item_type: '',
    quantity: '',
    from_location: '',
    to_location: '',
    note: '',
  });

  const items = useList<ItemType>('item-types', { page_size: 500 });
  const locations = useList<Location>('locations', { page_size: 200 });
  const transfer = useAction<Record<string, unknown>, Movement>({
    resource: 'stock/transfers',
    invalidates: ['stock', 'movements'],
  });

  async function submit() {
    setBanner(null);
    setDone(null);
    try {
      const movement = await transfer.mutateAsync({
        item_type: Number(form.item_type),
        quantity: form.quantity,
        from_location: Number(form.from_location),
        to_location: Number(form.to_location),
        note: form.note,
      });
      setDone(`${movement.quantity} ${movement.uom} moved to ${movement.to_label}.`);
      setForm({ ...form, quantity: '', note: '' });
    } catch (error) {
      // E4's refusal names the alternative — raise a gate-out — so it is shown
      // verbatim rather than replaced with a generic failure.
      setBanner(errorMessage(error));
    }
  }

  const insideYard = (locations.data?.results ?? []).filter(
    (location) => location.type !== 'VEHICLE',
  );

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Move stock"
        subtitle="Between places inside the yard. Anything leaving needs a gate-out."
      />

      {banner ? <Banner tone="error">{banner}</Banner> : null}
      {done ? <Banner tone="info">{done}</Banner> : null}

      <Card className="flex flex-col gap-3">
        <Field label="Item" htmlFor="tr-item">
          <Select
            id="tr-item"
            value={form.item_type}
            onChange={(event) => setForm({ ...form, item_type: event.target.value })}
          >
            <option value="">Choose…</option>
            {(items.data?.results ?? []).map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </Select>
        </Field>

        <Field label="Quantity" htmlFor="tr-quantity">
          <Input
            id="tr-quantity"
            inputMode="decimal"
            value={form.quantity}
            onChange={(event) => setForm({ ...form, quantity: event.target.value })}
          />
        </Field>

        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="From" htmlFor="tr-from">
            <Select
              id="tr-from"
              value={form.from_location}
              onChange={(event) => setForm({ ...form, from_location: event.target.value })}
            >
              <option value="">Choose…</option>
              {insideYard.map((location) => (
                <option key={location.id} value={location.id}>
                  {location.name}
                </option>
              ))}
            </Select>
          </Field>

          <Field label="To" htmlFor="tr-to">
            <Select
              id="tr-to"
              value={form.to_location}
              onChange={(event) => setForm({ ...form, to_location: event.target.value })}
            >
              <option value="">Choose…</option>
              {insideYard.map((location) => (
                <option key={location.id} value={location.id}>
                  {location.name}
                </option>
              ))}
            </Select>
          </Field>
        </div>

        <Field label="Note" htmlFor="tr-note">
          <Input
            id="tr-note"
            value={form.note}
            onChange={(event) => setForm({ ...form, note: event.target.value })}
          />
        </Field>

        <Button
          loading={transfer.isPending}
          disabled={!form.item_type || !form.quantity || !form.from_location || !form.to_location}
          onClick={submit}
        >
          Move it
        </Button>
      </Card>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Counts (E5)                                                                */
/* -------------------------------------------------------------------------- */

export function CountsPage() {
  const navigate = useNavigate();
  const [sheet, setSheet] = useState(false);
  const counts = useList<StockCount>('stock-counts', { page_size: 50 });

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Stock counts"
        subtitle="Counted against a location, posted with a reason per difference."
        actions={<Button onClick={() => setSheet(true)}>Start a count</Button>}
      />

      {counts.isLoading ? (
        <Spinner className="text-slate-400" />
      ) : (
        <DataList
          rows={counts.data?.results ?? []}
          rowKey={(row) => row.id}
          onRowClick={(row) => navigate(`/stock/counts/${row.id}`)}
          empty={
            <EmptyState
              title="No counts yet."
              hint="A count records what was expected at the moment of counting, so the variance is against that figure."
            />
          }
          columns={[
            { header: 'Number', cell: (row) => row.number || 'draft' },
            { header: 'Where', cell: (row) => row.location_name },
            { header: 'Status', cell: (row) => <StatusBadge status={row.status} /> },
            { header: 'Lines', cell: (row) => row.lines.length },
            {
              header: 'Counted',
              cell: (row) => row.counted_at?.slice(0, 10) ?? '—',
              wideOnly: true,
            },
          ]}
        />
      )}

      <NewCountSheet open={sheet} onClose={() => setSheet(false)} />
    </div>
  );
}

function NewCountSheet({ open, onClose }: { open: boolean; onClose: () => void }) {
  const navigate = useNavigate();
  const [location, setLocation] = useState('');
  const [banner, setBanner] = useState<string | null>(null);
  const locations = useList<Location>('locations', { page_size: 200 });
  const create = useAction<Record<string, unknown>, StockCount>({ resource: 'stock-counts' });

  return (
    <Sheet
      open={open}
      title="Start a count"
      onClose={onClose}
      footer={
        <>
          <Button variant="secondary" block onClick={onClose}>
            Cancel
          </Button>
          <Button
            block
            disabled={!location}
            loading={create.isPending}
            onClick={async () => {
              setBanner(null);
              try {
                const count = await create.mutateAsync({
                  location: Number(location),
                  counted_at: new Date().toISOString(),
                });
                onClose();
                navigate(`/stock/counts/${count.id}`);
              } catch (error) {
                setBanner(errorMessage(error));
              }
            }}
          >
            Start
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        {banner ? <Banner tone="error">{banner}</Banner> : null}
        <Field label="Counting" htmlFor="count-location">
          <Select
            id="count-location"
            value={location}
            onChange={(event) => setLocation(event.target.value)}
          >
            <option value="">Choose…</option>
            {(locations.data?.results ?? []).map((entry) => (
              <option key={entry.id} value={entry.id}>
                {entry.name}
              </option>
            ))}
          </Select>
        </Field>
      </div>
    </Sheet>
  );
}

export function CountDetailPage() {
  const { id } = useParams();
  const count = useResource<StockCount>(`stock-counts/${id}`);
  useCrumb(count.data?.number);
  const [banner, setBanner] = useState<string | null>(null);
  const [line, setLine] = useState({ item_type: '', counted_quantity: '', reason: '' });

  const items = useList<ItemType>('item-types', { page_size: 500 });
  const clients = useList<Client>('clients', { page_size: 200 });
  const addLine = useAction<Record<string, unknown>, StockCount>({
    resource: 'stock-counts',
    path: () => `${id}/lines`,
    invalidates: ['stock-counts'],
  });
  const post = useAction<Record<string, unknown>, StockCount>({
    resource: 'stock-counts',
    path: () => `${id}/post`,
    invalidates: ['stock-counts', 'stock', 'movements'],
  });

  if (count.isLoading) return <Spinner className="text-slate-400" />;
  if (count.isError) return <Banner tone="error">{errorMessage(count.error)}</Banner>;

  const document = count.data!;
  const isDraft = document.status === 'DRAFT';
  const withVariance = document.lines.filter((row) => Number(row.variance) !== 0);
  void clients;

  return (
    <div className="flex flex-col gap-4 pb-24">
      <PageHeader
        title={document.number || 'Draft count'}
        subtitle={
          <span className="flex items-center gap-2">
            <StatusBadge status={document.status} />
            {document.location_name}
          </span>
        }
        actions={
          <Link
            to="/stock/counts"
            className="flex min-h-[44px] items-center px-2 text-sm text-slate-700"
          >
            Back
          </Link>
        }
      />

      {banner ? <Banner tone="error">{banner}</Banner> : null}

      {isDraft ? (
        <Card className="flex flex-col gap-3">
          <h2 className="text-sm font-semibold text-slate-900">Count a line</h2>
          <p className="text-sm text-slate-600">
            What the system expected is captured as each line is added, so the
            difference is measured against the figure being disputed rather than
            against today's.
          </p>

          <Field label="Item" htmlFor="cl-item">
            <Select
              id="cl-item"
              value={line.item_type}
              onChange={(event) => setLine({ ...line, item_type: event.target.value })}
            >
              <option value="">Choose…</option>
              {(items.data?.results ?? []).map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </Select>
          </Field>

          <Field label="Counted" htmlFor="cl-qty">
            <Input
              id="cl-qty"
              inputMode="decimal"
              value={line.counted_quantity}
              onChange={(event) => setLine({ ...line, counted_quantity: event.target.value })}
            />
          </Field>

          <Field
            label="Reason for any difference"
            htmlFor="cl-reason"
            hint="Required by the posting step for every line that differs."
          >
            <Input
              id="cl-reason"
              value={line.reason}
              onChange={(event) => setLine({ ...line, reason: event.target.value })}
            />
          </Field>

          <Button
            variant="secondary"
            loading={addLine.isPending}
            disabled={!line.item_type || !line.counted_quantity}
            onClick={async () => {
              setBanner(null);
              try {
                await addLine.mutateAsync({
                  item_type: Number(line.item_type),
                  counted_quantity: line.counted_quantity,
                  reason: line.reason,
                });
                setLine({ item_type: '', counted_quantity: '', reason: '' });
                await count.refetch();
              } catch (error) {
                setBanner(errorMessage(error));
              }
            }}
          >
            Add line
          </Button>
        </Card>
      ) : null}

      <Card className="flex flex-col gap-2">
        <h2 className="text-sm font-semibold text-slate-900">Counted</h2>
        <DataList
          rows={document.lines}
          rowKey={(row) => row.id}
          empty={<EmptyState title="Nothing counted yet." />}
          columns={[
            { header: 'Item', cell: (row) => row.item_name },
            { header: 'Expected', cell: (row) => `${row.expected_quantity} ${row.uom}` },
            { header: 'Counted', cell: (row) => `${row.counted_quantity} ${row.uom}` },
            {
              header: 'Difference',
              cell: (row) => (
                <span
                  className={
                    Number(row.variance) === 0
                      ? 'text-slate-500'
                      : Number(row.variance) < 0
                        ? 'text-red-700'
                        : 'text-amber-700'
                  }
                >
                  {row.variance}
                </span>
              ),
            },
            { header: 'Reason', cell: (row) => row.reason || '—', wideOnly: true },
          ]}
        />
        {withVariance.length > 0 ? (
          <p className="text-sm text-amber-800">
            {withVariance.length} {withVariance.length === 1 ? 'line differs' : 'lines differ'}.
            Posting writes an adjustment for each, with its reason. A difference on
            client-owned stock always needs an approval, whatever the rules say.
          </p>
        ) : null}
      </Card>

      {/* `ActionBar`, not a hand-rolled fixed bar. A `fixed bottom-0` div sits
          *under* the shell's phone tab bar, which is also fixed at the bottom —
          so on the device this screen was designed for, its primary button was
          covered and unreachable. Found by running T8.13 on a phone viewport.
          `ActionBar` is sticky, so it stacks above the content and below the
          tabs. */}
      {isDraft ? (
        <ActionBar>
          <Button
            block
            loading={post.isPending}
            disabled={document.lines.length === 0}
            onClick={async () => {
              setBanner(null);
              try {
                await post.mutateAsync({});
                await count.refetch();
              } catch (error) {
                setBanner(errorMessage(error));
              }
            }}
          >
            Post the count
          </Button>
        </ActionBar>
      ) : null}
    </div>
  );
}
