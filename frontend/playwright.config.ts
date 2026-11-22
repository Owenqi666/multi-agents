import { defineConfig } from '@playwright/test';

// Browser end-to-end paths against the offline HTTP service. Run from frontend/ after a build;
// `pnpm run e2e` does both. One worker: each test starts its own environment.
export default defineConfig({
  testDir: 'e2e',
  timeout: 20 * 60_000,
  workers: 1,
  retries: 0,
  reporter: [['list'], ['json', { outputFile: 'e2e/records/report.json' }]],
  expect: { timeout: 30_000 },
  // The scenarios read the Chinese interface; a zh-CN browser opens it in Chinese on the first visit.
  use: { trace: 'off', locale: 'zh-CN', viewport: { width: 1280, height: 900 }, reducedMotion: 'reduce', actionTimeout: 60_000 },
});
