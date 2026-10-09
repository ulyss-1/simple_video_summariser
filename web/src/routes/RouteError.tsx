import { Link } from 'react-router'

// Deliberately does not read the thrown error: its text must never reach the user.
export function RouteError() {
  return (
    <main>
      <h1>Something went wrong</h1>
      <p>
        <Link to="/">Back to Library</Link>
      </p>
    </main>
  )
}
