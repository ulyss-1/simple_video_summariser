import { expect, test } from './fixtures/offline.ts'

// #234: select two Library rows with the keyboard only, confirm the bulk
// delete and see the result. Offline: /api/** is stubbed with page.route.
const videos = [1, 2, 3].map((n) => ({
  video_id: `v${String(n).padStart(10, '0')}`,
  title: `Bulk video ${n}`,
  channel_id: null,
  channel_title: null,
  published_at: '2026-09-01T12:00:00Z',
  duration_sec: 60,
  latest_analysis_at: null,
  has_transcript: false,
  origin: 'manual',
  unavailable: null,
  status: 'done',
  active_job: null,
  last_failure: null,
}))

test('selects two rows with the keyboard, confirms and sees the result', async ({ page }) => {
  let remaining = videos
  const bodies: unknown[] = []
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (request.method() === 'POST' && path === '/api/videos/delete') {
      const body = request.postDataJSON() as { video_ids: string[] }
      bodies.push(body)
      remaining = remaining.filter((v) => !body.video_ids.includes(v.video_id))
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          counts: { deleted: body.video_ids.length, not_found: 0 },
          results: body.video_ids.map((video_id) => ({ video_id, outcome: 'deleted' })),
        }),
      })
    }
    const body =
      path === '/api/videos'
        ? { items: remaining, total: remaining.length, offset: 0, limit: 50 }
        : { items: [], total: 0, offset: 0, limit: 50 }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
  })

  await page.goto('/')
  const first = page.getByRole('checkbox', { name: 'Select Bulk video 1' })
  await first.focus()
  await page.keyboard.press('Space')
  // Next stops: the row's title link, then the second row's checkbox (no channel button, no transcript link).
  await page.keyboard.press('Tab')
  await page.keyboard.press('Tab')
  await expect(page.getByRole('checkbox', { name: 'Select Bulk video 2' })).toBeFocused()
  await page.keyboard.press('Space')
  await expect(page.getByText('2 selected', { exact: true })).toBeVisible()

  const del = page.getByRole('button', { name: 'Delete 2 selected videos' })
  await del.focus()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('button', { name: 'Cancel' })).toBeFocused()
  await page.keyboard.press('Shift+Tab')
  await expect(page.getByRole('button', { name: 'Confirm delete' })).toBeFocused()
  await page.keyboard.press('Enter')

  await expect(page.getByText('Deleted 2 videos', { exact: true })).toBeVisible()
  expect(bodies).toEqual([{ video_ids: [videos[0]?.video_id, videos[1]?.video_id] }])
  await expect(page.getByRole('checkbox', { name: 'Select Bulk video 3' })).toBeVisible()
  await expect(page.getByRole('checkbox', { name: 'Select Bulk video 1' })).toHaveCount(0)
})
