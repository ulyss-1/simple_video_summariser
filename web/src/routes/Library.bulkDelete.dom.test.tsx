import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { components } from '../api'
import { setupUser } from '../test/user'
import { Library } from './Library'

const user = setupUser

type VideoItem = components['schemas']['VideoItem']

function vid(n: number, over: Partial<VideoItem> = {}): VideoItem {
  return {
    video_id: `v${String(n).padStart(10, '0')}`,
    title: `Title ${n}`,
    channel_id: null,
    channel_title: null,
    published_at: '2026-09-01T12:00:00Z',
    duration_sec: 60,
    latest_analysis_at: null,
    has_transcript: false,
    origin: 'manual',
    unavailable: null,
    status: 'done',
    active_job: null,
    last_failure: null,
    ...over,
  }
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

let server: VideoItem[]
let total: number | null
let gets: URL[]
let posts: { url: URL; body: unknown }[]
let postResponder: () => Response | Promise<Response>

function stubFetch() {
  gets = []
  posts = []
  total = null
  postResponder = () => json({ counts: { deleted: 0, not_found: 0 }, results: [] })
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init: RequestInit = {}) => {
      const url = new URL(String(input), 'http://localhost')
      if (init.method === 'POST') {
        posts.push({ url, body: JSON.parse(String(init.body)) })
        return Promise.resolve(postResponder())
      }
      if (url.pathname.endsWith('/channels')) return Promise.resolve(json({ items: [], total: 0, offset: 0, limit: 200 }))
      gets.push(url)
      const offset = Number(url.searchParams.get('offset') ?? 0)
      return Promise.resolve(json({ items: server.slice(offset), total: total ?? server.length, offset, limit: 50 }))
    }),
  )
}

async function settle() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
}

async function mount(entry = '/') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const router = createMemoryRouter([{ path: '/', element: <Library /> }], { initialEntries: [entry] })
  render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  await settle()
  return { client, router }
}

const group = () => screen.getByRole('group', { name: 'Selection' })
const box = (title: string) => screen.getByRole('checkbox', { name: `Select ${title}` })
const deleteButton = () => within(group()).getByRole('button', { name: /^Delete (\d+ )?selected videos?$|^Delete \d+ selected video/ })
const cancel = () => screen.getByRole('button', { name: 'Cancel' })
const live = () => within(group()).getByText(/selected$|^Deleted/)

const CONFIRM = (n: string) =>
  `Delete ${n}? Their transcripts, summaries and stored audio will be permanently removed. RSS and channel backfill will not bring them back; adding a video by its URL processes it again.`

beforeEach(() => {
  vi.useFakeTimers()
  server = [vid(1), vid(2), vid(3, { status: 'processing' }), vid(4)]
  stubFetch()
})

describe('selection', () => {
  it('has a labelled checkbox per row, falling back to the video ID', async () => {
    server = [vid(1), vid(2, { title: null }), vid(3, { title: '' })]
    await mount()
    expect(screen.getByRole('columnheader', { name: 'Select' })).toBeTruthy()
    expect(box('Title 1')).toBeTruthy()
    expect(box(server[1]!.video_id)).toBeTruthy()
    expect(box(server[2]!.video_id)).toBeTruthy()
  })

  it('starts empty with the group controls disabled', async () => {
    await mount()
    expect(live().textContent).toBe('No videos selected')
    expect((screen.getByRole('button', { name: 'Clear selection' }) as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByRole('button', { name: 'Delete selected videos' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('counts selections, selects all on the page and clears', async () => {
    await mount()
    await user().click(box('Title 1'))
    expect(live().textContent).toBe('1 selected')
    expect(screen.getByRole('button', { name: 'Delete 1 selected video' })).toBeTruthy()
    await user().click(screen.getByRole('button', { name: 'Select all on this page' }))
    expect(live().textContent).toBe('4 selected')
    expect(screen.getByRole('button', { name: 'Delete 4 selected videos' })).toBeTruthy()
    await user().click(screen.getByRole('button', { name: 'Clear selection' }))
    expect(live().textContent).toBe('No videos selected')
  })

  it('drops the selection when a sort changes', async () => {
    await mount()
    await user().click(box('Title 1'))
    await user().click(screen.getByRole('button', { name: /Append Title/ }))
    await settle()
    expect(live().textContent).toBe('No videos selected')
    expect((box('Title 1') as HTMLInputElement).checked).toBe(false)
  })

  it('drops a selected video that a refetch no longer lists', async () => {
    const { client } = await mount()
    await user().click(box('Title 1'))
    await user().click(box('Title 2'))
    server = server.filter((v) => v.title !== 'Title 1')
    await act(async () => {
      await client.invalidateQueries({ queryKey: ['videos'] })
    })
    await settle()
    expect(live().textContent).toBe('1 selected')
  })
})

describe('confirmation', () => {
  it('opens inline with the fixed sentence, a processing note, and focus on Cancel; sends nothing', async () => {
    await mount()
    await user().click(box('Title 1'))
    await user().click(box('Title 3'))
    await user().click(deleteButton())
    expect(screen.getByText(`${CONFIRM('2 videos')} Processing of 1 of them is stopped.`)).toBeTruthy()
    expect(document.activeElement).toBe(cancel())
    expect(posts).toHaveLength(0)
  })

  it('omits the processing note when none is processing, and uses the singular', async () => {
    await mount()
    await user().click(box('Title 1'))
    await user().click(deleteButton())
    expect(screen.getByText(CONFIRM('1 video'))).toBeTruthy()
  })

  it('Cancel and Escape close it, return focus to the delete button and send nothing', async () => {
    await mount()
    await user().click(box('Title 1'))
    const open = deleteButton()
    await user().click(open)
    await user().click(cancel())
    expect(screen.queryByRole('button', { name: 'Confirm delete' })).toBeNull()
    expect(document.activeElement).toBe(deleteButton())
    await user().click(deleteButton())
    fireEvent.keyDown(cancel(), { key: 'Escape' })
    await settle()
    expect(screen.queryByRole('button', { name: 'Confirm delete' })).toBeNull()
    expect(document.activeElement).toBe(deleteButton())
    expect(posts).toHaveLength(0)
  })
})

describe('result', () => {
  it('sends the IDs in row order, once, and locks every control while pending', async () => {
    let release!: (r: Response) => void
    postResponder = () => new Promise<Response>((resolve) => (release = resolve))
    await mount()
    await user().click(box('Title 3'))
    await user().click(box('Title 1'))
    await user().click(deleteButton())
    const confirm = screen.getByRole('button', { name: 'Confirm delete' })
    await user().click(confirm)
    await user().click(confirm)
    fireEvent.click(confirm)
    expect(posts).toHaveLength(1)
    expect(posts[0]?.url.pathname.endsWith('/videos/delete')).toBe(true)
    expect(posts[0]?.body).toEqual({ video_ids: [server[0]!.video_id, server[2]!.video_id] })
    for (const b of within(group()).getAllByRole('button')) expect((b as HTMLButtonElement).disabled).toBe(true)
    for (const c of screen.getAllByRole('checkbox')) expect((c as HTMLInputElement).disabled).toBe(true)
    await act(async () => {
      release(json({ counts: { deleted: 2, not_found: 0 }, results: [] }))
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(posts).toHaveLength(1)
  })

  it('on 200 closes, clears the selection, reports the counts, focuses the line and cleans the cache', async () => {
    const [a, b] = [server[0]!.video_id, server[1]!.video_id]
    const { client } = await mount()
    for (const key of ['video', 'transcript', 'analyses', 'analyses-total']) client.setQueryData([key, a], { x: 1 })
    client.setQueryData(['video', server[3]!.video_id], { x: 1 })
    await user().click(box('Title 1'))
    await user().click(box('Title 2'))
    await user().click(deleteButton())
    postResponder = () =>
      json({
        counts: { deleted: 1, not_found: 1 },
        results: [
          { video_id: a, outcome: 'deleted' },
          { video_id: b, outcome: 'not_found' },
        ],
      })
    server = server.slice(2)
    await user().click(screen.getByRole('button', { name: 'Confirm delete' }))
    await settle()
    expect(screen.queryByRole('button', { name: 'Confirm delete' })).toBeNull()
    expect(live().textContent).toBe('Deleted 1 video · 1 were already deleted')
    expect(document.activeElement).toBe(live())
    for (const key of ['video', 'transcript', 'analyses', 'analyses-total']) {
      expect(client.getQueryData([key, a])).toBeUndefined()
    }
    expect(client.getQueryData(['video', server[1]!.video_id])).toEqual({ x: 1 })
    expect(screen.queryByText('Title 1')).toBeNull()
    expect(screen.getAllByRole('checkbox')).toHaveLength(2)
  })

  it.each([
    [422, 'Could not delete these videos. Reload the page and try again.'],
    [429, 'Too many requests. Try again shortly.'],
    [503, 'The database is unavailable. Try again shortly.'],
    [500, 'Could not delete the videos. Check your connection and try again.'],
  ])('on %i keeps the confirmation and selection with fixed text', async (status, text) => {
    postResponder = () => json({ detail: 'secret server text' }, status)
    await mount()
    await user().click(box('Title 1'))
    await user().click(deleteButton())
    await user().click(screen.getByRole('button', { name: 'Confirm delete' }))
    await settle()
    expect(screen.getByRole('alert').textContent).toBe(text)
    expect(screen.queryByText(/secret/)).toBeNull()
    expect(screen.getByRole('button', { name: 'Confirm delete' })).toBeTruthy()
    expect(live().textContent).toBe('1 selected')
    expect(screen.getByText('Title 1')).toBeTruthy()
  })

  it('on a network failure shows the connection text', async () => {
    postResponder = () => Promise.reject(new TypeError('Failed to fetch'))
    await mount()
    await user().click(box('Title 1'))
    await user().click(deleteButton())
    await user().click(screen.getByRole('button', { name: 'Confirm delete' }))
    await settle()
    expect(screen.getByRole('alert').textContent).toBe(
      'Could not delete the videos. Check your connection and try again.',
    )
  })

  it('shows the past-the-end state after deleting the whole last page', async () => {
    server = Array.from({ length: 51 }, (_, i) => vid(i + 1))
    await mount('/?page=2')
    const only = server[50]!
    await user().click(box(only.title!))
    await user().click(deleteButton())
    postResponder = () =>
      json({ counts: { deleted: 1, not_found: 0 }, results: [{ video_id: only.video_id, outcome: 'deleted' }] })
    server = server.slice(0, 50)
    total = 50
    await user().click(screen.getByRole('button', { name: 'Confirm delete' }))
    await settle()
    expect(screen.getByText('No videos on this page')).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Go to the last page' })).toBeTruthy()
    expect(live().textContent).toBe('Deleted 1 video')
  })
})
