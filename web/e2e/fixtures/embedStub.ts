import type { BrowserContext, Frame, Page } from '@playwright/test'

// Offline stand-in for the youtube-nocookie.com embed (#178). Served by
// page.route from the real embed origin, so the transport's origin/source
// check (youtubeTransport.ts) is satisfied unchanged. It speaks the real
// postMessage protocol: it answers `listening` per `mode`, and records every
// `command` it receives so a spec can assert on what usePlayer().seekTo sent.
export type EmbedMode = 'ready' | 'error' | 'silent'

export const VIDEO_ID = 'dQw4w9WgXcQ'

const stubHtml = (mode: EmbedMode) => `<!doctype html><meta charset="utf-8"><title>stub embed</title>
<script>
  window.__commands = []
  window.__listening = 0
  window.addEventListener('message', function (e) {
    var m
    try { m = JSON.parse(e.data) } catch (_) { return }
    if (m.event === 'listening') {
      window.__listening += 1
      if (${JSON.stringify(mode)} === 'ready') {
        parent.postMessage(JSON.stringify({ event: 'initialDelivery', channel: 'widget', info: {} }), '*')
      } else if (${JSON.stringify(mode)} === 'error') {
        parent.postMessage(JSON.stringify({ event: 'onError', channel: 'widget', info: 150 }), '*')
      }
    } else if (m.event === 'command') {
      window.__commands.push({ func: m.func, args: m.args })
    }
  })
</script>`

export async function stubEmbed(page: Page, mode: EmbedMode): Promise<void> {
  await page.route('https://www.youtube-nocookie.com/embed/**', (route) =>
    route.fulfill({ status: 200, contentType: 'text/html', body: stubHtml(mode) }),
  )
}

export interface Command {
  func: string
  args: unknown[]
}

export function embedFrame(page: Page): Frame {
  const frame = page.frames().find((f) => f.url().startsWith('https://www.youtube-nocookie.com/embed/'))
  if (frame === undefined) throw new Error('the embed frame is not attached')
  return frame
}

export async function receivedCommands(page: Page): Promise<Command[]> {
  return embedFrame(page).evaluate(() => (window as unknown as { __commands: Command[] }).__commands)
}

/** Handshake messages the embed has received; 0 until its document has loaded. */
export async function listeningCount(page: Page): Promise<number> {
  const frame = page.frames().find((f) => f.url().startsWith('https://www.youtube-nocookie.com/embed/'))
  if (frame === undefined) return 0
  return frame
    .evaluate(() => (window as unknown as { __listening?: number }).__listening ?? 0)
    .catch(() => 0)
}

// The error state opens https://www.youtube.com/watch in a new tab. Popups are
// outside the per-page network guard, so answer them here: still no real network.
export async function stubWatchPage(context: BrowserContext): Promise<void> {
  await context.route('https://www.youtube.com/watch**', (route) =>
    route.fulfill({ status: 200, contentType: 'text/html', body: '<!doctype html><title>watch</title>' }),
  )
}

const analysis = (claims: number[]) => ({
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
  claims: claims.map((start_sec, i) => ({
    text: `Claim number ${i + 1}`,
    speaker: 'Ada',
    confidence: 'high',
    start_sec,
    source_chunk_seq: i,
  })),
  quotes: [],
})

/** Stubs the two /api/ reads the video page makes: a video whose claims carry the given times. */
export async function stubVideoApi(page: Page, claimSeconds: number[]): Promise<void> {
  await page.route('**/api/**', (route) => {
    const path = new URL(route.request().url()).pathname
    const body = path.endsWith('/analyses')
      ? { items: [], limit: 50, offset: 0, total: 0 }
      : {
          video_id: VIDEO_ID,
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
          analysis: analysis(claimSeconds),
        }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
  })
}
