import { renderToString } from 'react-dom/server'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { describe, expect, it } from 'vitest'
import { routes } from './index'

const ID = '-wNyEUrxzFU'

function render(entry: string) {
  const router = createMemoryRouter(routes, { initialEntries: [entry] })
  const html = renderToString(<RouterProvider router={router} />)
  return { html, router }
}

function heading(html: string): string {
  const m = /<h1[^>]*>(.*?)<\/h1>/.exec(html)
  return m?.[1] ?? ''
}

describe('route table', () => {
  it.each([
    ['/', 'Library'],
    [`/videos/${ID}`, 'VideoDetail'],
    [`/videos/${ID}/transcript`, 'Transcript'],
    [`/videos/${ID}/compare`, 'Compare'],
    ['/search', 'Search'],
    ['/ops', 'Ops'],
  ])('%s renders the %s placeholder', (path, name) => {
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
    const html = render('/videos/AbC-_xYz012').html
    expect(html).toContain('AbC-_xYz012')
    expect(render(`/videos/${ID}`).html).toContain(ID)
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
    expect(html).toMatch(/Page not found|Video not found|<h1/)
    expect(html).not.toContain('Something went wrong')
  })

  it.each([
    ['/?page=2&status=failed', '?page=2&amp;status=failed'],
    ['/search?q=hello%20world&page=3', '?q=hello%20world&amp;page=3'],
    [`/videos/${ID}?t=3723`, '?t=3723'],
    [`/videos/${ID}/transcript?page=3`, '?page=3'],
    [`/videos/${ID}/compare?x=1`, '?x=1'],
    ['/ops?state=dead&before=100', '?state=dead&amp;before=100'],
  ])('%s shows its search string unchanged', (path, shown) => {
    expect(render(path).html).toContain(shown)
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
