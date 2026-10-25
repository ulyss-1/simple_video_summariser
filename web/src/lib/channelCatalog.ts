// Pure helpers for the channel catalog (#242): the four ranges, runtime guards
// for the API bodies, the canonical video link, the UTC date and the fixed
// error texts. No React, no network.
import { ApiError, type components } from '../api'
import { publishedDate, statusFilterLabel, type VideoStatus } from './library'
import { parseVideoId } from './videoId'

export type CatalogPage = components['schemas']['CatalogPageOut']
export type CatalogItem = components['schemas']['CatalogItemOut']
export type BulkCatalogResponse = components['schemas']['CatalogProcessResponse']
export type BulkCatalogResult = components['schemas']['CatalogProcessItemOut']
export type MetadataRetry = components['schemas']['MetadataRetryOut']
export type CatalogLimit = CatalogPage['limit']

/** The label of each range and the exact bounded limit it asks for. "Show all" is the 5,000 safety cap. */
export const CATALOG_RANGES = [
  ['Show last 50', 50],
  ['Show last 200', 200],
  ['Show last 500', 500],
  ['Show all', 5000],
] as const satisfies ReadonlyArray<readonly [string, CatalogLimit]>

export const DEFAULT_CATALOG_LIMIT: CatalogLimit = 50

/** How long the catalog is polled while a row's metadata is still being looked up. */
export const CATALOG_POLL_MS = 5000

const CHANNEL_ID = /^UC[A-Za-z0-9_-]{22}$/

/** The route param is untrusted URL input: a canonical channel id, or null. */
export function parseChannelId(raw: string | undefined): string | null {
  return raw !== undefined && CHANNEL_ID.test(raw) ? raw : null
}

/** The canonical watch URL, derived from the validated id only; null for anything else. */
export function canonicalVideoUrl(videoId: string): string | null {
  const id = parseVideoId(videoId)
  return id === null ? null : `https://www.youtube.com/watch?v=${id}`
}

/** `YYYY-MM-DD` in UTC (the project's date convention), or null for no or an invalid date. */
export function formatCatalogDate(publishedAt: string | null): string | null {
  return publishedDate(publishedAt)
}

/** Whether any row still waits for its description and date (drives the polling). */
export function hasPendingMetadata(page: CatalogPage | undefined): boolean {
  return (
    page !== undefined &&
    page.items.some((item) => item.enrichment_state === 'pending' || item.enrichment_state === 'running')
  )
}

export function knownLabel(item: CatalogItem): string {
  return item.processing_status === null
    ? 'Already known'
    : `Already known · ${statusFilterLabel(item.processing_status satisfies VideoStatus)}`
}

// ---- Runtime guards: the body is checked, never trusted ----------------------

const ENRICHMENT = new Set(['pending', 'running', 'done', 'failed'])
const OUTCOMES = new Set(['queued', 'already_known', 'rejected'])
const STATUSES = new Set(['done', 'processing', 'unavailable', 'failed', 'idle'])

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

const nullableString = (v: unknown) => v === null || typeof v === 'string'

function isCatalogItem(value: unknown): value is CatalogItem {
  if (!isRecord(value)) return false
  return (
    typeof value.video_id === 'string' &&
    parseVideoId(value.video_id) !== null &&
    nullableString(value.title) &&
    nullableString(value.description) &&
    nullableString(value.published_at) &&
    nullableString(value.enrichment_error) &&
    typeof value.enrichment_state === 'string' &&
    ENRICHMENT.has(value.enrichment_state) &&
    typeof value.known === 'boolean' &&
    (value.processing_status === null ||
      (typeof value.processing_status === 'string' && STATUSES.has(value.processing_status)))
  )
}

export function isCatalogPage(value: unknown): value is CatalogPage {
  if (!isRecord(value)) return false
  return (
    typeof value.channel_id === 'string' &&
    CATALOG_RANGES.some(([, limit]) => limit === value.limit) &&
    (value.total_count === null || typeof value.total_count === 'number') &&
    Array.isArray(value.items) &&
    value.items.every(isCatalogItem)
  )
}

export function isBulkResponse(value: unknown): value is BulkCatalogResponse {
  if (!isRecord(value) || typeof value.channel_id !== 'string' || !Array.isArray(value.results)) return false
  return value.results.every(
    (r: unknown) =>
      isRecord(r) &&
      typeof r.video_id === 'string' &&
      typeof r.outcome === 'string' &&
      OUTCOMES.has(r.outcome) &&
      (r.job_id === null || typeof r.job_id === 'number') &&
      (r.reason === null || r.reason === 'not_in_catalog'),
  )
}

export function isMetadataRetry(value: unknown): value is MetadataRetry {
  return isRecord(value) && typeof value.video_id === 'string' && value.enrichment_state === 'pending'
}

// ---- Fixed, user-facing error text: nothing from a response is shown ----------

function statusOf(error: unknown): number | null {
  return error instanceof ApiError && error.kind === 'http' ? (error.status ?? null) : null
}

function isNetwork(error: unknown): boolean {
  return error instanceof ApiError && error.kind === 'network'
}

export function registerErrorMessage(error: unknown): string {
  if (isNetwork(error)) return 'Could not reach the server. Check your connection and try again.'
  switch (statusOf(error)) {
    case 404:
      return 'That channel was not found.'
    case 422:
      return 'Enter a YouTube channel ID, a youtube.com/channel/ URL, an @handle, or a /c/ or /user/ URL.'
    case 429:
      return 'Too many requests. Wait a moment and try again.'
    case 502:
      return 'The channel lookup failed. Try again later.'
    case 503:
      return 'The channel lookup is unavailable. Try again shortly.'
    default:
      return 'Could not add the channel. Try again.'
  }
}

export function catalogErrorMessage(error: unknown): string {
  if (isNetwork(error)) return 'Could not reach the server. Check your connection and try again.'
  switch (statusOf(error)) {
    case 404:
      return 'This channel is not registered.'
    case 429:
      return 'Too many requests. Wait a moment and try again.'
    case 502:
      return 'The YouTube listing failed. Try again later.'
    case 503:
      return 'The YouTube listing is unavailable. Try again shortly.'
    default:
      return 'Could not load the catalog. Try again.'
  }
}

export function processErrorMessage(error: unknown): string {
  if (isNetwork(error)) return 'Could not reach the server. Your selection is kept; try again.'
  switch (statusOf(error)) {
    case 404:
      return 'This channel is not registered. Your selection is kept.'
    case 422:
      return 'The selection was refused. Your selection is kept.'
    case 429:
      return 'Too many requests. Your selection is kept; wait a moment and try again.'
    case 503:
      return 'The database is unavailable. Your selection is kept; try again shortly.'
    default:
      return 'Could not queue the selected videos. Your selection is kept; try again.'
  }
}

export function retryMetadataErrorMessage(error: unknown): string {
  if (statusOf(error) === 409) return 'That metadata lookup is already in progress.'
  if (isNetwork(error)) return 'Could not reach the server. Try the metadata retry again.'
  return 'Could not retry the metadata lookup. Try again.'
}

/** "Process 1 selected video" / "Process 3 selected videos". */
export function processButtonLabel(count: number): string {
  return `Process ${count} selected video${count === 1 ? '' : 's'}`
}
