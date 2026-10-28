import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, type components } from '../api'

type SubmitRef = components['schemas']['SubmitRef']
type VideoSubmission = components['schemas']['VideoSubmission']

function isVideoSubmission(value: unknown): value is VideoSubmission {
  if (typeof value !== 'object' || value === null) return false
  const submission = value as Record<string, unknown>
  return (
    typeof submission.created === 'boolean' &&
    typeof submission.video_id === 'string' &&
    typeof submission.job_id === 'number' &&
    typeof submission.state === 'string'
  )
}

export function useSubmitVideo() {
  const client = useQueryClient()
  return useMutation({
    retry: 0,
    mutationFn: async (ref: SubmitRef): Promise<VideoSubmission> => {
      const result: unknown = await api.post('/videos', { body: ref })
      if (!isVideoSubmission(result)) throw new Error('Unexpected response shape')
      return result
    },
    onSuccess: async (result) => {
      await Promise.all([
        // A restart (#326) revives a job in place: an open detail page refetches.
        client.invalidateQueries({ queryKey: ['video', result.video_id] }),
        client.invalidateQueries({ queryKey: ['videos'] }),
        client.invalidateQueries({ queryKey: ['ops'] }),
        client.invalidateQueries({ queryKey: ['deleted-videos'] }),
      ])
    },
  })
}
