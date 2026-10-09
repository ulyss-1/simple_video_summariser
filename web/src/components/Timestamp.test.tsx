import { describe, expect, it } from 'vitest'
import { formatTimestamp } from './Timestamp'

// Runs in the Node project: the formatter is pure and must not need a DOM.
describe('formatTimestamp', () => {
  it.each([
    [0, '0:00'],
    [0.999, '0:00'],
    [1, '0:01'],
    [9, '0:09'],
    [10, '0:10'],
    [59, '0:59'],
    [59.999, '0:59'],
    [60, '1:00'],
    [599, '9:59'],
    [600, '10:00'],
    [3599, '59:59'],
    [3599.5, '59:59'],
    [3600, '1:00:00'],
    [3600.999, '1:00:00'],
    [3723, '1:02:03'],
    [36000, '10:00:00'],
    [359999, '99:59:59'],
    [360000, '100:00:00'],
  ])('%s -> %s', (seconds, expected) => {
    expect(formatTimestamp(seconds)).toBe(expected)
  })

  it('does not depend on TZ or locale', () => {
    expect(process.env['TZ']).toBe('America/Los_Angeles')
    expect(formatTimestamp(3600)).toBe('1:00:00')
    expect(formatTimestamp(0)).toBe('0:00')
    const original = Date.prototype.toLocaleTimeString
    Date.prototype.toLocaleTimeString = () => {
      throw new Error('Date must not be used')
    }
    try {
      expect(formatTimestamp(3723)).toBe('1:02:03')
    } finally {
      Date.prototype.toLocaleTimeString = original
    }
  })
})
