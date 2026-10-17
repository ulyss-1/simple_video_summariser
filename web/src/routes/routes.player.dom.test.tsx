// #181: seeking through the SHARED `routes` table with the REAL PlayerProvider
// and Player (no FakePlayerProvider, no test-supplied provider). The view tests
// use a local router with a fake provider, so only this file proves the
// registered routes mount a provider above every route that renders timestamps.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { routes } from './index'

const ID = 'dQw4w9WgXcQ'
const ORIGIN = 'https://www.youtube-nocookie.com'

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

const analysis = {
  id: 1,
  transcript_id: 1,
  transcript_source: 'captions',
  chunk_strategy: 'fixed',
  model: 'm',
  prompt_version: 'v1',
  created_at: '2026-10-01T08:09:10Z',
  input_tokens: 1,
  output_tokens: 1,
  cost_usd: null,
  duration_ms: null,
  tldr: 'Summary',
  speaker_roster: null,
  topics: [],
  claims: [{ text: 'Water is wet', speaker: 'Ada', confidence: 'high', start_sec: 75, source_chunk_seq: 0 }],
  quotes: [],
}

const video = {
  video_id: ID,
  title: 'My Video',
  channel_id: null,
  channel_title: null,
  published_at: null,
  duration_sec: 3723,
  latest_analysis_at: null,
  origin: 'manual',
  unavailable: null,
  status: 'done',
  active_job: null,
  last_failure: null,
  transcript: null,
  analysis,
}

const transcript = {
  video_id: ID,
  transcript_id: 7,
  source: 'youtube_manual',
  language: 'en',
  speaker_source: 'none',
  total_segments: 1,
  offset: 0,
  limit: 200,
  segments: [{ index: 0, start: 75, end: 79, text: 'Segment 0', speaker: null }],
}

let posts: Array<{ message: Record<string, unknown>; targetOrigin: string }>

beforeEach(() => {
  vi.useFakeTimers()
  posts = []
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = new URL(String(input), 'http://localhost')
      if (url.pathname.endsWith('/analyses')) return Promise.resolve(json({ items: [], limit: 50, offset: 0, total: 0 }))
      if (url.pathname.endsWith('/transcript')) return Promise.resolve(json(transcript))
      return Promise.resolve(json(video))
    }),
  )
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

async function mountRoute(entry: string) {
  const router = createMemoryRouter(routes, { initialEntries: [entry] })
  const { container } = render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
  return container
}

function readyEmbed(container: HTMLElement): void {
  const iframe = container.querySelector('iframe')
  if (!iframe?.contentWindow) throw new Error('no iframe rendered')
  const target = iframe.contentWindow
  vi.spyOn(target, 'postMessage').mockImplementation(((message: string, targetOrigin: string) => {
    posts.push({ message: JSON.parse(message), targetOrigin })
  }) as Window['postMessage'])
  fireEvent.load(iframe)
  act(() => {
    window.dispatchEvent(
      new MessageEvent('message', { data: JSON.stringify({ event: 'onReady' }), origin: ORIGIN, source: target }),
    )
  })
}

const seekCommands = () => posts.filter((p) => p.message['event'] === 'command' && p.message['func'] === 'seekTo')

describe.each([
  ['video detail', `/videos/${ID}`],
  ['transcript', `/videos/${ID}/transcript`],
])('%s route in the shared table', (_name, entry) => {
  it('renders timestamps as seek buttons and clicking one seeks the embed', async () => {
    const container = await mountRoute(entry)
    const button = screen.getByRole('button', { name: 'Seek to 1:15' })
    readyEmbed(container)
    fireEvent.click(button)
    expect(seekCommands().map((c) => c.message['args'])).toEqual([[75, true]])
    expect(seekCommands()[0]?.targetOrigin).toBe(ORIGIN)
  })
})

// Compare's own `real route table` test (Compare.dom.test.tsx) covers its
// provider; this guards the other two timestamp-bearing routes: a route that
// loses its provider degrades every timestamp to a bare <time>.
describe('provider presence guard', () => {
  it.each([
    ['video detail', `/videos/${ID}`],
    ['transcript', `/videos/${ID}/transcript`],
  ])('%s route has no timestamp outside a seek button', async (_name, entry) => {
    const container = await mountRoute(entry)
    // Other <time> elements (dates, duration) are legitimately plain; the
    // timestamp at 1:15 is the one that must be a seek button.
    expect(container.querySelectorAll('button.timestamp').length).toBeGreaterThan(0)
    expect(screen.getByRole('button', { name: 'Seek to 1:15' })).toBeTruthy()
  })
})
