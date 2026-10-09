import { readdirSync, readFileSync } from 'node:fs'
import { join, relative, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

// Mechanical check of the "exactly one place" rule (architecture.md section
// 9, #48): later views cannot grow a second seek path or a second YouTube
// integration without failing here.
const srcRoot = fileURLToPath(new URL('../', import.meta.url))

function walk(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = join(dir, entry.name)
    return entry.isDirectory() ? walk(full) : [full]
  })
}

const files = walk(srcRoot)
  .filter((f) => /\.(ts|tsx|js|jsx|html|css)$/.test(f))
  .filter((f) => !/\.test\.[jt]sx?$/.test(f))
  .filter((f) => !f.endsWith('schema.d.ts'))
  .filter((f) => !f.startsWith(srcRoot + 'test/')) // test support, never shipped
  .map((f) => ({ path: relative(srcRoot, f).split(sep).join('/'), text: readFileSync(f, 'utf8') }))

const inPlayerModule = (path: string) =>
  path === 'components/Player.tsx' || path.startsWith('components/player/')

function offenders(pattern: RegExp, allowed: (path: string) => boolean): string[] {
  return files.filter((f) => !allowed(f.path) && pattern.test(f.text)).map((f) => f.path)
}

describe('source scan over web/src', () => {
  it('scans a non-trivial set of files', () => {
    expect(files.length).toBeGreaterThan(10)
    expect(files.map((f) => f.path)).toContain('components/Timestamp.tsx')
  })

  it('only Timestamp.tsx calls seekTo', () => {
    expect(offenders(/\bseekTo\s*\(/, (p) => p === 'components/Timestamp.tsx')).toEqual([])
  })

  it.each([
    ['youtube-nocookie.com', /youtube-nocookie\.com/],
    ['postMessage', /postMessage/],
    ['the seekTo command name', /['"`]seekTo['"`]/],
  ])('only the player modules contain %s', (_name, pattern) => {
    expect(offenders(pattern, inPlayerModule)).toEqual([])
  })

  it.each([
    ['iframe_api', /iframe_api/],
    ['www-widgetapi', /www-widgetapi/],
    ['an external script src', /<script[^>]+src\s*=\s*["']?(https?:)?\/\//i],
    ['a dynamically created script element', /createElement\(\s*['"]script['"]/],
  ])('nothing contains %s', (_name, pattern) => {
    expect(offenders(pattern, () => false)).toEqual([])
  })

  it('the player module that must contain the embed host does', () => {
    expect(files.find((f) => f.path === 'components/player/youtubeTransport.ts')?.text).toContain(
      'youtube-nocookie.com',
    )
  })
})
