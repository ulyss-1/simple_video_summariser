import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import {
  EMBED_ORIGIN,
  buildEmbedUrl,
  buildWatchUrl,
  createTransport,
  parseEmbedMessage,
  type MessageSink,
  type MessageSource,
} from './youtubeTransport'

// The transport has no React and no DOM types in it, so it is tested in Node
// against hand-made fakes of the iframe, its contentWindow and `window`.

type Listener = (e: { origin: string; source: unknown; data: unknown }) => void

class FakeSink implements MessageSink {
  posts: Array<{ message: unknown; targetOrigin: string }> = []
  postMessage(message: string, targetOrigin: string): void {
    this.posts.push({ message: JSON.parse(message), targetOrigin })
  }
}

class FakeWindow implements MessageSource {
  listeners = new Set<Listener>()
  addEventListener(_t: 'message', l: Listener): void {
    this.listeners.add(l)
  }
  removeEventListener(_t: 'message', l: Listener): void {
    this.listeners.delete(l)
  }
  deliver(data: unknown, origin: string = EMBED_ORIGIN, source: unknown = embedWindow): void {
    for (const l of [...this.listeners]) l({ origin, source, data })
  }
}

let embedWindow: FakeSink
let win: FakeWindow
let iframe: { contentWindow: FakeSink | null }
let onReady: Mock<() => void>
let onError: Mock<() => void>

function make(options: { maxAttempts?: number; intervalMs?: number } = {}) {
  return createTransport({ iframe, win, onReady, onError, ...options })
}
const commands = () => embedWindow.posts.filter((p) => (p.message as { event: string }).event === 'command')
const handshakes = () => embedWindow.posts.filter((p) => (p.message as { event: string }).event === 'listening')
const msg = (event: string, extra: object = {}) => JSON.stringify({ event, ...extra })

beforeEach(() => {
  vi.useFakeTimers()
  embedWindow = new FakeSink()
  win = new FakeWindow()
  iframe = { contentWindow: embedWindow }
  onReady = vi.fn<() => void>()
  onError = vi.fn<() => void>()
})
afterEach(() => {
  vi.useRealTimers()
})

describe('buildEmbedUrl', () => {
  it('builds the nocookie embed URL with exactly the required params', () => {
    const url = new URL(buildEmbedUrl('-wNyEUrxzFU', 'https://app.example'))
    expect(url.origin).toBe('https://www.youtube-nocookie.com')
    expect(url.pathname).toBe('/embed/-wNyEUrxzFU')
    expect(Object.fromEntries(url.searchParams)).toEqual({
      enablejsapi: '1',
      origin: 'https://app.example',
      playsinline: '1',
      rel: '0',
    })
    expect(url.hash).toBe('')
  })
})

describe('buildWatchUrl', () => {
  it('builds the watch URL with a floored start second', () => {
    expect(buildWatchUrl('dQw4w9WgXcQ')).toBe('https://www.youtube.com/watch?v=dQw4w9WgXcQ')
    expect(buildWatchUrl('dQw4w9WgXcQ', 12.999)).toBe('https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=12s')
    expect(buildWatchUrl('dQw4w9WgXcQ', 0)).toBe('https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=0s')
  })
})

describe('parseEmbedMessage', () => {
  it.each([
    ['onReady', 'ready'],
    ['initialDelivery', 'ready'],
    ['onError', 'error'],
  ])('maps %s to %s', (event, kind) => {
    expect(parseEmbedMessage(msg(event))).toBe(kind)
  })

  it.each([
    ['non-string', { event: 'onReady' }],
    ['number', 5],
    ['undefined', undefined],
    ['invalid JSON', '{not json'],
    ['empty string', ''],
    ['JSON null', 'null'],
    ['JSON array', '[{"event":"onReady"}]'],
    ['JSON number', '5'],
    ['JSON string', '"onReady"'],
    ['no event field', '{"info":1}'],
    ['non-string event', '{"event":1}'],
    ['unknown event', msg('onStateChange', { info: 1 })],
    ['prototype-ish event name', msg('__proto__')],
    ['case-different event', msg('onready')],
  ])('ignores %s', (_name, data) => {
    expect(parseEmbedMessage(data)).toBeNull()
  })
})

describe('handshake', () => {
  it('sends nothing before start()', () => {
    make()
    vi.advanceTimersByTime(5_000)
    expect(embedWindow.posts).toEqual([])
  })

  it('sends the listening handshake to the exact origin, then repeats it', () => {
    const t = make()
    t.start()
    expect(handshakes()).toHaveLength(1)
    const first = handshakes()[0]!
    expect(first.targetOrigin).toBe('https://www.youtube-nocookie.com')
    expect(first.message).toMatchObject({ event: 'listening', channel: 'widget' })
    expect(typeof (first.message as { id: unknown }).id).toBe('number')
    vi.advanceTimersByTime(500)
    expect(handshakes()).toHaveLength(2)
    vi.advanceTimersByTime(1_000)
    expect(handshakes()).toHaveLength(4)
    t.dispose()
  })

  it('start() twice does not double the handshake or timers', () => {
    const t = make()
    t.start()
    t.start()
    expect(handshakes()).toHaveLength(1)
    vi.advanceTimersByTime(500)
    expect(handshakes()).toHaveLength(2)
    t.dispose()
  })

  it('stops retrying on the first valid message', () => {
    const t = make()
    t.start()
    win.deliver(msg('initialDelivery'))
    expect(onReady).toHaveBeenCalledTimes(1)
    vi.advanceTimersByTime(60_000)
    expect(handshakes()).toHaveLength(1)
    expect(onError).not.toHaveBeenCalled()
    t.dispose()
  })

  it('gives up after a bounded number of attempts and reports an error once', () => {
    const t = make({ maxAttempts: 4, intervalMs: 500 })
    t.start()
    vi.advanceTimersByTime(1_500)
    expect(handshakes()).toHaveLength(4)
    expect(onError).not.toHaveBeenCalled()
    vi.advanceTimersByTime(500)
    expect(onError).toHaveBeenCalledTimes(1)
    vi.advanceTimersByTime(60_000)
    expect(handshakes()).toHaveLength(4)
    expect(onError).toHaveBeenCalledTimes(1)
    expect(vi.getTimerCount()).toBe(0)
    expect(win.listeners.size).toBe(0)
  })

  it('defaults to 20 attempts every 500 ms', () => {
    const t = make()
    t.start()
    vi.advanceTimersByTime(9_500)
    expect(handshakes()).toHaveLength(20)
    expect(onError).not.toHaveBeenCalled()
    vi.advanceTimersByTime(500)
    expect(onError).toHaveBeenCalledTimes(1)
    t.dispose()
  })

  it('does not post when the iframe has no contentWindow', () => {
    iframe.contentWindow = null
    const t = make()
    t.start()
    vi.advanceTimersByTime(500)
    expect(embedWindow.posts).toEqual([])
    t.dispose()
  })
})

describe('incoming messages (untrusted)', () => {
  const hostile: Array<[string, () => void]> = [
    ['other origin youtube.com', () => win.deliver(msg('onReady'), 'https://www.youtube.com')],
    ['evil origin', () => win.deliver(msg('onReady'), 'https://evil.example')],
    ['origin with a suffix', () => win.deliver(msg('onReady'), `${EMBED_ORIGIN}.evil.example`)],
    ['origin with a prefix', () => win.deliver(msg('onReady'), `https://evil.${EMBED_ORIGIN.slice(8)}`)],
    ['http scheme', () => win.deliver(msg('onReady'), 'http://www.youtube-nocookie.com')],
    ['null origin', () => win.deliver(msg('onReady'), 'null')],
    ['empty origin', () => win.deliver(msg('onReady'), '')],
    ['source is window itself', () => win.deliver(msg('onReady'), EMBED_ORIGIN, win)],
    ['source is another iframe', () => win.deliver(msg('onReady'), EMBED_ORIGIN, new FakeSink())],
    ['source is null', () => win.deliver(msg('onReady'), EMBED_ORIGIN, null)],
    ['forged onError from wrong origin', () => win.deliver(msg('onError', { info: 150 }), 'https://evil.example')],
    ['non-string data', () => win.deliver({ event: 'onReady' })],
    ['invalid JSON', () => win.deliver('{nope')],
    ['JSON null', () => win.deliver('null')],
    ['JSON array', () => win.deliver('[]')],
    ['JSON number', () => win.deliver('7')],
    ['no event field', () => win.deliver('{}')],
    ['unknown event', () => win.deliver(msg('somethingElse'))],
  ]

  it.each(hostile)('ignores: %s', (_name, act) => {
    const t = make()
    t.start()
    expect(() => act()).not.toThrow()
    expect(onReady).not.toHaveBeenCalled()
    expect(onError).not.toHaveBeenCalled()
    // Still waiting: a queued seek is not flushed by a forged message.
    t.seek(5)
    expect(commands()).toEqual([])
    t.dispose()
  })

  it('does not trust a null source even when contentWindow is null too', () => {
    iframe.contentWindow = null
    const t = make()
    t.start()
    win.deliver(msg('onReady'), EMBED_ORIGIN, null)
    expect(onReady).not.toHaveBeenCalled()
    t.dispose()
  })

  it('ignores hostile messages without logging', () => {
    const log = vi.spyOn(console, 'error').mockImplementation(() => {})
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const t = make()
    t.start()
    for (const [, act] of hostile) act()
    expect(log).not.toHaveBeenCalled()
    expect(warn).not.toHaveBeenCalled()
    t.dispose()
  })

  it.each([2, 5, 100, 101, 150, 'weird', null])('onError with code %s becomes an error', (info) => {
    const t = make()
    t.start()
    win.deliver(msg('onError', { info }))
    expect(onError).toHaveBeenCalledTimes(1)
    expect(onReady).not.toHaveBeenCalled()
    expect(win.listeners.size).toBe(0)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('onError without info is still an error', () => {
    const t = make()
    t.start()
    win.deliver(msg('onError'))
    expect(onError).toHaveBeenCalledTimes(1)
  })

  it('reports ready only once, even for repeated onReady/initialDelivery', () => {
    const t = make()
    t.start()
    win.deliver(msg('initialDelivery'))
    win.deliver(msg('onReady'))
    expect(onReady).toHaveBeenCalledTimes(1)
    t.dispose()
  })

  it('an onError after ready is still honoured', () => {
    const t = make()
    t.start()
    win.deliver(msg('onReady'))
    win.deliver(msg('onError', { info: 150 }))
    expect(onError).toHaveBeenCalledTimes(1)
  })
})

describe('seek', () => {
  it('does not post a command before ready; the latest request wins and is sent once on ready', () => {
    const t = make()
    t.start()
    t.seek(10)
    t.seek(20)
    t.seek(30)
    expect(commands()).toEqual([])
    win.deliver(msg('onReady'))
    expect(commands().map((c) => c.message)).toEqual([
      expect.objectContaining({ event: 'command', func: 'seekTo', args: [30, true], channel: 'widget' }),
      expect.objectContaining({ event: 'command', func: 'playVideo', args: [], channel: 'widget' }),
    ])
    for (const c of commands()) expect(c.targetOrigin).toBe('https://www.youtube-nocookie.com')
    t.dispose()
  })

  it('sends no command on ready when nothing was queued', () => {
    const t = make()
    t.start()
    win.deliver(msg('onReady'))
    expect(commands()).toEqual([])
    t.dispose()
  })

  it('once ready, every seek sends one seekTo and one playVideo, in order, undebounced', () => {
    const t = make()
    t.start()
    win.deliver(msg('onReady'))
    t.seek(12.999)
    t.seek(999999)
    t.seek(0)
    expect(commands().map((c) => (c.message as { func: string; args: unknown[] }).func)).toEqual([
      'seekTo', 'playVideo', 'seekTo', 'playVideo', 'seekTo', 'playVideo',
    ])
    expect(commands().filter((c) => (c.message as { func: string }).func === 'seekTo').map((c) => (c.message as { args: unknown[] }).args)).toEqual([
      [12.999, true], [999999, true], [0, true],
    ])
    t.dispose()
  })

  it('a queued seek is not replayed on a second ready message', () => {
    const t = make()
    t.start()
    t.seek(5)
    win.deliver(msg('onReady'))
    win.deliver(msg('initialDelivery'))
    expect(commands()).toHaveLength(2)
    t.dispose()
  })

  it('drops the queued seek when the embed errors', () => {
    const t = make()
    t.start()
    t.seek(5)
    win.deliver(msg('onError', { info: 101 }))
    t.seek(6)
    expect(commands()).toEqual([])
  })
})

describe('dispose', () => {
  it('removes the listener, clears the timer, drops the queue and posts nothing afterwards', () => {
    const t = make()
    t.start()
    t.seek(5)
    t.dispose()
    expect(win.listeners.size).toBe(0)
    expect(vi.getTimerCount()).toBe(0)
    const before = embedWindow.posts.length
    win.deliver(msg('onReady'))
    t.seek(9)
    t.start()
    vi.advanceTimersByTime(60_000)
    expect(embedWindow.posts.length).toBe(before)
    expect(onReady).not.toHaveBeenCalled()
    expect(onError).not.toHaveBeenCalled()
  })

  it('is idempotent and does not throw', () => {
    const t = make()
    t.dispose()
    expect(() => t.dispose()).not.toThrow()
  })

  it('stops commands after being ready', () => {
    const t = make()
    t.start()
    win.deliver(msg('onReady'))
    t.dispose()
    t.seek(1)
    expect(commands()).toEqual([])
  })
})
