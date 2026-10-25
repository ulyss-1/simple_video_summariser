import { useEffect, useRef } from 'react'
import { Link } from 'react-router'
import { buildMomentHref } from '../lib/moment'
import { usePlayer } from './PlayerContext'

/** `M:SS` below one hour, `H:MM:SS` from one hour. Plain arithmetic: no Date, no Intl. */
export function formatTimestamp(seconds: number): string {
  const total = Number.isFinite(seconds) ? Math.max(0, Math.floor(seconds)) : 0
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  const ss = String(s).padStart(2, '0')
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${ss}`
  return `${m}:${ss}`
}

/** ISO 8601 duration for the `datetime` attribute, e.g. `PT1H2M3S`. */
function toIsoDuration(seconds: number): string {
  const total = Math.floor(seconds)
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  return `PT${h > 0 ? `${h}H` : ''}${h > 0 || m > 0 ? `${m}M` : ''}${s}S`
}

/**
 * The single click-to-seek primitive. Renders nothing for a missing or
 * invalid time. With no player it is a link to `/videos/:videoId?t=` when a
 * `videoId` is given (#129), otherwise plain text.
 */
export function Timestamp({
  seconds,
  videoId,
}: {
  seconds: number | null | undefined
  videoId?: string
}) {
  const player = usePlayer()
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return null

  const label = formatTimestamp(seconds)
  const time = <time dateTime={toIsoDuration(seconds)}>{label}</time>
  if (player === null) {
    const href = videoId === undefined ? null : buildMomentHref(videoId, seconds)
    if (href === null) return time
    return (
      <Link to={href} className="timestamp" aria-label={`Open at ${label}`}>
        {time}
      </Link>
    )
  }
  return (
    <button
      type="button"
      className="timestamp"
      aria-label={`Seek to ${label}`}
      onClick={() => {
        player.seekTo(seconds)
      }}
    >
      {time}
    </button>
  )
}

/**
 * Deep-link seek-on-load (#129). Lives here so this module keeps the only
 * literal seek call (sourceScan). It goes through the same `usePlayer().seekTo`
 * as a click, so #48's before-ready queue times it. Fires once per distinct
 * value, and never while the embed is in the `error` state (that seek would
 * open a tab). Call it where the `Player` is mounted (a child's register effect
 * runs before this one); pass null until then.
 */
export function useMomentSeek(seconds: number | null): void {
  const player = usePlayer()
  const failed = player?.status === 'error'
  const seekTo = player?.seekTo
  const done = useRef<number | null>(null)

  useEffect(() => {
    if (seconds === null) {
      done.current = null
      return
    }
    if (seekTo === undefined || failed || done.current === seconds) return
    done.current = seconds
    seekTo(seconds)
  }, [seconds, seekTo, failed])
}
