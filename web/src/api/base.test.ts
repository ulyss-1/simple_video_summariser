import { describe, expect, it } from 'vitest'
import { joinApiUrl } from './base'

describe('joinApiUrl', () => {
  it.each([
    ['/api', 'healthz'],
    ['/api', '/healthz'],
    ['/api/', 'healthz'],
    ['/api/', '/healthz'],
  ])('joins base %s and path %s with exactly one slash', (base, path) => {
    expect(joinApiUrl(base, path)).toBe('/api/healthz')
  })

  it('keeps the query string and nested path', () => {
    expect(joinApiUrl('/api', '/videos/abc?x=1')).toBe('/api/videos/abc?x=1')
  })

  it.each(['http://x', 'https://evil.example/a', '//x', '//x/healthz', 'HTTP://x', 'ftp://x'])(
    'rejects off-origin path %s',
    (path) => {
      expect(() => joinApiUrl('/api', path)).toThrow()
    },
  )

  it('rejects an off-origin path with a leading slash before the scheme', () => {
    expect(() => joinApiUrl('/api', '/\\x')).toThrow()
  })
})
