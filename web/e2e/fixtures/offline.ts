import { test as base, expect } from '@playwright/test'

// Default-deny network guard (#177): the browser analogue of
// UnexpectedNetworkAccessError in src/test/setup.ts. Registered first, so it is
// the fallback; routes a spec registers later take precedence ("stubbed").
// Allowed through: the harness's own origin, except /api/**, which has no
// backend here and must be stubbed (vite preview would answer it with index.html).
export const test = base.extend<{ blocked: string[] }>({
  blocked: [
    async ({ page, baseURL }, use, testInfo) => {
      const own = new URL(baseURL ?? '').origin
      const blocked: string[] = []
      await page.route('**/*', (route) => {
        const request = route.request()
        const url = new URL(request.url())
        if (url.origin === own && !url.pathname.startsWith('/api/')) {
          return route.fallback()
        }
        blocked.push(`${request.method()} ${request.url()} (${testInfo.file})`)
        return route.abort('blockedbyclient')
      })
      await use(blocked)
      expect(blocked, 'unexpected network access').toEqual([])
    },
    { auto: true },
  ],
})

export { expect }
