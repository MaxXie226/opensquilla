import { describe, expect, it } from 'vitest'

import {
  clearStoragePrefix,
  createGatewayConnectionStorage,
  type StorageLike,
} from '../src/index.js'

class MemoryStorage implements StorageLike {
  private readonly values = new Map<string, string>()

  get length(): number {
    return this.values.size
  }

  getItem(key: string): string | null {
    return this.values.get(key) ?? null
  }

  key(index: number): string | null {
    return [...this.values.keys()][index] ?? null
  }

  removeItem(key: string): void {
    this.values.delete(key)
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value)
  }
}

describe('Gateway connection storage', () => {
  it('persists the endpoint but keeps credentials in session storage', () => {
    const persistent = new MemoryStorage()
    const session = new MemoryStorage()
    persistent.setItem('opensquilla.wsToken', 'stale-persistent-secret')
    const storage = createGatewayConnectionStorage({
      persistent,
      session,
      defaultEndpoint: 'ws://default.test/ws',
    })

    storage.save({
      endpoint: 'wss://gateway.test/ws',
      token: '<synthetic-session-token>',
    })

    expect(storage.load()).toEqual({
      endpoint: 'wss://gateway.test/ws',
      token: '<synthetic-session-token>',
    })
    expect(persistent.getItem('opensquilla.wsUrl')).toBe('wss://gateway.test/ws')
    expect(persistent.getItem('opensquilla.wsToken')).toBeNull()
    expect(session.getItem('opensquilla.wsToken')).toBe('<synthetic-session-token>')
  })

  it('clears only keys owned by the requested namespace', () => {
    const storage = new MemoryStorage()
    storage.setItem('opensquilla.chat.draft:a', 'a')
    storage.setItem('opensquilla.chat.draft:b', 'b')
    storage.setItem('opensquilla.preference', 'keep')
    storage.setItem('unrelated', 'keep')

    clearStoragePrefix(storage, 'opensquilla.chat.draft:')

    expect(storage.getItem('opensquilla.chat.draft:a')).toBeNull()
    expect(storage.getItem('opensquilla.chat.draft:b')).toBeNull()
    expect(storage.getItem('opensquilla.preference')).toBe('keep')
    expect(storage.getItem('unrelated')).toBe('keep')
  })

  it('removes a stale persistent credential even when endpoint writes fail', () => {
    const persistent = new MemoryStorage()
    const session = new MemoryStorage()
    persistent.setItem('opensquilla.wsToken', 'stale-persistent-secret')
    const originalSetItem = persistent.setItem.bind(persistent)
    persistent.setItem = (key, value) => {
      if (key === 'opensquilla.wsUrl') throw new Error('synthetic quota failure')
      originalSetItem(key, value)
    }
    const storage = createGatewayConnectionStorage({
      persistent,
      session,
      defaultEndpoint: 'ws://default.test/ws',
    })

    storage.save({
      endpoint: 'wss://gateway.test/ws',
      token: '<synthetic-session-token>',
    })

    expect(persistent.getItem('opensquilla.wsToken')).toBeNull()
    expect(session.getItem('opensquilla.wsToken')).toBe('<synthetic-session-token>')
  })
})
