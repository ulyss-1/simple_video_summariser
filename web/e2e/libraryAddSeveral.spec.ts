import { expect, test } from './fixtures/offline.ts'

// #240: paste a list into the Library's "Add several videos" form, submit it
// with the keyboard and read the per-line outcome. Offline: /api/** is stubbed.
test('submits a pasted list with the keyboard and reports a bad line', async ({ page }) => {
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
          counts: { queued: 1, invalid: 1 },
          results: [
            { index: 0, outcome: 'queued', video_id: 'Ab_-123xyZ0', job_id: 1 },
            { index: 1, outcome: 'invalid', reason: 'not_a_video_ref' },
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
  const area = page.getByRole('textbox', { name: /Add several videos/ })
  await area.focus()
  await page.keyboard.insertText('Ab_-123xyZ0\nnope')
  await page.keyboard.press('Tab')
  await expect(page.getByRole('button', { name: 'Add videos' })).toBeFocused()
  await page.keyboard.press('Enter')

  await expect(page.getByText('Queued 1', { exact: true })).toBeVisible()
  await expect(page.getByText('Line 2 (nope) is not a YouTube video URL or ID')).toBeVisible()
  expect(bodies).toEqual([{ urls: ['Ab_-123xyZ0', 'nope'] }])
})
