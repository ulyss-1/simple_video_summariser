import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api'
import { isPlaylistListing, type PlaylistListing } from '../lib/playlist'

const playlistKey = (url: string) => ['playlist', url] as const

/**
 * POST /api/playlists/resolve (#342, #343) for the playlist URL in the page's
 * search params. A read that happens to be a POST, so it is a query: cached by
 * URL, never retried, never refetched on focus. `url` null requests nothing.
 */
export function usePlaylist(url: string | null) {
  return useQuery<PlaylistListing>({
    queryKey: ['playlist', url],
    enabled: url !== null,
    retry: 0,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    queryFn: async ({ signal }): Promise<PlaylistListing> => {
      if (url === null) throw new Error('No playlist URL')
      const body: unknown = await api.post('/playlists/resolve', { body: { url }, signal })
      if (!isPlaylistListing(body)) throw new Error('Unexpected response shape')
      return body
    },
  })
}

/** Re-read a playlist after videos were queued, so the `known` flags come from the server. */
export function useRefetchPlaylist(url: string) {
  const client = useQueryClient()
  return () => client.invalidateQueries({ queryKey: playlistKey(url) })
}
