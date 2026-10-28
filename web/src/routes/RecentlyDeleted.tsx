import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router'
import type { components } from '../api'
import { isRestoreConflict, restoreErrorMessage, useDeletedVideos } from '../hooks/useDeletedVideos'
import { useSubmitVideo } from '../hooks/useSubmitVideo'
import { ApiError } from '../hooks/useVideos'
import { canonicalVideoUrl } from '../lib/channelCatalog'
import { formatUtc } from '../lib/ops'

type DeletedVideoItem = components['schemas']['DeletedVideoItem']

export const DELETED_PAGE_SIZE = 50
const BUSY_TEXT = 'Still stopping its processing. Try again in a minute.'

function parsePage(raw: string | null): number {
  if (raw === null || !/^[1-9][0-9]{0,6}$/.test(raw)) return 1
  return Number(raw)
}

function itemTitle(item: DeletedVideoItem): string {
  return item.title === null || item.title === '' ? item.video_id : item.title
}

// The trash view of deletion tombstones (#236, design-system 7.14). Page state
// lives in the URL; TanStack Query owns the server state. `pageSize` is
// injectable so tests need only a few rows.
export function RecentlyDeleted({ pageSize = DELETED_PAGE_SIZE }: { pageSize?: number } = {}) {
  const [params, setParams] = useSearchParams()
  const page = parsePage(params.get('page'))
  const q = useDeletedVideos(page, pageSize)
  const [restored, setRestored] = useState<string | null>(null)
  const liveRef = useRef<HTMLParagraphElement>(null)
  useEffect(() => {
    if (restored !== null) liveRef.current?.focus()
  }, [restored])

  const data = q.data
  const offset = (page - 1) * pageSize
  const switching = q.isPlaceholderData
  const pastEnd = data !== undefined && data.total > 0 && data.items.length === 0
  const lastPage = data === undefined ? 1 : Math.max(1, Math.ceil(data.total / pageSize))
  const hasNext = data !== undefined && offset + data.items.length < data.total
  const goTo = (n: number) => setParams(n <= 1 ? {} : { page: String(n) })

  return (
    <section className="page">
      <h1>Recently deleted</h1>
      <p>
        Deleted videos are gone for good. Restoring one adds it again and processes it from the start; its
        earlier summaries are not recovered.
      </p>
      <p ref={liveRef} tabIndex={-1} aria-live="polite">
        {restored}
      </p>

      {q.isError ? (
        <div className="alert alert--danger" role="alert">
          <p>
            {q.error instanceof ApiError && q.error.status === 503
              ? 'The database is unavailable. Try again shortly.'
              : 'Could not load deleted videos'}
          </p>
          <button className="btn btn--primary" type="button" onClick={() => void q.refetch()}>
            Retry
          </button>
        </div>
      ) : (
        <>
          {(q.isFetching && data === undefined) || switching ? (
            <p className="muted" role="status">
              Loading…
            </p>
          ) : null}
          <p className="muted" role="status" aria-label="Results" aria-live="polite">
            {data !== undefined && data.items.length > 0
              ? `${offset + 1}–${offset + data.items.length} of ${data.total}`
              : ''}
          </p>
          {data !== undefined && data.total === 0 && <p>No deleted videos</p>}
          {pastEnd && (
            <p>
              <span>No videos on this page</span>{' '}
              <button className="btn" type="button" onClick={() => goTo(lastPage)}>
                Go to the last page
              </button>
            </p>
          )}

          {data !== undefined && data.items.length > 0 && (
            <div className="card card--flush table-scroll" role="region" aria-label="Deleted videos" tabIndex={0}>
              <table className="table">
                <thead>
                  <tr>
                    <th scope="col">Title</th>
                    <th scope="col">Channel</th>
                    <th scope="col">Deleted</th>
                    <th scope="col">Action</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((item) => (
                    <DeletedRow key={item.video_id} item={item} onRestored={setRestored} />
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <nav className="pagination" aria-label="Pagination">
            <button className="btn" type="button" disabled={page <= 1 || switching} onClick={() => goTo(page - 1)}>
              Previous
            </button>
            <button
              className="btn btn--primary"
              type="button"
              disabled={!hasNext || switching}
              onClick={() => goTo(page + 1)}
            >
              Next
            </button>
          </nav>
        </>
      )}
    </section>
  )
}

function DeletedRow({ item, onRestored }: { item: DeletedVideoItem; onRestored: (text: string) => void }) {
  const client = useQueryClient()
  const submit = useSubmitVideo()
  const [confirming, setConfirming] = useState(false)
  const [done, setDone] = useState(false)
  const [conflict, setConflict] = useState(false)
  const sentRef = useRef(false)
  const restoreRef = useRef<HTMLButtonElement>(null)
  const confirmRef = useRef<HTMLButtonElement>(null)
  const returnFocusRef = useRef(false)
  const title = itemTitle(item)
  const href = canonicalVideoUrl(item.video_id)
  const busy = submit.isPending

  useEffect(() => {
    if (confirming) confirmRef.current?.focus()
    else if (returnFocusRef.current) {
      returnFocusRef.current = false
      restoreRef.current?.focus()
    }
  }, [confirming])

  const close = () => {
    returnFocusRef.current = true
    setConfirming(false)
  }

  const confirm = () => {
    if (sentRef.current) return
    sentRef.current = true
    submit.mutate(
      { url: item.video_id },
      {
        onSuccess: () => {
          setDone(true)
          setConfirming(false)
          onRestored(`Restored ${title}: processing again`)
        },
        onError: (error) => {
          if (!isRestoreConflict(error)) return
          setConflict(true)
          setConfirming(false)
          void client.invalidateQueries({ queryKey: ['deleted-videos'] })
        },
        onSettled: () => {
          sentRef.current = false
        },
      },
    )
  }

  const blocked = conflict || !item.restorable
  return (
    <tr>
      <td>
        {title}
        {href !== null && (
          <>
            {' '}
            <a href={href} target="_blank" rel="noopener noreferrer">
              Open on YouTube
            </a>
          </>
        )}
      </td>
      <td>{item.channel_title ?? item.channel_id ?? 'Unknown channel'}</td>
      <td>
        <time dateTime={item.deleted_at}>{formatUtc(item.deleted_at)}</time>
      </td>
      <td
        onKeyDown={(e) => {
          if (e.key === 'Escape' && confirming && !busy) close()
        }}
      >
        <div aria-live="polite">
          {done ? (
            <>
              <span>Restored: processing again</span> <Link to={`/videos/${item.video_id}`}>Open video</Link>
            </>
          ) : blocked ? (
            <span>{BUSY_TEXT}</span>
          ) : null}
        </div>
        {!blocked && !done && !confirming && (
          <button
            ref={restoreRef}
            className="btn"
            type="button"
            aria-label={`Restore ${title}`}
            onClick={() => {
              submit.reset()
              setConfirming(true)
            }}
          >
            Restore
          </button>
        )}
        {confirming && (
          <div className="row">
            <p>Restore {title}? It is processed again from the start, which can take a while.</p>
            <button ref={confirmRef} className="btn btn--primary" type="button" disabled={busy} onClick={confirm}>
              Confirm restore
            </button>
            <button className="btn" type="button" disabled={busy} onClick={close}>
              Cancel
            </button>
            {submit.isError && (
              <p className="alert alert--danger" role="alert">
                {restoreErrorMessage(submit.error)}
              </p>
            )}
          </div>
        )}
      </td>
    </tr>
  )
}
