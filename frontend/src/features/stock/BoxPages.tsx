/**
 * T11.14 — box screens (design §4.15.10; P4, P7, P8).
 *
 * A box is a projection beside the ledger, so nothing here moves stock in the
 * accounting sense: take-out leaves the contents where they are, empty closes
 * the box, and move relocates the box and everything in it (E4's rule that
 * leaving the yard needs a gate-out is enforced by the server, and the location
 * choice offers only places inside it).
 */

import { useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';

import { api } from '../../api/client';
import { errorMessage, useAction, useDetail, useList, type Page } from '../../api/hooks';
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
} from '../../components/ui';
import { useCrumb } from '../../components/ui/breadcrumbs';
import {
  DataList,
  EmptyState,
  ListState,
  PageHeader,
  Sheet,
  Stat,
  StatusBadge,
} from '../../components/ui/data';
import type { Location } from '../settings/types';
import {
  buildTakeOutBody,
  bulkCountText,
  bulkKey,
  bulkProblem,
  documentLink,
  eventSubject,
  isEmptyTakeOut,
  parentText,
  pathText,
  takeOutSummary,
  trimQuantity,
  unitsCountText,
  type BoxDetail,
  type BoxEvent,
  type BoxNode,
  type BoxRow,
} from './boxHelpers';

const boxPath = (code: string) => `/stock/boxes/${encodeURIComponent(code)}`;
const when = (iso: string) => iso.slice(0, 16).replace('T', ' ');

/* -------------------------------------------------------------------------- */
/* List                                                                       */
/* -------------------------------------------------------------------------- */

export function BoxesPage() {
  const navigate = useNavigate();
  const [search, setSearch] = useState('');
  const [status, setStatus] = useState('OPEN');
  const [location, setLocation] = useState('');

  const locations = useList<Location>('locations', { page_size: 200 });
  const boxes = useList<BoxRow>('boxes', {
    search: search || undefined,
    status: status || undefined,
    location: location || undefined,
    page_size: 100,
  });

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Boxes"
        subtitle="Pallets, cartons and bags, and what is inside them."
        actions={
          <Link to="/stock" className="flex min-h-[44px] items-center px-2 text-sm text-slate-700">
            Back
          </Link>
        }
      />

      <Card className="grid gap-3 sm:grid-cols-3">
        <Field label="Code" htmlFor="box-search">
          <Input
            id="box-search"
            placeholder="Box code"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
        </Field>
        <Field label="Status" htmlFor="box-status">
          <Select id="box-status" value={status} onChange={(event) => setStatus(event.target.value)}>
            <option value="OPEN">Open</option>
            <option value="CLOSED">Closed</option>
            <option value="">Open and closed</option>
          </Select>
        </Field>
        <Field label="Where" htmlFor="box-location">
          <Select
            id="box-location"
            value={location}
            onChange={(event) => setLocation(event.target.value)}
          >
            <option value="">Everywhere</option>
            {(locations.data?.results ?? []).map((entry) => (
              <option key={entry.id} value={entry.id}>
                {entry.name}
              </option>
            ))}
          </Select>
        </Field>
      </Card>

      <ListState query={boxes}>
        <DataList
          rows={boxes.data?.results ?? []}
          rowKey={(row) => row.id}
          onRowClick={(row) => navigate(boxPath(row.code))}
          empty={
            <EmptyState
              title="No boxes match that."
              hint="A box is made when a delivery is received into one."
            />
          }
          columns={[
            {
              header: 'Box',
              cell: (row) => (
                <span className="flex flex-col">
                  <span className="font-medium">{row.code}</span>
                  {row.parent_code ? (
                    <span className="text-xs text-slate-500">{parentText(row.parent_code)}</span>
                  ) : null}
                </span>
              ),
            },
            { header: 'Where', cell: (row) => row.node_label },
            { header: 'Status', cell: (row) => <StatusBadge status={row.status} /> },
            {
              header: 'Units now',
              cell: (row) => row.units_now,
            },
            { header: 'Bulk lines', cell: (row) => row.bulk_lines_now, wideOnly: true },
          ]}
        />
      </ListState>
      {boxes.data?.next ? (
        <p className="text-sm text-slate-500">
          Showing the first 100. Narrow the search to see the rest.
        </p>
      ) : null}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Detail                                                                     */
/* -------------------------------------------------------------------------- */

export function BoxPage() {
  const params = useParams();
  const code = params.code ?? '';
  const { hasAny } = useSession();
  const canChange = hasAny(PERM.STOCK_ADJUST, PERM.GATE_IN_POST);

  const detail = useDetail<BoxDetail>('boxes', encodeURIComponent(code));
  useCrumb(detail.data?.code ?? code);

  const [sheet, setSheet] = useState<'take' | 'empty' | 'move' | null>(null);
  const [fresh, setFresh] = useState<BoxDetail | null>(null);
  const [banner, setBanner] = useState<string | null>(null);

  if (detail.isLoading) return <Spinner className="text-slate-400" />;
  if (detail.isError || !detail.data) {
    return (
      <div className="flex flex-col gap-3">
        <Banner tone="warning">No box here has that code.</Banner>
        <Link to="/stock/boxes" className="text-sm text-slate-700 underline">
          Back to boxes
        </Link>
      </div>
    );
  }

  // The action's own response is the freshest view; the refetch catches up behind it.
  const box = fresh && fresh.id === detail.data.id && fresh.code === detail.data.code
    ? fresh
    : detail.data;
  const isOpen = box.status === 'OPEN';

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={box.code}
        subtitle={
          <span className="flex flex-wrap items-center gap-2">
            <StatusBadge status={box.status} />
            {box.node_label}
            {box.parent_code ? (
              <Link to={boxPath(box.parent_code)} className="underline">
                {parentText(box.parent_code)}
              </Link>
            ) : null}
          </span>
        }
        actions={
          <Link
            to="/stock/boxes"
            className="flex min-h-[44px] items-center px-2 text-sm text-slate-700"
          >
            Back
          </Link>
        }
      />

      {banner ? <Banner tone="info">{banner}</Banner> : null}
      {!isOpen ? (
        <Banner tone="info">
          This box is closed{box.closed_at ? ` (${when(box.closed_at)})` : ''}. Its code is not
          used again, and its history stays here.
        </Banner>
      ) : null}

      <div className="grid gap-3 sm:grid-cols-3">
        <Stat label="Where it is" value={box.node_label} />
        <Stat label="Path" value={pathText(box.path)} />
        <Stat
          label="Received on"
          value={
            box.gate_in && box.gate_in_number ? (
              <Link to={`/gate-in/${box.gate_in}`} className="underline">
                {box.gate_in_number}
              </Link>
            ) : (
              'Made here'
            )
          }
        />
      </div>

      <Card className="flex flex-col gap-1">
        <p className="text-sm text-slate-900">{unitsCountText(box.counts)}</p>
        <p className="text-sm text-slate-900">{bulkCountText(box.counts)}</p>
      </Card>

      {canChange && isOpen ? (
        <div className="flex flex-wrap gap-2">
          <Button variant="secondary" onClick={() => setSheet('take')}>
            Take out
          </Button>
          <Button variant="secondary" onClick={() => setSheet('move')}>
            Move
          </Button>
          <Button variant="danger" onClick={() => setSheet('empty')}>
            Empty the box
          </Button>
        </div>
      ) : null}

      <Card className="flex flex-col gap-2">
        <h2 className="text-sm font-semibold text-slate-900">What is in it</h2>
        <BoxTree node={box} root />
      </Card>

      <Card className="flex flex-col gap-2">
        <h2 className="text-sm font-semibold text-slate-900">History</h2>
        <p className="text-xs text-slate-500">Includes every box inside this one.</p>
        <BoxHistory code={box.code} />
      </Card>

      {canChange && isOpen ? (
        <>
          <TakeOutSheet
            open={sheet === 'take'}
            box={box}
            onClose={() => setSheet(null)}
            onDone={(next) => {
              setFresh(next);
              setBanner('Taken out. Those items stay where they are, no longer in this box.');
              setSheet(null);
            }}
          />
          <EmptySheet
            open={sheet === 'empty'}
            box={box}
            onClose={() => setSheet(null)}
            onDone={(next) => {
              setFresh(next);
              setBanner('The box is empty and now closed.');
              setSheet(null);
            }}
          />
          <MoveSheet
            open={sheet === 'move'}
            box={box}
            onClose={() => setSheet(null)}
            onDone={(next) => {
              setFresh(next);
              setBanner(`Moved to ${next.node_label}.`);
              setSheet(null);
            }}
          />
        </>
      ) : null}
    </div>
  );
}

/** Everything a box action can change: the box, stock, and unit histories. */
function useBoxInvalidation() {
  const queryClient = useQueryClient();
  return () =>
    queryClient.invalidateQueries({
      predicate: (query) => {
        const head = String(query.queryKey[0] ?? '');
        return head === 'boxes' || head === 'serials' || head.startsWith('stock');
      },
    });
}

/* -------------------------------------------------------------------------- */
/* Tree                                                                       */
/* -------------------------------------------------------------------------- */

function BoxTree({ node, root = false }: { node: BoxNode; root?: boolean }) {
  const [open, setOpen] = useState(root);

  return (
    <div className={root ? 'flex flex-col gap-2' : 'flex flex-col gap-2 border-l-2 border-slate-200 pl-3'}>
      {!root ? (
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            aria-expanded={open}
            onClick={() => setOpen((value) => !value)}
            className="flex min-h-[44px] items-center gap-2 text-left text-sm font-medium text-slate-900"
          >
            <span aria-hidden>{open ? '▾' : '▸'}</span>
            {node.code}
          </button>
          <StatusBadge status={node.status} />
          <Link to={boxPath(node.code)} className="flex min-h-[44px] items-center text-sm underline">
            Open
          </Link>
          <span className="text-xs text-slate-500">{unitsCountText(node.counts)}</span>
        </div>
      ) : null}

      {open ? (
        <>
          {node.units.length > 0 ? (
            <ul className="flex flex-col divide-y divide-slate-100">
              {node.units.map((unit) => (
                <li key={unit.id} className="flex flex-wrap items-center gap-x-3 py-2 text-sm">
                  <Link to={`/stock/serials/${encodeURIComponent(unit.serial_number)}`} className="font-medium underline">
                    {unit.serial_number}
                  </Link>
                  <span className="text-slate-700">{unit.item_name}</span>
                  <StatusBadge status={unit.status} />
                </li>
              ))}
            </ul>
          ) : null}

          {node.bulk.length > 0 ? (
            <ul className="flex flex-col divide-y divide-slate-100">
              {node.bulk.map((line) => (
                <li key={bulkKey(line)} className="flex flex-wrap items-center gap-x-3 py-2 text-sm">
                  <span className="font-medium">
                    {trimQuantity(line.quantity)} {line.uom}
                  </span>
                  <span className="text-slate-700">{line.item_name}</span>
                  {/* E1: client-owned stock is marked wherever it appears. */}
                  <OwnershipBadge client={line.owner_name || null} />
                </li>
              ))}
            </ul>
          ) : null}

          {node.children.map((child) => (
            <BoxTree key={child.id} node={child} />
          ))}

          {node.units.length === 0 && node.bulk.length === 0 && node.children.length === 0 ? (
            <p className="text-sm text-slate-500">Nothing in it now.</p>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* History                                                                    */
/* -------------------------------------------------------------------------- */

/** One cursor page of events; "Older" mounts the next page beneath it. */
function BoxHistory({ code, cursor }: { code: string; cursor?: string }) {
  const [older, setOlder] = useState(false);
  const events = useQuery({
    queryKey: ['boxes', 'history', code, cursor ?? ''],
    queryFn: () =>
      api.get<Page<BoxEvent>>(
        `/boxes/${encodeURIComponent(code)}/history?page_size=25${
          cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''
        }`,
      ),
  });

  if (events.isLoading) return <Spinner className="text-slate-400" />;
  if (events.isError) return <Banner tone="error">{errorMessage(events.error)}</Banner>;

  const rows = events.data?.results ?? [];
  const next = events.data?.next ? new URL(events.data.next, window.location.origin).searchParams.get('cursor') : null;

  return (
    <>
      {rows.length === 0 && !cursor ? <EmptyState title="Nothing has happened to it yet." /> : null}
      <ul className="flex flex-col divide-y divide-slate-100">
        {rows.map((event) => {
          const link = documentLink(event);
          return (
            <li key={event.id} className="flex flex-col gap-0.5 py-2 text-sm">
              <span className="flex flex-wrap items-center gap-x-2">
                <span className="font-medium text-slate-900">{event.action_label}</span>
                <span className="text-slate-700">{eventSubject(event)}</span>
                {event.owner_client ? <OwnershipBadge client={event.owner_client} /> : null}
              </span>
              <span className="text-xs text-slate-500">
                {when(event.occurred_at)}
                {event.actor ? ` · ${event.actor}` : ''}
                {event.box_code !== code ? ` · in ${event.box_code}` : ''}
                {event.document_number ? ' · ' : ''}
                {event.document_number ? (
                  link ? (
                    <Link to={link} className="underline">
                      {event.document_number}
                    </Link>
                  ) : (
                    event.document_number
                  )
                ) : null}
              </span>
              {event.note ? <span className="text-xs text-slate-600">{event.note}</span> : null}
            </li>
          );
        })}
      </ul>
      {next ? (
        older ? (
          <BoxHistory code={code} cursor={next} />
        ) : (
          <Button variant="secondary" onClick={() => setOlder(true)}>
            Older
          </Button>
        )
      ) : null}
    </>
  );
}

/* -------------------------------------------------------------------------- */
/* Actions                                                                    */
/* -------------------------------------------------------------------------- */

interface SheetProps {
  open: boolean;
  box: BoxDetail;
  onClose: () => void;
  onDone: (next: BoxDetail) => void;
}

function TakeOutSheet({ open, box, onClose, onDone }: SheetProps) {
  const [unitIds, setUnitIds] = useState<number[]>([]);
  const [boxCodes, setBoxCodes] = useState<string[]>([]);
  const [quantities, setQuantities] = useState<Record<string, string>>({});
  const [banner, setBanner] = useState<string | null>(null);
  const invalidate = useBoxInvalidation();
  const takeOut = useAction<unknown, BoxDetail>({
    resource: 'boxes',
    path: () => `${encodeURIComponent(box.code)}/take-out`,
    invalidates: ['boxes'],
  });

  // Take-out works on what is directly in this box. Deeper contents are taken
  // out from the box they sit in, which the tree links to.
  const openChildren = box.children.filter((child) => child.status === 'OPEN');
  const body = buildTakeOutBody(box, { unitIds, bulkQuantities: quantities, boxCodes });
  const empty = isEmptyTakeOut(body);

  const toggle = <T,>(list: T[], value: T): T[] =>
    list.includes(value) ? list.filter((entry) => entry !== value) : [...list, value];

  async function submit() {
    setBanner(null);
    const problem = bulkProblem(box, quantities);
    if (problem) {
      setBanner(problem);
      return;
    }
    try {
      const next = await takeOut.mutateAsync(body);
      await invalidate();
      setUnitIds([]);
      setBoxCodes([]);
      setQuantities({});
      onDone(next);
    } catch (error) {
      setBanner(errorMessage(error));
    }
  }

  return (
    <Sheet
      open={open}
      title="Take out of this box"
      onClose={onClose}
      footer={
        <>
          <Button variant="secondary" block onClick={onClose}>
            Cancel
          </Button>
          <Button block disabled={empty} loading={takeOut.isPending} onClick={submit}>
            {empty ? 'Take out' : `Take out ${takeOutSummary(body)}`}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        {banner ? <Banner tone="error">{banner}</Banner> : null}
        <p className="text-sm text-slate-600">
          What you take out stays where it is. It is just no longer in {box.code}.
        </p>

        {box.units.length > 0 ? (
          <fieldset className="flex flex-col">
            <legend className="text-sm font-semibold text-slate-900">Units</legend>
            {box.units.map((unit) => (
              <Checkbox
                key={unit.id}
                id={`take-unit-${unit.id}`}
                label={unit.serial_number}
                hint={unit.item_name}
                checked={unitIds.includes(unit.id)}
                onChange={() => setUnitIds((list) => toggle(list, unit.id))}
              />
            ))}
          </fieldset>
        ) : null}

        {box.bulk.length > 0 ? (
          <fieldset className="flex flex-col gap-2">
            <legend className="text-sm font-semibold text-slate-900">Bulk</legend>
            {box.bulk.map((line) => (
              <Field
                key={bulkKey(line)}
                label={`${line.item_name}${line.owner_name ? ` (${line.owner_name})` : ''}`}
                htmlFor={`take-bulk-${bulkKey(line)}`}
                hint={`${trimQuantity(line.quantity)} ${line.uom} in the box`}
              >
                <Input
                  id={`take-bulk-${bulkKey(line)}`}
                  inputMode="decimal"
                  placeholder="0"
                  value={quantities[bulkKey(line)] ?? ''}
                  onChange={(event) =>
                    setQuantities((current) => ({ ...current, [bulkKey(line)]: event.target.value }))
                  }
                />
              </Field>
            ))}
          </fieldset>
        ) : null}

        {openChildren.length > 0 ? (
          <fieldset className="flex flex-col">
            <legend className="text-sm font-semibold text-slate-900">Boxes inside</legend>
            {openChildren.map((child) => (
              <Checkbox
                key={child.id}
                id={`take-box-${child.id}`}
                label={child.code}
                hint={unitsCountText(child.counts)}
                checked={boxCodes.includes(child.code)}
                onChange={() => setBoxCodes((list) => toggle(list, child.code))}
              />
            ))}
          </fieldset>
        ) : null}

        {box.units.length === 0 && box.bulk.length === 0 && openChildren.length === 0 ? (
          <p className="text-sm text-slate-500">Nothing directly in it to take out.</p>
        ) : null}
      </div>
    </Sheet>
  );
}

function EmptySheet({ open, box, onClose, onDone }: SheetProps) {
  const [banner, setBanner] = useState<string | null>(null);
  const invalidate = useBoxInvalidation();
  const empty = useAction<unknown, BoxDetail>({
    resource: 'boxes',
    path: () => `${encodeURIComponent(box.code)}/empty`,
    invalidates: ['boxes'],
  });

  return (
    <Sheet
      open={open}
      title="Empty the box"
      onClose={onClose}
      footer={
        <>
          <Button variant="secondary" block onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="danger"
            block
            loading={empty.isPending}
            onClick={async () => {
              setBanner(null);
              try {
                const next = await empty.mutateAsync({});
                await invalidate();
                onDone(next);
              } catch (error) {
                setBanner(errorMessage(error));
              }
            }}
          >
            Empty {box.code}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        {banner ? <Banner tone="error">{banner}</Banner> : null}
        <p className="text-sm text-slate-700">
          Everything comes out of {box.code}, and the box closes. Its code cannot be used again.
          {box.children.length > 0 ? ' Boxes inside it come out too.' : ''} The stock itself stays
          where it is.
        </p>
        <p className="text-sm font-medium text-slate-900">{unitsCountText(box.counts)}</p>
      </div>
    </Sheet>
  );
}

function MoveSheet({ open, box, onClose, onDone }: SheetProps) {
  const [to, setTo] = useState('');
  const [banner, setBanner] = useState<string | null>(null);
  const invalidate = useBoxInvalidation();
  const locations = useList<Location>('locations', { page_size: 200 });
  const move = useAction<unknown, BoxDetail>({
    resource: 'boxes',
    path: () => `${encodeURIComponent(box.code)}/move`,
    invalidates: ['boxes'],
  });
  // E4: anything leaving the yard needs a gate-out, so vehicles are not offered.
  const insideYard = (locations.data?.results ?? []).filter(
    (location) => location.type !== 'VEHICLE' && location.is_active,
  );

  return (
    <Sheet
      open={open}
      title="Move the box"
      onClose={onClose}
      footer={
        <>
          <Button variant="secondary" block onClick={onClose}>
            Cancel
          </Button>
          <Button
            block
            disabled={!to}
            loading={move.isPending}
            onClick={async () => {
              setBanner(null);
              try {
                const next = await move.mutateAsync({ to_location: Number(to) });
                await invalidate();
                setTo('');
                onDone(next);
              } catch (error) {
                setBanner(errorMessage(error));
              }
            }}
          >
            Move it
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        {banner ? <Banner tone="error">{banner}</Banner> : null}
        <p className="text-sm text-slate-600">
          {box.code} and everything in it move together. Now at {box.node_label}.
        </p>
        <Field label="To" htmlFor="box-move-to" hint="Places inside the yard. Anything leaving needs a gate-out.">
          <Select id="box-move-to" value={to} onChange={(event) => setTo(event.target.value)}>
            <option value="">Choose…</option>
            {insideYard.map((location) => (
              <option key={location.id} value={location.id}>
                {location.name}
              </option>
            ))}
          </Select>
        </Field>
      </div>
    </Sheet>
  );
}
