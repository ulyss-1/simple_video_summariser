import { useEffect, useId, useRef, useState, type FormEvent } from 'react'
import { Link } from 'react-router'
import { type components } from '../api'
import { batchSubmitErrorMessage, useSubmitVideosBatch } from '../hooks/useSubmitVideosBatch'
import { MAX_BATCH_SUBMIT } from '../lib/batchSubmit'
import { parseVideoId } from '../lib/videoId'

type BatchSubmitResponse = components['schemas']['BatchSubmitResponse']
type Outcome = components['schemas']['BatchSubmitItem']['outcome']

const LINE_PREVIEW = 60

interface Entry {
  line: number
  text: string
}

// One submitted line and what the server said about it.
interface Row extends Entry {
  outcome: Outcome
  videoId: string | null
  sameAsLine: number | null
}

// The non-blank, trimmed lines with their 1-based line numbers.
function entriesOf(value: string): Entry[] {
  const entries: Entry[] = []
  value.split(/\r\n|\r|\n/).forEach((raw, i) => {
    const text = raw.trim()
    if (text !== '') entries.push({ line: i + 1, text })
  })
  return entries
}

// Outcomes are indexed over the list that was sent, so map them back by position.
function rowsFrom(sent: readonly Entry[], response: BatchSubmitResponse): Row[] {
  return sent.map((entry, index) => {
    const item = response.results.find((r) => r.index === index)
    const first = item?.first_index === undefined || item.first_index === null ? undefined : sent[item.first_index]
    return {
      ...entry,
      outcome: item?.outcome ?? 'failed',
      videoId: item?.video_id ?? null,
      sameAsLine: first?.line ?? null,
    }
  })
}

const needsRetry = (row: Row) => row.outcome === 'failed' || row.outcome === 'cancellation_in_progress'
const needsFix = (row: Row) => row.outcome === 'invalid' || needsRetry(row)

interface Props {
  maxLines?: number
}

// Several videos at once (#240): one URL or ID per line, one result per line.
// Messages are fixed text; a line is echoed only as a React text child.
export function AddVideosForm({ maxLines = MAX_BATCH_SUBMIT }: Props) {
  const [open, setOpen] = useState(false)
  const [value, setValue] = useState('')
  const [local, setLocal] = useState<string | null>(null)
  const [rows, setRows] = useState<Row[] | null>(null)
  const [sending, setSending] = useState(0)
  const areaRef = useRef<HTMLTextAreaElement>(null)
  // Synchronous mirrors of what the person sees. A handler from an older render
  // compares its own copy with these and does nothing when they differ, so the
  // window between a response settling and the re-render cannot send twice.
  const valueRef = useRef('')
  const rowsRef = useRef<Row[] | null>(null)
  const busyRef = useRef(false)
  // The text that was sent (or put back after a success). It stays set after a
  // success, so the same text cannot be sent again; it is cleared when the text
  // changes, including the programmatic replacement, or when a request fails.
  const latchRef = useRef<string | null>(null)
  const mutation = useSubmitVideosBatch()
  const panelId = useId()
  const errorId = useId()
  const hintId = useId()
  const message = local ?? (mutation.isError ? batchSubmitErrorMessage(mutation.error) : null)
  const pending = mutation.isPending

  useEffect(() => {
    if (message !== null && !pending) areaRef.current?.focus()
  }, [message, pending])

  const setText = (text: string) => {
    valueRef.current = text
    setValue(text)
  }

  const finish = (next: Row[]) => {
    rowsRef.current = next
    setRows(next)
    // Keep only the lines the person can fix or resend.
    const keep = next.filter(needsFix).map((r) => r.text).join('\n')
    latchRef.current = keep
    setText(keep)
  }

  const send = (entries: Entry[], merge: (response: BatchSubmitResponse) => Row[]) => {
    busyRef.current = true
    setLocal(null)
    mutation.reset()
    setSending(entries.length)
    mutation.mutate(
      entries.map((e) => e.text),
      {
        onSuccess: (response) => {
          busyRef.current = false
          finish(merge(response))
        },
        onError: () => {
          busyRef.current = false
          latchRef.current = null
        },
      },
    )
  }

  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const current = valueRef.current
    if (busyRef.current || value !== current) return
    if (current !== '' && latchRef.current === current) return
    const entries = entriesOf(current)
    if (entries.length === 0) {
      setLocal('Enter at least one video URL or ID')
      return
    }
    if (entries.length > maxLines) {
      setLocal(`At most ${maxLines} videos at a time`)
      return
    }
    latchRef.current = current
    send(entries, (response) => rowsFrom(entries, response))
  }

  const retry = () => {
    const before = rowsRef.current
    if (busyRef.current || before === null || before !== rows) return
    const again = before.filter(needsRetry)
    if (again.length === 0) return
    send(again, (response) => {
      const fresh = rowsFrom(again, response)
      return before.map((row) => fresh.find((f) => f.line === row.line) ?? row)
    })
  }

  const count = (...outcomes: Outcome[]) => rows?.filter((r) => outcomes.includes(r.outcome)).length ?? 0
  const summary = [
    { label: 'Queued', n: count('queued', 'restarted') },
    { label: 'Already added', n: count('already_submitted') },
    { label: 'Not valid', n: count('invalid') },
  ].filter((part) => part.n > 0)

  return (
    <div className="card">
      <button
        className="btn"
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen(!open)}
      >
        Add several videos
      </button>
      {open && (
        <form id={panelId} onSubmit={onSubmit}>
          <label className="field">
            YouTube video URLs or IDs, one per line
            <textarea
              ref={areaRef}
              className="input input--multiline"
              rows={4}
              value={value}
              disabled={pending}
              aria-invalid={message === null ? undefined : true}
              aria-describedby={message === null ? hintId : `${hintId} ${errorId}`}
              onChange={(event) => {
                setText(event.target.value)
                latchRef.current = null
                setLocal(null)
                mutation.reset()
              }}
            />
          </label>
          <p id={hintId} className="muted">
            Blank lines are skipped. Up to {maxLines} at a time.
          </p>
          <button className="btn btn--primary" type="submit" disabled={pending}>
            Add videos
          </button>
          {pending && (
            <p className="muted" role="status">
              {sending === 1 ? 'Adding 1 video…' : `Adding ${sending} videos…`}
            </p>
          )}
          {message !== null && (
            <p id={errorId} className="alert alert--danger" role="alert">
              {message}
            </p>
          )}
          <p aria-live="polite">
            {summary.map((part) => `${part.label} ${part.n}`).join(' · ')}
          </p>
          {rows !== null && (
            <ul aria-label="Results" aria-live="polite">
              {rows.map((row) => (
                <li key={row.line}>
                  <RowText row={row} />
                </li>
              ))}
            </ul>
          )}
          {rows?.some(needsRetry) === true && (
            <button className="btn" type="button" disabled={pending} onClick={retry}>
              Retry these
            </button>
          )}
        </form>
      )}
    </div>
  )
}

function RowText({ row }: { row: Row }) {
  const id = row.videoId === null ? null : parseVideoId(row.videoId)
  const open =
    id === null ? null : (
      <>
        {' '}
        <Link to={`/videos/${id}`}>Open</Link>
      </>
    )
  switch (row.outcome) {
    case 'queued':
    case 'restarted':
      return (
        <>
          Queued: {id ?? row.line}
          {open}
        </>
      )
    case 'already_submitted':
      return (
        <>
          Already added: {id ?? row.line}
          {open}
        </>
      )
    case 'duplicate':
      return <>{`Line ${row.line}: same video as line ${row.sameAsLine ?? '?'}`}</>
    case 'invalid':
      return <>{`Line ${row.line}: not a YouTube video URL or ID (${row.text.slice(0, LINE_PREVIEW)}…)`}</>
    case 'cancellation_in_progress':
      return <>{`${id ?? `Line ${row.line}`}: still being deleted, try again in a minute`}</>
    case 'failed':
      return <>{`${id ?? `Line ${row.line}`}: could not be added`}</>
  }
}
