import { focusManager, QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { setupUser } from '../test/user'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { components } from '../api'
import { PROGRESS_POLL_MS } from '../lib/progress'
import { Ops } from './Ops'

const user = setupUser

type JobOut = components['schemas']['JobOut']
type JobListOut = components['schemas']['JobListOut']
type HealthOut = components['schemas']['HealthOut']
type OpsChannelListOut = components['schemas']['OpsChannelListOut']

function job(id: number, over: Partial<JobOut> = {}): JobOut {
  return {
    id,
    video_id: `v${String(id).padStart(10, '0')}`,
    video_title: `Title ${id}`,
    kind: 'ingest',
    dedupe_key: 'k',
    state: 'dead',
    priority: 0,
    attempts: 3,
    error_class: 'TOOL_FAILURE',
    last_error: `error ${id}`,
    run_after: '2026-10-01T12:00:00Z',
    locked_by: null,
    heartbeat_at: null,
    finished_at: '2026-10-01T23:30:00-07:00',
    created_at: '2026-09-30T08:05:00Z',
    progress: null,
    ...over,
  }
}

function list(items: JobOut[], next: number | null = null): JobListOut {
  return { items, next_before_id: next }
}

function health(queue: HealthOut['queue'] = {}): HealthOut {
  return { status: 'ok', queue }
}

function channels(items: OpsChannelListOut['items'] = []): OpsChannelListOut {
  return { items }
}

interface Call {
  url: URL
  method: string
  headers: Headers
  body: string | null
}
let calls: Call[]
type Reply = unknown | Response | Promise<Response>
type Handler = (c: Call) => Reply

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

function stubFetch(h: Handler) {
  calls = []
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const c: Call = {
        url: new URL(String(input), 'http://localhost'),
        method: init?.method ?? 'GET',
        headers: new Headers(init?.headers),
        body: typeof init?.body === 'string' ? init.body : null,
      }
      calls.push(c)
      const r = h(c)
      if (r instanceof Promise) return r
      return Promise.resolve(r instanceof Response ? r : json(r))
    }),
  )
}

/** Routes by path: healthz, ops/jobs (GET) and retry (POST). */
function api(opts: { health?: Handler; channels?: Handler; jobs?: Handler; retry?: Handler; bulk?: Handler; cancel?: Handler } = {}): Handler {
  return (c) => {
    if (c.url.pathname === '/api/healthz') return (opts.health ?? (() => health()))(c)
    if (c.url.pathname === '/api/ops/channels') return (opts.channels ?? (() => channels()))(c)
    if (c.url.pathname === '/api/ops/jobs') return (opts.jobs ?? (() => list([])))(c)
    if (c.url.pathname === '/api/ops/jobs/retry') {
      return (opts.bulk ?? (() => json({ detail: 'unexpected' }, 500)))(c)
    }
    if (/^\/api\/videos\/[^/]+\/cancel$/.test(c.url.pathname)) {
      return (opts.cancel ?? (() => json({ detail: 'video not found' }, 404)))(c)
    }
    if (/^\/api\/ops\/jobs\/\d+\/retry$/.test(c.url.pathname)) {
      return (opts.retry ?? (() => json({ detail: 'job not found' }, 404)))(c)
    }
    throw new Error(`unexpected request ${c.method} ${c.url.pathname}`)
  }
}

const jobGets = () => calls.filter((c) => c.url.pathname === '/api/ops/jobs')
const channelGets = () => calls.filter((c) => c.url.pathname === '/api/ops/channels')
const healthGets = () => calls.filter((c) => c.url.pathname === '/api/healthz')
const posts = () => calls.filter((c) => c.method === 'POST')
const lastJobGet = () => jobGets()[jobGets().length - 1] as Call

function deferred() {
  let resolve!: (r: Response) => void
  const promise = new Promise<Response>((res) => (resolve = res))
  return { promise, resolve }
}

async function settle() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
}

async function mount(entry = '/ops', defaults: { mutations?: { retry: number } } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, ...defaults } })
  const router = createMemoryRouter(
    [
      { path: '/ops', element: <Ops /> },
      { path: '/videos/:videoId', element: <p>detail page</p> },
    ],
    { initialEntries: [entry] },
  )
  const view = render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  await settle()
  return { router, view }
}

const depth = () => screen.getByRole('region', { name: 'Queue depth' })
const jobsRegion = () => screen.getByRole('region', { name: 'Jobs' })
const jobsTable = () => within(jobsRegion()).getByRole('table')
function rowFor(id: number): HTMLTableRowElement {
  const rows = within(jobsTable()).getAllByRole('row') as HTMLTableRowElement[]
  const row = rows.find((r) => r.cells[0]?.textContent === String(id))
  if (!row) throw new Error(`no row for job ${id}`)
  return row
}
const bodyRows = () => within(jobsTable()).getAllByRole('row').slice(1)
const click = async (el: HTMLElement) => {
  await user().click(el)
  await settle()
}
const select = (name: RegExp | string) => screen.getByRole('combobox', { name }) as HTMLSelectElement
const livePolite = () => Array.from(document.querySelectorAll('[aria-live="polite"]'))
const liveText = () => livePolite().map((e) => e.textContent).join('|')

beforeEach(() => {
  vi.useFakeTimers()
})

describe('channels needing attention', () => {
  it('requests attention and renders both conditions with exact gap guidance', async () => {
    stubFetch(
      api({
        channels: () =>
          channels([
            {
              channel_id: 'UCabc',
              title: 'Channel title',
              poll_failing_since: '2026-10-02T00:00:00Z',
              poll_failures: 3,
              last_poll_err: 'failed',
              feed_gap_detected_at: '2026-10-01T00:00:00Z',
              feed_gap_reason: 'no_anchor',
            },
          ]),
      }),
    )
    await mount()
    const request = calls.find((c) => c.url.pathname === '/api/ops/channels') as Call
    expect(request.url.searchParams.get('attention')).toBe('true')
    const region = screen.getByRole('region', { name: 'Channels needing attention' })
    expect(within(region).getByText('RSS polling failure (3)')).toBeTruthy()
    expect(within(region).getByText('Feed gap could not be recovered: no safe anchor')).toBeTruthy()
    expect(within(region).getByText('Run a channel backfill to recover older uploads')).toBeTruthy()
    expect(within(region).getByText('POST /api/channels/UCabc/backfill')).toBeTruthy()
    expect(within(region).getByText('2026-10-01 00:00 UTC')).toBeTruthy()
  })

  it('shows loading, empty, malformed, generic error and database unavailable states with Retry', async () => {
    const pending = deferred()
    stubFetch(api({ channels: () => pending.promise }))
    const first = await mount()
    expect(screen.getByRole('status').textContent).toBe('Loading channels…')
    pending.resolve(json(channels()))
    await settle()
    expect(screen.getByText('No channels need attention')).toBeTruthy()
    first.view.unmount()

    const replies = [json({ items: 'bad' }), json({}, 500), json({}, 503), json(channels())]
    stubFetch(api({ channels: () => replies.shift() as Response }))
    await mount()
    expect(screen.getByRole('alert').textContent).toContain('Could not load channels')
    await click(screen.getByRole('button', { name: 'Retry' }))
    expect(screen.getByRole('alert').textContent).toContain('Could not load channels')
    await click(screen.getByRole('button', { name: 'Retry' }))
    expect(screen.getByRole('alert').textContent).toContain('The database is unavailable. Try again shortly.')
    await click(screen.getByRole('button', { name: 'Retry' }))
    expect(screen.getByText('No channels need attention')).toBeTruthy()
  })

  it('renders hostile channel text as text and coexists with job pagination', async () => {
    const hostile = '<img src=x onerror=alert(1)>'
    stubFetch(api({ channels: () => channels([{ channel_id: 'UCx', title: hostile, poll_failing_since: null, poll_failures: 0, last_poll_err: null, feed_gap_detected_at: '2026-10-01T00:00:00Z', feed_gap_reason: 'suspected' }]), jobs: () => list([job(2)], 2) }))
    await mount()
    expect(screen.getByText(hostile)).toBeTruthy()
    expect(document.querySelector('img')).toBeNull()
    const pagination = screen.getByRole('navigation', { name: 'Pagination' })
    expect(within(pagination).getByRole('button', { name: 'Older' }).hasAttribute('disabled')).toBe(false)
  })

  it.each([
    ['suspected', 'Possible feed gap awaiting reconciliation'],
    ['listing_window_exhausted', 'Feed gap may continue beyond the reconciliation window'],
  ] as const)('renders %s copy', async (reason, copy) => {
    stubFetch(api({ channels: () => channels([{ channel_id: 'UCx', title: null, poll_failing_since: null, poll_failures: 0, last_poll_err: null, feed_gap_detected_at: '2026-10-01T00:00:00Z', feed_gap_reason: reason }]) }))
    await mount()
    expect(screen.getByText(copy)).toBeTruthy()
  })
})

describe('route and requests', () => {
  it('renders the Ops heading and requests dead jobs, limit 50, plus the queue depth', async () => {
    stubFetch(api())
    await mount()
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Ops')
    expect(jobGets()).toHaveLength(1)
    expect(Object.fromEntries(lastJobGet().url.searchParams)).toEqual({ state: 'dead', limit: '50' })
    expect(healthGets()).toHaveLength(1)
    expect(calls.every((c) => c.method === 'GET')).toBe(true)
  })

  it('a deep link restores the filters and the cursor, and sends them as query params', async () => {
    stubFetch(api())
    await mount('/ops?state=pending&kind=transcribe&error_class=none&before=77')
    expect(select('State').value).toBe('pending')
    expect(select('Kind').value).toBe('transcribe')
    expect(select('Error class').value).toBe('none')
    expect(Object.fromEntries(lastJobGet().url.searchParams)).toEqual({
      state: 'pending',
      kind: 'transcribe',
      error_class: 'none',
      before_id: '77',
      limit: '50',
    })
  })

  it('omits unset parameters instead of sending them empty', async () => {
    stubFetch(api())
    await mount('/ops?kind=analyze')
    const q = lastJobGet().url.searchParams
    expect(q.has('error_class')).toBe(false)
    expect(q.has('before_id')).toBe(false)
    expect(q.get('kind')).toBe('analyze')
  })
})

describe('filters', () => {
  it('has labelled selects with the documented options', async () => {
    stubFetch(api())
    await mount()
    const opts = (s: HTMLSelectElement) => Array.from(s.options).map((o) => [o.value, o.textContent])
    expect(opts(select('State')).map((o) => o[0])).toEqual(['pending', 'running', 'done', 'dead'])
    expect(select('State').value).toBe('dead')
    expect(opts(select('Kind'))).toEqual([
      ['', 'Any'],
      ['ingest', 'ingest'],
      ['transcribe', 'transcribe'],
      ['analyze', 'analyze'],
    ])
    expect(opts(select('Error class')).map((o) => o[0])).toEqual([
      '', 'PERMANENT_SOURCE', 'TRANSIENT_NETWORK', 'RATE_LIMITED', 'TOOL_FAILURE',
      'LLM_INVALID_OUTPUT', 'LLM_UNAVAILABLE', 'RESOURCE', 'BUG', 'CANCELLED', 'none',
    ])
    expect(opts(select('Error class'))[0]).toEqual(['', 'Any'])
    expect(opts(select('Error class')).at(-1)).toEqual(['none', 'No class'])
  })

  it('each change pushes a history entry, requests with the new filter and AND-combines', async () => {
    stubFetch(api())
    const { router } = await mount()
    await user().selectOptions(select('Kind'), 'transcribe')
    await settle()
    expect(router.state.location.search).toBe('?kind=transcribe')
    expect(router.state.historyAction).toBe('PUSH')
    await user().selectOptions(select('Error class'), 'BUG')
    await settle()
    await user().selectOptions(select('State'), 'done')
    await settle()
    expect(router.state.location.search).toBe('?state=done&kind=transcribe&error_class=BUG')
    expect(Object.fromEntries(lastJobGet().url.searchParams)).toEqual({
      state: 'done', kind: 'transcribe', error_class: 'BUG', limit: '50',
    })
    await user().selectOptions(select('Kind'), '')
    await settle()
    expect(lastJobGet().url.searchParams.has('kind')).toBe(false)
    await user().selectOptions(select('State'), 'dead')
    await settle()
    expect(router.state.location.search).toBe('?error_class=BUG')
  })

  it.each([
    ['State', 'pending', { state: 'pending' }],
    ['Kind', 'analyze', { state: 'dead', kind: 'analyze' }],
    ['Error class', 'BUG', { state: 'dead', error_class: 'BUG' }],
    ['Error class', 'none', { state: 'dead', error_class: 'none' }],
  ])('changing only %s to %s requests a fresh list (every filter is in the key)', async (label, value, expected) => {
    stubFetch(api())
    await mount()
    await user().selectOptions(select(label), value)
    await settle()
    expect(jobGets()).toHaveLength(2)
    expect(Object.fromEntries(lastJobGet().url.searchParams)).toEqual({ ...expected, limit: '50' })
  })

  it('changing a filter resets to the first page', async () => {
    stubFetch(api())
    const { router } = await mount('/ops?before=500&kind=ingest')
    await user().selectOptions(select('Kind'), 'analyze')
    await settle()
    expect(router.state.location.search).toBe('?kind=analyze')
    expect(lastJobGet().url.searchParams.has('before_id')).toBe(false)
  })

  it('Back restores the previous filter', async () => {
    stubFetch(api())
    const { router } = await mount()
    await user().selectOptions(select('Kind'), 'analyze')
    await settle()
    await act(async () => {
      await router.navigate(-1)
    })
    await settle()
    expect(select('Kind').value).toBe('')
    expect(lastJobGet().url.searchParams.has('kind')).toBe(false)
  })
})

describe('queue depth', () => {
  const rowText = (name: string) =>
    within(within(depth()).getByRole('rowheader', { name })
      .closest('tr') as HTMLElement).getAllByRole('cell').map((c) => c.textContent)

  it('is a table with column and row headers, known order, and zeros for missing cells', async () => {
    stubFetch(api({ health: () => health({ transcribe: { running: 2, dead: 1 }, ingest: { pending: 5 } }) }))
    await mount()
    const t = within(depth()).getByRole('table')
    expect(within(t).getAllByRole('columnheader').map((c) => c.textContent)).toEqual([
      'Kind', 'pending', 'running', 'done', 'dead',
    ])
    expect(within(t).getAllByRole('rowheader').map((c) => c.textContent)).toEqual(['ingest', 'transcribe', 'analyze'])
    expect(rowText('ingest')).toEqual(['5', '0', '0', '0'])
    expect(rowText('transcribe')).toEqual(['0', '2', '0', '1'])
    expect(rowText('analyze')).toEqual(['0', '0', '0', '0'])
  })

  it('an empty queue object is a table of zeros', async () => {
    stubFetch(api())
    await mount()
    const cells = within(within(depth()).getByRole('table')).getAllByRole('cell')
    expect(cells).toHaveLength(12)
    expect(cells.every((c) => c.textContent === '0')).toBe(true)
  })

  it('shows an unknown kind and an unknown state after the known ones without crashing', async () => {
    stubFetch(api({ health: () => health({ notify: { pending: 4, snoozed: 9 }, ingest: { dead: 2 } }) }))
    await mount()
    const t = within(depth()).getByRole('table')
    expect(within(t).getAllByRole('columnheader').map((c) => c.textContent)).toEqual([
      'Kind', 'pending', 'running', 'done', 'dead', 'snoozed',
    ])
    expect(within(t).getAllByRole('rowheader').map((c) => c.textContent)).toEqual([
      'ingest', 'transcribe', 'analyze', 'notify',
    ])
    expect(rowText('notify')).toEqual(['4', '0', '0', '0', '9'])
    expect(rowText('ingest')).toEqual(['0', '0', '0', '2', '0'])
  })

  it.each([
    [{}, 'No dead jobs'],
    [{ ingest: { dead: 1 } }, '1 dead job'],
    [{ ingest: { dead: 1 }, analyze: { dead: 2 }, notify: { dead: 4 } }, '7 dead jobs'],
  ])('states the dead total in text for %j', async (queue, text) => {
    stubFetch(api({ health: () => health(queue) }))
    await mount()
    expect(within(depth()).getByText(text)).toBeTruthy()
  })

  it('renders hostile kind and state names as text', async () => {
    stubFetch(api({ health: () => health({ '<img src=x onerror=alert(1)>': { '<b>x</b>': 1 } }) }))
    await mount()
    expect(within(depth()).getByText('<img src=x onerror=alert(1)>')).toBeTruthy()
    expect(document.querySelector('img, b')).toBeNull()
  })

  it('polls every 15 s while mounted and stops after unmount', async () => {
    stubFetch(api())
    const { view } = await mount()
    expect(healthGets()).toHaveLength(1)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(14_999)
    })
    expect(healthGets()).toHaveLength(1)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1)
    })
    expect(healthGets()).toHaveLength(2)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000)
    })
    expect(healthGets()).toHaveLength(3)
    view.unmount()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(120_000)
    })
    expect(healthGets()).toHaveLength(3)
  })

  it('Refresh refetches channels, depth and the job list at once', async () => {
    stubFetch(api())
    await mount()
    await click(screen.getByRole('button', { name: 'Refresh' }))
    expect(healthGets()).toHaveLength(2)
    expect(channelGets()).toHaveLength(2)
    expect(jobGets()).toHaveLength(2)
  })
})

describe('job rows', () => {
  it('lists rows in API order, untouched, with every documented field', async () => {
    const items = [
      job(30, { video_title: 'Zebra', kind: 'transcribe', attempts: 5, error_class: 'RESOURCE' }),
      job(10, { video_title: 'Apple' }),
      job(20, { video_title: null, video_id: 'abcdefghijk', finished_at: null, state: 'pending', error_class: null }),
    ]
    stubFetch(api({ jobs: () => list(items) }))
    await mount()
    expect(bodyRows().map((r) => (r as HTMLTableRowElement).cells[0]?.textContent)).toEqual(['30', '10', '20'])
    const r30 = rowFor(30)
    expect(within(r30).getByRole('link', { name: 'Zebra' }).getAttribute('href')).toBe('/videos/v0000000030')
    expect(r30.textContent).toContain('transcribe')
    expect(r30.textContent).toContain('dead')
    expect(r30.textContent).toContain('5')
    expect(r30.textContent).toContain('RESOURCE')
    expect(r30.textContent).toContain('Disk full or out of memory. Fix that before retrying')
    // finished job: finished_at, converted to UTC (the test TZ is America/Los_Angeles)
    expect(within(r30).getByText('2026-10-02 06:30 UTC')).toBeTruthy()
    // unfinished job: created_at; title falls back to the video id
    const r20 = rowFor(20)
    expect(within(r20).getByRole('link', { name: 'abcdefghijk' }).getAttribute('href')).toBe('/videos/abcdefghijk')
    expect(within(r20).getByText('2026-09-30 08:05 UTC')).toBeTruthy()
    expect(within(r20).getByText('No class')).toBeTruthy()
    expect(within(r20).getByText('Often a lost worker (stale heartbeat)')).toBeTruthy()
  })

  it('has column headers and never shows the payload', async () => {
    stubFetch(api({ jobs: () => list([{ ...job(1), payload: { secret: 'p' } } as JobOut]) }))
    await mount()
    expect(within(jobsTable()).getAllByRole('columnheader').length).toBeGreaterThanOrEqual(8)
    expect(document.body.textContent).not.toContain('secret')
  })

  it('encodes a hostile video id in the link', async () => {
    stubFetch(api({ jobs: () => list([job(1, { video_id: '../x?y#z', video_title: null })]) }))
    await mount()
    expect(within(rowFor(1)).getByRole('link').getAttribute('href')).toBe('/videos/..%2Fx%3Fy%23z')
  })

  it('shows an unknown error class as-is with no hint, and a prototype-ish one too', async () => {
    stubFetch(api({ jobs: () => list([job(1, { error_class: 'WEIRD_NEW' }), job(2, { error_class: 'constructor' })]) }))
    await mount()
    expect(within(rowFor(1)).getByText('WEIRD_NEW')).toBeTruthy()
    expect(within(rowFor(2)).getByText('constructor')).toBeTruthy()
    expect(rowFor(2).textContent).not.toContain('function')
  })

  it('falls back to created_at when finished_at is null', async () => {
    stubFetch(api({ jobs: () => list([job(1, { finished_at: null })]) }))
    await mount()
    expect(within(rowFor(1)).getByText('2026-09-30 08:05 UTC')).toBeTruthy()
  })

  it('renders hostile title, last_error and error_class as text and creates no element', async () => {
    const evil = '<img src=x onerror=alert(1)>'
    stubFetch(
      api({ jobs: () => list([job(1, { video_title: evil, last_error: evil, error_class: evil, locked_by: evil })]) }),
    )
    await mount()
    const row = rowFor(1)
    expect(within(row).getAllByText(evil).length).toBeGreaterThanOrEqual(3)
    await click(within(row).getByRole('button', { name: 'Show full error' }))
    expect(document.querySelector('img')).toBeNull()
    expect(row.querySelectorAll('pre')).toHaveLength(1)
    expect(row.querySelector('pre')?.textContent).toBe(evil)
  })
})

describe('last_error', () => {
  const trace = ['Traceback (most recent call last):', '  File "a.py", line 1, in <module>', '    boom()', 'ValueError: nöpe ✓'].join('\n')

  it('collapses to the first line and expands into a <pre> with the full text, then hides', async () => {
    stubFetch(api({ jobs: () => list([job(1, { last_error: trace })]) }))
    await mount()
    const row = rowFor(1)
    expect(within(row).getByText('Traceback (most recent call last):')).toBeTruthy()
    expect(row.querySelector('pre')).toBeNull()
    const show = within(row).getByRole('button', { name: 'Show full error' })
    expect(show.getAttribute('aria-expanded')).toBe('false')
    await click(show)
    const pre = row.querySelector('pre') as HTMLPreElement
    expect(pre.textContent).toBe(trace)
    expect(within(row).getByRole('button', { name: 'Hide' }).getAttribute('aria-expanded')).toBe('true')
    await click(within(row).getByRole('button', { name: 'Hide' }))
    expect(row.querySelector('pre')).toBeNull()
  })

  it('expanding one row leaves the others collapsed', async () => {
    stubFetch(api({ jobs: () => list([job(1, { last_error: trace }), job(2, { last_error: trace })]) }))
    await mount()
    await click(within(rowFor(1)).getByRole('button', { name: 'Show full error' }))
    expect(rowFor(1).querySelector('pre')).not.toBeNull()
    expect(rowFor(2).querySelector('pre')).toBeNull()
  })

  it('keeps an 8 KiB single line out of the collapsed view and whole in the <pre>', async () => {
    const big = 'x'.repeat(8192)
    stubFetch(api({ jobs: () => list([job(1, { last_error: big })]) }))
    await mount()
    const row = rowFor(1)
    expect(row.textContent).not.toContain('x'.repeat(201))
    expect(row.textContent).toContain('x'.repeat(199) + '…')
    await click(within(row).getByRole('button', { name: 'Show full error' }))
    expect(row.querySelector('pre')?.textContent).toBe(big)
  })

  it('handles a 100-line traceback', async () => {
    const tb = Array.from({ length: 100 }, (_, i) => `  line ${i}`).join('\n')
    stubFetch(api({ jobs: () => list([job(1, { last_error: `Traceback:\n${tb}` })]) }))
    await mount()
    const row = rowFor(1)
    expect(row.textContent).not.toContain('line 5')
    await click(within(row).getByRole('button', { name: 'Show full error' }))
    expect(row.querySelector('pre')?.textContent).toBe(`Traceback:\n${tb}`)
  })

  it.each([null, '', '   '])('shows a dash and no control for %j', async (v) => {
    stubFetch(api({ jobs: () => list([job(1, { last_error: v })]) }))
    await mount()
    const row = rowFor(1)
    expect(within(row).queryByRole('button', { name: /full error|Hide/ })).toBeNull()
    expect(within(row).getAllByText('—').length).toBeGreaterThanOrEqual(1)
  })
})

describe('paging', () => {
  const rows = (from: number, n: number) => Array.from({ length: n }, (_, i) => job(from - i))
  const older = () => screen.getByRole('button', { name: 'Older' }) as HTMLButtonElement
  const newest = () => screen.getByRole('button', { name: 'Newest' }) as HTMLButtonElement

  it('an empty list: both controls disabled', async () => {
    stubFetch(api())
    await mount()
    expect(older().disabled).toBe(true)
    expect(newest().disabled).toBe(true)
  })

  it('exactly 50 rows with next_before_id=null: Older is disabled', async () => {
    stubFetch(api({ jobs: () => list(rows(100, 50), null) }))
    await mount()
    expect(bodyRows()).toHaveLength(50)
    expect(older().disabled).toBe(true)
  })

  it('51 matching rows: page 1 full with a cursor, page 2 with one row, and back to newest', async () => {
    stubFetch(
      api({
        jobs: (c) => (c.url.searchParams.get('before_id') === '51' ? list([job(1)], null) : list(rows(101, 50), 51)),
      }),
    )
    const { router } = await mount()
    expect(bodyRows()).toHaveLength(50)
    expect(older().disabled).toBe(false)
    expect(newest().disabled).toBe(true)
    await click(older())
    expect(router.state.location.search).toBe('?before=51')
    expect(router.state.historyAction).toBe('PUSH')
    expect(lastJobGet().url.searchParams.get('before_id')).toBe('51')
    expect(bodyRows()).toHaveLength(1)
    expect(older().disabled).toBe(true)
    expect(newest().disabled).toBe(false)
    await click(newest())
    expect(router.state.location.search).toBe('')
    expect(lastJobGet().url.searchParams.has('before_id')).toBe(false)
    expect(bodyRows()).toHaveLength(50)
  })

  it('keeps the current rows with a loading indicator and disabled controls while the next page loads', async () => {
    const d = deferred()
    stubFetch(api({ jobs: (c) => (c.url.searchParams.has('before_id') ? d.promise : list(rows(100, 3), 98)) }))
    await mount()
    await click(older())
    expect(bodyRows()).toHaveLength(3)
    expect(within(jobsRegion()).getByText('Loading jobs…')).toBeTruthy()
    expect(older().disabled).toBe(true)
    expect(newest().disabled).toBe(true)
    await act(async () => {
      d.resolve(json(list([job(97)], null)))
    })
    await settle()
    expect(bodyRows()).toHaveLength(1)
    expect(within(jobsRegion()).queryByText('Loading jobs…')).toBeNull()
  })
})

describe('empty states', () => {
  it('default filter: "No dead jobs" and no reset control', async () => {
    stubFetch(api())
    await mount()
    expect(within(jobsRegion()).getByText('No dead jobs')).toBeTruthy()
    expect(within(jobsRegion()).queryByText('No jobs match these filters')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Reset filters' })).toBeNull()
  })

  it('other filters: "No jobs match these filters" with Reset filters, which clears the URL', async () => {
    stubFetch(api())
    const { router } = await mount('/ops?state=pending&kind=ingest')
    expect(within(jobsRegion()).getByText('No jobs match these filters')).toBeTruthy()
    expect(within(jobsRegion()).queryByText('No dead jobs')).toBeNull()
    await click(screen.getByRole('button', { name: 'Reset filters' }))
    expect(router.state.location.search).toBe('')
    expect(select('State').value).toBe('dead')
    expect(select('Kind').value).toBe('')
    expect(lastJobGet().url.searchParams.get('state')).toBe('dead')
  })

  it.each(['/ops?error_class=none', '/ops?state=pending', '/ops?kind=ingest', '/ops?state=dead&kind=analyze'])(
    'a single non-default filter (%s) triggers the second message',
    async (entry) => {
      stubFetch(api())
      await mount(entry)
      expect(within(jobsRegion()).getByText('No jobs match these filters')).toBeTruthy()
      expect(within(jobsRegion()).queryByText('No dead jobs')).toBeNull()
    },
  )

  it('?state=dead alone is still the default filter', async () => {
    stubFetch(api())
    await mount('/ops?state=dead')
    expect(within(jobsRegion()).getByText('No dead jobs')).toBeTruthy()
  })

  it('shows neither empty message from stale empty data while another filter loads', async () => {
    const d = deferred()
    stubFetch(api({ jobs: (c) => (c.url.searchParams.has('kind') ? d.promise : list([])) }))
    await mount()
    expect(within(jobsRegion()).getByText('No dead jobs')).toBeTruthy()
    await user().selectOptions(select('Kind'), 'ingest')
    await settle()
    expect(within(jobsRegion()).getByText('Loading jobs…')).toBeTruthy()
    expect(within(jobsRegion()).queryByText('No dead jobs')).toBeNull()
    expect(within(jobsRegion()).queryByText('No jobs match these filters')).toBeNull()
    await act(async () => {
      d.resolve(json(list([])))
    })
    await settle()
    expect(within(jobsRegion()).getByText('No jobs match these filters')).toBeTruthy()
  })
})

describe('untrusted URL parameters', () => {
  it('never sends or shows a marker in any invalid parameter, and drops them with replace', async () => {
    stubFetch(api())
    const { router } = await mount('/ops?state=MARKER1&kind=MARKER2&error_class=MARKER3&before=MARKER4')
    expect(calls.length).toBeGreaterThan(0)
    for (const c of calls) expect(c.url.toString()).not.toContain('MARKER')
    expect(document.body.innerHTML).not.toContain('MARKER')
    expect(Object.fromEntries(lastJobGet().url.searchParams)).toEqual({ state: 'dead', limit: '50' })
    expect(router.state.location.search).toBe('')
    expect(router.state.historyAction).toBe('REPLACE')
    expect(select('State').value).toBe('dead')
    expect(within(jobsRegion()).getByText('No dead jobs')).toBeTruthy()
  })

  it.each([
    ['state=DEAD', 'state'],
    ['kind=Ingest', 'kind'],
    ['state=', 'state'],
    ['kind=', 'kind'],
    ['error_class=', 'error_class'],
    ['error_class=bug', 'error_class'],
    ['before=0', 'before_id'],
    ['before=-1', 'before_id'],
    ['before=1.5', 'before_id'],
    ['before=abc', 'before_id'],
    ['before=1e3', 'before_id'],
    ['before=9007199254740992', 'before_id'],
    ['before=007', 'before_id'],
  ])('%s is rejected and not sent', async (qs, param) => {
    stubFetch(api())
    await mount(`/ops?${qs}`)
    const sent = lastJobGet().url.searchParams
    expect(sent.has(param)).toBe(param === 'state' ? true : false)
    if (param === 'state') expect(sent.get('state')).toBe('dead')
  })

  it('keeps the largest safe cursor', async () => {
    stubFetch(api())
    await mount('/ops?before=9007199254740991')
    expect(lastJobGet().url.searchParams.get('before_id')).toBe('9007199254740991')
  })
})

describe('load errors', () => {
  it('shows a loading indicator per part first, not an empty table or the empty message', async () => {
    const h = deferred()
    const j = deferred()
    stubFetch(api({ health: () => h.promise, jobs: () => j.promise }))
    await mount()
    expect(within(depth()).getByText('Loading queue depth…')).toBeTruthy()
    expect(within(jobsRegion()).getByText('Loading jobs…')).toBeTruthy()
    expect(screen.queryByRole('table')).toBeNull()
    expect(screen.queryByText('No dead jobs')).toBeNull()
    await act(async () => {
      h.resolve(json(health()))
      j.resolve(json(list([])))
    })
    await settle()
    expect(screen.queryByText('Loading jobs…')).toBeNull()
  })

  it('the depth failing does not stop the job list, and vice versa', async () => {
    stubFetch(api({ health: () => json({}, 500) , jobs: () => list([job(1)]) }))
    await mount()
    expect(within(depth()).getByText('Could not load queue depth')).toBeTruthy()
    expect(rowFor(1)).toBeTruthy()
  })

  it('the job list failing does not stop the depth', async () => {
    stubFetch(api({ health: () => health({ ingest: { dead: 3 } }), jobs: () => json({}, 500) }))
    await mount()
    expect(within(jobsRegion()).getByText('Could not load jobs')).toBeTruthy()
    expect(within(depth()).getByText('3 dead jobs')).toBeTruthy()
  })

  it('healthz 503 says the database is down, with Retry that refetches', async () => {
    let n = 0
    stubFetch(api({ health: () => (n++ === 0 ? json({ detail: 'SECRET' }, 503) : health({ ingest: { dead: 2 } })) }))
    await mount()
    expect(within(depth()).getByText('Queue depth unavailable: the database is down')).toBeTruthy()
    await click(within(depth()).getByRole('button', { name: 'Retry' }))
    expect(within(depth()).getByText('2 dead jobs')).toBeTruthy()
    expect(within(depth()).queryByText(/unavailable/)).toBeNull()
  })

  it('jobs 503 says the database is unavailable, with Retry that refetches', async () => {
    let n = 0
    stubFetch(api({ jobs: () => (n++ === 0 ? json({ detail: 'SECRET' }, 503) : list([job(4)])) }))
    await mount()
    expect(within(jobsRegion()).getByText('The database is unavailable. Try again shortly.')).toBeTruthy()
    await click(within(jobsRegion()).getByRole('button', { name: 'Retry' }))
    expect(rowFor(4)).toBeTruthy()
  })

  it.each([
    ['500', () => json({ detail: 'SECRET /api/healthz Traceback' }, 500)],
    ['422', () => json({ detail: [{ msg: 'SECRET', input: 'SECRET' }] }, 422)],
    ['404', () => json({ detail: 'SECRET' }, 404)],
    ['a network failure', () => Promise.reject(new TypeError('SECRET fetch failed /api/healthz'))],
    ['a malformed body', () => json({ unexpected: 'SECRET' })],
    ['a non-JSON body', () => new Response('SECRET <html>', { status: 200 })],
  ])('%s shows the generic message, a Retry button, and no body, URL or exception text', async (_n, reply) => {
    stubFetch(api({ health: reply as Handler, jobs: reply as Handler }))
    await mount()
    expect(within(depth()).getByText('Could not load queue depth')).toBeTruthy()
    expect(within(jobsRegion()).getByText('Could not load jobs')).toBeTruthy()
    expect(within(depth()).getByRole('button', { name: 'Retry' })).toBeTruthy()
    expect(within(jobsRegion()).getByRole('button', { name: 'Retry' })).toBeTruthy()
    expect(document.body.textContent).not.toMatch(/SECRET|\/api\/|TypeError|fetch failed/)
    expect(screen.queryByText('No dead jobs')).toBeNull()
  })
})

describe('retry', () => {
  const retryBtn = (id: number) => within(rowFor(id)).queryByRole('button', { name: 'Retry' })

  it('offers Retry only on dead rows', async () => {
    stubFetch(
      api({
        jobs: () =>
          list([job(4), job(3, { state: 'pending' }), job(2, { state: 'running' }), job(1, { state: 'done' })]),
      }),
    )
    await mount('/ops?state=done')
    expect(retryBtn(4)).not.toBeNull()
    for (const id of [1, 2, 3]) expect(retryBtn(id)).toBeNull()
    for (const id of [1, 2, 3]) expect(within(rowFor(id)).queryByRole('button', { name: /confirm|^cancel$/i })).toBeNull()
  })

  it('Retry asks first, names job, kind and video, sends nothing, and moves focus to Confirm', async () => {
    stubFetch(api({ jobs: () => list([job(12, { kind: 'analyze', video_title: 'My video' })]) }))
    await mount()
    await click(retryBtn(12) as HTMLElement)
    const row = rowFor(12)
    expect(posts()).toHaveLength(0)
    expect(row.textContent).toContain('job #12')
    expect(row.textContent).toContain('analyze')
    expect(row.textContent).toContain('My video')
    expect(row.textContent).not.toMatch(/hours of CPU/)
    expect(document.activeElement).toBe(within(row).getByRole('button', { name: 'Confirm retry' }))
  })

  it('a transcribe job warns that transcription can take hours of CPU', async () => {
    stubFetch(api({ jobs: () => list([job(12, { kind: 'transcribe' })]) }))
    await mount()
    await click(retryBtn(12) as HTMLElement)
    expect(rowFor(12).textContent).toMatch(/hours of CPU/)
  })

  it('Cancel sends nothing and returns focus to Retry', async () => {
    stubFetch(api({ jobs: () => list([job(12)]) }))
    await mount()
    await click(retryBtn(12) as HTMLElement)
    await click(within(rowFor(12)).getByRole('button', { name: 'Cancel' }))
    expect(posts()).toHaveLength(0)
    expect(within(rowFor(12)).queryByRole('button', { name: 'Confirm retry' })).toBeNull()
    expect(document.activeElement).toBe(retryBtn(12))
  })

  it('does not use window.confirm', async () => {
    const confirm = vi.fn(() => true)
    vi.stubGlobal('confirm', confirm)
    stubFetch(api({ jobs: () => list([job(12)]), retry: () => job(12, { state: 'pending' }) }))
    await mount()
    await click(retryBtn(12) as HTMLElement)
    await click(within(rowFor(12)).getByRole('button', { name: 'Confirm retry' }))
    expect(confirm).not.toHaveBeenCalled()
  })

  it('Confirm sends exactly one POST with a JSON content type and body {}', async () => {
    stubFetch(api({ jobs: () => list([job(12)]), retry: () => job(12, { state: 'pending' }) }))
    await mount()
    await click(retryBtn(12) as HTMLElement)
    await click(within(rowFor(12)).getByRole('button', { name: 'Confirm retry' }))
    expect(posts()).toHaveLength(1)
    const p = posts()[0] as Call
    expect(p.url.pathname).toBe('/api/ops/jobs/12/retry')
    expect(p.url.search).toBe('')
    expect(p.headers.get('content-type')).toBe('application/json')
    expect(p.body).toBe('{}')
  })

  it('a double click sends one request, disables that row, and leaves other rows usable', async () => {
    const d = deferred()
    stubFetch(api({ jobs: () => list([job(12), job(11)]), retry: () => d.promise }))
    await mount()
    await click(retryBtn(12) as HTMLElement)
    const confirm = within(rowFor(12)).getByRole('button', { name: 'Confirm retry' })
    await user().click(confirm)
    await user().click(confirm)
    await settle()
    expect(posts()).toHaveLength(1)
    expect((within(rowFor(12)).getByRole('button', { name: 'Confirm retry' }) as HTMLButtonElement).disabled).toBe(true)
    expect((within(rowFor(12)).getByRole('button', { name: 'Cancel' }) as HTMLButtonElement).disabled).toBe(true)
    expect((retryBtn(11) as HTMLButtonElement).disabled).toBe(false)
    await act(async () => {
      d.resolve(json(job(12, { state: 'pending' })))
    })
    await settle()
  })

  it('does not retry the request automatically on a server error', async () => {
    stubFetch(api({ jobs: () => list([job(12)]), retry: () => json({}, 500) }))
    await mount()
    await click(retryBtn(12) as HTMLElement)
    await click(within(rowFor(12)).getByRole('button', { name: 'Confirm retry' }))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    expect(posts()).toHaveLength(1)
  })

  it.each<[string, () => Reply, string]>([
    ['200', () => job(12, { state: 'pending' }), 'Queued again: job #12 is pending'],
    ['404', () => json({ detail: 'job not found' }, 404), 'This job no longer exists'],
    ['409 not dead (running)', () => json({ detail: 'job is not dead', state: 'running' }, 409), 'Job is already running'],
    ['409 not dead (unknown state)', () => json({ detail: 'job is not dead', state: '<b>x</b>' }, 409), 'Job is no longer dead'],
    ['409 superseded', () => json({ detail: 'superseded by a newer job', job_id: 99 }, 409), 'A newer job #99 exists for this video'],
    ['409 superseded (bad id)', () => json({ detail: 'superseded by a newer job', job_id: '99' }, 409), 'A newer job exists for this video'],
    ['409 other', () => json({ detail: 'SECRET' }, 409), 'The job could not be retried'],
    ['429', () => json({ detail: 'SECRET' }, 429), 'Too many requests. Try again shortly.'],
    ['503', () => json({ detail: 'SECRET' }, 503), 'The database is unavailable. Try again shortly.'],
    ['500', () => json({ detail: 'SECRET' }, 500), 'Retry failed'],
    ['415', () => json({ detail: 'SECRET' }, 415), 'Retry failed'],
    ['a network failure', () => Promise.reject(new TypeError('SECRET boom')), 'Retry failed'],
  ])('shows the fixed message for %s and never the body', async (_n, reply, message) => {
    // The list keeps returning the job, so the row stays and shows the message.
    stubFetch(api({ jobs: () => list([job(12)]), retry: reply }))
    await mount()
    await click(retryBtn(12) as HTMLElement)
    await click(within(rowFor(12)).getByRole('button', { name: 'Confirm retry' }))
    expect(within(rowFor(12)).getByText(message)).toBeTruthy()
    expect(liveText()).toContain(message)
    expect(document.body.textContent).not.toContain('SECRET')
    // the row is back to idle: Retry is offered again, no confirmation left open
    expect(retryBtn(12)).not.toBeNull()
    expect(within(rowFor(12)).queryByRole('button', { name: 'Confirm retry' })).toBeNull()
  })

  it('refetches the list and the depth after any response, so a retried job leaves the dead list', async () => {
    let retried = false
    stubFetch(
      api({
        jobs: () => list(retried ? [job(11)] : [job(12), job(11)]),
        retry: () => {
          retried = true
          return job(12, { state: 'pending' })
        },
        health: () => health({ ingest: { dead: retried ? 1 : 2, pending: retried ? 1 : 0 } }),
      }),
    )
    await mount()
    expect(within(depth()).getByText('2 dead jobs')).toBeTruthy()
    await click(retryBtn(12) as HTMLElement)
    await click(within(rowFor(12)).getByRole('button', { name: 'Confirm retry' }))
    expect(jobGets()).toHaveLength(2)
    expect(healthGets()).toHaveLength(2)
    expect(() => rowFor(12)).toThrow()
    expect(within(depth()).getByText('1 dead job')).toBeTruthy()
    // the message outlives the vanished row, in a polite live region
    expect(liveText()).toContain('Queued again: job #12 is pending')
    expect(screen.getByText('Queued again: job #12 is pending')).toBeTruthy()
  })

  it.each([404, 409, 429, 500, 503])('refetches after a %i too', async (status) => {
    stubFetch(api({ jobs: () => list([job(12)]), retry: () => json({ detail: 'x' }, status) }))
    await mount()
    await click(retryBtn(12) as HTMLElement)
    await click(within(rowFor(12)).getByRole('button', { name: 'Confirm retry' }))
    expect(jobGets()).toHaveLength(2)
    expect(healthGets()).toHaveLength(2)
  })

  it('does not refetch after a network failure', async () => {
    stubFetch(api({ jobs: () => list([job(12)]), retry: () => Promise.reject(new TypeError('down')) }))
    await mount()
    await click(retryBtn(12) as HTMLElement)
    await click(within(rowFor(12)).getByRole('button', { name: 'Confirm retry' }))
    expect(jobGets()).toHaveLength(1)
    expect(healthGets()).toHaveLength(1)
  })

  it('does not retry a network failure automatically, even when the client would', async () => {
    stubFetch(api({ jobs: () => list([job(12)]), retry: () => Promise.reject(new TypeError('down')) }))
    await mount('/ops', { mutations: { retry: 3 } })
    await click(retryBtn(12) as HTMLElement)
    await click(within(rowFor(12)).getByRole('button', { name: 'Confirm retry' }))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    expect(posts()).toHaveLength(1)
    expect(screen.getByText('Retry failed')).toBeTruthy()
  })

  it('Refresh clears the result message', async () => {
    stubFetch(api({ jobs: () => list([job(12)]), retry: () => json({}, 429) }))
    await mount()
    await click(retryBtn(12) as HTMLElement)
    await click(within(rowFor(12)).getByRole('button', { name: 'Confirm retry' }))
    expect(screen.getByText('Too many requests. Try again shortly.')).toBeTruthy()
    await click(screen.getByRole('button', { name: 'Refresh' }))
    expect(screen.queryByText('Too many requests. Try again shortly.')).toBeNull()
  })

  it('confirming another job clears an earlier result that arrived meanwhile', async () => {
    const a = deferred()
    const b = deferred()
    stubFetch(
      api({
        jobs: () => list([job(12), job(11)]),
        retry: (c) => (c.url.pathname.includes('/12/') ? a.promise : b.promise),
      }),
    )
    await mount()
    await click(retryBtn(12) as HTMLElement)
    await click(within(rowFor(12)).getByRole('button', { name: 'Confirm retry' }))
    await click(retryBtn(11) as HTMLElement)
    await act(async () => {
      a.resolve(json({}, 429))
    })
    await settle()
    expect(screen.getByText('Too many requests. Try again shortly.')).toBeTruthy()
    await click(within(rowFor(11)).getByRole('button', { name: 'Confirm retry' }))
    expect(screen.queryByText('Too many requests. Try again shortly.')).toBeNull()
    await act(async () => {
      b.resolve(json({}, 404))
    })
    await settle()
  })

  it('keeps the result message until the next action, then replaces it', async () => {
    let status = 429
    stubFetch(
      api({
        jobs: () => list([job(12)]),
        retry: () => (status === 429 ? json({}, 429) : json({ detail: 'job not found' }, 404)),
      }),
    )
    await mount()
    await click(retryBtn(12) as HTMLElement)
    await click(within(rowFor(12)).getByRole('button', { name: 'Confirm retry' }))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    expect(screen.getByText('Too many requests. Try again shortly.')).toBeTruthy()
    status = 404
    await click(retryBtn(12) as HTMLElement)
    expect(screen.queryByText('Too many requests. Try again shortly.')).toBeNull()
    await click(within(rowFor(12)).getByRole('button', { name: 'Confirm retry' }))
    expect(screen.getByText('This job no longer exists')).toBeTruthy()
    expect(screen.queryByText('Too many requests. Try again shortly.')).toBeNull()
  })
})

describe('accessibility and source', () => {
  it('every interactive control is a button, link or select with an accessible name', async () => {
    stubFetch(api({ jobs: () => list([job(1)]) }))
    await mount()
    for (const el of screen.getAllByRole('button')) expect(el.tagName).toBe('BUTTON')
    for (const s of screen.getAllByRole('combobox')) expect(s.tagName).toBe('SELECT')
    expect(select('State').labels?.length).toBe(1)
    expect(select('Kind').labels?.length).toBe(1)
    expect(select('Error class').labels?.length).toBe(1)
    expect(document.querySelector('[onclick]')).toBeNull()
    // Library precedent: tabIndex={0} on a labelled role="region" is the accessible
    // pattern for a wide, horizontally scrolling table. Any other tabindex is stray.
    for (const el of document.querySelectorAll('[tabindex]')) {
      expect(el.getAttribute('tabindex')).toBe('0')
      expect(el.getAttribute('role')).toBe('region')
      expect(el.getAttribute('aria-label')).toBeTruthy()
    }
  })
})

describe('transcription progress (#245)', () => {
  const NOW = Date.parse('2026-10-23T12:00:00Z')
  const running = (id: number, over: Partial<JobOut> = {}) =>
    job(id, {
      kind: 'transcribe',
      state: 'running',
      error_class: null,
      last_error: null,
      finished_at: null,
      progress: {
        percent: 40,
        done_sec: 400,
        duration_sec: 1000,
        elapsed_sec: 300,
        eta_sec: 720,
        estimated_sec: 600,
        estimate_source: 'default',
        updated_at: new Date(Date.now() - 5_000).toISOString(),
      },
      ...over,
    })
  const progressCell = (id: number) => {
    const headers = Array.from(within(jobsTable()).getAllByRole('columnheader')).map((h) => h.textContent)
    return rowFor(id).cells[headers.indexOf('Progress')] as HTMLTableCellElement
  }

  beforeEach(() => {
    vi.setSystemTime(NOW)
  })

  it('has its own Progress column, filled only for transcribe jobs that are running or pending', async () => {
    const pending = job(2, {
      kind: 'transcribe',
      state: 'pending',
      progress: {
        percent: null,
        done_sec: null,
        duration_sec: 1000,
        elapsed_sec: null,
        eta_sec: 600,
        estimated_sec: 600,
        estimate_source: 'history',
        updated_at: null,
      },
    })
    stubFetch(
      api({
        jobs: () =>
          list([running(1), pending, job(3, { kind: 'ingest', state: 'running' }), running(4, { progress: null })]),
      }),
    )
    await mount('/ops?state=running')

    expect(within(jobsTable()).getByRole('columnheader', { name: 'Progress' })).toBeTruthy()
    const first = progressCell(1)
    expect(within(first).getByText('40%')).toBeTruthy()
    expect(first.querySelector('progress')).not.toBeNull()
    expect(within(first).getByText('Elapsed 5 min')).toBeTruthy()
    expect(within(first).getByText('about 12 min left (rough)')).toBeTruthy()
    expect(progressCell(2).textContent).toBe('estimated about 10 min')
    expect(progressCell(3).textContent).toBe('')
    expect(progressCell(4).textContent).toBe('')
  })

  it('refetches every 30 s while a transcribe job is running, and stops once none is', async () => {
    let reply = list([running(1)])
    stubFetch(api({ jobs: () => reply }))
    await mount('/ops?state=running')
    expect(jobGets()).toHaveLength(1)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(29_999)
    })
    expect(jobGets()).toHaveLength(1)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1)
    })
    expect(jobGets()).toHaveLength(2)

    reply = list([])
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000)
    })
    expect(jobGets()).toHaveLength(3)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(120_000)
    })
    expect(jobGets()).toHaveLength(3)
  })

  it('does not poll for pending, other-kind or finished jobs', async () => {
    stubFetch(
      api({
        jobs: () =>
          list([
            job(1, { kind: 'transcribe', state: 'pending' }),
            job(2, { kind: 'ingest', state: 'running' }),
            job(3),
          ]),
      }),
    )
    await mount('/ops?state=running')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(120_000)
    })
    expect(jobGets()).toHaveLength(1)
  })

  it('does not poll while the tab is in the background', async () => {
    stubFetch(api({ jobs: () => list([running(1)]) }))
    await mount('/ops?state=running')
    focusManager.setFocused(false)
    try {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(120_000)
      })
      expect(jobGets()).toHaveLength(1)
    } finally {
      focusManager.setFocused(undefined)
    }
  })

  it('shows "no update for N min" once the figures are 90 s old, then drops it for fresh ones', async () => {
    const fresh = running(1)
    const stale = running(1, {
      progress: { ...(fresh.progress as NonNullable<JobOut['progress']>), updated_at: new Date(NOW - 90_000).toISOString() },
    })
    let reply = list([stale])
    stubFetch(api({ jobs: () => reply }))
    await mount('/ops?state=running')
    expect(within(progressCell(1)).getByRole('status').textContent).toBe('no update for 1 min')
    expect(progressCell(1).textContent).not.toMatch(/left/)

    reply = list([running(1)])
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000)
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50)
    })
    expect(within(progressCell(1)).queryByRole('status')).toBeNull()
    expect(within(progressCell(1)).getByText(/left/)).toBeTruthy()
  })
})

describe('retry all matching (#161)', () => {
  const bulkBtn = () => screen.queryByRole('button', { name: 'Retry all matching' }) as HTMLButtonElement | null
  const confirmBtn = () => screen.getByRole('button', { name: 'Confirm retry' }) as HTMLButtonElement
  const cancelBtn = () => screen.getByRole('button', { name: 'Cancel' }) as HTMLButtonElement
  const bulkPosts = () => posts().map((c) => JSON.parse(c.body ?? 'null') as Record<string, unknown>)
  const out = (over: Record<string, unknown> = {}) => ({
    retried: 3,
    job_ids: [3, 2, 1],
    has_more: false,
    dry_run: true,
    ...over,
  })
  /** Dry runs answer with the count; real runs with `real`. */
  const bulk = (real: () => Reply = () => out({ dry_run: false }), count = 3): Handler => (c) =>
    (JSON.parse(c.body ?? '{}') as { dry_run: boolean }).dry_run ? out({ retried: count }) : real()

  it('is offered in the dead view and absent for every other state', async () => {
    stubFetch(api())
    await mount()
    expect(bulkBtn()).not.toBeNull()
    for (const state of ['pending', 'running', 'done']) {
      stubFetch(api())
      document.body.innerHTML = ''
      await mount(`/ops?state=${state}`)
      expect(bulkBtn()).toBeNull()
    }
  })

  it('opens an inline confirmation with the dry-run count and the scope, and sends no real retry', async () => {
    stubFetch(api({ bulk: bulk() }))
    await mount()
    await click(bulkBtn() as HTMLElement)
    expect(bulkPosts()).toEqual([{ state: 'dead', limit: 50, dry_run: true }])
    expect(screen.getByText(/Retry 3 jobs \(all dead jobs\)\?/)).toBeTruthy()
    expect(screen.getByText(/A transcription can take hours of CPU\./)).toBeTruthy()
    expect(document.activeElement).toBe(confirmBtn())
    expect(bulkBtn()).toBeNull()
  })

  it('shows the transcribe warning only when transcription is in scope', async () => {
    stubFetch(api({ bulk: bulk() }))
    await mount('/ops?kind=analyze')
    await click(bulkBtn() as HTMLElement)
    expect(screen.getByText(/Retry 3 jobs \(dead jobs with kind analyze\)\?/)).toBeTruthy()
    expect(screen.queryByText(/hours of CPU/)).toBeNull()
  })

  it('Cancel sends nothing more and returns focus to the trigger', async () => {
    stubFetch(api({ bulk: bulk() }))
    await mount()
    await click(bulkBtn() as HTMLElement)
    await click(cancelBtn())
    expect(posts()).toHaveLength(1)
    expect(document.activeElement).toBe(bulkBtn())
  })

  it('says so, and offers no confirmation, when nothing matches', async () => {
    stubFetch(api({ bulk: bulk(undefined, 0) }))
    await mount()
    await click(bulkBtn() as HTMLElement)
    expect(screen.queryByRole('button', { name: 'Confirm retry' })).toBeNull()
    expect(liveText()).toContain('No dead jobs match these filters.')
  })

  it.each<[string, string, Record<string, unknown>]>([
    ['Any / Any', '/ops', { state: 'dead', limit: 50 }],
    ['kind', '/ops?kind=transcribe', { state: 'dead', kind: 'transcribe', limit: 50 }],
    ['class', '/ops?error_class=BUG', { state: 'dead', error_class: 'BUG', limit: 50 }],
    ['No class', '/ops?kind=ingest&error_class=none', { state: 'dead', kind: 'ingest', error_class: 'none', limit: 50 }],
  ])('Confirm sends the mapped body (%s)', async (_n, url, expected) => {
    stubFetch(api({ bulk: bulk() }))
    await mount(url)
    await click(bulkBtn() as HTMLElement)
    await click(confirmBtn())
    expect(bulkPosts()).toEqual([
      { ...expected, dry_run: true },
      { ...expected, dry_run: false },
    ])
    expect(posts()[1]?.headers.get('content-type')).toBe('application/json')
  })

  it('announces the count, closes the confirmation and refreshes the list and depth', async () => {
    stubFetch(api({ bulk: bulk(() => out({ retried: 3, dry_run: false })) }))
    await mount()
    const gets = jobGets().length
    const depthGets = healthGets().length
    await click(bulkBtn() as HTMLElement)
    await click(confirmBtn())
    expect(liveText()).toContain('Retried 3 jobs.')
    expect(liveText()).not.toContain('limit')
    expect(screen.queryByRole('button', { name: 'Confirm retry' })).toBeNull()
    expect(jobGets().length).toBeGreaterThan(gets)
    expect(healthGets().length).toBeGreaterThan(depthGets)
  })

  it('notes when the per-call cap was reached', async () => {
    stubFetch(api({ bulk: bulk(() => out({ retried: 50, has_more: true, dry_run: false })) }))
    await mount()
    await click(bulkBtn() as HTMLElement)
    await click(confirmBtn())
    expect(liveText()).toContain('Retried 50 jobs. The limit of 50 per call was reached and more remain')
  })

  it.each<[string, () => Reply, string]>([
    ['409', () => json({ detail: 'SECRET' }, 409), 'Concurrent change, try again'],
    ['429', () => json({ detail: 'SECRET' }, 429), 'Too many requests. Try again shortly.'],
    ['503', () => json({ detail: 'SECRET' }, 503), 'The database is unavailable. Try again shortly.'],
    ['422', () => json({ detail: 'SECRET' }, 422), 'Retry failed'],
    ['bad shape', () => json({ retried: 'x' }), 'Retry failed'],
    ['network', () => Promise.reject(new TypeError('SECRET boom')) as unknown as Response, 'Retry failed'],
  ])('a failing real retry (%s) shows a fixed message once, never the server text', async (_n, reply, text) => {
    stubFetch(api({ bulk: bulk(reply) }))
    await mount()
    await click(bulkBtn() as HTMLElement)
    await click(confirmBtn())
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    expect(liveText()).toContain(text)
    expect(document.body.textContent).not.toContain('SECRET')
    expect(posts()).toHaveLength(2)
  })

  it('a failing dry run shows a fixed message and opens no confirmation', async () => {
    stubFetch(api({ bulk: () => json({ detail: 'SECRET' }, 503) }))
    await mount()
    await click(bulkBtn() as HTMLElement)
    expect(liveText()).toContain('The database is unavailable. Try again shortly.')
    expect(screen.queryByRole('button', { name: 'Confirm retry' })).toBeNull()
    expect(bulkBtn()).not.toBeNull()
  })

  it('disables Confirm and Cancel in flight and a double click fires one retry', async () => {
    const d = deferred()
    stubFetch(api({ bulk: bulk(() => d.promise) }))
    await mount()
    await click(bulkBtn() as HTMLElement)
    const c = confirmBtn()
    fireEvent.click(c)
    fireEvent.click(c)
    await settle()
    expect(posts()).toHaveLength(2)
    expect(confirmBtn().disabled).toBe(true)
    expect(cancelBtn().disabled).toBe(true)
    await act(async () => {
      d.resolve(json(out({ dry_run: false })))
    })
    await settle()
  })

  it('disables the trigger while the dry run is in flight', async () => {
    const d = deferred()
    stubFetch(api({ bulk: () => d.promise }))
    await mount()
    fireEvent.click(bulkBtn() as HTMLElement)
    fireEvent.click(bulkBtn() as HTMLElement)
    await settle()
    expect(posts()).toHaveLength(1)
    expect(bulkBtn()?.disabled).toBe(true)
    await act(async () => {
      d.resolve(json(out()))
    })
    await settle()
  })
})

describe('retry all matching (#161)', () => {
  const bulkBtn = () => screen.queryByRole('button', { name: 'Retry all matching' }) as HTMLButtonElement | null
  const confirmBtn = () => screen.getByRole('button', { name: 'Confirm retry' }) as HTMLButtonElement
  const cancelBtn = () => screen.getByRole('button', { name: 'Cancel' }) as HTMLButtonElement
  const bulkPosts = () => posts().map((c) => JSON.parse(c.body ?? 'null') as Record<string, unknown>)
  const out = (over: Record<string, unknown> = {}) => ({
    retried: 3,
    job_ids: [3, 2, 1],
    has_more: false,
    dry_run: true,
    ...over,
  })
  /** Dry runs answer with the count; real runs with `real`. */
  const bulk = (real: () => Reply = () => out({ dry_run: false }), count = 3): Handler => (c) =>
    (JSON.parse(c.body ?? '{}') as { dry_run: boolean }).dry_run ? out({ retried: count }) : real()

  it('is offered in the dead view and absent for every other state', async () => {
    stubFetch(api())
    await mount()
    expect(bulkBtn()).not.toBeNull()
    for (const state of ['pending', 'running', 'done']) {
      stubFetch(api())
      document.body.innerHTML = ''
      await mount(`/ops?state=${state}`)
      expect(bulkBtn()).toBeNull()
    }
  })

  it('opens an inline confirmation with the dry-run count and the scope, and sends no real retry', async () => {
    stubFetch(api({ bulk: bulk() }))
    await mount()
    await click(bulkBtn() as HTMLElement)
    expect(bulkPosts()).toEqual([{ state: 'dead', limit: 50, dry_run: true }])
    expect(screen.getByText(/Retry 3 jobs \(all dead jobs\)\?/)).toBeTruthy()
    expect(screen.getByText(/A transcription can take hours of CPU\./)).toBeTruthy()
    expect(document.activeElement).toBe(confirmBtn())
    expect(bulkBtn()).toBeNull()
  })

  it('shows the transcribe warning only when transcription is in scope', async () => {
    stubFetch(api({ bulk: bulk() }))
    await mount('/ops?kind=analyze')
    await click(bulkBtn() as HTMLElement)
    expect(screen.getByText(/Retry 3 jobs \(dead jobs with kind analyze\)\?/)).toBeTruthy()
    expect(screen.queryByText(/hours of CPU/)).toBeNull()
  })

  it('Cancel sends nothing more and returns focus to the trigger', async () => {
    stubFetch(api({ bulk: bulk() }))
    await mount()
    await click(bulkBtn() as HTMLElement)
    await click(cancelBtn())
    expect(posts()).toHaveLength(1)
    expect(document.activeElement).toBe(bulkBtn())
  })

  it('says so, and offers no confirmation, when nothing matches', async () => {
    stubFetch(api({ bulk: bulk(undefined, 0) }))
    await mount()
    await click(bulkBtn() as HTMLElement)
    expect(screen.queryByRole('button', { name: 'Confirm retry' })).toBeNull()
    expect(liveText()).toContain('No dead jobs match these filters.')
  })

  it.each<[string, string, Record<string, unknown>]>([
    ['Any / Any', '/ops', { state: 'dead', limit: 50 }],
    ['kind', '/ops?kind=transcribe', { state: 'dead', kind: 'transcribe', limit: 50 }],
    ['class', '/ops?error_class=BUG', { state: 'dead', error_class: 'BUG', limit: 50 }],
    ['No class', '/ops?kind=ingest&error_class=none', { state: 'dead', kind: 'ingest', error_class: 'none', limit: 50 }],
  ])('Confirm sends the mapped body (%s)', async (_n, url, expected) => {
    stubFetch(api({ bulk: bulk() }))
    await mount(url)
    await click(bulkBtn() as HTMLElement)
    await click(confirmBtn())
    expect(bulkPosts()).toEqual([
      { ...expected, dry_run: true },
      { ...expected, dry_run: false },
    ])
    expect(posts()[1]?.headers.get('content-type')).toBe('application/json')
  })

  it('announces the count, closes the confirmation and refreshes the list and depth', async () => {
    stubFetch(api({ bulk: bulk(() => out({ retried: 3, dry_run: false })) }))
    await mount()
    const gets = jobGets().length
    const depthGets = healthGets().length
    await click(bulkBtn() as HTMLElement)
    await click(confirmBtn())
    expect(liveText()).toContain('Retried 3 jobs.')
    expect(liveText()).not.toContain('limit')
    expect(screen.queryByRole('button', { name: 'Confirm retry' })).toBeNull()
    expect(jobGets().length).toBeGreaterThan(gets)
    expect(healthGets().length).toBeGreaterThan(depthGets)
  })

  it('notes when the per-call cap was reached', async () => {
    stubFetch(api({ bulk: bulk(() => out({ retried: 50, has_more: true, dry_run: false })) }))
    await mount()
    await click(bulkBtn() as HTMLElement)
    await click(confirmBtn())
    expect(liveText()).toContain('Retried 50 jobs. The limit of 50 per call was reached and more remain')
  })

  it.each<[string, () => Reply, string]>([
    ['409', () => json({ detail: 'SECRET' }, 409), 'Concurrent change, try again'],
    ['429', () => json({ detail: 'SECRET' }, 429), 'Too many requests. Try again shortly.'],
    ['503', () => json({ detail: 'SECRET' }, 503), 'The database is unavailable. Try again shortly.'],
    ['422', () => json({ detail: 'SECRET' }, 422), 'Retry failed'],
    ['bad shape', () => json({ retried: 'x' }), 'Retry failed'],
    ['network', () => Promise.reject(new TypeError('SECRET boom')) as unknown as Response, 'Retry failed'],
  ])('a failing real retry (%s) shows a fixed message once, never the server text', async (_n, reply, text) => {
    stubFetch(api({ bulk: bulk(reply) }))
    await mount()
    await click(bulkBtn() as HTMLElement)
    await click(confirmBtn())
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    expect(liveText()).toContain(text)
    expect(document.body.textContent).not.toContain('SECRET')
    expect(posts()).toHaveLength(2)
  })

  it('a failing dry run shows a fixed message and opens no confirmation', async () => {
    stubFetch(api({ bulk: () => json({ detail: 'SECRET' }, 503) }))
    await mount()
    await click(bulkBtn() as HTMLElement)
    expect(liveText()).toContain('The database is unavailable. Try again shortly.')
    expect(screen.queryByRole('button', { name: 'Confirm retry' })).toBeNull()
    expect(bulkBtn()).not.toBeNull()
  })

  it('disables Confirm and Cancel in flight and a double click fires one retry', async () => {
    const d = deferred()
    stubFetch(api({ bulk: bulk(() => d.promise) }))
    await mount()
    await click(bulkBtn() as HTMLElement)
    const c = confirmBtn()
    fireEvent.click(c)
    fireEvent.click(c)
    await settle()
    expect(posts()).toHaveLength(2)
    expect(confirmBtn().disabled).toBe(true)
    expect(cancelBtn().disabled).toBe(true)
    await act(async () => {
      d.resolve(json(out({ dry_run: false })))
    })
    await settle()
  })

  it('disables the trigger while the dry run is in flight', async () => {
    const d = deferred()
    stubFetch(api({ bulk: () => d.promise }))
    await mount()
    fireEvent.click(bulkBtn() as HTMLElement)
    fireEvent.click(bulkBtn() as HTMLElement)
    await settle()
    expect(posts()).toHaveLength(1)
    expect(bulkBtn()?.disabled).toBe(true)
    await act(async () => {
      d.resolve(json(out()))
    })
    await settle()
  })
})
describe('cancel and CANCELLED jobs (#301)', () => {
  const REQ_AT = '2026-10-26T10:05:00Z'
  const active = (id: number, over: Partial<JobOut> = {}) =>
    job(id, {
      state: 'running',
      error_class: null,
      last_error: null,
      finished_at: null,
      ...over,
    })
  const cancelledJob = (id: number, over: Partial<JobOut> = {}) =>
    job(id, {
      error_class: 'CANCELLED',
      last_error: 'cancelled on request',
      ...over,
    })
  const cancelBtn = (id: number) =>
    within(rowFor(id)).queryByRole('button', { name: 'Cancel processing' })
  const cancelBody = (outcome: 'cancelled' | 'cancel_requested' | null) => ({
    video_id: 'v',
    jobs: outcome === null ? [] : [{ job_id: 1, kind: 'ingest', outcome }],
  })
  const REQUESTED =
    'Cancel requested: the job stops at its next checkpoint, which can take about a minute'
  const advance = async (ms: number) => {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ms)
    })
    await settle()
  }

  it('lists CANCELLED in the Error class filter and sends it from the URL', async () => {
    stubFetch(api({ jobs: () => list([cancelledJob(1)]) }))
    await mount('/ops?error_class=CANCELLED')
    expect(select('Error class').value).toBe('CANCELLED')
    expect(
      within(select('Error class')).getByRole('option', { name: 'CANCELLED' }),
    ).toBeTruthy()
    expect(lastJobGet().url.searchParams.get('error_class')).toBe('CANCELLED')
  })

  it('a CANCELLED dead row shows the label, the hint and a neutral badge; other dead rows stay red', async () => {
    stubFetch(api({ jobs: () => list([cancelledJob(2), job(1)]) }))
    await mount()
    const row = rowFor(2)
    expect(row.textContent).toContain('CANCELLED')
    expect(row.textContent).toContain('Cancelled on request; retry to resume')
    expect(within(row).getByText('dead').className).toBe('badge')
    expect(within(rowFor(1)).getByText('dead').className).toContain(
      'badge--danger',
    )
  })

  it('a cancelling row says "cancelling" with the request time, never "cancelled"', async () => {
    stubFetch(
      api({ jobs: () => list([active(3, { cancel_requested_at: REQ_AT })]) }),
    )
    await mount('/ops?state=running')
    const row = rowFor(3)
    expect(within(row).getByText('cancelling')).toBeTruthy()
    expect(within(row).queryByText('running')).toBeNull()
    expect(row.textContent).toContain('Cancel requested')
    expect(row.textContent).toContain('2026-10-26 10:05 UTC')
    expect(row.textContent).not.toMatch(/cancelled/i)
    expect(cancelBtn(3)).toBeNull()
  })

  it('running without a request, done with a stale request and pending are not "cancelling"', async () => {
    stubFetch(
      api({
        jobs: () =>
          list([
            active(3, { cancel_requested_at: null }),
            job(2, { state: 'done', cancel_requested_at: REQ_AT }),
            job(1, { state: 'pending', cancel_requested_at: REQ_AT }),
          ]),
      }),
    )
    await mount('/ops?state=running')
    expect(screen.queryByText('cancelling')).toBeNull()
    expect(screen.queryByText(/Cancel requested/)).toBeNull()
    expect(within(rowFor(3)).getByText('running')).toBeTruthy()
    expect(within(rowFor(2)).getByText('done')).toBeTruthy()
  })

  it('offers Cancel processing on pending and running rows without a request only', async () => {
    stubFetch(
      api({
        jobs: () =>
          list([
            job(5, { state: 'pending', error_class: null }),
            active(4),
            active(3, { cancel_requested_at: REQ_AT }),
            job(2, { state: 'done' }),
            cancelledJob(1),
          ]),
      }),
    )
    await mount('/ops?state=running')
    expect(cancelBtn(5)).not.toBeNull()
    expect(cancelBtn(4)).not.toBeNull()
    for (const id of [3, 2, 1]) expect(cancelBtn(id)).toBeNull()
  })

  it('opens an inline confirmation, sends nothing, and moves focus to Confirm cancel', async () => {
    stubFetch(
      api({ jobs: () => list([active(4, { video_title: 'My video' })]) }),
    )
    await mount('/ops?state=running')
    await click(cancelBtn(4) as HTMLElement)
    const row = rowFor(4)
    expect(posts()).toHaveLength(0)
    expect(row.textContent).toContain(
      "Cancel all processing of My video? This also cancels the video's other active jobs. Nothing is deleted.",
    )
    expect(row.textContent).not.toContain('starts over')
    expect(document.activeElement).toBe(
      within(row).getByRole('button', { name: 'Confirm cancel' }),
    )
  })

  it('a transcribe job adds that a transcription starts over if retried', async () => {
    stubFetch(api({ jobs: () => list([active(4, { kind: 'transcribe' })]) }))
    await mount('/ops?state=running')
    await click(cancelBtn(4) as HTMLElement)
    expect(rowFor(4).textContent).toContain(
      'A running transcription starts over if retried.',
    )
  })

  it('Keep processing sends nothing and returns focus to Cancel processing', async () => {
    stubFetch(api({ jobs: () => list([active(4)]) }))
    await mount('/ops?state=running')
    await click(cancelBtn(4) as HTMLElement)
    await click(
      within(rowFor(4)).getByRole('button', { name: 'Keep processing' }),
    )
    expect(posts()).toHaveLength(0)
    expect(
      within(rowFor(4)).queryByRole('button', { name: 'Confirm cancel' }),
    ).toBeNull()
    expect(document.activeElement).toBe(cancelBtn(4))
  })

  it('Confirm sends exactly one JSON POST /videos/{id}/cancel with the id percent-encoded', async () => {
    stubFetch(
      api({
        jobs: () => list([active(4, { video_id: 'a/b?c' })]),
        cancel: () => cancelBody('cancelled'),
      }),
    )
    await mount('/ops?state=running')
    await click(cancelBtn(4) as HTMLElement)
    await click(
      within(rowFor(4)).getByRole('button', { name: 'Confirm cancel' }),
    )
    expect(posts()).toHaveLength(1)
    const p = posts()[0] as Call
    expect(p.url.pathname).toBe('/api/videos/a%2Fb%3Fc/cancel')
    expect(p.url.search).toBe('')
    expect(p.headers.get('content-type')).toBe('application/json')
    expect(p.body).toBe('{}')
  })

  it('a double click sends one request and disables both buttons while it runs', async () => {
    const d = deferred()
    stubFetch(
      api({
        jobs: () => list([active(4), active(3)]),
        cancel: () => d.promise,
      }),
    )
    await mount('/ops?state=running')
    await click(cancelBtn(4) as HTMLElement)
    const confirmBtn = within(rowFor(4)).getByRole('button', {
      name: 'Confirm cancel',
    })
    await user().click(confirmBtn)
    await user().click(confirmBtn)
    await settle()
    expect(posts()).toHaveLength(1)
    expect(confirmBtn).toHaveProperty('disabled', true)
    expect(
      within(rowFor(4)).getByRole('button', { name: 'Keep processing' }),
    ).toHaveProperty('disabled', true)
    expect(cancelBtn(3)).toHaveProperty('disabled', false)
    d.resolve(json(cancelBody('cancelled')))
    await settle()
  })

  it.each([
    [202, cancelBody('cancel_requested'), REQUESTED],
    [200, cancelBody('cancelled'), 'Cancelled'],
    [200, cancelBody(null), 'Nothing to cancel: this video has no active jobs'],
    [404, { detail: 'secret <b>x</b>' }, 'This video no longer exists'],
    [429, { detail: 'x' }, 'Too many requests. Try again shortly.'],
    [503, { detail: 'x' }, 'The database is unavailable. Try again shortly.'],
    [500, { detail: 'secret' }, 'Cancel failed'],
    [422, { detail: 'secret' }, 'Cancel failed'],
  ])(
    'status %i is announced in the row as a fixed message',
    async (status, body, message) => {
      // The list stays the same, so the row is still there and its own live region speaks.
      stubFetch(
        api({
          jobs: () => list([active(4)]),
          cancel: () => json(body, status),
        }),
      )
      await mount('/ops?state=running')
      await click(cancelBtn(4) as HTMLElement)
      await click(
        within(rowFor(4)).getByRole('button', { name: 'Confirm cancel' }),
      )
      expect(
        within(rowFor(4)).getByText(message).getAttribute('aria-live'),
      ).toBe('polite')
      expect(document.body.textContent).not.toContain('secret')
      expect(
        within(rowFor(4)).queryByRole('button', { name: 'Confirm cancel' }),
      ).toBeNull()
    },
  )

  it('a network failure says "Cancel failed" and refetches nothing', async () => {
    stubFetch(
      api({
        jobs: () => list([active(4)]),
        cancel: () => Promise.reject(new TypeError('Failed to fetch')),
      }),
    )
    await mount('/ops?state=running')
    const before = jobGets().length
    await click(cancelBtn(4) as HTMLElement)
    await click(
      within(rowFor(4)).getByRole('button', { name: 'Confirm cancel' }),
    )
    expect(within(rowFor(4)).getByText('Cancel failed')).toBeTruthy()
    expect(jobGets()).toHaveLength(before)
    expect(posts()).toHaveLength(1)
  })

  it('refetches the list and the depth after any response, and does not retry on its own', async () => {
    stubFetch(
      api({
        jobs: () => list([active(4)]),
        cancel: () => json({ detail: 'x' }, 500),
      }),
    )
    await mount('/ops?state=running', { mutations: { retry: 3 } })
    const jobsBefore = jobGets().length
    const depthBefore = healthGets().length
    await click(cancelBtn(4) as HTMLElement)
    await click(
      within(rowFor(4)).getByRole('button', { name: 'Confirm cancel' }),
    )
    await advance(60_000)
    expect(posts()).toHaveLength(1)
    expect(jobGets().length).toBeGreaterThan(jobsBefore)
    expect(healthGets().length).toBeGreaterThan(depthBefore)
  })

  it('a stale list (the job already ended) is the "nothing to cancel" case, not an error', async () => {
    stubFetch(
      api({
        jobs: () => list([active(4)]),
        cancel: () => json(cancelBody(null)),
      }),
    )
    await mount('/ops?state=running')
    await click(cancelBtn(4) as HTMLElement)
    await click(
      within(rowFor(4)).getByRole('button', { name: 'Confirm cancel' }),
    )
    expect(rowFor(4).textContent).toContain('Nothing to cancel')
    expect(document.querySelector('[role="alert"]')).toBeNull()
  })

  it('polls every PROGRESS_POLL_MS while a row is cancelling, shows CANCELLED once it ends, then stops', async () => {
    let reply = list([active(3, { cancel_requested_at: REQ_AT })])
    stubFetch(api({ jobs: () => reply }))
    await mount('/ops?state=running')
    expect(jobGets()).toHaveLength(1)
    await advance(PROGRESS_POLL_MS - 1)
    expect(jobGets()).toHaveLength(1)
    await advance(1)
    expect(jobGets()).toHaveLength(2)
    expect(within(rowFor(3)).getByText('cancelling')).toBeTruthy()

    reply = list([cancelledJob(3)])
    await advance(PROGRESS_POLL_MS)
    expect(jobGets()).toHaveLength(3)
    await advance(1) // let the response render; the next poll is 30 s away
    expect(rowFor(3).textContent).toContain('CANCELLED')
    expect(screen.queryByText('cancelling')).toBeNull()

    await advance(PROGRESS_POLL_MS * 4)
    expect(jobGets()).toHaveLength(3)
  })

  it('does not poll for a running row without a request or a pending row', async () => {
    stubFetch(
      api({
        jobs: () =>
          list([
            active(2),
            job(1, { state: 'pending', cancel_requested_at: REQ_AT }),
          ]),
      }),
    )
    await mount('/ops?state=running')
    await advance(PROGRESS_POLL_MS * 4)
    expect(jobGets()).toHaveLength(1)
  })

  it('cancelling a pending job under state=pending: the row leaves and the live region keeps "Cancelled"', async () => {
    let reply = list([job(4, { state: 'pending', error_class: null })])
    stubFetch(
      api({
        jobs: () => reply,
        cancel: () => {
          reply = list([])
          return json(cancelBody('cancelled'))
        },
      }),
    )
    await mount('/ops?state=pending')
    await click(cancelBtn(4) as HTMLElement)
    await click(
      within(rowFor(4)).getByRole('button', { name: 'Confirm cancel' }),
    )
    expect(screen.queryByRole('region', { name: 'Jobs list' })).toBeNull()
    expect(liveText()).toContain('Cancelled')
  })

  it('a pending job cancelled under state=dead&error_class=CANCELLED shows there after the refetch', async () => {
    let state: 'pending' | 'cancelled' = 'pending'
    stubFetch(
      api({
        jobs: (c) => {
          const filter = c.url.searchParams.get('error_class')
          if (filter === 'CANCELLED')
            return list(state === 'cancelled' ? [cancelledJob(4)] : [])
          return list(
            state === 'pending'
              ? [job(4, { state: 'pending', error_class: null })]
              : [],
          )
        },
        cancel: () => {
          state = 'cancelled'
          return json(cancelBody('cancelled'))
        },
      }),
    )
    await mount('/ops?state=dead&error_class=CANCELLED')
    expect(screen.queryByRole('region', { name: 'Jobs list' })).toBeNull()
    state = 'cancelled'
    await click(screen.getByRole('button', { name: 'Refresh' }))
    expect(rowFor(4).textContent).toContain('CANCELLED')
  })

  it('retries a CANCELLED transcribe job end to end through the existing Retry flow', async () => {
    stubFetch(
      api({
        jobs: () => list([cancelledJob(7, { kind: 'transcribe' })]),
        retry: () => job(7, { state: 'pending' }),
      }),
    )
    await mount('/ops?error_class=CANCELLED')
    await click(within(rowFor(7)).getByRole('button', { name: 'Retry' }))
    expect(rowFor(7).textContent).toMatch(/hours of CPU/)
    await click(
      within(rowFor(7)).getByRole('button', { name: 'Confirm retry' }),
    )
    expect(posts()).toHaveLength(1)
    expect((posts()[0] as Call).url.pathname).toBe('/api/ops/jobs/7/retry')
    expect(rowFor(7).textContent).toContain('Queued again: job #7 is pending')
  })

  it('Retry all matching sends error_class CANCELLED in the dry run and the real call, and names the scope', async () => {
    stubFetch(
      api({
        jobs: () => list([cancelledJob(1)]),
        bulk: (c) =>
          json({
            retried: 1,
            job_ids: [1],
            has_more: false,
            dry_run: JSON.parse(c.body as string).dry_run,
          }),
      }),
    )
    await mount('/ops?state=dead&error_class=CANCELLED')
    await click(screen.getByRole('button', { name: 'Retry all matching' }))
    expect(
      screen.getByText(/dead jobs with error class CANCELLED/),
    ).toBeTruthy()
    await click(screen.getByRole('button', { name: 'Confirm retry' }))
    const bodies = posts().map((c) => JSON.parse(c.body as string))
    expect(bodies).toEqual([
      { state: 'dead', error_class: 'CANCELLED', limit: 50, dry_run: true },
      { state: 'dead', error_class: 'CANCELLED', limit: 50, dry_run: false },
    ])
  })

  it('shows "Could not load jobs" for a malformed cancel_requested_at', async () => {
    stubFetch(
      api({
        jobs: () => ({
          items: [{ ...active(1), cancel_requested_at: 5 }],
          next_before_id: null,
        }),
      }),
    )
    await mount('/ops?state=running')
    expect(screen.getByText(/Could not load jobs/)).toBeTruthy()
  })
})
