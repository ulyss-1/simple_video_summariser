import { describe, expect, it } from 'vitest'
import { ApiError } from '../api'
import {
  canonicalVideoUrl,
  catalogErrorMessage,
  CATALOG_RANGES,
  formatCatalogDate,
  hasPendingMetadata,
  isBulkResponse,
  isCatalogPage,
  isMetadataRetry,
  knownLabel,
  parseChannelId,
  processButtonLabel,
  processErrorMessage,
  registerErrorMessage,
  retryMetadataErrorMessage,
  type CatalogItem,
  type CatalogPage,
} from './channelCatalog'

const UC = 'UC' + 'a'.repeat(22)

function item(over: Partial<CatalogItem> = {}): CatalogItem {
  return {
    video_id: 'aaaaaaaaaaa',
    title: 'T',
    url: 'https://www.youtube.com/watch?v=aaaaaaaaaaa',
    description: null,
    published_at: null,
    enrichment_state: 'pending',
    enrichment_error: null,
    known: false,
    processing_status: null,
    ...over,
  }
}

const page = (items: CatalogItem[]): CatalogPage => ({ channel_id: UC, limit: 50, total_count: null, items })
const http = (status: number) => new ApiError('http', 'x', { status })

describe('ranges', () => {
  it('maps the four labels to exact bounded limits', () => {
    expect(CATALOG_RANGES).toEqual([
      ['Show last 50', 50],
      ['Show last 200', 200],
      ['Show last 500', 500],
      ['Show all', 5000],
    ])
  })
})

describe('formatCatalogDate', () => {
  it('formats in UTC under the suite non-UTC timezone', () => {
    expect(formatCatalogDate('2026-09-01T02:00:00Z')).toBe('2026-09-01')
    expect(formatCatalogDate('2026-08-31T23:30:00-07:00')).toBe('2026-09-01')
  })

  it.each([null, 'not a date', ''])('gives null for %j', (value) => {
    expect(formatCatalogDate(value)).toBeNull()
  })
})

describe('canonicalVideoUrl', () => {
  it('builds the watch URL from a valid id, a leading dash included', () => {
    expect(canonicalVideoUrl('-wNyEUrxzFU')).toBe('https://www.youtube.com/watch?v=-wNyEUrxzFU')
  })

  it.each(['short', 'aaaaaaaaaaaa', 'aaaaaaaaaa&', 'javascript:1', '../../etc', ''])(
    'refuses %j',
    (id) => {
      expect(canonicalVideoUrl(id)).toBeNull()
    },
  )
})

describe('parseChannelId', () => {
  it('accepts a canonical id and refuses everything else', () => {
    expect(parseChannelId(UC)).toBe(UC)
    for (const bad of [undefined, '', 'UC' + 'a'.repeat(21), 'UC' + 'a'.repeat(23), 'XX' + 'a'.repeat(22), UC + '\n']) {
      expect(parseChannelId(bad)).toBeNull()
    }
  })
})

describe('hasPendingMetadata', () => {
  it('is true while any row is pending or running', () => {
    expect(hasPendingMetadata(undefined)).toBe(false)
    expect(hasPendingMetadata(page([item({ enrichment_state: 'done' })]))).toBe(false)
    expect(hasPendingMetadata(page([item({ enrichment_state: 'failed', enrichment_error: 'x' })]))).toBe(false)
    expect(hasPendingMetadata(page([item({ enrichment_state: 'done' }), item({ enrichment_state: 'running' })]))).toBe(true)
    expect(hasPendingMetadata(page([item()]))).toBe(true)
  })
})

describe('knownLabel', () => {
  it('names the library status, or just says known for a deleted video', () => {
    expect(knownLabel(item({ known: true, processing_status: 'done' }))).toBe('Already known · Analysed')
    expect(knownLabel(item({ known: true, processing_status: 'idle' }))).toBe('Already known · Not processed')
    expect(knownLabel(item({ known: true, processing_status: null }))).toBe('Already known')
  })
})

describe('response guards', () => {
  it('accepts a well-formed page and rejects malformed ones', () => {
    expect(isCatalogPage(page([item()]))).toBe(true)
    const bad: unknown[] = [
      null,
      'x',
      { ...page([]), limit: 51 },
      { ...page([]), items: 'nope' },
      { ...page([]), total_count: '3' },
      page([item({ enrichment_state: 'bogus' as never })]),
      page([item({ video_id: 'short' })]),
      page([item({ processing_status: 'bogus' as never })]),
      page([item({ known: 'yes' as never })]),
    ]
    for (const value of bad) expect(isCatalogPage(value)).toBe(false)
  })

  it('accepts every outcome and rejects a bad entry in the results', () => {
    const ok = (outcome: string, reason: string | null = null) => ({
      channel_id: UC,
      results: [{ video_id: 'aaaaaaaaaaa', outcome, job_id: null, reason }],
    })
    expect(isBulkResponse(ok('queued'))).toBe(true)
    expect(isBulkResponse(ok('already_known'))).toBe(true)
    expect(isBulkResponse(ok('rejected', 'not_in_catalog'))).toBe(true)
    expect(isBulkResponse(ok('exploded'))).toBe(false)
    expect(isBulkResponse(ok('rejected', 'because'))).toBe(false)
    expect(isBulkResponse({ channel_id: UC, results: [{ video_id: 'a' }] })).toBe(false)
    expect(isBulkResponse({ channel_id: UC })).toBe(false)
  })

  it('accepts only a pending retry receipt', () => {
    expect(isMetadataRetry({ channel_id: UC, video_id: 'aaaaaaaaaaa', enrichment_state: 'pending' })).toBe(true)
    expect(isMetadataRetry({ channel_id: UC, video_id: 'aaaaaaaaaaa', enrichment_state: 'done' })).toBe(false)
    expect(isMetadataRetry(null)).toBe(false)
  })
})

describe('fixed error text', () => {
  const secret = new ApiError('http', 'x', { status: 500, detail: 'ZZMARKER42 stack trace' })

  it.each([
    registerErrorMessage,
    catalogErrorMessage,
    processErrorMessage,
    retryMetadataErrorMessage,
  ])('never shows server text or the exception message', (message) => {
    for (const error of [secret, http(404), http(422), http(502), http(503), new ApiError('network', 'ZZMARKER42'), new Error('ZZMARKER42'), 'ZZMARKER42']) {
      expect(message(error)).not.toContain('ZZMARKER42')
    }
  })

  it('maps the registration statuses', () => {
    expect(registerErrorMessage(http(404))).toBe('That channel was not found.')
    expect(registerErrorMessage(http(502))).toBe('The channel lookup failed. Try again later.')
    expect(registerErrorMessage(http(503))).toBe('The channel lookup is unavailable. Try again shortly.')
    expect(registerErrorMessage(new Error('x'))).toBe('Could not add the channel. Try again.')
  })
})

describe('processButtonLabel', () => {
  it('pluralises', () => {
    expect(processButtonLabel(1)).toBe('Process 1 selected video')
    expect(processButtonLabel(3)).toBe('Process 3 selected videos')
  })
})
