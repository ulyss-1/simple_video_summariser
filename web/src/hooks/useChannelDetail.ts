import { useQuery } from '@tanstack/react-query'
import { api, ApiError, type components } from '../api'

export type ChannelDetail = components['schemas']['ChannelDetailOut']

function isChannelDetail(v: unknown): v is ChannelDetail {
  if (typeof v !== 'object' || v === null) return false
  const o = v as Record<string, unknown>
  return (
    typeof o.channel_id === 'string' &&
    typeof o.active === 'boolean' &&
    typeof o.monitor_from === 'string' &&
    typeof o.video_count === 'number' &&
    (o.title === null || typeof o.title === 'string')
  )
}

/** The channel was not found (404): its page shows the not-found pattern. */
export function isChannelNotFound(error: unknown): boolean {
  return error instanceof ApiError && error.kind === 'http' && error.status === 404
}

/** Fixed, user-facing text for a channel that could not be loaded. Nothing from the exception is used. */
export function channelDetailErrorMessage(error: unknown): string {
  if (error instanceof ApiError && error.kind === 'http' && error.status === 503) {
    return 'The database is unavailable. Try again shortly.'
  }
  return 'Could not load the channel.'
}

/** GET /api/channels/{id} (#235): one channel's state, followed or not, with its video count. */
export function useChannelDetail(channelId: string) {
  return useQuery({
    queryKey: ['channel', channelId],
    queryFn: async ({ signal }): Promise<ChannelDetail> => {
      const body: unknown = await api.get('/channels/{channel_id}', {
        params: { path: { channel_id: channelId } },
        signal,
      })
      if (!isChannelDetail(body)) throw new Error('Unexpected response shape')
      return body
    },
  })
}
