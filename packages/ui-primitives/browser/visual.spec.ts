import { expect, test } from '@playwright/test'

test.beforeEach(async ({ page }) => {
  await page.goto('/')
})

test('keeps the public primitive visual contract', async ({ page }) => {
  const fixture = page.locator('#fixture')
  await expect(fixture).toBeVisible()
  await expect(fixture).toHaveCSS('width', '720px')
  await expect(fixture).toHaveCSS('height', '400px')
  await expect(fixture).toHaveScreenshot('public-primitives.png', {
    animations: 'disabled',
    caret: 'hide',
    // Native font rasterization differs across macOS and Linux. The fixed
    // canvas plus exact semantic style assertions below keep layout and token
    // regressions strict while allowing platform antialiasing differences.
    maxDiffPixelRatio: 0.05,
  })

  const primary = page.locator('#primary')
  const styles = await primary.evaluate((element) => {
    const computed = getComputedStyle(element)
    return {
      background: computed.backgroundColor,
      color: computed.color,
      radius: computed.borderRadius,
      minHeight: computed.minHeight,
    }
  })
  expect(styles).toEqual({
    background: 'rgb(242, 106, 27)',
    color: 'rgb(22, 11, 2)',
    radius: '10px',
    minHeight: '36px',
  })
})

test('supports keyboard-only dialog navigation and restoration', async ({ page }) => {
  const open = page.locator('#open-dialog')
  await open.focus()
  await page.keyboard.press('Enter')

  const dialog = page.getByRole('dialog', { name: 'Confirm action' })
  await expect(dialog).toBeVisible()
  await expect(dialog).toHaveAttribute('aria-modal', 'true')
  await expect(page.locator('#dialog-cancel')).toBeFocused()

  await page.keyboard.press('Shift+Tab')
  await expect(page.locator('#dialog-confirm')).toBeFocused()
  await page.keyboard.press('Escape')

  await expect(dialog).toBeHidden()
  await expect(open).toBeFocused()
})

test('keeps light and dark semantic grounds readable', async ({ page }) => {
  for (const [theme, expected] of [
    ['dark', { background: 'rgb(24, 24, 26)', text: 'rgb(245, 245, 247)' }],
    ['light', { background: 'rgb(247, 247, 248)', text: 'rgb(29, 29, 31)' }],
  ] as const) {
    await page.evaluate((value) => {
      document.documentElement.dataset.theme = value
    }, theme)
    const colors = await page.locator('body').evaluate((element) => {
      const computed = getComputedStyle(element)
      return {
        background: computed.backgroundColor,
        text: computed.color,
      }
    })
    expect(colors).toEqual(expected)
  }
})
