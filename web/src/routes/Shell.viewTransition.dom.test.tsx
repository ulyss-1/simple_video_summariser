import { act, fireEvent, render, screen } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import { Shell } from './Shell'

// #163: Shell nav links opt into View Transitions. jsdom has no
// document.startViewTransition, so the API is stubbed; the real cross-fade and
// the reduced-motion rule are checked by the #177/#178 browser harness.
function mount() {
  const table = [
    {
      path: '/',
      element: <Shell />,
      children: [
        { index: true, element: <p>library page</p> },
        { path: 'search', element: <p>search page</p> },
      ],
    },
  ]
  render(<RouterProvider router={createMemoryRouter(table, { initialEntries: ['/'] })} />)
}

describe('route View Transitions opt-in', () => {
  it('wraps an opted-in navigation in document.startViewTransition', async () => {
    const start = vi.fn((cb: () => void | Promise<void>) => {
      void cb()
      const done = Promise.resolve()
      return { finished: done, ready: done, updateCallbackDone: done, skipTransition: () => {} }
    })
    vi.stubGlobal('document', Object.assign(document, { startViewTransition: start }))
    mount()
    await act(async () => {
      fireEvent.click(screen.getByRole('link', { name: 'Search' }))
    })
    expect(start).toHaveBeenCalled()
    expect(await screen.findByText('search page')).toBeTruthy()
    delete (document as { startViewTransition?: unknown }).startViewTransition
  })

  it('navigates instantly and without error when the API is absent', async () => {
    expect('startViewTransition' in document).toBe(false)
    mount()
    await act(async () => {
      fireEvent.click(screen.getByRole('link', { name: 'Search' }))
    })
    expect(await screen.findByText('search page')).toBeTruthy()
  })
})
