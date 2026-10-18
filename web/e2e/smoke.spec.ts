import { expect, test } from './fixtures/offline.ts'

// Smoke: the production bundle loads in Chromium and deep links render.
// /api/** is stubbed; nothing here assumes a backend.
test.beforeEach(async ({ page }) => {
  await page.route('**/api/**', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }),
  )
})

test('the root route renders the Library view', async ({ page }) => {
  const response = await page.goto('/')
  expect(response?.status()).toBe(200)
  await expect(page.getByRole('heading', { level: 1, name: 'Library' })).toBeVisible()
  expect(new URL(page.url()).pathname).toBe('/')
})

test('a /ops deep link renders the Ops view, not the Library or error boundary', async ({ page }) => {
  const response = await page.goto('/ops')
  expect(response?.status()).toBe(200)
  await expect(page.getByRole('heading', { level: 1, name: 'Ops' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Library' })).toHaveCount(0)
  await expect(page.getByRole('heading', { name: 'Something went wrong' })).toHaveCount(0)
  expect(new URL(page.url()).pathname).toBe('/ops')
})
