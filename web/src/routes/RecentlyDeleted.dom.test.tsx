import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { components } from '../api'
import { setupUser } from '../test/user'
import { RecentlyDeleted } from './RecentlyDeleted'

type Item = components['schemas']['DeletedVideoItem']

const user = setupUser
const ID1 = 'aaaaaaaaaa1'
const ID2 = 'aaaaaaaaaa2'
const ID3 = 'aaaaaaaaaa3'

function item(id: string, over: Partial<Item> = {}): Item {
  return {
    video_id: id,
    title: `Title ${id}`,
    channel_id: null,
    channel_title: 'Some channel',
    deleted_at: '2026-10-01T23:30:00-07:00',
    restorable: true,
    ...over,
  }
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

interface Call {
  url: URL
  method: string
  body: string | null
}
let calls: Call[]

function stub(all: Item[], post: () => Response | Promise<Response> = () => json({ created: true, video_id: ID1, job_id: 1, state: 'queued' }, 202)) {
  calls = []
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'http://localhost')
      const method = init?.method ?? 'GET'
      calls.push({ url, method, body: typeof init?.body === 'string' ? init.body : null })
      if (method === 'POST' && url.pathname === '/api/videos') return Promise.resolve(post())
      if (url.pathname === '/api/deleted-videos') {
        const offset = Number(url.searchParams.get('offset') ?? 0)
        const limit = Number(url.searchParams.get('limit') ?? 50)
        return Promise.resolve(json({ items: all.slice(offset, offset + limit), total: all.length }))
      }
      throw new Error(`unexpected request ${method} ${url.pathname}`)
    }),
  )
}

function mount(entry = '/deleted', pageSize = 2) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const router = createMemoryRouter(
    [
      { path: '/deleted', element: <RecentlyDeleted pageSize={pageSize} /> },
      { path: '/videos/:videoId', element: <p>detail page</p> },
    ],
    { initialEntries: [entry] },
  )
  render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return router
}

const posts = () => calls.filter((c) => c.method === 'POST')

beforeEach(() => {
  vi.unstubAllGlobals()
})

describe('RecentlyDeleted', () => {
  it('lists rows with title, channel and a UTC date, and a safe YouTube link', async () => {
    stub([item(ID1), item(ID2, { title: null, channel_title: null, channel_id: 'UCx' }), item('bad', { title: null, channel_title: null })])
    mount('/deleted', 5)
    expect(await screen.findByRole('heading', { level: 1, name: 'Recently deleted' })).toBeTruthy()
    expect(screen.getByText(/Deleted videos are gone for good/)).toBeTruthy()
    const rows = await screen.findAllByRole('row')
    expect(within(rows[1] as HTMLElement).getByText('Title ' + ID1)).toBeTruthy()
    const link = within(rows[1] as HTMLElement).getByRole('link', { name: 'Open on YouTube' })
    expect(link.getAttribute('href')).toBe(`https://www.youtube.com/watch?v=${ID1}`)
    expect(link.getAttribute('rel')).toBe('noopener noreferrer')
    expect(within(rows[1] as HTMLElement).getByText('2026-10-02 06:30 UTC').getAttribute('datetime')).toBe(
      '2026-10-01T23:30:00-07:00',
    )
    expect(within(rows[2] as HTMLElement).getByText('UCx')).toBeTruthy()
    expect(within(rows[3] as HTMLElement).getByText('Unknown channel')).toBeTruthy()
    expect(within(rows[3] as HTMLElement).queryByRole('link')).toBeNull()
  })

  it('shows the empty text', async () => {
    stub([])
    mount()
    expect(await screen.findByText('No deleted videos')).toBeTruthy()
  })

  it('shows an error with Retry, and the 503 text', async () => {
    stub([])
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(json({ detail: 'x' }, 503))))
    mount()
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('The database is unavailable. Try again shortly.')
    expect(within(alert).getByRole('button', { name: 'Retry' })).toBeTruthy()
  })

  it('pages 2 per page with the page in the URL', async () => {
    stub([item(ID1), item(ID2), item(ID3)])
    const router = mount()
    expect(await screen.findByText('1–2 of 3')).toBeTruthy()
    const previous = screen.getByRole('button', { name: 'Previous' }) as HTMLButtonElement
    expect(previous.disabled).toBe(true)
    await user().click(screen.getByRole('button', { name: 'Next' }))
    expect(await screen.findByText('3–3 of 3')).toBeTruthy()
    expect(router.state.location.search).toBe('?page=2')
    expect((screen.getByRole('button', { name: 'Next' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('restores: confirm focus, one POST, live text, link, list refetched', async () => {
    const all = [item(ID1)]
    stub(all)
    mount()
    const u = user()
    await u.click(await screen.findByRole('button', { name: `Restore Title ${ID1}` }))
    expect(screen.getByText(`Restore Title ${ID1}? It is processed again from the start, which can take a while.`)).toBeTruthy()
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Confirm restore' }))
    await u.click(screen.getByRole('button', { name: 'Confirm restore' }))
    expect(await screen.findByText('Restored: processing again')).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Open video' }).getAttribute('href')).toBe(`/videos/${ID1}`)
    expect(posts()).toHaveLength(1)
    expect(JSON.parse(posts()[0]?.body ?? '')).toEqual({ url: ID1 })
    expect(document.activeElement?.textContent).toBe(`Restored Title ${ID1}: processing again`)
    expect(calls.filter((c) => c.url.pathname === '/api/deleted-videos').length).toBeGreaterThan(1)
  })

  it('Cancel and Escape return focus to Restore', async () => {
    stub([item(ID1)])
    mount()
    const u = user()
    const restore = await screen.findByRole('button', { name: `Restore Title ${ID1}` })
    await u.click(restore)
    await u.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(document.activeElement).toBe(screen.getByRole('button', { name: `Restore Title ${ID1}` }))
    await u.click(screen.getByRole('button', { name: `Restore Title ${ID1}` }))
    await u.keyboard('{Escape}')
    expect(screen.queryByRole('button', { name: 'Confirm restore' })).toBeNull()
    expect(document.activeElement).toBe(screen.getByRole('button', { name: `Restore Title ${ID1}` }))
    expect(posts()).toHaveLength(0)
  })

  it('a non-restorable row has no Restore and says why', async () => {
    stub([item(ID1, { restorable: false })])
    mount()
    expect(await screen.findByText('Still stopping its processing. Try again in a minute.')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Restore/ })).toBeNull()
  })

  it('a 409 shows the same text and refetches the list', async () => {
    stub([item(ID1)], () => json({ detail: 'busy' }, 409))
    mount()
    const u = user()
    await u.click(await screen.findByRole('button', { name: `Restore Title ${ID1}` }))
    const before = calls.filter((c) => c.url.pathname === '/api/deleted-videos').length
    await u.click(screen.getByRole('button', { name: 'Confirm restore' }))
    expect(await screen.findByText('Still stopping its processing. Try again in a minute.')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Confirm restore' })).toBeNull()
    expect(calls.filter((c) => c.url.pathname === '/api/deleted-videos').length).toBeGreaterThan(before)
  })

  it.each([
    [422, 'Could not restore the video. Reload the page and try again.'],
    [429, 'Too many requests. Try again shortly.'],
    [503, 'The database is unavailable. Try again shortly.'],
  ])('a %s keeps the confirmation open with fixed text', async (status, text) => {
    stub([item(ID1)], () => json({ detail: 'secret server text' }, status))
    mount()
    const u = user()
    await u.click(await screen.findByRole('button', { name: `Restore Title ${ID1}` }))
    await u.click(screen.getByRole('button', { name: 'Confirm restore' }))
    expect((await screen.findByRole('alert')).textContent).toBe(text)
    expect(screen.getByRole('button', { name: 'Confirm restore' })).toBeTruthy()
    expect(document.body.textContent).not.toContain('secret server text')
  })

  it('a network failure keeps the confirmation open with the connection text', async () => {
    stub([item(ID1)], () => {
      throw new TypeError('Failed to fetch')
    })
    mount()
    const u = user()
    await u.click(await screen.findByRole('button', { name: `Restore Title ${ID1}` }))
    await u.click(screen.getByRole('button', { name: 'Confirm restore' }))
    expect((await screen.findByRole('alert')).textContent).toBe(
      'Could not restore the video. Check your connection and try again.',
    )
  })
})
