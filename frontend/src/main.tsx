/**
 * Application entry point (design §7.1).
 *
 * TanStack Query provides the cache, retry and offline-friendly behaviour §1.1
 * asks for; the retry policy below is the part that matters on a cellular
 * connection (N-1).
 */

import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter } from 'react-router-dom';

import './index.css';
import { ApiError } from './api/client';
import { recoverFromStaleBundle } from './routes/lazyRoute';
import { SessionProvider } from './auth/session';
import { OfflineProvider } from './offline/OfflineProvider';
import { registerServiceWorker } from './offline/register';
import { AppRoutes } from './routes';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // A flaky cellular connection deserves a retry; a 404 or a permission
      // failure does not — retrying those just delays the error (§6.1).
      retry: (failureCount, error) => {
        if (error instanceof ApiError) {
          if (error.status >= 400 && error.status < 500) return false;
        }
        return failureCount < 2;
      },
      staleTime: 30_000,
      refetchOnWindowFocus: false,
    },
  },
});

// Vite fires this when a *preload* of a route chunk fails — before React ever
// tries to render it, so the error boundary never sees it and the default
// behaviour is an unhandled rejection. Same cause as a failed import (a build
// deployed while this tab was open) and the same cure.
window.addEventListener('vite:preloadError', (event) => {
  if (recoverFromStaleBundle()) event.preventDefault();
});

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <SessionProvider>
          {/* N1: the offline state wraps the routes, so the banner and the
              pending count are available on every screen. */}
          <OfflineProvider>
            <AppRoutes />
          </OfflineProvider>
        </SessionProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);

// T8.1: the service worker precaches the shell so gate-in and gate-out load with
// no network. Registered after render, so a failure here can never stop the app
// from starting — an unregistered worker means "no offline support", not "broken".
void registerServiceWorker();
