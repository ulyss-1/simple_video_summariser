import { readdirSync, readFileSync } from 'node:fs'
import { join, relative, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

// Mechanical check of the "exactly one place" rule (architecture.md section
// 9, #48): later views cannot grow a second seek path or a second YouTube
// integration without failing here.
//
// What this guards: accidental reintroduction of a second seek path or a
// second YouTube integration in ordinary source under web/src (.ts/.tsx/.js/
// .jsx/.mjs/.mts/.cjs/.cts/.html/.css), caught by matching source text.
//
// What this does NOT guard: deliberate evasion. This is a text scan, so it is
// defeated by, among others: string concatenation ('seek' + 'To('), a JSX
// `<script src={expr}>` whose URL is computed, an indirect call reached by a
// name other than `seekTo`, files under src/test/ or named *.test.*, and
// anything in web/index.html (outside src). Treat a green run as "nothing
// obvious slipped in", not as a security boundary. The real boundaries are the
// transport's origin/source checks and the CSP (#48, #57, #178).
const srcRoot = fileURLToPath(new URL('../', import.meta.url))

function walk(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = join(dir, entry.name)
    return entry.isDirectory() ? walk(full) : [full]
  })
}

const files = walk(srcRoot)
  .filter((f) => /\.(ts|tsx|js|jsx|mjs|mts|cjs|cts|html|css)$/.test(f))
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

  it('only Timestamp.tsx calls seekTo (direct, optional-call and indirect forms)', () => {
    // Catches `seekTo(`, `seekTo?.(`, and `.seekTo.call(` / `.apply(` / `.bind(`.
    // Still a text scan, so a name other than `seekTo` or a concatenated string
    // slips through — see the header comment.
    const seekToCall = /\bseekTo\s*(?:\?\.\s*)?\(|\.\s*seekTo\s*\.\s*(?:call|apply|bind)\s*\(/
    expect(offenders(seekToCall, (p) => p === 'components/Timestamp.tsx')).toEqual([])
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
