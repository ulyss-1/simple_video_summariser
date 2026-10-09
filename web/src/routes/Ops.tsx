import { useLocation } from 'react-router'

// Placeholder: replaced by #54.
export function Ops() {
  const { search } = useLocation()
  return (
    <section>
      <h1>Ops</h1>
      <p>
        Query: <code>{search}</code>
      </p>
    </section>
  )
}
