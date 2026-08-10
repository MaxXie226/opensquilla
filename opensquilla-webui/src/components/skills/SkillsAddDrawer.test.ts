// @vitest-environment happy-dom

import { createApp, defineComponent, h, nextTick, ref } from 'vue'
import { afterEach, describe, expect, it } from 'vitest'
import i18n from '@/i18n'
import type { SkillInstallQueueItem } from '@/composables/skills/useSkillRegistry'
import type { RegistryResult } from '@/types/skills'
import SkillsAddDrawer from './SkillsAddDrawer.vue'

const apps: ReturnType<typeof createApp>[] = []

afterEach(() => {
  while (apps.length) apps.pop()?.unmount()
  document.body.innerHTML = ''
})

function mountDrawer(options: {
  queue?: SkillInstallQueueItem[]
  queueRunning?: boolean
  mutationBlocked?: boolean
  results?: RegistryResult[]
} = {}) {
  const open = ref(false)
  const githubUrl = ref('https://github.com/acme/demo')
  const registryQuery = ref('demo')
  const queue = ref(options.queue || [])
  const queueRunning = ref(options.queueRunning || false)
  const installed: Array<[string, string, string]> = []

  const Root = defineComponent({
    setup() {
      return () => h('div', [
        h('button', {
          id: 'drawer-trigger',
          onClick: () => { open.value = true },
        }, 'Open drawer'),
        h(SkillsAddDrawer, {
          open: open.value,
          registryQuery: registryQuery.value,
          githubUrl: githubUrl.value,
          results: options.results || [],
          loading: false,
          registryDiagnostics: [],
          registrySearchError: '',
          queue: queue.value,
          queueRunning: queueRunning.value,
          mutationBlocked: options.mutationBlocked || false,
          queueRefreshWarning: '',
          'onUpdate:registryQuery': (value: string) => { registryQuery.value = value },
          'onUpdate:githubUrl': (value: string) => { githubUrl.value = value },
          onClose: () => { open.value = false },
          onInstall: (identifier: string, source: string, name: string) => {
            installed.push([identifier, source, name])
          },
        }),
      ])
    },
  })

  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp(Root)
  app.use(i18n)
  app.mount(host)
  apps.push(app)

  return { host, open, githubUrl, queue, queueRunning, installed }
}

describe('SkillsAddDrawer', () => {
  it('is absent by default, opens on GitHub, closes by scrim, and restores focus', async () => {
    const { open } = mountDrawer()
    const trigger = document.querySelector<HTMLButtonElement>('#drawer-trigger')!

    expect(document.querySelector('.sk-add-drawer')).toBeNull()
    trigger.focus()
    trigger.click()
    await nextTick()
    await nextTick()

    const drawer = document.querySelector<HTMLElement>('.sk-add-drawer')
    expect(drawer).not.toBeNull()
    expect(drawer?.getAttribute('role')).toBe('dialog')
    expect(document.querySelector('#skills-add-tab-github')?.getAttribute('aria-selected')).toBe('true')
    expect(document.activeElement).toBe(drawer?.querySelector('.sk-add-drawer__close'))

    document.querySelector<HTMLElement>('[data-testid="skills-add-scrim"]')?.click()
    await nextTick()
    expect(open.value).toBe(false)
    await new Promise(resolve => setTimeout(resolve, 320))
    expect(document.querySelector('.sk-add-drawer')).toBeNull()
    expect(document.activeElement).toBe(trigger)
  })

  it('keeps queue results across close and reopen and tolerates an old Gateway payload', async () => {
    const queue: SkillInstallQueueItem[] = [{
      id: '["github","legacy"]',
      identifier: 'legacy',
      source: 'github',
      displayName: 'legacy-skill',
      status: 'installed',
      result: { success: true, name: 'legacy-skill' },
    }]
    mountDrawer({ queue })
    const trigger = document.querySelector<HTMLButtonElement>('#drawer-trigger')!
    trigger.click()
    await nextTick()
    expect(document.querySelector('.sk-add-queue-item')?.textContent).toContain('legacy-skill')

    document.querySelector<HTMLButtonElement>('.sk-add-drawer__close')?.click()
    await nextTick()
    trigger.click()
    await nextTick()

    expect(document.querySelector('.sk-add-queue-item')?.getAttribute('data-status')).toBe('installed')
    expect(document.querySelector('.sk-add-queue-item')?.textContent).toContain('legacy-skill')
  })

  it('renders upstream diagnostic details as text and disables mutations while running', async () => {
    const queue: SkillInstallQueueItem[] = [{
      id: '["github","failed"]',
      identifier: 'failed',
      source: 'github',
      displayName: 'failed-skill',
      status: 'failed',
      error: 'Rejected',
      result: {
        success: false,
        diagnostics: [{
          code: 'DIALECT_FIELD_UNSUPPORTED',
          severity: 'error',
          phase: 'compatibility',
          blocking: true,
          message: 'Unsupported field.',
          details: { upstreamText: '<em data-e2e="must-stay-text">literal text</em>' },
        }],
      },
    }]
    mountDrawer({ queue, queueRunning: true })
    document.querySelector<HTMLButtonElement>('#drawer-trigger')?.click()
    await nextTick()

    const input = document.querySelector<HTMLTextAreaElement>('#skills-add-github-input')
    const installButton = document.querySelector<HTMLButtonElement>('[data-testid="skills-install-github"]')
    const retry = document.querySelector<HTMLButtonElement>('.sk-add-retry')
    expect(input?.disabled).toBe(true)
    expect(installButton?.disabled).toBe(true)
    expect(installButton?.getAttribute('aria-busy')).toBe('true')
    expect(installButton?.classList.contains('sk-add-primary--busy')).toBe(true)
    expect(retry?.disabled).toBe(true)

    document.querySelector<HTMLDetailsElement>('.sk-add-diagnostics')!.open = true
    await nextTick()
    expect(document.querySelector('.sk-add-diagnostics')?.textContent).toContain('literal text')
    expect(document.querySelector('[data-e2e="must-stay-text"]')).toBeNull()
  })

  it('disables install entry points without showing queue progress when another mutation owns the surface', async () => {
    mountDrawer({
      mutationBlocked: true,
      results: [{
        name: 'Demo',
        installReference: '@acme/demo',
        source: 'clawhub',
      }],
    })
    document.querySelector<HTMLButtonElement>('#drawer-trigger')?.click()
    await nextTick()

    const githubInstall = document.querySelector<HTMLButtonElement>('[data-testid="skills-install-github"]')
    expect(document.querySelector<HTMLTextAreaElement>('#skills-add-github-input')?.disabled).toBe(true)
    expect(githubInstall?.disabled).toBe(true)
    expect(githubInstall?.getAttribute('aria-busy')).toBe('false')

    document.querySelector<HTMLButtonElement>('#skills-add-tab-clawhub')?.click()
    await nextTick()
    expect(document.querySelector<HTMLButtonElement>('.sk-add-result .btn--primary')?.disabled).toBe(true)
  })

  it('installs the exact ClawHub installReference returned by search', async () => {
    const { installed } = mountDrawer({
      results: [{
        name: 'Demo',
        identifier: 'demo',
        installReference: '@verified/demo@1.2.3',
        source: 'clawhub',
        author: 'Verified',
        version: '1.2.3',
      }],
    })
    document.querySelector<HTMLButtonElement>('#drawer-trigger')?.click()
    await nextTick()
    document.querySelector<HTMLButtonElement>('#skills-add-tab-clawhub')?.click()
    await nextTick()
    document.querySelector<HTMLButtonElement>('.sk-add-result .btn--primary')?.click()

    expect(installed).toEqual([['@verified/demo@1.2.3', 'clawhub', 'Demo']])
  })
})
