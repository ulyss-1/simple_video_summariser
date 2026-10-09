// Dev-server proxy rule, mirroring nginx's `proxy_pass http://api:8000/;`.

/** Prefix of the paths the dev server forwards to the backend. */
export const API_PREFIX = '/api/'

/** Strip the /api prefix; paths outside /api/ are returned unchanged. */
export function rewriteApiPath(path: string): string {
  return path.startsWith(API_PREFIX) ? `/${path.slice(API_PREFIX.length)}` : path
}
