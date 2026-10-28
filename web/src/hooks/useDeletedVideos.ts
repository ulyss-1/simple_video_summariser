import { keepPreviousData, useQuery } from '@tanstack/react-query'
import type { components } from '../api'
import { api, ApiError as ClientError, rethrowHttp } from '../api'
import { ApiError } from './useVideos'

export type DeletedVideoList = components['schemas']['DeletedVideoList']

function isDeletedVideoList(v: unknown): v is DeletedVideoList {
  if (typeof v !== 'object' || v === null) return false
  const o = v as Record<string, unknown>
  return Array.isArray(o.items) && typeof o.total === 'number'
}

/** One page of GET /api/deleted-videos (#239). The key starts with `deleted-videos` so a restore can invalidate it. */
export function useDeletedVideos(page: number, pageSize: number) {
  const query = { offset: (page - 1) * pageSize, limit: pageSize }
  return useQuery({
    queryKey: ['deleted-videos', query],
    queryFn: async ({ signal }): Promise<DeletedVideoList> => {
      const body: unknown = await api
        .get('/deleted-videos', { params: { query }, signal })
        .catch((error: unknown) => rethrowHttp(error, (_e, status) => new ApiError(status)))
      if (!isDeletedVideoList(body)) throw new Error('Unexpected response shape')
      return body
    },
    placeholderData: keepPreviousData,
  })
}

function httpStatus(error: unknown): number | undefined {
  return error instanceof ClientError && error.kind === 'http' ? error.status : undefined
}

/** Fixed texts for a failed restore. Nothing from the response is shown. */
export function restoreErrorMessage(error: unknown): string {
  switch (httpStatus(error)) {
    case 422:
      return 'Could not restore the video. Reload the page and try again.'
    case 429:
      return 'Too many requests. Try again shortly.'
    case 503:
      return 'The database is unavailable. Try again shortly.'
    default:
      return 'Could not restore the video. Check your connection and try again.'
  }
}

/** 409: the video's deletion is still stopping its job (state changed since the list loaded). */
export function isRestoreConflict(error: unknown): boolean {
  return httpStatus(error) === 409
}
