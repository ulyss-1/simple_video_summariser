import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../api'

/** Fixed text for a failed trigger. Never echoes the response body. */
export function retranscribeErrorMessage(error: unknown): string {
  const status = error instanceof ApiError && error.kind === 'http' ? error.status : undefined
  if (status === 409) return 'This video cannot be transcribed with Whisper now. Showing its current state.'
  if (status === 429) return 'Too many requests. Try again in a moment.'
  return 'Could not start the Whisper transcription. Try again.'
}

/**
 * POST /api/videos/{id}/retranscribe (no body, #415). On success the video query is
 * invalidated so `active_job` appears and the polling takes over. No retry.
 */
export function useRetranscribe(videoId: string) {
  const client = useQueryClient()
  return useMutation({
    retry: 0,
    mutationFn: () =>
      api.post('/videos/{video_id}/retranscribe', { params: { path: { video_id: videoId } } }),
    onSuccess: () => client.invalidateQueries({ queryKey: ['video', videoId] }),
    onError: () => client.invalidateQueries({ queryKey: ['video', videoId] }),
  })
}
