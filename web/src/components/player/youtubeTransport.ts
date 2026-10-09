// Direct postMessage transport to a youtube-nocookie.com embed that was loaded
// with enablejsapi=1 (architecture.md section 9.1). No React, no DOM types and
// and no external script is ever loaded.
//
// Every incoming message is untrusted. It is accepted only when its origin is
// exactly EMBED_ORIGIN and its source is the iframe's own contentWindow, and
// only when its data is a JSON string describing an object with a string
// `event` field. Everything else is dropped silently.

export const EMBED_ORIGIN = 'https://www.youtube-nocookie.com'

const WATCH_ORIGIN = 'https://www.youtube.com'
const DEFAULT_INTERVAL_MS = 500
const DEFAULT_MAX_ATTEMPTS = 20

/** The embed URL for an id that the caller has already validated. */
export function buildEmbedUrl(videoId: string, pageOrigin: string): string {
  const url = new URL(EMBED_ORIGIN)
  url.pathname = `/embed/${videoId}`
  url.searchParams.set('enablejsapi', '1')
  url.searchParams.set('origin', pageOrigin)
  url.searchParams.set('playsinline', '1')
  url.searchParams.set('rel', '0')
  return url.toString()
}

/** The external fallback link, optionally at a moment. */
export function buildWatchUrl(videoId: string, seconds?: number): string {
  const url = new URL('/watch', WATCH_ORIGIN)
  url.searchParams.set('v', videoId)
  let href = url.toString()
  if (seconds !== undefined) href += `&t=${Math.floor(seconds)}s`
  return href
}

/** `ready` or `error` for a valid embed message, null for anything else. */
export function parseEmbedMessage(data: unknown): 'ready' | 'error' | null {
  if (typeof data !== 'string') return null
  let parsed: unknown
  try {
    parsed = JSON.parse(data)
  } catch {
    return null
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) return null
  const event: unknown = (parsed as Record<string, unknown>)['event']
  if (event === 'onReady' || event === 'initialDelivery') return 'ready'
  if (event === 'onError') return 'error'
  return null
}

export interface MessageSink {
  postMessage(message: string, targetOrigin: string): void
}

export interface IncomingMessage {
  readonly origin: string
  readonly source: unknown
  readonly data: unknown
}

export interface MessageSource {
  addEventListener(type: 'message', listener: (e: IncomingMessage) => void): void
  removeEventListener(type: 'message', listener: (e: IncomingMessage) => void): void
}

export interface TransportOptions {
  iframe: { readonly contentWindow: MessageSink | null }
  win: MessageSource
  onReady: () => void
  onError: () => void
  intervalMs?: number
  maxAttempts?: number
}

export interface Transport {
  /** Call on the iframe `load` event. Idempotent. */
  start(): void
  /** Seek and play, or queue (latest wins) until the embed is ready. */
  seek(seconds: number): void
  dispose(): void
}

let nextChannelId = 1

export function createTransport(options: TransportOptions): Transport {
  const { iframe, win, onReady, onError } = options
  const intervalMs = options.intervalMs ?? DEFAULT_INTERVAL_MS
  const maxAttempts = options.maxAttempts ?? DEFAULT_MAX_ATTEMPTS
  const id = nextChannelId++

  let started = false
  let ready = false
  let done = false
  let attempts = 0
  let pending: number | null = null
  let timer: ReturnType<typeof setInterval> | null = null

  function post(payload: object): void {
    const target = iframe.contentWindow
    if (target === null) return
    target.postMessage(JSON.stringify({ ...payload, id, channel: 'widget' }), EMBED_ORIGIN)
  }

  function sendSeek(seconds: number): void {
    post({ event: 'command', func: 'seekTo', args: [seconds, true] })
    post({ event: 'command', func: 'playVideo', args: [] })
  }

  function stopTimer(): void {
    if (timer !== null) {
      clearInterval(timer)
      timer = null
    }
  }

  function finish(): void {
    done = true
    stopTimer()
    pending = null
    win.removeEventListener('message', onMessage)
  }

  function onMessage(e: IncomingMessage): void {
    if (done) return
    const target = iframe.contentWindow
    if (e.origin !== EMBED_ORIGIN || target === null || e.source !== target) return
    const kind = parseEmbedMessage(e.data)
    if (kind === 'error') {
      finish()
      onError()
    } else if (kind === 'ready' && !ready) {
      ready = true
      stopTimer()
      if (pending !== null) {
        const seconds = pending
        pending = null
        sendSeek(seconds)
      }
      onReady()
    }
  }

  win.addEventListener('message', onMessage)

  return {
    start() {
      if (done || started) return
      started = true
      if (ready) return
      attempts = 1
      post({ event: 'listening' })
      timer = setInterval(() => {
        if (attempts >= maxAttempts) {
          finish()
          onError()
          return
        }
        attempts += 1
        post({ event: 'listening' })
      }, intervalMs)
    },
    seek(seconds) {
      if (done) return
      if (ready) sendSeek(seconds)
      else pending = seconds
    },
    dispose() {
      if (done) return
      finish()
    },
  }
}
