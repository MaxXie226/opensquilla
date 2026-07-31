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
      return {
        builtinDenyWritePaths: ['C:\\Users\\tester\\.ssh'],
        runtimeTarget: 'windows-x64',
        runtimeVersions: {
          python: { version: '3.13.14', available: true },
          node: { version: '24.18.1', available: true },
          gitBash: { version: '2.55.0', available: true },
        },
      }
    }
    if (method === 'sandbox.tokens.list') return { tokens: [] }
    if (method === 'sandbox.run_mode.preference.get') {
      return { runMode: 'safe', source: 'preference' }
    }
    if (method === 'config.get') {
      return {
        host: '127.0.0.1',
        auth: { allowed_client_cidrs: [] },
      }
    }
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
    if (method === 'sandbox.run_mode.preference.set') {
      return { runMode: params?.runMode, source: 'preference' }
    }
    if (method === 'config.patch') return { restartRequired: true }
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

    const save = [...el.querySelectorAll<HTMLButtonElement>(
      '[data-testid="save-sandbox-section"]',
    )].find(button => !button.disabled)!
    expect(save).toBeTruthy()
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

  it('clamps the recursive-delete backup quota to the visible 0.1 GiB minimum', async () => {
    const { el, call } = await mountPanel()
    const input = el.querySelector<HTMLInputElement>('[data-testid="sandbox-backup-quota"]')!
    input.value = '0'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await settle()

    const save = [...el.querySelectorAll<HTMLButtonElement>(
      '[data-testid="save-sandbox-section"]',
    )].find(button => !button.disabled)!
    save.click()
    await settle()

    expect(call).toHaveBeenCalledWith('sandbox.policy.update', expect.objectContaining({
      policy: expect.objectContaining({
        files: expect.objectContaining({
          backupQuotaBytes: Math.ceil(0.1 * 1024 ** 3),
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

  it('saves LAN binding and CIDRs through the restart-aware config boundary', async () => {
    const { el, call } = await mountPanel()
    el.querySelector<HTMLInputElement>('[data-testid="sandbox-listen-lan"]')!.click()
    const cidr = el.querySelector<HTMLInputElement>('input[placeholder="192.168.1.0/24"]')!
    cidr.value = '192.168.50.0/24'
    cidr.dispatchEvent(new Event('input', { bubbles: true }))
    cidr.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    await settle()
    const saves = [...el.querySelectorAll<HTMLButtonElement>('[data-testid="save-sandbox-section"]')]
    saves.find(button => !button.disabled)!.click()
    await settle()

    expect(call).toHaveBeenCalledWith('config.patch', {
      patch: {
        host: '0.0.0.0',
        auth: { allowed_client_cidrs: ['192.168.50.0/24'] },
      },
    })
  })
})
