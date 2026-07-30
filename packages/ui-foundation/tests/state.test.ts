import { describe, expect, it, vi } from 'vitest'

import {
  GatewayDataStore,
  createFoundationStores,
} from '../src/index.js'

describe('GatewayDataStore', () => {
  it('creates isolated state scopes for independent product roots', () => {
    const first = createFoundationStores()
    const second = createFoundationStores()

    first.gateway.setConnectionState('connected')
    first.gateway.applyHello({
      auth: { principal: { isOwner: true } },
      features: {
        methods: [
          'chat.history',
          'chat.send',
          'sessions.list',
          'sessions.resolve',
        ],
      },
    })
    first.gateway.markMethodUnavailable('chat.send')

    expect(first.gateway.snapshot.state).toBe('connected')
    expect(first.gateway.snapshot.capabilities).toEqual([
      'gateway.rpc',
      'gateway.sessions',
    ])
    expect(first.gateway.supportsMethod('chat.send')).toBe(false)
    expect(second.gateway.snapshot).toMatchObject({
      state: 'disconnected',
      methods: [],
      capabilities: [],
      auth: null,
    })
    expect(second.gateway.snapshot.unavailableMethods.size).toBe(0)
  })

  it('disposes listeners and publishes immutable snapshot collections', () => {
    const store = new GatewayDataStore()
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)

    expect(listener).toHaveBeenCalledOnce()
    store.setConnectionState('connecting')
    expect(listener).toHaveBeenCalledTimes(2)

    unsubscribe()
    store.setConnectionState('connected')
    expect(listener).toHaveBeenCalledTimes(2)

    const methods = store.snapshot.methods as string[]
    expect(() => methods.push('unsafe.method')).toThrow()
    expect(store.snapshot.methods).toEqual([])
  })

  it('fails safely when an old Hello omits capability metadata', () => {
    const store = new GatewayDataStore()
    store.applyHello({
      features: { methods: ['usage.status', 42] as unknown as string[] },
    })

    expect(store.snapshot.contractStatus).toBe('legacy-contract')
    expect(store.snapshot.capabilitySource).toBe('features.methods')
    expect(store.snapshot.methods).toEqual(['usage.status'])
    expect(store.snapshot.capabilities).toEqual(['gateway.rpc'])
    expect(store.supportsCapability('gateway.sessions')).toBe(false)
  })

  it('clears connection-scoped data while retaining the latest diagnostic', () => {
    const store = new GatewayDataStore()
    const diagnostic = new Error('synthetic sequence gap')
    store.applyHello({
      capabilities: ['gateway.rpc'],
      contractStatus: 'advertised',
      extensions: ['channel.synthetic'],
      features: { methods: ['usage.status'] },
    })
    store.setDiagnostic(diagnostic)
    store.setConnectionState('disconnected')

    expect(store.snapshot).toMatchObject({
      state: 'disconnected',
      methods: [],
      capabilities: [],
      extensions: [],
      contract: null,
      diagnostic,
    })
  })

  it('does not expose mutable nested state or unavailable-method sets', () => {
    const store = new GatewayDataStore()
    store.applyHello({
      auth: { principal: { isOwner: true } },
      features: { methods: ['usage.status'] },
    })
    store.markMethodUnavailable('usage.status')

    const auth = store.snapshot.auth as {
      principal: { isOwner: boolean }
    }
    expect(() => {
      auth.principal.isOwner = false
    }).toThrow()
    expect(store.snapshot.auth).toEqual({
      principal: { isOwner: true },
    })
    expect(
      (store.snapshot.unavailableMethods as Set<string>).add,
    ).toBeUndefined()
    expect([...store.snapshot.unavailableMethods]).toEqual(['usage.status'])
  })
})
