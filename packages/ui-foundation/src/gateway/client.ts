import {
  capabilitiesForMethods,
  ConnectionClosedError,
  GatewayClient,
  GatewayClientError,
  GatewayHttpError,
  HandshakeError,
  normalizeHelloFrame as normalizeSdkHelloFrame,
  RequestAbortError,
  RequestTimeoutError,
  SequenceGapError,
  type ContractInfo,
  type ErrorShape,
  type EventFrame,
  type GatewayCallOptions,
  type GatewayClientOptions,
  type GatewayConnectionWaitOptions,
  type GatewayConnectionState,
  type GatewayTerminationAction,
  type NormalizedHello,
  type NormalizedRuntimeInfo,
  type ProtocolRangeInfo,
} from '@opensquilla/client-sdk'

import type {
  GatewayClientBindings,
  GatewayConnectionSettings,
  GatewayUiClient,
  GatewayUiEventHandler,
} from './types.js'

export { capabilitiesForMethods }
export {
  ConnectionClosedError,
  GatewayClientError,
  GatewayHttpError,
  HandshakeError,
  RequestAbortError,
  RequestTimeoutError,
  SequenceGapError,
}
export type RpcErrorDetail = Partial<ErrorShape>
export type RpcCallOptions = GatewayCallOptions
export type RpcConnectionWaitOptions = GatewayConnectionWaitOptions
export type RpcTerminationAction = GatewayTerminationAction
export type ConnectionState = GatewayConnectionState
export type RpcContractInfo = ContractInfo
export type RpcRuntimeInfo = NormalizedRuntimeInfo
export type RpcProtocolRange = ProtocolRangeInfo
export type RpcEventHandler = GatewayUiEventHandler

export interface RpcClientError extends Error {
  code?: string
  details?: unknown
  retryable?: boolean
  retry_after_ms?: number
  accepted?: boolean
}

export class RpcTimeoutError extends Error implements RpcClientError {
  readonly code = 'RPC_TIMEOUT'

  constructor(
    readonly method: string,
    readonly timeoutMs: number,
  ) {
    super(`${method} timed out after ${timeoutMs}ms`)
    this.name = 'RpcTimeoutError'
  }
}

export class RpcAbortError extends Error implements RpcClientError {
  readonly code = 'RPC_ABORTED'

  constructor(readonly method: string) {
    super(`${method} was aborted`)
    this.name = 'RpcAbortError'
  }
}

export interface RpcFrame extends Record<string, unknown> {
  type?: string
  id?: string
  method?: string
  params?: Record<string, unknown>
  event?: string
  payload?: unknown
  meta?: Record<string, unknown>
  ok?: boolean
  error?: string | RpcErrorDetail
  protocol?: number
  policy?: Record<string, unknown>
  features?: {
    methods?: string[]
    events?: string[]
  }
  server?: {
    version?: string
    conn_id?: string
  }
  auth?: Record<string, unknown>
  contract?: Partial<RpcContractInfo>
  runtime?: Partial<RpcRuntimeInfo>
  protocolRange?: Partial<RpcProtocolRange>
  capabilities?: string[]
  extensions?: string[]
  contractStatus?: 'advertised' | 'legacy-contract'
  capabilitySource?: 'hello' | 'features.methods' | 'none'
  seq?: number
}

export function normalizeHelloFrame(data: RpcFrame, requestId: string): RpcFrame {
  return normalizeSdkHelloFrame(data, requestId) as unknown as RpcFrame
}

export interface CreateGatewayClientOptions
  extends Omit<
    GatewayClientOptions,
    'endpoint' | 'onDiagnostic' | 'onHello' | 'onStateChange' | 'token'
  > {
  endpoint: string
  token?: string
  bindings?: Partial<GatewayClientBindings>
}

export function createGatewayClient(options: CreateGatewayClientOptions): GatewayClient {
  const { bindings, ...clientOptions } = options
  return new GatewayClient({
    ...clientOptions,
    onStateChange: (state) => bindings?.onStateChange?.(state),
    onHello: (hello) => bindings?.onHello?.(hello),
    onDiagnostic: (error) => bindings?.onDiagnostic?.(error),
  })
}

function toLegacyError(error: unknown): unknown {
  if (error instanceof RequestTimeoutError) {
    return new RpcTimeoutError(error.method, error.timeoutMs)
  }
  if (error instanceof RequestAbortError) {
    return new RpcAbortError(error.method)
  }
  if (error instanceof GatewayClientError) {
    const legacy = error as GatewayClientError & RpcClientError
    if (error.retryAfterMs !== undefined) legacy.retry_after_ms = error.retryAfterMs
  }
  if (error instanceof ConnectionClosedError) {
    if (error.message === 'Gateway client disconnected') return new Error('Disconnected')
    if (error.message === 'Gateway connection closed') return new Error('Connection closed')
  }
  return error
}

/**
 * Compatibility facade for the public WebUI. New products should prefer
 * createGatewayClient() and GatewayDataStore directly.
 */
export class RpcClient implements GatewayUiClient {
  private client: GatewayClient | null = null
  private readonly listeners = new Map<string, Set<GatewayUiEventHandler>>()
  private connectionState: GatewayConnectionState = 'disconnected'
  private currentPolicy: Record<string, unknown> | null = null

  get state(): GatewayConnectionState {
    return this.connectionState
  }

  get policy(): Record<string, unknown> {
    return this.currentPolicy ?? {}
  }

  get _pending(): { readonly size: number } {
    return { size: this.client?.pendingRequestCount ?? 0 }
  }

  connect(endpoint: string, token?: string): void {
    this.client?.disconnect()
    const settings: GatewayConnectionSettings = {
      endpoint,
      ...(token ? { token } : {}),
    }
    this.client = createGatewayClient({
      ...settings,
      client: { name: 'opensquilla-web' },
      allowCallsDuringHandshake: true,
      connectTimeoutMs: 0,
      requestTimeoutMs: 0,
      bindings: {
        onStateChange: (state) => {
          this.connectionState = state
          if (state !== 'connected') this.currentPolicy = null
          this.emit('_state', state)
        },
        onHello: (hello) => {
          this.currentPolicy = hello.policy
          this.emit('_hello', hello)
        },
        onDiagnostic: (error) => {
          this.emit('_gap', diagnosticPayload(error))
        },
      },
    })
    this.client.on('*', (frame) => {
      this.emit(frame.event, frame.payload, frame.meta ?? {})
      this.emit('*', frame.event, frame.payload, frame.meta ?? {})
    })
    void this.client.connect().catch((error: unknown) => {
      this.emit('_error', toLegacyError(error))
    })
  }

  disconnect(): void {
    this.client?.disconnect()
    this.client = null
    this.currentPolicy = null
    if (this.connectionState !== 'disconnected') {
      this.connectionState = 'disconnected'
      this.emit('_state', 'disconnected')
    }
  }

  async call<T = unknown>(
    method: string,
    params: Record<string, unknown> = {},
    options: GatewayCallOptions = {},
  ): Promise<T> {
    if (!this.client) throw new Error('Not connected')
    try {
      return await this.client.call<T>(method, params, options)
    } catch (error) {
      throw toLegacyError(error)
    }
  }

  on(event: string, handler: GatewayUiEventHandler): () => void {
    const handlers = this.listeners.get(event) ?? new Set()
    handlers.add(handler)
    this.listeners.set(event, handlers)
    return () => {
      handlers.delete(handler)
      if (handlers.size === 0) this.listeners.delete(event)
    }
  }

  async waitForConnection(
    timeoutMs?: number,
    signal?: AbortSignal,
    actions?: GatewayConnectionWaitOptions,
  ): Promise<void> {
    if (!this.client) return Promise.reject(new Error('Not connected'))
    try {
      await this.client.waitForConnection(timeoutMs, signal, actions)
    } catch (error) {
      throw toLegacyError(error)
    }
  }

  private emit(event: string, ...args: unknown[]): void {
    for (const handler of this.listeners.get(event) ?? []) handler(...args)
  }
}

function diagnosticPayload(error: Error): unknown {
  const gap = error as SequenceGapError
  if (
    typeof gap.expected === 'number'
    && typeof gap.actual === 'number'
  ) {
    return {
      expected: gap.expected,
      actual: gap.actual,
      ...(gap.event ? { event: gap.event } : {}),
    }
  }
  return { reason: error.message }
}

export type {
  EventFrame,
  GatewayCallOptions,
  GatewayClientOptions,
  GatewayConnectionState,
  GatewayConnectionWaitOptions,
  GatewayTerminationAction,
  NormalizedHello,
}
