import { QueryClient } from '@tanstack/react-query'

const MAX_RETRIES = 2

function httpStatus(error: unknown): number | undefined {
  if (typeof error === 'object' && error !== null && 'status' in error) {
    const { status } = error as { status: unknown }
    return typeof status === 'number' ? status : undefined
  }
  return undefined
}

/** No retry for a 4xx; other errors retry at most MAX_RETRIES times. */
export function shouldRetry(failureCount: number, error: unknown): boolean {
  const status = httpStatus(error)
  if (status !== undefined && status >= 400 && status <= 499) return false
  return failureCount < MAX_RETRIES
}

export function createQueryClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: shouldRetry } } })
}
