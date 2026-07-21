import { defineConfig, devices } from '@playwright/test'

const baseURL = process.env.PLAYWRIGHT_BASE_URL ?? 'http://127.0.0.1:8080'

export default defineConfig({
  testDir: './e2e',
  globalSetup: './e2e/globalSetup.ts',
  forbidOnly: Boolean(process.env.CI),
  fullyParallel: false,
  retries: process.env.CI ? 2 : 0,
  workers: 1,
  // Playwright keeps per-test output by default and can write a failure DOM
  // context even when traces, screenshots, and video are disabled. Never
  // retain that directory because authentication pages receive credentials.
  preserveOutput: 'never',
  reporter: [['list']],
  use: {
    baseURL,
    // A service worker can satisfy a request before Playwright's route guard.
    // Writing E2E runs do not need offline behavior, so disable registration.
    serviceWorkers: 'block',
    // E2E login uses supplied credentials. Do not retain browser captures that
    // can contain an authentication flow in CI failure artifacts.
    trace: 'off',
    screenshot: 'off',
    video: 'off',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
})
