// @vitest-environment happy-dom

import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useRpcStore } from './rpc'

const connectCalls: Array<{ url: string; token?: string }> = []
const clients: Array<{
  emit: (event: string, ...args: unknown[]) => void
  disconnect: ReturnType<typeof vi.fn>
}> = []

function memoryStorage(): Storage {
  const values = new Map<string, string>()
  return {
    get length() { return values.size },
    clear: () => values.clear(),
    getItem: key => values.get(key) ?? null,
    key: index => [...values.keys()][index] ?? null,
    removeItem: key => { values.delete(key) },
    setItem: (key, value) => { values.set(key, value) },
  }
}

vi.mock('@/lib/rpc', () => ({
  capabilitiesForMethods: (methods: string[]) => {
    if (methods.length === 0) return []
    const available = new Set(methods)
    const capabilities = ['gateway.rpc']
    if (
      ['chat.history', 'chat.send', 'sessions.list', 'sessions.resolve']
        .every(method => available.has(method))
    ) capabilities.push('gateway.sessions')
    return capabilities
  },
  RpcClient: class {
    state = 'disconnected'
    private listeners = new Map<string, Array<(...args: unknown[]) => void>>()

    constructor() {
      clients.push(this)
    }

    connect(url: string, token?: string) {
      connectCalls.push({ url, token })
      this.state = 'connected'
      this.emit('_state', 'connected')
    }

    emit(event: string, ...args: unknown[]) {
      for (const handler of this.listeners.get(event) || []) handler(...args)
    }

    on(event: string, handler: (...args: unknown[]) => void) {
      const handlers = this.listeners.get(event) || []
      handlers.push(handler)
      this.listeners.set(event, handlers)
      return () => {
        this.listeners.set(event, (this.listeners.get(event) || []).filter(h => h !== handler))
      }
    }

    disconnect = vi.fn(() => {
      this.state = 'disconnected'
      this.emit('_state', 'disconnected')
    })
    waitForConnection = vi.fn()
    call = vi.fn()
  },
}))

describe('rpc link-token bootstrap', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    connectCalls.length = 0
    clients.length = 0
    vi.stubGlobal('localStorage', memoryStorage())
    vi.stubGlobal('sessionStorage', memoryStorage())
    localStorage.clear()
    sessionStorage.clear()
    window.history.replaceState(null, '', '/control/sessions')
  })

  it('uses a URL token over stale browser storage before initial connect', () => {
    localStorage.setItem('opensquilla.wsUrl', 'ws://old.example/ws')
    localStorage.setItem('opensquilla.chat.draft:agent:main:webchat:old', 'stale draft')
    localStorage.setItem('opensquilla.chat.runMode', 'full')
    localStorage.setItem('opensquilla.logs.runTrace', '1')
    localStorage.setItem('opensquilla.shortcuts', '{"new-chat":{"enabled":true}}')
    localStorage.setItem('unrelated.preference', 'keep')
    sessionStorage.setItem('opensquilla.wsToken', 'old-token')
    sessionStorage.setItem('opensquilla.cachedAuth', 'stale-auth')
    window.history.replaceState(null, '', '/control/?token=new-token')

    const store = useRpcStore()
    store.init()

    expect(connectCalls).toEqual([{ url: 'ws://localhost:3000/ws', token: 'new-token' }])
    expect(localStorage.getItem('opensquilla.wsUrl')).toBe('ws://localhost:3000/ws')
    expect(localStorage.getItem('opensquilla.chat.draft:agent:main:webchat:old')).toBeNull()
    expect(localStorage.getItem('opensquilla.chat.runMode')).toBe('full')
    expect(localStorage.getItem('opensquilla.logs.runTrace')).toBe('1')
    expect(localStorage.getItem('opensquilla.shortcuts')).toBe('{"new-chat":{"enabled":true}}')
    expect(localStorage.getItem('unrelated.preference')).toBe('keep')
    expect(sessionStorage.getItem('opensquilla.wsToken')).toBe('new-token')
    expect(sessionStorage.getItem('opensquilla.cachedAuth')).toBeNull()
    expect(window.location.href).toBe('http://localhost:3000/control/')
  })

  it('reconnects with a URL token when an already-loaded app navigates to a token link', () => {
    localStorage.setItem('opensquilla.wsUrl', 'ws://localhost:3000/ws')
    localStorage.setItem('opensquilla.chat.draft:agent:main:webchat:old', 'stale draft')
    sessionStorage.setItem('opensquilla.wsToken', 'old-token')
    sessionStorage.setItem('opensquilla.cachedAuth', 'stale-auth')

    const store = useRpcStore()
    store.init()
    expect(connectCalls).toEqual([{ url: 'ws://localhost:3000/ws', token: 'old-token' }])

    window.history.replaceState(null, '', '/control/sessions?token=new-token')
    expect(store.applyLinkTokenFromUrl()).toBe(true)

    expect(connectCalls).toEqual([
      { url: 'ws://localhost:3000/ws', token: 'old-token' },
      { url: 'ws://localhost:3000/ws', token: 'new-token' },
    ])
    expect(localStorage.getItem('opensquilla.chat.draft:agent:main:webchat:old')).toBeNull()
    expect(sessionStorage.getItem('opensquilla.wsToken')).toBe('new-token')
    expect(sessionStorage.getItem('opensquilla.cachedAuth')).toBeNull()
    expect(window.location.href).toBe('http://localhost:3000/control/sessions')
  })

  it('clears stale identity state before reconnecting with a URL token', () => {
    const store = useRpcStore()
    store.init()
    clients[0].emit('_hello', {
      policy: { allowedRunModes: ['full'] },
      auth: { principal: { isOwner: true } },
      features: { methods: ['usage.status', 'usage.query'] },
    })
    expect(store.policy).toEqual({ allowedRunModes: ['full'] })
    expect(store.auth).toEqual({ principal: { isOwner: true } })
    expect(store.supportsMethod('usage.query')).toBe(true)

    store.markMethodUnavailable('usage.query')
    expect(store.supportsMethod('usage.query')).toBe(false)

    window.history.replaceState(null, '', '/control/?token=new-token')

    expect(store.applyLinkTokenFromUrl()).toBe(true)
    expect(store.policy).toBeNull()
    expect(store.auth).toBeNull()
    expect(store.methods).toEqual([])
    expect(store.capabilities).toEqual([])
    expect(store.contractStatus).toBe('legacy-contract')
    expect(connectCalls[connectCalls.length - 1]).toEqual({
      url: 'ws://localhost:3000/ws',
      token: 'new-token',
    })
  })

  it('treats missing or malformed Hello methods as unsupported', () => {
    const store = useRpcStore()
    store.init()

    clients[0].emit('_hello', { features: { methods: ['usage.status', 42, null] } })

    expect(store.methods).toEqual(['usage.status'])
    expect(store.supportsMethod('usage.status')).toBe(true)
    expect(store.supportsMethod('usage.query')).toBe(false)

    clients[0].emit('_hello', {})
    expect(store.methods).toEqual([])
  })

  it('stores explicit contract, runtime, range, capabilities and extensions', () => {
    const store = useRpcStore()
    store.init()
    const contract = {
      schemaVersion: 1,
      digest: `sha256:${'a'.repeat(64)}`,
      generatedFrom: 'gateway',
    }

    clients[0].emit('_hello', {
      contract,
      contractStatus: 'advertised',
      runtime: {
        coreVersion: '0.5.0',
        buildCommit: null,
        platform: 'linux',
        arch: 'x86_64',
      },
      protocolRange: { min: 1, max: 3 },
      capabilities: ['gateway.rpc', 'gateway.sessions'],
      capabilitySource: 'hello',
      extensions: ['channel.example'],
      features: { methods: ['sessions.list'] },
    })

    expect(store.contract).toEqual(contract)
    expect(store.contractStatus).toBe('advertised')
    expect(store.runtime?.coreVersion).toBe('0.5.0')
    expect(store.protocolRange).toEqual({ min: 1, max: 3 })
    expect(store.capabilitySource).toBe('hello')
    expect(store.supportsCapability('gateway.sessions')).toBe(true)
    expect(store.extensions).toEqual(['channel.example'])

    store.disconnect()
    expect(store.contract).toBeNull()
    expect(store.contractStatus).toBe('legacy-contract')
    expect(store.runtime).toBeNull()
    expect(store.protocolRange).toBeNull()
    expect(store.capabilities).toEqual([])
    expect(store.extensions).toEqual([])
  })
})
