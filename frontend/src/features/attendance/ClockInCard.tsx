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
 * Offline (§4.18.9; R6, R13): with no signal, or when the server cannot be
 * reached, the phone checks the area itself against the cached bundle and
 * queues the clock-in; the open session is kept locally so the card still says
 * "clocked in" and Clock out still works. A queued entry reads "Waiting to
 * send"; a refused one stays here with the server's reason.
 */

import { useQueryClient } from '@tanstack/react-query';
import { useEffect, useMemo, useRef, useState } from 'react';

import { ApiError } from '../../api/client';
import { useList } from '../../api/hooks';
import { Banner, Button, Card, Field, Select, Spinner } from '../../components/ui';
import { newUuid } from '../../offline/db';
import { useOffline } from '../../offline/OfflineProvider';
import { isOnline, setOnline } from '../../offline/sync';
import { isNetworkError } from '../money/drafts';
import { useClockIn, useClockOut, useOpenSession } from './api';
import { formatDistance, formatElapsed, sortNearby, suggest, type NearbyPlace } from './nearby';
import { queueClockIn, queueClockOut, saveOpenSession, useQueuedAttendance } from './offline';
import { usePlaces, useAccuracyCap } from './places';
import { positionMessage, PositionError, readPosition } from './position';
import {
  earlyRefusal,
  resolveSession,
  sessionFromServer,
  type LocalSession,
} from './queued';
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
  const queued = useQueuedAttendance();
  const { places } = usePlaces();
  const { pending } = useOffline();
  const queryClient = useQueryClient();
  // A clock-out that has just been made stays on screen with its message until dismissed.
  const [held, setHeld] = useState<LocalSession | null>(null);

  // §4.18.9: what lands while the card is open changes the server's answer.
  useEffect(() => {
    void queryClient.invalidateQueries({ queryKey: ['work-sessions'] });
  }, [pending, queryClient]);

  // Keep the phone's copy of the open session current whenever the server has
  // answered and nothing is waiting, so a later offline Clock out has it.
  const serverKnown = open.isSuccess;
  const serverSession = serverKnown ? (open.data ?? null) : null;
  useEffect(() => {
    if (!serverKnown || !queued.ready || queued.waiting) return;
    void saveOpenSession(serverSession ? sessionFromServer(serverSession, places) : null);
  }, [serverKnown, serverSession, queued.ready, queued.waiting, places]);

  const session = resolveSession({
    server: serverKnown
      ? serverSession
        ? sessionFromServer(serverSession, places)
        : null
      : undefined,
    local: queued.local,
    waiting: queued.waiting,
  });

  if (!queued.ready || (open.isLoading && !session && !queued.waiting)) {
    return (
      <Card>
        <Spinner className="text-slate-400" />
      </Card>
    );
  }
  if (open.isError && !isNetworkError(open.error) && !session) {
    return (
      <Banner tone="error">Could not check whether you are clocked in. Reload to try again.</Banner>
    );
  }

  const refused = queued.entries.filter((entry) => entry.state === 'REFUSED');
  const waiting = queued.entries.filter((entry) => entry.state === 'WAITING');
  const shown = held ?? session;

  return (
    <div className="flex flex-col gap-3">
      {refused.map((entry) => (
        <Banner key={entry.client_uuid} tone="error">
          Your {entry.summary.toLowerCase()} was refused: {entry.reason || 'the server did not accept it'}.
          It stays on this phone. If you were there, ask the Director to add the day.
        </Banner>
      ))}
      {waiting.length > 0 ? (
        <Banner tone="info">
          Waiting to send: {waiting.map((entry) => entry.summary).join(', ')}. It goes when your
          phone has signal.
        </Banner>
      ) : null}
      {shown ? (
        <ClockedIn
          key={shown.session_client_uuid}
          session={shown}
          onDone={setHeld}
          onDismiss={held ? () => setHeld(null) : undefined}
        />
      ) : (
        <ClockedOut />
      )}
    </div>
  );
}

function ClockedIn({
  session,
  onDone,
  onDismiss,
}: {
  session: LocalSession;
  onDone: (session: LocalSession) => void;
  onDismiss?: () => void;
}) {
  const clockOut = useClockOut();
  const [now, setNow] = useState(() => Date.now());
  const [done, setDone] = useState<WorkSession | 'queued' | null>(null);
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
    const queueIt = async () => {
      await queueClockOut({ session, fix }, uuid.current);
      onDone(session);
      setDone('queued');
    };
    try {
      if (!isOnline()) {
        await queueIt();
      } else {
        try {
          const row = await clockOut.mutateAsync({
            fix,
            session_client_uuid: session.session_client_uuid,
            client_uuid: uuid.current,
          });
          onDone(session);
          setDone(row);
        } catch (e) {
          // The same client_uuid is queued, so if the server did save it the
          // replay finds that row rather than making a second (§4.18.9).
          if (!isNetworkError(e)) throw e;
          if (!(e instanceof ApiError)) setOnline(false);
          await queueIt();
        }
      }
    } catch (e) {
      setError(clockError(e));
    } finally {
      setBusy(false);
    }
  };

  const row = done && done !== 'queued' ? done : null;
  const outside = row?.flags?.includes('OUTSIDE_AT_CLOCK_OUT');
  const noPosition = row?.flags?.includes('NO_POSITION_AT_CLOCK_OUT');

  return (
    <Card className="flex flex-col gap-3">
      <div>
        <h2 className="text-sm font-semibold text-slate-900">
          {done ? 'Clocked out' : `Clocked in at ${session.place_name}`}
        </h2>
        {done ? (
          <p className="text-sm text-slate-600">
            {session.place_name}, {timeOfDay(session.clock_in_at)} to{' '}
            {row?.clock_out_at ? timeOfDay(row.clock_out_at) : 'now'}.
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
          {row?.out_distance_m != null ? formatDistance(row.out_distance_m) : 'away'} from{' '}
          {session.place_name}. It is recorded, and the approver will see it.
        </Banner>
      ) : null}
      {noPosition ? (
        <Banner tone="warning">
          Your phone gave no position, so the clock-out is recorded without one. The approver
          will see that.
        </Banner>
      ) : null}
      {done === 'queued' ? (
        <Banner tone="info">
          Waiting to send. The clock-out is saved on your phone and goes when it has signal.
        </Banner>
      ) : null}
      {!done && session.queued ? (
        <Banner tone="info">
          Waiting to send. The clock-in is saved on your phone and goes when it has signal.
        </Banner>
      ) : null}
      {error ? <Banner tone="error">{error}</Banner> : null}
      {done ? (
        onDismiss ? (
          <Button block variant="secondary" onClick={onDismiss}>
            Done
          </Button>
        ) : null
      ) : (
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
  const cap = useAccuracyCap();
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
  const candidates = useMemo<{ id: number; reference: string; title: string }[]>(() => {
    if (chosen?.kind !== 'site') return [];
    // No answer from the server (offline): the bundle's open projects for the site.
    if (!projects.data) return chosen.open_projects ?? [];
    return projects.data.results.filter((p) => p.sites.includes(chosen.id));
  }, [projects.data, chosen]);
  const needsProject = candidates.length >= 2;
  const project = needsProject ? projectPick : '';

  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!chosen || !fix) return;
    setError(null);
    setBusy(true);
    const queueIt = async () => {
      // §4.18.9: the phone refuses early, in the server's words, from the
      // area and cap it holds; the replay checks again at captured_at.
      const refusal = earlyRefusal(fix, chosen, cap);
      if (refusal) {
        setError(refusal);
        return;
      }
      const picked = candidates.find((p) => String(p.id) === project);
      await queueClockIn(
        {
          place: chosen,
          project: project ? Number(project) : null,
          project_name: picked ? `${picked.reference} · ${picked.title}` : null,
          fix,
        },
        uuid.current,
      );
      uuid.current = newUuid();
    };
    try {
      if (!isOnline()) {
        await queueIt();
      } else {
        try {
          await clockIn.mutateAsync({
            ...(chosen.kind === 'site' ? { site: chosen.id } : { location: chosen.id }),
            ...(project ? { project: Number(project) } : {}),
            fix,
            client_uuid: uuid.current,
          });
          uuid.current = newUuid();
        } catch (e) {
          // Queued under the same client_uuid: a response lost on the way back
          // cannot become a second clock-in (N2).
          if (!isNetworkError(e)) throw e;
          if (!(e instanceof ApiError)) setOnline(false);
          await queueIt();
        }
      }
    } catch (e) {
      setError(clockError(e));
    } finally {
      setBusy(false);
    }
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
            loading={busy}
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
