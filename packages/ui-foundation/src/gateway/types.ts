import type {
  CapabilitySource,
  ContractInfo,
  ContractStatus,
  GatewayCallOptions,
  GatewayConnectionState,
  GatewayConnectionWaitOptions,
  NormalizedHello,
  NormalizedRuntimeInfo,
  ProtocolRangeInfo,
} from '@opensquilla/client-sdk'

export type GatewayUiConnectionState = GatewayConnectionState
export type GatewayUiCallOptions = GatewayCallOptions
export type GatewayUiConnectionWaitOptions = GatewayConnectionWaitOptions
export type GatewayUiContract = ContractInfo
export type GatewayUiRuntime = NormalizedRuntimeInfo
export type GatewayUiProtocolRange = ProtocolRangeInfo
export type GatewayUiContractStatus = ContractStatus
export type GatewayUiCapabilitySource = CapabilitySource

export type GatewayUiEventHandler = {
  bivarianceHack(...args: unknown[]): void
}['bivarianceHack']

export interface GatewayUiClient {
  readonly state: GatewayUiConnectionState
  readonly policy: Record<string, unknown>
  connect(endpoint: string, token?: string): void
  disconnect(): void
  call<T = unknown>(
    method: string,
    params?: Record<string, unknown>,
    options?: GatewayUiCallOptions,
  ): Promise<T>
  on(event: string, handler: GatewayUiEventHandler): () => void
  waitForConnection(
    timeoutMs?: number,
    signal?: AbortSignal,
    actions?: GatewayUiConnectionWaitOptions,
  ): Promise<void>
}

export interface GatewayConnectionSettings {
  endpoint: string
  token?: string
}

export interface GatewayHelloInput {
  policy?: unknown
  auth?: unknown
  features?: { methods?: unknown }
  contract?: GatewayUiContract | null
  contractStatus?: GatewayUiContractStatus
  runtime?: GatewayUiRuntime | null
  protocolRange?: GatewayUiProtocolRange | null
  capabilities?: unknown
  capabilitySource?: GatewayUiCapabilitySource
  extensions?: unknown
}

export interface GatewayStateSnapshot {
  readonly state: GatewayUiConnectionState
  readonly policy: Readonly<Record<string, unknown>> | null
  readonly auth: Readonly<Record<string, unknown>> | null
  readonly methods: readonly string[]
  readonly contract: GatewayUiContract | null
  readonly contractStatus: GatewayUiContractStatus
  readonly runtime: GatewayUiRuntime | null
  readonly protocolRange: GatewayUiProtocolRange | null
  readonly capabilities: readonly string[]
  readonly capabilitySource: GatewayUiCapabilitySource
  readonly extensions: readonly string[]
  readonly unavailableMethods: ReadonlySet<string>
  readonly diagnostic: Error | null
}

export type GatewayStateListener = (snapshot: GatewayStateSnapshot) => void

export interface GatewayStateSource {
  readonly snapshot: GatewayStateSnapshot
  subscribe(listener: GatewayStateListener, emitCurrent?: boolean): () => void
}

export interface GatewayClientBindings {
  onStateChange(state: GatewayUiConnectionState): void
  onHello(hello: NormalizedHello): void
  onDiagnostic(error: Error): void
}
