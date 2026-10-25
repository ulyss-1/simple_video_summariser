import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../api'

/** Fixed text for a failed trigger. Never echoes the response body. */
export function reanalyzeErrorMessage(error: unknown): string {
  const status = error instanceof ApiError && error.kind === 'http' ? error.status : undefined
  if (status === 409) return 'An analysis is already running for this video.'
  if (status === 429) return 'Too many requests. Try again in a moment.'
  return 'Could not start the re-analysis. Try again.'
}

/**
 * POST /api/videos/{id}/reanalyze (no body). On success the video query is
 * invalidated so `active_job` appears and #50's polling takes over. No retry.
 */
export function useReanalyze(videoId: string) {
  const client = useQueryClient()
  return useMutation({
    retry: 0,
    mutationFn: () => api.post('/videos/{video_id}/reanalyze', { params: { path: { video_id: videoId } } }),
    onSuccess: () => client.invalidateQueries({ queryKey: ['video', videoId] }),
  })
}
