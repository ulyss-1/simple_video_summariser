import { describe, expect, it } from 'vitest'
import { parseRoster } from './roster'

describe('parseRoster', () => {
  it('returns name and role for well-formed entries, in order', () => {
    expect(
      parseRoster({ speakers: [{ name: 'Ada', role: 'host' }, { name: 'Bob', role: 'guest' }] }),
    ).toEqual([
      { name: 'Ada', role: 'host' },
      { name: 'Bob', role: 'guest' },
    ])
  })

  it.each([
    ['null', null],
    ['undefined', undefined],
    ['a string', 'speakers'],
    ['a number', 7],
    ['an array', [{ name: 'Ada' }]],
    ['an object without speakers', { other: 1 }],
    ['speakers that is not an array', { speakers: 'Ada' }],
    ['speakers that is an object', { speakers: { name: 'Ada' } }],
    ['an empty speakers array', { speakers: [] }],
  ])('returns [] for %s', (_n, value) => {
    expect(parseRoster(value)).toEqual([])
  })

  it('skips non-object entries and entries without a non-empty string name', () => {
    expect(
      parseRoster({
        speakers: [null, 3, 'Ada', [], {}, { name: 5 }, { name: '' }, { name: '   ' }, { name: 'Ok' }],
      }),
    ).toEqual([{ name: 'Ok', role: null }])
  })

  it('drops a missing, empty or non-string role', () => {
    expect(
      parseRoster({
        speakers: [{ name: 'A' }, { name: 'B', role: 4 }, { name: 'C', role: '' }, { name: 'D', role: null }],
      }).map((s) => s.role),
    ).toEqual([null, null, null, null])
  })
})
