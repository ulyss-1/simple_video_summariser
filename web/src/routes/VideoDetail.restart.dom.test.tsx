import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { components } from '../api'
import { FakePlayerProvider } from '../components/FakePlayerProvider'
import { setupUser } from '../test/user'
import { VideoDetail } from './VideoDetail'

type VideoDetailOut = components['schemas']['VideoDetail']
type ActiveJob = NonNullable<VideoDetailOut['active_job']>

const ID = 'dQw4w9WgXcQ'
const FINISHED = '2026-10-27T10:00:00Z'

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

const job = (over: Partial<ActiveJob> = {}): ActiveJob => ({
  id: 7,
  kind: 'transcribe',
  state: 'running',
  cancel_requested_at: null,
  ...over,
})

const cancelled = (kind = 'transcribe') =>
  video({ last_cancelled: { job_id: 7, kind, finished_at: FINISHED } })

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

let posts: { url: URL; body: string }[]
let videoGets: number
let getVideo: () => Response
let postVideos: () => Response | Promise<Response>
let bodies: VideoDetailOut[]

function stubFetch() {
  posts = []
  videoGets = 0
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'http://localhost')
      if ((init?.method ?? 'GET') === 'POST') {
        posts.push({ url, body: String(init?.body) })
        return Promise.resolve(postVideos())
      }
      if (url.pathname.endsWith('/analyses'))
        return Promise.resolve(json({ items: [], total: 0, offset: 0, limit: 1 }))
      if (url.pathname.endsWith(`/videos/${ID}`)) {
        videoGets += 1
        return Promise.resolve(getVideo())
      }
      return Promise.resolve(json({ detail: 'x' }, 404))
    }),
  )
}

/** Series of bodies, one per GET of the video; the last one repeats. */
function script(...list: VideoDetailOut[]) {
  bodies = list
  let i = 0
  getVideo = () => json(bodies[Math.min(i++, bodies.length - 1)])
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
      {
        path: '/videos/:videoId',
        element: (
          <FakePlayerProvider seeks={[]}>
            <VideoDetail />
          </FakePlayerProvider>
        ),
      },
    ],
    { initialEntries: [`/videos/${ID}`] },
  )
  render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  await settle()
  return { user: setupUser() }
}

const openBtn = () => screen.getByRole('button', { name: 'Restart processing' })
const queryOpenBtn = () =>
  screen.queryByRole('button', { name: 'Restart processing' })
const confirmBtn = () =>
  screen.getByRole('button', { name: 'Confirm restart' }) as HTMLButtonElement
const keepBtn = () =>
  screen.getByRole('button', { name: 'Keep cancelled' }) as HTMLButtonElement
const CHANGED =
  'Nothing to restart: the video changed. Showing its current state.'

beforeEach(() => {
  vi.useFakeTimers()
  script(cancelled())
  postVideos = () =>
    json(
      { created: false, restarted: true, video_id: ID, job_id: 7, state: 'pending' },
      202,
    )
  stubFetch()
})

describe('restart control visibility', () => {
  it('is shown next to the cancelled note for an idle cancelled video', async () => {
    await mount()
    expect(openBtn()).toBeTruthy()
    expect(screen.getByText('Processing cancelled (transcribe)')).toBeTruthy()
  })

  it.each([
    ['never processed', video()],
    [
      'pending',
      video({
        status: 'processing',
        active_job: job({ state: 'pending' }),
        last_cancelled: cancelled().last_cancelled,
      }),
    ],
    [
      'running',
      video({ status: 'processing', active_job: job(), last_cancelled: cancelled().last_cancelled }),
    ],
    [
      'cancelling',
      video({
        status: 'processing',
        active_job: job({ cancel_requested_at: FINISHED }),
        last_cancelled: cancelled().last_cancelled,
      }),
    ],
    [
      'done with a cancelled re-analysis',
      video({ status: 'done', analysis: ANALYSIS, last_cancelled: cancelled().last_cancelled }),
    ],
    [
      'failed',
      video({ status: 'failed', last_cancelled: cancelled().last_cancelled }),
    ],
    [
      'unavailable',
      video({ status: 'unavailable', unavailable: '', last_cancelled: cancelled().last_cancelled }),
    ],
  ])('is hidden when the video is %s', async (_n, body) => {
    script(body)
    await mount()
    expect(queryOpenBtn()).toBeNull()
  })
})

describe('restart confirmation', () => {
  it('names the video, warns about transcription and focuses Confirm restart', async () => {
    const { user } = await mount()
    await user.click(openBtn())
    expect(
      screen.getByText(
        'Restart processing of My Video? It is queued again at the front of the queue.',
      ),
    ).toBeTruthy()
    expect(screen.getByText('A transcription starts over.')).toBeTruthy()
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(confirmBtn())
    expect(posts).toHaveLength(0)
  })

  it('omits the transcription sentence for another kind', async () => {
    script(cancelled('analyze'))
    const { user } = await mount()
    await user.click(openBtn())
    expect(screen.queryByText(/starts over/)).toBeNull()
  })

  it('returns focus to Restart processing on Keep cancelled and on Escape', async () => {
    const { user } = await mount()
    await user.click(openBtn())
    await user.click(keepBtn())
    expect(document.activeElement).toBe(openBtn())
    await user.click(openBtn())
    await user.keyboard('{Escape}')
    expect(document.activeElement).toBe(openBtn())
    expect(posts).toHaveLength(0)
  })
})

describe('restart request and outcomes', () => {
  it('sends one POST /videos with the watch URL, even on a double click', async () => {
    let release: (r: Response) => void = () => {}
    postVideos = () => new Promise<Response>((r) => (release = r))
    const { user } = await mount()
    await user.click(openBtn())
    const btn = confirmBtn()
    await user.click(btn)
    await user.click(btn)
    expect(btn.disabled).toBe(true)
    expect(keepBtn().disabled).toBe(true)
    expect(posts).toHaveLength(1)
    expect(posts[0].url.pathname).toBe('/api/videos')
    expect(JSON.parse(posts[0].body)).toEqual({
      url: `https://www.youtube.com/watch?v=${ID}`,
    })
    await act(async () => {
      release(json({ created: false, restarted: true, video_id: ID, job_id: 7, state: 'pending' }, 202))
      await vi.advanceTimersByTimeAsync(0)
    })
  })

  it('shows the queued status and focuses it after 202 restarted', async () => {
    script(
      cancelled(),
      video({ status: 'processing', active_job: job({ state: 'pending' }) }),
    )
    const { user } = await mount()
    await user.click(openBtn())
    await user.click(confirmBtn())
    await settle()
    const status = screen.getByRole('status')
    expect(status.textContent).toBe('Queued: Transcribing')
    expect(document.activeElement).toBe(status)
    expect(queryOpenBtn()).toBeNull()
    expect(screen.queryByText(/Processing cancelled/)).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('on 200 not restarted: alerts, refetches and follows the refetched data', async () => {
    postVideos = () =>
      json({ created: false, restarted: false, video_id: ID, job_id: 7, state: 'done' })
    script(cancelled(), video({ status: 'done', analysis: ANALYSIS }))
    const { user } = await mount()
    const before = videoGets
    await user.click(openBtn())
    await user.click(confirmBtn())
    await settle()
    expect(screen.getByRole('alert').textContent).toBe(CHANGED)
    expect(videoGets).toBeGreaterThan(before)
    expect(queryOpenBtn()).toBeNull()
  })

  it('on 200 not restarted with the video still cancelled, the button stays usable', async () => {
    postVideos = () =>
      json({ created: false, restarted: false, video_id: ID, job_id: 7, state: 'x' })
    const { user } = await mount()
    await user.click(openBtn())
    await user.click(confirmBtn())
    await settle()
    expect(screen.getByRole('alert').textContent).toBe(CHANGED)
    expect(openBtn()).toBeTruthy()
  })

  it.each([
    ['404', 404, 'This video no longer exists.'],
    ['503', 503, 'The database is unavailable. Try again shortly.'],
    [
      '500',
      500,
      'Could not restart processing. Check your connection and try again.',
    ],
  ])('on %s: a fixed alert, closed confirmation, usable button', async (_n, status, text) => {
    postVideos = () => json({ detail: 'SECRET server message' }, status)
    const { user } = await mount()
    await user.click(openBtn())
    await user.click(confirmBtn())
    await settle()
    expect(screen.getByRole('alert').textContent).toBe(text)
    expect(screen.queryByText(/SECRET/)).toBeNull()
    expect(screen.queryByRole('button', { name: 'Confirm restart' })).toBeNull()
    expect(openBtn()).toBeTruthy()
    expect(posts).toHaveLength(1)
  })
})
