import { describe, expect, it } from 'vitest'
import { TEST_TIME_ZONE } from './timeZone'

describe('jsdom project environment', () => {
  it('provides a document and a location', () => {
    expect(document.body).toBeTruthy()
    expect(window.location.href).toMatch(/^http/)
  })

  it('runs in the pinned non-UTC time zone', () => {
    expect(Intl.DateTimeFormat().resolvedOptions().timeZone).toBe(TEST_TIME_ZONE)
    expect(new Date('2026-01-15T12:00:00Z').getHours()).toBe(4)
  })
})
