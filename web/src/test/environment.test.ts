import { describe, expect, it } from 'vitest'
import { TEST_TIME_ZONE } from './timeZone'

describe('node project environment', () => {
  it('has no DOM, so the split keeps #47 tests in Node', () => {
    expect(typeof document).toBe('undefined')
    expect(typeof window).toBe('undefined')
  })

  it('runs in the pinned non-UTC time zone', () => {
    expect(Intl.DateTimeFormat().resolvedOptions().timeZone).toBe(TEST_TIME_ZONE)
    // 2026-01-15T12:00Z is 04:00 in Los Angeles (UTC-8): fails if TZ is dropped.
    expect(new Date('2026-01-15T12:00:00Z').getHours()).toBe(4)
  })
})
