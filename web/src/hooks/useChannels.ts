import { useQuery } from '@tanstack/react-query'
import type { components } from '../api'
import { api } from '../api'

export type ChannelList = components['schemas']['ChannelList']

function isChannelList(v: unknown): v is ChannelList {
  if (typeof v !== 'object' || v === null) return false
  const o = v as Record<string, unknown>
  return Array.isArray(o.items) && typeof o.total === 'number'
}

/**
 * The first page of GET /api/channels (up to the server default of 200, far
 * above the expected tens of channels), for the Library channel picker. A
 * failure is just an error state: the picker degrades, nothing else depends on it.
 */
export function useChannels() {
  return useQuery({
    queryKey: ['channels'],
    queryFn: async ({ signal }): Promise<ChannelList> => {
      const body: unknown = await api.get('/channels', { signal })
      if (!isChannelList(body)) throw new Error('Unexpected response shape')
      return body
    },
  })
}
