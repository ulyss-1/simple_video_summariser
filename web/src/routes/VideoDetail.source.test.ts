import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

describe('video detail cancel flow source', () => {
  it.each([
    'VideoDetail.tsx',
    '../hooks/useCancelProcessing.ts',
    '../hooks/useDeleteVideo.ts',
    '../components/AnalysisEstimate.tsx',
    '../hooks/useAnalysisEstimate.ts',
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

  it.each(['../components/AnalysisEstimate.tsx', '../hooks/useAnalysisEstimate.ts', 'VideoDetail.tsx'])(
    '%s does not hand-memoise (React Compiler is on)',
    (file) => {
      const src = readFileSync(new URL(file, import.meta.url), 'utf8')
      expect(src).not.toMatch(/useMemo|useCallback/)
    },
  )

  it('the cost estimate comes from the typed helper and the page does no price arithmetic', () => {
    const hook = readFileSync(new URL('../hooks/useAnalysisEstimate.ts', import.meta.url), 'utf8')
    expect(hook).toMatch(/\.get\(\s*["']\/videos\/\{video_id\}\/analysis-estimate["']/)
    expect(hook).not.toMatch(/fetch\(/)
    expect(hook).toMatch(/queryKey: \[["']video["'], videoId, ["']analysis-estimate["'], latestAnalysisAt\]/)
    const view = readFileSync(new URL('../components/AnalysisEstimate.tsx', import.meta.url), 'utf8')
    expect(view).not.toMatch(/cost_usd\s*[*/+-]|[*/]\s*cost_usd/)
  })
})
