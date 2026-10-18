import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

describe('Ops view source', () => {
  it.each(['Ops.tsx', '../hooks/useOps.ts', '../lib/ops.ts'])('%s never injects HTML', (file) => {
    const src = readFileSync(new URL(file, import.meta.url), 'utf8')
    expect(src).not.toMatch(/dangerouslySetInnerHTML|innerHTML|insertAdjacentHTML/)
  })

  it('reaches the API only through the typed helper, with no raw fetch or hand-built URL', () => {
    const src = readFileSync(new URL('../hooks/useOps.ts', import.meta.url), 'utf8')
    expect(src).toMatch(/\.post\('\/ops\/jobs\/\{id\}\/retry'/)
    expect(src).toMatch(/\.post\('\/ops\/jobs\/retry'/)
    expect(src).not.toMatch(/apiUrl/)
    expect(src).not.toMatch(/fetch\(\s*[`'"]/)
  })

  it('builds the confirmation into the page: no window.confirm or alert', () => {
    for (const file of ['Ops.tsx', '../hooks/useOps.ts']) {
      const src = readFileSync(new URL(file, import.meta.url), 'utf8')
      expect(src).not.toMatch(/\b(window\.)?(confirm|alert)\(/)
    }
  })
})
