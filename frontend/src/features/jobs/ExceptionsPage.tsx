/**
 * T5.12 — the exceptions register (design §7.4; H3, M1).
 *
 * The criterion: "**every open variance is actionable from one list**."
 *
 * Actionable is the word doing the work. A register that only *lists* problems
 * gets read once and then ignored, because working through it means opening
 * another screen per row and losing your place. So each row carries its own
 * resolution: a variance is signed off here, a short release is acknowledged
 * here, and an overdue item opens the person holding it.
 *
 * The three kinds come from one endpoint (M1: "unresolved variances, overdue
 * custody, quarantined stock awaiting decision"), which is the point of the
 * requirement — three lists would be three places to forget to look. Sync
 * exceptions (T8.7) and quarantine (Phase 6) join the same list as further
 * kinds, not another screen.
 *
 * Resolving is deliberately not one tap. H3 asks for a resolution *and* whether
 * it is a write-off, and a write-off is somebody accepting a loss — the sheet
 * makes that a decision rather than a swipe.
 */

import { useState } from 'react';
import { Link } from 'react-router-dom';

import { errorMessage, useAction, useResource } from '../../api/hooks';
import {
  Banner,
  Button,
  Card,
  Checkbox,
  Field,
  Spinner,
  Textarea,
} from '../../components/ui';
import { EmptyState, PageHeader, Sheet, Stat } from '../../components/ui/data';
import { SearchField } from '../../components/ui/SearchField';
import type { ExceptionEntry } from '../dispatch/types';

const KINDS: { value: string; label: string }[] = [
  { value: '', label: 'Everything unresolved' },
  { value: 'variance', label: 'Variances' },
  { value: 'release_variance', label: 'Short releases' },
  { value: 'custody', label: 'Overdue custody' },
];

const KIND_LABELS: Record<string, string> = {
  variance: 'Variance',
  release_variance: 'Short release',
  custody: 'Overdue',
};

export default function ExceptionsPage() {
  const [kind, setKind] = useState('');
  const [search, setSearch] = useState('');
  const register = useResource<{ count: number; items: ExceptionEntry[] }>('exceptions', {
    ...(kind ? { kind } : {}),
    ...(search ? { search } : {}),
  });
  const [resolving, setResolving] = useState<ExceptionEntry | null>(null);
  const [banner, setBanner] = useState<string | null>(null);

  const acknowledge = useAction<{ id: number }>({
    resource: 'release-variances',
    path: (body) => `${body.id}/acknowledge`,
    invalidates: ['exceptions', 'release-variances', 'gate-outs'],
  });

  const items = register.data?.items ?? [];
  const counts = {
    variance: items.filter((item) => item.kind === 'variance').length,
    release: items.filter((item) => item.kind === 'release_variance').length,
    custody: items.filter((item) => item.kind === 'custody').length,
  };

  async function acknowledgeShortRelease(entry: ExceptionEntry) {
    setBanner(null);
    try {
      await acknowledge.mutateAsync({ id: entry.id });
    } catch (error) {
      setBanner(errorMessage(error));
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Exceptions"
        subtitle="Everything unresolved, in one list. Nothing leaves it without an answer."
        actions={
          <div className="flex flex-wrap gap-1">
            {KINDS.map((option) => (
              <Button
                key={option.value}
                variant={kind === option.value ? 'primary' : 'secondary'}
                className="min-h-0 px-3 py-1.5 text-sm"
                onClick={() => setKind(option.value)}
              >
                {option.label}
              </Button>
            ))}
          </div>
        }
      />

      <SearchField
        value={search}
        onChange={setSearch}
        label="Search exceptions"
        placeholder="Reference or reason"
      />

      {banner ? <Banner tone="error">{banner}</Banner> : null}
      {register.isError ? <Banner tone="error">{errorMessage(register.error)}</Banner> : null}

      <div className="grid grid-cols-3 gap-2">
        <Stat
          label="Variances"
          value={counts.variance}
          tone={counts.variance > 0 ? 'warn' : 'good'}
          hint="Declared and received disagree."
        />
        <Stat
          label="Short releases"
          value={counts.release}
          tone={counts.release > 0 ? 'warn' : 'good'}
          hint="Less went out than was approved."
        />
        <Stat
          label="Overdue"
          value={counts.custody}
          tone={counts.custody > 0 ? 'bad' : 'good'}
          hint="Past its return date."
        />
      </div>

      {register.isLoading ? (
        <Spinner className="text-slate-400" />
      ) : items.length === 0 ? (
        <EmptyState
          title="Nothing unresolved."
          hint="Variances, short releases and overdue custody all land here as they arise."
        />
      ) : (
        <ul className="flex flex-col gap-2">
          {items.map((entry) => (
            <li key={`${entry.kind}-${entry.id}`}>
              <Card className="flex flex-col gap-2">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div className="min-w-0">
                    <p className="text-sm font-semibold text-slate-900">
                      {KIND_LABELS[entry.kind] ?? entry.kind}
                      {entry.reference ? ` · ${entry.reference}` : ''}
                    </p>
                    <p className="text-sm text-slate-700">{entry.summary}</p>
                  </div>
                  <p className="shrink-0 text-xs text-slate-500">
                    {entry.raised_at?.slice(0, 16).replace('T', ' ')}
                  </p>
                </div>

                {/* The two figures that disagree, side by side. A register that
                    makes you open the document to see the numbers is a register
                    nobody works through. */}
                <div className="flex gap-4 text-sm">
                  <span className="text-slate-600">
                    expected{' '}
                    <span className="font-medium text-slate-900">
                      {entry.expected} {entry.uom}
                    </span>
                  </span>
                  <span className="text-slate-600">
                    actual{' '}
                    <span className="font-medium text-slate-900">
                      {entry.actual} {entry.uom}
                    </span>
                  </span>
                </div>

                <div className="flex flex-wrap gap-2">
                  {entry.kind === 'variance' ? (
                    <Button onClick={() => setResolving(entry)}>Resolve</Button>
                  ) : null}
                  {entry.kind === 'release_variance' ? (
                    <Button
                      loading={acknowledge.isPending}
                      onClick={() => void acknowledgeShortRelease(entry)}
                    >
                      Acknowledge
                    </Button>
                  ) : null}
                  {entry.kind === 'custody' ? (
                    // Nothing to resolve on the spot: the answer is either the
                    // material coming back (a gate-in) or a write-off, and both
                    // start from the person holding it (I3, I4).
                    <Link
                      to="/jobs/custody"
                      className="flex min-h-[44px] items-center rounded-lg border border-slate-300 px-4 text-sm font-medium text-slate-800"
                    >
                      Open custody
                    </Link>
                  ) : null}
                </div>
              </Card>
            </li>
          ))}
        </ul>
      )}

      <ResolveSheet
        entry={resolving}
        onClose={() => setResolving(null)}
        onResolved={() => void register.refetch()}
      />
    </div>
  );
}

/** H3: an approver signs a variance off, with an explanation. */
function ResolveSheet({
  entry,
  onClose,
  onResolved,
}: {
  entry: ExceptionEntry | null;
  onClose: () => void;
  onResolved: () => void;
}) {
  const [resolution, setResolution] = useState('');
  const [writeOff, setWriteOff] = useState(false);
  const [banner, setBanner] = useState<string | null>(null);

  const resolve = useAction<{ id: number; resolution: string; write_off: boolean }>({
    resource: 'variances',
    path: (body) => `${body.id}/resolve`,
    invalidates: ['exceptions', 'variances', 'stock', 'jobs'],
  });

  async function submit() {
    if (!entry) return;
    setBanner(null);
    try {
      await resolve.mutateAsync({
        id: entry.id,
        resolution,
        write_off: writeOff,
      });
      setResolution('');
      setWriteOff(false);
      onResolved();
      onClose();
    } catch (error) {
      setBanner(errorMessage(error));
    }
  }

  return (
    <Sheet
      open={Boolean(entry)}
      title="Resolve this variance"
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
            onClick={() => void submit()}
          >
            {writeOff ? 'Write it off' : 'Resolve'}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        {banner ? <Banner tone="error">{banner}</Banner> : null}

        <p className="text-sm text-slate-700">{entry?.summary}</p>

        <Field
          label="What happened"
          htmlFor="resolution"
          hint="This is what an auditor reads. “Found in the second van” is an answer; “resolved” is not."
        >
          <Textarea
            id="resolution"
            value={resolution}
            onChange={(event) => setResolution(event.target.value)}
          />
        </Field>

        <Checkbox
          id="write-off"
          label="Write off the difference"
          hint="Accepts the loss and adjusts stock to match reality. Without this the figures stay as they are and only the variance closes."
          checked={writeOff}
          onChange={(event) => setWriteOff(event.target.checked)}
        />
      </div>
    </Sheet>
  );
}
