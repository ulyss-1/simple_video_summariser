import { useEffect, useId, useRef, useState } from 'react'
import { Link, useParams } from 'react-router'
import {
  useChannelCatalog,
  useDiscoverCatalog,
  useProcessCatalogSelection,
  useRetryCatalogMetadata,
} from '../hooks/useChannelCatalog'
import {
  canonicalVideoUrl,
  catalogErrorMessage,
  CATALOG_RANGES,
  DEFAULT_CATALOG_LIMIT,
  formatCatalogDate,
  knownLabel,
  parseChannelId,
  processButtonLabel,
  processErrorMessage,
  retryMetadataErrorMessage,
  type BulkCatalogResponse,
  type BulkCatalogResult,
  type CatalogItem,
  type CatalogLimit,
} from '../lib/channelCatalog'

/** Longer descriptions are cut to this many characters until the reader expands them. */
const DESCRIPTION_PREVIEW_CHARS = 240

// Browse a registered channel's uploads and queue a hand-picked subset (#242).
// TanStack Query owns the catalog and the mutations; the selected ids are local
// to this route (architecture §9). Everything the API sends renders as a React
// text child, never as HTML.
export function ChannelCatalog() {
  const { channelId: raw } = useParams()
  const channelId = parseChannelId(raw)
  if (channelId === null) {
    return (
      <section className="page">
        <h1>Channel not found</h1>
        <p>
          <Link to="/channels/add">Add a channel</Link> · <Link to="/">Back to Library</Link>
        </p>
      </section>
    )
  }
  return <CatalogView key={channelId} channelId={channelId} />
}

function CatalogView({ channelId }: { channelId: string }) {
  const discover = useDiscoverCatalog(channelId)
  const retryMetadata = useRetryCatalogMetadata(channelId)
  const processSelection = useProcessCatalogSelection(channelId)

  const [shownLimit, setShownLimit] = useState<CatalogLimit | null>(null)
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set())
  const [confirming, setConfirming] = useState(false)
  const [results, setResults] = useState<BulkCatalogResponse | null>(null)

  const catalog = useChannelCatalog(channelId, shownLimit)

  const startedRef = useRef(false)
  const sentRef = useRef(false)
  const confirmRef = useRef<HTMLButtonElement>(null)
  const actionRef = useRef<HTMLButtonElement>(null)
  const returnFocusRef = useRef(false)

  // Discover once on arrival, at the default range. The ref also keeps a
  // development double effect from listing twice.
  useEffect(() => {
    if (startedRef.current) return
    startedRef.current = true
    discover.mutate(DEFAULT_CATALOG_LIMIT, {
      onSuccess: (page) => {
        setShownLimit(page.limit)
      },
    })
  }, [discover])

  useEffect(() => {
    if (confirming) confirmRef.current?.focus()
    else if (returnFocusRef.current) {
      returnFocusRef.current = false
      actionRef.current?.focus()
    }
  }, [confirming])

  const loadRange = (limit: CatalogLimit) => {
    discover.mutate(limit, {
      onSuccess: (page) => {
        setShownLimit(page.limit)
        // Keep a selected id only while it is still a new row of the loaded range.
        const keep = new Set(page.items.filter((item) => !item.known).map((item) => item.video_id))
        setSelected((current) => new Set([...current].filter((id) => keep.has(id))))
      },
    })
  }

  const items = catalog.data?.items ?? []
  const selectedIds = items
    .filter((item) => !item.known && selected.has(item.video_id))
    .map((item) => item.video_id)
  const busy = discover.isPending || processSelection.isPending
  const requested = discover.isPending ? (discover.variables ?? shownLimit) : shownLimit

  const toggle = (videoId: string) => {
    setSelected((current) => {
      const next = new Set(current)
      if (!next.delete(videoId)) next.add(videoId)
      return next
    })
  }

  const cancelConfirm = () => {
    returnFocusRef.current = true
    setConfirming(false)
  }

  const confirm = () => {
    if (sentRef.current) return
    sentRef.current = true
    const ids = selectedIds
    processSelection.mutate(ids, {
      onSuccess: (response) => {
        // Every submitted id has a result; only now is the selection released.
        setResults(response)
        setSelected((current) => new Set([...current].filter((id) => !ids.includes(id))))
      },
      onSettled: () => {
        sentRef.current = false
        returnFocusRef.current = true
        setConfirming(false)
      },
    })
  }

  const newIds = items.filter((item) => !item.known).map((item) => item.video_id)
  const total = catalog.data?.total_count ?? null

  return (
    <section className="page">
      <h1>Channel catalog</h1>
      <p className="muted">
        Channel <code>{channelId}</code> · <Link to={`/?channel=${channelId}`}>Videos from this channel</Link> ·{' '}
        <Link to="/channels/add">Add another channel</Link>
      </p>

      <div className="row" role="group" aria-label="Range">
        {CATALOG_RANGES.map(([label, limit]) => (
          <button
            key={limit}
            className={requested === limit ? 'btn btn--primary' : 'btn'}
            type="button"
            aria-pressed={requested === limit}
            disabled={busy}
            onClick={() => loadRange(limit)}
          >
            {label}
          </button>
        ))}
      </div>
      <p className="muted">Show all lists at most 5,000 uploads, newest first.</p>

      {discover.isPending && (
        <p role="status">{catalog.data === undefined ? 'Loading catalog…' : 'Loading range…'}</p>
      )}

      {discover.isError && (
        <div className="alert alert--danger" role="alert">
          <p>{catalogErrorMessage(discover.error)}</p>
          <button
            className="btn"
            type="button"
            onClick={() => loadRange(discover.variables ?? DEFAULT_CATALOG_LIMIT)}
          >
            Retry
          </button>
        </div>
      )}

      {retryMetadata.isError && (
        <p className="alert alert--danger" role="alert">
          {retryMetadataErrorMessage(retryMetadata.error)}
        </p>
      )}

      {catalog.isError && catalog.data !== undefined && (
        <div className="alert alert--danger" role="alert">
          <p>Could not refresh the catalog.</p>
          <button className="btn" type="button" onClick={() => void catalog.refetch()}>
            Retry
          </button>
        </div>
      )}

      {catalog.data !== undefined && (
        <>
          <p>
            {total === null
              ? `${items.length} uploads listed`
              : `Showing ${items.length} of ${total} uploads`}{' '}
            · {newIds.length} new
          </p>

          <div className="card" role="group" aria-label="Selection">
            <div className="row">
              <button
                className="btn"
                type="button"
                disabled={busy || newIds.length === 0}
                onClick={() => setSelected(new Set(newIds))}
              >
                Select all new
              </button>
              <button
                className="btn"
                type="button"
                disabled={busy || selectedIds.length === 0}
                onClick={() => setSelected(new Set())}
              >
                Clear selection
              </button>
              <button
                ref={actionRef}
                className="btn btn--primary"
                type="button"
                disabled={busy || selectedIds.length === 0 || confirming}
                onClick={() => setConfirming(true)}
              >
                {selectedIds.length === 0 ? 'Process selected videos' : processButtonLabel(selectedIds.length)}
              </button>
            </div>
            <p aria-live="polite">
              {selectedIds.length === 0 ? 'No videos selected' : `${selectedIds.length} selected`}
            </p>
            {confirming && (
              <div className="row">
                <p>
                  Process {selectedIds.length} selected video{selectedIds.length === 1 ? '' : 's'} from this
                  channel? Processing may use existing subtitles or download audio and transcribe when needed.
                </p>
                <button
                  ref={confirmRef}
                  className="btn btn--primary"
                  type="button"
                  disabled={processSelection.isPending}
                  onClick={confirm}
                >
                  Confirm processing
                </button>
                <button className="btn" type="button" disabled={processSelection.isPending} onClick={cancelConfirm}>
                  Cancel
                </button>
              </div>
            )}
            {processSelection.isError && (
              <p className="alert alert--danger" role="alert">
                {processErrorMessage(processSelection.error)}
              </p>
            )}
          </div>

          {items.length === 0 ? (
            <p>No uploads were listed for this channel.</p>
          ) : (
            <div className="table-scroll">
              <table className="table catalog-table">
                <caption className="muted">Channel uploads, newest first</caption>
                <thead>
                  <tr>
                    <th scope="col">Select</th>
                    <th scope="col">Title</th>
                    <th scope="col">Description</th>
                    <th scope="col">Published</th>
                    <th scope="col">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((item, index) => (
                    <CatalogRow
                      key={item.video_id}
                      item={item}
                      index={index}
                      selected={selected.has(item.video_id)}
                      disabled={busy}
                      retrying={retryMetadata.isPending && retryMetadata.variables === item.video_id}
                      onToggle={() => toggle(item.video_id)}
                      onRetry={() => retryMetadata.mutate(item.video_id)}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}

      <ProcessingResults results={results} />
    </section>
  )
}

function CatalogRow({
  item,
  index,
  selected,
  disabled,
  retrying,
  onToggle,
  onRetry,
}: {
  item: CatalogItem
  index: number
  selected: boolean
  disabled: boolean
  retrying: boolean
  onToggle: () => void
  onRetry: () => void
}) {
  const name = item.title ?? item.video_id
  const href = canonicalVideoUrl(item.video_id)
  const date = formatCatalogDate(item.published_at)
  const failed = item.enrichment_state === 'failed'
  const checked = selected && !item.known
  const classes = [
    'catalog-row',
    index % 2 === 0 ? 'catalog-row--even' : 'catalog-row--odd',
    checked ? 'catalog-row--selected' : '',
    item.known ? 'catalog-row--known' : '',
    failed ? 'catalog-row--failed' : '',
  ]
    .filter(Boolean)
    .join(' ')

  return (
    <tr className={classes}>
      <td>
        <input
          type="checkbox"
          aria-label={`Select ${name}`}
          checked={checked}
          disabled={item.known || disabled}
          onChange={onToggle}
        />
      </td>
      <th scope="row" className="catalog-title">
        {href === null ? (
          name
        ) : (
          <a href={href} target="_blank" rel="noopener noreferrer">
            {name}
          </a>
        )}
      </th>
      <td className="catalog-description">
        {item.enrichment_state === 'done' ? (
          <Description text={item.description} />
        ) : failed ? (
          <>
            <span>Metadata unavailable</span>{' '}
            <button
              className="btn"
              type="button"
              aria-label={`Retry metadata for ${name}`}
              disabled={retrying}
              onClick={onRetry}
            >
              {retrying ? 'Retrying…' : 'Retry'}
            </button>
          </>
        ) : (
          <span className="muted">Loading metadata…</span>
        )}
      </td>
      <td>
        {item.enrichment_state === 'done' && date !== null && item.published_at !== null ? (
          <time dateTime={item.published_at}>{date}</time>
        ) : item.enrichment_state === 'done' ? (
          'Date unknown'
        ) : (
          '—'
        )}
      </td>
      <td>{item.known ? knownLabel(item) : checked ? 'New · Selected' : 'New'}</td>
    </tr>
  )
}

function Description({ text }: { text: string | null }) {
  const [expanded, setExpanded] = useState(false)
  const id = useId()
  if (text === null || text.trim() === '') return <span className="muted">No description</span>
  const long = text.length > DESCRIPTION_PREVIEW_CHARS
  return (
    <>
      <span id={id} className="catalog-description-text">
        {long && !expanded ? `${text.slice(0, DESCRIPTION_PREVIEW_CHARS)}…` : text}
      </span>
      {long && (
        <>
          {' '}
          <button
            className="btn"
            type="button"
            aria-expanded={expanded}
            aria-controls={id}
            onClick={() => setExpanded((value) => !value)}
          >
            {expanded ? 'Show less' : 'Show more'}
          </button>
        </>
      )}
    </>
  )
}

function resultText(result: BulkCatalogResult): string {
  switch (result.outcome) {
    case 'queued':
      return `Queued: ${result.video_id}`
    case 'already_known':
      return `Already known: ${result.video_id}`
    case 'rejected':
      return `Rejected: ${result.video_id} — not in this channel catalog`
  }
}

function ProcessingResults({ results }: { results: BulkCatalogResponse | null }) {
  const headingId = useId()
  const count = (outcome: BulkCatalogResult['outcome']) =>
    results?.results.filter((r) => r.outcome === outcome).length ?? 0
  return (
    <>
      <p aria-live="polite">
        {results !== null &&
          `Queued ${count('queued')} · Already known ${count('already_known')} · Rejected ${count('rejected')}`}
      </p>
      {results !== null && (
        <section aria-labelledby={headingId}>
          <h2 id={headingId}>Processing results</h2>
          <ul>
            {results.results.map((result) => (
              <li key={result.video_id}>{resultText(result)}</li>
            ))}
          </ul>
        </section>
      )}
    </>
  )
}
