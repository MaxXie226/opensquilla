// @vitest-environment happy-dom

import { createApp, h, nextTick, ref } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import i18n from '@/i18n'
import { useSkillsCatalog } from '@/composables/skills/useSkillsCatalog'
import type { useRpcStore } from '@/stores/rpc'
import SkillGroup from './SkillGroup.vue'

const apps: ReturnType<typeof createApp>[] = []

afterEach(() => {
  while (apps.length) apps.pop()?.unmount()
  document.body.innerHTML = ''
})

describe('SkillGroup lifecycle compatibility', () => {
  it('loads and renders an old Gateway skills.list response without lifecycle fields', async () => {
    const rpc = {
      waitForConnection: vi.fn(async () => {}),
      call: vi.fn(async () => ({
        skills: [{
          name: 'legacy-community-skill',
          description: 'Loaded from an older Gateway response',
          layer: 'managed',
          status: 'ready',
          eligible: true,
        }],
      })),
    } as unknown as ReturnType<typeof useRpcStore>
    const loadProposals = vi.fn(async () => {})
    const catalog = useSkillsCatalog(rpc, {
      proposals: ref([]),
      autoEnabledSkills: ref([]),
      proposalsSettings: ref({
        available: false,
        enabled: false,
        on_dream_complete: false,
        auto_enable: false,
        auto_enable_max_risk: 'low',
      }),
      loadProposals,
    })

    await expect(catalog.loadData()).resolves.toBe(true)
    expect(rpc.call).toHaveBeenCalledWith('skills.list', { includeLifecycle: true })
    expect(loadProposals).toHaveBeenCalledOnce()
    expect(catalog.allSkills.value).toHaveLength(1)
    expect(catalog.allSkills.value[0]?.lifecycle).toBeUndefined()

    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp({
      setup: () => () => h(SkillGroup, {
        title: 'Managed',
        description: 'Community skills',
        skills: catalog.allSkills.value,
      }),
    })
    app.use(i18n)
    app.mount(host)
    apps.push(app)
    await nextTick()

    expect(host.textContent).toContain('legacy-community-skill')
    expect(host.textContent).toContain('Loaded from an older Gateway response')
    expect(host.querySelector('.sk-tile__dot.is-ready')).not.toBeNull()
    expect(host.querySelector('.sk-tile__lifecycle')).toBeNull()
  })
})
