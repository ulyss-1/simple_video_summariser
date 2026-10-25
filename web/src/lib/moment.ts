// The `?t=SECONDS` deep link to a moment (#129): the one parser (consumer) and
// the one href builder (producer), sharing a single bound so every emitted
// link parses back. No React.

/** The largest `t` accepted or emitted, in seconds: 100 h, far above any real video. */
export const T_MAX = 360_000

/** Plain decimal seconds only: no sign, exponent, hex, or YouTube `1h2m3s` forms. */
const PLAIN_SECONDS = /^\d+(\.\d+)?$/

/** True for a finite number within `[0, T_MAX]`. */
function inRange(seconds: number): boolean {
  return Number.isFinite(seconds) && seconds >= 0 && seconds <= T_MAX
}

/**
 * Reads `t` from untrusted URL input. Returns the exact (unfloored) seconds, or
 * null for absent, empty, non-plain-decimal, non-finite, out-of-range, or a
 * repeated `t` (`get` would silently take the first, so the ambiguity is rejected).
 */
export function parseStartTime(params: URLSearchParams): number | null {
  if (params.getAll('t').length > 1) return null
  const raw = params.get('t')
  if (raw === null || !PLAIN_SECONDS.test(raw)) return null
  const n = Number(raw)
  return inRange(n) ? n : null
}

/** `/videos/{id}?t=<whole seconds>`; null when `seconds` is not within `[0, T_MAX]`. */
export function buildMomentHref(videoId: string, seconds: number | null): string | null {
  if (typeof seconds !== 'number' || !inRange(seconds)) return null
  return `/videos/${encodeURIComponent(videoId)}?t=${Math.floor(seconds)}`
}
