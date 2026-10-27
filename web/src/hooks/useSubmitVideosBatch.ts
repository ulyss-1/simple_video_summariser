import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, ApiError, type components } from '../api'

export type BatchSubmitResponse = components['schemas']['BatchSubmitResponse']

/** Fixed, user-facing text for a failed batch request. Nothing from the response or the exception is used. */
export function batchSubmitErrorMessage(error: unknown): string {
  if (error instanceof ApiError && error.kind === 'http') {
    switch (error.status) {
      case 422:
        return 'Could not add these videos. Check the list and try again.'
      case 429:
        return 'Too many requests. Wait a moment and try again.'
      case 503:
        return 'The service is unavailable. Try again shortly.'
    }
  }
  return 'Could not add the videos. Check your connection and try again.'
}

function isBatchSubmitResponse(v: unknown): v is BatchSubmitResponse {
  if (typeof v !== 'object' || v === null) return false
  const o = v as { counts?: unknown; results?: unknown }
  return typeof o.counts === 'object' && o.counts !== null && Array.isArray(o.results)
}

/**
 * POST /api/videos/batch (#341, #240): one outcome per item, in request order.
 * Never retried automatically; a second POST of a list is the caller's call.
 */
export function useSubmitVideosBatch() {
  const client = useQueryClient()
  return useMutation({
    retry: 0,
    mutationFn: async (urls: string[]): Promise<BatchSubmitResponse> => {
      const body: unknown = await api.post('/videos/batch', { body: { urls } })
      if (!isBatchSubmitResponse(body)) throw new Error('Unexpected response shape')
      return body
    },
    onSuccess: async () => {
      await Promise.all([
        client.invalidateQueries({ queryKey: ['videos'] }),
        client.invalidateQueries({ queryKey: ['ops'] }),
      ])
    },
  })
}
