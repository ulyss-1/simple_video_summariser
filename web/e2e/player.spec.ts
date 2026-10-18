import { stubEmbed, stubVideoApi, stubWatchPage, receivedCommands, listeningCount, VIDEO_ID } from './fixtures/embedStub.ts'
import { expect, test } from './fixtures/offline.ts'
import type { Page } from '@playwright/test'

// Browser-level checks for Timestamp and Player (#178) that jsdom cannot do.
// Offline: /api/** and the youtube-nocookie.com embed are stubbed (see
// fixtures/embedStub.ts); the offline guard fails any other third-party request.
const CLAIMS = [75, 130, 3723]
const READY_LABEL = 'Seek to 1:15'

async function openVideo(page: Page): Promise<void> {
  await stubVideoApi(page, CLAIMS)
  await page.goto(`/videos/${VIDEO_ID}`)
  await expect(page.getByRole('button', { name: READY_LABEL })).toBeVisible()
  await expect(page.locator('iframe')).toBeVisible()
}

async function tabTo(page: Page, name: string): Promise<void> {
  // Real keyboard traversal from the top of the page, bounded.
  for (let i = 0; i < 40; i += 1) {
    await page.keyboard.press('Tab')
    if (await page.getByRole('button', { name }).evaluate((el) => el === document.activeElement)) return
  }
  throw new Error(`Tab never reached "${name}"`)
}

for (const [key, label] of [['Enter', 'Enter'], ['Space', ' ']] as const) {
  test(`keyboard: ${key} on the focused timestamp seeks exactly once and does not scroll`, async ({ page }) => {
    await stubEmbed(page, 'ready')
    await page.setViewportSize({ width: 800, height: 300 })
    await openVideo(page)
    // Wait for the handshake, so the click goes through the ready path.
    await expect.poll(() => listeningCount(page)).toBeGreaterThan(0)
    await tabTo(page, READY_LABEL)
    expect(await page.evaluate(() => (document.scrollingElement?.scrollHeight ?? 0) > innerHeight), 'page must be scrollable').toBe(true)
    const before = await page.evaluate(() => scrollY)

    await page.keyboard.press(label === ' ' ? 'Space' : label)

    await expect
      .poll(async () => (await receivedCommands(page)).map((c) => c.func))
      .toEqual(['seekTo', 'playVideo'])
    expect((await receivedCommands(page))[0]?.args).toEqual([75, true])
    expect(await page.evaluate(() => scrollY)).toBe(before)
    // Exactly once: nothing further arrives after the first delivery settles.
    await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))))
    expect((await receivedCommands(page)).filter((c) => c.func === 'seekTo')).toHaveLength(1)
  })
}

test('focus ring: a keyboard-focused timestamp has a visible outline the unfocused one lacks', async ({ page }) => {
  await stubEmbed(page, 'ready')
  await openVideo(page)
  const button = page.getByRole('button', { name: READY_LABEL })
  const outline = () =>
    button.evaluate((el) => {
      const s = getComputedStyle(el)
      return { style: s.outlineStyle, width: parseFloat(s.outlineWidth), color: s.outlineColor }
    })
  const unfocused = await outline()
  await tabTo(page, READY_LABEL)
  await expect(button).toBeFocused()
  const focused = await outline()
  expect(unfocused.style === 'none' || unfocused.width === 0).toBe(true)
  expect(focused.style).not.toBe('none')
  expect(focused.width).toBeGreaterThanOrEqual(2)
  expect(focused.color).not.toBe('rgba(0, 0, 0, 0)')
})

test('360 px: no horizontal scroll, 16/9 embed, every timestamp reachable', async ({ page }) => {
  await stubEmbed(page, 'ready')
  await page.setViewportSize({ width: 360, height: 640 })
  await openVideo(page)
  const overflow = await page.evaluate(() => {
    const el = document.scrollingElement
    return el === null ? null : { scroll: el.scrollWidth, client: el.clientWidth }
  })
  expect(overflow).not.toBeNull()
  expect(overflow!.scroll).toBeLessThanOrEqual(overflow!.client)

  const iframe = page.locator('iframe')
  expect(await iframe.evaluate((el) => getComputedStyle(el).aspectRatio.replace(/\s/g, ''))).toBe('16/9')
  const box = await iframe.boundingBox()
  expect(box).not.toBeNull()
  expect(box!.width / box!.height).toBeCloseTo(16 / 9, 1)
  expect(box!.x).toBeGreaterThanOrEqual(0)
  expect(box!.x + box!.width).toBeLessThanOrEqual(360)

  const buttons = page.getByRole('button', { name: /^Seek to / })
  await expect(buttons).toHaveCount(CLAIMS.length)
  for (const b of await buttons.all()) {
    await b.scrollIntoViewIfNeeded()
    await b.click({ trial: true })
  }
})

test('error fallback: notice, new-tab watch link, and a timestamp click opens the moment', async ({ page, context }) => {
  await stubEmbed(page, 'error')
  await stubWatchPage(context)
  await stubVideoApi(page, CLAIMS)
  await page.goto(`/videos/${VIDEO_ID}`)
  await expect(page.getByText('This video cannot be played here.')).toBeVisible()
  const link = page.locator('.player--error a')
  await expect(link).toHaveAttribute('href', `https://www.youtube.com/watch?v=${VIDEO_ID}`)
  await expect(link).toHaveAttribute('target', '_blank')
  await expect(link).toHaveAttribute('rel', 'noopener noreferrer')

  const errors: Error[] = []
  page.on('pageerror', (e) => errors.push(e))
  const popup = context.waitForEvent('page')
  await page.getByRole('button', { name: READY_LABEL }).click()
  expect((await popup).url()).toBe(`https://www.youtube.com/watch?v=${VIDEO_ID}&t=75s`)
  expect(errors).toEqual([])
})

test('handshake timeout: a silent embed ends in the error state once the attempts are exhausted', async ({ page }) => {
  // The Player does not expose the transport's intervalMs/maxAttempts (and
  // web/src may not change), so instead of injecting small values the page
  // clock is advanced past the real ones (500 ms x 20 attempts): no sleep.
  await stubEmbed(page, 'silent')
  await page.clock.install()
  await stubVideoApi(page, CLAIMS)
  await page.goto(`/videos/${VIDEO_ID}`)
  await expect(page.locator('iframe')).toBeVisible()
  await expect.poll(() => listeningCount(page)).toBe(1)
  await expect(page.getByText('This video cannot be played here.')).toHaveCount(0)

  await page.clock.runFor(10_000)

  await expect(page.getByText('This video cannot be played here.')).toBeVisible()
  await expect(page.locator('.player--error a')).toHaveAttribute('target', '_blank')
})
