import { defineConfig } from '@playwright/test';

// Browser end-to-end paths on T-0069's offline environment. Run from frontend/ after a build;
// `pnpm run e2e` does both. One worker: each test starts its own environment.
export default defineConfig({
  testDir: 'e2e',
  timeout: 20 * 60_000,
  workers: 1,
  retries: 0,
  reporter: [['list'], ['json', { outputFile: 'e2e/records/report.json' }]],
  expect: { timeout: 30_000 },
  use: { trace: 'off', viewport: { width: 1280, height: 900 }, reducedMotion: 'reduce', actionTimeout: 60_000 },
});
