import type { GatewayConnectionSettings } from './types.js'

export interface StorageLike {
  readonly length: number
  getItem(key: string): string | null
  key(index: number): string | null
  removeItem(key: string): void
  setItem(key: string, value: string): void
}

export interface GatewayConnectionStorageOptions {
  persistent: StorageLike
  session: StorageLike
  defaultEndpoint: string
  endpointKey?: string
  tokenKey?: string
}

export interface GatewayConnectionStorage {
  load(): GatewayConnectionSettings
  save(settings: GatewayConnectionSettings): void
  clear(): void
}

export function clearStoragePrefix(storage: StorageLike, prefix: string): void {
  try {
    const keys = Array.from(
      { length: storage.length },
      (_, index) => storage.key(index),
    )
    for (const key of keys) {
      if (key?.startsWith(prefix)) storage.removeItem(key)
    }
  } catch {
    // Restricted browser storage behaves like an empty best-effort store.
  }
}

export function createGatewayConnectionStorage(
  options: GatewayConnectionStorageOptions,
): GatewayConnectionStorage {
  const endpointKey = options.endpointKey ?? 'opensquilla.wsUrl'
  const tokenKey = options.tokenKey ?? 'opensquilla.wsToken'

  return {
    load(): GatewayConnectionSettings {
      let endpoint = options.defaultEndpoint
      let token = ''
      try {
        endpoint = options.persistent.getItem(endpointKey) || endpoint
      } catch {}
      try {
        token = options.session.getItem(tokenKey) || ''
      } catch {}
      return {
        endpoint,
        ...(token ? { token } : {}),
      }
    },

    save(settings: GatewayConnectionSettings): void {
      try {
        // Remove stale credentials first; endpoint quota or policy failures
        // must never prevent the security cleanup.
        options.persistent.removeItem(tokenKey)
      } catch {}
      try {
        options.persistent.setItem(
          endpointKey,
          settings.endpoint || options.defaultEndpoint,
        )
      } catch {}
      try {
        if (settings.token) options.session.setItem(tokenKey, settings.token)
        else options.session.removeItem(tokenKey)
      } catch {}
    },

    clear(): void {
      try {
        options.persistent.removeItem(endpointKey)
        options.persistent.removeItem(tokenKey)
      } catch {}
      try {
        options.session.removeItem(tokenKey)
      } catch {}
    },
  }
}
