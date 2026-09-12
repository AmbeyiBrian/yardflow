/**
 * Playwright configuration (design §14; T8.13).
 *
 * §14 asks for the two flows end to end "on a mobile viewport", and the viewport
 * is not decoration: every screen in this product was built for a phone in a
 * yard, and a suite that only ever ran at 1280px would pass while the thing
 * people actually use is broken. So the default project is a Pixel-sized
 * viewport, and a desktop project runs the same specs to catch the reverse.
 *
 * The tests drive a **real backend against a seeded tenant** rather than mocking
 * the API. A mocked end-to-end test proves the frontend agrees with a fixture
 * somebody wrote, which is exactly the agreement that has already gone wrong
 * twice in this project — the trailing-slash bug and the dropped serials both
 * passed every mocked expectation there was.
 */

import { defineConfig, devices } from '@playwright/test';

/** Where the tenant is addressed. Subdomains are per tenant (§2.2). */
const BASE_URL = process.env.E2E_BASE_URL ?? 'http://demo.localhost:5173';

export default defineConfig({
  testDir: './e2e',
  // A yard flow is a sequence — receive, request, approve, release — so the
  // specs run in file order and share a signed-in state where they say so.
  fullyParallel: false,
  workers: 1,
  // Generous: these hit a real database and a real Django dev server.
  timeout: 90_000,
  expect: { timeout: 15_000 },
  reporter: process.env.CI ? [['github'], ['list']] : [['list']],
  retries: process.env.CI ? 1 : 0,

  use: {
    baseURL: BASE_URL,
    // On failure, the trace is what makes a CI run diagnosable without a local
    // repro — which for a flow this long is the difference between fixing it and
    // guessing.
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },

  projects: [
    {
      name: 'phone',
      use: { ...devices['Pixel 7'] },
    },
    {
      name: 'desktop',
      use: { ...devices['Desktop Chrome'] },
    },
  ],

  // Started by hand in development; CI runs both processes itself. Not declared
  // as a webServer here because Django needs its own database and migrations,
  // and a half-configured autostart failing is harder to read than a clear
  // "nothing is listening on 5173".
});
