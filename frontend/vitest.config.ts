import { defineConfig } from 'vitest/config';

// T11.11: unit tests are the pure modules under src/ (readLabel, matchScan).
// A separate file from vite.config.ts so a test run never builds the PWA
// plugin, and `e2e/` stays Playwright's.
export default defineConfig({
  test: {
    include: ['src/**/*.test.ts'],
  },
});
