import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const FILES = ['Library.tsx', '../hooks/useBulkDeleteVideos.ts', '../hooks/forgetVideos.ts']

const read = (file: string) => readFileSync(new URL(file, import.meta.url), 'utf8')

describe('library bulk delete source', () => {
  it.each(FILES)('%s never injects HTML', (file) => {
    expect(read(file)).not.toMatch(/dangerouslySetInnerHTML|innerHTML|insertAdjacentHTML/)
  })

  it.each(FILES)('%s does not hand-memoise (the React Compiler does)', (file) => {
    expect(read(file)).not.toMatch(/useMemo|useCallback/)
  })

  it('goes through the typed client, never a literal fetch', () => {
    for (const file of FILES) expect(read(file)).not.toMatch(/\bfetch\(/)
  })
})
