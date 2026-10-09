import { fireEvent, render, screen } from '@testing-library/react'
import { createMemoryRouter, RouterProvider, type RouteObject } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import { routes } from './index'

const ID = '-wNyEUrxzFU'

function mount(entry: string, table: RouteObject[] = routes) {
  const router = createMemoryRouter(table, { initialEntries: [entry] })
  render(<RouterProvider router={router} />)
  return router
}

describe('shell and navigation', () => {
  it('navigates with the nav links without a document request', () => {
    const router = mount('/')
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Library')
    fireEvent.click(screen.getByRole('link', { name: 'Search' }))
    expect(router.state.location.pathname).toBe('/search')
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Search')
    fireEvent.click(screen.getByRole('link', { name: 'Ops' }))
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Ops')
  })

  it('shows the location search on the placeholder', () => {
    mount(`/videos/${ID}?t=3723`)
    expect(screen.getByText('?t=3723')).toBeTruthy()
  })

  it('keeps an API link as a plain anchor the router does not intercept', () => {
    mount('/')
    const a = screen.getByRole('link', { name: /api health/i })
    expect(a.getAttribute('href')).toBe('/api/healthz')
    // The router handles clicks on the React root, below `document`, so a
    // router-handled link is already preventDefault()ed by the time it bubbles
    // here. Record that, then cancel so jsdom does not try to navigate.
    let interceptedByRouter: boolean | undefined
    document.addEventListener('click', (e) => {
      interceptedByRouter = e.defaultPrevented
      e.preventDefault()
    })
    a.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 }))
    expect(interceptedByRouter).toBe(false)
  })
})

describe('not found', () => {
  it('renders the path as literal text, sets the title and links home', () => {
    mount('/<img src=x onerror=alert(1)>')
    expect(screen.getByRole('heading', { name: 'Page not found' })).toBeTruthy()
    expect(document.querySelector('img')).toBeNull()
    expect(screen.getByText('/<img src=x onerror=alert(1)>')).toBeTruthy()
    expect(document.title).toBe('Page not found')
    const home = screen.getAllByRole('link').filter((l) => l.textContent === 'Back to Library')
    expect(home[0]?.getAttribute('href')).toBe('/')
  })
})

describe('error boundary', () => {
  it('shows a generic message without the error text', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    function Boom(): never {
      throw new Error('secret-internal-detail')
    }
    const root = routes[0] as { children: RouteObject[] } & RouteObject
    const table = [
      { ...root, children: [...root.children, { path: 'boom', element: <Boom /> }] },
    ] as RouteObject[]
    mount('/boom', table)
    expect(screen.getByText('Something went wrong')).toBeTruthy()
    expect(document.body.textContent).not.toContain('secret-internal-detail')
    expect(screen.getByRole('link', { name: 'Back to Library' }).getAttribute('href')).toBe('/')
  })
})
