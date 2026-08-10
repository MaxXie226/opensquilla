import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  githubSkillDisplayName,
  skillRegistryOperationKey,
  useSkillRegistry,
} from './useSkillRegistry'
import { createSkillMutationGate } from './useSkillMutationGate'
import { useSkillProposals } from './useSkillProposals'

const pushToast = vi.hoisted(() => vi.fn())

vi.mock('@/composables/useToasts', () => ({
  useToasts: () => ({ pushToast }),
}))

afterEach(() => {
  vi.restoreAllMocks()
  pushToast.mockClear()
})

describe('useSkillRegistry install state', () => {
  it('marks the matching community result installed after a successful install', async () => {
    const call = vi.fn(async (method: string) => {
      if (method === 'skills.install') {
        return {
          success: true,
          name: 'Development Coding Agent',
          message: 'installed',
          installed: true,
          instruction_usable: false,
          lifecycle: {
            install_state: 'tracked',
            load_state: 'loaded',
            selection_state: 'shadowed',
            compatibility_state: 'degraded',
            readiness_state: 'ready',
          },
          diagnostics: [{
            code: 'TOOL_PREAPPROVAL_IGNORED',
            severity: 'warning',
            phase: 'compatibility',
            blocking: false,
            message: 'Scoped tool pre-approval is not applied.',
          }],
        }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const loadData = vi.fn(async () => true)
    const registry = useSkillRegistry({ call } as never, loadData)

    registry.registryResults.value = [
      {
        name: 'Development Coding Agent',
        description: 'Enhanced coding agent',
        identifier: 'development-coding-agent',
        installReference: '@alice/development-coding-agent',
        source: 'clawhub',
        installed: false,
      },
      {
        name: 'Development Coding Agent',
        identifier: 'development-coding-agent',
        installReference: '@bob/development-coding-agent',
        source: 'clawhub',
        installed: false,
      },
    ]

    await registry.installSkill('@alice/development-coding-agent', 'clawhub')

    expect(call).toHaveBeenCalledWith('skills.install', {
      identifier: '@alice/development-coding-agent',
      source: 'clawhub',
    })
    expect(loadData).toHaveBeenCalledOnce()
    expect(registry.registryResults.value.map(result => result.installed)).toEqual([true, false])
    expect(registry.registryResults.value[0].instruction_usable).toBe(false)
    expect(registry.registryResults.value[0].lifecycle?.selection_state).toBe('shadowed')
    expect(registry.registryResults.value[0].diagnostics?.[0].code)
      .toBe('TOOL_PREAPPROVAL_IGNORED')
    expect(registry.registryResults.value[1].lifecycle).toBeUndefined()
    expect(registry.installingId.value).toBeNull()
  })

  it('honors snake_case install references before non-unique registry identifiers', async () => {
    const call = vi.fn(async () => ({ success: true, installed: true }))
    const registry = useSkillRegistry({ call } as never, vi.fn(async () => true))
    registry.registryResults.value = [
      {
        name: 'Demo',
        identifier: 'shared-demo',
        install_reference: '@alice/shared-demo',
        source: 'clawhub',
        installed: false,
      },
      {
        name: 'Demo',
        identifier: 'shared-demo',
        install_reference: '@bob/shared-demo',
        source: 'clawhub',
        installed: false,
      },
    ]

    await registry.installSkill('@alice/shared-demo', 'clawhub')

    expect(registry.registryResults.value.map(result => result.installed)).toEqual([true, false])
  })

  it('keeps in-flight identity distinct across community sources', () => {
    expect(skillRegistryOperationKey('demo', 'clawhub'))
      .not.toBe(skillRegistryOperationKey('demo', 'github'))
  })

  it('derives concise GitHub queue labels without changing exact identifiers', async () => {
    const references = [
      'https://github.com/obra/superpowers/tree/main/brainstorming',
      'https://github.com/shadcn-ui/ui/blob/main/skills/shadcn/SKILL.md',
      'shadcn-ui/ui@6261bd89f72d794aea491482cc2acfd8dc3d63e2:skills/shadcn/SKILL.md',
      'owner/repository@0123456789abcdef:skill',
    ]
    const call = vi.fn(async (_method: string, _params: { identifier: string }) => ({
      success: true,
      installed: true,
    }))
    const registry = useSkillRegistry({ call } as never, vi.fn(async () => true))
    registry.githubUrl.value = references.join('\n')

    await registry.installGithub()

    expect(references.map(githubSkillDisplayName)).toEqual([
      'brainstorming',
      'shadcn',
      'shadcn',
      'repository',
    ])
    expect(registry.installQueue.value.map(item => item.displayName)).toEqual([
      'brainstorming',
      'shadcn',
      'shadcn',
      'repository',
    ])
    expect(call.mock.calls.map(([, params]) => params.identifier)).toEqual(references)
  })

  it('installs a deduplicated multi-line GitHub batch serially and refreshes once', async () => {
    let activeCalls = 0
    let maxActiveCalls = 0
    const identifiers: string[] = []
    const call = vi.fn(async (method: string, params: { identifier?: string }) => {
      if (method !== 'skills.install') throw new Error(`Unexpected RPC method: ${method}`)
      activeCalls += 1
      maxActiveCalls = Math.max(maxActiveCalls, activeCalls)
      identifiers.push(String(params.identifier))
      await Promise.resolve()
      activeCalls -= 1
      if (params.identifier === 'https://github.com/acme/skill-3') {
        throw new Error('fixture fetch failed')
      }
      return {
        success: true,
        name: String(params.identifier).split('/').slice(-1)[0],
        installed: true,
      }
    })
    const loadData = vi.fn(async () => true)
    const registry = useSkillRegistry({ call } as never, loadData)
    const lines = Array.from({ length: 15 }, (_, index) =>
      `https://github.com/acme/skill-${index + 1}`)
    registry.githubUrl.value = [lines[0], ...lines, lines[4]].join('\n')

    await registry.installGithub()

    expect(identifiers).toEqual(lines)
    expect(maxActiveCalls).toBe(1)
    expect(loadData).toHaveBeenCalledOnce()
    expect(registry.installQueue.value).toHaveLength(15)
    expect(registry.installQueue.value[2].status).toBe('failed')
    expect(registry.installQueue.value[3].status).toBe('installed')
    expect(registry.githubUrl.value).toBe('https://github.com/acme/skill-3')
    expect(registry.queueRunning.value).toBe(false)
  })

  it('ignores a rapid second submit while the first immutable install is in flight', async () => {
    let release: (() => void) | undefined
    const pending = new Promise<void>((resolve) => { release = resolve })
    const call = vi.fn(async () => {
      await pending
      return { success: true, installed: true }
    })
    const registry = useSkillRegistry({ call } as never, vi.fn(async () => true))
    registry.githubUrl.value = 'https://github.com/acme/demo'

    const first = registry.installGithub()
    const second = registry.installGithub()
    expect(registry.queueRunning.value).toBe(true)
    release?.()
    await Promise.all([first, second])

    expect(call).toHaveBeenCalledOnce()
  })

  it('refuses queue starts while dependency, uninstall, or reload owns the mutation gate', async () => {
    const pending = new Map<string, (value: { success: boolean; installed?: boolean }) => void>()
    const call = vi.fn((method: string) => {
      if (method === 'skills.install') return Promise.resolve({ success: true, installed: true })
      return new Promise<{ success: boolean }>((resolve) => { pending.set(method, resolve) })
    })
    const gate = createSkillMutationGate()
    const registry = useSkillRegistry({ call } as never, vi.fn(async () => true), gate)

    const dependency = registry.installDeps('demo', 'node')
    expect(gate.owner.value).toBe('dependency_install')
    await registry.installSkill('@acme/during-dependency', 'clawhub')
    expect(call.mock.calls.map(([method]) => method)).toEqual(['skills.deps.install'])
    pending.get('skills.deps.install')?.({ success: true })
    await dependency

    const uninstall = registry.uninstallSkill('demo')
    expect(gate.owner.value).toBe('uninstall')
    await registry.installSkill('@acme/during-uninstall', 'clawhub')
    expect(call.mock.calls.map(([method]) => method)).toEqual([
      'skills.deps.install',
      'skills.uninstall',
    ])
    pending.get('skills.uninstall')?.({ success: true })
    await uninstall

    expect(gate.acquire('reload')).toBe(true)
    await registry.installSkill('@acme/during-reload', 'clawhub')
    expect(call.mock.calls.some(([method]) => method === 'skills.install')).toBe(false)
    gate.release('reload')

    await registry.installSkill('@acme/after-release', 'clawhub')
    expect(call.mock.calls[call.mock.calls.length - 1]?.[0]).toBe('skills.install')
  })

  it('keeps dependency and uninstall mutations out while the queue owns the gate', async () => {
    let finishInstall: ((value: { success: boolean; installed: boolean }) => void) | undefined
    const installPending = new Promise<{ success: boolean; installed: boolean }>((resolve) => {
      finishInstall = resolve
    })
    const call = vi.fn(async (method: string) => {
      if (method === 'skills.install') return installPending
      return { success: true }
    })
    const gate = createSkillMutationGate()
    const registry = useSkillRegistry({ call } as never, vi.fn(async () => true), gate)

    const queue = registry.installSkill('@acme/demo', 'clawhub')
    expect(gate.owner.value).toBe('install_queue')
    const dependency = await registry.installDeps('demo', 'node')
    const uninstalled = await registry.uninstallSkill('demo')

    expect(dependency.success).toBe(false)
    expect(uninstalled).toBe(false)
    expect(call.mock.calls.map(([method]) => method)).toEqual(['skills.install'])

    finishInstall?.({ success: true, installed: true })
    await queue
    expect(gate.owner.value).toBeNull()
  })

  it('shares proposal mutation ownership with the install queue', async () => {
    let finishProposal: ((value: { settings: Record<string, unknown> }) => void) | undefined
    const proposalPending = new Promise<{ settings: Record<string, unknown> }>((resolve) => {
      finishProposal = resolve
    })
    const call = vi.fn(async (method: string) => {
      if (method === 'exec.proposals.settings.set') return proposalPending
      if (method === 'skills.install') return { success: true, installed: true }
      throw new Error(`Unexpected method ${method}`)
    })
    const gate = createSkillMutationGate()
    const proposals = useSkillProposals({ call } as never, vi.fn(async () => {}), gate)
    const registry = useSkillRegistry({ call } as never, vi.fn(async () => true), gate)

    const proposal = proposals.toggleAutoPropose('enabled', true)
    expect(gate.owner.value).toBe('proposal')
    await registry.installSkill('@acme/during-proposal', 'clawhub')
    expect(call.mock.calls.map(([method]) => method)).toEqual(['exec.proposals.settings.set'])

    finishProposal?.({ settings: { enabled: true } })
    await proposal

    expect(gate.acquire('install_queue')).toBe(true)
    await proposals.setAutoEnableRisk('low')
    expect(call.mock.calls.map(([method]) => method)).toEqual(['exec.proposals.settings.set'])
    gate.release('install_queue')

    await registry.installSkill('@acme/after-proposal', 'clawhub')
    expect(call.mock.calls[call.mock.calls.length - 1]?.[0]).toBe('skills.install')
  })

  it('keeps terminal results and retries only the selected failed item', async () => {
    let attempts = 0
    const call = vi.fn(async () => {
      attempts += 1
      return attempts === 1
        ? { success: false, message: 'not compatible' }
        : { success: true, unchanged: true, name: 'demo' }
    })
    const loadData = vi.fn(async () => true)
    const registry = useSkillRegistry({ call } as never, loadData)

    await registry.installSkill('@acme/demo', 'clawhub', 'Demo')
    expect(registry.installQueue.value[0].status).toBe('failed')

    await registry.retryQueueItem(registry.installQueue.value[0].id)

    expect(registry.installQueue.value[0].status).toBe('unchanged')
    expect(registry.installQueue.value[0].displayName).toBe('demo')
    expect(loadData).toHaveBeenCalledOnce()
  })

  it('searches ClawHub explicitly and retains source diagnostics', async () => {
    const diagnostic = {
      code: 'SOURCE_RATE_LIMITED',
      severity: 'error',
      phase: 'source',
      blocking: true,
      message: 'Try again later.',
    }
    const call = vi.fn(async () => ({ results: [], diagnostics: [diagnostic] }))
    const registry = useSkillRegistry({ call } as never, vi.fn(async () => true))
    registry.registryQuery.value = 'demo'

    await registry.searchRegistry()

    expect(call).toHaveBeenCalledWith('skills.search', {
      query: 'demo',
      limit: 20,
      source: 'clawhub',
    })
    expect(registry.registryDiagnostics.value).toEqual([diagnostic])
  })

  it('warns when installation succeeds but the catalog list cannot refresh', async () => {
    const call = vi.fn(async () => ({
      success: true,
      name: 'Development Coding Agent',
      message: 'installed',
    }))
    const loadData = vi.fn(async () => false)
    const registry = useSkillRegistry({ call } as never, loadData)

    await registry.installSkill('development-coding-agent', 'clawhub')

    expect(pushToast).toHaveBeenCalledWith(expect.any(String), { tone: 'warn' })
  })

  it('treats an unresolved envAny group as an incomplete dependency install', async () => {
    const call = vi.fn(async () => ({
      success: true,
      message: 'binary installed',
      missing_still: {
        bins: [],
        env: [],
        env_any: [['OPENROUTER_API_KEY', 'ARK_API_KEY']],
      },
    }))
    const registry = useSkillRegistry({ call } as never, vi.fn(async () => true))

    const outcome = await registry.installDeps('audio-cog', 'ffmpeg')

    expect(outcome.success).toBe(true)
    expect(outcome.complete).toBe(false)
    expect(outcome.missingStill.env_any).toEqual([
      ['OPENROUTER_API_KEY', 'ARK_API_KEY'],
    ])
  })
})
