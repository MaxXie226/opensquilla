// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia } from 'pinia'
import type { OpenSquillaAppComposition } from '@opensquilla/ui-foundation'
import { createWebPlatform } from '@/platform/web'
import { createPublicWebUiRoutes } from '@/router'
import { createPublicWebUiComposition, getPublicWebUiRuntimeState } from './root'

function installBrowserFixture(): void {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }))
}

describe('public WebUI composition', () => {
  const compositions: OpenSquillaAppComposition[] = []

  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
    delete window.opensquillaDesktop
    installBrowserFixture()
  })

  afterEach(async () => {
    await Promise.all(compositions.splice(0).map(composition => composition.dispose()))
  })

  async function createComposition(): Promise<OpenSquillaAppComposition> {
    const composition = await createPublicWebUiComposition({
      pinia: createPinia(),
      platform: createWebPlatform(),
    })
    compositions.push(composition)
    return composition
  }

  it('registers every complete community route without changing deep links', async () => {
    const composition = await createComposition()
    const routes = createPublicWebUiRoutes(composition, createWebPlatform())

    expect(routes.map(route => route.path)).toEqual([
      '/',
      '/chat',
      '/chat/new',
      '/sessions',
      '/overview',
      '/usage',
      '/logs',
      '/approvals',
      '/agents',
      '/skills',
      '/channels',
      '/cron',
      '/changelog',
      '/health',
      '/settings',
      '/settings/:section',
      '/config',
      '/setup',
      '/:pathMatch(.*)*',
    ])
    expect(composition.registry.routes).toHaveLength(14)
    expect(composition.registry.pages).toHaveLength(10)
    expect(composition.registry.navigation).toHaveLength(4)
    expect(composition.registry.state).toHaveLength(3)
  })

  it('keeps shared route views represented by one page contribution', async () => {
    const composition = await createComposition()
    const routes = createPublicWebUiRoutes(composition, createWebPlatform())
    const routeAt = (path: string) => routes.find(route => route.path === path)

    expect(routeAt('/chat')?.component).toBe(routeAt('/chat/new')?.component)
    expect(routeAt('/overview')?.component).toBe(routeAt('/usage')?.component)
    expect(routeAt('/skills')?.component).toBe(routeAt('/channels')?.component)
    expect(routeAt('/logs')?.component).not.toBe(routeAt('/overview')?.component)
  })

  it('loads bundled pages through the Foundation page boundary', async () => {
    const composition = await createComposition()

    await expect(
      composition.loadPage<Record<string, unknown>>(
        'opensquilla.webui.fallback.page.not-found',
      ),
    ).resolves.toHaveProperty('default')
  })

  it('creates isolated app and RPC stores for separate product compositions', async () => {
    const first = await createComposition()
    const second = await createComposition()
    const firstState = getPublicWebUiRuntimeState(first)
    const secondState = getPublicWebUiRuntimeState(second)

    expect(firstState.appStore).not.toBe(secondState.appStore)
    expect(firstState.rpcStore).not.toBe(secondState.rpcStore)
    expect(firstState.workbench.store).not.toBe(secondState.workbench.store)
    expect(firstState.workbench.registry).not.toBe(secondState.workbench.registry)
    firstState.appStore.setSidebarOpen(false)
    expect(firstState.appStore.sidebarOpen).toBe(false)
    expect(secondState.appStore.sidebarOpen).toBe(true)
  })

  it('uses structured unsupported results when no native host exists', async () => {
    const composition = await createComposition()

    expect(composition.native.bridgeVersion).toBeNull()
    expect(composition.native.capabilities).toEqual([])
    await expect(composition.native.invoke({
      capability: 'opensquilla.host.open-artifact',
    })).resolves.toMatchObject({
      ok: false,
      error: {
        code: 'unsupported',
        capability: 'opensquilla.host.open-artifact',
      },
    })
  })

  it('contains only public, product-neutral feature declarations', async () => {
    const composition = await createComposition()
    const serialized = JSON.stringify(composition.registry)

    expect(composition.registry.features.map(feature => feature.id)).toEqual([
      'opensquilla.webui.shell',
      'opensquilla.webui.chat',
      'opensquilla.webui.sessions',
      'opensquilla.webui.observability',
      'opensquilla.webui.agents',
      'opensquilla.webui.capabilities',
      'opensquilla.webui.automation',
      'opensquilla.webui.editorial',
      'opensquilla.webui.settings',
      'opensquilla.webui.workbench',
      'opensquilla.webui.fallback',
    ])
    expect(serialized).not.toMatch(/private|entitlement|product manifest/i)
  })
})
