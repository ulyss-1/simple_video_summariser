import { useLocation } from 'react-router'

// Placeholder: replaced by #52.
export function Search() {
  const { search } = useLocation()
  return (
    <section>
      <h1>Search</h1>
      <p>
        Query: <code>{search}</code>
      </p>
    </section>
  )
}
