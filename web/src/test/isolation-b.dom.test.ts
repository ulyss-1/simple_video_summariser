import { expect, it } from 'vitest'

// Pairs with isolation-a.dom.test.ts; must pass whichever file runs first.
it('file B sees none of file A state', () => {
  expect(document.title).toBe('')
  expect(localStorage.getItem('leaked-by-a')).toBeNull()
  expect('leakedByA' in globalThis).toBe(false)
  expect(document.body.childElementCount).toBe(0)
})
