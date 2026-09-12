/**
 * Offline state for the whole app (design §8.1; N1, T8.3).
 *
 * §8.1: "the UI shows an unmistakable offline banner and a pending-sync count.
 * Records created offline are badged until confirmed."
 *
 * **Unmistakable** is the requirement, and it is why this is a banner across the
 * top rather than an icon. A storekeeper who does not notice they are offline
 * will capture a delivery, close the app, and assume it is in the system — and
 * the moment they discover otherwise is the moment they stop trusting it.
 *
 * The pending count comes from IndexedDB rather than from memory, so it survives
 * a reload and a force-quit. T8.3's criterion is exactly that: "three gate-ins
 * captured offline are visibly pending **and survive an app restart**."
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';

import { offlineSupported, pendingCount, referenceAge } from './db';
import {
  drainQueue,
  fetchBundle,
  isOnline,
  startOnlineWatch,
  watchOnline,
} from './sync';

interface OfflineValue {
  online: boolean;
  /** How many captures are waiting to reach the server. */
  pending: number;
  /** When the cached reference data was last refreshed (T8.2). */
  referenceFetchedAt: string | null;
  supported: boolean;
  syncing: boolean;
  lastOutcome: string | null;
  refresh: () => Promise<void>;
  syncNow: () => Promise<void>;
}

const OfflineContext = createContext<OfflineValue | null>(null);

/** How often the pending count is re-read. Cheap: one IndexedDB count. */
const POLL_MS = 5_000;

export function OfflineProvider({ children }: { children: ReactNode }) {
  const [online, setOnlineState] = useState(isOnline());
  const [pending, setPending] = useState(0);
  const [referenceFetchedAt, setReferenceFetchedAt] = useState<string | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [lastOutcome, setLastOutcome] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setPending(await pendingCount());
    setReferenceFetchedAt(await referenceAge());
  }, []);

  const runSync = useCallback(async () => {
    setSyncing(true);
    try {
      const bundle = await fetchBundle();
      const outcome = await drainQueue();
      if (!bundle.ok) {
        setLastOutcome('Still no connection.');
      } else if (outcome.idle) {
        setLastOutcome(
          `Up to date. ${bundle.passes} approved pass${bundle.passes === 1 ? '' : 'es'} ready for offline release.`,
        );
      } else {
        const parts = [
          outcome.applied ? `${outcome.applied} sent` : '',
          outcome.rejected ? `${outcome.rejected} refused — see exceptions` : '',
        ].filter(Boolean);
        setLastOutcome(parts.join(', ') || 'Nothing to send.');
      }
    } finally {
      setSyncing(false);
      await refresh();
    }
  }, [refresh]);

  useEffect(() => {
    void refresh();
    const stopWatch = startOnlineWatch();
    const stopListening = watchOnline((value) => {
      setOnlineState(value);
      void refresh();
    });
    const timer = window.setInterval(() => void refresh(), POLL_MS);

    return () => {
      stopWatch();
      stopListening();
      window.clearInterval(timer);
    };
  }, [refresh]);

  // Drain on arrival, so a device that comes back into signal while the app is
  // open catches up without anybody pressing anything.
  useEffect(() => {
    if (online && pending > 0 && !syncing) {
      void runSync();
    }
    // Deliberately not depending on `syncing`: this should fire when coming
    // online or when something is queued, not when a sync finishes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [online, pending]);

  const value = useMemo<OfflineValue>(
    () => ({
      online,
      pending,
      referenceFetchedAt,
      supported: offlineSupported(),
      syncing,
      lastOutcome,
      refresh,
      syncNow: runSync,
    }),
    [online, pending, referenceFetchedAt, syncing, lastOutcome, refresh, runSync],
  );

  return <OfflineContext.Provider value={value}>{children}</OfflineContext.Provider>;
}

export function useOffline(): OfflineValue {
  const value = useContext(OfflineContext);
  if (!value) {
    throw new Error('useOffline must be used inside an OfflineProvider.');
  }
  return value;
}
