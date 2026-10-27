import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import { setupUser } from '../test/user'
import { routes } from './index'

// #340: between a settled DELETE and the unmount of the page, Confirm must not
// send again. `navigate` is replaced by a no-op that resolves at once, so the
// page deterministically stays mounted after the request has settled.
vi.mock('react-router', async (importOriginal) => ({
  ...(await importOriginal<typeof import('react-router')>()),
  useNavigate: () => () => Promise.resolve(),
}))

const UC = 'UC' + 'a'.repeat(22)

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

describe('the delete guard after a settled request', () => {
  it('stays latched and disabled while the page is still mounted', async () => {
    const deletes: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL, init: RequestInit = {}) => {
        const url = new URL(String(input), 'http://localhost')
        const method = (init.method ?? 'GET').toUpperCase()
        if (method === 'DELETE') {
          deletes.push(url.pathname)
          return Promise.resolve(json({ channel_id: UC, deleted_videos: 1, cancelled_backfill: null }))
        }
        if (method === 'GET' && url.pathname === `/api/channels/${UC}`)
          return Promise.resolve(
            json({
              channel_id: UC,
              title: 'Example channel',
              active: true,
              added_at: '2026-09-01T00:00:00Z',
              monitor_from: '2026-09-01T00:00:00Z',
              video_count: 1,
            }),
          )
        return Promise.resolve(json({ channel_id: UC, limit: 50, total_count: 0, items: [] }))
      }),
    )
    const u = setupUser()
    const router = createMemoryRouter(routes, { initialEntries: [`/channels/${UC}/catalog`] })
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )
    await u.click(await screen.findByRole('button', { name: 'Delete channel and its videos' }))
    const confirm = screen.getByRole('button', { name: 'Confirm delete' })
    await u.click(confirm)
    await waitFor(() => expect((confirm as HTMLButtonElement).disabled).toBe(true))
    // Let every callback of the settled mutation run, then try again.
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect((confirm as HTMLButtonElement).disabled).toBe(true)
    await u.click(confirm)
    expect(deletes).toEqual([`/api/channels/${UC}`])
  })
})
