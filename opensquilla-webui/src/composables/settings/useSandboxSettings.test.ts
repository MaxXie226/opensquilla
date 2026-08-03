import { afterEach, describe, expect, it, vi } from 'vitest'
import type { EffectScope } from 'vue'

const policy = {
  schemaVersion: 2,
  policyVersion: 0,
  files: {
    customDenyWritePaths: [],
    recursiveDeleteBackupEnabled: true,
    backupQuotaBytes: 3 * 1024 ** 3,
  },
  commands: {
    requireApprovalPrefixes: [],
    autoAllowPrefixes: [],
    systemTools: 'prompt',
  },
  network: {
    blockAllNetwork: false,
    allowDomains: [],
    denyDomains: [],
  },
  runtimes: {
    enabled: true,
    python: true,
    node: true,
    gitBash: true,
  },
}

const unavailableReport = {
  available: false,
  backend: 'windows_default',
  platform: 'win32',
  code: 'probe_timeout',
  reason: 'timed out',
  setupSupported: true,
  restartRequired: false,
  probeVersion: 1,
  capabilities: [],
}

async function settle(): Promise<void> {
  for (let index = 0; index < 8; index += 1) await Promise.resolve()
}

async function createSandboxSettings(options: {
  desktop?: boolean
  capabilityError?: boolean
  capabilityResult?: unknown
  setupState?: 'not_setup' | 'ready'
} = {}) {
  vi.resetModules()
  const call = vi.fn(async (method: string, params?: Record<string, unknown>) => {
    if (method === 'sandbox.policy.get') return structuredClone(policy)
    if (method === 'sandbox.policy.defaults') return {}
    if (method === 'sandbox.run_mode.preference.get') return { runMode: 'full' }
    if (method === 'sandbox.capability.status') {
      if (options.capabilityError) throw new Error('probe failed')
      return options.capabilityResult ?? unavailableReport
    }
    if (method === 'sandbox.setup.status') {
      const state = options.setupState ?? 'not_setup'
      return {
        state,
        platform: 'win32',
        message: state === 'ready' ? 'ready' : 'setup required',
        requiresAdmin: state !== 'ready',
      }
    }
    if (method === 'sandbox.setup.ensure') {
      return {
        state: 'ready',
        platform: 'win32',
        message: 'ready',
        requiresAdmin: false,
      }
    }
    throw new Error(`unexpected method: ${method} ${JSON.stringify(params)}`)
  })
  vi.doMock('@/stores/rpc', () => ({
    useRpcStore: () => ({
      waitForConnection: vi.fn(async () => {}),
      call,
    }),
  }))
  vi.doMock('@/platform', () => ({
    usePlatform: () => ({
      capabilities: { isDesktop: options.desktop === true },
      settings: {},
    }),
  }))

  const { effectScope } = await import('vue')
  const { useSandboxSettings } = await import('./useSandboxSettings')
  const scope: EffectScope = effectScope()
  const settings = scope.run(() => useSandboxSettings())!
  return { call, scope, settings }
}

function capabilityCalls(call: ReturnType<typeof vi.fn>) {
  return call.mock.calls.filter(([method]) => method === 'sandbox.capability.status')
}

afterEach(() => {
  vi.doUnmock('@/stores/rpc')
  vi.doUnmock('@/platform')
  vi.restoreAllMocks()
  vi.useRealTimers()
})

describe('useSandboxSettings capability checks', () => {
  it.each([
    ['unavailable report', { capabilityResult: unavailableReport }],
    ['failed report', { capabilityError: true }],
  ])('does not automatically retry a %s after 10, 30, or 60 seconds', async (_label, options) => {
    vi.useFakeTimers()
    const { call, scope, settings } = await createSandboxSettings(options)

    await settings.load()
    await settle()
    expect(capabilityCalls(call)).toHaveLength(1)

    for (const elapsed of [10_000, 20_000, 30_000]) {
      await vi.advanceTimersByTimeAsync(elapsed)
      await settle()
      expect(capabilityCalls(call)).toHaveLength(1)
    }

    scope.stop()
  })

  it('performs exactly one forced check for an explicit retry', async () => {
    vi.useFakeTimers()
    const { call, scope, settings } = await createSandboxSettings()

    await settings.load()
    await settle()
    await settings.loadCapability(true)

    expect(capabilityCalls(call)).toEqual([
      ['sandbox.capability.status', undefined],
      ['sandbox.capability.status', { refresh: true }],
    ])
    await vi.advanceTimersByTimeAsync(60_000)
    await settle()
    expect(capabilityCalls(call)).toHaveLength(2)

    scope.stop()
  })

  it('performs exactly one forced refresh after successful setup', async () => {
    const { call, scope, settings } = await createSandboxSettings({
      desktop: true,
      capabilityResult: { ...unavailableReport, available: true, code: 'ready' },
    })

    await settings.load()
    await settle()
    expect(capabilityCalls(call)).toHaveLength(0)

    await settings.ensureSandboxSetupForSafeMode()

    expect(capabilityCalls(call)).toEqual([
      ['sandbox.capability.status', { refresh: true }],
    ])
    scope.stop()
  })

  it('ignores a stale capability result after its scope closes', async () => {
    vi.useFakeTimers()
    let resolveCapability!: (value: unknown) => void
    const pendingCapability = new Promise<unknown>((resolve) => {
      resolveCapability = resolve
    })
    const { call, scope, settings } = await createSandboxSettings({
      capabilityResult: pendingCapability,
    })

    const loading = settings.loadCapability()
    await settle()
    scope.stop()
    resolveCapability({ ...unavailableReport, available: true, code: 'ready' })
    await loading
    await vi.advanceTimersByTimeAsync(60_000)

    expect(settings.capability.value).toBeNull()
    expect(capabilityCalls(call)).toHaveLength(1)
  })
})
