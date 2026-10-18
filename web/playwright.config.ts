import { defineConfig, devices } from '@playwright/test'

// Browser-level harness (#177). Separate from the Vitest unit suite (#127):
// specs live in e2e/ as *.spec.ts; Vitest only collects src/**.
//
// One-off, out-of-band browser install (never run by `npm ci` / `npm run build`;
// @playwright/test has no install script, so neither downloads a browser):
//   npx playwright install --with-deps chromium
//
// Serving: `vite preview` of the PRODUCTION build (the same dist/ the #57 nginx
// image ships), built first so the suite never runs against a stale dist/.
// This is authoritative for routing, deep-link, reload and history checks.
// LIMITATION: `vite preview` emits no response headers, so it does NOT
// reproduce #57's nginx CSP (default-src 'self'; frame-src
// https://www.youtube-nocookie.com; ...) or `Referrer-Policy: no-referrer`
// (ops/nginx.conf). Embed-under-real-CSP checks are #178's job.
//
// Port 4317 is deliberately not Vite's 5173/4173, and reuseExistingServer is
// off, so a developer's own dev/preview server is never used or killed.
const PORT = 4317

export default defineConfig({
  testDir: './e2e',
  // Explicit, so Playwright's default (which also matches *.test.ts, Vitest's
  // naming) never collects unit tests.
  testMatch: '**/*.spec.ts',
  forbidOnly: true,
  retries: 0,
  reporter: 'list',
  use: { baseURL: `http://localhost:${PORT}` },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: `npm run build && npx vite preview --port ${PORT} --strictPort`,
    url: `http://localhost:${PORT}`,
    reuseExistingServer: false,
    timeout: 120_000,
  },
})
