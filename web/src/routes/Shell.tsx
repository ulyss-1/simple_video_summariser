import { Link, Outlet, ScrollRestoration } from 'react-router'

// Minimal app shell. Final nav links are owned by #52 (Search) and #54 (Ops);
// styling waits for #124.
export function Shell() {
  return (
    <>
      <header>
        <nav aria-label="Main">
          <Link to="/">Library</Link> <Link to="/search">Search</Link> <Link to="/ops">Ops</Link>{' '}
          {/* Plain anchor on purpose: API URLs belong to the server, not the router. */}
          <a href="/api/healthz">API health</a>
        </nav>
      </header>
      <main>
        <Outlet />
      </main>
      <ScrollRestoration />
    </>
  )
}
