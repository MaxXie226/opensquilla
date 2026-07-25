import { expect, test, type Locator, type Page } from '@playwright/test'

const CONTROL_URL = '/control/'
const SESSION_KEY = 'agent:main:webchat:e2eworkbench'

const ARTIFACTS = [
  {
    id: 'workbench-notes',
    name: 'notes.txt',
    mime: 'text/plain',
    size: 18,
    download_url: '/api/v1/artifacts/workbench-notes',
  },
  {
    id: 'workbench-guide',
    name: 'guide.md',
    mime: 'text/markdown',
    size: 28,
    download_url: '/api/v1/artifacts/workbench-guide',
  },
  {
    id: 'workbench-demo',
    name: 'demo.html',
    mime: 'text/html',
    size: 80,
    download_url: '/api/v1/artifacts/workbench-demo',
  },
]

async function installWorkbenchGateway(
  page: Page,
  requests: Map<string, number> = new Map(),
  artifacts = ARTIFACTS,
) {
  await page.route('**/api/approvals', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ pending: [] }),
  }))
  await page.route('**/api/v1/artifacts/**', route => {
    const pathname = new URL(route.request().url()).pathname
    requests.set(pathname, (requests.get(pathname) || 0) + 1)
    if (pathname.endsWith('/workbench-notes')) {
      return route.fulfill({
        status: 200,
        contentType: 'text/plain',
        body: 'Workbench notes stay mounted.',
      })
    }
    if (pathname.endsWith('/workbench-guide')) {
      return route.fulfill({
        status: 200,
        contentType: 'text/markdown',
        body: '# Guide\n\nPersistent markdown preview.',
      })
    }
    if (pathname.endsWith('/workbench-demo')) {
      return route.fulfill({
        status: 200,
        contentType: 'text/html',
        body: '<!doctype html><title>Demo</title><p id="preview">Offline demo</p>',
      })
    }
    return route.fulfill({ status: 404, body: 'missing artifact' })
  })
  await page.routeWebSocket(/\/ws$/, ws => {
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(message => {
      let frame: Record<string, unknown>
      try {
        frame = JSON.parse(String(message)) as Record<string, unknown>
      } catch {
        return
      }
      if (frame.type !== 'req') return
      const method = String(frame.method || '')
      if (method === 'connect') {
        ws.send(JSON.stringify({ protocol: 3, policy: { tick_interval_ms: 30000 } }))
        return
      }
      if (method === 'chat.history') {
        ws.send(JSON.stringify({
          type: 'res',
          id: frame.id,
          ok: true,
          payload: {
            messages: [
              {
                role: 'user',
                text: 'Create previewable files.',
                id: 'workbench-user',
                timestamp: Math.floor(Date.now() / 1000) - 120,
              },
              {
                role: 'assistant',
                text: 'The files are ready.',
                id: 'workbench-assistant',
                timestamp: Math.floor(Date.now() / 1000) - 60,
                artifacts,
              },
            ],
            has_more: false,
          },
        }))
        return
      }
      const payloads: Record<string, unknown> = {
        'agents.list': { agents: [] },
        'commands.list_for_surface': { commands: [] },
        'config.get': {
          squilla_router: { enabled: false, rollout_phase: 'observe', tiers: {} },
          permissions: {},
          skills: {},
        },
        'onboarding.status': { audioConfigured: false },
        'sessions.list': { sessions: [], has_more: false },
        'sessions.messages.subscribe': {
          subscribed: true,
          replay_complete: true,
          current_stream_seq: 0,
          run_status: 'idle',
        },
        'usage.status': { sessions: [] },
      }
      ws.send(JSON.stringify({
        type: 'res',
        id: frame.id,
        ok: true,
        payload: payloads[method] ?? {},
      }))
    })
  })
}

async function openWorkbenchSession(
  page: Page,
  requests: Map<string, number> = new Map(),
  artifacts = ARTIFACTS,
) {
  await installWorkbenchGateway(page, requests, artifacts)
  await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_KEY))
  await expect(page.locator('.conn-pill')).toBeVisible({ timeout: 10000 })
  await expect(page.locator('.msg-artifact-chip')).toHaveCount(
    artifacts.length,
    { timeout: 10000 },
  )
}

async function visibleHeaderAction(page: Page, testId: string): Promise<Locator> {
  const action = page.locator(`[data-testid="${testId}"]:visible`).first()
  await expect(action).toBeVisible()
  return action
}

async function deliverablesHeaderAction(page: Page): Promise<Locator> {
  const direct = page.locator('[data-testid="chat-session-action-deliverables"]:visible').first()
  if (await direct.isVisible()) return direct

  const primary = page.locator('[data-testid="chat-header-primary-action"]:visible').first()
  if (await primary.isVisible() && await primary.getAttribute('data-action') === 'deliverables') {
    return primary
  }

  await page.getByTestId('chat-session-actions-trigger').click()
  return visibleHeaderAction(page, 'chat-session-action-deliverables')
}

test.describe('Application Workbench', () => {
  test('header opens the newest preview and restores the most recent concrete tab', async ({ page }) => {
    const requests = new Map<string, number>()
    await openWorkbenchSession(page, requests)

    const deliverables = await deliverablesHeaderAction(page)
    await deliverables.click()

    const workbench = page.getByTestId('workbench-host')
    await expect(workbench).toBeVisible()
    await expect(workbench).toHaveAttribute('role', 'complementary')
    await expect(workbench.locator('.workbench-host__single-title'))
      .toContainText('demo.html')
    await expect(workbench.locator('[data-workbench-item-id]')).toHaveCount(1)
    await expect(workbench.locator('.artifact-preview__frame--html')).toBeVisible()
    expect(requests.get('/api/v1/artifacts/workbench-demo')).toBe(1)

    await page.locator('.msg-artifact-chip', { hasText: 'notes.txt' })
      .getByRole('button', { name: 'Open notes.txt' })
      .click()
    await expect(workbench.getByRole('tablist')).toBeVisible()
    await expect(workbench.getByRole('tab')).toHaveCount(2)
    await expect(workbench.locator('.artifact-preview__text'))
      .toContainText('Workbench notes stay mounted.')
    expect(requests.get('/api/v1/artifacts/workbench-notes')).toBe(1)

    await page.locator('.msg-artifact-chip', { hasText: 'notes.txt' })
      .getByRole('button', { name: 'Open notes.txt' })
      .click()
    await expect(workbench.getByRole('tab')).toHaveCount(2)
    expect(requests.get('/api/v1/artifacts/workbench-notes')).toBe(1)

    await page.locator('.msg-artifact-chip', { hasText: 'guide.md' })
      .getByRole('button', { name: 'Open guide.md' })
      .click()
    await expect(workbench.getByRole('tab')).toHaveCount(3)
    await expect(workbench.locator('.artifact-preview__markdown')).toContainText('Guide')
    expect(requests.get('/api/v1/artifacts/workbench-guide')).toBe(1)

    await workbench.getByRole('button', { name: 'Collapse workbench' }).click()
    await expect(workbench).toBeHidden()
    await expect(page.getByTestId('workbench-host')).toHaveCount(1)
    await expect(page.locator('[data-workbench-item-id]')).toHaveCount(3)

    await (await deliverablesHeaderAction(page)).click()
    await expect(workbench).toBeVisible()
    await expect(workbench.locator('.artifact-preview__markdown')).toContainText('Guide')
    expect(requests.get('/api/v1/artifacts/workbench-guide')).toBe(1)
  })

  test('header falls back to the legacy deliverables drawer when nothing is previewable', async ({ page }) => {
    const downloadOnlyArtifacts = [{
      id: 'workbench-data',
      name: 'data.json',
      mime: 'application/json',
      size: 24,
      download_url: '/api/v1/artifacts/workbench-data',
    }]
    await openWorkbenchSession(page, new Map(), downloadOnlyArtifacts)

    await (await deliverablesHeaderAction(page)).click()

    await expect(page.getByTestId('workbench-host')).toHaveCount(0)
    await expect(page.getByRole('dialog', { name: 'Deliverables (1)' })).toBeVisible()
  })

  test('opening the same artifact card activates one existing item', async ({ page }) => {
    await openWorkbenchSession(page)
    const notes = page.locator('.msg-artifact-chip', { hasText: 'notes.txt' })
    const open = notes.getByRole('button', { name: 'Open notes.txt' })

    await open.click()
    const workbench = page.getByTestId('workbench-host')
    await expect(workbench).toBeVisible()
    await expect(workbench.locator('[data-workbench-item-id]')).toHaveCount(1)

    await workbench.getByRole('button', { name: 'Collapse workbench' }).click()
    await open.click()
    await expect(workbench).toBeVisible()
    await expect(workbench.locator('[data-workbench-item-id]')).toHaveCount(1)
    await expect(workbench.locator('.workbench-host__tabs')).toHaveCount(0)
  })

  test('mobile Workbench is a dialog and Escape collapses it with focus restored', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 667 })
    await openWorkbenchSession(page)

    const deliverables = await deliverablesHeaderAction(page)
    await deliverables.click()

    const workbench = page.getByTestId('workbench-host')
    await expect(workbench).toBeVisible()
    await expect(workbench).toHaveAttribute('role', 'dialog')
    await expect(workbench).toHaveAttribute('aria-modal', 'true')
    await expect(workbench).toHaveCSS('width', '375px')

    await page.keyboard.press('Escape')
    await expect(workbench).toBeHidden()
    await expect(page.getByTestId('chat-header-primary-action')).toBeFocused()
  })
})
