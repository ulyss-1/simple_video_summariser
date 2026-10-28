import { Fragment, useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router'
import type { components } from '../api'
import { AnalysisView } from '../components/AnalysisView'
import { JobProgress } from '../components/JobProgress'
import { Player } from '../components/Player'
import { buildWatchUrl } from '../components/player/youtubeTransport'
import { formatTimestamp, useMomentSeek } from '../components/Timestamp'
import { useAnalysisTotal } from '../hooks/useAnalyses'
import { cancelProcessingErrorMessage, useCancelProcessing } from '../hooks/useCancelProcessing'
import { deleteErrorMessage, useDeleteVideo } from '../hooks/useDeleteVideo'
import { useRestartProcessing } from '../hooks/useRestartProcessing'
import { reanalyzeErrorMessage, useReanalyze } from '../hooks/useReanalyze'
import { useVideo, VideoApiError } from '../hooks/useVideo'
import { parseStartTime } from '../lib/moment'
import { parseVideoId } from '../lib/videoId'

type Video = components['schemas']['VideoDetail']

const STAGES: Record<string, string> = {
  ingest: 'Fetching video details',
  transcribe: 'Transcribing',
  analyze: 'Analysing',
}

const UNAVAILABLE_NOTE = 'This video is no longer available on YouTube'

function pipelineStatus(v: Video): string {
  switch (v.status) {
    case 'processing': {
      if (v.active_job?.cancel_requested_at != null) return cancellingText(v.active_job.kind)
      const stage = (v.active_job && STAGES[v.active_job.kind]) ?? 'Processing'
      return v.active_job?.state === 'pending' ? `Queued: ${stage}` : stage
    }
    case 'failed': {
      const f = v.last_failure
      const detail = f ? ` (${[f.kind, f.error_class].filter((x) => x).join(', ')})` : ''
      return `Processing failed${detail}`
    }
    case 'unavailable':
      return UNAVAILABLE_NOTE
    default:
      return v.last_cancelled == null ? 'Not analysed yet' : cancelledText(v.last_cancelled.kind)
  }
}

function cancellingText(kind: string): string {
  const stage = STAGES[kind]
  return stage === undefined ? 'Cancelling…' : `Cancelling: ${stage}…`
}

const cancelledText = (kind: string) => `Processing cancelled (${kind})`

const CANCELLING_HINT =
  'This usually takes up to a minute: the worker learns of the request on its next heartbeat, then stops its download or model call.'

/** `YYYY-MM-DD` in UTC, or null when absent or unparsable. */
function utcDate(value: string | null): string | null {
  if (value === null) return null
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? null : d.toISOString().slice(0, 10)
}

function NotFound() {
  useEffect(() => {
    document.title = 'Video not found'
  }, [])
  return (
    <section className="page">
      <h1>Video not found</h1>
      <p>
        <Link className="btn" to="/" viewTransition>
          Back to Library
        </Link>
      </p>
    </section>
  )
}

/** Inline confirmation for deleting the video (design-system §7.7), never a modal. */
function DeleteVideo({
  videoId,
  name,
  processing,
  onGone,
}: {
  videoId: string
  name: string
  processing: boolean
  onGone: () => void
}) {
  const navigate = useNavigate()
  const del = useDeleteVideo(videoId, () => {
    onGone()
    return navigate('/', { state: { notice: 'Video deleted' } })
  })
  const [confirming, setConfirming] = useState(false)
  const openRef = useRef<HTMLButtonElement>(null)
  const cancelRef = useRef<HTMLButtonElement>(null)
  const restoreFocus = useRef(false)
  // Set synchronously on click: isPending only turns true on a later render.
  const sent = useRef(false)

  useEffect(() => {
    if (confirming) cancelRef.current?.focus()
    else if (restoreFocus.current) {
      restoreFocus.current = false
      openRef.current?.focus()
    }
  }, [confirming])

  const close = () => {
    restoreFocus.current = true
    del.reset()
    setConfirming(false)
  }

  const confirm = () => {
    if (sent.current) return
    sent.current = true
    del.mutate(undefined, {
      onSettled: () => {
        sent.current = false
      },
    })
  }

  if (!confirming) {
    return (
      <p>
        <button type="button" ref={openRef} onClick={() => setConfirming(true)}>
          Delete video
        </button>
      </p>
    )
  }
  return (
    <div
      onKeyDown={(e) => {
        if (e.key === 'Escape' && !del.isPending) close()
      }}
    >
      <p>Delete {name}?</p>
      <p>The transcript, summaries and stored audio will be permanently removed.</p>
      {processing && <p>Processing is stopped and any running work is discarded.</p>}
      {del.isError && <p role="alert">{deleteErrorMessage(del.error)}</p>}
      <button type="button" disabled={del.isPending} onClick={confirm}>
        Delete video
      </button>{' '}
      <button type="button" ref={cancelRef} disabled={del.isPending} onClick={close}>
        Cancel
      </button>
    </div>
  )
}

/**
 * Inline confirmation for stopping a video's jobs without deleting it (#300,
 * design-system §7.7), never a modal. The request lives in the page so that it
 * survives this control disappearing when the refetch shows the new state.
 */
function CancelProcessing({
  name,
  kind,
  cancel,
}: {
  name: string
  kind: string
  cancel: ReturnType<typeof useCancelProcessing>
}) {
  const [confirming, setConfirming] = useState(false)
  const openRef = useRef<HTMLButtonElement>(null)
  const keepRef = useRef<HTMLButtonElement>(null)
  const restoreFocus = useRef(false)
  // Set synchronously on click: isPending only turns true on a later render.
  const sent = useRef(false)

  useEffect(() => {
    if (confirming) keepRef.current?.focus()
    else if (restoreFocus.current) {
      restoreFocus.current = false
      openRef.current?.focus()
    }
  }, [confirming])

  const close = () => {
    restoreFocus.current = true
    cancel.reset()
    setConfirming(false)
  }

  const confirm = () => {
    if (sent.current) return
    sent.current = true
    cancel.mutate(undefined, {
      onSettled: () => {
        sent.current = false
      },
    })
  }

  if (!confirming) {
    return (
      <button type="button" ref={openRef} onClick={() => setConfirming(true)}>
        Cancel processing
      </button>
    )
  }
  return (
    <div
      onKeyDown={(e) => {
        if (e.key === 'Escape' && !cancel.isPending) close()
      }}
    >
      <p>Cancel processing of {name}? Nothing is deleted.</p>
      {kind === 'transcribe' && <p>A running transcription starts over if it is resumed.</p>}
      {cancel.isError && <p role="alert">{cancelProcessingErrorMessage(cancel.error)}</p>}
      <button type="button" disabled={cancel.isPending} onClick={confirm}>
        Confirm cancel
      </button>{' '}
      <button type="button" ref={keepRef} disabled={cancel.isPending} onClick={close}>
        Keep processing
      </button>
    </div>
  )
}

/**
 * Inline confirmation for restarting a cancelled video (#326, design-system
 * §7.7), never a modal. The page owns the alert text, since this control
 * disappears when the refetch shows the new state.
 */
function RestartProcessing({
  name,
  kind,
  restart,
}: {
  name: string
  kind: string
  restart: ReturnType<typeof useRestartProcessing>
}) {
  const [confirming, setConfirming] = useState(false)
  const openRef = useRef<HTMLButtonElement>(null)
  const confirmRef = useRef<HTMLButtonElement>(null)
  const restoreFocus = useRef(false)
  // Set synchronously on click: isPending only turns true on a later render.
  const sent = useRef(false)

  useEffect(() => {
    if (confirming) confirmRef.current?.focus()
    else if (restoreFocus.current) {
      restoreFocus.current = false
      openRef.current?.focus()
    }
  }, [confirming])

  const close = () => {
    restoreFocus.current = true
    setConfirming(false)
  }

  const confirm = () => {
    if (sent.current) return
    sent.current = true
    void restart.send(close).finally(() => {
      sent.current = false
    })
  }

  if (!confirming) {
    return (
      <button
        type="button"
        ref={openRef}
        onClick={() => {
          restart.clearNotice()
          setConfirming(true)
        }}
      >
        Restart processing
      </button>
    )
  }
  return (
    <div
      onKeyDown={(e) => {
        if (e.key === 'Escape' && !restart.isPending) close()
      }}
    >
      <p>Restart processing of {name}? It is queued again at the front of the queue.</p>
      {kind === 'transcribe' && <p>A transcription starts over.</p>}
      <button ref={confirmRef} type="button" disabled={restart.isPending} onClick={confirm}>
        Confirm restart
      </button>{' '}
      <button type="button" disabled={restart.isPending} onClick={close}>
        Keep cancelled
      </button>
    </div>
  )
}

export function VideoDetail() {
  const videoId = parseVideoId(useParams().videoId)
  if (videoId === null) return <NotFound />
  // Keyed by id so nothing from a previous video survives a navigation.
  return <VideoPage key={videoId} videoId={videoId} />
}

function VideoPage({ videoId }: { videoId: string }) {
  // Set once the video is deleted: the queries below stop watching it, so
  // removing their cache entries cannot make them fetch the video again.
  const [gone, setGone] = useState(false)
  const query = useVideo(gone ? null : videoId)
  const reanalyze = useReanalyze(videoId)
  // Bumped when a cancel has been answered and the page refetched: the status
  // line then takes focus, since the control that had it is gone.
  const [focusStatus, setFocusStatus] = useState(0)
  const cancel = useCancelProcessing(videoId, () => setFocusStatus((n) => n + 1))
  const restart = useRestartProcessing(videoId, () => setFocusStatus((n) => n + 1))
  const statusRef = useRef<HTMLParagraphElement>(null)
  useEffect(() => {
    if (focusStatus > 0) statusRef.current?.focus()
  }, [focusStatus])
  // Only the count is needed for the Compare link; a failure just hides it.
  const analysisTotal = useAnalysisTotal(gone ? null : videoId).data
  const video = query.data
  const heading = video ? video.title || videoId : null
  const [params] = useSearchParams()
  // The Player mounts with the loaded video, so the deep-link seek waits for it.
  useMomentSeek(video === undefined ? null : parseStartTime(params))

  useEffect(() => {
    if (heading !== null) document.title = heading
  }, [heading])

  // A 404 after earlier data (a cancel or poll found the video gone) ends the page too.
  if (query.isError && query.error instanceof VideoApiError && query.error.status === 404) return <NotFound />
  if (video === undefined) {
    if (query.isError) {
      return (
        <section className="page">
          <h1>Could not load this video</h1>
          <p className="alert alert--danger">Something went wrong. Try again.</p>
          <button
            className="btn btn--primary"
            type="button"
            onClick={() => {
              void query.refetch()
            }}
          >
            Retry
          </button>
        </section>
      )
    }
    return <p className="muted" role="status">Loading video…</p>
  }

  const channel = video.channel_title || video.channel_id
  const published = utcDate(video.published_at)
  const analysis = video.analysis
  const meta = [
    channel,
    published,
    video.duration_sec === null ? null : formatTimestamp(video.duration_sec),
  ].filter((x): x is string => x !== null && x !== '')

  return (
    <article className="page video-detail">
      <h1>{heading}</h1>
      {meta.length > 0 && (
        <p className="muted">
          {meta.map((m, i) => (
            <Fragment key={i}>
              {i > 0 && ' · '}
              <span>{m}</span>
            </Fragment>
          ))}
        </p>
      )}
      <p className="row video-detail__links">
        <a href={buildWatchUrl(videoId)} target="_blank" rel="noopener noreferrer">
          Open on YouTube
        </a>
        {video.transcript !== null && (
          <>
            {' · '}
            <Link to={`/videos/${videoId}/transcript`} viewTransition>Transcript</Link>
          </>
        )}
        {analysisTotal !== undefined && analysisTotal >= 2 && (
          <>
            {' · '}
            <Link to={`/videos/${videoId}/compare`} viewTransition>Compare analyses</Link>
          </>
        )}
        {analysis !== null && (
          <>
            {' · '}
            <a href={`/api/videos/${videoId}/render`} target="_blank" rel="noopener noreferrer">
              Printable version
            </a>
          </>
        )}
      </p>
      {video.transcript !== null && (
        <p>
          <button
            className="btn btn--primary"
            type="button"
            disabled={reanalyze.isPending || video.active_job !== null}
            onClick={() => reanalyze.mutate()}
          >
            Re-analyse
          </button>
        </p>
      )}
      {reanalyze.isError && (
        <p className="alert alert--danger" role="alert">
          {reanalyzeErrorMessage(reanalyze.error)}
        </p>
      )}
      <Player videoId={videoId} title={heading ?? undefined} />
      {/* Any non-null `unavailable`, including '', means unavailable. */}
      {video.unavailable !== null && analysis !== null && <p className="alert">{UNAVAILABLE_NOTE}</p>}
      {analysis !== null && video.active_job !== null && (
        <p className="alert" tabIndex={-1} ref={statusRef}>
          {video.active_job.cancel_requested_at == null ? 'Re-analysis in progress' : 'Cancelling re-analysis…'}
        </p>
      )}
      {analysis !== null && video.active_job === null && video.last_cancelled != null && (
        <p className="alert" tabIndex={-1} ref={statusRef}>{cancelledText(video.last_cancelled.kind)}</p>
      )}
      {analysis === null ? (
        <p className="alert" role="status" tabIndex={-1} ref={statusRef}>{pipelineStatus(video)}</p>
      ) : (
        <AnalysisView analysis={analysis} />
      )}
      {video.active_job?.cancel_requested_at != null && <p className="muted">{CANCELLING_HINT}</p>}
      {video.active_job !== null && video.progress != null && (
        <div>
          <JobProgress kind={video.active_job.kind} state={video.active_job.state} progress={video.progress} />
        </div>
      )}
      {video.status === 'idle' && video.active_job === null && video.last_cancelled != null && (
        <p>
          <RestartProcessing name={heading ?? videoId} kind={video.last_cancelled.kind} restart={restart} />
        </p>
      )}
      {restart.notice !== null && (
        <p className="alert alert--danger" role="alert">
          {restart.notice}
        </p>
      )}
      <div className="row">
        {video.active_job !== null && video.active_job.cancel_requested_at == null && (
          <CancelProcessing name={heading ?? videoId} kind={video.active_job.kind} cancel={cancel} />
        )}
        <DeleteVideo
          videoId={videoId}
          name={heading ?? videoId}
          processing={video.active_job !== null}
          onGone={() => setGone(true)}
        />
      </div>
    </article>
  )
}
