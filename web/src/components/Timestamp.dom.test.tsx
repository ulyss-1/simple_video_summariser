import { fireEvent, render, screen } from '@testing-library/react'
import { StrictMode } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { FakePlayerProvider } from './FakePlayerProvider'
import { Timestamp } from './Timestamp'

function withPlayer(seconds: number | null | undefined, seeks: number[] = []) {
  return render(
    <FakePlayerProvider seeks={seeks}>
      <Timestamp seconds={seconds} />
    </FakePlayerProvider>,
  )
}

describe('Timestamp with a provider', () => {
  it('renders a native type=button containing a <time> with an ISO duration', () => {
    const { container } = withPlayer(3723)
    const button = screen.getByRole('button', { name: 'Seek to 1:02:03' })
    expect(button.tagName).toBe('BUTTON')
    expect(button.getAttribute('type')).toBe('button')
    expect(button.getAttribute('aria-label')).toBe('Seek to 1:02:03')
    const time = button.querySelector('time')
    expect(time?.textContent).toBe('1:02:03')
    expect(time?.getAttribute('datetime')).toBe('PT1H2M3S')
    expect(container.querySelector('a')).toBeNull()
    expect(container.innerHTML).not.toContain('href')
    expect(button.classList.contains('timestamp')).toBe(true)
  })

  it.each([
    [0, '0:00', 'PT0S'],
    [59.999, '0:59', 'PT59S'],
    [60, '1:00', 'PT1M0S'],
    [3599.5, '59:59', 'PT59M59S'],
    [3600, '1:00:00', 'PT1H0M0S'],
  ])('formats %s as %s with datetime %s', (seconds, text, iso) => {
    withPlayer(seconds)
    const time = screen.getByRole('button').querySelector('time')
    expect(time?.textContent).toBe(text)
    expect(time?.getAttribute('datetime')).toBe(iso)
  })

  it('a click seeks exactly once with the exact, unfloored value', () => {
    const seeks: number[] = []
    withPlayer(12.999, seeks)
    fireEvent.click(screen.getByRole('button'))
    expect(seeks).toEqual([12.999])
  })

  it('seeks to 0 (a valid time, not falsy-skipped)', () => {
    const seeks: number[] = []
    withPlayer(0, seeks)
    fireEvent.click(screen.getByRole('button'))
    expect(seeks).toEqual([0])
  })

  it('does not submit a surrounding form', () => {
    const onSubmit = vi.fn((e: { preventDefault: () => void }) => e.preventDefault())
    render(
      <form onSubmit={onSubmit}>
        <FakePlayerProvider>
          <Timestamp seconds={5} />
        </FakePlayerProvider>
      </form>,
    )
    fireEvent.click(screen.getByRole('button'))
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('is reachable by keyboard because it is a focusable native button', () => {
    withPlayer(5)
    const button = screen.getByRole('button')
    button.focus()
    expect(document.activeElement).toBe(button)
    // Enter and Space activate a native <button> by firing click; jsdom does
    // not synthesise that, so the native element is the assertion.
    expect(button.hasAttribute('tabindex')).toBe(false)
    expect(button.hasAttribute('disabled')).toBe(false)
  })

  it('one click under StrictMode seeks once', () => {
    const seeks: number[] = []
    render(
      <StrictMode>
        <FakePlayerProvider seeks={seeks}>
          <Timestamp seconds={9} />
        </FakePlayerProvider>
      </StrictMode>,
    )
    fireEvent.click(screen.getByRole('button'))
    expect(seeks).toEqual([9])
  })
})

describe('Timestamp renders nothing for invalid input', () => {
  it.each([
    ['null', null],
    ['undefined', undefined],
    ['NaN', Number.NaN],
    ['Infinity', Number.POSITIVE_INFINITY],
    ['-Infinity', Number.NEGATIVE_INFINITY],
    ['-0.001', -0.001],
    ['-1', -1],
  ])('%s', (_name, value) => {
    const log = vi.spyOn(console, 'error').mockImplementation(() => {})
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const { container: withProvider } = withPlayer(value)
    expect(withProvider.innerHTML).toBe('')
    const { container: without } = render(<Timestamp seconds={value} />)
    expect(without.innerHTML).toBe('')
    expect(log).not.toHaveBeenCalled()
    expect(warn).not.toHaveBeenCalled()
  })
})

describe('Timestamp without a provider', () => {
  it('shows plain <time> text, no button, and does not throw', () => {
    const { container } = render(<Timestamp seconds={3723} />)
    expect(screen.queryByRole('button')).toBeNull()
    expect(container.querySelector('button')).toBeNull()
    expect(container.querySelector('a')).toBeNull()
    const time = container.querySelector('time')
    expect(time?.textContent).toBe('1:02:03')
    expect(time?.getAttribute('datetime')).toBe('PT1H2M3S')
  })

  it('renders zero as 0:00', () => {
    const { container } = render(<Timestamp seconds={0} />)
    expect(container.querySelector('time')?.textContent).toBe('0:00')
  })
})
