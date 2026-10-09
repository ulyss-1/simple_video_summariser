import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderToString } from 'react-dom/server'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { describe, expect, it } from 'vitest'
import { routes } from './index'

const ID = '-wNyEUrxzFU'

function render(entry: string) {
  const router = createMemoryRouter(routes, { initialEntries: [entry] })
  // The Library view (#49) uses TanStack Query; SSR renders its loading state.
  const html = renderToString(
    <QueryClientProvider client={new QueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return { html, router }
}

function heading(html: string): string {
  const m = /<h1[^>]*>(.*?)<\/h1>/.exec(html)
  return m?.[1] ?? ''
}

describe('route table', () => {
  it.each([
    ['/', 'Library'],
    [`/videos/${ID}/transcript`, ID],
    [`/videos/${ID}/compare`, ID],
    ['/search', 'Search'],
    ['/ops', 'Ops'],
  ])('%s renders the view with heading %s', (path, name) => {
    expect(heading(render(path).html)).toContain(name)
  })

  it.each([
    ['/search/', '/search'],
    [`/videos/${ID}/`, `/videos/${ID}`],
    [`/videos/${ID}/transcript/`, `/videos/${ID}/transcript`],
    [`/videos/${ID}/compare/`, `/videos/${ID}/compare`],
    ['/ops/', '/ops'],
  ])('trailing slash %s renders the same view as %s', (withSlash, without) => {
    expect(heading(render(withSlash).html)).toBe(heading(render(without).html))
    expect(heading(render(withSlash).html)).not.toContain('Page not found')
  })

  it('passes the video id through exactly, case and leading dash kept', () => {
    // VideoDetail (#50) renders its loading state on the server; a valid id
    // reaches the query instead of the "Video not found" state.
    expect(render('/videos/AbC-_xYz012').html).toContain('Loading video')
    expect(render(`/videos/${ID}`).html).toContain('Loading video')
  })

  it.each(['/videos/short', '/videos/abcdefghijkl', '/videos/abc%2Fdefghi', '/videos/..', '/videos/%20%20%20%20%20%20%20%20%20%20%20'])(
    'shows "Video not found" for the invalid id in %s',
    (path) => {
      const { html } = render(path)
      expect(html).toContain('Video not found')
    },
  )

  it.each([
    '/foo',
    '/videos',
    `/videos/${ID}/extra`,
    `/videos/${ID}/transcript/x`,
    `/videos/${ID}/compare/x`,
    '/search/x',
    '/opsx',
    '/index.html.bak',
  ])('%s renders the not-found view', (path) => {
    const { html } = render(path)
    expect(html).toContain('Page not found')
    expect(html).toContain('href="/"')
  })

  it('shows the requested path on the not-found page as escaped text', () => {
    const { html } = render('/<img src=x onerror=alert(1)>')
    expect(html).not.toContain('<img')
    expect(html).toContain('Page not found')
  })

  it.each([
    ['/videos/%E0%A4%A'],
    ['/search?q=%E0%A4%A'],
    ['/%E0%A4%A'],
    [`/videos/${ID}?t=%E0%A4%A`],
  ])('does not throw on malformed encoding in %s', (path) => {
    const { html } = render(path)
    expect(html).toMatch(/Page not found|Video not found|Loading video|<h1/)
    expect(html).not.toContain('Something went wrong')
  })

})

describe('search string on the Search view', () => {
  // Search (#52) reads its query from the URL instead of printing the string.
  it('/search?q=hello%20world&page=3 reaches the view: the box shows the decoded query', () => {
    expect(render('/search?q=hello%20world&page=3').html).toContain('value="hello world"')
  })
})

describe('compare route query string', () => {
  // The Compare view (#53) no longer prints its query string, so the router
  // state is what proves the query reaches the view unchanged.
  it.each([`?x=1`, `?a=3&b=7`, `?a=1&a=2&b=%E0%A4%A`])('%s reaches the compare route unchanged', (search) => {
    const { html, router } = render(`/videos/${ID}/compare${search}`)
    expect(router.state.location.search).toBe(search)
    expect(router.state.location.pathname).toBe(`/videos/${ID}/compare`)
    expect(heading(html)).toBe(ID)
  })
})

describe('ops route query string', () => {
  // The shared `routes` table, not a local router: this is what proves routing
  // passes the query through to the Ops view (#122).
  it.each(['?state=dead&before=100', '?state=pending&kind=ingest&error_class=none', '?state=dead&before=100&x=1'])(
    '%s reaches the ops route unchanged',
    (search) => {
      const { html, router } = render(`/ops${search}`)
      expect(router.state.location.search).toBe(search)
      expect(router.state.location.pathname).toBe('/ops')
      expect(heading(html)).toBe('Ops')
    },
  )
})

describe('search string on the Ops view', () => {
  // Ops (#54) reads its filters from the URL instead of printing the string:
  // the selects show them. (The cursor reaches the request: Ops.dom.test.tsx.)
  it('/ops?state=pending&kind=ingest&error_class=none&before=100 reaches the view: the selects show the filters', () => {
    const { html } = render('/ops?state=pending&kind=ingest&error_class=none&before=100')
    expect(html).toContain('<option value="pending" selected="">')
    expect(html).toContain('<option value="ingest" selected="">')
    expect(html).toContain('<option value="none" selected="">')
  })

  it('/ops?state=bogus falls back to the default filter and does not echo the value', () => {
    const { html } = render('/ops?state=bogus')
    expect(html).toContain('<option value="dead" selected="">')
    expect(html).not.toContain('bogus')
  })
})

describe('history', () => {
  it('walks back and forward through in-app navigations', async () => {
    const router = createMemoryRouter(routes, { initialEntries: ['/'] })
    const at = () => [router.state.location.pathname, router.state.location.search]
    expect(at()).toEqual(['/', ''])
    await router.navigate('/search?q=a%20b&page=2')
    expect(at()).toEqual(['/search', '?q=a%20b&page=2'])
    await router.navigate(`/videos/${ID}?t=5`)
    expect(at()).toEqual([`/videos/${ID}`, '?t=5'])
    await router.navigate(-1)
    expect(at()).toEqual(['/search', '?q=a%20b&page=2'])
    await router.navigate(-1)
    expect(at()).toEqual(['/', ''])
    await router.navigate(1)
    expect(at()).toEqual(['/search', '?q=a%20b&page=2'])
  })
})
