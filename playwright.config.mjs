import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests_e2e',
  timeout: 30_000,
  retries: 1,
  use: {
    baseURL: process.env.E2E_BASE_URL || 'https://hidden-conviction.aotwone.workers.dev',
    headless: true,
    viewport: { width: 1440, height: 1000 },
  },
  reporter: [['list'], ['html', { outputFolder: 'playwright-report', open: 'never' }]],
  outputDir: 'test-results',
});
