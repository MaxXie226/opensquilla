import { ref, computed } from 'vue'
import { defineStore } from 'pinia'
import {
  RpcClient,
  capabilitiesForMethods,
  type RpcContractInfo,
  type RpcEventHandler,
  type RpcProtocolRange,
  type RpcRuntimeInfo,
} from '@/lib/rpc'

const WS_URL_KEY = 'opensquilla.wsUrl'
const WS_TOKEN_KEY = 'opensquilla.wsToken'
const CACHED_AUTH_KEY = 'opensquilla.cachedAuth'
const CHAT_DRAFT_PREFIX = 'opensquilla.chat.draft:'

function getDefaultRpcUrl(): string {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${location.host}/ws`
}

function clearStoragePrefix(storage: Storage, prefix: string): void {
  try {
    const keys = Array.from({ length: storage.length }, (_, index) => storage.key(index))
    for (const key of keys) {
      if (!key) continue
      if (key.startsWith(prefix)) storage.removeItem(key)
    }
  } catch {}
}

function clearLinkTokenBrowserState(): void {
  try {
    localStorage.removeItem(WS_URL_KEY)
    clearStoragePrefix(localStorage, CHAT_DRAFT_PREFIX)
  } catch {}
  try {
    sessionStorage.removeItem(WS_TOKEN_KEY)
    sessionStorage.removeItem(CACHED_AUTH_KEY)
  } catch {}
}

function consumeLinkTokenFromUrl(): { url: string; token: string } | null {
  let url: URL
  try {
    url = new URL(window.location.href)
  } catch {
    return null
  }
  const token = (url.searchParams.get('token') || '').trim()
  if (!token) return null

  clearLinkTokenBrowserState()
  const rpcUrl = getDefaultRpcUrl()
  saveConnectionSettings(rpcUrl, token)

  try {
    url.searchParams.delete('token')
    const cleaned = `${url.pathname}${url.search}${url.hash}`
    window.history.replaceState(null, '', cleaned)
  } catch {}

  return { url: rpcUrl, token }
}

function loadConnectionSettings(): { url: string; token: string } {
  let url = getDefaultRpcUrl()
  let token = ''
  try { url = localStorage.getItem(WS_URL_KEY) || url } catch {}
  try { token = sessionStorage.getItem(WS_TOKEN_KEY) || '' } catch {}
  return { url, token }
}

function saveConnectionSettings(url: string, token: string): void {
  try { localStorage.setItem(WS_URL_KEY, url || getDefaultRpcUrl()) } catch {}
  try {
    if (token) sessionStorage.setItem(WS_TOKEN_KEY, token)
    else sessionStorage.removeItem(WS_TOKEN_KEY)
  } catch {}
}

export const useRpcStore = defineStore('rpc', () => {
  const client = ref<RpcClient | null>(null)
  const state = ref<'disconnected' | 'connecting' | 'connected'>('disconnected')
  const policy = ref<Record<string, unknown> | null>(null)
  const auth = ref<Record<string, unknown> | null>(null)
  const methods = ref<string[]>([])
  const contract = ref<RpcContractInfo | null>(null)
  const contractStatus = ref<'advertised' | 'legacy-contract'>('legacy-contract')
  const runtime = ref<RpcRuntimeInfo | null>(null)
  const protocolRange = ref<RpcProtocolRange | null>(null)
  const capabilities = ref<string[]>([])
  const capabilitySource = ref<'hello' | 'features.methods' | 'none'>('none')
  const extensions = ref<string[]>([])
  const unavailableMethods = ref<Set<string>>(new Set())
  const error = ref<string | null>(null)

  const isConnected = computed(() => state.value === 'connected')
  const isConnecting = computed(() => state.value === 'connecting')

  function init() {
    const rpc = new RpcClient()
    client.value = rpc

    rpc.on('_state', (s: 'disconnected' | 'connecting' | 'connected') => {
      state.value = s
    })

    rpc.on('_hello', (data: {
      policy?: Record<string, unknown>
      auth?: Record<string, unknown>
      features?: { methods?: unknown }
      contract?: RpcContractInfo
      contractStatus?: 'advertised' | 'legacy-contract'
      runtime?: RpcRuntimeInfo
      protocolRange?: RpcProtocolRange
      capabilities?: unknown
      capabilitySource?: 'hello' | 'features.methods' | 'none'
      extensions?: unknown
    }) => {
      policy.value = data.policy || null
      auth.value = data.auth || null
      methods.value = Array.isArray(data.features?.methods)
        ? data.features.methods.filter((method): method is string => typeof method === 'string')
        : []
      contract.value = data.contract || null
      contractStatus.value = data.contractStatus || (data.contract ? 'advertised' : 'legacy-contract')
      runtime.value = data.runtime || null
      protocolRange.value = data.protocolRange || null
      capabilities.value = Array.isArray(data.capabilities)
        ? data.capabilities.filter((item): item is string => typeof item === 'string')
        : capabilitiesForMethods(methods.value)
      capabilitySource.value =
        data.capabilitySource ||
        (Array.isArray(data.capabilities)
          ? 'hello'
          : methods.value.length > 0
            ? 'features.methods'
            : 'none')
      extensions.value = Array.isArray(data.extensions)
        ? data.extensions.filter((item): item is string => typeof item === 'string')
        : []
      unavailableMethods.value = new Set()
    })

    rpc.on('_gap', (detail: unknown) => {
      console.warn('[RPC] Sequence gap detected:', detail)
    })

    // Auto-connect on init. Desktop shells use the local gateway serving this UI.
    consumeLinkTokenFromUrl()
    const { url, token } = loadConnectionSettings()
    if (rpc.state === 'disconnected') {
      rpc.connect(url, token || undefined)
    }
  }

  async function connect(url: string, token?: string) {
    if (!client.value) throw new Error('RPC client not initialized')
    error.value = null
    saveConnectionSettings(url, token || '')
    client.value.connect(url, token)
  }

  function applyLinkTokenFromUrl(): boolean {
    const settings = consumeLinkTokenFromUrl()
    if (!settings) return false
    if (client.value) {
      client.value.disconnect()
      error.value = null
      policy.value = null
      auth.value = null
      methods.value = []
      contract.value = null
      contractStatus.value = 'legacy-contract'
      runtime.value = null
      protocolRange.value = null
      capabilities.value = []
      capabilitySource.value = 'none'
      extensions.value = []
      unavailableMethods.value = new Set()
      client.value.connect(settings.url, settings.token)
    }
    return true
  }

  function disconnect() {
    client.value?.disconnect()
    state.value = 'disconnected'
    policy.value = null
    auth.value = null
    methods.value = []
    contract.value = null
    contractStatus.value = 'legacy-contract'
    runtime.value = null
    protocolRange.value = null
    capabilities.value = []
    capabilitySource.value = 'none'
    extensions.value = []
    unavailableMethods.value = new Set()
  }

  function supportsMethod(method: string): boolean {
    return methods.value.includes(method) && !unavailableMethods.value.has(method)
  }

  function supportsCapability(capability: string): boolean {
    return capabilities.value.includes(capability)
  }

  function markMethodUnavailable(method: string): void {
    if (!method) return
    unavailableMethods.value = new Set([...unavailableMethods.value, method])
  }

  async function call<T = unknown>(method: string, params?: Record<string, unknown>): Promise<T> {
    if (!client.value) throw new Error('RPC client not initialized')
    if (state.value !== 'connected') {
      throw new Error(`Cannot call ${method}: not connected (state: ${state.value})`)
    }
    return client.value.call(method, params) as Promise<T>
  }

  function on(event: string, handler: RpcEventHandler): () => void {
    if (!client.value) {
      console.warn(`[RPC] No client for event subscription: ${event}`)
      return () => {}
    }
    return client.value.on(event, handler)
  }

  function waitForConnection(timeoutMs?: number): Promise<void> {
    if (!client.value) return Promise.reject(new Error('RPC client not initialized'))
    if (state.value === 'connected') return Promise.resolve()
    return client.value.waitForConnection(timeoutMs)
  }

  return {
    client,
    state,
    policy,
    auth,
    methods,
    contract,
    contractStatus,
    runtime,
    protocolRange,
    capabilities,
    capabilitySource,
    extensions,
    error,
    isConnected,
    isConnecting,
    init,
    connect,
    applyLinkTokenFromUrl,
    disconnect,
    supportsMethod,
    supportsCapability,
    markMethodUnavailable,
    call,
    on,
    waitForConnection,
  }
})
