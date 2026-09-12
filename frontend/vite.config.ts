import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { VitePWA } from 'vite-plugin-pwa';

// Design §12.0: the Vite dev server proxies /api to Django, so a single origin
// serves the app in development and no CORS configuration is needed locally.
//
// Tenants are addressed by subdomain (§2.2), e.g. http://silvertech.localhost:5173.
// `host: true` accepts any Host header so those names resolve, and the proxy
// forwards the Host unchanged — without that, Django would resolve the wrong
// tenant, or none.
export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
    // T8.1, §8.1: Workbox precaches the app shell so the two offline flows load
    // with no network. Deliberately narrow — §8 keeps offline capture to gate-in
    // and gate-out, and precaching the reporting suite would mean a technician
    // downloading megabytes they will never open (N-1).
    VitePWA({
      registerType: 'autoUpdate',
      // Off in development, and opt-in when you actually want to test offline:
      //
      //     VITE_DEV_SW=true npm run dev
      //
      // It used to be on by default, and the cost was a day of confusion. A
      // service worker in development precaches the dev bundle and then keeps
      // serving it — and because a controlling worker answers navigations from
      // its own cache, **Ctrl+Shift+R does not get past it**. Every code change
      // looks like it did nothing, and old API URLs keep being requested from a
      // build that no longer exists on disk. Nothing about the offline flows
      // (§8) needs testing on every ordinary run.
      devOptions: { enabled: process.env.VITE_DEV_SW === 'true', type: 'module' },
      // The manifest is a plain file in `public/`, not generated here.
      //
      // The plugin only injects its `<link rel="manifest">` when its dev service
      // worker is enabled, and that is off by default — a worker caching a dev
      // bundle makes every code change look like it did nothing. Installability
      // should not depend on a caching option, so the link lives in index.html
      // and the manifest beside the icons it names.
      manifest: false,
      workbox: {
        // The shell and every JS/CSS chunk. Route-level splitting means the
        // gate-in and gate-out bundles come along with it, which is what T8.1
        // asks for; the shell is useless without them.
        globPatterns: ['**/*.{js,css,html,svg,woff2}'],
        navigateFallback: '/index.html',
        // Never intercept the API. An offline write must reach the queue (§8.1)
        // rather than a cached 200 that pretends it succeeded — a cached POST
        // response would be the worst possible lie to tell a storekeeper.
        navigateFallbackDenylist: [/^\/api/, /^\/attachments/],
        runtimeCaching: [
          {
            // Reference data is cached in IndexedDB by the bundle fetch, so the
            // only thing worth a runtime cache is the read that populates it.
            urlPattern: /\/api\/v1\/sync\/bundle$/,
            handler: 'NetworkFirst',
            options: {
              cacheName: 'yardflow-bundle',
              networkTimeoutSeconds: 8,
              expiration: { maxEntries: 2, maxAgeSeconds: 60 * 60 * 24 },
            },
          },
        ],
      },
    }),
  ],
  server: {
    host: true,
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: false,
      },
      '/attachments': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: false,
      },
      // A tenant's logo (A4). Django serves it under MEDIA_URL on 8000; without
      // this the app asks 5173 for it and the letterhead is a broken image on
      // every screen that previews a document. In production the file comes
      // from S3 as a pre-signed URL, so nothing is proxied there.
      '/media': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: false,
      },
    },
  },
  build: {
    // Long-cache hashed assets behind CloudFront (§12.1).
    sourcemap: true,
  },
});
