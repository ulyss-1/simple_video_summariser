import { createContext, use, useRef, useState, type ReactNode } from 'react'

// The one place the player handle lives (architecture.md section 9). Views see
// only `usePlayer()`; `Player` registers itself through the internal half.

export type PlayerStatus = 'loading' | 'ready' | 'error'

export interface PlayerHandle {
  seekTo: (seconds: number) => void
  status: PlayerStatus
}

/** What a mounted `Player` plugs into the provider. */
export interface PlayerController {
  seek: (seconds: number) => void
}

export interface PlayerContextValue extends PlayerHandle {
  register: (controller: PlayerController | null) => void
  setStatus: (status: PlayerStatus) => void
}

export const PlayerContext = createContext<PlayerContextValue | null>(null)

export function PlayerProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<PlayerStatus>('loading')
  const controller = useRef<PlayerController | null>(null)

  const value: PlayerContextValue = {
    status,
    setStatus,
    seekTo: (seconds) => {
      controller.current?.seek(seconds)
    },
    register: (next) => {
      controller.current = next
    },
  }
  return <PlayerContext value={value}>{children}</PlayerContext>
}

/** `null` outside a provider, so callers can fall back instead of throwing. */
export function usePlayer(): PlayerHandle | null {
  const ctx = use(PlayerContext)
  if (ctx === null) return null
  return { seekTo: ctx.seekTo, status: ctx.status }
}

/** For `Player` only. */
export function usePlayerInternals(): PlayerContextValue | null {
  return use(PlayerContext)
}
