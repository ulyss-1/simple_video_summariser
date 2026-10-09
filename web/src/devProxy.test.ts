import { describe, expect, it } from 'vitest'
import { rewriteApiPath } from './devProxy'

describe('rewriteApiPath', () => {
  it('strips the /api prefix', () => {
    expect(rewriteApiPath('/api/healthz')).toBe('/healthz')
  })

  it('turns /api/ itself into /', () => {
    expect(rewriteApiPath('/api/')).toBe('/')
  })

  it('keeps the query string', () => {
    expect(rewriteApiPath('/api/search?q=a&b=2')).toBe('/search?q=a&b=2')
  })

  it('turns /api/?x=1 into /?x=1', () => {
    expect(rewriteApiPath('/api/?x=1')).toBe('/?x=1')
  })

  it('strips only the first /api', () => {
    expect(rewriteApiPath('/api/api/x')).toBe('/api/x')
  })

  it.each(['/apiary', '/api-docs', '/api', '/other/api/x'])(
    'leaves %s untouched because it is not under /api/',
    (path) => {
      expect(rewriteApiPath(path)).toBe(path)
    },
  )
})
