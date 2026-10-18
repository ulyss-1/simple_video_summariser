// Channel backfill from the Library (#165). Fixed messages only: a response
// body is never echoed.
import { ApiError, type components } from '../api'

export type BackfillResponse = components['schemas']['BackfillResponse']

export const MIN_BACKFILL_LIMIT = 1
/** The server's MAX_BACKFILL_LIMIT. */
export const MAX_BACKFILL_LIMIT = 500
export const DEFAULT_BACKFILL_LIMIT = 200

/** The limit as a whole number in 1..500, or null when the text is anything else. */
export function parseBackfillLimit(text: string): number | null {
  const t = text.trim()
  if (!/^\d{1,4}$/.test(t)) return null
  const n = Number(t)
  return n >= MIN_BACKFILL_LIMIT && n <= MAX_BACKFILL_LIMIT ? n : null
}

function isCount(v: unknown): v is number {
  return typeof v === 'number' && Number.isSafeInteger(v) && v >= 0
}

export function isBackfillResponse(v: unknown): v is BackfillResponse {
  if (typeof v !== 'object' || v === null) return false
  const o = v as Record<string, unknown>
  return (
    typeof o.channel_id === 'string' &&
    typeof o.dry_run === 'boolean' &&
    isCount(o.limit) &&
    isCount(o.listed) &&
    isCount(o.new_videos) &&
    isCount(o.enqueued)
  )
}

/** Cap hit: only the newest `limit` uploads were considered. */
export function backfillCapHit(out: BackfillResponse): boolean {
  return out.listed >= out.limit
}

/** Cost figures from the dry run, as a short suffix; empty when none are present. */
export function backfillEstimateText(out: BackfillResponse): string {
  const parts: string[] = []
  const hours = out.estimate?.transcribe_audio_sec
  if (typeof hours === 'number' && hours > 0) parts.push(`about ${(hours / 3600).toFixed(1)} h of audio to transcribe`)
  const usd = out.estimate?.llm?.cost_usd
  if (typeof usd === 'number') parts.push(`estimated LLM cost $${usd.toFixed(2)}`)
  return parts.length === 0 ? '' : ` (${parts.join(', ')})`
}

export function backfillOutcomeMessage(out: BackfillResponse): string {
  const n = out.enqueued
  const head =
    n === 0
      ? 'Nothing was enqueued.'
      : `Enqueued ${n} ${n === 1 ? 'video' : 'videos'} for back-fill.`
  return backfillCapHit(out)
    ? `${head} Older uploads remain beyond the newest ${out.limit}; run another back-fill to reach them.`
    : head
}

export function backfillErrorMessage(error: unknown): string {
  const status = error instanceof ApiError && error.kind === 'http' ? error.status : undefined
  switch (status) {
    case 404:
      return 'That channel is not registered. Register it first.'
    case 409:
      return 'Concurrent change, try again'
    case 415:
      return 'The request was not accepted. Reload and try again.'
    case 422:
      return 'That channel or limit is not valid.'
    case 429:
      return 'Too many requests. Wait a moment and try again.'
    case 502:
      return 'Could not list that channel right now. Try again shortly.'
    case 503:
      return 'The service is unavailable right now. Try again shortly.'
    default:
      return 'Back-fill failed. Try again.'
  }
}
