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
// (ops/nginx.conf). Embed-under-real-CSP checks live in the opt-in
// `real-embed` project below (#178).
//
// Port 4317 is deliberately not Vite's 5173/4173, and reuseExistingServer is
// off, so a developer's own dev/preview server is never used or killed.
const PORT = 4317

// Opt-in `real-embed` project (#178): the real youtube-nocookie.com embed under
// the REAL production CSP and `Referrer-Policy: no-referrer`. Needs Docker and
// real YouTube network, so it is never part of `npm run test:e2e` or the CI e2e
// job. Run it on demand:  npm run test:e2e:real-embed
// Playwright has one global webServer, so the opt-in run (E2E_REAL_EMBED=1)
// swaps the whole config: only the real-embed project, served by the #57 nginx
// production image (ops/Dockerfile.frontend + ops/nginx.conf), whose own
// headers the browser receives. No policy string is copied here.
const REAL_EMBED = process.env['E2E_REAL_EMBED'] === '1'
const REAL_PORT = 4318
const REAL_NAME = 'ytdigest-web-e2e'

export default defineConfig({
  testDir: './e2e',
  // Explicit, so Playwright's default (which also matches *.test.ts, Vitest's
  // naming) never collects unit tests.
  testMatch: '**/*.spec.ts',
  forbidOnly: true,
  retries: 0,
  reporter: 'list',
  // Playwright kills the docker CLI, not the container it started.
  globalTeardown: REAL_EMBED ? './e2e/real-embed/teardown.ts' : undefined,
  use: { baseURL: `http://localhost:${REAL_EMBED ? REAL_PORT : PORT}` },
  projects: REAL_EMBED
    ? [
        {
          name: 'real-embed',
          testMatch: 'real-embed/**/*.spec.ts',
          use: { ...devices['Desktop Chrome'] },
        },
      ]
    : [
        {
          name: 'chromium',
          testIgnore: 'real-embed/**',
          use: { ...devices['Desktop Chrome'] },
        },
      ],
  webServer: REAL_EMBED
    ? {
        // Build context is the repo root (the Dockerfile COPYs web/ and ops/).
        command:
          `docker rm -f ${REAL_NAME} >/dev/null 2>&1; ` +
          `docker build -f ../ops/Dockerfile.frontend -t ${REAL_NAME}:latest .. && ` +
          `exec docker run --rm --name ${REAL_NAME} -p 127.0.0.1:${REAL_PORT}:80 ${REAL_NAME}:latest`,
        url: `http://localhost:${REAL_PORT}`,
        reuseExistingServer: false,
        timeout: 300_000,
      }
    : {
        command: `npm run build && npx vite preview --port ${PORT} --strictPort`,
        url: `http://localhost:${PORT}`,
        reuseExistingServer: false,
        timeout: 120_000,
      },
})
