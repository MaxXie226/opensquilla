import { expect, test, type Page } from '@playwright/test'

const CONTROL_URL = '/control/'

interface RouteProbe {
  readonly path: string
  readonly expectedPath?: RegExp
  readonly selector: string
}

const PUBLIC_ROUTE_PROBES: readonly RouteProbe[] = [
  { path: 'chat/new', selector: '.chat-textarea' },
  { path: 'sessions', selector: '.content' },
  { path: 'overview', selector: '.content' },
  { path: 'usage', selector: '.content' },
  { path: 'logs', selector: '.content' },
  { path: 'approvals', expectedPath: /\/control\/sessions$/, selector: '.content' },
  { path: 'agents', selector: '.content' },
  { path: 'skills', selector: '.content' },
  { path: 'channels', selector: '.content' },
  { path: 'cron', selector: '.content' },
  { path: 'changelog', selector: '.content' },
  { path: 'health', expectedPath: /\/control\/overview$/, selector: '.content' },
  { path: 'settings', selector: '.settings-modal' },
  { path: 'settings/appearance', selector: '.settings-modal' },
  { path: 'config', expectedPath: /\/control\/settings$/, selector: '.settings-modal' },
  { path: 'setup', expectedPath: /\/control\/settings\/auto$/, selector: '.settings-modal' },
  { path: 'route-that-does-not-exist', selector: '.content' },
]

async function assertCompositionRoute(page: Page, probe: RouteProbe): Promise<void> {
  const pageErrors: string[] = []
  page.on('pageerror', error => pageErrors.push(error.message))

  await page.goto(`${CONTROL_URL}${probe.path}`)
  await expect(page.locator('.conn-pill')).toBeVisible({ timeout: 10_000 })
  if (probe.expectedPath) {
    await expect(page).toHaveURL(probe.expectedPath)
  } else {
    await expect(page).toHaveURL(new RegExp(`/control/${probe.path.replace('/', '\\/')}$`))
  }
  await expect(page.locator(probe.selector).first()).toBeVisible({ timeout: 10_000 })
  await expect(page.getByRole('alert', {
    name: 'OpenSquilla could not start. Reload the page or update the client.',
  })).toHaveCount(0)
  expect(pageErrors).toEqual([])
}

test.describe('Public WebUI composition', () => {
  for (const probe of PUBLIC_ROUTE_PROBES) {
    test(`keeps the ${probe.path} route available`, async ({ page }) => {
      await assertCompositionRoute(page, probe)
    })
  }
})
