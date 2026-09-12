/**
 * The sync queue and the conflicts it produced (design §8.1, §8.4; N1, N3, T8.3, T8.7).
 *
 * Two lists, and they answer different questions:
 *
 * **On this device** — what has been captured and not yet sent. T8.3's criterion
 * is that three offline gate-ins are "visibly pending and survive an app
 * restart", so this reads IndexedDB rather than memory.
 *
 * **Conflicts** — what the server refused. T8.7 asks that "every synced conflict
 * is resolvable without developer intervention", which means the screen has to
 * show the payload and take a resolution. A conflict a storekeeper can see but
 * not close is one they will ring somebody about.
 *
 * Resolving deliberately does not retry the payload. Whatever went wrong needs a
 * person's judgement — the material may have been received on another document,
 * or issued to somebody else, or never have arrived — and a retry button would
 * post whichever version happened to win.
 */

import { useEffect, useState } from 'react';

import { errorMessage, useAction, useList } from '../api/hooks';
import { Banner, Button, Card, Field, Spinner, Textarea } from '../components/ui';
import { Checkbox } from '../components/ui';
import { EmptyState, PageHeader, Sheet, Stat, StatusBadge } from '../components/ui/data';
import { type QueuedMutation, allQueued, clearApplied } from './db';
import { useOffline } from './OfflineProvider';

interface SyncExceptionRow {
  id: number;
  status: string;
  code: string;
  reason: string;
  details: Record<string, unknown>;
  operation: string;
  client_uuid: string;
  captured_at: string | null;
  captured_by: string;
  payload: Record<string, unknown>;
  resolution: string;
  resolved_by_name: string;
  resolved_at: string | null;
  is_open: boolean;
  created_at: string;
}

const OPERATION_LABELS: Record<string, string> = {
  GATE_IN: 'Delivery received',
  GATE_OUT_REQUEST: 'Material requested',
  GATE_OUT_RELEASE: 'Pass released',
};

export default function SyncPage() {
  const { online, pending, syncing, syncNow, lastOutcome, referenceFetchedAt } =
    useOffline();
  const [rows, setRows] = useState<QueuedMutation[]>([]);
  const [resolving, setResolving] = useState<SyncExceptionRow | null>(null);

  const exceptions = useList<SyncExceptionRow>(
    'sync-exceptions',
    { status: 'OPEN', page_size: 50 },
    { enabled: online },
  );

  useEffect(() => {
    void allQueued().then(setRows);
  }, [pending, syncing]);

  const queued = rows.filter((row) => row.status === 'PENDING');
  const refused = rows.filter((row) => row.status === 'REJECTED');
  const sent = rows.filter((row) => row.status === 'APPLIED');

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Sync"
        subtitle="What this device has captured, and anything the yard could not accept."
        actions={
          <Button loading={syncing} onClick={() => void syncNow()}>
            {online ? 'Send now' : 'Try again'}
          </Button>
        }
      />

      {lastOutcome ? <Banner tone="info">{lastOutcome}</Banner> : null}

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Stat
          label="Waiting to send"
          value={queued.length}
          tone={queued.length > 0 ? 'warn' : 'good'}
        />
        <Stat
          label="Refused"
          value={refused.length + (exceptions.data?.results.length ?? 0)}
          tone={refused.length > 0 ? 'bad' : 'good'}
          hint="Needs a decision."
        />
        <Stat label="Sent" value={sent.length} tone="good" />
        <Stat
          label="Item list"
          value={referenceFetchedAt ? referenceFetchedAt.slice(11, 16) : '—'}
          hint="When the offline copy was refreshed."
        />
      </div>

      <Card className="flex flex-col gap-3">
        <h2 className="text-sm font-semibold text-slate-900">On this device</h2>
        {rows.length === 0 ? (
          <EmptyState
            title="Nothing captured offline."
            hint="Anything you capture without signal appears here until it is sent."
          />
        ) : (
          <ul className="flex flex-col gap-2">
            {rows.map((row) => (
              <li
                key={row.client_uuid}
                className="flex flex-wrap items-start justify-between gap-2 border-b border-slate-100 pb-2 last:border-0"
              >
                <div className="min-w-0">
                  <p className="text-sm font-medium text-slate-900">
                    {OPERATION_LABELS[row.operation] ?? row.operation}
                    {row.document_number ? ` · ${row.document_number}` : ''}
                  </p>
                  <p className="text-xs text-slate-500">
                    captured {row.captured_at.slice(0, 16).replace('T', ' ')}
                    {row.attempts > 0 ? ` · ${row.attempts} attempt(s)` : ''}
                  </p>
                  {row.exception_reason ? (
                    <p className="text-sm text-red-700">{row.exception_reason}</p>
                  ) : null}
                  {row.last_error && row.status === 'PENDING' ? (
                    <p className="text-xs text-slate-500">{row.last_error}</p>
                  ) : null}
                </div>
                <StatusBadge status={row.status} />
              </li>
            ))}
          </ul>
        )}
        {sent.length > 0 ? (
          <Button
            variant="ghost"
            onClick={async () => {
              await clearApplied();
              setRows(await allQueued());
            }}
          >
            Clear the {sent.length} already sent
          </Button>
        ) : null}
      </Card>

      {/* §8.4, T8.7: the server's side of the same story, with the payload. */}
      <Card className="flex flex-col gap-3">
        <h2 className="text-sm font-semibold text-slate-900">
          Conflicts the yard could not accept
        </h2>
        {!online ? (
          <p className="text-sm text-slate-500">
            Needs a connection to load.
          </p>
        ) : exceptions.isLoading ? (
          <Spinner className="text-slate-400" />
        ) : (exceptions.data?.results.length ?? 0) === 0 ? (
          <EmptyState
            title="Nothing outstanding."
            hint="A capture that no longer fits current stock lands here rather than being forced or dropped."
          />
        ) : (
          <ul className="flex flex-col gap-3">
            {(exceptions.data?.results ?? []).map((row) => (
              <li
                key={row.id}
                className="flex flex-col gap-2 border-b border-slate-100 pb-3 last:border-0"
              >
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div className="min-w-0">
                    <p className="text-sm font-semibold text-slate-900">
                      {OPERATION_LABELS[row.operation] ?? row.operation}
                      {row.captured_by ? ` · captured by ${row.captured_by}` : ''}
                    </p>
                    <p className="text-sm text-slate-700">{row.reason}</p>
                    <p className="text-xs text-slate-500">
                      {row.code} ·{' '}
                      {row.captured_at?.slice(0, 16).replace('T', ' ') ??
                        row.created_at.slice(0, 16).replace('T', ' ')}
                    </p>
                  </div>
                  <Button onClick={() => setResolving(row)}>Resolve</Button>
                </div>

                {/* The payload, because it is the only record of what the person
                    on site actually said. */}
                <details className="text-xs text-slate-600">
                  <summary className="cursor-pointer">What was captured</summary>
                  <pre className="mt-1 overflow-x-auto rounded bg-slate-50 p-2">
                    {JSON.stringify(row.payload, null, 2)}
                  </pre>
                </details>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <ResolveSheet
        row={resolving}
        onClose={() => setResolving(null)}
        onResolved={() => void exceptions.refetch()}
      />
    </div>
  );
}

function ResolveSheet({
  row,
  onClose,
  onResolved,
}: {
  row: SyncExceptionRow | null;
  onClose: () => void;
  onResolved: () => void;
}) {
  const [resolution, setResolution] = useState('');
  const [discard, setDiscard] = useState(false);
  const [banner, setBanner] = useState<string | null>(null);

  const resolve = useAction<{ id: number; resolution: string; discard: boolean }>({
    resource: 'sync-exceptions',
    path: (body) => `${body.id}/resolve`,
    invalidates: ['sync-exceptions', 'exceptions'],
  });

  return (
    <Sheet
      open={Boolean(row)}
      title="Resolve this conflict"
      onClose={onClose}
      footer={
        <>
          <Button variant="secondary" block onClick={onClose}>
            Cancel
          </Button>
          <Button
            block
            disabled={!resolution.trim()}
            loading={resolve.isPending}
            onClick={async () => {
              if (!row) return;
              setBanner(null);
              try {
                await resolve.mutateAsync({ id: row.id, resolution, discard });
                setResolution('');
                setDiscard(false);
                onResolved();
                onClose();
              } catch (error) {
                setBanner(errorMessage(error));
              }
            }}
          >
            {discard ? 'Discard it' : 'Resolve'}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        {banner ? <Banner tone="error">{banner}</Banner> : null}

        <Banner tone="info">
          Resolving records what was done. It does not re-send the capture —
          whatever went wrong needs your judgement, and a silent retry would post
          whichever version happened to win.
        </Banner>

        <p className="text-sm text-slate-700">{row?.reason}</p>

        <Field
          label="What was done about it"
          htmlFor="sync-resolution"
          hint="“Received on GRN-000004 the next morning” is an answer; “fixed” is not."
        >
          <Textarea
            id="sync-resolution"
            value={resolution}
            onChange={(event) => setResolution(event.target.value)}
          />
        </Field>

        <Checkbox
          id="sync-discard"
          label="Discard — this never happened"
          hint="The material was never received, or the request is moot. The record of the attempt stays either way."
          checked={discard}
          onChange={(event) => setDiscard(event.target.checked)}
        />
      </div>
    </Sheet>
  );
}
