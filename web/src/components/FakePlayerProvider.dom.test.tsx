import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { FakePlayerProvider } from './FakePlayerProvider'
import { usePlayer } from './PlayerContext'

function Probe() {
  const player = usePlayer()
  return (
    <button type="button" onClick={() => player?.seekTo(7)}>
      {player?.status ?? 'none'}
    </button>
  )
}

describe('FakePlayerProvider', () => {
  it('records seekTo calls and exposes the chosen status', () => {
    const seeks: number[] = []
    render(
      <FakePlayerProvider status="error" seeks={seeks}>
        <Probe />
      </FakePlayerProvider>,
    )
    screen.getByRole('button', { name: 'error' }).click()
    expect(seeks).toEqual([7])
  })

  it('defaults to ready', () => {
    render(
      <FakePlayerProvider>
        <Probe />
      </FakePlayerProvider>,
    )
    expect(screen.getByRole('button', { name: 'ready' })).toBeTruthy()
  })
})

describe('usePlayer without a provider', () => {
  it('returns null instead of throwing', () => {
    render(<Probe />)
    expect(screen.getByRole('button', { name: 'none' })).toBeTruthy()
  })
})
