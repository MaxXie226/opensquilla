// @vitest-environment happy-dom

import { createApp, h, nextTick } from 'vue'
import { createI18n } from 'vue-i18n'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { RegistryResult } from '@/types/skills'
import { skillRegistryOperationKey } from '@/composables/skills/useSkillRegistry'
import SkillsRegistryPanel from './SkillsRegistryPanel.vue'

const apps: ReturnType<typeof createApp>[] = []

afterEach(() => {
  while (apps.length) apps.pop()?.unmount()
  document.body.innerHTML = ''
})

describe('SkillsRegistryPanel install provenance', () => {
  it('renders every community installation lifecycle label', async () => {
    const host = document.createElement('div')
    document.body.appendChild(host)
    const results = [
      {
        name: 'active-skill',
        installed: true,
        lifecycle: {
          install_state: 'tracked',
          load_state: 'loaded',
          selection_state: 'active',
          compatibility_state: 'instruction_only',
          readiness_state: 'ready',
        },
      },
      {
        name: 'needs-setup-skill',
        installed: true,
        lifecycle: {
          install_state: 'tracked',
          load_state: 'loaded',
          selection_state: 'active',
          compatibility_state: 'instruction_only',
          readiness_state: 'needs_setup',
        },
      },
      {
        name: 'degraded-skill',
        installed: true,
        instruction_usable: true,
        diagnostics: [{
          code: 'TOOL_PREAPPROVAL_IGNORED',
          severity: 'warning',
          phase: 'compatibility',
          blocking: false,
          message: 'Scoped tool pre-approval is not applied.',
        }],
        lifecycle: {
          install_state: 'tracked',
          load_state: 'loaded',
          selection_state: 'active',
          compatibility_state: 'degraded',
          readiness_state: 'ready',
        },
      },
      {
        name: 'shadowed-skill',
        installed: true,
        lifecycle: {
          install_state: 'tracked',
          load_state: 'loaded',
          selection_state: 'shadowed',
          compatibility_state: 'instruction_only',
          readiness_state: 'ready',
        },
      },
      {
        name: 'disabled-skill',
        installed: true,
        lifecycle: {
          install_state: 'tracked',
          load_state: 'loaded',
          selection_state: 'disabled',
          compatibility_state: 'instruction_only',
          readiness_state: 'ready',
        },
      },
      {
        name: 'hidden-skill',
        installed: true,
        lifecycle: {
          install_state: 'tracked',
          load_state: 'loaded',
          selection_state: 'hidden',
          compatibility_state: 'instruction_only',
          readiness_state: 'ready',
        },
      },
      {
        name: 'offline-skill',
        installed: true,
        lifecycle: {
          install_state: 'tracked',
          load_state: 'validated_offline',
          selection_state: 'active',
          compatibility_state: 'instruction_only',
          readiness_state: 'ready',
        },
      },
      {
        name: 'rejected-skill',
        installed: true,
        lifecycle: {
          install_state: 'tracked',
          load_state: 'rejected',
          selection_state: 'shadowed',
          compatibility_state: 'instruction_only',
          readiness_state: 'unknown',
        },
      },
      {
        name: 'unsupported-skill',
        installed: true,
        lifecycle: {
          install_state: 'tracked',
          load_state: 'not_discovered',
          selection_state: 'shadowed',
          compatibility_state: 'unsupported',
          readiness_state: 'unknown',
        },
      },
      {
        name: 'restored-skill',
        installed: true,
        lifecycle: {
          install_state: 'tracked',
          load_state: 'serving_previous',
          selection_state: 'active',
          compatibility_state: 'instruction_only',
          readiness_state: 'ready',
        },
      },
      {
        name: 'missing-skill',
        installed: false,
        lifecycle: {
          install_state: 'missing',
          load_state: 'not_discovered',
          selection_state: 'shadowed',
          compatibility_state: 'instruction_only',
          readiness_state: 'unknown',
        },
      },
      {
        name: 'drifted-skill',
        installed: true,
        lifecycle: {
          install_state: 'drifted',
          load_state: 'loaded',
          selection_state: 'active',
          compatibility_state: 'instruction_only',
          readiness_state: 'ready',
        },
      },
    ] satisfies RegistryResult[]
    const app = createApp({
      setup: () => () => h(SkillsRegistryPanel, {
        registryQuery: '',
        githubUrl: '',
        results,
        loading: false,
        installingId: null,
      }),
    })
    app.use(createI18n({
      legacy: false,
      locale: 'en',
      missingWarn: false,
      fallbackWarn: false,
      messages: {
        en: {
          cronSkills: {
            registry: {
              stateActive: 'Active',
              stateDegraded: 'Limited compatibility',
              stateNeedsSetup: 'Setup required',
              stateShadowed: 'Shadowed',
              stateDisabled: 'Disabled',
              stateHidden: 'Hidden from model catalog',
              stateNextStart: 'Validated for next start',
              stateRejected: 'Rejected as incompatible',
              stateRestored: 'Previous version restored',
              stateMissing: 'Installed files missing',
              stateDrifted: 'Local changes detected',
            },
            tile: { installed: 'Installed' },
          },
        },
      },
    }))
    app.mount(host)
    apps.push(app)
    await nextTick()

    const expected = new Map<string, [string, string]>([
      ['active-skill', ['Active', 'success']],
      ['degraded-skill', ['Limited compatibility', 'warning']],
      ['needs-setup-skill', ['Setup required', 'warning']],
      ['shadowed-skill', ['Shadowed', 'neutral']],
      ['disabled-skill', ['Disabled', 'neutral']],
      ['hidden-skill', ['Hidden from model catalog', 'neutral']],
      ['offline-skill', ['Validated for next start', 'info']],
      ['rejected-skill', ['Rejected as incompatible', 'danger']],
      ['unsupported-skill', ['Rejected as incompatible', 'danger']],
      ['restored-skill', ['Previous version restored', 'warning']],
      ['missing-skill', ['Installed files missing', 'danger']],
      ['drifted-skill', ['Local changes detected', 'warning']],
    ])
    const tiles = [...host.querySelectorAll<HTMLElement>('.sk-tile')]
    for (const [name, [label, tone]] of expected) {
      const tile = tiles.find(candidate => candidate.title.startsWith(name))
      expect(tile?.textContent, name).toContain(label)
      expect(tile?.querySelector('.sk-tile__lifecycle')?.getAttribute('data-tone'), name)
        .toBe(tone)
    }
  })

  it('shows the source and trust level before installation', async () => {
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp({
      setup: () => () => h(SkillsRegistryPanel, {
        registryQuery: 'calendar',
        githubUrl: '',
        results: [{
          name: 'community-calendar',
          description: 'Community calendar integration',
          source: 'github',
          trust_level: 'community',
          identifier: 'example/community-calendar',
          installed: false,
        }],
        loading: false,
        installingId: null,
      }),
    })
    app.use(createI18n({
      legacy: false,
      locale: 'en',
      missingWarn: false,
      fallbackWarn: false,
      messages: { en: {} },
    }))
    app.mount(host)
    apps.push(app)
    await nextTick()

    expect(host.textContent).toContain('github')
    expect(host.textContent).toContain('community')
    expect(host.querySelector('.sk-tile__lifecycle')).toBeNull()
  })

  it('renders a shadowed installation lifecycle state', async () => {
    const host = document.createElement('div')
    document.body.appendChild(host)
    const onInstall = vi.fn()
    const app = createApp({
      setup: () => () => h(SkillsRegistryPanel, {
        registryQuery: 'calendar',
        githubUrl: '',
        results: [{
          name: 'community-calendar',
          source: 'github',
          identifier: 'example/community-calendar',
          installReference: 'example/community-calendar@0123456789abcdef:SKILL.md',
          installed: true,
          lifecycle: {
            install_state: 'tracked',
            load_state: 'loaded',
            selection_state: 'shadowed',
            compatibility_state: 'instruction_only',
            readiness_state: 'ready',
          },
        }],
        loading: false,
        installingId: null,
        onInstall,
      }),
    })
    app.use(createI18n({
      legacy: false,
      locale: 'en',
      missingWarn: false,
      fallbackWarn: false,
      messages: {
        en: {
          cronSkills: {
            registry: { stateShadowed: 'Shadowed' },
            tile: { installed: 'Installed' },
          },
        },
      },
    }))
    app.mount(host)
    apps.push(app)
    await nextTick()

    expect(host.textContent).toContain('Shadowed')
    expect(host.textContent).toContain('Installed')
    // Already-installed results do not offer a second mutation button.
    expect(onInstall).not.toHaveBeenCalled()
  })

  it('renders rejection ahead of a shadowed selection state', async () => {
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp({
      setup: () => () => h(SkillsRegistryPanel, {
        registryQuery: 'calendar',
        githubUrl: '',
        results: [{
          name: 'community-calendar',
          source: 'github',
          identifier: 'example/community-calendar',
          installed: true,
          lifecycle: {
            install_state: 'tracked',
            load_state: 'rejected',
            selection_state: 'shadowed',
            compatibility_state: 'instruction_only',
            readiness_state: 'unknown',
          },
        }],
        loading: false,
        installingId: null,
      }),
    })
    app.use(createI18n({
      legacy: false,
      locale: 'en',
      missingWarn: false,
      fallbackWarn: false,
      messages: {
        en: {
          cronSkills: {
            registry: {
              stateRejected: 'Rejected as incompatible',
              stateShadowed: 'Shadowed',
            },
            tile: { installed: 'Installed' },
          },
        },
      },
    }))
    app.mount(host)
    apps.push(app)
    await nextTick()

    expect(host.textContent).toContain('Rejected as incompatible')
    expect(host.textContent).not.toContain('Shadowed')
  })

  it('shows missing installed files ahead of a shadowed selection state', async () => {
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp({
      setup: () => () => h(SkillsRegistryPanel, {
        registryQuery: 'calendar',
        githubUrl: '',
        results: [{
          name: 'community-calendar',
          source: 'github',
          identifier: 'example/community-calendar',
          installed: false,
          lifecycle: {
            install_state: 'missing',
            load_state: 'not_discovered',
            selection_state: 'shadowed',
            compatibility_state: 'instruction_only',
            readiness_state: 'unknown',
          },
        }],
        loading: false,
        installingId: null,
      }),
    })
    app.use(createI18n({
      legacy: false,
      locale: 'en',
      missingWarn: false,
      fallbackWarn: false,
      messages: {
        en: {
          cronSkills: {
            registry: {
              stateMissing: 'Installed files missing',
              stateShadowed: 'Shadowed',
            },
          },
        },
      },
    }))
    app.mount(host)
    apps.push(app)
    await nextTick()

    expect(host.textContent).toContain('Installed files missing')
    expect(host.textContent).not.toContain('Shadowed')
    expect(host.querySelector('.sk-tile__lifecycle')?.getAttribute('data-tone')).toBe('danger')
    expect(host.querySelector('.sk-tile__add')).not.toBeNull()
  })

  it('emits the immutable install reference selected by search', async () => {
    const host = document.createElement('div')
    document.body.appendChild(host)
    const onInstall = vi.fn()
    const app = createApp({
      setup: () => () => h(SkillsRegistryPanel, {
        registryQuery: 'calendar',
        githubUrl: '',
        results: [{
          name: 'community-calendar',
          source: 'github',
          identifier: 'example/community-calendar',
          installReference: 'example/community-calendar@0123456789abcdef:SKILL.md',
          installed: false,
        }],
        loading: false,
        installingId: null,
        onInstall,
      }),
    })
    app.use(createI18n({
      legacy: false,
      locale: 'en',
      missingWarn: false,
      fallbackWarn: false,
      messages: { en: {} },
    }))
    app.mount(host)
    apps.push(app)
    await nextTick()

    ;(host.querySelector('.sk-tile__add') as HTMLButtonElement).click()
    expect(onInstall).toHaveBeenCalledWith(
      'example/community-calendar@0123456789abcdef:SKILL.md',
      'github',
    )
  })

  it('tracks busy state by source and exact install reference', async () => {
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp({
      setup: () => () => h(SkillsRegistryPanel, {
        registryQuery: 'demo',
        githubUrl: '',
        results: [
          { name: 'demo', source: 'clawhub', installReference: 'demo' },
          { name: 'demo', source: 'github', installReference: 'demo' },
        ],
        loading: false,
        installingId: skillRegistryOperationKey('demo', 'github'),
      }),
    })
    app.use(createI18n({
      legacy: false,
      locale: 'en',
      missingWarn: false,
      fallbackWarn: false,
      messages: { en: {} },
    }))
    app.mount(host)
    apps.push(app)
    await nextTick()

    const buttons = [...host.querySelectorAll<HTMLButtonElement>('.sk-tile__add')]
    expect(buttons).toHaveLength(2)
    expect(buttons[0].disabled).toBe(false)
    expect(buttons[1].disabled).toBe(true)
    expect(host.querySelectorAll('.sk-tile__spinner')).toHaveLength(1)
  })
})
