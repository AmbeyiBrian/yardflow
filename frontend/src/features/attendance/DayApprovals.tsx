/**
 * Approvals, Days tab (design §4.18.5, §4.18.11; R13).
 *
 * A day is split into slices, one per approver (a PM per project, the
 * Director role for the rest). The sheet shows every session with its
 * distances, flags and corrections (original beside corrected, with the
 * reason), says which slice is mine and which wait on someone else, and lets
 * me approve or reject my own slice only. The server enforces that; this
 * screen just does not offer what it would refuse.
 */

import { useState } from 'react';

import { useList } from '../../api/hooks';
import { Banner, Button, Card, Field, Spinner, Textarea } from '../../components/ui';
import { EmptyState, ListState, Sheet, StatusBadge } from '../../components/ui/data';
import { useDecideDay, useWorkDay } from './api';
import { canDecide, correctionPair, sliceLabel, splitSlices } from './approvals';
import { formatDistance } from './nearby';
import { clockError, flagHint, flagLabel, hoursLabel } from './rules';
import type { WorkDay, WorkSession } from './types';

const dayLabel = (iso: string) =>
  new Date(`${iso}T12:00:00`).toLocaleDateString([], {
    weekday: 'short',
    day: 'numeric',
    month: 'short',
  });

const timeLabel = (iso: string | null) =>
  iso ? new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : 'open';

/** One session, read-only: place, times, hours, distances, flags, corrections. */
export function SessionReadout({ session }: { session: WorkSession }) {
  return (
    <Card className="flex flex-col gap-2">
      <div>
        <p className="text-sm font-medium text-slate-900">{session.place_name}</p>
        <p className="text-sm text-slate-600">
          {timeLabel(session.clock_in_at)} to {timeLabel(session.clock_out_at)} ·{' '}
          {hoursLabel(session.hours)}
          {session.project_name ? ` · ${session.project_name}` : ''}
        </p>
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
      {(session.corrections ?? []).map((c) => {
        const pair = correctionPair(c, timeLabel);
        return (
          <div key={c.id} className="rounded-lg bg-slate-50 p-2 text-sm text-slate-700">
            <p>
              <span className="text-slate-500">Original:</span> {pair.original}
            </p>
            <p>
              <span className="text-slate-500">Corrected:</span>{' '}
              <span className="font-medium">{pair.corrected}</span>
            </p>
            <p className="text-slate-600">Why: {c.reason}</p>
          </div>
        );
      })}
    </Card>
  );
}

export function DayApprovalQueue() {
  const days = useList<WorkDay>('work-days', { awaiting_me: true, page_size: 100 });
  const [openId, setOpenId] = useState<number | null>(null);

  return (
    <div className="flex flex-col gap-3">
      <ListState query={days}>
        {(days.data?.results ?? []).length === 0 ? (
          <EmptyState title="Nothing waiting." hint="Days to approve appear here." />
        ) : (
          <ul className="flex flex-col gap-2">
            {(days.data?.results ?? []).map((d) => (
              <li key={d.id}>
                <button
                  type="button"
                  onClick={() => setOpenId(d.id)}
                  className="flex min-h-[44px] w-full flex-col gap-1 rounded-xl border border-slate-200 bg-white p-4 text-left"
                >
                  <span className="flex items-center justify-between gap-2">
                    <span className="text-sm font-medium text-slate-900">
                      {d.person_name ?? 'Someone'}
                    </span>
                    <span className="text-sm text-slate-700">{hoursLabel(d.hours)}</span>
                  </span>
                  <span className="text-sm text-slate-600">{dayLabel(d.date)}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </ListState>
      {openId !== null ? <DecideDaySheet key={openId} id={openId} onClose={() => setOpenId(null)} /> : null}
    </div>
  );
}

function DecideDaySheet({ id, onClose }: { id: number; onClose: () => void }) {
  const day = useWorkDay(id);
  const decide = useDecideDay();
  const [reason, setReason] = useState('');
  const [needReason, setNeedReason] = useState(false);
  const [error, setError] = useState('');
  const row = day.data;
  const mayDecide = row ? canDecide(row) : false;
  const { others } = splitSlices(row?.slices);

  function run(approved: boolean) {
    if (!approved && !reason.trim()) {
      setNeedReason(true);
      return;
    }
    setError('');
    decide.mutate(
      { id, approved, reason: approved ? '' : reason.trim() },
      { onSuccess: onClose, onError: (e) => setError(clockError(e)) },
    );
  }

  return (
    <Sheet
      open
      title={row ? `${row.person_name ?? 'Day'}, ${dayLabel(row.date)}` : 'Day'}
      onClose={onClose}
      footer={
        mayDecide ? (
          <>
            <Button variant="danger" className="flex-1" disabled={decide.isPending} onClick={() => run(false)}>
              Reject
            </Button>
            <Button className="flex-1" disabled={decide.isPending} onClick={() => run(true)}>
              {decide.isPending ? <Spinner /> : 'Approve'}
            </Button>
          </>
        ) : (
          <Button variant="secondary" block onClick={onClose}>
            Close
          </Button>
        )
      }
    >
      <div className="flex flex-col gap-3">
        {day.isLoading ? <Spinner className="text-slate-400" /> : null}
        {day.isError ? <Banner tone="error">{clockError(day.error)}</Banner> : null}
        {row ? (
          <>
            <p className="flex items-center gap-2 text-sm text-slate-700">
              {hoursLabel(row.hours)} <StatusBadge status={row.status} />
            </p>
            {(row.slices ?? []).length > 0 ? (
              <ul className="flex flex-col gap-1 text-sm text-slate-700">
                {(row.slices ?? []).map((s) => (
                  <li key={s.id} className={s.is_mine ? 'font-medium text-slate-900' : undefined}>
                    {sliceLabel(s)}
                  </li>
                ))}
              </ul>
            ) : null}
            {mayDecide && others.some((s) => s.status === 'PENDING') ? (
              <Banner tone="info">
                You decide your part only. The rest of the day waits on the others.
              </Banner>
            ) : null}
            {(row.sessions ?? []).map((s) => (
              <SessionReadout key={s.id} session={s} />
            ))}
            {mayDecide ? (
              <Field
                label="Reason"
                htmlFor="day-reason"
                hint="Required to reject. The person sees it and can correct the day."
                error={needReason && !reason.trim() ? 'Say why you are rejecting it.' : undefined}
              >
                <Textarea
                  id="day-reason"
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                />
              </Field>
            ) : null}
          </>
        ) : null}
        {error ? <Banner tone="error">{error}</Banner> : null}
      </div>
    </Sheet>
  );
}
