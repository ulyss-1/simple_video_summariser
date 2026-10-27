import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { components } from '../api'
import { FakePlayerProvider } from '../components/FakePlayerProvider'
import { Library } from './Library'
import { VideoDetail } from './VideoDetail'

type VideoDetailOut = components['schemas']['VideoDetail']

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
    status: 'done',
    active_job: null,
    last_failure: null,
    last_cancelled: null,
    transcript: null,
    analysis: null,
    progress: null,
    ...over,
  }
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

const CONFIRM_TEXT = 'The transcript, summaries and stored audio will be permanently removed.'

let deletes: { url: URL; method: string }[]
let gets: URL[]
let deleteResponder: () => Response | Promise<Response>
let detail: VideoDetailOut

function stubFetch() {
  deletes = []
  gets = []
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'http://localhost')
      const method = init?.method ?? 'GET'
      if (method === 'DELETE') {
        deletes.push({ url, method })
        return Promise.resolve(deleteResponder())
      }
      gets.push(url)
      if (url.pathname.endsWith('/analyses')) return Promise.resolve(json({ items: [], total: 0, offset: 0, limit: 1 }))
      if (url.pathname.endsWith(`/videos/${ID}`)) return Promise.resolve(json(detail))
      if (url.pathname.endsWith('/videos')) return Promise.resolve(json({ items: [], total: 0, offset: 0, limit: 50 }))
      return Promise.resolve(json({ detail: 'x' }, 404))
    }),
  )
}

async function settle() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
}

async function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
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
  return { client, router }
}

const open = () => fireEvent.click(screen.getByRole('button', { name: 'Delete video' }))
const confirmBtn = () => screen.getAllByRole('button', { name: 'Delete video' })[0] as HTMLButtonElement
const cancelBtn = () => screen.getByRole('button', { name: 'Cancel' }) as HTMLButtonElement

beforeEach(() => {
  vi.useFakeTimers()
  detail = video()
  deleteResponder = () => new Response(null, { status: 204 })
  stubFetch()
})

describe('delete confirmation', () => {
  it('offers a Delete video button and no confirmation initially', async () => {
    await mount()
    expect(screen.getByRole('button', { name: 'Delete video' })).toBeTruthy()
    expect(screen.queryByText(CONFIRM_TEXT)).toBeNull()
  })

  it('names the video by title and moves focus to Cancel', async () => {
    await mount()
    open()
    expect(screen.getByText('Delete My Video?')).toBeTruthy()
    expect(screen.getByText(CONFIRM_TEXT)).toBeTruthy()
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(cancelBtn())
    // The original control is replaced by the confirmation's own button.
    expect(screen.getAllByRole('button', { name: 'Delete video' })).toHaveLength(1)
    expect(deletes).toHaveLength(0)
  })

  it.each([
    ['null', null],
    ['empty', ''],
  ])('uses the video id when the title is %s', async (_n, title) => {
    detail = video({ title })
    await mount()
    open()
    expect(screen.getByText(`Delete ${ID}?`)).toBeTruthy()
  })

  it('renders a hostile title as text', async () => {
    detail = video({ title: '<img src=x onerror=alert(1)>' })
    await mount()
    open()
    expect(screen.getByText('Delete <img src=x onerror=alert(1)>?')).toBeTruthy()
    expect(document.querySelector('img')).toBeNull()
  })

  it('Cancel closes it, restores focus and sends nothing', async () => {
    await mount()
    open()
    fireEvent.click(cancelBtn())
    expect(screen.queryByText(CONFIRM_TEXT)).toBeNull()
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Delete video' }))
    expect(deletes).toHaveLength(0)
  })

  it('Escape closes it, restores focus and sends nothing', async () => {
    await mount()
    open()
    fireEvent.keyDown(cancelBtn(), { key: 'Escape' })
    expect(screen.queryByText(CONFIRM_TEXT)).toBeNull()
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Delete video' }))
    expect(deletes).toHaveLength(0)
  })
})

describe('delete request', () => {
  it('sends one DELETE while pending, with both buttons disabled', async () => {
    let release: (r: Response) => void = () => {}
    deleteResponder = () => new Promise<Response>((r) => (release = r))
    await mount()
    open()
    fireEvent.click(confirmBtn())
    fireEvent.click(confirmBtn())
    await settle()
    fireEvent.click(confirmBtn())
    expect(deletes).toHaveLength(1)
    expect(deletes[0]?.url.pathname.endsWith(`/videos/${ID}`)).toBe(true)
    expect(confirmBtn().disabled).toBe(true)
    expect(cancelBtn().disabled).toBe(true)
    await act(async () => release(new Response(null, { status: 204 })))
    await settle()
  })

  it.each([
    ['204', 204],
    ['404', 404],
  ])('on %s cleans the cache and goes to the Library with a notice', async (_n, status) => {
    deleteResponder = () => (status === 204 ? new Response(null, { status }) : json({ detail: 'gone' }, 404))
    const { client, router } = await mount()
    client.setQueryData(['transcript', ID, 0], {})
    client.setQueryData(['analyses', ID], {})
    client.setQueryData(['analyses-total', ID], 3)
    client.setQueryData(['other', ID], 'kept')
    open()
    fireEvent.click(confirmBtn())
    await settle()
    expect(router.state.location.pathname).toBe('/')
    expect(deletes).toHaveLength(1)
    const notice = screen.getByText('Video deleted')
    expect(notice.closest('[aria-live="polite"]')).not.toBeNull()
    for (const key of [['video', ID], ['transcript', ID, 0], ['analyses', ID], ['analyses-total', ID]]) {
      expect(client.getQueryData(key), `cached ${key.join('/')}`).toBeUndefined()
    }
    expect(client.getQueryData(['other', ID])).toBe('kept')
    // Back cannot show the deleted video from cache.
    const before = gets.length
    detail = video({ title: 'Ghost' })
    await act(async () => {
      await router.navigate(-1)
    })
    await settle()
    expect(router.state.location.pathname).toBe(`/videos/${ID}`)
    expect(screen.queryByText('My Video')).toBeNull()
    expect(gets.length).toBeGreaterThan(before)
  })

  it('shows the notice once: it is not kept in the history entry', async () => {
    const { router } = await mount()
    open()
    fireEvent.click(confirmBtn())
    await settle()
    expect(screen.getByText('Video deleted')).toBeTruthy()
    expect(router.state.location.state).toBeNull()
  })

  it.each([
    [500, 'Could not delete the video. Check your connection and try again.'],
    [429, 'Too many requests. Try again shortly.'],
    [503, 'The database is unavailable. Try again shortly.'],
  ])('on %s shows a fixed alert and keeps everything', async (status, message) => {
    deleteResponder = () => json({ detail: 'SECRET raw text', video_id: 'zzzzzzzzzzz' }, status)
    const { client, router } = await mount()
    open()
    fireEvent.click(confirmBtn())
    await settle()
    expect(screen.getByRole('alert').textContent).toBe(message)
    expect(document.body.textContent).not.toContain('SECRET')
    expect(document.body.textContent).not.toContain('zzzzzzzzzzz')
    expect(router.state.location.pathname).toBe(`/videos/${ID}`)
    expect(screen.getByText(CONFIRM_TEXT)).toBeTruthy()
    expect(client.getQueryData(['video', ID])).toBeDefined()
    expect(confirmBtn().disabled).toBe(false)
    expect(cancelBtn().disabled).toBe(false)
    // Trying again sends a second request.
    fireEvent.click(confirmBtn())
    await settle()
    expect(deletes).toHaveLength(2)
  })

  it('on a network failure shows a fixed alert and keeps everything', async () => {
    deleteResponder = () => {
      throw new TypeError('Failed to fetch: secret-host')
    }
    const { client, router } = await mount()
    open()
    fireEvent.click(confirmBtn())
    await settle()
    expect(screen.getByRole('alert').textContent).toBe('Could not delete the video. Check your connection and try again.')
    expect(document.body.textContent).not.toContain('secret-host')
    expect(router.state.location.pathname).toBe(`/videos/${ID}`)
    expect(screen.getByText(CONFIRM_TEXT)).toBeTruthy()
    expect(client.getQueryData(['video', ID])).toBeDefined()
  })

  it('closing the confirmation clears a previous error', async () => {
    deleteResponder = () => json({}, 503)
    await mount()
    open()
    fireEvent.click(confirmBtn())
    await settle()
    fireEvent.click(cancelBtn())
    expect(screen.queryByRole('alert')).toBeNull()
  })
})
