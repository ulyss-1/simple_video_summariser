import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { useEffect } from 'react'
import { Greeting } from './fixtures/Greeting'

const unmounted: string[] = []

function Tracked() {
  useEffect(() => () => void unmounted.push('tracked'), [])
  return null
}

// The second test fails if the setup's explicit afterEach(cleanup) is removed
// (globals are off, so Testing Library cannot register its own).
describe('setup resets the document after every test', () => {
  const originalTitle = document.title

  it('first test dirties the document', () => {
    render(<Greeting name="Ada" />)
    render(<Tracked />)
    expect(screen.getByRole('heading', { name: 'Hello, Ada' })).toBeTruthy()
    const stray = document.createElement('div')
    stray.id = 'stray'
    document.body.append(stray)
    const link = document.createElement('link')
    link.id = 'stray-link'
    document.head.append(link)
    document.title = 'dirty title'
    localStorage.setItem('k', 'v')
    sessionStorage.setItem('k', 'v')
    vi.stubGlobal('leaky', 1)
    vi.useFakeTimers()
  })

  it('second test sees a clean document', () => {
    expect(document.body.childElementCount).toBe(0)
    expect(document.getElementById('stray')).toBeNull()
    expect(document.getElementById('stray-link')).toBeNull()
    expect(unmounted).toEqual(['tracked']) // cleanup() unmounted the React tree
    expect(document.title).toBe(originalTitle)
    expect(localStorage.length).toBe(0)
    expect(sessionStorage.length).toBe(0)
    expect('leaky' in globalThis).toBe(false)
    expect(vi.isFakeTimers()).toBe(false)
  })
})
