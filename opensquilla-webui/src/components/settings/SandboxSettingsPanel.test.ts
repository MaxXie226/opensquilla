// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { App } from 'vue'

const mounted: App[] = []

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
} as const

async function settle() {
  for (let index = 0; index < 8; index++) await Promise.resolve()
}

async function mountPanel() {
  vi.resetModules()
  document.body.innerHTML = ''
  const call = vi.fn(async (method: string, params?: Record<string, unknown>) => {
    if (method === 'sandbox.capability.status') {
      return {
        available: true,
        backend: 'windows_default',
        platform: 'win32',
        code: 'ready',
        reason: 'ready',
        setupSupported: true,
        restartRequired: false,
        probeVersion: 1,
        capabilities: ['process'],
      }
    }
    if (method === 'sandbox.policy.get') return JSON.parse(JSON.stringify(policy))
    if (method === 'sandbox.policy.defaults') {
      return { builtinDenyWritePaths: ['C:\\Users\\tester\\.ssh'] }
    }
    if (method === 'sandbox.tokens.list') return { tokens: [] }
    if (method === 'sandbox.policy.update') {
      const saved = JSON.parse(JSON.stringify(params?.policy))
      saved.policyVersion = Number(params?.basePolicyVersion) + 1
      return saved
    }
    if (method === 'sandbox.tokens.create') {
      return {
        token: 'osq_public_secret-once',
        record: {
          publicId: 'public',
          name: params?.name,
          capabilities: ['host.execute', 'task.read', 'task.submit'],
          createdAt: 1,
          lastUsedAt: null,
          lastPeer: null,
        },
      }
    }
    if (method === 'sandbox.tokens.revoke') return { revoked: true }
    throw new Error(`unexpected method: ${method}`)
  })
  vi.doMock('@/stores/rpc', () => ({
    useRpcStore: () => ({
      waitForConnection: vi.fn(async () => {}),
      call,
    }),
  }))

  const { createApp } = await import('vue')
  const i18n = (await import('@/i18n')).default
  i18n.global.locale.value = 'en'
  const Component = (await import('./SandboxSettingsPanel.vue')).default
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(Component)
  app.use(i18n)
  app.mount(el)
  mounted.push(app)
  await settle()
  return { el, call }
}

afterEach(() => {
  while (mounted.length) mounted.pop()!.unmount()
  vi.doUnmock('@/stores/rpc')
  vi.restoreAllMocks()
  document.body.innerHTML = ''
})

describe('SandboxSettingsPanel', () => {
  it('loads immutable file rules and saves a versioned custom rule', async () => {
    const { el, call } = await mountPanel()
    expect(el.querySelector('[data-testid="builtin-file-rules"]')?.textContent)
      .toContain('C:\\Users\\tester\\.ssh')

    const input = el.querySelector<HTMLInputElement>('input[placeholder="Add a protected path"]')!
    input.value = 'D:\\Secrets'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    await settle()

    const save = el.querySelector<HTMLButtonElement>('[data-testid="save-sandbox-section"]')!
    expect(save.disabled).toBe(false)
    save.click()
    await settle()

    expect(call).toHaveBeenCalledWith('sandbox.policy.update', expect.objectContaining({
      basePolicyVersion: 0,
      policy: expect.objectContaining({
        files: expect.objectContaining({
          customDenyWritePaths: ['D:\\Secrets'],
        }),
      }),
    }))
  })

  it('shows a newly-created named token only in the create response', async () => {
    const { el, call } = await mountPanel()
    const name = el.querySelector<HTMLInputElement>('input[placeholder^="Token name"]')!
    name.value = 'Laptop'
    name.dispatchEvent(new Event('input', { bubbles: true }))
    await settle()
    el.querySelector<HTMLButtonElement>('[data-testid="create-sandbox-token"]')!.click()
    await settle()

    expect(call).toHaveBeenCalledWith('sandbox.tokens.create', {
      name: 'Laptop',
      hostExecute: true,
    })
    expect(el.querySelector('[data-testid="revealed-sandbox-token"]')?.textContent)
      .toContain('osq_public_secret-once')
  })
})
