// YouTube video ids: exactly 11 characters from [A-Za-z0-9_-]. The route param
// is untrusted URL input, so every view that reads `:videoId` goes through
// this helper (#50, #51, #53) instead of writing its own pattern.
// `[A-Za-z0-9_-]` is ASCII-only; `\z` does not exist in JS, so check length
// separately to keep `$` from accepting a trailing newline.
const VIDEO_ID = /^[A-Za-z0-9_-]{11}$/

export function parseVideoId(raw: string | undefined): string | null {
  if (raw === undefined || raw.length !== 11) return null
  return VIDEO_ID.test(raw) ? raw : null
}
