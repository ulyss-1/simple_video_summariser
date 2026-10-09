import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

describe('Search view source', () => {
  it.each(['Search.tsx', '../hooks/useSearch.ts', '../lib/search.ts'])('%s never injects HTML', (file) => {
    const src = readFileSync(new URL(file, import.meta.url), 'utf8')
    expect(src).not.toMatch(/dangerouslySetInnerHTML|innerHTML|insertAdjacentHTML/)
  })
})
