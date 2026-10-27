import { expect, test } from './fixtures/offline.ts'

// #240: open the Library's "Add several videos" disclosure with the keyboard,
// submit three lines (one invalid, one duplicate) and read three results.
// Offline: /api/** is stubbed.
test('opens the disclosure by keyboard and shows one result per line', async ({ page }) => {
  const bodies: unknown[] = []
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (request.method() === 'POST' && path === '/api/videos/batch') {
      bodies.push(request.postDataJSON())
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          counts: { queued: 1, duplicate: 1, invalid: 1 },
          results: [
            { index: 0, outcome: 'queued', video_id: 'Ab_-123xyZ0', job_id: 1 },
            { index: 1, outcome: 'duplicate', video_id: 'Ab_-123xyZ0', first_index: 0 },
            { index: 2, outcome: 'invalid', reason: 'not_a_video_ref' },
          ],
        }),
      })
    }
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ items: [], total: 0, offset: 0, limit: 50 }),
    })
  })

  await page.goto('/')
  const disclosure = page.getByRole('button', { name: 'Add several videos' })
  await expect(disclosure).toHaveAttribute('aria-expanded', 'false')
  await disclosure.focus()
  await page.keyboard.press('Enter')
  await expect(disclosure).toHaveAttribute('aria-expanded', 'true')

  const area = page.getByRole('textbox', { name: 'YouTube video URLs or IDs, one per line' })
  await area.focus()
  await page.keyboard.insertText('Ab_-123xyZ0\nhttps://youtu.be/Ab_-123xyZ0\nnope')
  await page.keyboard.press('Tab')
  await expect(page.getByRole('button', { name: 'Add videos' })).toBeFocused()
  await page.keyboard.press('Enter')

  const items = page.getByRole('list', { name: 'Results' }).getByRole('listitem')
  await expect(items).toHaveCount(3)
  await expect(items.nth(0)).toContainText('Queued: Ab_-123xyZ0')
  await expect(items.nth(1)).toHaveText('Line 2: same video as line 1')
  await expect(items.nth(2)).toHaveText('Line 3: not a YouTube video URL or ID (nope…)')
  await expect(page.getByText('Queued 1 · Not valid 1')).toBeVisible()
  expect(bodies).toEqual([{ urls: ['Ab_-123xyZ0', 'https://youtu.be/Ab_-123xyZ0', 'nope'] }])
})
