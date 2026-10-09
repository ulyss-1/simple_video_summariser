import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { describe, expect, it } from 'vitest'

const webRoot = fileURLToPath(new URL('../../', import.meta.url))

describe('React Compiler', () => {
  it('transforms test fixtures through the real vite.config.ts', async () => {
    const server = await createServer({
      root: webRoot,
      configFile: `${webRoot}vite.config.ts`,
      appType: 'custom',
      logLevel: 'silent',
      server: { middlewareMode: true, hmr: false, ws: false },
    })
    try {
      const result = await server.transformRequest('/src/test/fixtures/Greeting.tsx')
      expect(result?.code).toContain('react.memo_cache_sentinel')
    } finally {
      await server.close()
    }
  })
})
