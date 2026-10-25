import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, type components } from '../api'
import { parseChannelId } from '../lib/channelCatalog'

type SubmitRef = components['schemas']['SubmitRef']

/**
 * POST /api/channels (#242 reuses the existing registration). Resolves to the
 * canonical channel id from the response, which is checked before it is used in
 * a URL. Never retried automatically.
 */
export function useRegisterChannel() {
  const client = useQueryClient()
  return useMutation({
    retry: 0,
    mutationFn: async (ref: SubmitRef): Promise<string> => {
      const result: unknown = await api.post('/channels', { body: ref })
      const raw = typeof result === 'object' && result !== null ? (result as { channel_id?: unknown }).channel_id : undefined
      const id = parseChannelId(typeof raw === 'string' ? raw : undefined)
      if (id === null) throw new Error('Unexpected response shape')
      return id
    },
    onSuccess: () =>
      Promise.all([
        client.invalidateQueries({ queryKey: ['channels'] }),
        client.invalidateQueries({ queryKey: ['ops'] }),
      ]),
  })
}
