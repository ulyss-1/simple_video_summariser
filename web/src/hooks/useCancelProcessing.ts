import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../api'

/** Fixed, user-facing text for a failed cancel. Nothing from the response or the exception is used. */
export function cancelProcessingErrorMessage(error: unknown): string {
  if (error instanceof ApiError && error.kind === 'http') {
    switch (error.status) {
      case 429:
        return 'Too many requests. Try again shortly.'
      case 503:
        return 'The database is unavailable. Try again shortly.'
    }
  }
  return 'Could not cancel processing. Check your connection and try again.'
}

/**
 * POST /api/videos/{id}/cancel from the video detail page (#300). `202`, `200`
 * (including `jobs: []`, a job that finished meanwhile) and `404` (the video
 * is gone; the refetch then shows the not-found page) all count as done. Then
 * the video query is invalidated, which supersedes a poll already in flight,
 * and `onDone` runs once the fresh state has been fetched. Never retried
 * automatically. Not `useCancelVideo`, which is the Ops view's.
 */
export function useCancelProcessing(videoId: string, onDone: () => void) {
  const client = useQueryClient()
  return useMutation({
    retry: 0,
    mutationFn: async (): Promise<void> => {
      try {
        await api.post('/videos/{video_id}/cancel', {
          params: { path: { video_id: videoId } },
          body: {},
        })
      } catch (error) {
        if (
          error instanceof ApiError &&
          error.kind === 'http' &&
          error.status === 404
        )
          return
        // A 2xx whose body is not the expected JSON still stopped the job.
        if (error instanceof ApiError && error.kind === 'parse') return
        throw error
      }
    },
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ['video', videoId] })
      onDone()
    },
  })
}
