import { expect, test } from './fixtures/offline.ts'

// #343: load a playlist, select two entries with the keyboard, confirm, and
// check the batch request body. Offline: /api/** is stubbed.
const PL = 'https://www.youtube.com/playlist?list=PLabcdefghijkl'
const IDS = ['Ab_-123xyZ0', 'dQw4w9WgXcQ', 'aaaaaaaaaaa']

test('loads a playlist, selects two entries by keyboard and queues them', async ({ page }) => {
  const batches: unknown[] = []
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (request.method() === 'POST' && path === '/api/playlists/resolve') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          playlist_id: 'PLabcdefghijkl',
          title: 'My list',
          total_count: 3,
          truncated: false,
          entries: IDS.map((id, i) => ({
            position: i + 1,
            video_id: id,
            title: `Video ${i + 1}`,
            url: `https://www.youtube.com/watch?v=${id}`,
            known: i === 2,
            processing_status: i === 2 ? 'done' : null,
          })),
        }),
      })
    }
    if (request.method() === 'POST' && path === '/api/videos/batch') {
      const body = request.postDataJSON() as { urls: string[] }
      batches.push(body)
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          counts: { queued: 2 },
          results: body.urls.map((id, index) => ({ index, outcome: 'queued', video_id: id, job_id: index })),
        }),
      })
    }
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ items: [], total: 0, offset: 0, limit: 50 }),
    })
  })

  await page.goto('/playlists/import')
  await page.getByRole('textbox', { name: 'YouTube playlist URL' }).fill(PL)
  await page.keyboard.press('Enter')
  await expect(page.getByRole('heading', { name: 'My list' })).toBeVisible()
  await expect(page).toHaveURL(/\?url=/)

  const first = page.getByRole('checkbox', { name: 'Select Video 1' })
  await first.focus()
  await page.keyboard.press('Space')
  await page.getByRole('checkbox', { name: 'Select Video 2' }).focus()
  await page.keyboard.press('Space')
  await expect(page.getByText('2 selected', { exact: true })).toBeVisible()
  await expect(page.getByRole('checkbox', { name: 'Select Video 3' })).toBeDisabled()

  await page.getByRole('button', { name: 'Add 2 selected videos' }).focus()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('button', { name: 'Confirm' })).toBeFocused()
  await page.keyboard.press('Enter')

  await expect(page.getByText('Queued 2')).toBeVisible()
  expect(batches).toEqual([{ urls: [IDS[0], IDS[1]] }])
})
