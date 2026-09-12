/**
 * The offline banner and pending count (design §8.1; N1, T8.3).
 *
 * §8.1 asks for "an **unmistakable** offline banner and a pending-sync count".
 * So it is a full-width bar at the top of every screen, in a colour nothing else
 * uses, and it says what it means in plain words: what is not saved yet, and
 * what will happen to it.
 *
 * It also stays visible while *online* if anything is still queued. A device
 * that reconnected but has three unsent captures is not in a safe state, and
 * hiding the count the moment the radio comes back is how those three get
 * forgotten.
 */

import { Link } from 'react-router-dom';

import { useOffline } from './OfflineProvider';

export function OfflineBanner() {
  const { online, pending, syncing, lastOutcome, syncNow, supported, referenceFetchedAt } =
    useOffline();

  if (online && pending === 0) {
    // Nothing to say. A permanent "you are online" bar is noise, and noise is
    // what makes people stop reading banners.
    return null;
  }

  const offlineTone = 'bg-amber-500 text-amber-950';
  const catchingUpTone = 'bg-sky-600 text-white';

  return (
    <div
      role="status"
      aria-live="polite"
      className={`flex flex-wrap items-center justify-between gap-2 px-4 py-2 text-sm font-medium ${
        online ? catchingUpTone : offlineTone
      }`}
    >
      <span className="flex flex-wrap items-center gap-2">
        {online ? (
          <>
            <span>Back online.</span>
            <span>
              {pending} capture{pending === 1 ? '' : 's'} still to send.
            </span>
          </>
        ) : (
          <>
            <span className="font-semibold">No connection.</span>
            {supported ? (
              <span>
                Gate-in and gate-out still work. Everything you capture is saved
                on this device and sent when you have signal.
              </span>
            ) : (
              // A browser with IndexedDB blocked cannot hold anything. Saying so
              // is far better than letting somebody capture a delivery into
              // nothing.
              <span className="font-semibold">
                This browser cannot save anything offline — do not capture until
                you have signal.
              </span>
            )}
            {pending > 0 ? (
              <span>
                {pending} waiting to send.
              </span>
            ) : null}
            {referenceFetchedAt ? (
              <span className="opacity-80">
                Item list as at {referenceFetchedAt.slice(11, 16)}.
              </span>
            ) : null}
          </>
        )}
        {lastOutcome ? <span className="opacity-90">{lastOutcome}</span> : null}
      </span>

      <span className="flex shrink-0 items-center gap-2">
        {pending > 0 ? (
          <Link
            to="/sync"
            className={`rounded px-2 py-1 underline-offset-2 hover:underline ${
              online ? 'text-white' : 'text-amber-950'
            }`}
          >
            See what is waiting
          </Link>
        ) : null}
        <button
          type="button"
          onClick={() => void syncNow()}
          disabled={syncing}
          className={`min-h-[32px] rounded px-3 py-1 text-sm font-semibold ${
            online ? 'bg-white/20 hover:bg-white/30' : 'bg-amber-950/10 hover:bg-amber-950/20'
          } disabled:opacity-60`}
        >
          {syncing ? 'Syncing…' : online ? 'Send now' : 'Try again'}
        </button>
      </span>
    </div>
  );
}

/** A badge for a record that has not reached the server yet (§8.1). */
export function UnsyncedBadge({ children = 'Not sent yet' }: { children?: string }) {
  return (
    <span className="rounded-full bg-amber-100 px-2 py-0.5 text-xs font-semibold text-amber-900">
      {children}
    </span>
  );
}
