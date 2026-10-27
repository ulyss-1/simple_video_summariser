import type { QueryClient } from '@tanstack/react-query'

/**
 * Cache cleanup after videos were deleted (#232, #234): every query scoped to
 * a video is removed, so Back cannot render it, and the lists that could have
 * shown it are invalidated. Shared by the single and the bulk delete hooks.
 * The caller leaves any page that watches these queries first.
 */
export function forgetVideos(client: QueryClient, videoIds: readonly string[]): Promise<unknown> {
  for (const videoId of videoIds) {
    for (const key of ['video', 'transcript', 'analyses', 'analyses-total']) {
      client.removeQueries({ queryKey: [key, videoId] })
    }
  }
  return Promise.all(
    ['videos', 'channels', 'search', 'ops'].map((key) => client.invalidateQueries({ queryKey: [key] })),
  )
}

/** The video IDs a cached query can show for this channel: library rows, video pages, catalog rows. */
function cachedChannelVideoIds(client: QueryClient, channelId: string): string[] {
  const ids = new Set<string>()
  const read = (rows: unknown, onlyChannel: boolean) => {
    if (!Array.isArray(rows)) return
    for (const row of rows) {
      const r = row as { video_id?: unknown; channel_id?: unknown } | null
      if (typeof r?.video_id === 'string' && (!onlyChannel || r.channel_id === channelId)) ids.add(r.video_id)
    }
  }
  for (const [, list] of client.getQueriesData<{ items?: unknown }>({ queryKey: ['videos'] })) {
    read(list?.items, true)
  }
  for (const [, page] of client.getQueriesData<{ items?: unknown }>({ queryKey: ['channel-catalog', channelId] })) {
    read(page?.items, false)
  }
  for (const [, video] of client.getQueriesData<{ video_id?: unknown; channel_id?: unknown }>({
    queryKey: ['video'],
  })) {
    read(video === undefined ? undefined : [video], true)
  }
  return [...ids]
}

/**
 * Cache cleanup after a channel and all its videos were deleted (#340): the
 * channel's own queries and every query of each of its videos found in the
 * cache are removed, then the lists are invalidated like after a video delete.
 * The caller leaves the channel page first.
 */
export function forgetChannel(client: QueryClient, channelId: string): Promise<unknown> {
  const videoIds = cachedChannelVideoIds(client, channelId)
  client.removeQueries({ queryKey: ['channel', channelId] })
  client.removeQueries({ queryKey: ['channel-catalog', channelId] })
  return forgetVideos(client, videoIds)
}
