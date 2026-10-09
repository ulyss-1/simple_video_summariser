import { describe, expect, it } from 'vitest'
import { createQueryClient, shouldRetry } from './queryClient'

class HttpError extends Error {
  status: number
  constructor(status: number) {
    super(`HTTP ${status}`)
    this.status = status
  }
}

describe('shouldRetry', () => {
  it.each([400, 404, 499])('never retries status %i', (status) => {
    expect(shouldRetry(0, new HttpError(status))).toBe(false)
    expect(shouldRetry(1, new HttpError(status))).toBe(false)
  })

  it.each([399, 500, 503])('retries status %i like any other error', (status) => {
    expect(shouldRetry(0, new HttpError(status))).toBe(true)
    expect(shouldRetry(1, new HttpError(status))).toBe(true)
    expect(shouldRetry(2, new HttpError(status))).toBe(false)
  })

  it('retries an error without a status at failure count 0 and 1 only', () => {
    expect(shouldRetry(0, new Error('network'))).toBe(true)
    expect(shouldRetry(1, new Error('network'))).toBe(true)
    expect(shouldRetry(2, new Error('network'))).toBe(false)
    expect(shouldRetry(3, new Error('network'))).toBe(false)
  })

  it('treats a non-numeric status as no status', () => {
    expect(shouldRetry(0, { status: '404' })).toBe(true)
  })

  it('handles non-object errors', () => {
    expect(shouldRetry(0, null)).toBe(true)
    expect(shouldRetry(2, 'boom')).toBe(false)
  })
})

describe('createQueryClient', () => {
  it('uses shouldRetry for queries and returns a fresh client per call', () => {
    const a = createQueryClient()
    expect(a.getDefaultOptions().queries?.retry).toBe(shouldRetry)
    expect(createQueryClient()).not.toBe(a)
  })
})
