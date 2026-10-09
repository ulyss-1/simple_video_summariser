import { afterAll, expect, it } from 'vitest'

// Pairs with isolation-b.dom.test.ts. The state is written in afterAll so it
// outlives the setup's per-test reset: only a fresh jsdom window per file
// keeps it from reaching file B, in either run order.
afterAll(() => {
  document.title = 'leaked by A'
  localStorage.setItem('leaked-by-a', '1')
  Object.defineProperty(globalThis, 'leakedByA', { value: 1, configurable: true })
  document.body.append(document.createElement('aside'))
})

it('file A can set state', () => {
  document.title = 'set by A'
  expect(document.title).toBe('set by A')
})
