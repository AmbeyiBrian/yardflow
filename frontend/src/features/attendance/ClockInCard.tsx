/**
 * The clock-in card (design §4.18.11; R13).
 *
 * Clocked out: reads the position once, lists clockable places nearest first
 * ("You are 80 m from X"), asks which project when the chosen site has two or
 * more open, and clocks in. The server makes the area check, so a refusal
 * ("You are 640 m from X (limit 200 m)", "Turn location on to clock in") is
 * shown as it states it.
 *
 * Clocked in: the place, the time since, and Clock out. Clock-out is never
 * blocked on position (R13); it says afterwards if the phone was outside.
 *
 * Online only for now; offline capture arrives with T16.15.
 */

import { useEffect, useMemo, useRef, useState } from 'react';

import { useList } from '../../api/hooks';
import { Banner, Button, Card, Field, Select, Spinner } from '../../components/ui';
import { newUuid } from '../../offline/db';
import { useClockIn, useClockOut, useOpenSession } from './api';
import { formatDistance, formatElapsed, sortNearby, suggest, type NearbyPlace } from './nearby';
import { usePlaces } from './places';
import { positionMessage, PositionError, readPosition } from './position';
import { clockError } from './rules';
import type { Fix, WorkSession } from './types';
import type { Project } from '../projects/types';

type Reading =
  | { state: 'reading' }
  | { state: 'ok'; fix: Fix }
  | { state: 'error'; message: string };

function usePosition() {
  const [reading, setReading] = useState<Reading>({ state: 'reading' });
  const run = useRef(0);

  const read = () => {
    const mine = ++run.current;
    setReading({ state: 'reading' });
    readPosition().then(
      (fix) => mine === run.current && setReading({ state: 'ok', fix }),
      (error: unknown) =>
        mine === run.current &&
        setReading({
          state: 'error',
          message: positionMessage(error instanceof PositionError ? error.code : 'UNAVAILABLE'),
        }),
    );
  };

  return { reading, read };
}

function timeOfDay(iso: string) {
  return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

export function ClockInCard() {
  const open = useOpenSession();
  if (open.isLoading) {
    return (
      <Card>
        <Spinner className="text-slate-400" />
      </Card>
    );
  }
  if (open.isError) {
    return (
      <Banner tone="error">Could not check whether you are clocked in. Reload to try again.</Banner>
    );
  }
  return open.data ? <ClockedIn session={open.data} /> : <ClockedOut />;
}

function ClockedIn({ session }: { session: WorkSession }) {
  const clockOut = useClockOut();
  const [now, setNow] = useState(() => Date.now());
  const [done, setDone] = useState<WorkSession | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const uuid = useRef(newUuid());

  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(t);
  }, []);

  const submit = async () => {
    setError(null);
    setBusy(true);
    // R13: a clock-out is never refused on position, so a failed read is not
    // a reason to stay clocked in; it goes without a fix and is flagged.
    const fix = await readPosition().catch(() => null);
    clockOut.mutate(
      { fix, session_client_uuid: String(session.id), client_uuid: uuid.current },
      {
        onSuccess: (row) => setDone(row),
        onError: (e) => setError(clockError(e)),
        onSettled: () => setBusy(false),
      },
    );
  };

  const outside = done?.flags?.includes('OUTSIDE_AT_CLOCK_OUT');
  const noPosition = done?.flags?.includes('NO_POSITION_AT_CLOCK_OUT');

  return (
    <Card className="flex flex-col gap-3">
      <div>
        <h2 className="text-sm font-semibold text-slate-900">
          {done ? 'Clocked out' : `Clocked in at ${session.place_name}`}
        </h2>
        {done ? (
          <p className="text-sm text-slate-600">
            {session.place_name}, {timeOfDay(session.clock_in_at)} to{' '}
            {done.clock_out_at ? timeOfDay(done.clock_out_at) : 'now'}.
          </p>
        ) : (
          <p className="text-sm text-slate-600">
            Since {timeOfDay(session.clock_in_at)} ·{' '}
            {formatElapsed(now - new Date(session.clock_in_at).getTime())}
            {session.project_name ? ` · ${session.project_name}` : ''}
          </p>
        )}
      </div>
      {outside ? (
        <Banner tone="warning">
          You clocked out{' '}
          {done?.out_distance_m != null ? formatDistance(done.out_distance_m) : 'away'} from{' '}
          {session.place_name}. It is recorded, and the approver will see it.
        </Banner>
      ) : null}
      {noPosition ? (
        <Banner tone="warning">
          Your phone gave no position, so the clock-out is recorded without one. The approver
          will see that.
        </Banner>
      ) : null}
      {error ? <Banner tone="error">{error}</Banner> : null}
      {done ? null : (
        <Button block loading={busy} onClick={submit}>
          Clock out
        </Button>
      )}
    </Card>
  );
}

function ClockedOut() {
  const { places, loading: placesLoading, failed } = usePlaces();
  const { reading, read } = usePosition();
  const clockIn = useClockIn();
  const [picked, setPicked] = useState<string | null>(null);
  const [projectPick, setProjectPick] = useState('');
  const [error, setError] = useState<string | null>(null);
  const uuid = useRef(newUuid());

  // One read when the card opens; "Read my position again" for the rest.
  useEffect(() => {
    read();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const fix = reading.state === 'ok' ? reading.fix : null;
  const sorted = useMemo<NearbyPlace[]>(() => (fix ? sortNearby(fix, places) : []), [fix, places]);
  const chosen = useMemo(() => {
    const key = picked ?? (() => {
      const s = suggest(sorted);
      return s ? `${s.kind}:${s.id}` : null;
    })();
    return sorted.find((p) => `${p.kind}:${p.id}` === key) ?? null;
  }, [picked, sorted]);

  const projects = useList<Project>(
    'projects',
    { site: chosen?.id, status: 'OPEN', page_size: 50 },
    { enabled: chosen?.kind === 'site' },
  );
  const candidates = useMemo(
    () =>
      chosen?.kind === 'site'
        ? (projects.data?.results ?? []).filter((p) => p.sites.includes(chosen.id))
        : [],
    [projects.data, chosen],
  );
  const needsProject = candidates.length >= 2;
  const project = needsProject ? projectPick : '';

  const submit = () => {
    if (!chosen || !fix) return;
    setError(null);
    clockIn.mutate(
      {
        ...(chosen.kind === 'site' ? { site: chosen.id } : { location: chosen.id }),
        ...(project ? { project: Number(project) } : {}),
        fix,
        client_uuid: uuid.current,
      },
      {
        onSuccess: () => {
          uuid.current = newUuid();
        },
        onError: (e) => setError(clockError(e)),
      },
    );
  };

  return (
    <Card className="flex flex-col gap-3">
      <div>
        <h2 className="text-sm font-semibold text-slate-900">Clock in</h2>
        <p className="text-sm text-slate-600">You are not clocked in.</p>
      </div>

      {reading.state === 'reading' ? (
        <p className="flex items-center gap-2 text-sm text-slate-600">
          <Spinner className="text-slate-400" /> Finding your position…
        </p>
      ) : null}

      {reading.state === 'error' ? (
        <Banner tone="warning">
          <span className="flex flex-col items-start gap-2">
            <span>{reading.message}</span>
            <Button variant="secondary" onClick={read}>
              Try again
            </Button>
          </span>
        </Banner>
      ) : null}

      {failed ? <Banner tone="error">Could not load the places you can clock in at.</Banner> : null}

      {fix && !placesLoading && sorted.length === 0 && !failed ? (
        <p className="text-sm text-slate-600">
          No site, yard or office has coordinates yet, so there is nowhere to clock in. Ask an
          administrator to add them in settings.
        </p>
      ) : null}

      {fix && sorted.length > 0 ? (
        <fieldset className="flex flex-col gap-2">
          <legend className="sr-only">Where are you?</legend>
          {sorted.slice(0, 8).map((p) => {
            const key = `${p.kind}:${p.id}`;
            const selected = chosen ? `${chosen.kind}:${chosen.id}` === key : false;
            const inside = p.distance_m <= p.radius_m;
            return (
              <label
                key={key}
                className={`flex min-h-[44px] cursor-pointer items-center gap-3 rounded-lg border px-3 py-2 ${
                  selected ? 'border-slate-900 bg-slate-50' : 'border-slate-200'
                }`}
              >
                <input
                  type="radio"
                  name="clock-place"
                  checked={selected}
                  onChange={() => {
                    setPicked(key);
                    setProjectPick('');
                    setError(null);
                  }}
                />
                <span className="flex flex-col">
                  <span className="text-sm font-medium text-slate-900">
                    {p.name} <span className="font-normal text-slate-500">· {p.label}</span>
                  </span>
                  <span className={`text-sm ${inside ? 'text-emerald-700' : 'text-slate-600'}`}>
                    You are {formatDistance(p.distance_m)} from {p.name}
                    {inside ? '' : ` (limit ${formatDistance(p.radius_m)})`}
                  </span>
                </span>
              </label>
            );
          })}
        </fieldset>
      ) : null}

      {needsProject ? (
        <Field label="Which project?" htmlFor="clock-project">
          <Select
            id="clock-project"
            value={projectPick}
            onChange={(e) => setProjectPick(e.target.value)}
          >
            <option value="">Choose a project</option>
            {candidates.map((p) => (
              <option key={p.id} value={p.id}>
                {p.reference} · {p.title}
              </option>
            ))}
          </Select>
        </Field>
      ) : null}

      {error ? <Banner tone="error">{error}</Banner> : null}

      {fix ? (
        <div className="flex flex-col gap-2">
          <Button
            block
            loading={clockIn.isPending}
            disabled={!chosen || (needsProject && !projectPick)}
            onClick={submit}
          >
            Clock in{chosen ? ` at ${chosen.name}` : ''}
          </Button>
          <Button variant="ghost" onClick={read}>
            Read my position again
          </Button>
        </div>
      ) : null}
    </Card>
  );
}
