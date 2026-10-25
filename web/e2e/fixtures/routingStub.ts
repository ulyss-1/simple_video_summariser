import type { Page } from '@playwright/test'
import { stubEmbed } from './embedStub.ts'

// Offline stubs for the routing specs (#222): every route of architecture §9
// renders its own h1 without a backend. The video routes mount a <Player>, so
// the youtube-nocookie.com embed is stubbed too (the offline guard fails on it).
export const ID = '-wNyEUrxzFU'
export const TITLE = 'Routing Video'

const video = {
  video_id: ID,
  title: TITLE,
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
  analysis: null,
}

export async function stubRoutingApi(page: Page): Promise<void> {
  await stubEmbed(page, 'ready')
  await page.route('**/api/**', (route) => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/healthz') {
      // The control for "a plain anchor makes a document request": the stub plays the backend.
      return route.fulfill({ status: 200, contentType: 'text/html', body: '<!doctype html><title>healthz</title>ok' })
    }
    const body =
      path === `/api/videos/${ID}`
        ? video
        : { items: [], results: [], runs: [], limit: 50, offset: 0, total: 0 }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
  })
}
