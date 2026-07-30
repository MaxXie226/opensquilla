import {
  createWebNativeCapabilityAdapter,
  evaluatePresentationAvailability,
  normalizeCapabilities,
} from './capabilities.js'
import { CompositionContractError } from './errors.js'
import { createContributionRegistrar } from './registrar.js'
import {
  NATIVE_CAPABILITY_API_VERSION,
  type CapabilityId,
  type ContributionId,
  type CreateOpenSquillaAppOptions,
  type NativeCapabilityAdapter,
  type OpenSquillaAppComposition,
  type PageLoadContext,
  type PresentationAvailability,
  type ScopedStateInstance,
} from './types.js'

class ReadonlySetView<T> implements ReadonlySet<T> {
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
    this.#values.forEach((value) => callbackfn.call(thisArg, value, value, this))
  }

  [Symbol.iterator](): SetIterator<T> {
    return this.#values[Symbol.iterator]()
  }
}

export async function createOpenSquillaApp(
  options: CreateOpenSquillaAppOptions,
): Promise<OpenSquillaAppComposition> {
  const native = options.native ?? createWebNativeCapabilityAdapter()
  assertNativeAdapterVersion(native)
  const capabilityIds = normalizeCapabilities([
    ...(options.capabilities ?? []),
    ...native.capabilities,
  ])
  const knownCapabilityIds = normalizeCapabilities([
    ...(options.knownCapabilities ?? []),
    ...capabilityIds,
  ])
  const capabilities = new ReadonlySetView<CapabilityId>(capabilityIds)
  const gatewayScopes = new ReadonlySetView(options.gatewayScopes ?? [])
  const services = Object.freeze({ ...(options.services ?? {}) })
  const registry = createContributionRegistrar(options.features).finalize({
    knownCapabilities: knownCapabilityIds,
    capabilities: capabilityIds,
  })
  const states = new Map<string, ScopedStateInstance>()
  const initialized: ScopedStateInstance[] = []

  const getService = <T = unknown>(id: string): T | undefined => (
    services[id] as T | undefined
  )

  try {
    for (const registered of registry.state) {
      const instance = await registered.contribution.create(Object.freeze({
        featureId: registered.featureId,
        namespace: registered.contribution.namespace,
        capabilities,
        native,
        getService,
      }))
      if (!isScopedStateInstance(instance)) {
        throw new TypeError(
          `State "${registered.contributionId}" returned an invalid scoped instance`,
        )
      }
      states.set(registered.contribution.namespace, instance)
      initialized.push(instance)
    }
  } catch (error) {
    await disposeStates(initialized)
    throw new CompositionContractError(
      'state_initialization_failed',
      'A scoped state factory failed during application composition',
      {},
      { cause: error },
    )
  }

  let disposed = false
  const stateOwners = new Map(
    registry.state.map((entry) => [entry.contribution.namespace, entry.featureId]),
  )
  const pages = new Map(
    registry.pages.map((entry) => [entry.contributionId, entry]),
  )

  const assertActive = (): void => {
    if (disposed) {
      throw new CompositionContractError(
        'composition_disposed',
        'The application composition has already been disposed',
      )
    }
  }

  const getState = <T = unknown>(namespace: string): T => {
    assertActive()
    const state = states.get(namespace)
    if (state === undefined) {
      throw new CompositionContractError(
        'unknown_state_namespace',
        `State namespace "${namespace}" is not registered`,
        { namespace },
      )
    }
    return state.value as T
  }

  const loadPage = async <TPage = unknown>(
    pageId: ContributionId,
  ): Promise<TPage> => {
    assertActive()
    const registered = pages.get(pageId)
    if (registered === undefined) {
      throw new CompositionContractError(
        'unknown_page_id',
        `Page "${pageId}" is not registered`,
        { pageId },
      )
    }
    const context: PageLoadContext = Object.freeze({
      featureId: registered.featureId,
      capabilities,
      native,
      getOwnState<T = unknown>(namespace: string): T {
        if (stateOwners.get(namespace) !== registered.featureId) {
          throw new CompositionContractError(
            'unknown_state_namespace',
            `Page "${pageId}" cannot access state namespace "${namespace}"`,
            { pageId, namespace, featureId: registered.featureId },
          )
        }
        return getState<T>(namespace)
      },
      getService,
    })
    return await registered.contribution.load(context) as TPage
  }

  const composition: OpenSquillaAppComposition = {
    registry,
    capabilities,
    gatewayScopes,
    native,
    get disposed() {
      return disposed
    },
    getState,
    loadPage,
    availability(requirements): PresentationAvailability {
      assertActive()
      return evaluatePresentationAvailability(requirements, {
        capabilities,
        gatewayScopes,
      })
    },
    async dispose(): Promise<void> {
      if (disposed) return
      disposed = true
      const errors = await disposeStates(initialized)
      states.clear()
      if (errors.length > 0) {
        throw new AggregateError(errors, 'One or more scoped state disposers failed')
      }
    },
  }
  return Object.freeze(composition)
}

function assertNativeAdapterVersion(native: NativeCapabilityAdapter): void {
  if (native.apiVersion !== NATIVE_CAPABILITY_API_VERSION) {
    throw new CompositionContractError(
      'unsupported_native_adapter_version',
      `Native capability adapter uses unsupported API version ${String(native.apiVersion)}`,
      {
        expected: NATIVE_CAPABILITY_API_VERSION,
        actual: native.apiVersion,
      },
    )
  }
}

function isScopedStateInstance(value: unknown): value is ScopedStateInstance {
  return value !== null && typeof value === 'object' && 'value' in value
}

async function disposeStates(
  states: readonly ScopedStateInstance[],
): Promise<unknown[]> {
  const errors: unknown[] = []
  for (const state of [...states].reverse()) {
    try {
      await state.dispose?.()
    } catch (error) {
      errors.push(error)
    }
  }
  return errors
}
