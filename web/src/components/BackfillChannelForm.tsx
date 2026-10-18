import { useEffect, useId, useRef, useState, type FormEvent } from 'react'
import { useBackfillChannel } from '../hooks/useBackfillChannel'
import { useChannels } from '../hooks/useChannels'
import {
  backfillErrorMessage,
  backfillEstimateText,
  backfillOutcomeMessage,
  DEFAULT_BACKFILL_LIMIT,
  MAX_BACKFILL_LIMIT,
  MIN_BACKFILL_LIMIT,
  parseBackfillLimit,
  type BackfillResponse,
} from '../lib/backfill'

// Picks a registered channel (canonical IDs from GET /channels), dry-runs, then
// confirms inline before the real run: the #161 pattern, no window.confirm.
export function BackfillChannelForm() {
  const id = useId()
  const channels = useChannels()
  const { preview, run } = useBackfillChannel()
  const [channelId, setChannelId] = useState('')
  const [limitText, setLimitText] = useState(String(DEFAULT_BACKFILL_LIMIT))
  const [pending, setPending] = useState<{ out: BackfillResponse; limit: number; channelId: string } | null>(null)
  const [note, setNote] = useState<{ text: string; error: boolean } | null>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const confirmRef = useRef<HTMLButtonElement>(null)
  const restoreFocus = useRef(false)
  // Set synchronously on click: isPending only turns true on a later tick.
  const sent = useRef(false)
  const confirming = pending !== null
  const busy = preview.isPending || run.isPending
  const items = channels.data?.items ?? []
  const limit = parseBackfillLimit(limitText)
  const limitInvalid = limit === null

  // Focus follows the confirmation: into it when it opens, back out on Cancel.
  useEffect(() => {
    if (confirming) confirmRef.current?.focus()
    else if (restoreFocus.current) {
      restoreFocus.current = false
      triggerRef.current?.focus()
    }
  }, [confirming])

  const fail = (error: unknown) => setNote({ text: backfillErrorMessage(error), error: true })
  const nameOf = (cid: string) => items.find((c) => c.channel_id === cid)?.title ?? cid

  const open = (e: FormEvent) => {
    e.preventDefault()
    if (sent.current || confirming || channelId === '' || limit === null) return
    sent.current = true
    setNote(null)
    preview.mutate(
      { channelId, limit },
      {
        onSuccess: (out) => {
          if (out.new_videos === 0) setNote({ text: 'Nothing new to back-fill for this channel.', error: false })
          else setPending({ out, limit, channelId })
        },
        onError: fail,
        onSettled: () => {
          sent.current = false
        },
      },
    )
  }

  const confirm = () => {
    if (sent.current || pending === null) return
    sent.current = true
    run.mutate(
      { channelId: pending.channelId, limit: pending.limit },
      {
        onSuccess: (out) => setNote({ text: backfillOutcomeMessage(out), error: false }),
        onError: fail,
        onSettled: () => {
          sent.current = false
          setPending(null)
        },
      },
    )
  }

  const n = pending?.out.new_videos ?? 0

  return (
    <form className="card filters" onSubmit={open} noValidate>
      <label className="field" htmlFor={`${id}-channel`}>
        Channel to back-fill{' '}
        <select
          id={`${id}-channel`}
          className="input"
          value={channelId}
          disabled={busy || confirming || channels.isPending}
          onChange={(e) => {
            setChannelId(e.target.value)
            setNote(null)
          }}
        >
          <option value="">Choose a channel</option>
          {items.map((c) => (
            <option key={c.channel_id} value={c.channel_id}>
              {c.title ?? c.channel_id}
            </option>
          ))}
        </select>
      </label>
      <label className="field" htmlFor={`${id}-limit`}>
        Newest uploads to consider ({MIN_BACKFILL_LIMIT}–{MAX_BACKFILL_LIMIT}){' '}
        <input
          id={`${id}-limit`}
          className="input"
          type="text"
          inputMode="numeric"
          value={limitText}
          disabled={busy || confirming}
          aria-invalid={limitInvalid}
          aria-describedby={limitInvalid ? `${id}-limit-err` : undefined}
          onChange={(e) => {
            setLimitText(e.target.value)
            setNote(null)
          }}
        />
      </label>
      {limitInvalid && (
        <p id={`${id}-limit-err`} className="alert alert--danger" role="alert">
          Enter a whole number from {MIN_BACKFILL_LIMIT} to {MAX_BACKFILL_LIMIT}
        </p>
      )}
      {channels.isError && (
        <p className="alert alert--danger" role="alert">
          Could not load channels
        </p>
      )}
      {confirming ? (
        <div className="ops__confirm">
          <p>
            Back-fill {n} {n === 1 ? 'video' : 'videos'} for {nameOf(pending.channelId)}?
            {backfillEstimateText(pending.out)} Transcribing them can take hours of CPU.
          </p>
          <button className="btn btn--primary" type="button" ref={confirmRef} disabled={busy} onClick={confirm}>
            {run.isPending ? 'Backfilling…' : 'Confirm backfill'}
          </button>{' '}
          <button
            className="btn"
            type="button"
            disabled={busy}
            onClick={() => {
              restoreFocus.current = true
              setPending(null)
            }}
          >
            Cancel
          </button>
        </div>
      ) : (
        <button
          className="btn btn--primary"
          type="submit"
          ref={triggerRef}
          disabled={busy || channelId === '' || limitInvalid}
        >
          {preview.isPending ? 'Checking…' : 'Backfill'}
        </button>
      )}
      <p className={note?.error ? 'alert alert--danger' : 'muted'} role={note?.error ? 'alert' : undefined} aria-live="polite">
        {note?.text ?? ''}
      </p>
    </form>
  )
}
