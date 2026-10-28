import { expect, test } from './fixtures/offline.ts'

// #236: open Recently deleted from the nav and restore one row with the
// keyboard only. Offline: /api/** is stubbed with page.route.
const ID = 'aaaaaaaaaa1'

test('restores a deleted video with the keyboard only', async ({ page }) => {
  let rows = [
    {
      video_id: ID,
      title: 'Gone video',
      channel_id: null,
      channel_title: 'Some channel',
      deleted_at: '2026-10-01T12:00:00Z',
      restorable: true,
    },
  ]
  const bodies: unknown[] = []
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (request.method() === 'POST' && path === '/api/videos') {
      bodies.push(request.postDataJSON())
      rows = []
      return route.fulfill({
        status: 202,
        contentType: 'application/json',
        body: JSON.stringify({ created: true, video_id: ID, job_id: 1, state: 'queued' }),
      })
    }
    const body =
      path === '/api/deleted-videos'
        ? { items: rows, total: rows.length }
        : { items: [], total: 0, offset: 0, limit: 50 }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
  })

  await page.goto('/')
  const nav = page.getByRole('link', { name: 'Recently deleted' })
  await nav.focus()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('heading', { level: 1, name: 'Recently deleted' })).toBeVisible()

  const restore = page.getByRole('button', { name: 'Restore Gone video' })
  await restore.focus()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('button', { name: 'Confirm restore' })).toBeFocused()
  await page.keyboard.press('Enter')

  await expect(page.getByText('Restored Gone video: processing again', { exact: true })).toBeFocused()
  expect(bodies).toEqual([{ url: ID }])
  await expect(page.getByText('No deleted videos')).toBeVisible()
})
