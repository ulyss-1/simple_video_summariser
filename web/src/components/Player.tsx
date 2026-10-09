import { useEffect, useRef, useState } from 'react'
import { parseVideoId } from '../lib/videoId'
import { usePlayerInternals } from './PlayerContext'
import {
  buildEmbedUrl,
  buildWatchUrl,
  createTransport,
  type Transport,
} from './player/youtubeTransport'

/**
 * One YouTube embed, controlled through the `usePlayer()` context. `videoId`
 * is untrusted (route or API) and is validated before any URL is built.
 */
export function Player({ videoId, title }: { videoId: string; title?: string }) {
  const id = parseVideoId(videoId)
  if (id === null) return <UnavailablePlayer />
  // Keyed by id: a new video gets a fresh iframe, transport and seek queue.
  return <EmbedPlayer key={id} videoId={id} title={title} />
}

function UnavailablePlayer() {
  const internals = usePlayerInternals()
  const register = internals?.register
  const setStatus = internals?.setStatus

  useEffect(() => {
    // A seek in this state does nothing, and in particular opens no tab.
    register?.({ seek: () => {} })
    setStatus?.('error')
    return () => {
      register?.(null)
      setStatus?.('loading')
    }
  }, [register, setStatus])

  return <div className="player player--error">Video unavailable</div>
}

function EmbedPlayer({ videoId, title }: { videoId: string; title: string | undefined }) {
  const internals = usePlayerInternals()
  const register = internals?.register
  const setStatus = internals?.setStatus
  const iframeRef = useRef<HTMLIFrameElement>(null)
  const transportRef = useRef<Transport | null>(null)
  const [phase, setPhase] = useState<'loading' | 'ready' | 'error'>('loading')

  useEffect(() => {
    const iframe = iframeRef.current
    if (iframe === null) return
    const transport = createTransport({
      iframe,
      win: window,
      onReady: () => {
        setPhase('ready')
      },
      onError: () => {
        setPhase('error')
      },
    })
    transportRef.current = transport
    return () => {
      transport.dispose()
      transportRef.current = null
    }
  }, [])

  useEffect(() => {
    register?.({
      seek: (seconds) => {
        if (phase === 'error') {
          window.open(buildWatchUrl(videoId, seconds), '_blank', 'noopener')
        } else {
          transportRef.current?.seek(seconds)
        }
      },
    })
    return () => {
      register?.(null)
    }
  }, [register, phase, videoId])

  useEffect(() => {
    setStatus?.(phase)
    return () => {
      setStatus?.('loading')
    }
  }, [setStatus, phase])

  if (phase === 'error') {
    return (
      <div className="player player--error">
        <p>This video cannot be played here.</p>
        <a href={buildWatchUrl(videoId)} target="_blank" rel="noopener noreferrer">
          Watch on YouTube
        </a>
      </div>
    )
  }

  return (
    <div className="player">
      <iframe
        ref={iframeRef}
        src={buildEmbedUrl(videoId, window.location.origin)}
        title={title ? `Video: ${title}` : 'YouTube video player'}
        allow="autoplay; encrypted-media; picture-in-picture; fullscreen"
        allowFullScreen
        referrerPolicy="strict-origin-when-cross-origin"
        style={{ width: '100%', aspectRatio: '16 / 9' }}
        onLoad={() => {
          transportRef.current?.start()
        }}
      />
    </div>
  )
}
