import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, within } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { BulkCatalogResponse, CatalogItem, CatalogLimit, CatalogPage } from '../lib/channelCatalog'
import { setupUser } from '../test/user'
import { routes } from './index'

const UC = 'UC' + 'a'.repeat(22)
const CATALOG_PATH = `/channels/${UC}/catalog`
const MARKER = 'ZZMARKER42'

const vid = (n: number) => `v${String(n).padStart(10, '0')}`

function item(n: number, over: Partial<CatalogItem> = {}): CatalogItem {
  return {
    video_id: vid(n),
    title: `Title ${n}`,
    url: `https://www.youtube.com/watch?v=${vid(n)}`,
    description: null,
    published_at: null,
    enrichment_state: 'pending',
    enrichment_error: null,
    known: false,
    processing_status: null,
    ...over,
  }
}

function done(n: number, over: Partial<CatalogItem> = {}): CatalogItem {
  return item(n, {
    enrichment_state: 'done',
    description: `Description ${n}`,
    published_at: '2026-09-01T02:00:00Z',
    ...over,
  })
}

function page(items: CatalogItem[], limit: CatalogLimit = 50): CatalogPage {
  return { channel_id: UC, limit, total_count: 321, items }
}

interface Call {
  url: URL
  method: string
  body: Record<string, unknown> | null
  contentType: string | null
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

/** A fake of the catalog API: no network, every request recorded. */
class FakeApi {
  calls: Call[] = []
  all: CatalogItem[] = [1, 2, 3, 4, 5, 6].map((n) => item(n))
  served: CatalogPage = page([])
  register: () => Response | Promise<Response> = () => json({ channel_id: UC, created: true }, 201)
  discover: (limit: CatalogLimit) => Response | Promise<Response> = (limit) => {
    this.served = page(this.all.slice(0, limit === 50 ? 3 : this.all.length), limit)
    return json(this.served)
  }
  retry: (videoId: string) => Response | Promise<Response> = (videoId) =>
    json({ channel_id: UC, video_id: videoId, enrichment_state: 'pending' }, 202)
  process: (ids: string[]) => Response | Promise<Response> = (ids) =>
    json(
      {
        channel_id: UC,
        results: ids.map((id) => ({ video_id: id, outcome: 'queued', job_id: 1, reason: null })),
      } satisfies BulkCatalogResponse,
      202,
    )

  handle(url: URL, method: string, body: Record<string, unknown> | null): Response | Promise<Response> {
    const path = url.pathname
    if (method === 'POST' && path === '/api/channels') return this.register()
    if (method === 'POST' && path === `/api/channels/${UC}/catalog/discover`)
      return this.discover(body?.limit as CatalogLimit)
    if (method === 'GET' && path === `/api/channels/${UC}/catalog`) return json(this.served)
    const retry = new RegExp(`^/api/channels/${UC}/catalog/([^/]+)/metadata/retry$`).exec(path)
    if (method === 'POST' && retry !== null) return this.retry(retry[1] as string)
    if (method === 'POST' && path === `/api/channels/${UC}/catalog/process`)
      return this.process(body?.video_ids as string[])
    throw new Error(`unexpected request: ${method} ${path}`)
  }

  count(method: string, suffix: string): Call[] {
    return this.calls.filter((c) => c.method === method && c.url.pathname.endsWith(suffix))
  }
  get gets(): Call[] {
    return this.calls.filter((c) => c.method === 'GET')
  }
}

function install(api: FakeApi) {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init: RequestInit = {}) => {
      const url = new URL(String(input), 'http://localhost')
      const method = (init.method ?? 'GET').toUpperCase()
      const body = typeof init.body === 'string' ? (JSON.parse(init.body) as Record<string, unknown>) : null
      const contentType = new Headers(init.headers).get('content-type')
      api.calls.push({ url, method, body, contentType })
      // The API answers 415 to a body that is not declared JSON (routes_write).
      if (body !== null && contentType?.split(';')[0]?.trim().toLowerCase() !== 'application/json')
        return Promise.resolve(json({ detail: 'Content-Type must be application/json' }, 415))
      return Promise.resolve(api.handle(url, method, body))
    }),
  )
}

// TanStack Query hands its updates to React in a zero-delay timer, which fake
// timers run 1 ms later when scheduled from inside a tick, so each round
// advances 1 ms rather than 0 (the rounds also let the fetch promises settle).
async function settle() {
  for (let i = 0; i < 4; i++) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1)
    })
  }
}

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
  await settle()
}

async function mount(entry: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const router = createMemoryRouter(routes, { initialEntries: [entry] })
  render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  await settle()
  return router
}

async function mountCatalog(api = new FakeApi()) {
  install(api)
  const router = await mount(CATALOG_PATH)
  return { api, router }
}

const user = () => setupUser()
const checkbox = (n: number) => screen.getByRole('checkbox', { name: `Select Title ${n}` }) as HTMLInputElement
const rowOf = (n: number) => screen.getByRole('link', { name: `Title ${n}` }).closest('tr') as HTMLElement
const button = (name: string | RegExp) => screen.getByRole('button', { name })

beforeEach(() => {
  vi.useFakeTimers()
})

describe('registering a channel', () => {
  async function submit(value: string) {
    const u = user()
    await u.type(screen.getByLabelText('YouTube channel'), value)
    await u.click(button('Add channel'))
    await settle()
  }

  it('registers a supported reference, then opens its default catalog', async () => {
    const api = new FakeApi()
    install(api)
    const router = await mount('/channels/add')
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Add channel')

    await submit('  @example  ')

    expect(api.count('POST', '/channels')[0]?.body).toEqual({ url: '@example' })
    expect(router.state.location.pathname).toBe(CATALOG_PATH)
    expect(api.count('POST', '/catalog/discover')[0]?.body).toEqual({ limit: 50 })
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Channel catalog')
    expect(screen.getAllByRole('link', { name: /^Title \d$/ })).toHaveLength(3)
  })

  it('offers Add channel in the main navigation', async () => {
    install(new FakeApi())
    const router = await mount('/')
    void router
    const nav = screen.getByRole('navigation', { name: 'Main' })
    await user().click(within(nav).getByRole('link', { name: 'Add channel' }))
    await settle()
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Add channel')
  })

  it('has a visible label and no placeholder-only input', async () => {
    install(new FakeApi())
    await mount('/channels/add')
    const input = screen.getByLabelText('YouTube channel')
    expect(input.getAttribute('placeholder')).toBeNull()
  })

  it.each([
    [404, 'That channel was not found.'],
    [422, 'Enter a YouTube channel ID, a youtube.com/channel/ URL, an @handle, or a /c/ or /user/ URL.'],
    [429, 'Too many requests. Wait a moment and try again.'],
    [502, 'The channel lookup failed. Try again later.'],
    [503, 'The channel lookup is unavailable. Try again shortly.'],
    [500, 'Could not add the channel. Try again.'],
  ])('shows fixed text for a %s and echoes neither input nor server text', async (status, message) => {
    const api = new FakeApi()
    api.register = () => json({ detail: `server says ${MARKER}` }, status)
    install(api)
    const router = await mount('/channels/add')

    await submit(`@${MARKER}`)

    const alert = screen.getByRole('alert')
    expect(alert.textContent).toBe(message)
    expect(document.body.textContent).not.toContain(MARKER)
    expect(router.state.location.pathname).toBe('/channels/add')
    expect(screen.getByLabelText('YouTube channel').getAttribute('aria-invalid')).toBe('true')
  })

  it('shows a network error as fixed text', async () => {
    const api = new FakeApi()
    api.register = () => {
      throw new TypeError('Failed to fetch https://secret.example')
    }
    install(api)
    await mount('/channels/add')

    await submit('@example')

    expect(screen.getByRole('alert').textContent).toBe(
      'Could not reach the server. Check your connection and try again.',
    )
    expect(document.body.textContent).not.toContain('secret.example')
  })

  it('refuses a response whose channel id is not canonical', async () => {
    const api = new FakeApi()
    api.register = () => json({ channel_id: '../../x' }, 201)
    install(api)
    const router = await mount('/channels/add')

    await submit('@example')

    expect(screen.getByRole('alert').textContent).toBe('Could not add the channel. Try again.')
    expect(router.state.location.pathname).toBe('/channels/add')
  })

  it('submits once however many times Enter is pressed while it is pending', async () => {
    const api = new FakeApi()
    let release: (r: Response) => void = () => {}
    api.register = () => new Promise<Response>((resolve) => (release = resolve))
    install(api)
    await mount('/channels/add')
    const u = user()
    await u.type(screen.getByLabelText('YouTube channel'), '@example')

    await u.keyboard('{Enter}{Enter}')
    await settle()
    expect(api.count('POST', '/channels')).toHaveLength(1)
    expect((button('Adding…') as HTMLButtonElement).disabled).toBe(true)

    release(json({ channel_id: UC }, 201))
    await settle()
    expect(api.count('POST', '/channels')).toHaveLength(1)
  })
})

describe('the catalog', () => {
  it('lists the newest 50 by default, in the server order, with canonical links', async () => {
    const { api } = await mountCatalog()

    expect(api.count('POST', '/catalog/discover')[0]?.body).toEqual({ limit: 50 })
    const links = screen.getAllByRole('link', { name: /^Title \d$/ })
    expect(links.map((a) => a.textContent)).toEqual(['Title 1', 'Title 2', 'Title 3'])
    const first = links[0] as HTMLAnchorElement
    expect(first.getAttribute('href')).toBe(`https://www.youtube.com/watch?v=${vid(1)}`)
    expect(first.getAttribute('target')).toBe('_blank')
    expect(first.getAttribute('rel')).toBe('noopener noreferrer')
    expect(screen.getByText(/Showing 3 of 321 uploads/)).toBeTruthy()
    for (const label of ['Show last 50', 'Show last 200', 'Show last 500', 'Show all']) {
      expect(button(label)).toBeTruthy()
    }
    expect(button('Show last 50').getAttribute('aria-pressed')).toBe('true')
    expect(button('Show all').getAttribute('aria-pressed')).toBe('false')
  })

  it('derives the link from the id, never from the server url', async () => {
    const api = new FakeApi()
    api.all = [item(1, { url: 'https://evil.example/x' })]
    await mountCatalog(api)
    expect(screen.getByRole('link', { name: 'Title 1' }).getAttribute('href')).toBe(
      `https://www.youtube.com/watch?v=${vid(1)}`,
    )
  })

  it.each([
    ['Show last 200', 200],
    ['Show last 500', 500],
    ['Show all', 5000],
    ['Show last 50', 50],
  ] as const)('%s asks for exactly %i and keeps the order', async (label, limit) => {
    const { api } = await mountCatalog()

    await user().click(button(label === 'Show last 50' ? 'Show last 200' : label))
    await settle()
    if (label === 'Show last 50') {
      await user().click(button(label))
      await settle()
    }

    expect(api.count('POST', '/catalog/discover').at(-1)?.body).toEqual({ limit })
    expect(button(label).getAttribute('aria-pressed')).toBe('true')
    const titles = screen.getAllByRole('link', { name: /^Title \d$/ }).map((a) => a.textContent)
    expect(titles).toEqual(api.served.items.map((i) => i.title))
  })

  it('fills description and date row by row as enrichment progresses, then stops polling', async () => {
    const api = new FakeApi()
    await mountCatalog(api)
    expect(screen.getAllByText('Loading metadata…')).toHaveLength(3)
    const getsBefore = api.gets.length

    api.served = page([done(1), item(2, { enrichment_state: 'running' }), item(3)])
    await advance(5000)

    expect(api.gets.length).toBe(getsBefore + 1)
    expect(within(rowOf(1)).getByText('Description 1')).toBeTruthy()
    expect(within(rowOf(1)).getByText('2026-09-01')).toBeTruthy()
    expect(within(rowOf(2)).getByText('Loading metadata…')).toBeTruthy()

    api.served = page([done(1), done(2), done(3)])
    await advance(5000)
    expect(screen.queryByText('Loading metadata…')).toBeNull()

    const settled = api.gets.length
    await advance(20000)
    expect(api.gets.length).toBe(settled)
  })

  it('shows the date in UTC even when the suite time zone is not UTC', async () => {
    const api = new FakeApi()
    api.all = [done(1, { published_at: '2026-09-01T02:00:00Z' })]
    await mountCatalog(api)

    expect(within(rowOf(1)).getByText('2026-09-01')).toBeTruthy()
    expect(within(rowOf(1)).getByText('2026-09-01').tagName).toBe('TIME')
  })

  it('isolates a failed row, offers a per-row retry and returns it to loading', async () => {
    const api = new FakeApi()
    api.all = [done(1), item(2, { enrichment_state: 'failed', enrichment_error: 'ToolFailureError' }), item(3)]
    await mountCatalog(api)

    expect(within(rowOf(2)).getByText('Metadata unavailable')).toBeTruthy()
    expect(within(rowOf(1)).getByText('Description 1')).toBeTruthy()
    expect(within(rowOf(3)).getByText('Loading metadata…')).toBeTruthy()
    expect(rowOf(2).className).toContain('catalog-row--failed')
    expect(document.body.textContent).not.toContain('ToolFailureError')

    api.served = page([done(1), item(2), item(3)])
    await user().click(button('Retry metadata for Title 2'))
    await settle()

    expect(api.count('POST', '/metadata/retry')).toHaveLength(1)
    expect(api.count('POST', '/metadata/retry')[0]?.url.pathname).toBe(
      `/api/channels/${UC}/catalog/${vid(2)}/metadata/retry`,
    )
    expect(within(rowOf(2)).getByText('Loading metadata…')).toBeTruthy()
    expect(screen.queryByText('Metadata unavailable')).toBeNull()
  })

  it('reports a failed metadata retry as fixed text', async () => {
    const api = new FakeApi()
    api.all = [item(1, { enrichment_state: 'failed', enrichment_error: 'x' })]
    api.retry = () => json({ detail: MARKER }, 409)
    await mountCatalog(api)

    await user().click(button('Retry metadata for Title 1'))
    await settle()

    expect(screen.getByRole('alert').textContent).toBe('That metadata lookup is already in progress.')
    expect(document.body.textContent).not.toContain(MARKER)
  })

  it('shows known videos with their status and keeps them out of reach', async () => {
    const api = new FakeApi()
    api.all = [
      done(1, { known: true, processing_status: 'done' }),
      done(2, { known: true, processing_status: null }),
      done(3),
    ]
    await mountCatalog(api)

    expect(checkbox(1).disabled).toBe(true)
    expect(checkbox(2).disabled).toBe(true)
    expect(checkbox(3).disabled).toBe(false)
    expect(within(rowOf(1)).getByText('Already known · Analysed')).toBeTruthy()
    expect(within(rowOf(2)).getByText('Already known')).toBeTruthy()
    expect(rowOf(1).className).toContain('catalog-row--known')
    expect(within(rowOf(3)).getByText('New')).toBeTruthy()
    expect(document.body.textContent).not.toContain('null')
  })

  it('alternates row surfaces by position and keeps them when the range changes', async () => {
    const { api } = await mountCatalog()
    const classes = () =>
      screen.getAllByRole('link', { name: /^Title \d$/ }).map((a) => (a.closest('tr') as HTMLElement).className)

    expect(classes().map((c) => (c.includes('catalog-row--even') ? 'even' : 'odd'))).toEqual(['even', 'odd', 'even'])

    await user().click(button('Show last 200'))
    await settle()

    expect(api.served.items).toHaveLength(6)
    expect(classes().map((c) => (c.includes('catalog-row--even') ? 'even' : 'odd'))).toEqual([
      'even',
      'odd',
      'even',
      'odd',
      'even',
      'odd',
    ])
  })

  it('truncates a long description behind an accessible expansion control', async () => {
    const api = new FakeApi()
    const long = 'word '.repeat(100).trim()
    api.all = [done(1, { description: long })]
    await mountCatalog(api)

    const toggle = within(rowOf(1)).getByRole('button', { name: 'Show more' })
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    expect(within(rowOf(1)).queryByText(long)).toBeNull()

    await user().click(toggle)

    const less = within(rowOf(1)).getByRole('button', { name: 'Show less' })
    expect(less.getAttribute('aria-expanded')).toBe('true')
    expect(within(rowOf(1)).getByText(long)).toBeTruthy()
  })

  it('renders hostile metadata as literal text', async () => {
    const hostile = '<img src=x onerror=alert(1)>'
    const api = new FakeApi()
    api.all = [done(1, { title: hostile, description: hostile })]
    await mountCatalog(api)

    expect(screen.getAllByText(hostile)).toHaveLength(2)
    expect(document.querySelector('img')).toBeNull()
  })

  it('shows a page-level retryable error and keeps the channel when discovery fails', async () => {
    const api = new FakeApi()
    const ok = api.discover
    api.discover = () => json({ detail: `boom ${MARKER}` }, 503)
    await mountCatalog(api)

    expect(screen.getByRole('alert').textContent).toContain('The YouTube listing is unavailable. Try again shortly.')
    expect(document.body.textContent).not.toContain(MARKER)
    expect(screen.getByText(UC)).toBeTruthy()
    expect(screen.queryByRole('table')).toBeNull()

    api.discover = ok
    await user().click(button('Retry'))
    await settle()

    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.getAllByRole('link', { name: /^Title \d$/ })).toHaveLength(3)
    expect(api.count('POST', '/catalog/discover').map((c) => c.body)).toEqual([{ limit: 50 }, { limit: 50 }])
  })

  it.each([
    [404, 'This channel is not registered.'],
    [502, 'The YouTube listing failed. Try again later.'],
  ])('maps a %s to fixed text', async (status, message) => {
    const api = new FakeApi()
    api.discover = () => json({}, status)
    await mountCatalog(api)

    expect(screen.getByRole('alert').textContent).toContain(message)
  })

  it('keeps the loaded range when a range change fails', async () => {
    const api = new FakeApi()
    await mountCatalog(api)
    api.discover = () => json({}, 503)

    await user().click(button('Show all'))
    await settle()

    expect(screen.getByRole('alert').textContent).toContain('unavailable')
    expect(button('Show last 50').getAttribute('aria-pressed')).toBe('true')
    expect(screen.getAllByRole('link', { name: /^Title \d$/ })).toHaveLength(3)
  })

  it('says so when the channel has no listable uploads', async () => {
    const api = new FakeApi()
    api.discover = () => json({ channel_id: UC, limit: 50, total_count: null, items: [] })
    await mountCatalog(api)

    expect(screen.getByText('No uploads were listed for this channel.')).toBeTruthy()
  })

  it('does not call the network for a malformed channel id', async () => {
    const api = new FakeApi()
    install(api)

    await mount('/channels/not-a-channel/catalog')

    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Channel not found')
    expect(api.calls).toEqual([])
  })
})

describe('selecting and processing', () => {
  async function mountWithSix() {
    const api = new FakeApi()
    api.all = [1, 2, 3, 4, 5, 6].map((n) => done(n))
    api.discover = (limit) => {
      api.served = page(api.all.slice(0, limit === 50 ? 4 : 6), limit)
      return json(api.served)
    }
    return mountCatalog(api)
  }

  it('offers an accessible checkbox per new row and counts the selection', async () => {
    await mountWithSix()
    const u = user()

    expect(button('Process selected videos')).toHaveProperty('disabled', true)
    await u.click(checkbox(2))

    expect(checkbox(2).checked).toBe(true)
    expect(rowOf(2).className).toContain('catalog-row--selected')
    expect(within(rowOf(2)).getByText('New · Selected')).toBeTruthy()
    expect(screen.getByText('1 selected')).toBeTruthy()
    expect(button('Process 1 selected video')).toBeTruthy()
    await u.click(checkbox(3))
    expect(button('Process 2 selected videos')).toBeTruthy()
  })

  it('selects all new rows and clears the selection', async () => {
    const api = new FakeApi()
    api.all = [done(1, { known: true, processing_status: 'done' }), done(2), done(3)]
    await mountCatalog(api)
    const u = user()

    await u.click(button('Select all new'))
    expect([checkbox(1).checked, checkbox(2).checked, checkbox(3).checked]).toEqual([false, true, true])

    await u.click(button('Clear selection'))
    expect([checkbox(2).checked, checkbox(3).checked]).toEqual([false, false])
  })

  it('retains only selected rows still present after changing range', async () => {
    const api = new FakeApi()
    api.all = [1, 2, 3, 4, 5, 6].map((n) => done(n))
    api.discover = (limit) => {
      // 200 lists 1..4; the next 50 lists 2..5 (newer uploads dropped 1, added 5)
      api.served = page(limit === 200 ? api.all.slice(0, 4) : api.all.slice(1, 5), limit)
      return json(api.served)
    }
    install(api)
    await mount(CATALOG_PATH) // loads the default 50: videos 2..5
    const u = user()
    await u.click(button('Show last 200'))
    await settle()
    await u.click(checkbox(1))
    await u.click(checkbox(2))
    expect([checkbox(1).checked, checkbox(2).checked]).toEqual([true, true])

    await u.click(button('Show last 50'))
    await settle()

    expect(screen.queryByRole('checkbox', { name: 'Select Title 1' })).toBeNull()
    expect(checkbox(2).checked).toBe(true)
    expect(checkbox(5).checked).toBe(false)
    expect(button('Process 1 selected video')).toBeTruthy()
  })

  it('requires one confirmation, then reports every result without collapsing partial outcomes', async () => {
    const { api } = await mountWithSix()
    const u = user()
    // click order differs from catalog order; the request follows the catalog
    await u.click(checkbox(3))
    await u.click(checkbox(1))
    await u.click(checkbox(2))
    api.process = (ids) =>
      json(
        {
          channel_id: UC,
          results: [
            { video_id: ids[0], outcome: 'queued', job_id: 7, reason: null },
            { video_id: ids[1], outcome: 'already_known', job_id: null, reason: null },
            { video_id: ids[2], outcome: 'rejected', job_id: null, reason: 'not_in_catalog' },
          ],
        },
        202,
      )

    await u.click(button('Process 3 selected videos'))
    expect(api.count('POST', '/catalog/process')).toHaveLength(0)
    expect(
      screen.getByText(
        'Process 3 selected videos from this channel? Processing may use existing subtitles or download audio and transcribe when needed.',
      ),
    ).toBeTruthy()
    expect(document.activeElement).toBe(button('Confirm processing'))

    await u.click(button('Confirm processing'))
    await settle()

    expect(api.count('POST', '/catalog/process')).toHaveLength(1)
    expect(api.count('POST', '/catalog/process')[0]?.body).toEqual({ video_ids: [vid(1), vid(2), vid(3)] })
    expect(screen.getByText(`Queued: ${vid(1)}`)).toBeTruthy()
    expect(screen.getByText(`Already known: ${vid(2)}`)).toBeTruthy()
    expect(screen.getByText(`Rejected: ${vid(3)} — not in this channel catalog`)).toBeTruthy()
    expect(screen.getByText('Queued 1 · Already known 1 · Rejected 1')).toBeTruthy()
    expect(screen.getByText('No videos selected')).toBeTruthy()
    expect(screen.queryByText(/Confirm processing/)).toBeNull()
  })

  it('cannot be submitted twice, and locks the controls while it runs', async () => {
    const { api } = await mountWithSix()
    const u = user()
    await u.click(checkbox(1))
    let release: (r: Response) => void = () => {}
    api.process = () => new Promise<Response>((resolve) => (release = resolve))
    await u.click(button('Process 1 selected video'))

    await u.click(button('Confirm processing'))
    await u.click(button('Confirm processing'))
    await settle()

    expect(api.count('POST', '/catalog/process')).toHaveLength(1)
    expect((button('Confirm processing') as HTMLButtonElement).disabled).toBe(true)
    expect((button('Cancel') as HTMLButtonElement).disabled).toBe(true)
    expect(checkbox(2).disabled).toBe(true)
    expect((button('Show all') as HTMLButtonElement).disabled).toBe(true)

    release(json({ channel_id: UC, results: [{ video_id: vid(1), outcome: 'queued', job_id: 1, reason: null }] }, 202))
    await settle()
    expect(checkbox(2).disabled).toBe(false)
    expect(api.count('POST', '/catalog/process')).toHaveLength(1)
  })

  it('cancelling sends nothing and returns focus to the bulk button', async () => {
    const { api } = await mountWithSix()
    const u = user()
    await u.click(checkbox(1))
    await u.click(button('Process 1 selected video'))

    await u.click(button('Cancel'))
    await settle()

    expect(api.count('POST', '/catalog/process')).toHaveLength(0)
    expect(document.activeElement).toBe(button('Process 1 selected video'))
    expect(screen.queryByRole('button', { name: 'Confirm processing' })).toBeNull()
    expect(checkbox(1).checked).toBe(true)
  })

  it.each([
    ['a server error', () => json({ detail: `db ${MARKER}` }, 500), 'Could not queue the selected videos. Your selection is kept; try again.'],
    ['an unavailable database', () => json({}, 503), 'The database is unavailable. Your selection is kept; try again shortly.'],
    [
      'a network failure',
      () => {
        throw new TypeError('Failed to fetch')
      },
      'Could not reach the server. Your selection is kept; try again.',
    ],
  ])('keeps the selection and shows fixed text after %s', async (_name, reply, message) => {
    const { api } = await mountWithSix()
    const u = user()
    await u.click(checkbox(1))
    api.process = reply
    await u.click(button('Process 1 selected video'))

    await u.click(button('Confirm processing'))
    await settle()

    expect(screen.getByRole('alert').textContent).toBe(message)
    expect(document.body.textContent).not.toContain(MARKER)
    expect(checkbox(1).checked).toBe(true)
    expect(button('Process 1 selected video')).toBeTruthy()
  })

  it('refetches the catalog after processing so the new status shows', async () => {
    const { api } = await mountWithSix()
    const u = user()
    await u.click(checkbox(1))
    await u.click(button('Process 1 selected video'))
    api.served = page([done(1, { known: true, processing_status: 'processing' }), ...api.served.items.slice(1)])

    await u.click(button('Confirm processing'))
    await settle()

    expect(within(rowOf(1)).getByText('Already known · Processing')).toBeTruthy()
    expect(checkbox(1).disabled).toBe(true)
  })
})
