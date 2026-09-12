/**
 * The last line of defence (design §7.1; N-1).
 *
 * The app had no error boundary, which meant any error thrown while rendering
 * unmounted the tree and left a blank white screen. On a phone in a yard that is
 * indistinguishable from a dead app: there is nothing to read, nothing to tap,
 * and no reason to think a reload would help.
 *
 * So: catch it, say what to do, and keep the two things that actually recover —
 * reload, and go back to the start. Two states worth telling apart, because the
 * advice differs:
 *
 * - **A stale bundle.** A new version was deployed while this tab was open, so
 *   the screen it wants no longer exists at the URL it knows. Reloading fixes it,
 *   and `lazyRoute` normally reloads before anybody sees this.
 * - **Offline.** The screen was never cached and there is no network to fetch it.
 *   Reloading will not help; going back to a screen that *is* cached will.
 */

import { Component, type ErrorInfo, type ReactNode } from 'react';

import { isChunkLoadFailure } from '../routes/lazyRoute';
import { Button } from './ui';

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Kept to the console rather than sent anywhere: there is no error reporting
    // service in this deployment, and inventing one here would be worse than
    // leaving a trace somebody can read over a storekeeper's shoulder.
    console.error('YardFlow could not render this screen', error, info.componentStack);
  }

  render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;

    const stale = isChunkLoadFailure(error);
    const offline = stale && !navigator.onLine;

    return (
      <div className="flex min-h-full items-center justify-center bg-slate-50 px-4 py-10">
        <div className="w-full max-w-md rounded-lg border border-slate-200 bg-white p-6">
          <h1 className="text-lg font-semibold text-slate-900">
            {offline
              ? 'This screen is not available offline'
              : stale
                ? 'A newer version is available'
                : 'Something went wrong on this screen'}
          </h1>

          <p className="mt-2 text-sm text-slate-600">
            {offline
              ? 'It was never saved to this phone, so it cannot open without a connection. Gate-in and gate-out still work offline.'
              : stale
                ? 'This tab was open when the app was updated. Reload to pick up the new version — nothing you have saved is affected.'
                : 'Your work is not lost. Reload the screen, and if it happens again tell whoever administers this system.'}
          </p>

          <div className="mt-5 flex flex-col gap-2">
            {!offline ? (
              <Button block onClick={() => window.location.reload()}>
                Reload
              </Button>
            ) : null}
            <Button
              variant="secondary"
              block
              // A full navigation rather than a router push: the router is inside
              // the tree that just failed, so pushing may land on the same broken
              // screen.
              onClick={() => {
                window.location.assign('/');
              }}
            >
              Go to the home screen
            </Button>
          </div>

          {/* The message, for whoever is asked to look. Not the stack — a
              storekeeper reading a stack trace is nobody's idea of a fix. */}
          <p className="mt-4 break-words text-xs text-slate-400">{error.message}</p>
        </div>
      </div>
    );
  }
}
