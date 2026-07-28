import type {
  ContractInfo,
  PolicyInfo,
  ProtocolRangeInfo,
  RuntimeInfo,
} from './generated.js'
import { HandshakeError } from './errors.js'

export const CLIENT_MIN_PROTOCOL = 3 as const
export const CLIENT_MAX_PROTOCOL = 3 as const

export type ContractStatus = 'advertised' | 'legacy-contract'
export type CapabilitySource = 'hello' | 'features.methods' | 'none'

export interface ClientProtocolRange {
  min: number
  max: number
}

export interface NormalizedRuntimeInfo extends Omit<RuntimeInfo, 'arch' | 'platform'> {
  arch?: string
  platform?: string
}

export interface NormalizedHello {
  type: 'hello-ok'
  id?: string
  protocol: number
  policy: PolicyInfo & Record<string, unknown>
  features: {
    methods: string[]
    events: string[]
  }
  server: {
    version: string
    conn_id?: string
  }
  auth?: Record<string, unknown>
  contract?: ContractInfo
  contractStatus: ContractStatus
  runtime: NormalizedRuntimeInfo
  protocolRange: ProtocolRangeInfo
  capabilities: string[]
  capabilitySource: CapabilitySource
  extensions: string[]
  [key: string]: unknown
}

const CAPABILITY_ID_PATTERN = /^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)+$/
const DIGEST_PATTERN = /^sha256:[0-9a-f]{64}$/

const CAPABILITY_METHOD_REQUIREMENTS: ReadonlyArray<readonly [string, readonly string[]]> = [
  ['gateway.sessions', ['chat.history', 'chat.send', 'sessions.list', 'sessions.resolve']],
]

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === 'object' && !Array.isArray(value)
}

function stringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string')
    : []
}

function stableIds(value: unknown): string[] {
  return stringList(value).filter((item) => CAPABILITY_ID_PATTERN.test(item))
}

function isWireInteger(value: unknown): value is number {
  return Number.isInteger(value)
}

function optionalString(value: unknown): string | undefined {
  return typeof value === 'string' && value ? value : undefined
}

export function capabilitiesForMethods(methods: readonly string[]): string[] {
  if (methods.length === 0) return []
  const available = new Set(methods)
  return [
    'gateway.rpc',
    ...CAPABILITY_METHOD_REQUIREMENTS.filter(([, required]) =>
      required.every((method) => available.has(method))
    ).map(([capability]) => capability),
  ]
}

export function normalizeHelloFrame(
  input: unknown,
  requestId: string,
  clientRange: ClientProtocolRange = {
    min: CLIENT_MIN_PROTOCOL,
    max: CLIENT_MAX_PROTOCOL,
  },
): NormalizedHello {
  if (!isRecord(input) || input.type !== 'hello-ok') {
    throw new HandshakeError('Expected hello-ok frame')
  }
  if (
    !isWireInteger(input.protocol) ||
    input.protocol < clientRange.min ||
    input.protocol > clientRange.max
  ) {
    throw new HandshakeError('Negotiated protocol is outside the requested range')
  }

  const hasNewMetadata = ['contract', 'runtime', 'protocolRange', 'capabilities', 'extensions']
    .some((field) => Object.prototype.hasOwnProperty.call(input, field))
  if (input.id === undefined) {
    if (hasNewMetadata) {
      throw new HandshakeError('New-format hello-ok frame is missing response id')
    }
  } else if (input.id !== requestId) {
    throw new HandshakeError('hello-ok response id does not match connect request')
  }

  const advertisedRange = isRecord(input.protocolRange) ? input.protocolRange : undefined
  const protocolRange =
    advertisedRange &&
    isWireInteger(advertisedRange.min) &&
    isWireInteger(advertisedRange.max) &&
    advertisedRange.min <= advertisedRange.max
      ? { min: advertisedRange.min, max: advertisedRange.max }
      : { min: input.protocol, max: input.protocol }
  if (protocolRange.max < clientRange.min || protocolRange.min > clientRange.max) {
    throw new HandshakeError('Gateway protocol range does not overlap client range')
  }

  const rawContract = isRecord(input.contract) ? input.contract : undefined
  const contractValid =
    !!rawContract &&
    isWireInteger(rawContract.schemaVersion) &&
    typeof rawContract.digest === 'string' &&
    DIGEST_PATTERN.test(rawContract.digest) &&
    typeof rawContract.generatedFrom === 'string' &&
    rawContract.generatedFrom.length > 0
  const contract = contractValid
    ? {
        schemaVersion: rawContract.schemaVersion as number,
        digest: rawContract.digest as string,
        generatedFrom: rawContract.generatedFrom as string,
      }
    : undefined

  const rawFeatures = isRecord(input.features) ? input.features : {}
  const methods = stringList(rawFeatures.methods)
  const events = stringList(rawFeatures.events)
  const capabilitiesPresent = Object.prototype.hasOwnProperty.call(input, 'capabilities')
  const capabilities = capabilitiesPresent
    ? stableIds(input.capabilities)
    : capabilitiesForMethods(methods)
  const capabilitySource: CapabilitySource =
    capabilitiesPresent && Array.isArray(input.capabilities)
      ? 'hello'
      : !capabilitiesPresent && methods.length > 0
        ? 'features.methods'
        : 'none'

  const rawServer = isRecord(input.server) ? input.server : {}
  const serverVersion = optionalString(rawServer.version) ?? 'unknown'
  const rawRuntime = isRecord(input.runtime) ? input.runtime : undefined
  const coreVersion = optionalString(rawRuntime?.coreVersion) ?? serverVersion
  const runtime: NormalizedRuntimeInfo = {
    coreVersion,
    buildCommit: optionalString(rawRuntime?.buildCommit) ?? null,
  }
  const platform = optionalString(rawRuntime?.platform)
  const arch = optionalString(rawRuntime?.arch)
  if (platform !== undefined) runtime.platform = platform
  if (arch !== undefined) runtime.arch = arch

  const server: NormalizedHello['server'] = { version: serverVersion }
  const connectionId = optionalString(rawServer.conn_id)
  if (connectionId !== undefined) server.conn_id = connectionId

  const normalized: NormalizedHello = {
    ...input,
    type: 'hello-ok',
    protocol: input.protocol,
    policy: (isRecord(input.policy) ? input.policy : {}) as PolicyInfo &
      Record<string, unknown>,
    features: { methods, events },
    server,
    contractStatus: contractValid ? 'advertised' : 'legacy-contract',
    runtime,
    protocolRange,
    capabilities,
    capabilitySource,
    extensions: stableIds(input.extensions),
  }
  if (typeof input.id === 'string') normalized.id = input.id
  if (isRecord(input.auth)) normalized.auth = input.auth
  if (contract !== undefined) normalized.contract = contract
  else delete normalized.contract
  return normalized
}
