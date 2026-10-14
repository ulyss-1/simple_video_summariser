import { act, fireEvent, render, screen } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Shell } from './routes/Shell'

const root = document.documentElement

// jsdom has no matchMedia. Shell's theme toggle (theme.ts) needs it.
function stubOs(dark: boolean) {
  let listener: (() => void) | null = null
  const mq = {
    matches: dark,
    addEventListener: (_: string, l: () => void) => (listener = l),
    removeEventListener: () => (listener = null),
  }
  vi.stubGlobal('matchMedia', () => mq)
  return (nextDark: boolean) => {
    mq.matches = nextDark
    act(() => listener?.())
  }
}

function mount() {
  const router = createMemoryRouter([{ path: '/', element: <Shell />, children: [{ index: true, element: <p>x</p> }] }])
  render(<RouterProvider router={router} />)
}

const toggle = () => screen.getByRole('button', { name: /theme/i })

beforeEach(() => root.removeAttribute('data-theme'))
afterEach(() => root.removeAttribute('data-theme'))

describe('theme toggle', () => {
  it('switches between light and dark and names the action', () => {
    stubOs(false)
    mount()
    expect(toggle().textContent).toBe('Switch to dark theme')
    fireEvent.click(toggle())
    expect(root.getAttribute('data-theme')).toBe('dark')
    expect(toggle().textContent).toBe('Switch to light theme')
    fireEvent.click(toggle())
    expect(root.getAttribute('data-theme')).toBe('light')
  })

  it('persists the choice and restores it on the next mount', () => {
    stubOs(false)
    mount()
    fireEvent.click(toggle())
    expect(localStorage.getItem('theme')).toBe('dark')
    document.body.replaceChildren()
    root.removeAttribute('data-theme')
    mount()
    expect(root.getAttribute('data-theme')).toBe('dark')
    expect(toggle().textContent).toBe('Switch to light theme')
  })

  it('overrides an OS dark preference with an explicit light choice', () => {
    stubOs(true)
    mount()
    expect(root.hasAttribute('data-theme')).toBe(false)
    expect(toggle().textContent).toBe('Switch to light theme')
    fireEvent.click(toggle())
    expect(root.getAttribute('data-theme')).toBe('light')
  })

  it('follows the OS when nothing is stored, including live changes', () => {
    const setOs = stubOs(false)
    mount()
    expect(toggle().textContent).toBe('Switch to dark theme')
    setOs(true)
    expect(toggle().textContent).toBe('Switch to light theme')
    expect(root.hasAttribute('data-theme')).toBe(false)
  })

  it('ignores a garbage stored value and survives blocked storage', () => {
    stubOs(false)
    localStorage.setItem('theme', 'purple')
    mount()
    expect(root.hasAttribute('data-theme')).toBe(false)
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    fireEvent.click(toggle())
    expect(root.getAttribute('data-theme')).toBe('dark')
  })

  it('is a real button, operable from the keyboard', () => {
    stubOs(false)
    mount()
    expect(toggle().tagName).toBe('BUTTON')
    expect(toggle().getAttribute('type')).toBe('button')
  })
})

describe('pre-paint script', () => {
  const src = readFileSync('public/theme-init.js', 'utf8')
  const run = () => new Function(src)()

  it('applies a stored choice before the bundle loads', () => {
    localStorage.setItem('theme', 'dark')
    run()
    expect(root.getAttribute('data-theme')).toBe('dark')
  })

  it('leaves the attribute off with no or invalid stored choice', () => {
    run()
    expect(root.hasAttribute('data-theme')).toBe(false)
    localStorage.setItem('theme', 'x')
    run()
    expect(root.hasAttribute('data-theme')).toBe(false)
  })

  it('does not throw when storage is blocked', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    expect(run).not.toThrow()
  })

  it('is loaded from index.html as a same-origin script (CSP script-src self)', () => {
    const html = readFileSync('index.html', 'utf8')
    expect(html).toContain('<script src="/theme-init.js"></script>')
  })
})
