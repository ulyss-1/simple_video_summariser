import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '../api'
import { isBackfillResponse, type BackfillResponse } from '../lib/backfill'

export interface BackfillArgs {
  channelId: string
  limit: number
}

async function postBackfill({ channelId, limit }: BackfillArgs, dryRun: boolean): Promise<BackfillResponse> {
  const body: unknown = await api.post('/channels/{channel_id}/backfill', {
    params: { path: { channel_id: channelId } },
    body: { limit, dry_run: dryRun },
  })
  if (!isBackfillResponse(body)) throw new Error('Unexpected response shape')
  return body
}

/**
 * POST /api/channels/{id}/backfill (#165). `preview` is the dry run that counts
 * the new videos; `run` enqueues them and then refreshes the library and the
 * channel list. Neither is retried automatically.
 */
export function useBackfillChannel() {
  const client = useQueryClient()
  const preview = useMutation({ retry: 0, mutationFn: (a: BackfillArgs) => postBackfill(a, true) })
  const run = useMutation({
    retry: 0,
    mutationFn: (a: BackfillArgs) => postBackfill(a, false),
    onSuccess: () =>
      Promise.all([
        client.invalidateQueries({ queryKey: ['videos'] }),
        client.invalidateQueries({ queryKey: ['channels'] }),
      ]),
  })
  return { preview, run }
}
