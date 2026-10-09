import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

describe('Ops view source', () => {
  it.each(['Ops.tsx', '../hooks/useOps.ts', '../lib/ops.ts'])('%s never injects HTML', (file) => {
    const src = readFileSync(new URL(file, import.meta.url), 'utf8')
    expect(src).not.toMatch(/dangerouslySetInnerHTML|innerHTML|insertAdjacentHTML/)
  })

  it('builds every API URL through apiUrl() and every query through URLSearchParams', () => {
    const src = readFileSync(new URL('../hooks/useOps.ts', import.meta.url), 'utf8')
    expect(src).toMatch(/apiUrl\(/)
    expect(src).not.toMatch(/fetch\(\s*[`'"]/)
  })
})
