import { describe, expect, it } from 'vitest'
import { parseVideoId } from './videoId'

describe('parseVideoId', () => {
  it.each([
    ['-wNyEUrxzFU', 'leading dash'],
    ['_abcdefghij', 'leading underscore'],
    ['dQw4w9WgXcQ', 'mixed case, case kept'],
    ['a-_B0123456', 'every allowed class'],
  ])('accepts %s (%s) unchanged', (raw) => {
    expect(parseVideoId(raw)).toBe(raw)
  })

  it('accepts exactly 11 characters and rejects 10 and 12', () => {
    expect(parseVideoId('a'.repeat(10))).toBeNull()
    expect(parseVideoId('a'.repeat(11))).toBe('a'.repeat(11))
    expect(parseVideoId('a'.repeat(12))).toBeNull()
  })

  it.each([
    ['', 'empty string'],
    ['abc%2Fdefghi', 'encoded slash, 12 chars'],
    ['abcde/fghij', 'literal slash, 11 chars'],
    ['abcdefghi..', 'dot dot, 11 chars'],
    ['..', 'dot dot'],
    ['abcde fghij', 'space, 11 chars'],
    ['abcdefghij\n', 'trailing newline (regex $ pitfall)'],
    ['abcdefghijé', 'non-ASCII, 11 chars'],
    ['abcdefghij٣', 'non-ASCII digit, 11 chars'],
    ['abcdefghi<>', 'markup characters'],
  ])('rejects %j (%s)', (raw) => {
    expect(parseVideoId(raw)).toBeNull()
  })
})
