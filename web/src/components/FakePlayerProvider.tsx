import type { ReactNode } from 'react'
import { PlayerContext, type PlayerContextValue, type PlayerStatus } from './PlayerContext'

/**
 * Test double for views (#50, #51, #53): records every seek into `seeks` and
 * reports whatever `status` it is given. No iframe is involved.
 */
export function FakePlayerProvider({
  children,
  seeks = [],
  status = 'ready',
}: {
  children: ReactNode
  seeks?: number[]
  status?: PlayerStatus
}) {
  const value: PlayerContextValue = {
    status,
    seekTo: (seconds) => {
      seeks.push(seconds)
    },
    register: () => {},
    setStatus: () => {},
  }
  return <PlayerContext value={value}>{children}</PlayerContext>
}
