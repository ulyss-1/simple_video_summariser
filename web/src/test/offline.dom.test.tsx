import { render } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

describe('fetch default', () => {
  it('throws a named error naming the file and the attempted request', () => {
    let caught: unknown
    try {
      void fetch('/api/videos')
    } catch (error) {
      caught = error
    }
    expect(caught).toBeInstanceOf(Error)
    const error = caught as Error
    expect(error.name).toBe('UnexpectedNetworkAccessError')
    expect(error.message).toContain('unexpected network access in tests: GET /api/videos')
    expect(error.message).toContain('offline.dom.test.tsx')
  })

  it('names the method when one is given', () => {
    expect(() => fetch('/api/jobs', { method: 'post' })).toThrow(
      'unexpected network access in tests: POST /api/jobs',
    )
  })

  it('lets a test stub fetch per test', async () => {
    const stub = vi.fn(async () => new Response('ok'))
    vi.stubGlobal('fetch', stub)
    const response = await fetch('/api/x')
    expect(await response.text()).toBe('ok')
    expect(stub).toHaveBeenCalledOnce()
  })

  it('has the throwing default back after a stubbed test', () => {
    expect(() => fetch('/api/x')).toThrow('unexpected network access in tests')
  })
})

describe('off-origin embeds', () => {
  it('render without any network access and without hanging', async () => {
    const spy = vi.fn()
    vi.stubGlobal('fetch', spy)
    const { container } = render(
      <div>
        <img alt="thumb" src="https://i.ytimg.com/vi/abc/hqdefault.jpg" />
        <iframe
          title="player"
          src="https://www.youtube-nocookie.com/embed/abc?enablejsapi=1"
        />
      </div>,
    )
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(container.querySelector('img')?.getAttribute('src')).toContain('i.ytimg.com')
    expect(container.querySelector('iframe')?.getAttribute('src')).toContain(
      'youtube-nocookie.com',
    )
    expect(container.querySelector('img')?.complete).toBe(false)
    expect(spy).not.toHaveBeenCalled()
  })
})
