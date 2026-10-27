import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, ApiError, type components } from '../api'

export type UnfollowResponse = components['schemas']['UnfollowResponse']

/** Fixed, user-facing text for a failed unfollow. Nothing from the response or the exception is used. */
export function unfollowErrorMessage(error: unknown): string {
  if (error instanceof ApiError && error.kind === 'http') {
    switch (error.status) {
      case 429:
        return 'Too many requests. Try again shortly.'
      case 503:
        return 'The database is unavailable. Try again shortly.'
    }
  }
  return 'Could not unfollow the channel. Check your connection and try again.'
}

function isUnfollowResponse(v: unknown): v is UnfollowResponse {
  if (typeof v !== 'object' || v === null) return false
  const o = v as { active?: unknown; backfill?: unknown }
  return o.active === false && 'backfill' in o
}

/**
 * POST /api/channels/{id}/unfollow (#235, #340) with the body `{}`. Never
 * retried automatically. On success the channel, the channel list and Ops are
 * refetched; a 404 refetches the channel too, so the page shows it as gone.
 */
export function useUnfollowChannel(channelId: string) {
  const client = useQueryClient()
  return useMutation({
    retry: 0,
    mutationFn: async (): Promise<UnfollowResponse> => {
      const body: unknown = await api.post('/channels/{channel_id}/unfollow', {
        params: { path: { channel_id: channelId } },
        body: {},
      })
      if (!isUnfollowResponse(body)) throw new Error('Unexpected response shape')
      return body
    },
    onSuccess: () =>
      Promise.all(
        [['channel', channelId], ['channels'], ['ops']].map((queryKey) => client.invalidateQueries({ queryKey })),
      ),
    onError: (error) => {
      if (error instanceof ApiError && error.kind === 'http' && error.status === 404) {
        void client.invalidateQueries({ queryKey: ['channel', channelId] })
      }
    },
  })
}
