import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { components } from '../api'
import { BackfillChannelForm } from './BackfillChannelForm'

type BackfillResponse = components['schemas']['BackfillResponse']

const UC = 'UC' + 'a'.repeat(22)
const UD = 'UC' + 'b'.repeat(22)
const XSS = '<img src=x onerror=alert(1)>MARKER'

const resp = (over: Partial<BackfillResponse> = {}): BackfillResponse =>
  ({
    channel_id: UC,
    dry_run: true,
    limit: 200,
    listed: 3,
    total_count: 3,
    already_known: 0,
    new_videos: 3,
    enqueued: 0,
    video_ids: [],
    videos: [],
    backfill_id: null,
    estimate: {
      transcribe_audio_sec: 7200,
      llm: { cost_usd: 0.5 },
    },
    ...over,
  }) as unknown as BackfillResponse

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

const channelList = {
  items: [
    { channel_id: UC, title: 'Alpha', active: true, video_count: 2 },
    { channel_id: UD, title: null, active: true, video_count: 0 },
  ],
  total: 2,
  limit: 200,
  offset: 0,
}

let fetchMock: ReturnType<typeof vi.fn>
let client: QueryClient
let backfill: (body: { limit: number; dry_run: boolean }) => Response | Promise<Response>

function posts() {
  return fetchMock.mock.calls
    .map((c) => c as [string, RequestInit])
    .filter(([, init]) => init?.method === 'POST')
    .map(([url, init]) => ({ path: new URL(url, 'http://localhost').pathname, body: JSON.parse(String(init.body)) }))
}

async function flush() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
}

async function mount() {
  fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const path = new URL(String(input), 'http://localhost').pathname
    if (init?.method === 'POST') return Promise.resolve(backfill(JSON.parse(String(init.body))))
    if (path === '/api/channels') return Promise.resolve(json(channelList))
    return Promise.resolve(json({}, 404))
  })
  vi.stubGlobal('fetch', fetchMock)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <BackfillChannelForm />
    </QueryClientProvider>,
  )
  await flush()
}

const picker = () => screen.getByLabelText(/Channel to back-fill/) as HTMLSelectElement
const limitInput = () => screen.getByLabelText(/Newest uploads/) as HTMLInputElement

async function start(limit = '200') {
  fireEvent.change(picker(), { target: { value: UC } })
  fireEvent.change(limitInput(), { target: { value: limit } })
  fireEvent.click(screen.getByRole('button', { name: 'Backfill' }))
  await flush()
}

beforeEach(() => {
  vi.useFakeTimers()
  backfill = (b) => json(resp({ dry_run: b.dry_run, enqueued: b.dry_run ? 0 : 3 }), b.dry_run ? 200 : 202)
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('BackfillChannelForm', () => {
  it('offers only registered channels, with a labelled picker', async () => {
    await mount()
    const values = Array.from(picker().options).map((o) => o.value)
    expect(values).toEqual(['', UC, UD])
    expect(screen.getByRole('option', { name: 'Alpha' })).toBeTruthy()
    expect(screen.getByRole('option', { name: UD })).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Backfill' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('dry-runs, confirms inline, then runs for real and reports the count', async () => {
    await mount()
    const confirmSpy = vi.spyOn(window, 'confirm')
    const spy = vi.spyOn(client, 'invalidateQueries')
    await start('200')
    expect(posts()).toEqual([{ path: `/api/channels/${UC}/backfill`, body: { limit: 200, dry_run: true } }])
    expect(screen.getByText(/Back-fill 3 videos for Alpha\?/)).toBeTruthy()
    expect(screen.getByText(/about 2\.0 h of audio/)).toBeTruthy()
    expect(screen.getByText(/\$0\.50/)).toBeTruthy()
    expect(screen.getByText(/hours of CPU/)).toBeTruthy()
    const ok = screen.getByRole('button', { name: 'Confirm backfill' })
    expect(document.activeElement).toBe(ok)
    expect(posts()).toHaveLength(1)

    fireEvent.click(ok)
    await flush()
    expect(posts()[1]).toEqual({ path: `/api/channels/${UC}/backfill`, body: { limit: 200, dry_run: false } })
    expect(screen.getByText('Enqueued 3 videos for back-fill.')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Confirm backfill' })).toBeNull()
    expect(spy).toHaveBeenCalledWith({ queryKey: ['videos'] })
    expect(spy).toHaveBeenCalledWith({ queryKey: ['channels'] })
    expect(confirmSpy).not.toHaveBeenCalled()
  })

  it('Cancel sends nothing and returns focus to the trigger', async () => {
    await mount()
    await start()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await flush()
    expect(posts()).toHaveLength(1)
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Backfill' }))
  })

  it('opens no confirmation when nothing is new', async () => {
    backfill = () => json(resp({ new_videos: 0, listed: 2 }))
    await mount()
    await start()
    expect(screen.getByText('Nothing new to back-fill for this channel.')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Confirm backfill' })).toBeNull()
    expect(posts()).toHaveLength(1)
  })

  it('says older uploads remain when the cap is hit', async () => {
    backfill = (b) => json(resp({ dry_run: b.dry_run, limit: 3, listed: 3, new_videos: 3, enqueued: b.dry_run ? 0 : 3 }), 202)
    await mount()
    await start('3')
    fireEvent.click(screen.getByRole('button', { name: 'Confirm backfill' }))
    await flush()
    expect(screen.getByText(/Enqueued 3 videos for back-fill\. Older uploads remain beyond the newest 3/)).toBeTruthy()
  })

  it('a real run that enqueues nothing is reported on a 200', async () => {
    backfill = (b) => json(resp({ dry_run: b.dry_run, enqueued: 0, listed: 3 }))
    await mount()
    await start()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm backfill' }))
    await flush()
    expect(screen.getByText('Nothing was enqueued.')).toBeTruthy()
  })

  it.each(['0', '501', '1.5', 'abc', ''])('limit %j is rejected client-side and sends nothing', async (v) => {
    await mount()
    fireEvent.change(picker(), { target: { value: UC } })
    fireEvent.change(limitInput(), { target: { value: v } })
    expect(screen.getByRole('alert').textContent).toContain('1 to 500')
    expect((screen.getByRole('button', { name: 'Backfill' }) as HTMLButtonElement).disabled).toBe(true)
    expect(posts()).toHaveLength(0)
  })

  it('disables the controls while pending and a double click sends one request', async () => {
    let release: (r: Response) => void = () => {}
    backfill = () => new Promise<Response>((r) => (release = r))
    await mount()
    fireEvent.change(picker(), { target: { value: UC } })
    const btn = screen.getByRole('button', { name: 'Backfill' })
    fireEvent.click(btn)
    fireEvent.click(btn)
    await flush()
    expect(posts()).toHaveLength(1)
    const busy = screen.getByRole('button', { name: 'Checking…' }) as HTMLButtonElement
    expect(busy.disabled).toBe(true)
    release(json(resp()))
    await flush()
    // Confirm: double click sends one real run, buttons disabled in flight.
    backfill = () => new Promise<Response>((r) => (release = r))
    const ok = screen.getByRole('button', { name: 'Confirm backfill' })
    fireEvent.click(ok)
    fireEvent.click(ok)
    await flush()
    expect(posts()).toHaveLength(2)
    expect((screen.getByRole('button', { name: 'Backfilling…' }) as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByRole('button', { name: 'Cancel' }) as HTMLButtonElement).disabled).toBe(true)
    release(json(resp({ dry_run: false, enqueued: 3 }), 202))
    await flush()
    expect(screen.getByText('Enqueued 3 videos for back-fill.')).toBeTruthy()
  })

  it.each([
    [404, /not registered/],
    [409, /Concurrent change/],
    [415, /not accepted/],
    [422, /not valid/],
    [503, /unavailable/],
  ])('dry-run status %i shows a fixed message without the body', async (status, re) => {
    backfill = () => json({ detail: XSS }, status)
    await mount()
    await start()
    expect(screen.getByRole('alert').textContent).toMatch(re)
    expect(screen.queryByText(/MARKER/)).toBeNull()
    expect(screen.queryByRole('button', { name: 'Confirm backfill' })).toBeNull()
  })

  it('a failed real run shows the error and closes the confirmation', async () => {
    backfill = (b) => (b.dry_run ? json(resp()) : json({ detail: XSS }, 503))
    await mount()
    await start()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm backfill' }))
    await flush()
    expect(screen.getByRole('alert').textContent).toMatch(/unavailable/)
    expect(screen.queryByText(/MARKER/)).toBeNull()
    expect(screen.queryByRole('button', { name: 'Confirm backfill' })).toBeNull()
  })

  it('a network failure shows the generic message', async () => {
    backfill = () => {
      throw new TypeError('network')
    }
    await mount()
    await start()
    expect(screen.getByRole('alert').textContent).toMatch(/Back-fill failed/)
  })
})
