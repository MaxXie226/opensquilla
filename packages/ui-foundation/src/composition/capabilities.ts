import { CompositionContractError } from './errors.js'
import {
  NATIVE_CAPABILITY_API_VERSION,
  type CapabilityId,
  type ContributionRequirements,
  type NativeCapabilityAdapter,
  type NativeCapabilityInvocation,
  type NativeCapabilityResult,
  type PresentationAvailability,
  type PresentationEnvironment,
} from './types.js'

const CAPABILITY_ID_PATTERN = /^[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*)+$/

export interface CreateNativeCapabilityAdapterOptions {
  readonly bridgeVersion: string
  readonly capabilities: readonly CapabilityId[]
  invoke<T = unknown>(
    invocation: NativeCapabilityInvocation,
  ): Promise<NativeCapabilityResult<T>>
}

export function assertCapabilityId(capability: CapabilityId): void {
  if (!CAPABILITY_ID_PATTERN.test(capability)) {
    throw new CompositionContractError(
      'invalid_capability',
      `Capability "${capability}" must be a lowercase namespaced identifier`,
      { capability },
    )
  }
}

export function normalizeCapabilities(
  capabilities: Iterable<CapabilityId>,
): readonly CapabilityId[] {
  const normalized = [...new Set(capabilities)]
  for (const capability of normalized) assertCapabilityId(capability)
  return Object.freeze(normalized.sort())
}

export function createNativeCapabilityAdapter(
  options: CreateNativeCapabilityAdapterOptions,
): NativeCapabilityAdapter {
  const capabilities = normalizeCapabilities(options.capabilities)
  const supported = new Set(capabilities)

  return Object.freeze({
    apiVersion: NATIVE_CAPABILITY_API_VERSION,
    bridgeVersion: options.bridgeVersion,
    capabilities,
    async invoke<T = unknown>(
      invocation: NativeCapabilityInvocation,
    ): Promise<NativeCapabilityResult<T>> {
      assertCapabilityId(invocation.capability)
      if (!supported.has(invocation.capability)) {
        return unsupportedResult(invocation.capability)
      }
      if (invocation.signal?.aborted) {
        return {
          ok: false,
          error: {
            code: 'unavailable',
            capability: invocation.capability,
            message: `Capability "${invocation.capability}" invocation was aborted`,
          },
        }
      }
      try {
        return await options.invoke<T>(invocation)
      } catch (error) {
        return {
          ok: false,
          error: {
            code: 'failed',
            capability: invocation.capability,
            message: error instanceof Error
              ? error.message
              : `Capability "${invocation.capability}" failed`,
          },
        }
      }
    },
  })
}

export function createWebNativeCapabilityAdapter(): NativeCapabilityAdapter {
  return Object.freeze({
    apiVersion: NATIVE_CAPABILITY_API_VERSION,
    bridgeVersion: null,
    capabilities: Object.freeze([]),
    async invoke<T = unknown>(
      invocation: NativeCapabilityInvocation,
    ): Promise<NativeCapabilityResult<T>> {
      assertCapabilityId(invocation.capability)
      return unsupportedResult(invocation.capability)
    },
  })
}

export function evaluatePresentationAvailability(
  requirements: ContributionRequirements | undefined,
  environment: PresentationEnvironment,
): PresentationAvailability {
  const missingCapabilities = normalizeRequestedValues(
    requirements?.capabilities,
    environment.capabilities,
  )
  const missingGatewayScopes = normalizeRequestedValues(
    requirements?.gatewayScopes,
    environment.gatewayScopes ?? new Set<string>(),
  )
  const reasons = [
    ...(missingCapabilities.length > 0 ? ['missing_capability' as const] : []),
    ...(missingGatewayScopes.length > 0 ? ['missing_gateway_scope' as const] : []),
  ]

  return Object.freeze({
    available: reasons.length === 0,
    reasons: Object.freeze(reasons),
    missingCapabilities: Object.freeze(missingCapabilities),
    missingGatewayScopes: Object.freeze(missingGatewayScopes),
  })
}

function normalizeRequestedValues(
  requested: readonly string[] | undefined,
  available: ReadonlySet<string>,
): string[] {
  return [...new Set(requested ?? [])]
    .filter((value) => !available.has(value))
    .sort()
}

function unsupportedResult<T = unknown>(
  capability: CapabilityId,
): NativeCapabilityResult<T> {
  return {
    ok: false,
    error: {
      code: 'unsupported',
      capability,
      message: `Capability "${capability}" is not supported by this host`,
    },
  }
}
