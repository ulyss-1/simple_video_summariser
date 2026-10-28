import { useState } from 'react'
import { ApiError } from '../api'
import { useSubmitVideo } from './useSubmitVideo'

const CHANGED = 'Nothing to restart: the video changed. Showing its current state.'

/** Fixed, user-facing text for a failed restart. Nothing from the response or the exception is used. */
export function restartErrorMessage(error: unknown): string {
  if (error instanceof ApiError && error.kind === 'http') {
    switch (error.status) {
      case 404:
        return 'This video no longer exists.'
      case 429:
        return 'Too many requests. Try again shortly.'
      case 503:
        return 'The database is unavailable. Try again shortly.'
    }
  }
  return 'Could not restart processing. Check your connection and try again.'
}

/**
 * Restart a cancelled video from its detail page (#326): one `POST /videos`
 * through `useSubmitVideo` (#304 revives the newest cancelled job in place),
 * which also refetches the video. `send` calls `onSettled` for every outcome
 * (the caller closes its confirmation); `onRestarted` runs after a
 * `restarted: true` answer and the refetch. `restarted: false` and any error
 * set a fixed notice, owned here so it outlives the control.
 */
export function useRestartProcessing(videoId: string, onRestarted: () => void) {
  const submit = useSubmitVideo()
  const [notice, setNotice] = useState<string | null>(null)

  const send = async (onSettled: () => void): Promise<void> => {
    setNotice(null)
    try {
      const result = await submit.mutateAsync({
        url: `https://www.youtube.com/watch?v=${videoId}`,
      })
      onSettled()
      if (result.restarted === true) onRestarted()
      else setNotice(CHANGED)
    } catch (error) {
      setNotice(restartErrorMessage(error))
      onSettled()
    }
  }

  return {
    send,
    isPending: submit.isPending,
    notice,
    clearNotice: () => setNotice(null),
  }
}
