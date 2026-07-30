import { capabilitiesForMethods } from '@opensquilla/client-sdk'

import type {
  GatewayHelloInput,
  GatewayStateListener,
  GatewayStateSnapshot,
  GatewayStateSource,
  GatewayUiConnectionState,
} from './types.js'

const EMPTY_STATE: GatewayStateSnapshot = Object.freeze({
  state: 'disconnected',
  policy: null,
  auth: null,
  methods: Object.freeze([]),
  contract: null,
  contractStatus: 'legacy-contract',
  runtime: null,
  protocolRange: null,
  capabilities: Object.freeze([]),
  capabilitySource: 'none',
  extensions: Object.freeze([]),
  unavailableMethods: new Set<string>(),
  diagnostic: null,
})

function stringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string')
    : []
}

function recordOrNull(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function frozenClone<T>(value: T): T {
  if (Array.isArray(value)) {
    return Object.freeze(value.map((item) => frozenClone(item))) as T
  }
  if (value && typeof value === 'object') {
    const prototype = Object.getPrototypeOf(value)
    if (prototype === Object.prototype || prototype === null) {
      const result: Record<string, unknown> = {}
      for (const [key, item] of Object.entries(value)) {
        result[key] = frozenClone(item)
      }
      return Object.freeze(result) as T
    }
  }
  return value
}

class ReadonlySetSnapshot<T> implements ReadonlySet<T> {
  readonly #values: Set<T>

  constructor(values: Iterable<T>) {
    this.#values = new Set(values)
    Object.freeze(this)
  }

  get size(): number {
    return this.#values.size
  }

  has(value: T): boolean {
    return this.#values.has(value)
  }

  entries(): SetIterator<[T, T]> {
    return this.#values.entries()
  }

  keys(): SetIterator<T> {
    return this.#values.keys()
  }

  values(): SetIterator<T> {
    return this.#values.values()
  }

  forEach(
    callbackfn: (value: T, value2: T, set: ReadonlySet<T>) => void,
    thisArg?: unknown,
  ): void {
    this.#values.forEach((value) => {
      callbackfn.call(thisArg, value, value, this)
    })
  }

  [Symbol.iterator](): SetIterator<T> {
    return this.#values[Symbol.iterator]()
  }
}

function cloneSnapshot(snapshot: GatewayStateSnapshot): GatewayStateSnapshot {
  return Object.freeze({
    ...snapshot,
    policy: snapshot.policy ? frozenClone(snapshot.policy) : null,
    auth: snapshot.auth ? frozenClone(snapshot.auth) : null,
    methods: Object.freeze([...snapshot.methods]),
    contract: snapshot.contract ? frozenClone(snapshot.contract) : null,
    runtime: snapshot.runtime ? frozenClone(snapshot.runtime) : null,
    protocolRange: snapshot.protocolRange
      ? frozenClone(snapshot.protocolRange)
      : null,
    capabilities: Object.freeze([...snapshot.capabilities]),
    extensions: Object.freeze([...snapshot.extensions]),
    unavailableMethods: new ReadonlySetSnapshot(snapshot.unavailableMethods),
  })
}

export class GatewayDataStore implements GatewayStateSource {
  private value = cloneSnapshot(EMPTY_STATE)
  private readonly listeners = new Set<GatewayStateListener>()

  get snapshot(): GatewayStateSnapshot {
    return this.value
  }

  subscribe(listener: GatewayStateListener, emitCurrent = true): () => void {
    this.listeners.add(listener)
    if (emitCurrent) listener(this.value)
    return () => this.listeners.delete(listener)
  }

  setConnectionState(state: GatewayUiConnectionState): void {
    const next: GatewayStateSnapshot = state === 'connected'
      ? { ...this.value, state, diagnostic: null }
      : {
          ...EMPTY_STATE,
          state,
          diagnostic: this.value.diagnostic,
        }
    this.publish(next)
  }

  applyHello(input: GatewayHelloInput): void {
    const rawFeatures = input.features
    const features =
      rawFeatures && typeof rawFeatures === 'object'
        ? rawFeatures as { methods?: unknown }
        : {}
    const methods = stringList(features.methods)
    const explicitCapabilities = Array.isArray(input.capabilities)
    const capabilities = explicitCapabilities
      ? stringList(input.capabilities)
      : capabilitiesForMethods(methods)
    this.publish({
      ...this.value,
      policy: recordOrNull(input.policy),
      auth: recordOrNull(input.auth),
      methods,
      contract: input.contract ?? null,
      contractStatus:
        input.contractStatus === 'advertised'
        || input.contractStatus === 'legacy-contract'
          ? input.contractStatus
          : input.contract
            ? 'advertised'
            : 'legacy-contract',
      runtime: input.runtime ?? null,
      protocolRange: input.protocolRange ?? null,
      capabilities,
      capabilitySource:
        input.capabilitySource === 'hello'
        || input.capabilitySource === 'features.methods'
        || input.capabilitySource === 'none'
          ? input.capabilitySource
          : explicitCapabilities
            ? 'hello'
            : methods.length > 0
              ? 'features.methods'
              : 'none',
      extensions: stringList(input.extensions),
      unavailableMethods: new Set(),
      diagnostic: null,
    })
  }

  setDiagnostic(diagnostic: Error): void {
    this.publish({ ...this.value, diagnostic })
  }

  markMethodUnavailable(method: string): void {
    if (!method || this.value.unavailableMethods.has(method)) return
    this.publish({
      ...this.value,
      unavailableMethods: new Set([...this.value.unavailableMethods, method]),
    })
  }

  supportsMethod(method: string): boolean {
    return (
      this.value.methods.includes(method)
      && !this.value.unavailableMethods.has(method)
    )
  }

  supportsCapability(capability: string): boolean {
    return this.value.capabilities.includes(capability)
  }

  reset(): void {
    this.publish(EMPTY_STATE)
  }

  dispose(): void {
    this.listeners.clear()
    this.value = cloneSnapshot(EMPTY_STATE)
  }

  private publish(snapshot: GatewayStateSnapshot): void {
    this.value = cloneSnapshot(snapshot)
    for (const listener of this.listeners) listener(this.value)
  }
}

export interface FoundationStoreContext {
  gateway?: GatewayDataStore
}

export function createFoundationStores(
  context: FoundationStoreContext = {},
): { gateway: GatewayDataStore } {
  return {
    gateway: context.gateway ?? new GatewayDataStore(),
  }
}
