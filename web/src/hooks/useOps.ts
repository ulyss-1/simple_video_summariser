import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError, rethrowHttp } from '../api'
import {
  bulkRetryBody,
  isBulkRetryOut,
  jobsQuery,
  isHealth,
  isPositiveSafeInt,
  isJobList,
  isOpsChannelList,
  retryMessage,
  type HealthOut,
  type BulkRetryOut,
  type JobListOut,
  type OpsChannelListOut,
  type OpsFilters,
  type RetryOutcome,
} from '../lib/ops'
import { PROGRESS_POLL_MS } from '../lib/progress'

export type { HealthOut, JobListOut, JobOut, OpsChannelOut } from '../lib/ops'

/** The queue depth is re-read this often while the Ops view is mounted. */
export const QUEUE_DEPTH_POLL_MS = 15_000

/** A non-2xx response. Carries only the status: the body is never surfaced. */
export class OpsError extends Error {
  readonly status: number
  constructor(status: number) {
    super(`HTTP ${status}`)
    this.name = 'OpsError'
    this.status = status
  }
}

/** Queue depth by kind and state (GET /api/healthz), refetched every 15 s while mounted. */
export function useQueueDepth() {
  return useQuery({
    queryKey: ['ops', 'queue-depth'],
    queryFn: async ({ signal }): Promise<HealthOut> => {
      const body: unknown = await api
        .get('/healthz', { signal })
        .catch((error: unknown) => rethrowHttp(error, (_e, status) => new OpsError(status)))
      if (!isHealth(body)) throw new Error('Unexpected response shape')
      return body
    },
    refetchInterval: QUEUE_DEPTH_POLL_MS,
  })
}

export function useOpsChannels() {
  return useQuery({
    queryKey: ['ops', 'channels', 'attention'],
    queryFn: async ({ signal }): Promise<OpsChannelListOut> => {
      const body: unknown = await api
        .get('/ops/channels', { params: { query: { attention: true } }, signal })
        .catch((error: unknown) => rethrowHttp(error, (_e, status) => new OpsError(status)))
      if (!isOpsChannelList(body)) throw new Error('Unexpected response shape')
      return body
    },
  })
}

/**
 * One page of GET /api/ops/jobs. The key carries every filter and the cursor.
 * While the page shows a running transcribe job it is refetched every 30 s (its
 * progress is stored that often); with none it is not polled. A hidden tab is not
 * polled either: TanStack pauses `refetchInterval` unless told otherwise.
 */
export function useOpsJobs(filters: OpsFilters, before: number | null) {
  return useQuery({
    queryKey: ['ops', 'jobs', filters.state, filters.kind, filters.errorClass, before],
    queryFn: async ({ signal }): Promise<JobListOut> => {
      const body: unknown = await api
        .get('/ops/jobs', { params: { query: jobsQuery(filters, before) }, signal })
        .catch((error: unknown) => rethrowHttp(error, (_e, status) => new OpsError(status)))
      if (!isJobList(body)) throw new Error('Unexpected response shape')
      return body
    },
    placeholderData: keepPreviousData,
    refetchInterval: (q) =>
      q.state.data?.items.some((j) => j.kind === 'transcribe' && j.state === 'running')
        ? PROGRESS_POLL_MS
        : false,
  })
}

/**
 * POST /api/ops/jobs/{id}/retry. Never retried automatically. Every HTTP status
 * (and a network failure) is turned into a fixed message and handed to
 * `onResult`, which also runs if the row that started it has since vanished.
 * After any HTTP response the job list and the depth are refetched; after a
 * network failure nothing is, since nothing is known to have changed.
 */
export function useRetryJob(onResult: (jobId: number, message: string) => void) {
  const client = useQueryClient()
  return useMutation({
    retry: 0,
    mutationFn: async (id: number): Promise<RetryOutcome> => {
      if (!isPositiveSafeInt(id)) throw new Error('Invalid job id')
      // An HTTP error or an unreadable 2xx body becomes an outcome; a network
      // failure rejects: the mutation errors, with no refetch.
      return api
        .post('/ops/jobs/{id}/retry', { params: { path: { id } }, body: {} })
        .then((body): RetryOutcome => ({ type: 'http', status: 200, body }))
        .catch((error: unknown): RetryOutcome => {
          if (error instanceof ApiError && (error.kind === 'http' || error.kind === 'parse')) {
            return {
              type: 'http',
              status: error.status ?? 200,
              body: error.body ?? undefined,
            }
          }
          throw error
        })
    },
    onSuccess: async (outcome, id) => {
      onResult(id, retryMessage(id, outcome))
      await client.invalidateQueries({ queryKey: ['ops'] })
    },
    onError: (_error, id) => onResult(id, retryMessage(id, { type: 'network' })),
  })
}

async function postBulkRetry(filters: OpsFilters, dryRun: boolean): Promise<BulkRetryOut> {
  const body: unknown = await api
    .post('/ops/jobs/retry', { body: bulkRetryBody(filters, dryRun) })
    .catch((error: unknown) => rethrowHttp(error, (_e, status) => new OpsError(status)))
  if (!isBulkRetryOut(body)) throw new Error('Unexpected response shape')
  return body
}

/**
 * POST /api/ops/jobs/retry (#161). `preview` is the dry run that counts the
 * matching dead jobs; `run` retries them and then refreshes everything under
 * ['ops']. Neither is retried automatically.
 */
export function useBulkRetry() {
  const client = useQueryClient()
  const preview = useMutation({ retry: 0, mutationFn: (filters: OpsFilters) => postBulkRetry(filters, true) })
  const run = useMutation({
    retry: 0,
    mutationFn: (filters: OpsFilters) => postBulkRetry(filters, false),
    onSuccess: () => client.invalidateQueries({ queryKey: ['ops'] }),
  })
  return { preview, run }
}
