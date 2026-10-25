import { expect, test, type Page } from '@playwright/test'

// Opt-in (#178): the REAL youtube-nocookie.com embed under the REAL production
// CSP and Referrer-Policy, served by the #57 nginx image. Run with
//   npm run test:e2e:real-embed
// (needs Docker and real YouTube network; excluded from test:e2e and CI).
// Deliberately NOT the offline fixture: the embed must reach the real service.
// /api/** has no backend behind nginx here, so it is stubbed.
const EMBEDDABLE = process.env['REAL_EMBED_ID'] ?? 'jNQXAC9IVRw'
// An embedding-disabled video, so the real embed reports 101/150.
const RESTRICTED = process.env['REAL_EMBED_RESTRICTED_ID'] ?? 'CevxZvSJ5mU'
const SEEK_TO = 30
const DEADLINE = 45_000

test.setTimeout(DEADLINE * 2)

interface Evidence {
  csp: string[]
  messages: Array<{ event?: string; info?: unknown }>
}

async function open(page: Page, videoId: string) {
  const consoleLines: string[] = []
  page.on('console', (m) => consoleLines.push(`[${m.type()}] ${m.text()}`))
  // Records, in the page, every message from the embed and every CSP violation.
  await page.addInitScript(() => {
    const w = window as unknown as { __csp: string[]; __msgs: unknown[] }
    w.__csp = []
    w.__msgs = []
    document.addEventListener('securitypolicyviolation', (e) => {
      w.__csp.push(`${e.violatedDirective} blocked ${e.blockedURI}`)
    })
    window.addEventListener('message', (e) => {
      if (e.origin !== 'https://www.youtube-nocookie.com' || typeof e.data !== 'string') return
      try {
        w.__msgs.push(JSON.parse(e.data))
      } catch {
        /* not JSON */
      }
    })
  })
  await page.route('**/api/**', (route) => {
    const path = new URL(route.request().url()).pathname
    const body = path.endsWith('/analyses')
      ? { items: [], limit: 50, offset: 0, total: 0 }
      : {
          video_id: videoId, title: 'Real embed', channel_id: null, channel_title: null,
          published_at: null, duration_sec: 600, latest_analysis_at: null, origin: 'manual',
          unavailable: null, status: 'done', active_job: null, last_failure: null, transcript: null,
          analysis: {
            id: 1, transcript_id: 1, transcript_source: 'captions', chunk_strategy: 'fixed', model: 'm',
            prompt_version: 'v1', created_at: '2026-10-01T08:09:10Z', input_tokens: 1, output_tokens: 1,
            cost_usd: null, duration_ms: null, tldr: 'Summary', speaker_roster: null, topics: [],
            claims: [{ text: 'A claim', speaker: 'Ada', confidence: 'high', start_sec: SEEK_TO, source_chunk_seq: 0 }],
            quotes: [],
          },
        }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
  })
  const response = await page.goto(`/videos/${videoId}`)
  const headers = response?.headers() ?? {}
  const evidence = async (): Promise<Evidence> => {
    const seen = await page.evaluate(() => {
      const w = window as unknown as { __csp: string[]; __msgs: Evidence['messages'] }
      return { csp: w.__csp, messages: w.__msgs }
    })
    return seen
  }
  return { headers, consoleLines, evidence }
}

function cspConsole(lines: string[]): string[] {
  return lines.filter((l) => /content security policy|refused to/i.test(l))
}

test('the real embed loads, answers the handshake and plays under the real CSP', async ({ page }, testInfo) => {
  const { headers, consoleLines, evidence } = await open(page, EMBEDDABLE)
  // The header is the image's own; assert only what the embed depends on.
  expect(headers['content-security-policy']).toContain('frame-src https://www.youtube-nocookie.com')
  expect(headers['referrer-policy']).toBe('no-referrer')

  await expect.poll(async () => (await evidence()).messages.some((m) => m.event === 'onReady' || m.event === 'initialDelivery'), {
    timeout: DEADLINE,
    message: 'handshake never reached ready',
  }).toBe(true)

  await page.getByRole('button', { name: 'Seek to 0:30' }).click()
  // The real embed reports playing (playerState 1) or the moved currentTime.
  await expect.poll(async () => {
    const { messages } = await evidence()
    return messages.some((m) => {
      const info = m.info as { playerState?: number; currentTime?: number } | null | undefined
      return info?.playerState === 1 || (info?.currentTime ?? 0) >= SEEK_TO - 1
    })
  }, { timeout: DEADLINE, message: 'the embed never reported playing/seeked' }).toBe(true)

  const seen = await evidence()
  console.log(`embed messages: ${JSON.stringify(seen.messages.map((m) => m.event))}`)
  await testInfo.attach('evidence', {
    body: JSON.stringify({ videoId: EMBEDDABLE, browser: page.context().browser()?.version(), csp: headers['content-security-policy'], referrerPolicy: headers['referrer-policy'], consoleLines }, null, 2),
    contentType: 'application/json',
  })
  console.log(`videoId=${EMBEDDABLE} browser=${page.context().browser()?.version()}`)
  console.log(`CSP: ${headers['content-security-policy']}`)
  console.log(`Referrer-Policy: ${headers['referrer-policy']}`)
  console.log(`console:\n${consoleLines.join('\n')}`)
  expect(seen.csp, 'securitypolicyviolation events').toEqual([])
  expect(cspConsole(consoleLines), 'CSP console messages').toEqual([])
  expect(seen.messages.filter((m) => m.event === 'onError'), 'player error (incl. 153)').toEqual([])
  await expect(page.getByText('This video cannot be played here.')).toHaveCount(0)
})

test('the real onError path (101/150) shows the fallback notice and watch link', async ({ page }) => {
  const { evidence } = await open(page, RESTRICTED)
  await expect(page.getByText('This video cannot be played here.')).toBeVisible({ timeout: DEADLINE })
  const link = page.locator('.player--error a')
  await expect(link).toHaveAttribute('href', `https://www.youtube.com/watch?v=${RESTRICTED}`)
  await expect(link).toHaveAttribute('target', '_blank')
  const seen = await evidence()
  const errors = seen.messages.filter((m) => m.event === 'onError')
  console.log(`restricted videoId=${RESTRICTED} onError=${JSON.stringify(errors)}`)
  expect(errors.length).toBeGreaterThan(0)
  expect(seen.csp).toEqual([])
})
