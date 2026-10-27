import { useEffect, useRef, useState } from 'react'
import { Link, useLocation, useNavigate, useSearchParams } from 'react-router'
import { AddVideoForm } from '../components/AddVideoForm'
import { AddVideosForm } from '../components/AddVideosForm'
import { BackfillChannelForm } from '../components/BackfillChannelForm'
import { bulkDeleteErrorMessage, useBulkDeleteVideos } from '../hooks/useBulkDeleteVideos'
import { useChannels } from '../hooks/useChannels'
import { ApiError, useVideos } from '../hooks/useVideos'
import {
  hasFilters,
  isReversedRange,
  isValidDate,
  MAX_OFFSET,
  PAGE_SIZE,
  parseLibraryParams,
  publishedDate,
  statusFilterLabel,
  statusLabel,
  STATUSES,
  toSearchParams,
  type LibraryState,
  type SortKey,
  type SortOrder,
  type VideoSort,
  type VideoStatus,
} from '../lib/library'

// The notices a delete elsewhere may hand over: #232 (video), #340 (channel).
const NOTICE = /^(Video deleted|Channel deleted( · \d+ videos? removed)?)$/
function isNotice(value: unknown): value is string {
  return typeof value === 'string' && NOTICE.test(value)
}

// Semantic HTML first; styling is class hooks into web/index.css (design-system
// §11, #120). Filter and page state lives in the URL only; TanStack Query owns
// the server state (architecture §9).
export function Library() {
  const [params, setParams] = useSearchParams()
  const { state, invalid } = parseLibraryParams(params)

  // One-shot notice passed by a delete on the video page (#232). It is read
  // once, then dropped from the history entry so a reload does not repeat it.
  const location = useLocation()
  const navigate = useNavigate()
  const [notice] = useState(() => {
    const value = (location.state as { notice?: unknown } | null)?.notice
    return isNotice(value) ? value : null
  })
  const hasNotice = location.state !== null && location.state !== undefined
  useEffect(() => {
    if (hasNotice) void navigate({ pathname: location.pathname, search: location.search }, { replace: true, state: null })
  }, [hasNotice, location.pathname, location.search, navigate])

  // An invalid parameter is dropped from the URL (replace, not push). This
  // render already ignores it, so nothing invalid is ever requested.
  const invalidKeys = invalid.join(',')
  useEffect(() => {
    if (invalidKeys === '') return
    const next = new URLSearchParams(params)
    for (const key of invalidKeys.split(',')) next.delete(key)
    setParams(next, { replace: true })
  }, [invalidKeys, params, setParams])

  const reversed = isReversedRange(state)
  const q = useVideos(state, !reversed)
  const channels = useChannels()

  // Bulk-delete selection (#234): route-local, never in the URL. A change of
  // page, filter or sort drops it (reset while rendering, so no stale frame),
  // and only IDs still on screen count, so nothing off-screen can be deleted.
  const bulk = useBulkDeleteVideos()
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set())
  const [confirming, setConfirming] = useState(false)
  const [result, setResult] = useState<string | null>(null)
  const pageKey = toSearchParams(state).toString()
  const [selectedKey, setSelectedKey] = useState(pageKey)
  if (selectedKey !== pageKey) {
    setSelectedKey(pageKey)
    setSelected(new Set())
    setConfirming(false)
    setResult(null)
  }
  const sentRef = useRef(false)
  const resultRef = useRef<HTMLParagraphElement>(null)
  const actionRef = useRef<HTMLButtonElement>(null)
  const cancelRef = useRef<HTMLButtonElement>(null)
  const returnFocusRef = useRef(false)

  const rows = q.data?.items ?? []
  const selectedRows = rows.filter((v) => selected.has(v.video_id))
  const selectedIds = selectedRows.map((v) => v.video_id)
  const count = selectedIds.length
  const processingCount = selectedRows.filter((v) => v.status === 'processing').length
  const confirmOpen = confirming && count > 0
  const busy = bulk.isPending

  useEffect(() => {
    if (confirmOpen) cancelRef.current?.focus()
    else if (returnFocusRef.current) {
      returnFocusRef.current = false
      actionRef.current?.focus()
    }
  }, [confirmOpen])

  useEffect(() => {
    if (result !== null) resultRef.current?.focus()
  }, [result])

  const toggle = (videoId: string) => {
    setResult(null)
    setSelected((current) => {
      const next = new Set(current)
      if (!next.delete(videoId)) next.add(videoId)
      return next
    })
  }

  const openConfirm = () => {
    bulk.reset()
    setConfirming(true)
  }

  const closeConfirm = () => {
    returnFocusRef.current = true
    setConfirming(false)
  }

  const confirmDelete = () => {
    if (sentRef.current) return
    sentRef.current = true
    bulk.mutate(selectedIds, {
      onSuccess: (response) => {
        const { deleted, not_found: notFound } = response.counts
        setResult(
          `Deleted ${deleted} video${deleted === 1 ? '' : 's'}` +
            (notFound > 0 ? ` · ${notFound} were already deleted` : ''),
        )
        setSelected(new Set())
        setConfirming(false)
      },
      onSettled: () => {
        sentRef.current = false
      },
    })
  }

  const go = (patch: Partial<LibraryState>) => {
    setParams(toSearchParams({ ...state, page: 1, ...patch }))
  }

  const sortBy = (sort: VideoSort) => {
    const index = state.sorts.findIndex((pair) => pair.sort === sort)
    if (index < 0) go({ sorts: [...state.sorts, { sort, order: defaultOrder(sort) }] })
    else go({ sorts: state.sorts.map((pair, i) => (i === index ? { ...pair, order: opposite(pair.order) } : pair)) })
  }

  const moveSort = (index: number, delta: -1 | 1) => {
    const sorts = [...state.sorts]
    const other = index + delta
    ;[sorts[index], sorts[other]] = [sorts[other] as SortKey, sorts[index] as SortKey]
    go({ sorts })
  }

  const removeSort = (index: number) => {
    const sorts = state.sorts.filter((_pair, i) => i !== index)
    go({ sorts: sorts.length === 0 ? [{ sort: 'published', order: 'desc' }] : sorts })
  }

  const data = q.data
  const offset = (state.page - 1) * PAGE_SIZE
  const switching = q.isPlaceholderData
  const pastEnd = data !== undefined && data.total > 0 && data.items.length === 0
  const lastPage = data === undefined ? 1 : Math.min(Math.ceil(data.total / PAGE_SIZE), MAX_OFFSET / PAGE_SIZE + 1)
  const hasNext =
    data !== undefined && offset + data.items.length < data.total && state.page * PAGE_SIZE <= MAX_OFFSET

  const channelTitle =
    state.channel === null
      ? null
      : (data?.items.find((v) => v.channel_id === state.channel && v.channel_title !== null)
          ?.channel_title ?? null)

  const channelItems = channels.data?.items ?? []
  const selectedListed = channelItems.some((c) => c.channel_id === state.channel)

  return (
    <section className="page">
      <h1>Library</h1>
      <p aria-live="polite">{notice}</p>

      <AddVideoForm />
      <AddVideosForm />
      <p>
        <Link to="/playlists/import">Import a playlist</Link>
      </p>
      <BackfillChannelForm />

      <form className="card filters" onSubmit={(e) => e.preventDefault()}>
        <label className="field">
          From{' '}
          <input
            className="input"
            type="date"
            value={state.from ?? ''}
            onChange={(e) => onDate('from', e.target.value)}
          />
        </label>
        <label className="field">
          To{' '}
          <input
            className="input"
            type="date"
            value={state.to ?? ''}
            onChange={(e) => onDate('to', e.target.value)}
          />
        </label>
        <label className="field">
          Status{' '}
          <select
            className="input"
            value={state.status ?? ''}
            onChange={(e) => go({ status: (e.target.value || null) as VideoStatus | null })}
          >
            <option value="">Any</option>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {statusFilterLabel(s)}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          Channel{' '}
          <select
            className="input"
            value={state.channel ?? ''}
            disabled={channels.isPending}
            onChange={(e) => go({ channel: e.target.value || null })}
          >
            <option value="">All channels</option>
            {state.channel !== null && !selectedListed && (
              <option value={state.channel}>{channelTitle ?? state.channel}</option>
            )}
            {channelItems.map((c) => (
              <option key={c.channel_id} value={c.channel_id}>
                {c.title ?? c.channel_id}
              </option>
            ))}
          </select>
        </label>
        {hasFilters(state) && (
          <button
            className="btn"
            type="button"
            onClick={() => setParams(toSearchParams({ ...state, channel: null, from: null, to: null, status: null, page: 1 }))}
          >
            Clear all filters
          </button>
        )}
      </form>

      <div className="card" role="group" aria-label="Sort order">
        <strong>Sort order</strong>
        <ol>
          {state.sorts.map((pair, index) => (
            <li key={pair.sort}>
              {index + 1} {sortLabel(pair.sort)} {direction(pair.order)}{' '}
              <button
                className="btn"
                type="button"
                aria-label={`Move ${sortLabel(pair.sort)} earlier`}
                disabled={index === 0}
                onClick={() => moveSort(index, -1)}
              >
                Earlier
              </button>{' '}
              <button
                className="btn"
                type="button"
                aria-label={`Move ${sortLabel(pair.sort)} later`}
                disabled={index === state.sorts.length - 1}
                onClick={() => moveSort(index, 1)}
              >
                Later
              </button>{' '}
              <button
                className="btn"
                type="button"
                aria-label={`Remove ${sortLabel(pair.sort)} from sort order`}
                onClick={() => removeSort(index)}
              >
                Remove
              </button>
            </li>
          ))}
        </ol>
        {!isDefaultSort(state.sorts) && (
          <button
            className="btn"
            type="button"
            aria-label="Reset sort to Published descending"
            onClick={() => go({ sorts: [{ sort: 'published', order: 'desc' }] })}
          >
            Reset sort
          </button>
        )}
      </div>

      {reversed && (
        <p className="alert alert--danger" role="alert">
          'From' is after 'To'
        </p>
      )}

      {state.channel !== null && (
        <div className="row" role="group" aria-label="Active filters">
          Channel: <strong>{channelTitle ?? state.channel}</strong>{' '}
          <button className="btn" type="button" onClick={() => go({ channel: null })}>
            Clear
          </button>{' '}
          <Link to={`/channels/${state.channel}/catalog`}>Manage channel</Link>
        </div>
      )}

      {q.isError ? (
        <div className="alert alert--danger" role="alert">
          <p>
            {q.error instanceof ApiError && q.error.status === 503
              ? 'The database is unavailable. Try again shortly.'
              : 'Could not load videos'}
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

          {data !== undefined && data.total === 0 && !hasFilters(state) && (
            <p>Nothing has been processed yet</p>
          )}
          {data !== undefined && data.total === 0 && hasFilters(state) && (
            <p>No videos match these filters</p>
          )}
          {pastEnd && (
            <p>
              <span>No videos on this page</span>{' '}
              <Link to={{ search: toSearchParams({ ...state, page: lastPage }).toString() }}>
                Go to the last page
              </Link>
            </p>
          )}

          {data !== undefined && (data.total > 0 || result !== null) && (
            <div className="card" role="group" aria-label="Selection">
              <div className="row">
                <button
                  className="btn"
                  type="button"
                  disabled={busy || switching || rows.length === 0}
                  onClick={() => {
                    setResult(null)
                    setSelected(new Set(rows.map((v) => v.video_id)))
                  }}
                >
                  Select all on this page
                </button>
                <button
                  className="btn"
                  type="button"
                  disabled={busy || count === 0}
                  onClick={() => setSelected(new Set())}
                >
                  Clear selection
                </button>
                <button
                  ref={actionRef}
                  className="btn btn--primary"
                  type="button"
                  disabled={busy || count === 0 || confirmOpen}
                  onClick={openConfirm}
                >
                  {count === 0 ? 'Delete selected videos' : `Delete ${count} selected video${count === 1 ? '' : 's'}`}
                </button>
              </div>
              <p ref={resultRef} tabIndex={-1} aria-live="polite">
                {result ?? (count === 0 ? 'No videos selected' : `${count} selected`)}
              </p>
              {confirmOpen && (
                <div
                  className="row"
                  onKeyDown={(e) => {
                    if (e.key === 'Escape' && !busy) closeConfirm()
                  }}
                >
                  <p>
                    Delete {count} video{count === 1 ? '' : 's'}? Their transcripts, summaries and stored audio
                    will be permanently removed. RSS and channel backfill will not bring them back; adding a
                    video by its URL processes it again.
                    {processingCount > 0 && ` Processing of ${processingCount} of them is stopped.`}
                  </p>
                  <button className="btn btn--primary" type="button" disabled={busy} onClick={confirmDelete}>
                    Confirm delete
                  </button>
                  <button ref={cancelRef} className="btn" type="button" disabled={busy} onClick={closeConfirm}>
                    Cancel
                  </button>
                </div>
              )}
              {confirmOpen && bulk.isError && (
                <p className="alert alert--danger" role="alert">
                  {bulkDeleteErrorMessage(bulk.error)}
                </p>
              )}
            </div>
          )}

          {data !== undefined && data.items.length > 0 && (
            <div className="card card--flush table-scroll" role="region" aria-label="Videos" tabIndex={0}>
              <table className="table">
                <thead>
                  <tr>
                    <th scope="col">Select</th>
                    <SortHeader label="Title" sort="title" state={state} onSort={sortBy} />
                    <SortHeader label="Channel" sort="channel" state={state} onSort={sortBy} />
                    <SortHeader label="Published" sort="published" state={state} onSort={sortBy} />
                    <SortHeader label="Status" sort="status" state={state} onSort={sortBy} />
                    <th scope="col">Transcript</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((v) => {
                    const status = statusLabel(v)
                    const id = v.channel_id
                    return (
                      <tr key={v.video_id}>
                        <td>
                          <input
                            type="checkbox"
                            aria-label={`Select ${v.title === null || v.title === '' ? v.video_id : v.title}`}
                            checked={selected.has(v.video_id)}
                            disabled={busy}
                            onChange={() => toggle(v.video_id)}
                          />
                        </td>
                        <td>
                          <Link to={`/videos/${v.video_id}`} viewTransition>{v.title ?? v.video_id}</Link>
                        </td>
                        <td>
                          {id !== null ? (
                            <button className="btn" type="button" onClick={() => go({ channel: id })}>
                              {v.channel_title ?? id}
                            </button>
                          ) : (
                            (v.channel_title ?? 'Unknown channel')
                          )}
                        </td>
                        <td>{publishedDate(v.published_at) ?? 'Date unknown'}</td>
                        <td>
                          <span className={`badge badge--${statusTone(v.status)}`}>
                            <span>{status.label}</span>
                            {status.detail !== null && (
                              <>
                                {' '}
                                (<span>{status.detail}</span>)
                              </>
                            )}
                          </span>
                        </td>
                        <td>
                          {v.has_transcript ? (
                            <Link to={`/videos/${v.video_id}/transcript`}>Transcript</Link>
                          ) : (
                            <span className="muted">Not yet</span>
                          )}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}

          <nav className="pagination" aria-label="Pagination">
            <button
              className="btn"
              type="button"
              disabled={state.page <= 1 || switching || reversed}
              onClick={() => go({ page: state.page - 1 })}
            >
              Previous
            </button>
            <button
              className="btn btn--primary"
              type="button"
              disabled={!hasNext || switching || reversed}
              onClick={() => go({ page: state.page + 1 })}
            >
              Next
            </button>
          </nav>
        </>
      )}
    </section>
  )

  function onDate(key: 'from' | 'to', value: string) {
    if (value === '') go({ [key]: null })
    else if (isValidDate(value)) go({ [key]: value })
  }
}

interface SortHeaderProps {
  label: string
  sort: VideoSort
  state: Pick<LibraryState, 'sorts'>
  onSort: (sort: VideoSort) => void
}

function SortHeader({ label, sort, state, onSort }: SortHeaderProps) {
  const index = state.sorts.findIndex((pair) => pair.sort === sort)
  const pair = index < 0 ? null : state.sorts[index] as SortKey
  const action = pair === null ? defaultOrder(sort) : opposite(pair.order)
  return (
    <th scope="col" aria-sort={index === 0 && pair !== null ? direction(pair.order) : undefined}>
      <button
        className="btn"
        type="button"
        onClick={() => onSort(sort)}
        aria-label={pair === null ? `Append ${label} ${direction(action)}` : `Toggle ${label} to ${direction(action)}`}
      >
        {label}
        {pair !== null ? ` ${index + 1} ${direction(pair.order)}` : ''}
      </button>
    </th>
  )
}

function sortLabel(sort: VideoSort): string {
  return sort.charAt(0).toUpperCase() + sort.slice(1)
}

function isDefaultSort(sorts: readonly SortKey[]): boolean {
  return sorts.length === 1 && sorts[0]?.sort === 'published' && sorts[0].order === 'desc'
}

function defaultOrder(sort: VideoSort): SortOrder {
  return sort === 'published' ? 'desc' : 'asc'
}

function opposite(order: SortOrder): SortOrder {
  return order === 'asc' ? 'desc' : 'asc'
}

function direction(order: SortOrder): 'ascending' | 'descending' {
  return order === 'asc' ? 'ascending' : 'descending'
}

type StatusTone = 'success' | 'warning' | 'danger' | 'neutral'

// Badge colour per video status; unknown and idle states stay neutral.
function statusTone(status: VideoStatus): StatusTone {
  switch (status) {
    case 'done':
      return 'success'
    case 'processing':
      return 'warning'
    case 'failed':
      return 'danger'
    default:
      return 'neutral'
  }
}
