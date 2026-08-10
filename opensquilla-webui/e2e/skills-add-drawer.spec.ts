import { expect, test, type Page } from '@playwright/test'

const CONTROL_URL = '/control/skills'
const INSTALL_DELAY_MS = 120
const FAILED_INDEX = 4
const UNCHANGED_INDEX = 7

type RpcFrame = {
  id?: string | number
  method?: string
  params?: Record<string, unknown>
  type?: string
}

type SkillGatewayCapture = {
  inFlight: number
  installIdentifiers: string[]
  installSources: string[]
  listCalls: number
  maxInFlight: number
  searchParams: Array<Record<string, unknown>>
}

function response(id: string | number | undefined, payload: unknown) {
  return JSON.stringify({ type: 'res', id, ok: true, payload })
}

function lifecycle(
  readinessState: 'ready' | 'needs_setup' = 'ready',
) {
  return {
    install_state: 'tracked',
    load_state: 'loaded',
    selection_state: 'active',
    compatibility_state: 'instruction_only',
    readiness_state: readinessState,
  }
}

function catalogPayload() {
  return {
    skills: [
      {
        name: 'meta-synthetic',
        description: 'Synthetic meta Skill used only by the browser contract test.',
        kind: 'meta',
        layer: 'bundled',
        status: 'ready',
        lifecycle: {
          ...lifecycle(),
          install_state: 'untracked',
        },
      },
      {
        name: 'bundled-synthetic',
        description: 'Synthetic bundled Skill used only by the browser contract test.',
        kind: 'skill',
        layer: 'bundled',
        status: 'ready',
        lifecycle: {
          ...lifecycle(),
          install_state: 'untracked',
        },
      },
      {
        name: 'managed-synthetic',
        description: 'Synthetic managed Skill used only by the browser contract test.',
        kind: 'skill',
        layer: 'managed',
        status: 'needs_setup',
        lifecycle: lifecycle('needs_setup'),
      },
    ],
  }
}

function installPayload(identifier: string, index: number) {
  const suffix = String(index + 1).padStart(2, '0')
  const immutableRevision = `${suffix}`.repeat(20)

  if (index === FAILED_INDEX) {
    return {
      success: false,
      unchanged: false,
      name: `synthetic-skill-${suffix}`,
      message: 'Synthetic compatibility failure',
      installed: false,
      active: false,
      instruction_usable: false,
      effectiveFrom: 'next_turn',
      catalogGeneration: 41,
      diagnostics: [{
        code: 'DIALECT_FIELD_UNSUPPORTED',
        severity: 'error',
        phase: 'compatibility',
        blocking: true,
        message: 'Synthetic scoped capability is unsupported.',
        hint: 'Remove the unsupported field before retrying.',
        details: {
          upstreamText: '<em data-e2e="must-stay-text">literal upstream text</em>',
        },
      }],
      resolution: {
        source: 'github',
        canonicalIdentifier: identifier,
        publisher: 'synthetic-publisher',
        version: '1.0.0',
        immutableRevision,
        immutable: true,
      },
    }
  }

  return {
    success: true,
    unchanged: index === UNCHANGED_INDEX,
    name: `synthetic-skill-${suffix}`,
    message: index === UNCHANGED_INDEX ? 'Already current' : 'Installed',
    installed: true,
    active: true,
    instruction_usable: true,
    installId: `synthetic-install-${suffix}`,
    lifecycle: lifecycle(),
    effectiveFrom: 'next_turn',
    catalogGeneration: 42,
    diagnostics: index === UNCHANGED_INDEX
      ? [{
          code: 'ALREADY_CURRENT',
          severity: 'info',
          phase: 'store',
          blocking: false,
          message: 'The immutable artifact is already installed.',
        }]
      : [],
    resolution: {
      source: 'github',
      canonicalIdentifier: identifier,
      publisher: 'synthetic-publisher',
      version: '1.0.0',
      immutableRevision,
      immutable: true,
    },
  }
}

async function installSkillGateway(page: Page): Promise<SkillGatewayCapture> {
  const capture: SkillGatewayCapture = {
    inFlight: 0,
    installIdentifiers: [],
    installSources: [],
    listCalls: 0,
    maxInFlight: 0,
    searchParams: [],
  }

  await page.route('**/api/approvals', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ pending: [] }),
  }))

  await page.routeWebSocket(/\/ws$/, ws => {
    ws.onMessage(raw => {
      let frame: RpcFrame
      try {
        frame = JSON.parse(String(raw)) as RpcFrame
      } catch {
        return
      }
      if (frame.type !== 'req') return

      if (frame.method === 'connect') {
        ws.send(JSON.stringify({
          type: 'hello-ok',
          protocol: 3,
          server: { version: 'e2e', conn_id: 'skills-add-drawer-e2e' },
          features: {
            methods: [
              'skills.list',
              'skills.search',
              'skills.install',
              'exec.proposals.list',
              'exec.proposals.auto_enabled.list',
              'exec.proposals.settings.get',
            ],
            events: [],
          },
          snapshot: {},
          policy: { tick_interval_ms: 30_000 },
          auth: { principal: { isOwner: true } },
        }))
        return
      }

      if (frame.method === 'skills.list') {
        capture.listCalls += 1
        ws.send(response(frame.id, catalogPayload()))
        return
      }

      if (frame.method === 'skills.search') {
        capture.searchParams.push(frame.params || {})
        ws.send(response(frame.id, {
          results: [{
            name: 'synthetic-search-result',
            description: 'A synthetic registry result.',
            author: 'Synthetic Publisher',
            version: '1.0.0',
            source: 'clawhub',
            trust_level: 'community',
            installReference: 'synthetic-publisher/synthetic-search-result@1.0.0',
          }],
        }))
        return
      }

      if (frame.method === 'skills.install') {
        const identifier = String(frame.params?.identifier || '')
        const index = capture.installIdentifiers.length
        capture.installIdentifiers.push(identifier)
        capture.installSources.push(String(frame.params?.source || ''))
        capture.inFlight += 1
        capture.maxInFlight = Math.max(capture.maxInFlight, capture.inFlight)
        const delay = frame.params?.source === 'clawhub' ? 750 : INSTALL_DELAY_MS
        setTimeout(() => {
          capture.inFlight -= 1
          ws.send(response(frame.id, installPayload(identifier, index)))
        }, delay)
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
        'exec.proposals.list': { proposals: [] },
        'exec.proposals.auto_enabled.list': { skills: [] },
        'exec.proposals.settings.get': {
          settings: {
            available: false,
            enabled: false,
            on_dream_complete: false,
            auto_enable: false,
            auto_enable_max_risk: 'low',
          },
        },
        'sessions.list': { sessions: [], has_more: false },
        'usage.status': { sessions: [] },
      }
      ws.send(response(frame.id, payloads[String(frame.method)] ?? {}))
    })

    ws.send(JSON.stringify({
      type: 'event',
      event: 'connect.challenge',
      payload: { nonce: 'skills-add-drawer-e2e' },
    }))
  })

  return capture
}

async function openSkills(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('opensquilla-locale', 'en')
    localStorage.setItem('opensquilla-theme', 'light')
  })
  await page.goto(CONTROL_URL)
  await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 15_000 })
  await expect(page.getByTestId('skills-catalog')).toBeVisible({ timeout: 15_000 })
}

test.describe('Add Skill drawer', () => {
  test('keeps the catalog full-width until the accessible overlay is opened', async ({ page }) => {
    await installSkillGateway(page)
    await openSkills(page)

    const trigger = page.getByTestId('skills-add-trigger')
    const catalog = page.getByTestId('skills-catalog')
    await expect(trigger).toHaveAttribute('aria-expanded', 'false')
    await expect(page.getByRole('dialog', { name: 'Add Skill' })).toHaveCount(0)
    await expect(page.getByRole('tab', { name: 'Installed', exact: true })).toHaveCount(0)
    await expect(page.getByRole('tab', { name: 'Community', exact: true })).toHaveCount(0)

    const before = await catalog.boundingBox()
    expect(before).not.toBeNull()

    await trigger.click()
    const dialog = page.getByRole('dialog', { name: 'Add Skill' })
    await expect(dialog).toBeVisible()
    await expect(trigger).toHaveAttribute('aria-expanded', 'true')
    await expect(dialog.getByRole('button', { name: 'Close' })).toBeFocused()

    const after = await catalog.boundingBox()
    const drawer = await dialog.boundingBox()
    expect(after).not.toBeNull()
    expect(drawer).not.toBeNull()
    expect(Math.abs(after!.x - before!.x)).toBeLessThanOrEqual(1)
    expect(Math.abs(after!.width - before!.width)).toBeLessThanOrEqual(1)
    expect(Math.abs(drawer!.width - 460)).toBeLessThanOrEqual(1)

    await page.keyboard.press('Shift+Tab')
    await expect(dialog.locator('#skills-add-github-input')).toBeFocused()
    await page.keyboard.press('Tab')
    await expect(dialog.getByRole('button', { name: 'Close' })).toBeFocused()

    await page.keyboard.press('Escape')
    await expect(dialog).toHaveCount(0)
    await expect(trigger).toBeFocused()
    await expect(trigger).toHaveAttribute('aria-expanded', 'false')

    await trigger.click()
    await expect(dialog).toBeVisible()
    await page.getByTestId('skills-add-scrim').click({ position: { x: 2, y: 2 } })
    await expect(dialog).toHaveCount(0)
    await expect(trigger).toBeFocused()
  })

  test('installs a ClawHub search result by its exact server reference', async ({ page }) => {
    const capture = await installSkillGateway(page)
    await openSkills(page)

    await page.getByTestId('skills-add-trigger').click()
    const dialog = page.getByRole('dialog', { name: 'Add Skill' })
    await dialog.getByRole('tab', { name: 'ClawHub', exact: true }).click()
    await dialog.locator('#skills-add-clawhub-query').fill('synthetic search')
    await dialog.getByRole('button', { name: 'Search', exact: true }).click()

    await expect(dialog.locator('.sk-add-result')).toHaveCount(1)
    await expect(dialog.locator('.sk-add-result')).toContainText('Synthetic Publisher')
    expect(capture.searchParams).toEqual([{
      query: 'synthetic search',
      limit: 20,
      source: 'clawhub',
    }])

    const searchResult = dialog.locator('.sk-add-result')
    const searchResultAction = searchResult.getByRole('button', { name: 'Install', exact: true })
    await searchResultAction.click()
    await expect(searchResult).toHaveAttribute('data-status', 'installing')
    await expect(searchResult.getByRole('button')).toHaveAttribute('aria-busy', 'true')
    await expect(searchResult.getByRole('button')).toContainText('Installing')
    await expect(dialog.locator('.sk-add-queue-item[data-status="installing"]')).toHaveCount(1)
    await expect.poll(() => capture.installIdentifiers.length).toBe(1)
    expect(capture.installIdentifiers).toEqual([
      'synthetic-publisher/synthetic-search-result@1.0.0',
    ])
    expect(capture.installSources).toEqual(['clawhub'])
    await expect(searchResult.getByRole('button')).toContainText('Installed')
  })

  test('installs a deduplicated 15-item batch serially and preserves progress across close', async ({ page }) => {
    const capture = await installSkillGateway(page)
    await openSkills(page)
    await expect.poll(() => capture.listCalls).toBe(1)
    const initialListCalls = capture.listCalls

    const references = Array.from({ length: 15 }, (_, index) => {
      const number = String(index + 1).padStart(2, '0')
      return `https://github.com/synthetic/skill-${number}/tree/${number.repeat(20)}/skill`
    })

    const trigger = page.getByTestId('skills-add-trigger')
    await trigger.click()
    const dialog = page.getByRole('dialog', { name: 'Add Skill' })
    const input = dialog.locator('#skills-add-github-input')
    await input.fill([...references, references[2]].join('\n'))
    await dialog.getByTestId('skills-install-github').click()

    await expect.poll(() => capture.installIdentifiers.length).toBeGreaterThan(0)
    await expect(dialog.locator('.sk-add-queue-item')).toHaveCount(15)
    await dialog.getByRole('button', { name: 'Close' }).click()
    await expect(dialog).toHaveCount(0)

    await expect.poll(() => capture.installIdentifiers.length).toBeGreaterThanOrEqual(3)
    await trigger.click()
    await expect(dialog).toBeVisible()
    await expect(dialog.locator('.sk-add-queue-item')).toHaveCount(15)

    await expect.poll(() => capture.installIdentifiers.length, { timeout: 15_000 }).toBe(15)
    await expect.poll(() => capture.inFlight, { timeout: 15_000 }).toBe(0)
    await expect(dialog.locator('.sk-add-queue-item[data-status="queued"]')).toHaveCount(0)
    await expect(dialog.locator('.sk-add-queue-item[data-status="installing"]')).toHaveCount(0)
    await expect(dialog.locator('.sk-add-queue-item[data-status="installed"]')).toHaveCount(13)
    await expect(dialog.locator('.sk-add-queue-item[data-status="unchanged"]')).toHaveCount(1)
    await expect(dialog.locator('.sk-add-queue-item[data-status="failed"]')).toHaveCount(1)

    expect(capture.maxInFlight).toBe(1)
    expect(capture.installIdentifiers).toEqual(references)
    await expect.poll(() => capture.listCalls).toBe(initialListCalls + 1)

    const failed = dialog.locator('.sk-add-queue-item[data-status="failed"]')
    await expect(failed).toContainText('Synthetic compatibility failure')
    await failed.locator('summary').click()
    await expect(failed).toContainText('DIALECT_FIELD_UNSUPPORTED')
    await expect(failed).toContainText('literal upstream text')
    await expect(failed.locator('[data-e2e="must-stay-text"]')).toHaveCount(0)

    const unchanged = dialog.locator('.sk-add-queue-item[data-status="unchanged"]')
    await expect(unchanged).toContainText('synthetic-skill-08')
    await expect(unchanged).toContainText('Available next turn')
    await unchanged.locator('summary').click()
    await expect(unchanged).toContainText('ALREADY_CURRENT')

    await expect(dialog.locator('#skills-add-github-input')).toHaveValue(references[FAILED_INDEX])
    await expect(dialog.locator('.sk-add-queue-item').last()).toHaveAttribute('data-status', 'installed')
  })

  test('uses a full-width drawer at the 390px breakpoint', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    await installSkillGateway(page)
    await openSkills(page)

    await page.getByTestId('skills-add-trigger').click()
    const dialog = page.getByRole('dialog', { name: 'Add Skill' })
    await expect(dialog).toBeVisible()
    await expect.poll(async () => Math.abs((await dialog.boundingBox())?.x ?? 390))
      .toBeLessThanOrEqual(1)
    const box = await dialog.boundingBox()
    expect(box).not.toBeNull()
    expect(Math.abs(box!.x)).toBeLessThanOrEqual(1)
    expect(Math.abs(box!.width - 390)).toBeLessThanOrEqual(1)
    expect(Math.abs(box!.height - 844)).toBeLessThanOrEqual(1)
  })
})
