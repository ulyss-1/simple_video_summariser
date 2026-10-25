import { useRef, useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router'
import { useRegisterChannel } from '../hooks/useRegisterChannel'
import { registerErrorMessage } from '../lib/channelCatalog'

const ERROR_ID = 'add-channel-error'
const HINT_ID = 'add-channel-hint'

// Register a channel (POST /channels, unchanged) and open its catalog (#242).
// The message under the field is fixed text: nothing the user typed or the
// server answered is echoed.
export function AddChannel() {
  const [value, setValue] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)
  const pendingRef = useRef(false)
  const navigate = useNavigate()
  const mutation = useRegisterChannel()
  const message = mutation.isError ? registerErrorMessage(mutation.error) : null

  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (pendingRef.current) return
    pendingRef.current = true
    mutation.reset()
    mutation.mutate(
      { url: value.trim() },
      {
        onSuccess: (channelId) => {
          void navigate(`/channels/${channelId}/catalog`)
        },
        onError: () => inputRef.current?.focus(),
        onSettled: () => {
          pendingRef.current = false
        },
      },
    )
  }

  return (
    <section className="page">
      <h1>Add channel</h1>
      <form className="card" onSubmit={onSubmit}>
        <label className="field">
          YouTube channel{' '}
          <input
            ref={inputRef}
            className="input"
            type="text"
            value={value}
            aria-invalid={message === null ? undefined : true}
            aria-describedby={message === null ? HINT_ID : `${HINT_ID} ${ERROR_ID}`}
            onChange={(event) => {
              setValue(event.target.value)
              mutation.reset()
            }}
          />
        </label>
        <p id={HINT_ID} className="muted">
          A channel ID, a youtube.com/channel/ URL, an @handle, or a /c/ or /user/ URL.
        </p>
        <button className="btn btn--primary" type="submit" disabled={mutation.isPending}>
          {mutation.isPending ? 'Adding…' : 'Add channel'}
        </button>
        {message !== null && (
          <p id={ERROR_ID} className="alert alert--danger" role="alert">
            {message}
          </p>
        )}
      </form>
    </section>
  )
}
