import { useEffect } from 'react'
import { Link, useLocation } from 'react-router'

export function NotFound() {
  const { pathname } = useLocation()
  useEffect(() => {
    document.title = 'Page not found'
  }, [])
  return (
    <section>
      <h1>Page not found</h1>
      <p>
        No page at <code>{pathname}</code>
      </p>
      <p>
        <Link to="/">Back to Library</Link>
      </p>
    </section>
  )
}
