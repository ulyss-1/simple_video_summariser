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
 * invalid time, and plain text when there is no player to seek.
 */
export function Timestamp({ seconds }: { seconds: number | null | undefined }) {
  const player = usePlayer()
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return null

  const label = formatTimestamp(seconds)
  const time = <time dateTime={toIsoDuration(seconds)}>{label}</time>
  if (player === null) return time
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
