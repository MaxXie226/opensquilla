// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'

const settle = () => new Promise((resolve) => setTimeout(resolve, 20))

function setDesktopApi(api: unknown): void {
  ;(window as unknown as { opensquillaDesktop?: unknown }).opensquillaDesktop = api
}

function desktopApi(overrides: Record<string, unknown> = {}) {
  return {
    getOsLocale: async () => 'en',
    isAutoUpdateEnabled: async () => true,
    getGatewayStatus: async () => ({
      url: 'http://127.0.0.1:1',
      port: 1,
      owned: true,
      status: 'ready',
      logPath: '',
    }),
    ...overrides,
  }
}

async function mountPanel(api: ReturnType<typeof desktopApi>) {
  vi.resetModules()
  document.body.innerHTML = ''
  setDesktopApi(api)
  const { createApp, nextTick } = await import('vue')
  const i18n = (await import('@/i18n')).default
  i18n.global.locale.value = 'en'
  const Component = (await import('./DesktopRuntimePanel.vue')).default
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(Component)
  app.use(i18n)
  app.mount(el)
  await settle()
  await nextTick()
  const { toasts } = (await import('@/composables/useToasts')).useToasts()
  toasts.value = []
  return { app, el, toasts }
}

function findCloseBehaviorSelect(el: HTMLElement): HTMLSelectElement {
  const select = el.querySelector<HTMLSelectElement>(
    '[data-testid="desktop-close-behavior-select"]',
  )
  if (!select) throw new Error('Desktop close behavior select was not rendered')
  return select
}

beforeEach(() => setDesktopApi(undefined))

describe('DesktopRuntimePanel close behavior preference', () => {
  it('stays hidden when an older desktop shell does not expose the preference bridge', async () => {
    const { app, el } = await mountPanel(desktopApi())

    expect(el.querySelector('[data-testid="desktop-close-behavior"]')).toBeNull()
    app.unmount()
  })

  it('loads the current preference and saves a selection immediately', async () => {
    const getDesktopPreferences = vi.fn(async () => ({
      mainWindowCloseBehavior: 'quit' as const,
      canRunInBackground: true,
      platform: 'darwin' as const,
    }))
    const saveDesktopPreferences = vi.fn(async (payload: {
      mainWindowCloseBehavior: 'background' | 'quit' | 'ask'
    }) => ({
      mainWindowCloseBehavior: payload.mainWindowCloseBehavior,
      canRunInBackground: true,
      platform: 'darwin' as const,
    }))
    const { app, el } = await mountPanel(desktopApi({
      getDesktopPreferences,
      saveDesktopPreferences,
    }))
    const select = findCloseBehaviorSelect(el)

    expect(getDesktopPreferences).toHaveBeenCalledTimes(1)
    expect(select.value).toBe('quit')

    select.value = 'ask'
    select.dispatchEvent(new Event('change', { bubbles: true }))
    await settle()

    expect(saveDesktopPreferences).toHaveBeenCalledWith({
      mainWindowCloseBehavior: 'ask',
    })
    expect(select.value).toBe('ask')
    app.unmount()
  })

  it('rolls back the selection and shows a danger toast when saving fails', async () => {
    const saveDesktopPreferences = vi.fn(async () => {
      throw new Error('preferences are read-only')
    })
    const { app, el, toasts } = await mountPanel(desktopApi({
      getDesktopPreferences: async () => ({
        mainWindowCloseBehavior: 'background' as const,
        canRunInBackground: true,
        platform: 'win32' as const,
      }),
      saveDesktopPreferences,
    }))
    const select = findCloseBehaviorSelect(el)

    select.value = 'ask'
    select.dispatchEvent(new Event('change', { bubbles: true }))
    await settle()

    expect(select.value).toBe('background')
    expect(toasts.value[toasts.value.length - 1]).toMatchObject({
      message: 'Could not save the window close setting: preferences are read-only',
      tone: 'danger',
    })
    app.unmount()
  })

  it('limits close behavior to Quit when the shell cannot stay running', async () => {
    const { app, el } = await mountPanel(desktopApi({
      getDesktopPreferences: async () => ({
        mainWindowCloseBehavior: 'quit' as const,
        canRunInBackground: false,
        platform: 'linux' as const,
      }),
      saveDesktopPreferences: async () => ({
        mainWindowCloseBehavior: 'quit' as const,
        canRunInBackground: false,
        platform: 'linux' as const,
      }),
    }))

    const background = findCloseBehaviorSelect(el)
      .querySelector<HTMLOptionElement>('option[value="background"]')
    const ask = findCloseBehaviorSelect(el)
      .querySelector<HTMLOptionElement>('option[value="ask"]')
    expect(background?.disabled).toBe(true)
    expect(ask?.disabled).toBe(true)
    expect(el.textContent).toContain('Background mode is unavailable on this platform.')
    app.unmount()
  })
})
