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
