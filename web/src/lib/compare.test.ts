import { describe, expect, it } from 'vitest'
import {
  alignByWindow,
  countClaims,
  formatCost,
  formatDuration,
  formatUtcMinutes,
  formatUtcSeconds,
  parseAnalysisParam,
  resolveSelection,
  selectionMatchesUrl,
  windowLabel,
} from './compare'

describe('parseAnalysisParam', () => {
  it.each([
    ['1', 1],
    ['7', 7],
    ['10', 10],
    ['9007199254740991', Number.MAX_SAFE_INTEGER],
  ])('accepts %s', (raw, expected) => {
    expect(parseAnalysisParam([raw])).toBe(expected)
  })

  it.each([
    ['zero', '0'],
    ['negative', '-1'],
    ['plus sign', '+1'],
    ['fraction', '1.5'],
    ['trailing dot', '1.'],
    ['exponent', '1e3'],
    ['letters', 'abc'],
    ['leading zeros', '007'],
    ['empty', ''],
    ['space before', ' 1'],
    ['space after', '1 '],
    ['trailing newline', '1\n'],
    ['hex', '0x10'],
    ['above 2^53 - 1', '9007199254740992'],
    ['far above', '99999999999999999999999999'],
    ['non-ASCII digit', '١'],
    ['markup', '<img src=x>'],
  ])('drops %s', (_name, raw) => {
    expect(parseAnalysisParam([raw])).toBeNull()
  })

  it('drops a repeated param, even when the values are equal', () => {
    expect(parseAnalysisParam(['1', '2'])).toBeNull()
    expect(parseAnalysisParam(['1', '1'])).toBeNull()
  })

  it('treats an absent param as absent', () => {
    expect(parseAnalysisParam([])).toBeNull()
  })
})

type Run = { id: number; model: string; prompt_version: string }
const run = (id: number, model: string, prompt_version: string): Run => ({ id, model, prompt_version })

describe('resolveSelection', () => {
  // API order: newest first.
  const mixed = [run(5, 'gpt', 'v2'), run(4, 'gpt', 'v2'), run(3, 'local', 'v2'), run(2, 'gpt', 'v1')]
  const same = [run(3, 'gpt', 'v2'), run(2, 'gpt', 'v2'), run(1, 'gpt', 'v2')]

  const pick = (runs: Run[], a: string[] = [], b: string[] = []) => {
    const r = resolveSelection(runs, a, b)
    return [r.a, r.b, r.unknownRequested]
  }

  it('defaults A to the newest and B to the newest run with a different pair', () => {
    expect(pick(mixed)).toEqual([5, 3, false])
  })

  it('defaults B to the second newest when every run shares the pair', () => {
    expect(pick(same)).toEqual([3, 2, false])
  })

  it('does not let a differing pair hide behind the same-pair rerun order', () => {
    const runs = [run(9, 'a', 'v1'), run(8, 'a', 'v1'), run(7, 'b', 'v1')]
    expect(pick(runs)).toEqual([9, 7, false])
  })

  it('differs on prompt_version alone, and on model alone', () => {
    expect(pick([run(3, 'm', 'v2'), run(2, 'm', 'v2'), run(1, 'm', 'v1')])).toEqual([3, 1, false])
    expect(pick([run(3, 'm', 'v1'), run(2, 'm', 'v1'), run(1, 'n', 'v1')])).toEqual([3, 1, false])
  })

  it('uses list order, never created_at, so ties keep the API order', () => {
    // Equal created_at is invisible here on purpose: the order is the API's.
    expect(pick([run(6, 'x', 'v1'), run(5, 'y', 'v1'), run(4, 'x', 'v1')])).toEqual([6, 5, false])
  })

  it('with only a, picks B by the rule relative to A', () => {
    expect(pick(mixed, ['2'])).toEqual([2, 5, false])
    expect(pick(mixed, ['5'])).toEqual([5, 3, false])
    expect(pick(mixed, ['4'])).toEqual([4, 3, false])
  })

  it('with only a and every pair equal, B is the newest other run', () => {
    expect(pick(same, ['3'])).toEqual([3, 2, false])
    expect(pick(same, ['2'])).toEqual([2, 3, false])
    expect(pick(same, ['1'])).toEqual([1, 3, false])
  })

  it('with only b, A is the newest run other than B', () => {
    expect(pick(mixed, [], ['5'])).toEqual([4, 5, false])
    expect(pick(mixed, [], ['2'])).toEqual([5, 2, false])
  })

  it('uses both when both are valid and distinct', () => {
    expect(pick(mixed, ['2'], ['3'])).toEqual([2, 3, false])
  })

  it('a equal to b drops b and picks it by the default rule, with no notice', () => {
    expect(pick(mixed, ['3'], ['3'])).toEqual([3, 5, false])
    expect(pick(mixed, ['5'], ['5'])).toEqual([5, 3, false])
  })

  it('drops an id that is not among the runs and flags the notice', () => {
    expect(pick(mixed, ['99'])).toEqual([5, 3, true])
    expect(pick(mixed, [], ['99'])).toEqual([5, 3, true])
    expect(pick(mixed, ['99'], ['98'])).toEqual([5, 3, true])
    expect(pick(mixed, ['2'], ['99'])).toEqual([2, 5, true])
    expect(pick(mixed, ['99'], ['2'])).toEqual([5, 2, true])
  })

  it('a malformed id is dropped without the notice', () => {
    expect(pick(mixed, ['abc'], ['0'])).toEqual([5, 3, false])
    expect(pick(mixed, ['1', '2'])).toEqual([5, 3, false])
  })

  it('shows a lone run alone, with b null', () => {
    const one = [run(8, 'm', 'v1')]
    expect(pick(one)).toEqual([8, null, false])
    expect(pick(one, ['8'], ['8'])).toEqual([8, null, false])
    expect(pick(one, [], ['8'])).toEqual([8, null, false])
    expect(pick(one, ['5'])).toEqual([8, null, true])
  })

  it('has no selection for zero runs', () => {
    expect(pick([])).toEqual([null, null, false])
    expect(pick([], ['1'], ['2'])).toEqual([null, null, true])
  })
})

describe('selectionMatchesUrl', () => {
  const q = (s: string) => new URLSearchParams(s)
  it('is true only when a and b are each present exactly once with the resolved value', () => {
    expect(selectionMatchesUrl(q('a=1&b=2'), 1, 2)).toBe(true)
    expect(selectionMatchesUrl(q('b=2&a=1&x=y'), 1, 2)).toBe(true)
    expect(selectionMatchesUrl(q(''), 1, 2)).toBe(false)
    expect(selectionMatchesUrl(q('a=1'), 1, 2)).toBe(false)
    expect(selectionMatchesUrl(q('b=2'), 1, 2)).toBe(false)
    expect(selectionMatchesUrl(q('a=2&b=1'), 1, 2)).toBe(false)
    expect(selectionMatchesUrl(q('a=01&b=2'), 1, 2)).toBe(false)
    expect(selectionMatchesUrl(q('a=1&a=1&b=2'), 1, 2)).toBe(false)
    expect(selectionMatchesUrl(q('a=1&b=2&b=2'), 1, 2)).toBe(false)
  })
  it('with a null side, requires that param to be absent', () => {
    expect(selectionMatchesUrl(q('a=1'), 1, null)).toBe(true)
    expect(selectionMatchesUrl(q('a=1&b=2'), 1, null)).toBe(false)
    expect(selectionMatchesUrl(q(''), null, null)).toBe(true)
    expect(selectionMatchesUrl(q('a=1'), null, null)).toBe(false)
    expect(selectionMatchesUrl(q('b=1'), null, null)).toBe(false)
    expect(selectionMatchesUrl(q('b=1'), 1, null)).toBe(false)
  })
})

describe('formatCost', () => {
  it.each([
    [null, 'not recorded'],
    [0, '$0.0000'],
    [0.01234, '$0.0123'],
    [0.01236, '$0.0124'],
    [1, '$1.0000'],
    [12.5, '$12.5000'],
    [Number.NaN, 'not recorded'],
    [Number.POSITIVE_INFINITY, 'not recorded'],
  ])('%s -> %s', (value, text) => {
    expect(formatCost(value)).toBe(text)
  })
})

describe('formatDuration', () => {
  it.each([
    [null, 'not recorded'],
    [0, '0 s'],
    [999, '0 s'],
    [1000, '1 s'],
    [59_999, '59 s'],
    [60_000, '1 min 0 s'],
    [61_000, '1 min 1 s'],
    [3_599_000, '59 min 59 s'],
    [3_600_000, '1 h 0 min 0 s'],
    [3_723_000, '1 h 2 min 3 s'],
    [90_000_000, '25 h 0 min 0 s'],
    [-1, 'not recorded'],
    [Number.NaN, 'not recorded'],
    [Number.POSITIVE_INFINITY, 'not recorded'],
  ])('%s -> %s', (ms, text) => {
    expect(formatDuration(ms)).toBe(text)
  })
})

describe('UTC dates', () => {
  it('formats to the minute and to the second in UTC regardless of TZ', () => {
    expect(formatUtcMinutes('2026-10-01T08:09:10Z')).toBe('2026-10-01 08:09 UTC')
    expect(formatUtcSeconds('2026-10-01T08:09:10Z')).toBe('2026-10-01 08:09:10 UTC')
  })
  it('converts an offset to UTC', () => {
    expect(formatUtcMinutes('2026-09-30T23:30:00-07:00')).toBe('2026-10-01 06:30 UTC')
    expect(formatUtcSeconds('2026-09-30T23:30:00-07:00')).toBe('2026-10-01 06:30:00 UTC')
  })
  it('falls back to the raw text for an unparsable value', () => {
    expect(formatUtcMinutes('garbage')).toBe('garbage')
    expect(formatUtcSeconds('garbage')).toBe('garbage')
  })
})

describe('countClaims', () => {
  const c = (speaker: string, confidence: string | null) => ({ speaker, confidence })
  it('counts each confidence level, not-given, and unknown speakers', () => {
    const counts = countClaims([
      c('Ada', 'high'),
      c('Ada', 'high'),
      c('unknown', 'medium'),
      c('Bob', 'low'),
      c('Bob', null),
      c('Bob', 'certain'),
      c('Bob', 'HIGH'),
      c('Unknown', 'low'),
    ])
    expect(counts).toEqual({ high: 2, medium: 1, low: 2, notGiven: 3, unknownSpeaker: 1 })
  })
  it('is all zero for no claims', () => {
    expect(countClaims([])).toEqual({ high: 0, medium: 0, low: 0, notGiven: 0, unknownSpeaker: 0 })
  })
})

describe('windowLabel', () => {
  it.each([
    [0, '0:00–5:00'],
    [11, '55:00–1:00:00'],
    [12, '1:00:00–1:05:00'],
  ])('window %i -> %s', (k, label) => {
    expect(windowLabel(k)).toBe(label)
  })
  it('respects a custom window size', () => {
    expect(windowLabel(1, 60)).toBe('1:00–2:00')
  })
})

describe('alignByWindow', () => {
  const it_ = (start_sec: number | null, text = String(start_sec)) => ({ start_sec, text })
  const shape = (rows: ReturnType<typeof alignByWindow<{ start_sec: number | null; text: string }>>) =>
    rows.map((r) => [r.label, r.a.map((x) => x.text), r.b.map((x) => x.text)])

  it('returns no rows when both lists are empty', () => {
    expect(alignByWindow([], [])).toEqual([])
  })

  it('keeps a one-sided window with the other side empty', () => {
    const rows = alignByWindow([it_(10)], [])
    expect(shape(rows)).toEqual([['0:00–5:00', ['10'], []]])
    expect(shape(alignByWindow([], [it_(10)]))).toEqual([['0:00–5:00', [], ['10']]])
  })

  it('puts items exactly on a boundary in the later window', () => {
    const rows = alignByWindow([it_(0), it_(299.999), it_(300)], [it_(599.999), it_(600)])
    expect(shape(rows)).toEqual([
      ['0:00–5:00', ['0', '299.999'], []],
      ['5:00–10:00', ['300'], ['599.999']],
      ['10:00–15:00', [], ['600']],
    ])
  })

  it('leaves out windows with no item on either side', () => {
    const rows = alignByWindow([it_(10)], [it_(1000)])
    expect(rows.map((r) => r.label)).toEqual(['0:00–5:00', '15:00–20:00'])
  })

  it('orders rows by window whatever the input order, with No timestamp last', () => {
    const rows = alignByWindow([it_(null), it_(700), it_(10)], [it_(400), it_(Number.NaN, 'nan')])
    expect(rows.map((r) => r.label)).toEqual(['0:00–5:00', '5:00–10:00', '10:00–15:00', 'No timestamp'])
  })

  it('groups null, negative, NaN and infinite start_sec under No timestamp only', () => {
    const rows = alignByWindow(
      [it_(null, 'n'), it_(-1, 'neg'), it_(Number.NaN, 'nan')],
      [it_(Number.POSITIVE_INFINITY, 'inf'), it_(Number.NEGATIVE_INFINITY, 'ninf')],
    )
    expect(shape(rows)).toEqual([['No timestamp', ['n', 'neg', 'nan'], ['inf', 'ninf']]])
  })

  it('shows a one-sided No timestamp row for either side', () => {
    expect(shape(alignByWindow([it_(null, 'a')], []))).toEqual([['No timestamp', ['a'], []]])
    expect(shape(alignByWindow([], [it_(null, 'b')]))).toEqual([['No timestamp', [], ['b']]])
    expect(alignByWindow([it_(1)], [])).toHaveLength(1)
  })

  it('treats negative zero as a timestamp at 0', () => {
    expect(shape(alignByWindow([it_(-0, 'z')], []))).toEqual([['0:00–5:00', ['z'], []]])
  })

  it('keeps API order within a cell', () => {
    const rows = alignByWindow([it_(50, 'late'), it_(10, 'early')], [])
    expect(shape(rows)[0]?.[1]).toEqual(['late', 'early'])
  })

  it('makes 36 windows for a 3-hour run', () => {
    const items = Array.from({ length: 36 }, (_, k) => it_(k * 300 + 1, `i${k}`))
    const rows = alignByWindow(items, items)
    expect(rows).toHaveLength(36)
    expect(rows[35]?.label).toBe('2:55:00–3:00:00')
  })

  it('honours a custom window size', () => {
    expect(alignByWindow([it_(59), it_(60)], [], 60).map((r) => r.label)).toEqual(['0:00–1:00', '1:00–2:00'])
  })

  it('rejects a non-positive or non-finite window size', () => {
    expect(() => alignByWindow([], [], 0)).toThrow(RangeError)
    expect(() => alignByWindow([], [], -5)).toThrow(RangeError)
    expect(() => alignByWindow([], [], Number.NaN)).toThrow(RangeError)
  })

  it('is deterministic and does not mutate or alias its input', () => {
    const a = [it_(10), it_(700)]
    const b = [it_(400)]
    const copyA = structuredClone(a)
    const first = alignByWindow(a, b)
    expect(alignByWindow(a, b)).toEqual(first)
    expect(a).toEqual(copyA)
    first[0]?.a.push(it_(1, 'x'))
    expect(a).toHaveLength(2)
  })
})
