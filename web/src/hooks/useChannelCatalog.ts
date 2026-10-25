import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api'
import {
  CATALOG_POLL_MS,
  hasPendingMetadata,
  isBulkResponse,
  isCatalogPage,
  isMetadataRetry,
  type BulkCatalogResponse,
  type CatalogLimit,
  type CatalogPage,
  type MetadataRetry,
} from '../lib/channelCatalog'

const catalogKey = (channelId: string, limit: CatalogLimit) => ['channel-catalog', channelId, limit] as const

/**
 * The stored catalog, GET /api/channels/{id}/catalog. Polled every
 * CATALOG_POLL_MS only while a row still waits for its description and date.
 * `limit` null (nothing discovered yet) requests nothing.
 */
export function useChannelCatalog(channelId: string, limit: CatalogLimit | null) {
  return useQuery<CatalogPage>({
    queryKey: ['channel-catalog', channelId, limit],
    enabled: limit !== null,
    // A discovery seeds this key; do not refetch it straight away.
    staleTime: CATALOG_POLL_MS,
    refetchOnWindowFocus: false,
    refetchInterval: (query) => (hasPendingMetadata(query.state.data) ? CATALOG_POLL_MS : false),
    queryFn: async ({ signal }): Promise<CatalogPage> => {
      if (limit === null) throw new Error('No range to load')
      const page: unknown = await api.get('/channels/{channel_id}/catalog', {
        params: { path: { channel_id: channelId }, query: { limit } },
        signal,
      })
      if (!isCatalogPage(page)) throw new Error('Unexpected response shape')
      return page
    },
  })
}

/**
 * POST /api/channels/{id}/catalog/discover: lists the newest `limit` uploads and
 * stores them. The page it returns is written into the catalog query for that
 * range, so the table renders without a second request. Never retried by itself.
 */
export function useDiscoverCatalog(channelId: string) {
  const client = useQueryClient()
  return useMutation({
    retry: 0,
    mutationFn: async (limit: CatalogLimit): Promise<CatalogPage> => {
      const page: unknown = await api.post('/channels/{channel_id}/catalog/discover', {
        params: { path: { channel_id: channelId } },
        body: { limit },
      })
      if (!isCatalogPage(page) || page.limit !== limit) throw new Error('Unexpected response shape')
      return page
    },
    onSuccess: (page, limit) => {
      client.setQueryData(catalogKey(channelId, limit), page)
    },
  })
}

/** POST .../catalog/{video_id}/metadata/retry: put one failed lookup back in the queue. */
export function useRetryCatalogMetadata(channelId: string) {
  const client = useQueryClient()
  return useMutation({
    retry: 0,
    mutationFn: async (videoId: string): Promise<MetadataRetry> => {
      const result: unknown = await api.post('/channels/{channel_id}/catalog/{video_id}/metadata/retry', {
        params: { path: { channel_id: channelId, video_id: videoId } },
      })
      if (!isMetadataRetry(result)) throw new Error('Unexpected response shape')
      return result
    },
    onSettled: () => client.invalidateQueries({ queryKey: ['channel-catalog', channelId] }),
  })
}

/**
 * POST .../catalog/process: one channel-scoped bulk action. Never retried (a
 * repeat would be harmless on the server, but the user decides), and the result
 * lists every submitted id.
 */
export function useProcessCatalogSelection(channelId: string) {
  const client = useQueryClient()
  return useMutation({
    retry: 0,
    mutationFn: async (videoIds: readonly string[]): Promise<BulkCatalogResponse> => {
      const result: unknown = await api.post('/channels/{channel_id}/catalog/process', {
        params: { path: { channel_id: channelId } },
        body: { video_ids: [...videoIds] },
      })
      if (!isBulkResponse(result)) throw new Error('Unexpected response shape')
      return result
    },
    onSuccess: async () => {
      await Promise.all(
        [['channel-catalog', channelId], ['videos'], ['ops']].map((queryKey) =>
          client.invalidateQueries({ queryKey }),
        ),
      )
    },
  })
}
