import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { components } from '../api'
import { setupUser } from '../test/user'
import { routes } from './index'

// #340: unfollow or delete a channel from its page, and the Library link to it.
// `fetch` is stubbed; one channel with 0 to 3 videos per test.

type Detail = components['schemas']['ChannelDetailOut']

const UC = 'UC' + 'a'.repeat(22)
const PAGE = `/channels/${UC}/catalog`
const user = setupUser

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

function detail(over: Partial<Detail> = {}): Detail {
  return {
    channel_id: UC,
    title: 'Example channel',
    active: true,
    added_at: '2026-09-01T10:00:00Z',
    monitor_from: '2026-09-02T23:30:00Z',
    video_count: 3,
    ...over,
  }
}

interface Call {
  method: string
  path: string
  body: unknown
}

let calls: Call[]
let current: Detail
let getDetail: () => Response | Promise<Response>
let unfollow: () => Response | Promise<Response>
let remove: () => Response | Promise<Response>
let libraryRows: unknown[]

beforeEach(() => {
  calls = []
  current = detail()
  libraryRows = []
  getDetail = () => json(current)
  unfollow = () => {
    current = { ...current, active: false }
    return json({ channel_id: UC, active: false, changed: true, backfill: null })
  }
  remove = () => json({ channel_id: UC, deleted_videos: current.video_count, cancelled_backfill: null })
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init: RequestInit = {}) => {
      const url = new URL(String(input), 'http://localhost')
      const method = (init.method ?? 'GET').toUpperCase()
      const body = typeof init.body === 'string' ? (JSON.parse(init.body) as unknown) : null
      calls.push({ method, path: url.pathname, body })
      const p = url.pathname
      if (method === 'GET' && p === `/api/channels/${UC}`) return Promise.resolve(getDetail())
      if (method === 'POST' && p === `/api/channels/${UC}/unfollow`) return Promise.resolve(unfollow())
      if (method === 'DELETE' && p === `/api/channels/${UC}`) return Promise.resolve(remove())
      if (method === 'POST' && p === `/api/channels/${UC}/catalog/discover`)
        return Promise.resolve(json({ channel_id: UC, limit: 50, total_count: 0, items: [] }))
      if (method === 'GET' && p === `/api/channels/${UC}/catalog`)
        return Promise.resolve(json({ channel_id: UC, limit: 50, total_count: 0, items: [] }))
      if (method === 'GET' && p === '/api/videos')
        return Promise.resolve(json({ items: libraryRows, total: libraryRows.length, offset: 0, limit: 50 }))
      if (method === 'GET' && p === '/api/channels')
        return Promise.resolve(json({ items: [], total: 0, offset: 0, limit: 200 }))
      throw new Error(`unexpected request: ${method} ${p}`)
    }),
  )
})

async function mount(entry = PAGE) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const router = createMemoryRouter(routes, { initialEntries: [entry] })
  render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return { client, router }
}

const section = () => screen.getByRole('region', { name: 'Channel' })
const count = (method: string, suffix = '') =>
  calls.filter((c) => c.method === method && c.path === `/api/channels/${UC}${suffix}`).length

async function loaded() {
  await screen.findByRole('heading', { level: 2, name: 'Channel' })
}

describe('the channel section', () => {
  it('shows the title, the follow date in UTC and the video count above the range buttons', async () => {
    await mount()
    await loaded()
    expect(section().textContent).toContain('Example channel · Following since 2026-09-02 · 3 videos in the Library')
    const range = screen.getByRole('group', { name: 'Range' })
    expect(section().compareDocumentPosition(range) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Unfollow channel' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Delete channel and its videos' })).toBeTruthy()
  })

  it('uses the channel ID for a missing title and the singular for one video', async () => {
    current = detail({ title: '', video_count: 1 })
    await mount()
    await loaded()
    expect(section().textContent).toContain(`${UC} · Following since 2026-09-02 · 1 video in the Library`)
  })

  it('shows an unfollowed channel with only the delete button and a way to add it again', async () => {
    current = detail({ active: false })
    await mount()
    await loaded()
    expect(section().textContent).toContain('Not followed')
    expect(screen.queryByRole('button', { name: 'Unfollow channel' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Delete channel and its videos' })).toBeTruthy()
    expect(
      screen.getByText(/New uploads are not checked\. Add the channel again to follow it from now on\./),
    ).toBeTruthy()
    expect(section().querySelector('a')?.getAttribute('href')).toBe('/channels/add')
  })

  it('shows the not-found pattern for a 404', async () => {
    getDetail = () => json({ detail: 'x' }, 404)
    await mount()
    expect(await screen.findByRole('heading', { level: 1, name: 'Channel not found' })).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Back to Library' })).toBeTruthy()
  })

  it.each([
    [503, 'The database is unavailable. Try again shortly.'],
    [500, 'Could not load the channel.'],
  ])('shows an alert with Retry for a %i, and Retry loads it', async (status, text) => {
    getDetail = () => json({ detail: 'x' }, status)
    await mount()
    expect((await screen.findByRole('alert')).textContent).toContain(text)
    getDetail = () => json(current)
    await user().click(screen.getByRole('button', { name: 'Retry' }))
    await loaded()
  })
})

describe('unfollow', () => {
  it('opens an inline confirmation on Keep following, sends nothing yet, and Escape restores focus', async () => {
    const u = user()
    await mount()
    await loaded()
    await u.click(screen.getByRole('button', { name: 'Unfollow channel' }))
    expect(
      screen.getByText(
        'Stop following Example channel? Its 3 videos stay in the Library. New uploads are no longer checked, and an unfinished backfill is cancelled.',
      ),
    ).toBeTruthy()
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Keep following' }))
    expect(count('POST', '/unfollow')).toBe(0)
    await u.keyboard('{Escape}')
    expect(screen.queryByRole('button', { name: 'Confirm unfollow' })).toBeNull()
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Unfollow channel' }))
    expect(count('POST', '/unfollow')).toBe(0)
  })

  it('Keep following closes it and restores focus', async () => {
    const u = user()
    await mount()
    await loaded()
    await u.click(screen.getByRole('button', { name: 'Unfollow channel' }))
    await u.click(screen.getByRole('button', { name: 'Keep following' }))
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Unfollow channel' }))
  })

  it('sends one POST with {} on a double click, announces it and shows Not followed', async () => {
    let release!: () => void
    unfollow = async () => {
      await new Promise<void>((resolve) => {
        release = resolve
      })
      current = { ...current, active: false }
      return json({ channel_id: UC, active: false, changed: true, backfill: null })
    }
    const u = user()
    await mount()
    await loaded()
    await u.click(screen.getByRole('button', { name: 'Unfollow channel' }))
    const confirm = screen.getByRole('button', { name: 'Confirm unfollow' })
    await u.click(confirm)
    await u.click(confirm)
    await act(async () => {
      release()
    })
    await waitFor(() => expect(section().textContent).toContain('Not followed'))
    expect(calls.filter((c) => c.method === 'POST' && c.path.endsWith('/unfollow'))).toEqual([
      { method: 'POST', path: `/api/channels/${UC}/unfollow`, body: {} },
    ])
    expect(screen.queryByRole('button', { name: 'Confirm unfollow' })).toBeNull()
    const status = screen.getByText('Channel unfollowed')
    expect(status.getAttribute('aria-live')).toBe('polite')
    expect(document.activeElement).toBe(status)
    expect(screen.queryByRole('button', { name: 'Unfollow channel' })).toBeNull()
  })

  it('also announces a cancelled backfill', async () => {
    unfollow = () => {
      current = { ...current, active: false }
      return json({ channel_id: UC, active: false, changed: true, backfill: { backfill_id: 'b1', cancelled_jobs: 2 } })
    }
    const u = user()
    await mount()
    await loaded()
    await u.click(screen.getByRole('button', { name: 'Unfollow channel' }))
    await u.click(screen.getByRole('button', { name: 'Confirm unfollow' }))
    expect(await screen.findByText('Channel unfollowed · Backfill cancelled')).toBeTruthy()
  })

  it('shows the not-found state after a 404', async () => {
    unfollow = () => {
      getDetail = () => json({ detail: 'x' }, 404)
      return json({ detail: 'x' }, 404)
    }
    const u = user()
    await mount()
    await loaded()
    await u.click(screen.getByRole('button', { name: 'Unfollow channel' }))
    await u.click(screen.getByRole('button', { name: 'Confirm unfollow' }))
    expect(await screen.findByRole('heading', { level: 1, name: 'Channel not found' })).toBeTruthy()
  })

  it.each([
    [429, 'Too many requests. Try again shortly.'],
    [503, 'The database is unavailable. Try again shortly.'],
    [500, 'Could not unfollow the channel. Check your connection and try again.'],
  ])('keeps the confirmation open with a fixed alert for a %i', async (status, text) => {
    unfollow = () => json({ detail: 'SECRET' }, status)
    const u = user()
    await mount()
    await loaded()
    await u.click(screen.getByRole('button', { name: 'Unfollow channel' }))
    await u.click(screen.getByRole('button', { name: 'Confirm unfollow' }))
    expect((await screen.findByRole('alert')).textContent).toBe(text)
    expect(screen.getByRole('button', { name: 'Confirm unfollow' })).toBeTruthy()
    expect(document.body.textContent).not.toContain('SECRET')
  })

  it('keeps the confirmation open with a fixed alert on a network failure', async () => {
    unfollow = () => {
      throw new TypeError('Failed to fetch')
    }
    const u = user()
    await mount()
    await loaded()
    await u.click(screen.getByRole('button', { name: 'Unfollow channel' }))
    await u.click(screen.getByRole('button', { name: 'Confirm unfollow' }))
    expect((await screen.findByRole('alert')).textContent).toBe(
      'Could not unfollow the channel. Check your connection and try again.',
    )
  })
})

describe('delete', () => {
  it('opens the confirmation with the count and focuses Cancel; Escape restores focus', async () => {
    const u = user()
    await mount()
    await loaded()
    await u.click(screen.getByRole('button', { name: 'Delete channel and its videos' }))
    expect(
      screen.getByText(
        'Delete Example channel and all 3 of its videos, including videos you added by URL? Their transcripts, summaries and stored audio are permanently removed and any processing is stopped. Following the channel again will not bring these videos back; adding a video by its URL processes it again.',
      ),
    ).toBeTruthy()
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Cancel' }))
    await u.keyboard('{Escape}')
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Delete channel and its videos' }))
    expect(count('DELETE')).toBe(0)
  })

  it('uses the short sentence for a channel without videos', async () => {
    current = detail({ video_count: 0 })
    const u = user()
    await mount()
    await loaded()
    await u.click(screen.getByRole('button', { name: 'Delete channel and its videos' }))
    expect(screen.getByText('Delete Example channel? It has no videos in the Library.')).toBeTruthy()
  })

  it('sends one DELETE, leaves for the Library with the notice and makes no further channel request', async () => {
    const u = user()
    const { router } = await mount()
    await loaded()
    await u.click(screen.getByRole('button', { name: 'Delete channel and its videos' }))
    const confirm = screen.getByRole('button', { name: 'Confirm delete' })
    await u.click(confirm)
    await u.click(confirm)
    await waitFor(() => expect(router.state.location.pathname).toBe('/'))
    expect(router.state.location.search).toBe('')
    expect(count('DELETE')).toBe(1)
    expect(await screen.findByText('Channel deleted · 3 videos removed')).toBeTruthy()
    expect(screen.getByRole('heading', { level: 1, name: 'Library' })).toBeTruthy()

    // Nothing of the channel is requested after the delete went out, even once the lists refetch.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50))
    })
    const sent = calls.findIndex((c) => c.method === 'DELETE')
    expect(
      calls.slice(sent + 1).filter((c) => c.path === `/api/channels/${UC}` || c.path.includes(`/${UC}/catalog`)),
    ).toEqual([])
  })

  it('removes the cached queries of the channel and of its videos, and invalidates the lists', async () => {
    const u = user()
    const { client } = await mount()
    await loaded()
    const vidOf = (n: number) => `v${String(n).padStart(10, '0')}`
    client.setQueryData(['videos', { channel: UC }], { items: [{ video_id: vidOf(1), channel_id: UC }], total: 1 })
    client.setQueryData(['video', vidOf(1)], { video_id: vidOf(1), channel_id: UC })
    client.setQueryData(['video', vidOf(2)], { video_id: vidOf(2), channel_id: 'UC' + 'b'.repeat(22) })
    client.setQueryData(['transcript', vidOf(1)], { x: 1 })
    client.setQueryData(['search', 'q'], { x: 1 })
    await u.click(screen.getByRole('button', { name: 'Delete channel and its videos' }))
    await u.click(screen.getByRole('button', { name: 'Confirm delete' }))
    await waitFor(() => expect(client.getQueryData(['channel', UC])).toBeUndefined())
    expect(client.getQueryData(['transcript', vidOf(1)])).toBeUndefined()
    expect(client.getQueryData(['video', vidOf(1)])).toBeUndefined()
    expect(client.getQueryData(['video', vidOf(2)])).toBeDefined()
    expect(client.getQueryState(['search', 'q'])?.isInvalidated).toBe(true)
    expect(client.getQueryCache().find({ queryKey: ['channel-catalog', UC], exact: false })).toBeUndefined()
  })

  it('counts a 404 as already deleted', async () => {
    remove = () => json({ detail: 'x' }, 404)
    const u = user()
    const { router } = await mount()
    await loaded()
    await u.click(screen.getByRole('button', { name: 'Delete channel and its videos' }))
    await u.click(screen.getByRole('button', { name: 'Confirm delete' }))
    await waitFor(() => expect(router.state.location.pathname).toBe('/'))
    expect(await screen.findByText('Channel deleted')).toBeTruthy()
  })

  it.each([
    [429, 'Too many requests. Try again shortly.'],
    [503, 'The database is unavailable. Try again shortly.'],
    [500, 'Could not delete the channel. Check your connection and try again.'],
  ])('keeps the confirmation open with a fixed alert for a %i', async (status, text) => {
    remove = () => json({ detail: 'SECRET' }, status)
    const u = user()
    const { router } = await mount()
    await loaded()
    await u.click(screen.getByRole('button', { name: 'Delete channel and its videos' }))
    await u.click(screen.getByRole('button', { name: 'Confirm delete' }))
    expect((await screen.findByRole('alert')).textContent).toBe(text)
    expect(router.state.location.pathname).toBe(PAGE)
    expect(document.body.textContent).not.toContain('SECRET')
  })

  it('keeps the confirmation open on a network failure', async () => {
    remove = () => {
      throw new TypeError('Failed to fetch')
    }
    const u = user()
    await mount()
    await loaded()
    await u.click(screen.getByRole('button', { name: 'Delete channel and its videos' }))
    await u.click(screen.getByRole('button', { name: 'Confirm delete' }))
    expect((await screen.findByRole('alert')).textContent).toBe(
      'Could not delete the channel. Check your connection and try again.',
    )
  })
})

describe('the Library link', () => {
  it('links the active channel filter to its page, next to Clear', async () => {
    await mount(`/?channel=${UC}`)
    const group = await screen.findByRole('group', { name: 'Active filters' })
    expect(group.querySelector('a')?.getAttribute('href')).toBe(PAGE)
    expect(group.textContent).toContain('Manage channel')
    expect(group.querySelector('button')?.textContent).toBe('Clear')
  })

  it('has no link without a channel filter', async () => {
    await mount('/')
    await screen.findByRole('heading', { level: 1, name: 'Library' })
    expect(screen.queryByRole('link', { name: 'Manage channel' })).toBeNull()
  })

  it.each(['Channel deleted', 'Channel deleted · 1 video removed', 'Video deleted'])(
    'shows the one-shot notice %s',
    async (notice) => {
      const router = createMemoryRouter(routes, { initialEntries: [{ pathname: '/', state: { notice } }] })
      render(
        <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
          <RouterProvider router={router} />
        </QueryClientProvider>,
      )
      expect(await screen.findByText(notice)).toBeTruthy()
    },
  )

  it('ignores an arbitrary notice', async () => {
    const router = createMemoryRouter(routes, {
      initialEntries: [{ pathname: '/', state: { notice: 'Channel deleted <b>x</b>' } }],
    })
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )
    await screen.findByRole('heading', { level: 1, name: 'Library' })
    expect(screen.queryByText(/Channel deleted/)).toBeNull()
  })
})
