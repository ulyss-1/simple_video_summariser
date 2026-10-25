import { readdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { join, relative } from 'node:path'
import { describe, expect, it } from 'vitest'

// Mechanical guards for #118: the helper is the only caller of fetch.
const srcDir = fileURLToPath(new URL('..', import.meta.url))

function sources(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name)
    if (entry.isDirectory()) return sources(path)
    return /\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name) ? [path] : []
  })
}

describe('request helper source scan', () => {
  it('client.ts has no literal API prefix and no host', () => {
    const text = readFileSync(join(srcDir, 'api/client.ts'), 'utf8')
    expect(text).not.toMatch(/\/api\b/)
    expect(text).not.toMatch(/https?:\/\//)
    expect(text).not.toMatch(/mode:\s*['"]cors/)
  })

  it('client.ts is the one place that sets headers and credentials', () => {
    const text = readFileSync(join(srcDir, 'api/client.ts'), 'utf8')
    expect(text.match(/credentials:/g)).toHaveLength(1)
    expect(text.match(/\bAccept\b/g)?.length).toBe(1)
  })

  it('no other source file outside src/api calls fetch(', () => {
    const offenders = sources(srcDir)
      .map((path) => ({ rel: relative(srcDir, path), text: readFileSync(path, 'utf8') }))
      .filter(({ rel }) => !rel.startsWith('api/'))
      .filter(({ text }) => /(?<![.\w])fetch\(/.test(text))
      .map(({ rel }) => rel)
    expect(offenders).toEqual([])
  })

  it('the hooks reach the API only through the helper', () => {
    for (const rel of readdirSync(join(srcDir, 'hooks')).map((f) => `hooks/${f}`)) {
      expect(readFileSync(join(srcDir, rel), 'utf8')).not.toMatch(/apiUrl/)
    }
  })
})
