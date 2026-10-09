import babel from '@rolldown/plugin-babel'
import react, { reactCompilerPreset } from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'
import { API_PREFIX, rewriteApiPath } from './src/devProxy.ts'

// Non-VITE_ on purpose: it must never reach the client bundle.
const apiProxyTarget = process.env.API_PROXY_TARGET ?? 'http://localhost:8000'

export default defineConfig({
  base: '/',
  plugins: [react(), babel({ presets: [reactCompilerPreset()] })],
  build: { sourcemap: false },
  server: {
    proxy: {
      [API_PREFIX]: { target: apiProxyTarget, rewrite: rewriteApiPath },
    },
  },
  test: {
    environment: 'node',
    include: ['src/**/*.test.{ts,tsx}'],
  },
})
