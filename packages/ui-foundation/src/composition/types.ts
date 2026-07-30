export const UI_COMPOSITION_API_VERSION = 1 as const
export const NATIVE_CAPABILITY_API_VERSION = 1 as const

export type UiCompositionApiVersion = typeof UI_COMPOSITION_API_VERSION
export type NativeCapabilityApiVersion = typeof NATIVE_CAPABILITY_API_VERSION

export type FeatureId = string
export type ContributionId = string
export type CapabilityId = string

export type Awaitable<T> = T | Promise<T>

export interface ContributionRequirements {
  /**
   * Presentation requirement only. Gateway authorization remains authoritative
   * even when the host reports every scope in this list.
   */
  readonly gatewayScopes?: readonly string[]
  readonly capabilities?: readonly CapabilityId[]
}

export interface PageLoadContext {
  readonly featureId: FeatureId
  readonly capabilities: ReadonlySet<CapabilityId>
  readonly native: NativeCapabilityAdapter
  getOwnState<T = unknown>(namespace: string): T
  getService<T = unknown>(id: string): T | undefined
}

export type PageLoader<TPage = unknown> = (
  context: PageLoadContext,
) => Awaitable<TPage>

export interface PageContribution<TPage = unknown> {
  readonly id: ContributionId
  readonly load: PageLoader<TPage>
  readonly order?: number
}

export interface RouteContribution {
  readonly id: ContributionId
  readonly path: string
  readonly name: string
  readonly pageId: ContributionId
  readonly order?: number
  readonly requirements?: ContributionRequirements
  readonly metadata?: Readonly<Record<string, unknown>>
}

export type NavigationSlot = 'primary' | 'secondary' | 'footer'

export interface NavigationContribution {
  readonly id: ContributionId
  readonly routeId: ContributionId
  readonly slot: NavigationSlot
  readonly label: string
  readonly order?: number
  readonly requirements?: ContributionRequirements
  readonly metadata?: Readonly<Record<string, unknown>>
}

export interface StateFactoryContext {
  readonly featureId: FeatureId
  readonly namespace: string
  readonly capabilities: ReadonlySet<CapabilityId>
  readonly native: NativeCapabilityAdapter
  getService<T = unknown>(id: string): T | undefined
}

export interface ScopedStateInstance<TState = unknown> {
  readonly value: TState
  dispose?(): Awaitable<void>
}

export interface StateContribution<TState = unknown> {
  readonly id: ContributionId
  readonly namespace: string
  readonly order?: number
  create(context: StateFactoryContext): Awaitable<ScopedStateInstance<TState>>
}

export interface FeatureContributions {
  readonly pages?: readonly PageContribution[]
  readonly routes?: readonly RouteContribution[]
  readonly navigation?: readonly NavigationContribution[]
  readonly state?: readonly StateContribution[]
}

/**
 * A product-neutral, statically linked feature declaration.
 *
 * The host selects which declarations to pass to the registrar. This contract
 * intentionally carries no product enablement, branding, license, or secret
 * data and is not a remote-code loading mechanism.
 */
export interface FeatureModuleContract {
  readonly id: FeatureId
  readonly apiVersion: UiCompositionApiVersion
  readonly dependsOn?: readonly FeatureId[]
  readonly requiredCapabilities?: readonly CapabilityId[]
  readonly optionalCapabilities?: readonly CapabilityId[]
  readonly order?: number
  readonly contributions?: FeatureContributions
}

export type NativeCapabilityErrorCode =
  | 'unsupported'
  | 'unavailable'
  | 'invalid_request'
  | 'denied'
  | 'failed'

export interface NativeCapabilityFailure {
  readonly code: NativeCapabilityErrorCode
  readonly capability: CapabilityId
  readonly message: string
}

export type NativeCapabilityResult<T = unknown> =
  | { readonly ok: true; readonly value: T }
  | { readonly ok: false; readonly error: NativeCapabilityFailure }

export interface NativeCapabilityInvocation {
  readonly capability: CapabilityId
  readonly request?: unknown
  readonly signal?: AbortSignal
}

/**
 * Versioned host boundary. Declared capabilities describe callable operations;
 * they never grant permission and do not replace host-side input, sender, path,
 * or policy validation.
 */
export interface NativeCapabilityAdapter {
  readonly apiVersion: NativeCapabilityApiVersion
  readonly bridgeVersion: string | null
  readonly capabilities: readonly CapabilityId[]
  invoke<T = unknown>(
    invocation: NativeCapabilityInvocation,
  ): Promise<NativeCapabilityResult<T>>
}

export interface ContributionOwner {
  readonly featureId: FeatureId
  readonly contributionId: ContributionId
}

export interface RegisteredPage extends ContributionOwner {
  readonly contribution: PageContribution
}

export interface RegisteredRoute extends ContributionOwner {
  readonly contribution: RouteContribution
}

export interface RegisteredNavigation extends ContributionOwner {
  readonly contribution: NavigationContribution
}

export interface RegisteredState extends ContributionOwner {
  readonly contribution: StateContribution
}

export interface FeatureCapabilityStatus {
  readonly featureId: FeatureId
  readonly missingOptionalCapabilities: readonly CapabilityId[]
}

export interface ContributionRegistrySnapshot {
  readonly apiVersion: UiCompositionApiVersion
  readonly features: readonly FeatureModuleContract[]
  readonly pages: readonly RegisteredPage[]
  readonly routes: readonly RegisteredRoute[]
  readonly navigation: readonly RegisteredNavigation[]
  readonly state: readonly RegisteredState[]
  readonly featureCapabilities: readonly FeatureCapabilityStatus[]
}

export type PresentationUnavailableReason =
  | 'missing_capability'
  | 'missing_gateway_scope'

export interface PresentationAvailability {
  readonly available: boolean
  readonly reasons: readonly PresentationUnavailableReason[]
  readonly missingCapabilities: readonly CapabilityId[]
  readonly missingGatewayScopes: readonly string[]
}

export interface PresentationEnvironment {
  readonly capabilities: ReadonlySet<CapabilityId>
  readonly gatewayScopes?: ReadonlySet<string>
}

export interface CreateOpenSquillaAppOptions {
  readonly features: readonly FeatureModuleContract[]
  readonly knownCapabilities?: readonly CapabilityId[]
  readonly capabilities?: readonly CapabilityId[]
  readonly gatewayScopes?: readonly string[]
  readonly native?: NativeCapabilityAdapter
  readonly services?: Readonly<Record<string, unknown>>
}

export interface OpenSquillaAppComposition {
  readonly registry: ContributionRegistrySnapshot
  readonly capabilities: ReadonlySet<CapabilityId>
  readonly gatewayScopes: ReadonlySet<string>
  readonly native: NativeCapabilityAdapter
  readonly disposed: boolean
  getState<T = unknown>(namespace: string): T
  loadPage<TPage = unknown>(pageId: ContributionId): Promise<TPage>
  availability(
    requirements?: ContributionRequirements,
  ): PresentationAvailability
  dispose(): Promise<void>
}
