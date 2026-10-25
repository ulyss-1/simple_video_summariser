import { describe, expect, it } from 'vitest'
import { buildMomentHref, parseStartTime, T_MAX } from './moment'

const q = (s: string) => new URLSearchParams(s)

describe('parseStartTime', () => {
  it.each([
    ['t=0', 0],
    ['t=12.5', 12.5],
    ['t=3723', 3723],
    [`t=${T_MAX}`, T_MAX],
    ['t=7&page=2', 7],
  ])('%s -> %s', (query, expected) => {
    expect(parseStartTime(q(query))).toBe(expected)
  })

  it.each([
    '',
    't=',
    't=-1',
    't=-0.001',
    't=abc',
    't=1e999',
    't=Infinity',
    't=NaN',
    `t=${T_MAX + 1}`,
    't=10&t=20',
    't=1h2m3s',
    't=90s',
    't=1e2',
    't=0x10',
    't=+5',
    't=%205',
    't=5.',
  ])('%j -> null', (query) => {
    expect(parseStartTime(q(query))).toBeNull()
  })
})

describe('buildMomentHref', () => {
  it.each([
    [12.5, '/videos/abc?t=12'],
    [0, '/videos/abc?t=0'],
    [0.999, '/videos/abc?t=0'],
    [T_MAX, `/videos/abc?t=${T_MAX}`],
  ])('%s -> %s', (sec, href) => {
    expect(buildMomentHref('abc', sec)).toBe(href)
  })

  it.each([null, -0.5, Number.NaN, Number.POSITIVE_INFINITY, T_MAX + 1, 1e21])('%s -> no link', (sec) => {
    expect(buildMomentHref('abc', sec)).toBeNull()
  })

  it('encodes the video id', () => {
    expect(buildMomentHref('a/b c', 1)).toBe('/videos/a%2Fb%20c?t=1')
  })

  it.each([0, 0.5, 59.9, 3723, T_MAX - 0.5, T_MAX])('every emitted href for %s parses back', (sec) => {
    const href = buildMomentHref('abc', sec)
    expect(href).not.toBeNull()
    const parsed = parseStartTime(new URL(href ?? '', 'http://x').searchParams)
    expect(parsed).toBe(Math.floor(sec))
  })
})
