/**
 * Lazy route loading that survives a deployment (design §7.1, §8.1).
 *
 * Every screen is code-split, so the page holds URLs for chunks belonging to the
 * bundle it loaded with. Deploy a new build and those filenames change: the tab
 * somebody left open overnight then asks for a chunk that no longer exists, gets
 * a 404, and React's `lazy` rejects. With no error boundary above it that
 * unmounted the whole tree and left a blank screen — which is what a storekeeper
 * saw on the first tap after a release, on the three screens they use most.
 *
 * The fix is a reload, because a reload fetches the current `index.html` and with
 * it the current chunk names. Two guards make that safe:
 *
 * - **Only once.** A flag in `sessionStorage` means a chunk that is genuinely
 *   missing shows an error instead of reloading forever. A reload loop is worse
 *   than the blank screen it replaces.
 * - **Only when online.** Offline, the chunk is missing because the service
 *   worker never cached it, and reloading cannot conjure it. Better to let the
 *   boundary say so.
 *
 * Anything that is not a chunk failure is rethrown untouched — a component that
 * throws on render is a bug to fix, not a reason to reload.
 */

import { lazy, type ComponentType } from 'react';

const RELOADED = 'yardflow:reloaded-for-stale-bundle';

/** Storage throws in some privacy modes, and a logo is not worth a crash. */
function remember(key: string): boolean {
  try {
    if (sessionStorage.getItem(key)) return false;
    sessionStorage.setItem(key, '1');
    return true;
  } catch {
    // No storage: allow the reload once per page load rather than never. The
    // in-memory guard below still prevents a loop within this session.
    return !reloadedThisPageLoad;
  }
}

function forget(key: string): void {
  try {
    sessionStorage.removeItem(key);
  } catch {
    /* nothing to clean up */
  }
}

let reloadedThisPageLoad = false;

/**
 * Is this the browser failing to fetch a module, rather than the module failing?
 *
 * Matched on the message because there is no error type to check: each engine
 * words it differently, and Vite adds its own.
 */
export function isChunkLoadFailure(error: unknown): boolean {
  const message = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
  return (
    /failed to fetch dynamically imported module/i.test(message) ||
    /error loading dynamically imported module/i.test(message) ||
    /importing a module script failed/i.test(message) ||
    /ChunkLoadError/i.test(message) ||
    // Safari, which reports the URL and little else.
    /Unable to load script|Importing a module script failed/i.test(message)
  );
}

/** Reload once to pick up the current bundle. Returns false if it will not. */
export function recoverFromStaleBundle(): boolean {
  if (!navigator.onLine) return false;
  if (!remember(RELOADED)) return false;
  reloadedThisPageLoad = true;
  window.location.reload();
  return true;
}

/**
 * `lazy`, plus recovery from a stale bundle.
 *
 * Use this for every route. The import is retried by reloading the page rather
 * than by asking again for the same missing URL — asking twice for a 404 is just
 * a slower 404.
 */
export function lazyRoute<T extends ComponentType<unknown>>(load: () => Promise<{ default: T }>) {
  return lazy(() =>
    load().then(
      (module) => {
        // Got there: this bundle is current, so a future failure is allowed its
        // own reload.
        forget(RELOADED);
        return module;
      },
      (error: unknown) => {
        if (isChunkLoadFailure(error) && recoverFromStaleBundle()) {
          // The reload is already underway. Never resolving is deliberate: it
          // leaves the spinner up for the half-second until the page goes, which
          // is what somebody should see, rather than an error that is about to
          // be irrelevant.
          return new Promise<{ default: T }>(() => {});
        }
        throw error;
      },
    ),
  );
}
