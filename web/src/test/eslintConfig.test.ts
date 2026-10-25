import { fileURLToPath } from 'node:url'
import { ESLint } from 'eslint'
import { describe, expect, it } from 'vitest'

const webRoot = fileURLToPath(new URL('../../', import.meta.url))
const eslint = new ESLint({ cwd: webRoot })

async function lint(code: string, filePath = 'src/Fixture.tsx') {
  const [result] = await eslint.lintText(code, { filePath })
  return result?.messages ?? []
}

describe('eslint.config.js', () => {
  it('flags a hook called inside an if as an error', async () => {
    const messages = await lint(`
import { useState } from 'react'
export function A({ on }: { on: boolean }) {
  if (on) {
    const [v] = useState(0)
    return <p>{v}</p>
  }
  return null
}
`)
    const hit = messages.find((m) => m.ruleId === 'react-hooks/rules-of-hooks')
    expect(hit?.severity).toBe(2)
  })

  it('flags a missing effect dependency', async () => {
    const messages = await lint(`
import { useEffect } from 'react'
export function A({ id }: { id: number }) {
  useEffect(() => {
    document.title = String(id)
  }, [])
  return null
}
`)
    expect(messages.map((m) => m.ruleId)).toContain('react-hooks/exhaustive-deps')
  })

  it('flags a ref read during render as a React Compiler rule', async () => {
    const messages = await lint(`
import { useRef } from 'react'
export function A() {
  const ref = useRef(0)
  return <p>{ref.current}</p>
}
`)
    expect(messages.map((m) => m.ruleId)).toContain('react-hooks/refs')
  })

  it('flags a prop mutation as a React Compiler rule', async () => {
    const messages = await lint(`
export function A({ item }: { item: { n: number } }) {
  item.n = 1
  return <p>{item.n}</p>
}
`)
    expect(messages.map((m) => m.ruleId)).toContain('react-hooks/immutability')
  })

  it('flags a component defined inside another component', async () => {
    const messages = await lint(`
export function Outer() {
  function Inner() {
    return <p>x</p>
  }
  return <Inner />
}
`)
    expect(messages.map((m) => m.ruleId)).toContain('react-hooks/static-components')
  })

  it('flags an unused variable in a .ts file', async () => {
    const messages = await lint('const unused = 1\nexport {}\n', 'src/fixture.ts')
    expect(messages.map((m) => m.ruleId)).toContain('@typescript-eslint/no-unused-vars')
  })

  it('reports nothing for a clean component', async () => {
    const messages = await lint(`
import { useEffect, useState } from 'react'
export function A({ id }: { id: number }) {
  const [v, setV] = useState(0)
  useEffect(() => {
    document.title = String(id)
  }, [id])
  return <button onClick={() => setV(v + 1)}>{v}</button>
}
`)
    expect(messages).toEqual([])
  })
})
