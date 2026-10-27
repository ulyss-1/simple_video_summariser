import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../api'

/** Fixed, user-facing text for a failed delete. Nothing from the response or the exception is used. */
export function deleteErrorMessage(error: unknown): string {
  if (error instanceof ApiError && error.kind === 'http') {
    switch (error.status) {
      case 429:
        return 'Too many requests. Try again shortly.'
      case 503:
        return 'The database is unavailable. Try again shortly.'
    }
  }
  return 'Could not delete the video. Check your connection and try again.'
}

/**
 * DELETE /api/videos/{id}. A 404 counts as success (already deleted). On
 * success every cached query scoped to the video is removed, so Back cannot
 * render it, and the lists that could have shown it are invalidated. Never
 * retried automatically.
 */
export function useDeleteVideo(videoId: string, onDeleted: () => void | Promise<void>) {
  const client = useQueryClient()
  return useMutation({
    retry: 0,
    mutationFn: async (): Promise<void> => {
      try {
        await api.delete('/videos/{video_id}', { params: { path: { video_id: videoId } } })
      } catch (error) {
        if (error instanceof ApiError && error.kind === 'http' && error.status === 404) return
        throw error
      }
    },
    onSuccess: async () => {
      // Leave the page first: removing a query its mounted observer watches
      // would make that observer fetch the deleted video again.
      await onDeleted()
      for (const key of ['video', 'transcript', 'analyses', 'analyses-total']) {
        client.removeQueries({ queryKey: [key, videoId] })
      }
      await Promise.all(
        ['videos', 'search', 'ops'].map((key) => client.invalidateQueries({ queryKey: [key] })),
      )
    },
  })
}
