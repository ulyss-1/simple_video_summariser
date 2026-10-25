import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const FILES = [
  'ChannelCatalog.tsx',
  'AddChannel.tsx',
  '../hooks/useChannelCatalog.ts',
  '../hooks/useRegisterChannel.ts',
  '../lib/channelCatalog.ts',
]

const read = (file: string) => readFileSync(new URL(file, import.meta.url), 'utf8')

describe('channel catalog source', () => {
  it.each(FILES)('%s never injects HTML', (file) => {
    expect(read(file)).not.toMatch(/dangerouslySetInnerHTML|innerHTML|insertAdjacentHTML/)
  })

  it.each(FILES)('%s does not hand-memoise (the React Compiler does)', (file) => {
    expect(read(file)).not.toMatch(/useMemo|useCallback/)
  })

  it('keeps the selection in the route and adds no global store', () => {
    const route = read('ChannelCatalog.tsx')
    expect(route).toMatch(/useState<ReadonlySet<string>>/)
    for (const file of FILES) expect(read(file)).not.toMatch(/zustand|redux|jotai|createContext/)
  })

  it('opens every external link with both safe attributes', () => {
    const src = read('ChannelCatalog.tsx')
    const anchors = src.match(/<a\b[^>]*>/g) ?? []
    expect(anchors.length).toBeGreaterThan(0)
    for (const anchor of anchors) {
      expect(anchor).toContain('target="_blank"')
      expect(anchor).toContain('rel="noopener noreferrer"')
    }
  })

  it('builds API calls through the typed client, never a literal fetch', () => {
    for (const file of FILES) expect(read(file)).not.toMatch(/\bfetch\(/)
  })
})
