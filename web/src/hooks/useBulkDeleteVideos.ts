import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, ApiError, type components } from '../api'
import { forgetVideos } from './forgetVideos'

export type BulkDeleteResponse = components['schemas']['BulkDeleteResponse']

/** Fixed, user-facing text for a failed bulk delete. Nothing from the response or the exception is used. */
export function bulkDeleteErrorMessage(error: unknown): string {
  if (error instanceof ApiError && error.kind === 'http') {
    switch (error.status) {
      case 422:
        return 'Could not delete these videos. Reload the page and try again.'
      case 429:
        return 'Too many requests. Try again shortly.'
      case 503:
        return 'The database is unavailable. Try again shortly.'
    }
  }
  return 'Could not delete the videos. Check your connection and try again.'
}

function isBulkDeleteResponse(v: unknown): v is BulkDeleteResponse {
  if (typeof v !== 'object' || v === null) return false
  const o = v as { counts?: { deleted?: unknown; not_found?: unknown }; results?: unknown }
  return (
    typeof o.counts?.deleted === 'number' && typeof o.counts.not_found === 'number' && Array.isArray(o.results)
  )
}

/**
 * POST /api/videos/delete (#238, #234): all or nothing, so an error means
 * nothing was deleted. On success the cache of every ID in `results` is
 * cleaned like a single delete. Never retried automatically.
 */
export function useBulkDeleteVideos() {
  const client = useQueryClient()
  return useMutation({
    retry: 0,
    mutationFn: async (videoIds: string[]): Promise<BulkDeleteResponse> => {
      const body: unknown = await api.post('/videos/delete', { body: { video_ids: videoIds } })
      if (!isBulkDeleteResponse(body)) throw new Error('Unexpected response shape')
      return body
    },
    onSuccess: (response) => {
      // Not awaited: the caller reports the result at once; the lists refetch behind it.
      void forgetVideos(
        client,
        response.results.map((r) => r.video_id),
      )
    },
  })
}
