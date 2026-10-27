import { Fragment, useId, useRef, useState, type FormEvent } from 'react'
import { type components } from '../api'
import { batchSubmitErrorMessage, useSubmitVideosBatch } from '../hooks/useSubmitVideosBatch'

type BatchSubmitResponse = components['schemas']['BatchSubmitResponse']
type Outcome = components['schemas']['BatchSubmitItem']['outcome']

/** The server's cap on one request (MAX_BATCH_SUBMIT, #341). A prop so a test can lower it. */
export const MAX_BATCH_LINES = 200

const SUMMARY: readonly { key: keyof BatchSubmitResponse['counts']; label: string }[] = [
  { key: 'queued', label: 'Queued' },
  { key: 'restarted', label: 'Restarted' },
  { key: 'already_submitted', label: 'Already added' },
  { key: 'duplicate', label: 'Repeated in the list' },
  { key: 'invalid', label: 'Not a video' },
  { key: 'cancellation_in_progress', label: 'Still cancelling' },
  { key: 'failed', label: 'Failed' },
]

const PROBLEM: Partial<Record<Outcome, string>> = {
  invalid: 'is not a YouTube video URL or ID',
  cancellation_in_progress: 'is still being cancelled; try again shortly',
  failed: 'could not be saved; try again',
}

const LINE_PREVIEW = 60

interface Entry {
  line: number
  text: string
}

// The non-blank lines with their 1-based line numbers, so a per-item outcome
// (indexed over the sent list) maps back to what the person sees.
function entriesOf(value: string): Entry[] {
  const entries: Entry[] = []
  value.split(/\r\n|\r|\n/).forEach((raw, i) => {
    const text = raw.trim()
    if (text !== '') entries.push({ line: i + 1, text })
  })
  return entries
}

interface Props {
  maxLines?: number
}

// Several videos at once (#240): one URL or ID per line, one outcome per line.
// Messages are fixed text; a line is echoed only as React text, never from the server.
export function AddVideosForm({ maxLines = MAX_BATCH_LINES }: Props) {
  const [value, setValue] = useState('')
  const [local, setLocal] = useState<string | null>(null)
  const [report, setReport] = useState<{ response: BatchSubmitResponse; entries: Entry[] } | null>(null)
  const areaRef = useRef<HTMLTextAreaElement>(null)
  // The list in flight, then the list that succeeded. A submit of that exact
  // text is ignored until it is edited or a request fails: the latch is set
  // before the request, kept after success (so a repeat click between the
  // settle and the re-render cannot send it twice) and cleared only on error.
  const sentRef = useRef<string | null>(null)
  const mutation = useSubmitVideosBatch()
  const errorId = useId()
  const hintId = useId()
  const message = local ?? (mutation.isError ? batchSubmitErrorMessage(mutation.error) : null)

  const clearFeedback = () => {
    setLocal(null)
    setReport(null)
    mutation.reset()
  }

  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (sentRef.current === value) return
    const entries = entriesOf(value)
    if (entries.length === 0) {
      setLocal('Enter at least one video URL or ID, one per line.')
      areaRef.current?.focus()
      return
    }
    if (entries.length > maxLines) {
      setLocal(`Add at most ${maxLines} videos at a time; the list has ${entries.length}.`)
      areaRef.current?.focus()
      return
    }
    sentRef.current = value
    clearFeedback()
    mutation.mutate(
      entries.map((e) => e.text),
      {
        onSuccess: (response) => setReport({ response, entries }),
        onError: () => {
          sentRef.current = null
          areaRef.current?.focus()
        },
      },
    )
  }

  const problems =
    report === null
      ? []
      : report.response.results.flatMap((item) => {
          const reason = PROBLEM[item.outcome]
          const entry = report.entries[item.index]
          return reason === undefined || entry === undefined ? [] : [{ item, entry, reason }]
        })
  const counts = report?.response.counts
  const parts = SUMMARY.filter(({ key }) => (counts?.[key] ?? 0) > 0)

  return (
    <form className="card" onSubmit={onSubmit}>
      <label className="field">
        Add several videos: YouTube URLs or IDs, one per line
        <textarea
          ref={areaRef}
          className="input input--multiline"
          rows={4}
          value={value}
          aria-invalid={message === null ? undefined : true}
          aria-describedby={message === null ? hintId : `${hintId} ${errorId}`}
          onChange={(event) => {
            setValue(event.target.value)
            sentRef.current = null
            clearFeedback()
          }}
        />
      </label>
      <p id={hintId} className="muted">
        Blank lines are skipped. Up to {maxLines} at a time.
      </p>
      <button className="btn btn--primary" type="submit" disabled={mutation.isPending}>
        {mutation.isPending ? 'Adding…' : 'Add videos'}
      </button>
      {message !== null && (
        <p id={errorId} className="alert alert--danger" role="alert">
          {message}
        </p>
      )}
      <p aria-live="polite">
        {report !== null &&
          parts.map(({ key, label }, i) => (
            <Fragment key={key}>
              {i > 0 && ' · '}
              <span>{`${label} ${counts?.[key] ?? 0}`}</span>
            </Fragment>
          ))}
      </p>
      {problems.length > 0 && (
        <ul aria-label="Lines that need attention">
          {problems.map(({ item, entry, reason }) => (
            <li key={item.index}>
              Line {entry.line}
              {entry.text.length > LINE_PREVIEW ? '' : ` (${entry.text})`} {reason}
            </li>
          ))}
        </ul>
      )}
    </form>
  )
}
