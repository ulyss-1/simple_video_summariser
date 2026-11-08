import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { components } from '../api'
import { FakePlayerProvider } from '../components/FakePlayerProvider'
import { VideoDetail } from './VideoDetail'

type VideoDetailOut = components['schemas']['VideoDetail']
type AnalysisOut = components['schemas']['AnalysisOut']

const ID = 'dQw4w9WgXcQ'

function analysis(over: Partial<AnalysisOut> = {}): AnalysisOut {
  return {
    id: 1,
    transcript_id: 1,
    transcript_source: 'captions',
    chunk_strategy: 'fixed',
    model: 'test-model-1',
    prompt_version: 'v7',
    created_at: '2026-10-01T08:09:10Z',
    input_tokens: 111111,
    output_tokens: 222222,
    cache_read_tokens: null,
    cache_write_tokens: null,
    calls: null,
    cost_usd: 3.5,
    duration_ms: 987654,
    tldr: 'A short summary.',
    speaker_roster: { speakers: [{ name: 'Ada', role: 'host' }] },
    topics: [{ seq: 0, title: 'Intro', summary: 'Opening remarks', start_sec: 5 }],
    claims: [{ text: 'Water is wet', speaker: 'Ada', confidence: 'high', start_sec: 65, source_chunk_seq: 0 }],
    quotes: [{ text: 'Hello there', speaker: 'Ada', start_sec: 10, source_chunk_seq: 0 }],
    ...over,
  }
}

function video(over: Partial<VideoDetailOut> = {}): VideoDetailOut {
  return {
    video_id: ID,
    title: 'My Video',
    channel_id: 'UC' + 'a'.repeat(22),
    channel_title: 'My Channel',
    published_at: '2026-09-30T23:30:00-07:00',
    duration_sec: 3723,
    latest_analysis_at: null,
    has_transcript: false,
    origin: 'manual',
    unavailable: null,
    status: 'done',
    active_job: null,
    last_failure: null,
    last_cancelled: null,
    transcript: null,
    analysis: analysis(),
    progress: null,
    ...over,
  }
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

let requests: URL[]
let seeks: number[]
type Responder = (url: URL, n: number) => Response | Promise<Response>

function stubFetch(respond: Responder) {
  requests = []
  const fn = vi.fn((input: RequestInfo | URL) => {
    const url = new URL(String(input), 'http://localhost')
    // The "Compare analyses" count request (#53) is covered in
    // VideoDetail.compareLink.dom.test.tsx; here it only has to fail quietly.
    if (url.pathname.endsWith('/analyses')) return Promise.resolve(json({ detail: 'not under test' }, 404))
    // The cost estimate (#153) is covered in VideoDetail.estimate.dom.test.tsx; here it
    // only has to answer, and not count as a request of the page under test.
    if (url.pathname.endsWith('/analysis-estimate')) {
      return Promise.resolve(
        json({ video_id: ID, summarizer: 'ollama', transcript_chars: null, batch: true, estimates: [] }),
      )
    }
    requests.push(url)
    return Promise.resolve(respond(url, requests.length))
  })
  vi.stubGlobal('fetch', fn)
  return fn
}

async function settle() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
}

async function mount(entry = `/videos/${ID}`) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const router = createMemoryRouter(
    [
      { path: '/', element: <p>Library page</p> },
      {
        path: '/videos/:videoId',
        element: (
          <FakePlayerProvider seeks={seeks}>
            <VideoDetail />
          </FakePlayerProvider>
        ),
      },
    ],
    { initialEntries: [entry] },
  )
  render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  await settle()
  return router
}

async function mountWith(v: VideoDetailOut) {
  stubFetch(() => json(v))
  return mount(`/videos/${v.video_id}`)
}

beforeEach(() => {
  vi.useFakeTimers()
  seeks = []
})

describe('Transcribe with Whisper (#415)', () => {
  const auto = { id: 3, source: 'youtube_auto', language: 'en', segment_count: 10, speaker_source: 'none' }
  const open = () => screen.getByRole('button', { name: 'Transcribe with Whisper' })
  const posts = () => requests.filter((u) => u.pathname.endsWith('/retranscribe'))

  async function mountPosting(v: VideoDetailOut, post: () => Response) {
    stubFetch((url) => (url.pathname.endsWith('/retranscribe') ? post() : json(v)))
    await mount()
  }

  it.each([
    ['youtube_manual', 'YouTube captions (manual)'],
    ['whisper', 'Whisper'],
  ])('shows the %s source without the button', async (source, label) => {
    await mountWith(video({ transcript: { ...auto, source } }))
    expect(document.body.textContent).toContain(`Transcript source: ${label}`)
    expect(screen.queryByRole('button', { name: 'Transcribe with Whisper' })).toBeNull()
  })

  it('shows the auto-caption source and the button', async () => {
    await mountWith(video({ transcript: auto }))
    expect(document.body.textContent).toContain('Transcript source: YouTube auto-generated captions')
    expect((open() as HTMLButtonElement).disabled).toBe(false)
  })

  it('is disabled while a job is active', async () => {
    await mountWith(video({ transcript: auto, active_job: { id: 2, kind: 'analyze', state: 'running' } }))
    expect((open() as HTMLButtonElement).disabled).toBe(true)
  })

  it('asks inline first, and Cancel sends nothing and returns focus', async () => {
    await mountPosting(video({ transcript: auto }), () => json({}, 202))
    fireEvent.click(open())
    await settle()
    expect(document.body.textContent).toContain('hours of CPU')
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Confirm transcription' }))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await settle()
    expect(posts()).toHaveLength(0)
    expect(document.activeElement).toBe(open())
  })

  it('Escape closes the confirmation', async () => {
    await mountWith(video({ transcript: auto }))
    fireEvent.click(open())
    await settle()
    fireEvent.keyDown(screen.getByRole('button', { name: 'Confirm transcription' }), { key: 'Escape' })
    await settle()
    expect(screen.queryByRole('button', { name: 'Confirm transcription' })).toBeNull()
  })

  it('confirming POSTs once to the retranscribe endpoint and closes the confirmation', async () => {
    await mountPosting(
      video({ transcript: auto }),
      () => json({ video_id: ID, job_id: 9, state: 'pending', created: true }, 202),
    )
    fireEvent.click(open())
    await settle()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm transcription' }))
    await settle()
    expect(posts().map((u) => u.pathname)).toEqual([`/api/videos/${ID}/retranscribe`])
    expect(screen.queryByRole('button', { name: 'Confirm transcription' })).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it.each([
    [409, 'This video cannot be transcribed with Whisper now. Showing its current state.'],
    [500, 'Could not start the Whisper transcription. Try again.'],
  ])('shows a fixed alert for %i and never echoes the body', async (status, message) => {
    await mountPosting(video({ transcript: auto }), () => json({ detail: 'SECRET' }, status))
    fireEvent.click(open())
    await settle()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm transcription' }))
    await settle()
    expect(screen.getByRole('alert').textContent).toBe(message)
    expect(document.body.textContent).not.toContain('SECRET')
    expect(posts()).toHaveLength(1)
  })
})
