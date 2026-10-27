import { expect, test } from './fixtures/offline.ts'

// #340: unfollow with the keyboard only, and delete a channel and land on the
// Library with the notice. Offline: /api/** is stubbed with page.route.
const UC = 'UC' + 'a'.repeat(22)

test.describe('channel page controls', () => {
  let active: boolean
  let requests: { method: string; path: string; body: unknown }[]

  test.beforeEach(async ({ page }) => {
    active = true
    requests = []
    await page.route('**/api/**', async (route) => {
      const request = route.request()
      const path = new URL(request.url()).pathname
      const method = request.method()
      requests.push({ method, path, body: request.postData() === null ? null : request.postDataJSON() })
      const fulfil = (body: unknown, status = 200) =>
        route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
      if (path === `/api/channels/${UC}` && method === 'GET')
        return fulfil({
          channel_id: UC,
          title: 'Stub channel',
          active,
          added_at: '2026-09-01T00:00:00Z',
          monitor_from: '2026-09-01T00:00:00Z',
          video_count: 2,
        })
      if (path === `/api/channels/${UC}/unfollow` && method === 'POST') {
        active = false
        return fulfil({ channel_id: UC, active: false, changed: true, backfill: null })
      }
      if (path === `/api/channels/${UC}` && method === 'DELETE')
        return fulfil({ channel_id: UC, deleted_videos: 2, cancelled_backfill: null })
      if (path === `/api/channels/${UC}/catalog/discover` || path === `/api/channels/${UC}/catalog`)
        return fulfil({ channel_id: UC, limit: 50, total_count: 0, items: [] })
      if (path === '/api/videos') return fulfil({ items: [], total: 0, offset: 0, limit: 50 })
      return fulfil({ items: [], total: 0, offset: 0, limit: 200 })
    })
  })

  test('unfollows with the keyboard only', async ({ page }) => {
    await page.goto(`/channels/${UC}/catalog`)
    await expect(page.getByRole('heading', { level: 2, name: 'Channel' })).toBeVisible()
    await page.getByRole('button', { name: 'Unfollow channel' }).focus()
    await page.keyboard.press('Enter')
    await expect(page.getByRole('button', { name: 'Keep following' })).toBeFocused()
    await page.keyboard.press('Shift+Tab')
    await expect(page.getByRole('button', { name: 'Confirm unfollow' })).toBeFocused()
    await page.keyboard.press('Enter')

    await expect(page.getByText('Channel unfollowed', { exact: true })).toBeVisible()
    await expect(page.getByText('Not followed')).toBeVisible()
    expect(requests.filter((r) => r.method === 'POST' && r.path.endsWith('/unfollow'))).toEqual([
      { method: 'POST', path: `/api/channels/${UC}/unfollow`, body: {} },
    ])
  })

  test('deletes the channel and lands on the Library with the notice', async ({ page }) => {
    await page.goto(`/channels/${UC}/catalog`)
    await page.getByRole('button', { name: 'Delete channel and its videos' }).click()
    await expect(page.getByRole('button', { name: 'Cancel' })).toBeFocused()
    await page.getByRole('button', { name: 'Confirm delete' }).click()

    await expect(page.getByRole('heading', { level: 1, name: 'Library' })).toBeVisible()
    await expect(page.getByText('Channel deleted · 2 videos removed', { exact: true })).toBeVisible()
    await expect(page).toHaveURL(/\/$/)
    expect(requests.filter((r) => r.method === 'DELETE')).toHaveLength(1)
  })
})
