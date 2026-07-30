import { ref, computed } from 'vue'
import { defineStore } from 'pinia'
import {
  GatewayDataStore,
  clearStoragePrefix,
  createGatewayConnectionStorage,
  type GatewayHelloInput,
  type GatewayStateSnapshot,
} from '@opensquilla/ui-foundation'
import {
  RpcClient,
  type RpcCallOptions,
  type RpcConnectionWaitOptions,
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

function connectionStorage() {
  return createGatewayConnectionStorage({
    persistent: localStorage,
    session: sessionStorage,
    defaultEndpoint: getDefaultRpcUrl(),
    endpointKey: WS_URL_KEY,
    tokenKey: WS_TOKEN_KEY,
  })
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
  const settings = connectionStorage().load()
  return { url: settings.endpoint, token: settings.token || '' }
}

function saveConnectionSettings(url: string, token: string): void {
  connectionStorage().save({
    endpoint: url || getDefaultRpcUrl(),
    ...(token ? { token } : {}),
  })
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
  const gatewayData = new GatewayDataStore()

  function applyGatewaySnapshot(snapshot: GatewayStateSnapshot): void {
    state.value = snapshot.state
    policy.value = snapshot.policy ? { ...snapshot.policy } : null
    auth.value = snapshot.auth ? { ...snapshot.auth } : null
    methods.value = [...snapshot.methods]
    contract.value = snapshot.contract
    contractStatus.value = snapshot.contractStatus
    runtime.value = snapshot.runtime
    protocolRange.value = snapshot.protocolRange
    capabilities.value = [...snapshot.capabilities]
    capabilitySource.value = snapshot.capabilitySource
    extensions.value = [...snapshot.extensions]
    unavailableMethods.value = new Set(snapshot.unavailableMethods)
  }

  gatewayData.subscribe(applyGatewaySnapshot)

  const isConnected = computed(() => state.value === 'connected')
  const isConnecting = computed(() => state.value === 'connecting')
  const isLocalOwner = computed(() => {
    if (!isConnected.value) return false
    const principal = auth.value?.principal
    return Boolean(
      principal
      && typeof principal === 'object'
      && (principal as Record<string, unknown>).isOwner === true,
    )
  })
  const canManageProjectWorkspaces = computed(() =>
    isLocalOwner.value
    && supportsMethod('workspaces.list'))
  const canChooseProject = computed(() =>
    canManageProjectWorkspaces.value
    && supportsMethod('workspaces.open'))

  function init() {
    const rpc = new RpcClient()
    client.value = rpc

    rpc.on('_state', (s: 'disconnected' | 'connecting' | 'connected') => {
      gatewayData.setConnectionState(s)
    })

    rpc.on('_hello', (data: GatewayHelloInput) => {
      gatewayData.applyHello(data)
    })

    rpc.on('_gap', (detail: unknown) => {
      console.warn('[RPC] Sequence gap detected:', detail)
      gatewayData.setDiagnostic(
        detail instanceof Error
          ? detail
          : new Error('Gateway connection diagnostic'),
      )
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
      gatewayData.reset()
      client.value.connect(settings.url, settings.token)
    }
    return true
  }

  function disconnect() {
    client.value?.disconnect()
    gatewayData.reset()
  }

  function supportsMethod(method: string): boolean {
    return methods.value.includes(method) && !unavailableMethods.value.has(method)
  }

  function supportsCapability(capability: string): boolean {
    return capabilities.value.includes(capability)
  }

  function markMethodUnavailable(method: string): void {
    gatewayData.markMethodUnavailable(method)
  }

  async function call<T = unknown>(
    method: string,
    params?: Record<string, unknown>,
    options?: RpcCallOptions,
  ): Promise<T> {
    if (!client.value) throw new Error('RPC client not initialized')
    if (state.value !== 'connected') {
      throw new Error(`Cannot call ${method}: not connected (state: ${state.value})`)
    }
    return (
      options
        ? client.value.call(method, params, options)
        : client.value.call(method, params)
    ) as Promise<T>
  }

  function on(event: string, handler: RpcEventHandler): () => void {
    if (!client.value) {
      console.warn(`[RPC] No client for event subscription: ${event}`)
      return () => {}
    }
    return client.value.on(event, handler)
  }

  function waitForConnection(
    timeoutMs?: number,
    signal?: AbortSignal,
    actions?: RpcConnectionWaitOptions,
  ): Promise<void> {
    if (!client.value) return Promise.reject(new Error('RPC client not initialized'))
    return client.value.waitForConnection(timeoutMs, signal, actions)
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
    isLocalOwner,
    canManageProjectWorkspaces,
    canChooseProject,
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
