import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../api'
import { forgetChannel } from './forgetVideos'

/** Fixed, user-facing text for a failed channel delete. Nothing from the response or the exception is used. */
export function deleteChannelErrorMessage(error: unknown): string {
  if (error instanceof ApiError && error.kind === 'http') {
    switch (error.status) {
      case 429:
        return 'Too many requests. Try again shortly.'
      case 503:
        return 'The database is unavailable. Try again shortly.'
    }
  }
  return 'Could not delete the channel. Check your connection and try again.'
}

/** The one-shot Library notice after a channel delete; the count is omitted when unknown (a 404). */
export function channelDeletedNotice(deletedVideos: number | null): string {
  return deletedVideos === null
    ? 'Channel deleted'
    : `Channel deleted · ${deletedVideos} video${deletedVideos === 1 ? '' : 's'} removed`
}

/**
 * DELETE /api/channels/{id} (#339, #340). A 404 counts as success (already
 * deleted, count unknown). `onDeleted` leaves the page first; only then is
 * every query of the channel and of its videos removed, so no mounted observer
 * fetches the deleted channel again. Never retried automatically.
 */
export function useDeleteChannel(
  channelId: string,
  onDeleted: (deletedVideos: number | null) => void | Promise<void>,
) {
  const client = useQueryClient()
  return useMutation({
    retry: 0,
    mutationFn: async (): Promise<number | null> => {
      try {
        const body: unknown = await api.delete('/channels/{channel_id}', {
          params: { path: { channel_id: channelId } },
        })
        const count = (body as { deleted_videos?: unknown } | null)?.deleted_videos
        if (typeof count !== 'number') throw new Error('Unexpected response shape')
        return count
      } catch (error) {
        if (error instanceof ApiError && error.kind === 'http' && error.status === 404) return null
        throw error
      }
    },
    onSuccess: async (deletedVideos) => {
      await onDeleted(deletedVideos)
      await forgetChannel(client, channelId)
    },
  })
}
