import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

describe('video detail cancel flow source', () => {
  it.each([
    'VideoDetail.tsx',
    '../hooks/useCancelProcessing.ts',
    '../hooks/useDeleteVideo.ts',
  ])('%s never injects HTML', (file) => {
    const src = readFileSync(new URL(file, import.meta.url), 'utf8')
    expect(src).not.toMatch(
      /dangerouslySetInnerHTML|innerHTML|insertAdjacentHTML/,
    )
  })

  it('reaches the API only through the typed helper, with no raw fetch or hand-built URL', () => {
    const src = readFileSync(
      new URL('../hooks/useCancelProcessing.ts', import.meta.url),
      'utf8',
    )
    expect(src).toMatch(/\.post\('\/videos\/\{video_id\}\/cancel'/)
    expect(src).not.toMatch(/fetch\(/)
    expect(src).not.toMatch(/apiUrl/)
  })

  it('no code depends on a delete 409 any more', () => {
    const src = readFileSync(
      new URL('../hooks/useDeleteVideo.ts', import.meta.url),
      'utf8',
    )
    expect(src).not.toMatch(
      /409|being processed|currently processing|cancellation in progress/,
    )
  })
})
