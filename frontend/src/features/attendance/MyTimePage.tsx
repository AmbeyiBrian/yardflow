/**
 * My time (design §4.18.11; R13), at `/time`.
 *
 * The clock-in card, then my days with hours and status. A day opens in a
 * sheet showing its sessions, the distances and the flags. A rejected day
 * shows the reason and, for a session of the rejected slice, an "Add a
 * correction" form: the time and a required reason (§4.18.6). The recorded
 * times are never overwritten; the server keeps original and corrected.
 */

import { useState } from 'react';

import { errorMessage } from '../../api/hooks';
import { Banner, Button, Card, Field, Input, Spinner, Textarea } from '../../components/ui';
import { EmptyState, ListState, PageHeader, Sheet, StatusBadge } from '../../components/ui/data';
import { TabStrip } from '../../components/ui/TabStrip';
import { useDayAccess } from './access';
import { AddDaySheet } from './AddDaySheet';
import { timeScopes } from './approvals';
import { useCorrectSession, useWorkDay, useWorkDays } from './api';
import { ClockInCard } from './ClockInCard';
import { formatDistance } from './nearby';
import { clockError, dayStatusLabel, flagHint, flagLabel, hoursLabel } from './rules';
import type { WorkDay, WorkSession } from './types';

const dayLabel = (iso: string) =>
  new Date(`${iso}T12:00:00`).toLocaleDateString([], {
    weekday: 'short',
    day: 'numeric',
    month: 'short',
  });

const timeLabel = (iso: string | null) =>
  iso ? new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : 'open';

/** `datetime-local` wants local "YYYY-MM-DDTHH:mm". */
function toLocalInput(iso: string | null): string {
  if (!iso) return '';
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export default function MyTimePage() {
  // §4.18.11: Team for PMs, Everyone for view-all and the Director.
  const access = useDayAccess();
  const scopes = timeScopes(access);
  const [picked, setScope] = useState<'mine' | 'team' | 'all'>('mine');
  const scope = scopes.includes(picked) ? picked : 'mine';
  const days = useWorkDays({ scope });
  const [openId, setOpenId] = useState<number | null>(null);
  const [adding, setAdding] = useState(false);

  return (
    <div className="flex flex-col gap-5">
      <PageHeader title="My time" subtitle="Your days, hours and whether they are approved." />
      <ClockInCard />

      {scopes.length > 1 ? (
        <TabStrip<'mine' | 'team' | 'all'>
          tabs={scopes.map((key) => ({
            key,
            label: key === 'mine' ? 'Mine' : key === 'team' ? 'Team' : 'Everyone',
          }))}
          current={scope}
          onSelect={setScope}
          aria-label="Whose time"
        />
      ) : null}
      {scope === 'all' && access.isDirector ? (
        <Button variant="secondary" className="self-start" onClick={() => setAdding(true)}>
          Add a day
        </Button>
      ) : null}

      <ListState query={days}>
        {(days.data?.results ?? []).length === 0 ? (
          <EmptyState
            title="No days yet"
            hint="Clock in and your day appears here."
          />
        ) : (
          <ul className="flex flex-col gap-2">
            {(days.data?.results ?? []).map((d) => (
              <li key={d.id}>
                <DayRow day={d} showPerson={scope !== 'mine'} onOpen={() => setOpenId(d.id)} />
              </li>
            ))}
          </ul>
        )}
      </ListState>

      <DaySheet id={openId} onClose={() => setOpenId(null)} />
      {adding ? <AddDaySheet onClose={() => setAdding(false)} /> : null}
    </div>
  );
}

function DayRow({
  day,
  onOpen,
  showPerson,
}: {
  day: WorkDay;
  onOpen: () => void;
  showPerson?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onOpen}
      className="flex min-h-[44px] w-full flex-col gap-1 rounded-xl border border-slate-200 bg-white p-4 text-left"
    >
      <span className="flex items-center justify-between gap-2">
        <span className="text-sm font-medium text-slate-900">
          {showPerson && day.person_name ? `${day.person_name}, ` : ''}
          {dayLabel(day.date)}
        </span>
        <span className="flex items-center gap-2">
          <span className="text-sm text-slate-700">{hoursLabel(day.hours)}</span>
          <StatusBadge status={day.status} />
          <span className="sr-only">{dayStatusLabel(day.status)}</span>
        </span>
      </span>
      {day.status === 'REJECTED' && day.rejection_reason ? (
        <span className="text-sm text-red-800">Rejected: {day.rejection_reason}</span>
      ) : null}
    </button>
  );
}

function DaySheet({ id, onClose }: { id: number | null; onClose: () => void }) {
  const day = useWorkDay(id ?? undefined);
  const row = day.data;

  return (
    <Sheet
      open={id !== null}
      title={row ? dayLabel(row.date) : 'Day'}
      onClose={onClose}
      footer={
        <Button variant="secondary" block onClick={onClose}>
          Close
        </Button>
      }
    >
      {day.isLoading ? <Spinner className="text-slate-400" /> : null}
      {day.isError ? <Banner tone="error">{errorMessage(day.error)}</Banner> : null}
      {row ? (
        <div className="flex flex-col gap-4">
          <p className="flex items-center gap-2 text-sm text-slate-700">
            {hoursLabel(row.hours)} <StatusBadge status={row.status} />
          </p>
          {row.status === 'REJECTED' && row.rejection_reason ? (
            <Banner tone="error">Rejected: {row.rejection_reason}</Banner>
          ) : null}
          {(row.sessions ?? []).map((s) => (
            <SessionBlock key={s.id} session={s} dayRejected={row.status === 'REJECTED'} />
          ))}
        </div>
      ) : null}
    </Sheet>
  );
}

function SessionBlock({ session, dayRejected }: { session: WorkSession; dayRejected: boolean }) {
  const [correcting, setCorrecting] = useState(false);
  const canCorrect = dayRejected && session.can_correct !== false && session.slice_status !== 'APPROVED';

  return (
    <Card className="flex flex-col gap-2">
      <div className="flex items-start justify-between gap-2">
        <div>
          <p className="text-sm font-medium text-slate-900">{session.place_name}</p>
          <p className="text-sm text-slate-600">
            {timeLabel(session.clock_in_at)} to {timeLabel(session.clock_out_at)} ·{' '}
            {hoursLabel(session.hours)}
            {session.project_name ? ` · ${session.project_name}` : ''}
          </p>
        </div>
      </div>
      <p className="text-sm text-slate-600">
        In: {session.in_distance_m != null ? `${formatDistance(session.in_distance_m)} away` : 'no position'}
        {session.radius_m ? ` (limit ${formatDistance(session.radius_m)})` : ''}
        {' · '}Out:{' '}
        {session.out_distance_m != null ? `${formatDistance(session.out_distance_m)} away` : 'no position'}
      </p>
      {session.flags.length > 0 ? (
        <ul className="flex flex-wrap gap-1.5">
          {session.flags.map((f) => (
            <li
              key={f}
              title={flagHint(f)}
              className="rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-900"
            >
              {flagLabel(f)}
            </li>
          ))}
        </ul>
      ) : null}
      {session.added_reason ? (
        <p className="text-sm text-slate-600">Reason given: {session.added_reason}</p>
      ) : null}
      {(session.corrections ?? []).map((c) => (
        <p key={c.id} className="text-sm text-slate-600">
          Corrected to {timeLabel(c.corrected_in_at)} – {timeLabel(c.corrected_out_at)} (was{' '}
          {timeLabel(c.original_in_at)} – {timeLabel(c.original_out_at)}): {c.reason}
        </p>
      ))}
      {canCorrect ? (
        correcting ? (
          <CorrectionForm session={session} onDone={() => setCorrecting(false)} />
        ) : (
          <Button variant="secondary" onClick={() => setCorrecting(true)}>
            Add a correction
          </Button>
        )
      ) : null}
    </Card>
  );
}

function CorrectionForm({ session, onDone }: { session: WorkSession; onDone: () => void }) {
  const correct = useCorrectSession();
  const [inAt, setInAt] = useState(toLocalInput(session.clock_in_at));
  const [outAt, setOutAt] = useState(toLocalInput(session.clock_out_at));
  const [reason, setReason] = useState('');
  const [error, setError] = useState<string | null>(null);

  const submit = () => {
    if (!reason.trim()) {
      setError('Say why the time is being corrected.');
      return;
    }
    if (!inAt || !outAt) {
      setError('Give both the start and the end.');
      return;
    }
    if (new Date(outAt) < new Date(inAt)) {
      setError('The end cannot be before the start.');
      return;
    }
    setError(null);
    correct.mutate(
      {
        id: session.id,
        kind: 'EDIT',
        corrected_in_at: new Date(inAt).toISOString(),
        corrected_out_at: new Date(outAt).toISOString(),
        reason: reason.trim(),
      },
      { onSuccess: onDone, onError: (e) => setError(clockError(e)) },
    );
  };

  return (
    <div className="flex flex-col gap-3 border-t border-slate-200 pt-3">
      <Field label="Started" htmlFor={`in-${session.id}`}>
        <Input
          id={`in-${session.id}`}
          type="datetime-local"
          value={inAt}
          onChange={(e) => setInAt(e.target.value)}
        />
      </Field>
      <Field label="Finished" htmlFor={`out-${session.id}`}>
        <Input
          id={`out-${session.id}`}
          type="datetime-local"
          value={outAt}
          onChange={(e) => setOutAt(e.target.value)}
        />
      </Field>
      <Field
        label="Why"
        htmlFor={`why-${session.id}`}
        hint="The approver reads this beside the original times, which stay on record."
      >
        <Textarea
          id={`why-${session.id}`}
          value={reason}
          onChange={(e) => setReason(e.target.value)}
        />
      </Field>
      {error ? <Banner tone="error">{error}</Banner> : null}
      <div className="flex gap-2">
        <Button loading={correct.isPending} onClick={submit}>
          Send correction
        </Button>
        <Button variant="ghost" onClick={onDone}>
          Cancel
        </Button>
      </div>
    </div>
  );
}
