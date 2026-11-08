import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

// #411: the server decides "overdue"; the SPA must neither compare
// cancel_requested_at with a clock nor hold a threshold constant.
describe('cancel overdue is server-decided', () => {
  const files = ['routes/VideoDetail.tsx', 'routes/Ops.tsx', 'lib/ops.ts', 'hooks/useVideo.ts']
  it.each(files)('%s reads no clock and has no 30 s constant', (f) => {
    const src = readFileSync(new URL(`../${f}`, import.meta.url), 'utf8')
    const clockOnCancel = src.split('\n').filter((l) => /cancel_requested_at/.test(l) && /Date\b/.test(l))
    expect(clockOnCancel).toEqual([])
    expect(src).not.toMatch(/OVERDUE_SEC/)
  })
})
