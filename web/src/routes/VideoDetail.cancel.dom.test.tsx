import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { components } from '../api'
import { FakePlayerProvider } from '../components/FakePlayerProvider'
import { POLL_INTERVAL_MS } from '../hooks/useVideo'
import { setupUser } from '../test/user'
import { Library } from './Library'
import { VideoDetail } from './VideoDetail'

type VideoDetailOut = components['schemas']['VideoDetail']
type ActiveJob = NonNullable<VideoDetailOut['active_job']>

const ID = 'dQw4w9WgXcQ'

function video(over: Partial<VideoDetailOut> = {}): VideoDetailOut {
  return {
    video_id: ID,
    title: 'My Video',
    channel_id: 'UC' + 'a'.repeat(22),
    channel_title: 'My Channel',
    published_at: '2026-09-30T12:00:00Z',
    duration_sec: 60,
    latest_analysis_at: null,
    has_transcript: false,
    origin: 'manual',
    unavailable: null,
    status: 'idle',
    active_job: null,
    last_failure: null,
    last_cancelled: null,
    transcript: null,
    analysis: null,
    progress: null,
    ...over,
  }
}

function job(over: Partial<ActiveJob> = {}): ActiveJob {
  return {
    id: 7,
    kind: 'transcribe',
    state: 'running',
    cancel_requested_at: null,
    ...over,
  }
}

const running = (over: Partial<ActiveJob> = {}) =>
  video({ status: 'processing', active_job: job(over) })
const REQUESTED = '2026-10-27T10:00:00Z'
const cancelled = (kind = 'transcribe') =>
  video({ last_cancelled: { job_id: 7, kind, finished_at: REQUESTED } })

const ANALYSIS = {
  id: 1,
  tldr: 'Short summary',
  topics: [],
  claims: [],
  quotes: [],
} as unknown as NonNullable<VideoDetailOut['analysis']>

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

let gets: URL[]
let posts: { url: URL; body: string }[]
let deletes: URL[]
/** Answers each GET of the video. */
let getVideo: () => Response | Promise<Response>
let postCancel: () => Response | Promise<Response>
let deleteVideo: () => Response | Promise<Response>

function stubFetch() {
  gets = []
  posts = []
  deletes = []
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'http://localhost')
      const method = init?.method ?? 'GET'
      if (method === 'POST') {
        posts.push({ url, body: String(init?.body) })
        return Promise.resolve(postCancel())
      }
      if (method === 'DELETE') {
        deletes.push(url)
        return Promise.resolve(deleteVideo())
      }
      gets.push(url)
      if (url.pathname.endsWith('/analyses'))
        return Promise.resolve(
          json({ items: [], total: 0, offset: 0, limit: 1 }),
        )
      if (url.pathname.endsWith(`/videos/${ID}`))
        return Promise.resolve(getVideo())
      if (url.pathname.endsWith('/videos'))
        return Promise.resolve(
          json({ items: [], total: 0, offset: 0, limit: 50 }),
        )
      return Promise.resolve(json({ detail: 'x' }, 404))
    }),
  )
}

/** Series of bodies, one per GET of the video; the last one repeats. */
function script(...bodies: VideoDetailOut[]) {
  let i = 0
  getVideo = () => json(bodies[Math.min(i++, bodies.length - 1)])
}

const videoGets = () =>
  gets.filter((u) => u.pathname.endsWith(`/videos/${ID}`)).length

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

async function settle() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
}

async function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  const router = createMemoryRouter(
    [
      { path: '/', element: <Library /> },
      {
        path: '/videos/:videoId',
        element: (
          <FakePlayerProvider seeks={[]}>
            <VideoDetail />
          </FakePlayerProvider>
        ),
      },
    ],
    { initialEntries: ['/', `/videos/${ID}`], initialIndex: 1 },
  )
  render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  await settle()
  return { client, router, user: setupUser() }
}

const cancelBtn = () =>
  screen.getByRole('button', { name: 'Cancel processing' })
const queryCancelBtn = () =>
  screen.queryByRole('button', { name: 'Cancel processing' })
const confirmBtn = () =>
  screen.getByRole('button', { name: 'Confirm cancel' }) as HTMLButtonElement
const keepBtn = () =>
  screen.getByRole('button', { name: 'Keep processing' }) as HTMLButtonElement
const HINT =
  'This usually takes up to a minute: the worker learns of the request on its next heartbeat, then stops its download or model call.'

beforeEach(() => {
  vi.useFakeTimers()
  script(running())
  postCancel = () => json({ jobs: [] })
  deleteVideo = () => new Response(null, { status: 204 })
  stubFetch()
})

describe('cancel control visibility', () => {
  it.each([
    [
      'pending',
      video({
        status: 'processing',
        active_job: job({ state: 'pending', kind: 'ingest' }),
      }),
    ],
    ['running', running()],
    [
      'running with an analysis',
      video({
        status: 'done',
        active_job: job({ kind: 'analyze' }),
        analysis: ANALYSIS,
      }),
    ],
  ])('is shown next to Delete video when a job is %s', async (_n, body) => {
    script(body)
    await mount()
    expect(cancelBtn()).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Delete video' })).toBeTruthy()
  })

  it.each([
    ['no job', video()],
    ['no job, cancelled', cancelled()],
    ['a cancel request', running({ cancel_requested_at: REQUESTED })],
  ])('is hidden with %s', async (_n, body) => {
    script(body)
    await mount()
    expect(queryCancelBtn()).toBeNull()
    expect(screen.getByRole('button', { name: 'Delete video' })).toBeTruthy()
  })
})

describe('cancel confirmation', () => {
  it('names the video, warns about transcription and moves focus to Keep processing', async () => {
    const { user } = await mount()
    await user.click(cancelBtn())
    expect(
      screen.getByText('Cancel processing of My Video? Nothing is deleted.'),
    ).toBeTruthy()
    expect(
      screen.getByText('A running transcription starts over if it is resumed.'),
    ).toBeTruthy()
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(keepBtn())
    expect(posts).toHaveLength(0)
  })

  it('omits the transcription sentence for another kind', async () => {
    script(running({ kind: 'analyze' }))
    const { user } = await mount()
    await user.click(cancelBtn())
    expect(screen.queryByText(/starts over/)).toBeNull()
  })

  it.each([
    ['null', null],
    ['empty', ''],
  ])('uses the video id when the title is %s', async (_n, title) => {
    script(video({ ...running(), title }))
    const { user } = await mount()
    await user.click(cancelBtn())
    expect(
      screen.getByText(`Cancel processing of ${ID}? Nothing is deleted.`),
    ).toBeTruthy()
  })

  it('renders a hostile title as text', async () => {
    script(video({ ...running(), title: '<img src=x onerror=alert(1)>' }))
    const { user } = await mount()
    await user.click(cancelBtn())
    expect(
      screen.getByText(
        'Cancel processing of <img src=x onerror=alert(1)>? Nothing is deleted.',
      ),
    ).toBeTruthy()
    expect(document.querySelector('img')).toBeNull()
  })

  it('Keep processing closes it, restores focus and sends nothing', async () => {
    const { user } = await mount()
    await user.click(cancelBtn())
    await user.click(keepBtn())
    expect(screen.queryByRole('button', { name: 'Confirm cancel' })).toBeNull()
    expect(document.activeElement).toBe(cancelBtn())
    expect(posts).toHaveLength(0)
  })

  it('Escape closes it, restores focus and sends nothing', async () => {
    const { user } = await mount()
    await user.click(cancelBtn())
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('button', { name: 'Confirm cancel' })).toBeNull()
    expect(document.activeElement).toBe(cancelBtn())
    expect(posts).toHaveLength(0)
  })
})

describe('cancel request', () => {
  it('sends one POST with an empty body even when activated twice, disabling both buttons', async () => {
    let release: (r: Response) => void = () => {}
    postCancel = () => new Promise<Response>((r) => (release = r))
    const { user } = await mount()
    await user.click(cancelBtn())
    await user.click(confirmBtn())
    await user.click(confirmBtn())
    expect(posts).toHaveLength(1)
    expect(posts[0]?.url.pathname.endsWith(`/videos/${ID}/cancel`)).toBe(true)
    expect(posts[0]?.body).toBe('{}')
    expect(confirmBtn().disabled).toBe(true)
    expect(keepBtn().disabled).toBe(true)
    await act(async () => release(json({ jobs: [] })))
    await settle()
  })

  it('on 202 closes, refetches at once and focuses the Cancelling status line', async () => {
    script(running(), running({ cancel_requested_at: REQUESTED }))
    postCancel = () =>
      json(
        {
          jobs: [
            { job_id: 7, kind: 'transcribe', outcome: 'cancel_requested' },
          ],
        },
        202,
      )
    const { user } = await mount()
    expect(videoGets()).toBe(1)
    await user.click(cancelBtn())
    await user.click(confirmBtn())
    await settle()
    expect(videoGets()).toBe(2)
    expect(screen.queryByRole('button', { name: 'Confirm cancel' })).toBeNull()
    expect(queryCancelBtn()).toBeNull()
    const status = screen.getByRole('status')
    expect(status.textContent).toBe('Cancelling: Transcribing…')
    expect(status.tabIndex).toBe(-1)
    expect(document.activeElement).toBe(status)
  })

  it('on 200 with every job cancelled refetches and reads Processing cancelled', async () => {
    script(running(), cancelled())
    postCancel = () =>
      json({ jobs: [{ job_id: 7, kind: 'transcribe', outcome: 'cancelled' }] })
    const { user } = await mount()
    await user.click(cancelBtn())
    await user.click(confirmBtn())
    await settle()
    expect(videoGets()).toBe(2)
    expect(screen.getByRole('status').textContent).toBe(
      'Processing cancelled (transcribe)',
    )
    expect(document.activeElement).toBe(screen.getByRole('status'))
  })

  it('on 200 with no jobs is a success and shows whatever the state now is', async () => {
    script(running(), video({ status: 'done', analysis: ANALYSIS }))
    postCancel = () => json({ jobs: [] })
    const { user } = await mount()
    await user.click(cancelBtn())
    await user.click(confirmBtn())
    await settle()
    expect(videoGets()).toBe(2)
    expect(screen.queryByRole('alert')).toBeNull()
    expect(queryCancelBtn()).toBeNull()
  })

  it('on 404 refetches and shows the not-found page', async () => {
    let n = 0
    getVideo = () =>
      n++ === 0 ? json(running()) : json({ detail: 'gone' }, 404)
    postCancel = () => json({ detail: 'gone' }, 404)
    const { user } = await mount()
    await user.click(cancelBtn())
    await user.click(confirmBtn())
    await settle()
    expect(
      screen.getByRole('heading', { name: 'Video not found' }),
    ).toBeTruthy()
  })

  it.each([
    [429, 'Too many requests. Try again shortly.'],
    [503, 'The database is unavailable. Try again shortly.'],
    [500, 'Could not cancel processing. Check your connection and try again.'],
  ])(
    'on %s keeps the confirmation open with a fixed alert',
    async (status, message) => {
      postCancel = () =>
        json({ detail: 'SECRET raw text', video_id: 'zzzzzzzzzzz' }, status)
      const { user } = await mount()
      await user.click(cancelBtn())
      await user.click(confirmBtn())
      await settle()
      expect(screen.getByRole('alert').textContent).toBe(message)
      expect(document.body.textContent).not.toContain('SECRET')
      expect(document.body.textContent).not.toContain('zzzzzzzzzzz')
      expect(confirmBtn().disabled).toBe(false)
      expect(videoGets()).toBe(1)
      await user.click(confirmBtn())
      expect(posts).toHaveLength(2)
    },
  )

  it('on a network failure keeps the confirmation open with a fixed alert', async () => {
    postCancel = () => {
      throw new TypeError('Failed to fetch: secret-host')
    }
    const { user } = await mount()
    await user.click(cancelBtn())
    await user.click(confirmBtn())
    await settle()
    expect(screen.getByRole('alert').textContent).toBe(
      'Could not cancel processing. Check your connection and try again.',
    )
    expect(document.body.textContent).not.toContain('secret-host')
  })

  it('closing the confirmation clears a previous error', async () => {
    postCancel = () => json({}, 503)
    const { user } = await mount()
    await user.click(cancelBtn())
    await user.click(confirmBtn())
    await settle()
    await user.click(keepBtn())
    await user.click(cancelBtn())
    expect(screen.queryByRole('alert')).toBeNull()
  })
})

describe('Cancelling state', () => {
  it('reads Cancelling with the heartbeat hint, never Cancelled, and keeps Delete only', async () => {
    script(running({ cancel_requested_at: REQUESTED }))
    await mount()
    expect(screen.getByRole('status').textContent).toBe(
      'Cancelling: Transcribing…',
    )
    expect(screen.getByText(HINT)).toBeTruthy()
    expect(document.body.textContent).not.toContain('Cancelled')
    expect(queryCancelBtn()).toBeNull()
    expect(screen.getByRole('button', { name: 'Delete video' })).toBeTruthy()
  })

  it('says only Cancelling… for an unknown kind', async () => {
    script(running({ kind: 'zzz', cancel_requested_at: REQUESTED }))
    await mount()
    expect(screen.getByRole('status').textContent).toBe('Cancelling…')
  })

  it('turns the re-analysis alert into Cancelling re-analysis… and keeps Re-analyse disabled', async () => {
    script(
      video({
        status: 'done',
        active_job: job({ kind: 'analyze', cancel_requested_at: REQUESTED }),
        analysis: ANALYSIS,
        has_transcript: true,
        transcript: {
          id: 1,
          source: 'captions',
          language: 'en',
          speaker_source: 'none',
          segment_count: 1,
        },
      }),
    )
    await mount()
    expect(screen.getByText('Cancelling re-analysis…')).toBeTruthy()
    expect(screen.queryByText('Re-analysis in progress')).toBeNull()
    expect(
      (screen.getByRole('button', { name: 'Re-analyse' }) as HTMLButtonElement)
        .disabled,
    ).toBe(true)
    expect(screen.getByText(HINT)).toBeTruthy()
  })
})

describe('Cancelled state', () => {
  it('reads Processing cancelled (kind), not failed or not analysed', async () => {
    script(cancelled('ingest'))
    await mount()
    expect(screen.getByRole('status').textContent).toBe(
      'Processing cancelled (ingest)',
    )
    expect(document.body.textContent).not.toContain('Processing failed')
    expect(document.body.textContent).not.toContain('Not analysed yet')
  })

  it('adds an alert above an existing analysis and keeps Re-analyse enabled', async () => {
    script(
      video({
        status: 'done',
        analysis: ANALYSIS,
        has_transcript: true,
        transcript: {
          id: 1,
          source: 'captions',
          language: 'en',
          speaker_source: 'none',
          segment_count: 1,
        },
        last_cancelled: { job_id: 7, kind: 'analyze', finished_at: REQUESTED },
      }),
    )
    await mount()
    expect(
      screen.getByText('Processing cancelled (analyze)').className,
    ).toContain('alert')
    expect(
      (screen.getByRole('button', { name: 'Re-analyse' }) as HTMLButtonElement)
        .disabled,
    ).toBe(false)
  })

  it('labels a failed video exactly as before', async () => {
    script(
      video({
        status: 'failed',
        last_failure: {
          job_id: 3,
          kind: 'ingest',
          error_class: 'BUG',
          finished_at: REQUESTED,
        },
      }),
    )
    await mount()
    expect(screen.getByRole('status').textContent).toBe(
      'Processing failed (ingest, BUG)',
    )
  })

  it('shows Queued: through the active job once the cancelled job is revived', async () => {
    script(video({ status: 'processing', active_job: job({ state: 'pending' }) }))
    await mount()
    expect(screen.getByRole('status').textContent).toBe('Queued: Transcribing')
  })
})

describe('forced orderings', () => {
  it('(a) a stale poll resolved after the cancel never reverts the page', async () => {
    const stale: { release: (r: Response) => void }[] = []
    let n = 0
    getVideo = () => {
      n++
      if (n === 1) return json(running())
      if (n === 2)
        return new Promise<Response>((r) => stale.push({ release: r })); // the poll
      return json(running({ cancel_requested_at: REQUESTED })); // the refetch after the cancel
    }
    postCancel = () =>
      json(
        {
          jobs: [
            { job_id: 7, kind: 'transcribe', outcome: 'cancel_requested' },
          ],
        },
        202,
      )
    const { user } = await mount()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS)
    })
    expect(stale).toHaveLength(1)
    await user.click(cancelBtn())
    await user.click(confirmBtn())
    await settle()
    expect(screen.getByRole('status').textContent).toBe(
      'Cancelling: Transcribing…',
    )
    await act(async () => stale[0]?.release(json(running())))
    await settle()
    expect(screen.getByRole('status').textContent).toBe(
      'Cancelling: Transcribing…',
    )
    expect(screen.queryByText('Transcribing')).toBeNull()
  })

  it('(b) polling shows Cancelling before Processing cancelled and never the word Cancelled early', async () => {
    script(running(), running({ cancel_requested_at: REQUESTED }), cancelled())
    await mount()
    expect(screen.getByRole('status').textContent).toBe('Transcribing')
    expect(document.body.textContent).not.toContain('Cancelled')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS)
    })
    await settle()
    await advance(1)
    expect(screen.getByRole('status').textContent).toBe(
      'Cancelling: Transcribing…',
    )
    expect(document.body.textContent).not.toContain('Cancelled')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS)
    })
    await settle()
    expect(screen.getByRole('status').textContent).toBe(
      'Processing cancelled (transcribe)',
    )
    const before = videoGets()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 2)
    })
    expect(videoGets()).toBe(before)
  })

  it('(c) deleting a running video: a poll resolved afterwards neither renders it nor refetches', async () => {
    const stale: { release: (r: Response) => void }[] = []
    let n = 0
    getVideo = () => {
      n++
      if (n === 1) return json(running())
      return new Promise<Response>((r) => stale.push({ release: r }))
    }
    const { user, router, client } = await mount()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS)
    })
    expect(stale).toHaveLength(1)
    await user.click(screen.getByRole('button', { name: 'Delete video' }))
    expect(
      screen.getByText(
        'Processing is stopped and any running work is discarded.',
      ),
    ).toBeTruthy()
    await user.click(
      screen.getAllByRole('button', { name: 'Delete video' })[0] as HTMLElement,
    )
    await settle()
    expect(deletes).toHaveLength(1)
    expect(router.state.location.pathname).toBe('/')
    expect(screen.getByText('Video deleted')).toBeTruthy()
    const before = videoGets()
    await act(async () => stale[0]?.release(json(running())))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 2)
    })
    expect(videoGets()).toBe(before)
    expect(screen.queryByText('My Video')).toBeNull()
    expect(client.getQueryData(['video', ID])).toBeUndefined()
  })
})
