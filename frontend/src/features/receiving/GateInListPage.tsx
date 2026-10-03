/**
 * Gate-in list and detail (design §7.4; D1–D8, M4).
 *
 * The detail screen is where posting and voiding happen, and both say what they
 * will do before they do it: posting creates stock, voiding reverses it and keeps
 * the number. A storekeeper should never be surprised by either.
 */

import { useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { useCrumb } from '../../components/ui/breadcrumbs';

import { errorMessage, useAction, useDetail, useList } from '../../api/hooks';
import { PhotoCapture } from '../../components/PhotoCapture';
import { Banner, Button, Card, Field, Input, Spinner } from '../../components/ui';
import { DataList, EmptyState, ListState, PageHeader, Sheet, StatusBadge } from '../../components/ui/data';
import { boxLabel, buildTree, describeCounts, type BoxNode, type DraftBox } from './gateInBoxes';
import type { GateIn, GateInLineInput } from './types';

export default function GateInListPage() {
  const navigate = useNavigate();
  const [search, setSearch] = useState('');
  const gateIns = useList<GateIn>('gate-ins', { search: search || undefined, page_size: 50 });

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Gate-in"
        subtitle="Everything received, and everything still a draft."
        actions={<Button onClick={() => navigate('/gate-in/new')}>Receive a delivery</Button>}
      />

      <Input
        className="max-w-sm"
        aria-label="Search deliveries"
        placeholder="Number, supplier or delivery note"
        value={search}
        onChange={(event) => setSearch(event.target.value)}
      />

      <ListState query={gateIns}>
        <DataList
          rows={gateIns.data?.results ?? []}
          rowKey={(row) => row.id}
          onRowClick={(row) => navigate(`/gate-in/${row.id}`)}
          empty={
            <EmptyState
              title="Nothing received yet."
              hint="A delivery becomes stock when it is posted."
              action={<Button onClick={() => navigate('/gate-in/new')}>Receive a delivery</Button>}
            />
          }
          columns={[
            { header: 'Number', cell: (row) => row.number || 'draft' },
            { header: 'Status', cell: (row) => <StatusBadge status={row.status} /> },
            {
              header: 'From',
              cell: (row) => row.supplier_name || row.client_name || row.origin_site_ref || '—',
            },
            { header: 'Into', cell: (row) => row.to_location_name ?? '—', wideOnly: true },
            { header: 'Lines', cell: (row) => row.lines.length },
            {
              header: 'Received',
              cell: (row) => row.received_at?.slice(0, 10) ?? '—',
              wideOnly: true,
            },
          ]}
        />
      </ListState>
    </div>
  );
}

export function GateInDetailPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const gateIn = useDetail<GateIn>('gate-ins', id);
  useCrumb(gateIn.data?.number);
  const [voidSheet, setVoidSheet] = useState(false);
  const [discardSheet, setDiscardSheet] = useState(false);
  const [reason, setReason] = useState('');
  const [banner, setBanner] = useState<string | null>(null);

  const post = useAction<{ id: string }, GateIn>({
    resource: 'gate-ins',
    path: (body) => `${body.id}/post`,
    invalidates: ['gate-ins', 'stock', 'movements', 'serials', 'drums'],
  });
  const voidIt = useAction<{ id: string; reason: string }, GateIn>({
    resource: 'gate-ins',
    path: (body) => `${body.id}/void`,
    invalidates: ['gate-ins', 'stock', 'movements', 'serials', 'drums'],
  });
  // Only ever reachable on a draft — the server refuses anything posted, since
  // that is in the ledger and is voided instead.
  const discard = useAction<{ id: string }, unknown>({
    resource: 'gate-ins',
    method: 'delete',
    path: (body) => String(body.id),
    invalidates: ['gate-ins'],
  });

  if (gateIn.isLoading) return <Spinner className="text-slate-400" />;
  if (gateIn.isError) return <Banner tone="error">{errorMessage(gateIn.error)}</Banner>;

  const document = gateIn.data!;
  const isDraft = document.status === 'DRAFT';
  // Boxes come back with their generated codes once posted (P10, 4.15.5).
  const boxes = document.boxes ?? [];
  const tree = buildTree(boxes, document.lines);

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={document.number || 'Draft delivery'}
        subtitle={
          <span className="flex items-center gap-2">
            <StatusBadge status={document.status} />
            {document.source_type.replaceAll('_', ' ').toLowerCase()}
          </span>
        }
        actions={
          <>
            <Link to="/gate-in" className="flex min-h-[44px] items-center px-2 text-sm text-slate-700">
              Back
            </Link>
            {isDraft ? (
              <>
                {/* A draft has moved nothing, so correcting one is ordinary
                    work — and until this existed the only way to fix a
                    mis-keyed quantity was to key the whole delivery again. */}
                <Link
                  to={`/gate-in/${document.id}/edit`}
                  className="flex min-h-[44px] items-center px-2 text-sm text-slate-700"
                >
                  Edit
                </Link>
                <Button
                  loading={post.isPending}
                  onClick={async () => {
                    setBanner(null);
                    try {
                      await post.mutateAsync({ id: String(document.id) });
                    } catch (error) {
                      setBanner(errorMessage(error));
                    }
                  }}
                >
                  Post
                </Button>
                <Button variant="ghost" className="text-red-700" onClick={() => setDiscardSheet(true)}>
                  Discard
                </Button>
              </>
            ) : null}
            {document.status === 'POSTED' ? (
              <Button variant="danger" onClick={() => setVoidSheet(true)}>
                Void
              </Button>
            ) : null}
          </>
        }
      />

      {banner ? <Banner tone="error">{banner}</Banner> : null}
      {isDraft ? (
        <Banner tone="warning">
          A draft has changed nothing. Posting is what creates the stock.
        </Banner>
      ) : null}
      {document.status === 'VOID' ? (
        <Banner tone="warning">
          Voided: {document.void_reason} — the movements were reversed, and the
          number stays used so the sequence has no hole.
        </Banner>
      ) : null}

      <Card className="grid gap-3 sm:grid-cols-2">
        <Detail label="Received into" value={document.to_location_name} />
        <Detail label="For site" value={document.for_site_name || 'Not for a particular site'} />
        <Detail label="Received at" value={document.received_at?.slice(0, 16).replace('T', ' ')} />
        <Detail
          label="From"
          value={document.supplier_name || document.client_name || document.origin_site_ref}
        />
        <Detail label="Their delivery note" value={document.client_delivery_note_ref} />
        {document.returned_by_name ? (
          <Detail label="Handed back by" value={document.returned_by_name} />
        ) : null}
        {document.origin_site_ref ? (
          <Detail label="Site" value={document.origin_site_ref} />
        ) : null}
      </Card>

      {boxes.length > 0 ? (
        <Card className="flex flex-col gap-2">
          <h2 className="text-sm font-semibold text-slate-900">Boxes</h2>
          <p className="text-sm text-slate-600">
            {isDraft
              ? 'A box with no code gets one when this is received.'
              : 'Write each code on its box, so it can be found by scanning it later.'}
          </p>
          <ul className="flex flex-col gap-1">
            {tree.roots.map((node) => (
              <BoxOutline key={node.box.key} node={node} depth={0} />
            ))}
          </ul>
        </Card>
      ) : null}

      <Card className="flex flex-col gap-2">
        <h2 className="text-sm font-semibold text-slate-900">Lines</h2>
        <DataList
          rows={document.lines}
          rowKey={(row) => String(row.id ?? row.line_number)}
          columns={[
            { header: 'Item', cell: (row) => row.item_name ?? row.item_type },
            { header: 'Quantity', cell: (row) => `${row.quantity} ${row.uom}` },
            {
              header: 'Condition',
              cell: (row) => (
                <span className={row.is_unserviceable ? 'text-amber-800' : undefined}>
                  {row.condition.replaceAll('_', ' ').toLowerCase()}
                  {row.is_unserviceable ? ' · quarantined' : ''}
                </span>
              ),
            },
            {
              header: 'For site',
              cell: (row) => row.for_site_name || document.for_site_name || '—',
            },
            {
              header: 'Box',
              // Said on the line itself (P10): where the generated codes get
              // read off, to be written on the carton.
              cell: (row) => linePlacement(row, boxes),
            },
            {
              header: 'Owner',
              // "Client owned" alone raises the question it is meant to
              // answer — and the answer is on the line already.
              cell: (row) =>
                row.owner_client
                  ? row.owner_client_name
                    ? `${row.owner_client_name} owns this`
                    : 'client owned'
                  : 'own stock',
              wideOnly: true,
            },
            {
              header: 'Identified',
              wideOnly: true,
              cell: (row) =>
                row.serials?.length
                  ? row.serials.map((serial) => serial.serial_number).join(', ')
                  : row.reels?.length
                    ? row.reels.map((reel) => `${reel.drum_number} (${reel.length})`).join(', ')
                    : '—',
            },
          ]}
        />
      </Card>

      {document.status !== 'VOID' ? (
        <Card className="flex flex-col gap-2">
          {/* This organization may require one before posting (D6, C8). The
              rule existed and the screen offered nowhere to satisfy it, so a
              storekeeper met a refusal with no way past it. */}
          <PhotoCapture
            targetType="receiving.GateIn"
            targetId={document.id}
            label="Photos and the delivery note"
            hint="A photo of what arrived, or a scan of the supplier's note. Some organizations require one before a delivery can be posted."
          />
        </Card>
      ) : null}

      <Sheet
        open={discardSheet}
        title="Discard this draft"
        onClose={() => setDiscardSheet(false)}
        footer={
          <>
            <Button variant="secondary" block onClick={() => setDiscardSheet(false)}>
              Keep it
            </Button>
            <Button
              variant="danger"
              block
              loading={discard.isPending}
              onClick={async () => {
                setBanner(null);
                try {
                  await discard.mutateAsync({ id: String(document.id) });
                  navigate('/gate-in');
                } catch (error) {
                  setDiscardSheet(false);
                  setBanner(errorMessage(error));
                }
              }}
            >
              Discard it
            </Button>
          </>
        }
      >
        <p className="text-sm text-slate-600">
          Nothing has been posted, so no stock changes and nothing is reversed —
          the draft is simply gone, with its {document.lines.length}{' '}
          {document.lines.length === 1 ? 'line' : 'lines'}.
        </p>
      </Sheet>

      <Sheet
        open={voidSheet}
        title="Void this delivery"
        onClose={() => setVoidSheet(false)}
        footer={
          <>
            <Button variant="secondary" block onClick={() => setVoidSheet(false)}>
              Cancel
            </Button>
            <Button
              variant="danger"
              block
              disabled={!reason.trim()}
              loading={voidIt.isPending}
              onClick={async () => {
                setBanner(null);
                try {
                  await voidIt.mutateAsync({ id: String(document.id), reason });
                  setVoidSheet(false);
                  setReason('');
                } catch (error) {
                  setBanner(errorMessage(error));
                }
              }}
            >
              Void
            </Button>
          </>
        }
      >
        <div className="flex flex-col gap-3">
          <p className="text-sm text-slate-700">
            The movements are reversed rather than deleted, and the document keeps
            its number. An auditor sees both the receipt and its reversal, which is
            the point.
          </p>
          <Field label="Why" htmlFor="void-reason">
            <Input
              id="void-reason"
              value={reason}
              placeholder="Wrong supplier note; received again on GRN-000014"
              onChange={(event) => setReason(event.target.value)}
            />
          </Field>
        </div>
      </Sheet>
    </div>
  );
}

/** Where a line's contents went: its box, or the boxes its units are spread over. */
function linePlacement(line: GateInLineInput, boxes: DraftBox[]): string {
  if (line.tracking_mode === 'BULK') {
    return line.box_key ? boxLabel(boxes, line.box_key) : 'Loose';
  }
  if (line.tracking_mode === 'SERIALIZED' && line.serials?.length) {
    const counts = new Map<string, number>();
    for (const serial of line.serials) {
      const name = serial.box_key ? boxLabel(boxes, serial.box_key) : 'Loose';
      counts.set(name, (counts.get(name) ?? 0) + 1);
    }
    return [...counts].map(([name, count]) => `${name} (${count})`).join(', ');
  }
  return 'Loose';
}

/** A box and what is in it, indented under the box it sits in. */
function BoxOutline({ node, depth }: { node: BoxNode; depth: number }) {
  return (
    <li>
      <p className="break-words text-sm text-slate-900" style={{ paddingLeft: depth * 16 }}>
        <span className="font-mono font-medium">{node.label}</span> · {describeCounts(node)}
      </p>
      {node.children.length > 0 ? (
        <ul className="flex flex-col gap-1">
          {node.children.map((child) => (
            <BoxOutline key={child.box.key} node={child} depth={depth + 1} />
          ))}
        </ul>
      ) : null}
    </li>
  );
}

function Detail({ label, value }: { label: string; value?: string | null }) {
  return (
    <div>
      <p className="text-xs font-medium tracking-wide text-slate-500 uppercase">{label}</p>
      <p className="text-sm text-slate-900">{value || '—'}</p>
    </div>
  );
}
