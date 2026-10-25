import type { Page } from '@playwright/test'
import { expect, test } from './fixtures/offline.ts'
import { ID, TITLE, stubRoutingApi } from './fixtures/routingStub.ts'

// #122's five real-browser routing criteria (deep link, reload, query string,
// Back/Forward, no document request) plus not-found, on the #177 harness.
// Offline: /api/** and the embed are stubbed (fixtures/routingStub.ts).
const ROUTES = [
  { path: '/', heading: 'Library' },
  { path: `/videos/${ID}`, heading: TITLE },
  { path: `/videos/${ID}/transcript`, heading: TITLE },
  { path: `/videos/${ID}/compare`, heading: TITLE },
  { path: '/search', heading: 'Search' },
  { path: '/ops', heading: 'Ops' },
] as const

test.beforeEach(async ({ page }) => {
  await stubRoutingApi(page)
})

const loc = (page: Page) => page.evaluate(() => ({ pathname: location.pathname, search: location.search }))

async function expectView(page: Page, heading: string, pathname: string, search = ''): Promise<void> {
  await expect(page.getByRole('heading', { level: 1, name: heading, exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Page not found' })).toHaveCount(0)
  await expect(page.getByRole('heading', { name: 'Something went wrong' })).toHaveCount(0)
  expect(await loc(page)).toEqual({ pathname, search })
}

for (const { path, heading } of ROUTES) {
  test(`(1) deep link ${path} returns 200 and renders its own view`, async ({ page }) => {
    const response = await page.goto(path)
    expect(response?.status()).toBe(200)
    await expectView(page, heading, path)
  })

  test(`(2) reload on ${path} re-renders the same view`, async ({ page }) => {
    await page.goto(path)
    await expectView(page, heading, path)
    const response = await page.reload()
    expect(response?.status()).toBe(200)
    await expectView(page, heading, path)
  })
}

const QUERIES = [
  { path: '/', search: '?page=2&status=failed', heading: 'Library' },
  { path: '/search', search: '?q=hello%20world&page=3', heading: 'Search' },
  { path: `/videos/${ID}`, search: '?t=3723', heading: TITLE },
  { path: `/videos/${ID}/transcript`, search: '?page=3', heading: TITLE },
  { path: '/ops', search: '?state=dead&before=100', heading: 'Ops' },
] as const

for (const { path, search, heading } of QUERIES) {
  test(`(3) ${path}${search} keeps its exact query string through load, reload and Back/Forward`, async ({ page }) => {
    await page.goto(path + search)
    await expectView(page, heading, path, search)
    await page.reload()
    await expectView(page, heading, path, search)

    // Leave for another route in-app, then come back with Back and Forward.
    await page.getByRole('link', { name: path === '/ops' ? 'Library' : 'Ops', exact: true }).click()
    await expect(page.getByRole('heading', { level: 1, name: path === '/ops' ? 'Library' : 'Ops' })).toBeVisible()
    await page.goBack()
    await expectView(page, heading, path, search)
    await page.goForward()
    await expect(page.getByRole('heading', { level: 1, name: path === '/ops' ? 'Library' : 'Ops' })).toBeVisible()
    await page.goBack()
    await expectView(page, heading, path, search)
  })
}

test('(4) Back/Forward restores the right view and search at each step', async ({ page }) => {
  await page.goto('/')
  await expectView(page, 'Library', '/')
  const nav = page.getByRole('navigation')
  await nav.getByRole('link', { name: 'Search', exact: true }).click()
  await expectView(page, 'Search', '/search')
  // The search form is the app's own in-app navigation that carries a query.
  await page.getByRole('searchbox').fill('a b')
  await page.getByRole('searchbox').press('Enter')
  await expect.poll(async () => (await loc(page)).search).not.toBe('')
  const search = (await loc(page)).search
  expect(new URLSearchParams(search).get('q')).toBe('a b')
  await nav.getByRole('link', { name: 'Ops', exact: true }).click()
  await expectView(page, 'Ops', '/ops')

  await page.goBack()
  await expectView(page, 'Search', '/search', search)
  await page.goBack()
  await expectView(page, 'Search', '/search')
  await page.goForward()
  await expectView(page, 'Search', '/search', search)
})

test('(5) an in-app NavLink click makes no document request; a plain anchor does', async ({ page }) => {
  await page.goto('/')
  await expectView(page, 'Library', '/')
  const documents: string[] = []
  page.on('request', (r) => {
    if (r.resourceType() === 'document') documents.push(new URL(r.url()).pathname)
  })

  await page.getByRole('navigation').getByRole('link', { name: 'Search', exact: true }).click()
  await expectView(page, 'Search', '/search')
  await page.getByRole('navigation').getByRole('link', { name: 'Ops', exact: true }).click()
  await expectView(page, 'Ops', '/ops')
  expect(documents, 'router links must not reload the document').toEqual([])

  // Control: the plain <a href="/api/healthz"> is not intercepted by the router.
  const control = page.waitForRequest((r) => r.resourceType() === 'document' && new URL(r.url()).pathname === '/api/healthz')
  await page.locator('.shell-nav__link--aside').click()
  await control
  expect(documents).toEqual(['/api/healthz'])
})

test('(6) an unknown path renders the not-found view with the path as text', async ({ page }) => {
  const response = await page.goto('/no-such-page')
  expect(response?.status()).toBe(200)
  await expect(page.getByRole('heading', { level: 1, name: 'Page not found' })).toBeVisible()
  await expect(page.locator('code')).toHaveText('/no-such-page')
  await expect(page.getByRole('heading', { name: 'Something went wrong' })).toHaveCount(0)
})
