import { NavLink, Outlet, ScrollRestoration } from 'react-router'
import { useTheme } from '../theme'

// App shell: header with the main nav, then the routed page in <main>.
// Styled with the Hybrid tokens (#124, _docs/design-system.md section 11).
// Final nav links are owned by #52 (Search) and #54 (Ops).
export function Shell() {
  const { theme, toggleTheme } = useTheme()
  const next = theme === 'dark' ? 'light' : 'dark'
  return (
    <>
      <header className="shell-header">
        <nav className="shell-nav" aria-label="Main">
          <NavLink to="/" end viewTransition className="shell-nav__link">
            Library
          </NavLink>
          <NavLink to="/search" viewTransition className="shell-nav__link">
            Search
          </NavLink>
          <NavLink to="/channels/add" viewTransition className="shell-nav__link">
            Add channel
          </NavLink>
          <NavLink to="/ops" viewTransition className="shell-nav__link">
            Ops
          </NavLink>
          {/* Plain anchor on purpose: API URLs belong to the server, not the router. */}
          <a href="/api/healthz" className="shell-nav__link shell-nav__link--aside">
            API health
          </a>
          <button type="button" className="theme-toggle" onClick={toggleTheme}>
            Switch to {next} theme
          </button>
        </nav>
      </header>
      <main className="shell-main">
        <Outlet />
      </main>
      <ScrollRestoration />
    </>
  )
}
