import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { setupUser } from '../test/user'
import { AddVideosForm } from './AddVideosForm'

const user = setupUser()

let posts: { url: URL; urls: string[] }[]
let responder: () => Response | Promise<Response>

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

const ok = (results: object[]) => json({ results, counts: {} })

beforeEach(() => {
  posts = []
  responder = () => ok([])
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init: RequestInit = {}) => {
      posts.push({
        url: new URL(String(input), 'http://localhost'),
        urls: (JSON.parse(String(init.body)) as { urls: string[] }).urls,
      })
      return Promise.resolve(responder())
    }),
  )
})

async function mount(maxLines?: number) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <AddVideosForm maxLines={maxLines} />
      </QueryClientProvider>
    </MemoryRouter>,
  )
  await user.click(screen.getByRole('button', { name: 'Add several videos' }))
  return screen.getByRole('textbox', { name: 'YouTube video URLs or IDs, one per line' }) as HTMLTextAreaElement
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
const C = 'aaaaaaaaaaa'

const lis = () =>
  within(screen.getByRole('list', { name: 'Results' }))
    .getAllByRole('listitem')
    .map((li) => li.textContent)

describe('AddVideosForm', () => {
  it('is collapsed until the disclosure button opens it', async () => {
    const client = new QueryClient()
    render(
      <MemoryRouter>
        <QueryClientProvider client={client}>
          <AddVideosForm />
        </QueryClientProvider>
      </MemoryRouter>,
    )
    const button = screen.getByRole('button', { name: 'Add several videos' })
    expect(button.getAttribute('aria-expanded')).toBe('false')
    expect(screen.queryByRole('textbox')).toBeNull()
    await user.click(button)
    expect(button.getAttribute('aria-expanded')).toBe('true')
    expect(screen.getByRole('button', { name: 'Add videos' })).toBeTruthy()
  })

  it('sends the trimmed non-blank lines once and lists one result per line', async () => {
    responder = () =>
      ok([
        { index: 0, outcome: 'queued', video_id: A, job_id: 1 },
        { index: 1, outcome: 'already_submitted', video_id: B, state: 'done' },
        { index: 2, outcome: 'restarted', video_id: C, job_id: 2 },
      ])
    const area = await mount()
    await submit(area, `  https://youtu.be/${A}  \n\n${B}\r\n${C}`)
    expect(posts).toHaveLength(1)
    expect(posts[0]?.url.pathname).toBe('/api/videos/batch')
    expect(posts[0]?.urls).toEqual([`https://youtu.be/${A}`, B, C])
    expect(lis()).toEqual([`Queued: ${A} Open`, `Already added: ${B} Open`, `Queued: ${C} Open`])
    expect(screen.getAllByRole('link', { name: 'Open' })[0]?.getAttribute('href')).toBe(`/videos/${A}`)
    expect(screen.getByText('Queued 2 · Already added 1')).toBeTruthy()
    expect(area.value).toBe('')
  })

  it('words each outcome and keeps only the lines to fix or resend', async () => {
    const long = 'x'.repeat(70)
    responder = () =>
      ok([
        { index: 0, outcome: 'queued', video_id: A },
        { index: 1, outcome: 'duplicate', video_id: A, first_index: 0 },
        { index: 2, outcome: 'invalid', reason: 'not_a_video_ref' },
        { index: 3, outcome: 'cancellation_in_progress', video_id: B },
        { index: 4, outcome: 'failed', video_id: C },
      ])
    const area = await mount()
    await submit(area, `${A}\n${A}\n${long}\n${B}\n${C}`)
    expect(lis()).toEqual([
      `Queued: ${A} Open`,
      'Line 2: same video as line 1',
      `Line 3: not a YouTube video URL or ID (${'x'.repeat(60)}…)`,
      `${B}: still being deleted, try again in a minute`,
      `${C}: could not be added`,
    ])
    expect(screen.getByText('Queued 1 · Not valid 1')).toBeTruthy()
    expect(area.value).toBe(`${long}\n${B}\n${C}`)
    expect(screen.getByRole('list', { name: 'Results' }).getAttribute('aria-live')).toBe('polite')
  })

  it('Retry these resends only the failed and cancelling lines and replaces their results', async () => {
    responder = () =>
      ok([
        { index: 0, outcome: 'invalid', reason: 'not_a_video_ref' },
        { index: 1, outcome: 'failed', video_id: B },
        { index: 2, outcome: 'cancellation_in_progress', video_id: C },
      ])
    const area = await mount()
    await submit(area, `nope\n${B}\n${C}`)
    responder = () =>
      ok([
        { index: 0, outcome: 'queued', video_id: B },
        { index: 1, outcome: 'failed', video_id: C },
      ])
    await user.click(screen.getByRole('button', { name: 'Retry these' }))
    expect(posts).toHaveLength(2)
    expect(posts[1]?.urls).toEqual([B, C])
    expect(lis()).toEqual([
      'Line 1: not a YouTube video URL or ID (nope…)',
      `Queued: ${B} Open`,
      `${C}: could not be added`,
    ])
    expect(area.value).toBe(`nope\n${C}`)
    responder = () => ok([{ index: 0, outcome: 'queued', video_id: C }])
    await user.click(screen.getByRole('button', { name: 'Retry these' }))
    expect(posts[2]?.urls).toEqual([C])
    expect(screen.queryByRole('button', { name: 'Retry these' })).toBeNull()
  })

  it('refuses an empty list and an over-cap list without a request', async () => {
    const area = await mount(2)
    await submit(area, ' \n \n')
    expect(screen.getByRole('alert').textContent).toBe('Enter at least one video URL or ID')
    expect(area.getAttribute('aria-invalid')).toBe('true')
    expect(area.getAttribute('aria-describedby')).toContain(screen.getByRole('alert').id)
    await submit(area, `${A}\n${B}\n${C}`)
    expect(screen.getByRole('alert').textContent).toBe('At most 2 videos at a time')
    expect(posts).toHaveLength(0)
  })

  it.each([
    [415, 'Could not read the list. Check it and try again.'],
    [422, 'Could not read the list. Check it and try again.'],
    [429, 'Too many requests. Try again shortly.'],
    [503, 'The service is unavailable. Try again shortly.'],
    [500, 'Could not add the videos. Check your connection and try again.'],
  ])('shows fixed text for HTTP %i and keeps the text', async (status, text) => {
    responder = () => json({ detail: 'secret <b>internals</b>' }, status)
    const area = await mount()
    await submit(area, A)
    expect((await screen.findByRole('alert')).textContent).toBe(text)
    expect(area.value).toBe(A)
    expect(document.activeElement).toBe(area)
  })

  it('shows the connection text on a network failure', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new TypeError('failed to fetch'))))
    const area = await mount()
    await submit(area, A)
    expect((await screen.findByRole('alert')).textContent).toBe('Could not add the videos. Check your connection and try again.')
  })

  it('disables the field and says how many are being added while pending, and sends once', async () => {
    let release!: (r: Response) => void
    responder = () => new Promise<Response>((resolve) => (release = resolve))
    const area = await mount()
    await submit(area, `${A}\n${B}`)
    fireEvent.submit(form(area))
    fireEvent.submit(form(area))
    expect(area.disabled).toBe(true)
    expect((screen.getByRole('button', { name: 'Add videos' }) as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByRole('status').textContent).toBe('Adding 2 videos…')
    await act(async () =>
      release(
        ok([
          { index: 0, outcome: 'queued', video_id: A },
          { index: 1, outcome: 'queued', video_id: B },
        ]),
      ),
    )
    expect(posts).toHaveLength(1)
    expect(area.disabled).toBe(false)
  })

  it('stays latched in the window between the settle and the re-render', async () => {
    responder = () => ok([{ index: 0, outcome: 'failed', video_id: A }])
    const area = await mount()
    fireEvent.change(area, { target: { value: A } })
    // One act: the response settles and the text is put back, then a submit from
    // the render before the settle arrives.
    await act(async () => {
      fireEvent.submit(form(area))
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
      fireEvent.submit(form(area))
    })
    await flush()
    expect(area.value).toBe(A)
    // The put-back text is the one that was sent: sending it again needs an edit.
    fireEvent.submit(form(area))
    await flush()
    expect(posts).toHaveLength(1)
    await submit(area, `${A}\n`)
    expect(posts).toHaveLength(2)
  })

  it('releases the latch when the request fails', async () => {
    let fail = true
    responder = () => (fail ? json({}, 503) : ok([{ index: 0, outcome: 'queued', video_id: A }]))
    const area = await mount()
    await submit(area, A)
    fail = false
    fireEvent.submit(form(area))
    await flush()
    expect(posts).toHaveLength(2)
    expect(lis()).toEqual([`Queued: ${A} Open`])
  })
})
