// The only place the API base appears. Every request goes through apiUrl().

const SCHEME = /^[a-z][a-z0-9+.-]*:/i
const AUTHORITY_OR_BACKSLASH = /^[/\\]{2}|^\\/

/** Join base and path with exactly one `/`. Throws for an off-origin path. */
export function joinApiUrl(base: string, path: string): string {
  if (SCHEME.test(path) || AUTHORITY_OR_BACKSLASH.test(path)) {
    throw new Error(`API path must be relative to the API base: ${path}`)
  }
  return `${base.replace(/\/+$/, '')}/${path.replace(/^\/+/, '')}`
}

export function apiUrl(path: string): string {
  return joinApiUrl(import.meta.env.VITE_API_BASE ?? '/api', path)
}
