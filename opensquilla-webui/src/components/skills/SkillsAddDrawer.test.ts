// @vitest-environment happy-dom

import { createApp, defineComponent, h, nextTick, ref } from 'vue'
import { afterEach, describe, expect, it } from 'vitest'
import i18n from '@/i18n'
import type {
  SkillInstallActivities,
  SkillInstallQueueItem,
  SkillInstallSource,
} from '@/composables/skills/useSkillRegistry'
import type { RegistryResult } from '@/types/skills'
import SkillsAddDrawer from './SkillsAddDrawer.vue'

const apps: ReturnType<typeof createApp>[] = []

afterEach(() => {
  while (apps.length) apps.pop()?.unmount()
  document.body.innerHTML = ''
})

function mountDrawer(options: {
  queue?: SkillInstallQueueItem[]
  activities?: SkillInstallActivities
  runningSource?: SkillInstallSource | null
  mutationBlocked?: boolean
  results?: RegistryResult[]
} = {}) {
  const open = ref(false)
  const githubUrl = ref('https://github.com/acme/demo')
  const registryQuery = ref('demo')
  const queue = ref(options.queue || [])
  const queueSource: SkillInstallSource = queue.value[0]?.source === 'clawhub'
    ? 'clawhub'
    : 'github'
  const activities = ref<SkillInstallActivities>(options.activities || {
    clawhub: {
      items: queueSource === 'clawhub' ? queue.value : [],
      refreshWarning: '',
    },
    github: {
      items: queueSource === 'github' ? queue.value : [],
      refreshWarning: '',
    },
  })
  const runningSource = ref<SkillInstallSource | null>(options.runningSource ?? null)
  const installed: Array<[string, string, string]> = []
  const retried: string[] = []
  const cleared: SkillInstallSource[] = []

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
          activities: activities.value,
          runningSource: runningSource.value,
          mutationBlocked: options.mutationBlocked || false,
          'onUpdate:registryQuery': (value: string) => { registryQuery.value = value },
          'onUpdate:githubUrl': (value: string) => { githubUrl.value = value },
          onClose: () => { open.value = false },
          onInstall: (identifier: string, source: string, name: string) => {
            installed.push([identifier, source, name])
          },
          onRetry: (id: string) => { retried.push(id) },
          onClearActivity: (source: SkillInstallSource) => {
            cleared.push(source)
            activities.value[source] = { items: [], refreshWarning: '' }
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

  return {
    host,
    open,
    githubUrl,
    queue,
    activities,
    runningSource,
    installed,
    retried,
    cleared,
  }
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
    expect((document.querySelector('.sk-add-activity-body') as HTMLElement)?.style.display)
      .toBe('none')

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
        }, {
          code: 'NO_DETAILS',
          severity: 'warning',
          phase: 'archive',
          blocking: false,
          message: 'No structured details.',
          details: {},
        }],
      },
    }]
    mountDrawer({ queue, runningSource: 'github' })
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
    expect(document.querySelectorAll('.sk-add-diagnostics pre')).toHaveLength(1)
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

  it('shows install activity before long ClawHub search results', async () => {
    const queue: SkillInstallQueueItem[] = [{
      id: '["clawhub","@verified/demo"]',
      identifier: '@verified/demo',
      source: 'clawhub',
      displayName: 'demo',
      status: 'failed',
      error: 'Rejected',
    }]
    mountDrawer({
      queue,
      results: [{
        name: 'Demo',
        installReference: '@verified/demo',
        source: 'clawhub',
      }],
    })
    document.querySelector<HTMLButtonElement>('#drawer-trigger')?.click()
    await nextTick()
    document.querySelector<HTMLButtonElement>('#skills-add-tab-clawhub')?.click()
    await nextTick()

    const activity = document.querySelector<HTMLElement>('.sk-add-queue')!
    const results = document.querySelector<HTMLElement>('.sk-add-results')!
    expect(activity.compareDocumentPosition(results) & Node.DOCUMENT_POSITION_FOLLOWING)
      .toBeTruthy()
  })

  it('shows the active queue state on the clicked search result and retries failures in place', async () => {
    const operationKey = '["clawhub","@verified/demo"]'
    const queue: SkillInstallQueueItem[] = [{
      id: operationKey,
      identifier: '@verified/demo',
      source: 'clawhub',
      displayName: 'Demo',
      status: 'installing',
    }]
    const mounted = mountDrawer({
      queue,
      runningSource: 'clawhub',
      results: [{
        name: 'Demo',
        installReference: '@verified/demo',
        source: 'clawhub',
      }],
    })
    document.querySelector<HTMLButtonElement>('#drawer-trigger')?.click()
    await nextTick()
    document.querySelector<HTMLButtonElement>('#skills-add-tab-clawhub')?.click()
    await nextTick()

    const result = document.querySelector<HTMLElement>('.sk-add-result')!
    const action = result.querySelector<HTMLButtonElement>('button')!
    expect(result.dataset.status).toBe('installing')
    expect(action.textContent).toContain('Installing')
    expect(action.getAttribute('aria-busy')).toBe('true')
    expect(action.querySelector('.sk-spinner')).not.toBeNull()

    mounted.queue.value[0].status = 'failed'
    mounted.queue.value[0].error = 'Manifest rejected'
    mounted.runningSource.value = null
    await nextTick()

    expect(result.dataset.status).toBe('failed')
    expect(action.textContent).toContain('Retry')
    expect(result.textContent).toContain('Manifest rejected')
    action.click()
    expect(mounted.retried).toEqual([operationKey])
  })

  it('keeps source activity isolated and exposes inactive failures on the source tab only', async () => {
    const activities: SkillInstallActivities = {
      clawhub: {
        items: [{
          id: '["clawhub","@acme/failed"]',
          identifier: '@acme/failed',
          source: 'clawhub',
          displayName: 'Claw failure',
          status: 'failed',
          error: 'Manifest rejected',
          result: { success: false, installed: false },
        }],
        refreshWarning: '',
      },
      github: {
        items: [{
          id: '["github","acme/ready"]',
          identifier: 'acme/ready',
          source: 'github',
          displayName: 'GitHub success',
          status: 'installed',
          result: { success: true, installed: true },
        }],
        refreshWarning: '',
      },
    }
    mountDrawer({ activities })
    document.querySelector<HTMLButtonElement>('#drawer-trigger')?.click()
    await nextTick()

    const githubActivity = document.querySelector<HTMLElement>('.sk-add-queue[data-source="github"]')!
    expect(githubActivity.textContent).toContain('GitHub success')
    expect(githubActivity.textContent).not.toContain('Claw failure')
    expect(githubActivity.querySelector<HTMLElement>('.sk-add-activity-body')?.style.display)
      .toBe('none')
    expect(document.querySelector('#skills-add-tab-clawhub .sk-add-source-failures')?.textContent)
      .toBe('1')

    document.querySelector<HTMLButtonElement>('#skills-add-tab-clawhub')?.click()
    await nextTick()
    const clawActivity = document.querySelector<HTMLElement>('.sk-add-queue[data-source="clawhub"]')!
    expect(clawActivity.textContent).toContain('Claw failure')
    expect(clawActivity.textContent).not.toContain('GitHub success')
    expect(clawActivity.querySelector<HTMLElement>('.sk-add-activity-body')?.style.display)
      .not.toBe('none')
  })

  it('shows background progress on its source tab and keeps read-only search available', async () => {
    const activities: SkillInstallActivities = {
      clawhub: { items: [], refreshWarning: '' },
      github: {
        items: [{
          id: '["github","acme/running"]',
          identifier: 'acme/running',
          source: 'github',
          displayName: 'running-skill',
          status: 'installing',
        }],
        refreshWarning: '',
      },
    }
    mountDrawer({ activities, runningSource: 'github' })
    document.querySelector<HTMLButtonElement>('#drawer-trigger')?.click()
    await nextTick()
    const runningActivity = document.querySelector<HTMLElement>('.sk-add-queue')!
    expect(runningActivity.querySelector<HTMLElement>('.sk-add-activity-body')?.style.display)
      .not.toBe('none')
    expect(runningActivity.querySelector<HTMLButtonElement>('.sk-add-activity-toggle')?.disabled)
      .toBe(true)
    document.querySelector<HTMLButtonElement>('#skills-add-tab-clawhub')?.click()
    await nextTick()

    const githubTab = document.querySelector<HTMLElement>('#skills-add-tab-github')!
    expect(githubTab.querySelector('.sk-spinner')).not.toBeNull()
    expect(githubTab.textContent).toContain('Installing running-skill')
    expect(document.querySelector('.sk-add-queue')).toBeNull()
    expect(document.querySelector<HTMLInputElement>('#skills-add-clawhub-query')?.disabled)
      .toBe(false)
    expect(document.querySelector<HTMLButtonElement>('.sk-add-search-row button')?.disabled)
      .toBe(false)
    expect(document.querySelector<HTMLButtonElement>('.sk-add-result button')).toBeNull()
  })

  it('summarizes terminal outcomes, allows manual disclosure, and clears only terminal activity', async () => {
    const activities: SkillInstallActivities = {
      clawhub: { items: [], refreshWarning: '' },
      github: {
        items: [{
          id: '["github","acme/installed"]',
          identifier: 'acme/installed',
          source: 'github',
          displayName: 'installed',
          status: 'installed',
        }, {
          id: '["github","acme/current"]',
          identifier: 'acme/current',
          source: 'github',
          displayName: 'current',
          status: 'unchanged',
        }],
        refreshWarning: '',
      },
    }
    const mounted = mountDrawer({ activities })
    document.querySelector<HTMLButtonElement>('#drawer-trigger')?.click()
    await nextTick()

    const activity = document.querySelector<HTMLElement>('.sk-add-queue')!
    expect(activity.textContent).toContain('2 / 2 processed')
    expect(activity.textContent).toContain('1 installed')
    expect(activity.textContent).toContain('1 already current')
    expect(activity.textContent).toContain('0 failed')
    expect(activity.querySelector<HTMLElement>('.sk-add-activity-body')?.style.display)
      .toBe('none')

    activity.querySelector<HTMLButtonElement>('.sk-add-activity-toggle')?.click()
    await nextTick()
    expect(activity.querySelector<HTMLElement>('.sk-add-activity-body')?.style.display)
      .not.toBe('none')

    const clear = Array.from(activity.querySelectorAll<HTMLButtonElement>('button'))
      .find(button => button.textContent?.includes('Clear activity'))!
    clear.click()
    await nextTick()
    expect(mounted.cleared).toEqual(['github'])
    expect(document.querySelector('.sk-add-queue')).toBeNull()
  })

  it('renders failed operation truth without misleading lifecycle or publication metadata', async () => {
    const missingLifecycle = {
      install_state: 'missing' as const,
      load_state: 'not_discovered' as const,
      selection_state: 'active' as const,
      compatibility_state: 'native' as const,
      readiness_state: 'unknown' as const,
    }
    const activities: SkillInstallActivities = {
      clawhub: {
        items: [{
          id: '["clawhub","@acme/new"]',
          identifier: '@acme/new',
          source: 'clawhub',
          displayName: 'new-skill',
          status: 'failed',
          error: 'Security scan blocked installation',
          result: {
            success: false,
            installed: false,
            effectiveFrom: 'next_turn',
            catalogGeneration: 0,
            lifecycle: missingLifecycle,
            resolution: {
              publisher: 'acme',
              version: '1.1.0',
              immutableRevision: '1.1.0',
            },
          },
        }],
        refreshWarning: '',
      },
      github: { items: [], refreshWarning: '' },
    }
    mountDrawer({ activities })
    document.querySelector<HTMLButtonElement>('#drawer-trigger')?.click()
    await nextTick()
    document.querySelector<HTMLButtonElement>('#skills-add-tab-clawhub')?.click()
    await nextTick()

    const activity = document.querySelector<HTMLElement>('.sk-add-queue')!
    expect(activity.textContent).toContain('Not installed')
    expect(activity.textContent).not.toContain('Installed files missing')
    expect(activity.textContent).not.toContain('Available next turn')
    expect(activity.textContent).not.toContain('Catalog generation')
    expect(Array.from(activity.querySelectorAll('.sk-add-queue-item__meta span'))
      .filter(node => node.textContent === '1.1.0')).toHaveLength(1)
  })

  it('reports a preserved installation when a reinstall fails', async () => {
    const activities: SkillInstallActivities = {
      clawhub: {
        items: [{
          id: '["clawhub","@acme/existing"]',
          identifier: '@acme/existing',
          source: 'clawhub',
          displayName: 'existing-skill',
          status: 'failed',
          error: 'Update rejected',
          result: {
            success: false,
            installed: true,
            lifecycle: {
              install_state: 'tracked',
              load_state: 'loaded',
              selection_state: 'active',
              compatibility_state: 'instruction_only',
              readiness_state: 'ready',
            },
          },
        }],
        refreshWarning: '',
      },
      github: { items: [], refreshWarning: '' },
    }
    mountDrawer({ activities })
    document.querySelector<HTMLButtonElement>('#drawer-trigger')?.click()
    await nextTick()
    document.querySelector<HTMLButtonElement>('#skills-add-tab-clawhub')?.click()
    await nextTick()

    expect(document.querySelector('.sk-add-queue')?.textContent)
      .toContain('Existing installation preserved')
    expect(document.querySelector('.sk-add-queue')?.textContent).toContain('Active')
  })
})
