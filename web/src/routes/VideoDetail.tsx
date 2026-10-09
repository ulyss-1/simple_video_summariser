import { Fragment, useEffect } from 'react'
import { Link, useParams } from 'react-router'
import type { components } from '../api'
import { AnalysisView } from '../components/AnalysisView'
import { Player } from '../components/Player'
import { buildWatchUrl } from '../components/player/youtubeTransport'
import { formatTimestamp } from '../components/Timestamp'
import { useVideo, VideoApiError } from '../hooks/useVideo'
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
      return 'Not analysed yet'
  }
}

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
    <section>
      <h1>Video not found</h1>
      <p>
        <Link to="/">Back to Library</Link>
      </p>
    </section>
  )
}

export function VideoDetail() {
  const videoId = parseVideoId(useParams().videoId)
  if (videoId === null) return <NotFound />
  // Keyed by id so nothing from a previous video survives a navigation.
  return <VideoPage key={videoId} videoId={videoId} />
}

function VideoPage({ videoId }: { videoId: string }) {
  const query = useVideo(videoId)
  const video = query.data
  const heading = video ? video.title || videoId : null

  useEffect(() => {
    if (heading !== null) document.title = heading
  }, [heading])

  if (video === undefined) {
    if (query.isError) {
      if (query.error instanceof VideoApiError && query.error.status === 404) return <NotFound />
      return (
        <section>
          <h1>Could not load this video</h1>
          <p>Something went wrong. Try again.</p>
          <button
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
    return <p role="status">Loading video…</p>
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
    <article>
      <h1>{heading}</h1>
      {meta.length > 0 && (
        <p>
          {meta.map((m, i) => (
            <Fragment key={i}>
              {i > 0 && ' · '}
              <span>{m}</span>
            </Fragment>
          ))}
        </p>
      )}
      <p>
        <a href={buildWatchUrl(videoId)} target="_blank" rel="noopener noreferrer">
          Open on YouTube
        </a>
        {analysis !== null && (
          <>
            {' · '}
            <a href={`/api/videos/${videoId}/render`} target="_blank" rel="noopener noreferrer">
              Printable version
            </a>
          </>
        )}
      </p>
      <Player videoId={videoId} title={heading ?? undefined} />
      {video.unavailable !== null && analysis !== null && <p>{UNAVAILABLE_NOTE}</p>}
      {analysis !== null && video.active_job !== null && <p>Re-analysis in progress</p>}
      {analysis === null ? <p role="status">{pipelineStatus(video)}</p> : <AnalysisView analysis={analysis} />}
    </article>
  )
}
