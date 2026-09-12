/**
 * Gate-out list, detail and release (design §7.3, §7.4; F1–F8, G1–G4).
 *
 * T4.23's criterion is the one that shapes the release screen: "releasing a load
 * with one short line **records the variance and completes the release**."
 *
 * So the short quantity is an ordinary input, not an error state. G1's edge case
 * says a short load is a variance rather than a refusal, and the reason for that
 * is practical: a gate that blocks on a discrepancy is a gate people drive round,
 * and then nothing is recorded at all. The screen therefore asks *why* and lets
 * the load go.
 *
 * The verify-against-approved checklist is the other half. A guard comparing a
 * physical load to a pass needs the approved quantity and the actual side by
 * side, per line, with serial numbers spelled out — because "2 RRUs" checks
 * nothing, and "HW-RRU-88001, HW-RRU-88002" checks the load.
 */

import { useMemo, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';

import { openDocument } from '../../api/client';
import { errorMessage, useAction, useDetail, useList } from '../../api/hooks';
import { useSession } from '../../auth/session';
import { PERM } from '../../auth/permissions';
import {
  Banner,
  Button,
  Card,
  Field,
  Input,
  OwnershipBadge,
  Spinner,
  Textarea,
} from '../../components/ui';
import { DataList, EmptyState, PageHeader, Sheet, StatusBadge, Stat } from '../../components/ui/data';
import type { GateOut } from './types';

const OPEN_STATUSES = 'DRAFT,PENDING_APPROVAL,APPROVED,PARTIALLY_RELEASED';

export default function GateOutListPage() {
  const navigate = useNavigate();
  const { hasAny } = useSession();
  const [search, setSearch] = useState('');
  const [scope, setScope] = useState<'open' | 'all'>('open');

  const passes = useList<GateOut>('gate-outs', {
    search: search || undefined,
    status__in: scope === 'open' ? OPEN_STATUSES : undefined,
    page_size: 50,
  });

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Gate-out"
        subtitle="Requests, approvals waiting, and passes ready to release."
        actions={
          hasAny(PERM.GATE_OUT_REQUEST) ? (
            <Button onClick={() => navigate('/gate-out/new')}>Request material</Button>
          ) : undefined
        }
      />

      <div className="flex flex-wrap gap-2">
        <Input
          className="max-w-sm"
          aria-label="Search gate passes"
          placeholder="Pass number, vehicle or driver"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <Button
          variant={scope === 'open' ? 'primary' : 'secondary'}
          onClick={() => setScope(scope === 'open' ? 'all' : 'open')}
        >
          {scope === 'open' ? 'Showing open' : 'Showing everything'}
        </Button>
      </div>

      {passes.isError ? <Banner tone="error">{errorMessage(passes.error)}</Banner> : null}
      {passes.isLoading ? (
        <Spinner className="text-slate-400" />
      ) : (
        <DataList
          rows={passes.data?.results ?? []}
          rowKey={(row) => row.id}
          onRowClick={(row) => navigate(`/gate-out/${row.id}`)}
          empty={
            <EmptyState
              title="Nothing out or waiting."
              hint="A gate pass is what authorises material to leave the yard."
              action={
                hasAny(PERM.GATE_OUT_REQUEST) ? (
                  <Button onClick={() => navigate('/gate-out/new')}>Request material</Button>
                ) : undefined
              }
            />
          }
          columns={[
            { header: 'Pass', cell: (row) => row.number || 'draft' },
            { header: 'Status', cell: (row) => <StatusBadge status={row.status} /> },
            { header: 'Going to', cell: (row) => row.destination_label },
            { header: 'Held by', cell: (row) => row.custody_holder_name },
            { header: 'Lines', cell: (row) => row.lines.length, wideOnly: true },
            {
              header: 'Waiting on',
              wideOnly: true,
              cell: (row) =>
                row.pending_approval
                  ? `${row.pending_approval.role ?? 'an approver'} (level ${row.pending_approval.level})`
                  : '—',
            },
            {
              header: '',
              cell: (row) =>
                row.is_expired ? (
                  <span className="text-red-700">expired</span>
                ) : row.is_releasable ? (
                  <span className="text-emerald-700">ready to release</span>
                ) : null,
            },
          ]}
        />
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Detail                                                                     */
/* -------------------------------------------------------------------------- */

/**
 * G4: the paper the driver carries.
 *
 * Fetched rather than linked. The access token lives in memory (§7.2), so a
 * plain `href` sends no Authorization header and 401s — which is what this
 * button used to do, meaning the pass could not be printed from the app at all.
 */
async function printPass(id: number) {
  await openDocument(`/gate-outs/${id}/pdf`);
}

export function GateOutDetailPage() {
  const { id } = useParams();
  const { hasAny, user } = useSession();
  const pass = useDetail<GateOut>('gate-outs', id);
  const [banner, setBanner] = useState<string | null>(null);
  const [reasonSheet, setReasonSheet] = useState<'reject' | 'cancel' | 'close' | null>(null);
  const [reason, setReason] = useState('');
  const [releasing, setReleasing] = useState(false);

  // Spelled out rather than built in a loop: these are hooks, and hooks have to
  // be called unconditionally and in a fixed order.
  const invalidates = ['gate-outs', 'approvals', 'stock', 'movements', 'notifications'];
  const submitAction = useAction<Record<string, unknown>, GateOut>({
    resource: 'gate-outs',
    path: () => `${id}/submit`,
    invalidates,
  });
  const approveAction = useAction<Record<string, unknown>, GateOut>({
    resource: 'gate-outs',
    path: () => `${id}/approve`,
    invalidates,
  });
  const rejectAction = useAction<Record<string, unknown>, GateOut>({
    resource: 'gate-outs',
    path: () => `${id}/reject`,
    invalidates,
  });
  const amendAction = useAction<Record<string, unknown>, GateOut>({
    resource: 'gate-outs',
    path: () => `${id}/amend`,
    invalidates,
  });
  const cancelAction = useAction<Record<string, unknown>, GateOut>({
    resource: 'gate-outs',
    path: () => `${id}/cancel`,
    invalidates,
  });
  const closeAction = useAction<Record<string, unknown>, GateOut>({
    resource: 'gate-outs',
    path: () => `${id}/close`,
    invalidates,
  });

  if (pass.isLoading) return <Spinner className="text-slate-400" />;
  if (pass.isError) return <Banner tone="error">{errorMessage(pass.error)}</Banner>;

  const document = pass.data!;
  const isRequester = document.requested_by === user?.id;

  async function run(
    mutation: typeof submitAction,
    body: Record<string, unknown> = {},
  ) {
    setBanner(null);
    try {
      await mutation.mutateAsync(body);
      setReasonSheet(null);
      setReason('');
    } catch (error) {
      setBanner(errorMessage(error));
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={document.number || 'Draft request'}
        subtitle={
          <span className="flex flex-wrap items-center gap-2">
            <StatusBadge status={document.status} />
            {document.destination_label}
          </span>
        }
        actions={
          <>
            {/* A draft authorises nothing, so correcting one is ordinary work.
                Without this the only way to fix a line was to cancel the
                request and key it again. */}
            {document.status === 'DRAFT' ? (
              <Link
                to={`/gate-out/${document.id}/edit`}
                className="flex min-h-[44px] items-center px-2 text-sm text-slate-700"
              >
                Edit
              </Link>
            ) : null}
            <Link
              to="/gate-out"
              className="flex min-h-[44px] items-center px-2 text-sm text-slate-700"
            >
              Back
            </Link>
          </>
        }
      />

      {banner ? <Banner tone="error">{banner}</Banner> : null}

      {document.status === 'DRAFT' ? (
        <Banner tone="info">
          A draft authorises nothing. Sending it for approval is what starts the
          routing.
        </Banner>
      ) : null}

      {document.pending_approval ? (
        <Banner tone="warning">
          Waiting on {document.pending_approval.role ?? 'an approver'} at level{' '}
          {document.pending_approval.level}
          {document.pending_approval.due_at
            ? `, due ${document.pending_approval.due_at.slice(0, 16).replace('T', ' ')}`
            : ''}
          .
        </Banner>
      ) : null}

      {document.is_expired ? (
        <Banner tone="error">
          This pass has expired, so it can no longer be released. An approval
          given three weeks ago is not permission to walk material out today.
        </Banner>
      ) : null}

      {document.status === 'REJECTED' ? (
        <Banner tone="error">Rejected: {document.reject_reason}</Banner>
      ) : null}
      {document.status === 'CANCELLED' ? (
        <Banner tone="warning">Cancelled: {document.cancel_reason}</Banner>
      ) : null}

      <div className="grid gap-3 sm:grid-cols-3">
        <Stat label="Taken by" value={document.custody_holder_name} />
        <Stat
          label="Expires"
          value={document.expires_at ? document.expires_at.slice(0, 16).replace('T', ' ') : '—'}
          tone={document.is_expired ? 'bad' : 'neutral'}
        />
        <Stat
          label="Vehicle"
          value={document.vehicle_reg || '—'}
          hint={document.driver_name || undefined}
        />
      </div>

      <Card className="flex flex-col gap-2">
        <h2 className="text-sm font-semibold text-slate-900">What is on the pass</h2>
        <DataList
          rows={document.lines}
          rowKey={(row) => String(row.id)}
          columns={[
            { header: 'Item', cell: (row) => row.item_name },
            {
              header: 'Criticality',
              // §5.1: this is what routed the approval, so it belongs on the
              // screen an approver is looking at.
              cell: (row) => (row.criticality ?? '').toLowerCase(),
              wideOnly: true,
            },
            { header: 'Approved', cell: (row) => `${row.requested_qty} ${row.uom}` },
            { header: 'Released', cell: (row) => `${row.released_qty ?? '0'} ${row.uom}` },
            {
              header: 'Identified',
              cell: (row) =>
                row.serials?.length
                  ? row.serials.map((serial) => serial.serial_number).join(', ')
                  : row.reels?.length
                    ? row.reels
                        .map((reel) => `${reel.drum_number} (${reel.length_requested})`)
                        .join(', ')
                    : '—',
            },
            {
              header: 'Owner',
              cell: (row) => (
                <OwnershipBadge client={row.owner_client ? 'Client owned' : null} />
              ),
              wideOnly: true,
            },
            {
              header: 'Back by',
              cell: (row) => row.expected_return_date ?? '—',
              wideOnly: true,
            },
          ]}
        />
      </Card>

      {document.notes ? (
        <Card>
          <p className="text-xs font-medium tracking-wide text-slate-500 uppercase">Notes</p>
          <p className="text-sm text-slate-800">{document.notes}</p>
        </Card>
      ) : null}

      <div className="flex flex-wrap gap-2">
        {document.status === 'DRAFT' && hasAny(PERM.GATE_OUT_REQUEST) ? (
          <Button loading={submitAction.isPending} onClick={() => run(submitAction)}>
            Send for approval
          </Button>
        ) : null}

        {document.pending_approval && hasAny(PERM.GATE_OUT_APPROVE) ? (
          <>
            <Button
              loading={approveAction.isPending}
              onClick={() => run(approveAction, { reason: '' })}
            >
              Approve
            </Button>
            <Button variant="danger" onClick={() => setReasonSheet('reject')}>
              Reject
            </Button>
          </>
        ) : null}

        {document.is_releasable && hasAny(PERM.GATE_OUT_RELEASE) ? (
          <Button onClick={() => setReleasing(true)}>Release at the gate</Button>
        ) : null}

        {['APPROVED', 'PENDING_APPROVAL'].includes(document.status) &&
        hasAny(PERM.GATE_OUT_REQUEST) ? (
          <Button variant="secondary" loading={amendAction.isPending} onClick={() => run(amendAction)}>
            Amend
          </Button>
        ) : null}

        {['DRAFT', 'PENDING_APPROVAL', 'APPROVED'].includes(document.status) &&
        hasAny(PERM.GATE_OUT_REQUEST) ? (
          <Button variant="ghost" onClick={() => setReasonSheet('cancel')}>
            Cancel
          </Button>
        ) : null}

        {document.status === 'PARTIALLY_RELEASED' && hasAny(PERM.GATE_OUT_RELEASE) ? (
          <Button variant="secondary" onClick={() => setReasonSheet('close')}>
            Close what is left
          </Button>
        ) : null}

        {document.number ? (
          // Fetched with the auth header rather than linked: the access token is
          // in memory, so a plain href would 401 (see `openDocument`).
          <Button variant="secondary" onClick={() => void printPass(document.id)}>
            Print the pass
          </Button>
        ) : null}
      </div>

      {isRequester && document.pending_approval && hasAny(PERM.GATE_OUT_APPROVE) ? (
        <Banner tone="info">
          You raised this request. §5.3 refuses a self-approval unless this
          organization has deliberately allowed it, so approving here will be
          rejected — someone else has to sign it off.
        </Banner>
      ) : null}

      <Sheet
        open={reasonSheet !== null}
        title={
          reasonSheet === 'reject'
            ? 'Reject this request'
            : reasonSheet === 'cancel'
              ? 'Cancel this pass'
              : 'Close the outstanding lines'
        }
        onClose={() => setReasonSheet(null)}
        footer={
          <>
            <Button variant="secondary" block onClick={() => setReasonSheet(null)}>
              Back
            </Button>
            <Button
              variant={reasonSheet === 'reject' ? 'danger' : 'primary'}
              block
              disabled={!reason.trim()}
              onClick={() =>
                run(
                  reasonSheet === 'reject'
                    ? rejectAction
                    : reasonSheet === 'cancel'
                      ? cancelAction
                      : closeAction,
                  { reason },
                )
              }
            >
              Confirm
            </Button>
          </>
        }
      >
        <div className="flex flex-col gap-3">
          <p className="text-sm text-slate-700">
            {reasonSheet === 'reject'
              ? 'A rejection has to say why, so the requester knows what to change.'
              : reasonSheet === 'cancel'
                ? 'A pass is cancelled with a reason and never deleted, and never after a release.'
                : 'The outstanding balance is written off with a reason, and the pass closes.'}
          </p>
          <Field label="Reason" htmlFor="reason">
            <Textarea
              id="reason"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
          </Field>
        </div>
      </Sheet>

      {releasing ? (
        <ReleaseSheet
          gateOut={document}
          onClose={() => setReleasing(false)}
          onDone={() => {
            setReleasing(false);
            void pass.refetch();
          }}
        />
      ) : null}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* T4.23 — the gate                                                           */
/* -------------------------------------------------------------------------- */

function ReleaseSheet({
  gateOut,
  onClose,
  onDone,
}: {
  gateOut: GateOut;
  onClose: () => void;
  onDone: () => void;
}) {
  const { user } = useSession();
  const settings = user?.organization?.settings ?? null;

  const [vehicle, setVehicle] = useState(gateOut.vehicle_reg);
  const [driver, setDriver] = useState(gateOut.driver_name);
  const [checked, setChecked] = useState<Record<string, boolean>>({});
  const [actual, setActual] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      gateOut.lines.map((line) => [String(line.id), line.outstanding_qty ?? line.requested_qty]),
    ),
  );
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [banner, setBanner] = useState<string | null>(null);

  const release = useAction<Record<string, unknown>, GateOut>({
    resource: 'gate-outs',
    path: () => `${gateOut.id}/release`,
    invalidates: ['gate-outs', 'stock', 'movements', 'custody-expectations', 'notifications'],
  });

  const shortLines = useMemo(
    () =>
      gateOut.lines.filter((line) => {
        const outstanding = Number(line.outstanding_qty ?? line.requested_qty);
        return Number(actual[String(line.id)] ?? outstanding) < outstanding;
      }),
    [gateOut.lines, actual],
  );

  const missingReason = shortLines.some((line) => !reasons[String(line.id)]?.trim());
  const allChecked = gateOut.lines.every((line) => checked[String(line.id)]);

  async function submit() {
    setBanner(null);
    try {
      await release.mutateAsync({
        vehicle_reg: vehicle,
        driver_name: driver,
        released_lines: actual,
        variance_reasons: reasons,
      });
      onDone();
    } catch (error) {
      setBanner(errorMessage(error));
    }
  }

  return (
    <Sheet
      open
      title={`Release ${gateOut.number}`}
      onClose={onClose}
      footer={
        <>
          <Button variant="secondary" block onClick={onClose}>
            Not yet
          </Button>
          <Button
            block
            loading={release.isPending}
            disabled={missingReason}
            onClick={submit}
          >
            {shortLines.length > 0 ? 'Release short' : 'Release'}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        {banner ? <Banner tone="error">{banner}</Banner> : null}

        <p className="text-sm text-slate-700">
          Check the load against the pass, line by line. A line loaded short is
          fine — say why and the release still completes, with the difference
          recorded as a variance.
        </p>

        <ul className="flex flex-col gap-2">
          {gateOut.lines.map((line) => {
            const key = String(line.id);
            const outstanding = line.outstanding_qty ?? line.requested_qty;
            const isShort = Number(actual[key] ?? outstanding) < Number(outstanding);
            return (
              <li key={key} className="rounded-lg border border-slate-200 p-3">
                <label className="flex min-h-[44px] items-start gap-3">
                  <input
                    type="checkbox"
                    className="mt-1 size-5 shrink-0 accent-slate-900"
                    checked={Boolean(checked[key])}
                    onChange={(event) =>
                      setChecked({ ...checked, [key]: event.target.checked })
                    }
                  />
                  <span className="text-sm">
                    <span className="font-medium text-slate-900">{line.item_name}</span>
                    <span className="block text-slate-600">
                      approved {line.requested_qty} {line.uom}
                      {Number(line.released_qty ?? 0) > 0
                        ? ` · ${line.released_qty} already gone`
                        : ''}
                    </span>
                    {line.serials?.length ? (
                      // "2 RRUs" checks nothing; the serial numbers check the load.
                      <span className="block font-mono text-xs text-slate-700">
                        {line.serials.map((serial) => serial.serial_number).join(', ')}
                      </span>
                    ) : null}
                    {line.reels?.length ? (
                      <span className="block font-mono text-xs text-slate-700">
                        {line.reels
                          .map((reel) => `${reel.drum_number} · ${reel.length_requested}`)
                          .join(', ')}
                      </span>
                    ) : null}
                    {line.no_serial_reason ? (
                      // D3: this line is a serialized item going out untagged.
                      // The storekeeper at the gate is the last person who can
                      // notice that a label was simply missed, so the recorded
                      // reason belongs on the checklist rather than in the file.
                      <span className="block text-xs text-amber-700">
                        no serial: {line.no_serial_reason}
                      </span>
                    ) : null}
                  </span>
                </label>

                <div className="mt-2 grid gap-2 sm:grid-cols-2">
                  <Field label={`Actually loaded (${line.uom})`} htmlFor={`actual-${key}`}>
                    <Input
                      id={`actual-${key}`}
                      inputMode="decimal"
                      value={actual[key] ?? ''}
                      onChange={(event) => setActual({ ...actual, [key]: event.target.value })}
                    />
                  </Field>
                  {isShort ? (
                    <Field
                      label="Why short"
                      htmlFor={`reason-${key}`}
                      error={!reasons[key]?.trim() ? 'Required for a short line.' : undefined}
                    >
                      <Input
                        id={`reason-${key}`}
                        value={reasons[key] ?? ''}
                        placeholder="Only seven on the shelf"
                        onChange={(event) =>
                          setReasons({ ...reasons, [key]: event.target.value })
                        }
                      />
                    </Field>
                  ) : null}
                </div>
              </li>
            );
          })}
        </ul>

        <Field label="Vehicle registration" htmlFor="rel-vehicle">
          <Input
            id="rel-vehicle"
            value={vehicle}
            placeholder="KDA 411K"
            onChange={(event) => setVehicle(event.target.value)}
          />
        </Field>

        <Field label="Driver" htmlFor="rel-driver">
          <Input
            id="rel-driver"
            value={driver}
            onChange={(event) => setDriver(event.target.value)}
          />
        </Field>

        {settings?.signature_required_on_release ? (
          <Banner tone="warning">
            This organization requires a signature at the gate. The signature pad
            is part of T8.5's offline release work; until then the vehicle and
            driver are the record.
          </Banner>
        ) : null}

        {!allChecked ? (
          <p className="text-sm text-slate-500">
            {gateOut.lines.length - Object.values(checked).filter(Boolean).length} line(s)
            not yet checked off. The checklist is for the person at the gate — it
            does not block the release.
          </p>
        ) : null}
      </div>
    </Sheet>
  );
}
