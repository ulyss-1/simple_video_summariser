import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { AddVideosForm } from './AddVideosForm'

type Post = { url: URL; urls: string[] }

let posts: Post[]
let responder: () => Response | Promise<Response>

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

const ok = (results: object[], counts: object) => json({ results, counts })

beforeEach(() => {
  posts = []
  responder = () => ok([], {})
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init: RequestInit = {}) => {
      const url = new URL(String(input), 'http://localhost')
      posts.push({ url, urls: (JSON.parse(String(init.body)) as { urls: string[] }).urls })
      return Promise.resolve(responder())
    }),
  )
})

function mount(maxLines?: number) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <AddVideosForm maxLines={maxLines} />
    </QueryClientProvider>,
  )
  return screen.getByRole('textbox', { name: /Add several videos/ }) as HTMLTextAreaElement
}

const form = (area: HTMLElement) => area.closest('form') as HTMLFormElement

async function flush() {
  await act(async () => {
    await Promise.resolve()
  })
}

async function submit(area: HTMLTextAreaElement, value: string) {
  fireEvent.change(area, { target: { value } })
  fireEvent.submit(form(area))
  await flush()
}

const A = 'Ab_-123xyZ0'
const B = 'dQw4w9WgXcQ'

describe('AddVideosForm', () => {
  it('sends the trimmed non-blank lines to /videos/batch and summarises the outcomes', async () => {
    responder = () =>
      ok(
        [
          { index: 0, outcome: 'queued', video_id: A, job_id: 1 },
          { index: 1, outcome: 'already_submitted', video_id: B, state: 'done' },
        ],
        { queued: 1, already_submitted: 1 },
      )
    const area = mount()
    await submit(area, `  https://youtu.be/${A}  \n\n${B}\r\n`)
    expect(posts).toHaveLength(1)
    expect(posts[0]?.url.pathname).toBe('/api/videos/batch')
    expect(posts[0]?.urls).toEqual([`https://youtu.be/${A}`, B])
    expect(screen.getByText('Queued 1')).toBeTruthy()
    expect(screen.getByText('Already added 1')).toBeTruthy()
    expect(screen.queryByRole('list', { name: 'Lines that need attention' })).toBeNull()
  })

  it('maps invalid, failed and cancelling items back to their line numbers', async () => {
    responder = () =>
      ok(
        [
          { index: 0, outcome: 'queued', video_id: A, job_id: 1 },
          { index: 1, outcome: 'invalid', reason: 'not_a_video_ref' },
          { index: 2, outcome: 'failed', video_id: B },
        ],
        { queued: 1, invalid: 1, failed: 1 },
      )
    const area = mount()
    await submit(area, `${A}\n\nnot a video\n${B}`)
    const list = screen.getByRole('list', { name: 'Lines that need attention' })
    const items = Array.from(list.querySelectorAll('li')).map((li) => li.textContent)
    expect(items).toEqual([
      'Line 3 (not a video) is not a YouTube video URL or ID',
      `Line 4 (${B}) could not be saved; try again`,
    ])
  })

  it('refuses an empty list and an over-cap list without a request', async () => {
    const area = mount(2)
    await submit(area, ' \n \n')
    expect((await screen.findByRole('alert')).textContent).toBe('Enter at least one video URL or ID, one per line.')
    await submit(area, `${A}\n${B}\nZZZZZZZZZZZ`)
    expect(screen.getByRole('alert').textContent).toBe('Add at most 2 videos at a time; the list has 3.')
    expect(posts).toHaveLength(0)
    expect(document.activeElement).toBe(area)
  })

  it('shows fixed text and refocuses the field on a server error', async () => {
    responder = () => json({ detail: 'secret <b>internals</b>' }, 503)
    const area = mount()
    await submit(area, A)
    await flush()
    const alert = screen.getByRole('alert')
    expect(alert.textContent).toBe('The service is unavailable. Try again shortly.')
    expect(area.getAttribute('aria-invalid')).toBe('true')
    expect(document.activeElement).toBe(area)
  })

  it('sends a list once while the request is in flight', async () => {
    let release!: (r: Response) => void
    responder = () => new Promise<Response>((resolve) => (release = resolve))
    const area = mount()
    await submit(area, A)
    fireEvent.submit(form(area))
    fireEvent.submit(form(area))
    expect((screen.getByRole('button', { name: 'Adding…' }) as HTMLButtonElement).disabled).toBe(true)
    await act(async () => release(ok([{ index: 0, outcome: 'queued', video_id: A }], { queued: 1 })))
    expect(posts).toHaveLength(1)
  })

  it('stays latched in the window between the settle and the re-render', async () => {
    responder = () => ok([{ index: 0, outcome: 'queued', video_id: A }], { queued: 1 })
    const area = mount()
    fireEvent.change(area, { target: { value: A } })
    // One act: the response settles, then a second submit arrives before any
    // later render could have reset or replaced anything.
    await act(async () => {
      fireEvent.submit(form(area))
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
      fireEvent.submit(form(area))
    })
    await flush()
    expect(screen.getByText('Queued 1')).toBeTruthy()
    fireEvent.submit(form(area))
    await flush()
    expect(posts).toHaveLength(1)
  })

  it('releases the latch on an error, and on an edit after success', async () => {
    let fail = true
    responder = () => (fail ? json({}, 500) : ok([{ index: 0, outcome: 'queued', video_id: A }], { queued: 1 }))
    const area = mount()
    await submit(area, A)
    fail = false
    fireEvent.submit(form(area))
    await flush()
    expect(posts).toHaveLength(2)
    fireEvent.submit(form(area))
    await flush()
    expect(posts).toHaveLength(2)
    await submit(area, `${A}\n${B}`)
    expect(posts).toHaveLength(3)
  })
})
