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
          <NavLink to="/deleted" viewTransition className="shell-nav__link">
            Recently deleted
          </NavLink>
          {/* Plain anchor on purpose: API URLs belong to the server, not the router. */}
          <a href="/api/healthz" className="shell-nav__link shell-nav__link--aside">
            API health
          </a>
          <button
            type="button"
            className="theme-toggle"
            onClick={toggleTheme}
            aria-label={`Switch to ${next} theme`}
            title={`Switch to ${next} theme`}
          >
            <ThemeIcon target={next} />
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

// The icon shows the theme the button switches TO: a moon in light mode, a sun
// in dark mode. Decorative; the button's aria-label carries the name.
function ThemeIcon({ target }: { target: 'light' | 'dark' }) {
  return (
    <svg
      width="20"
      height="20"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {target === 'dark' ? (
        <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />
      ) : (
        <>
          <circle cx="12" cy="12" r="4" />
          <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
        </>
      )}
    </svg>
  )
}
