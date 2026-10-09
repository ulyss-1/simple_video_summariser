import { useLocation } from 'react-router'

// Placeholder: replaced by #49.
export function Library() {
  const { search } = useLocation()
  return (
    <section>
      <h1>Library</h1>
      <p>
        Query: <code>{search}</code>
      </p>
    </section>
  )
}
