// Test-only fixture components. Lives under src/test/, never src/components/,
// so it cannot be mistaken for app code or imported by the app (#127).
import { useEffect, useState } from 'react'

export function Greeting({ name }: { name: string }) {
  return (
    <section>
      <h1>Hello, {name}</h1>
      <button type="button">Wave</button>
    </section>
  )
}

/** Updates itself after a timeout: exercises fake timers with waitFor. */
export function DelayedStatus({ delayMs }: { delayMs: number }) {
  const [ready, setReady] = useState(false)
  useEffect(() => {
    const id = setTimeout(() => setReady(true), delayMs)
    return () => clearTimeout(id)
  }, [delayMs])
  return <p role="status">{ready ? 'ready' : 'loading'}</p>
}

/** Handles window "message" events: the shape of #48's postMessage transport. */
export function MessageListener() {
  const [last, setLast] = useState('none')
  useEffect(() => {
    const onMessage = (event: MessageEvent) => setLast(String(event.data))
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [])
  return <output>{last}</output>
}
