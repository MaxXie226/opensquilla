import {
  assertCapabilityId,
  normalizeCapabilities,
} from './capabilities.js'
import { CompositionContractError } from './errors.js'
import {
  UI_COMPOSITION_API_VERSION,
  type CapabilityId,
  type ContributionId,
  type ContributionRequirements,
  type ContributionRegistrySnapshot,
  type FeatureCapabilityStatus,
  type FeatureContributions,
  type FeatureId,
  type FeatureModuleContract,
  type NavigationContribution,
  type RegisteredNavigation,
  type RegisteredPage,
  type RegisteredRoute,
  type RegisteredState,
  type RouteContribution,
} from './types.js'

const NAMESPACED_ID_PATTERN = /^[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*)+$/
const ROUTE_NAME_PATTERN = /^[A-Za-z][A-Za-z0-9._-]*$/

export interface FinalizeContributionRegistryOptions {
  readonly knownCapabilities?: readonly CapabilityId[]
  readonly capabilities?: readonly CapabilityId[]
}

export class ContributionRegistrar {
  readonly #features = new Map<FeatureId, FeatureModuleContract>()

  register(feature: FeatureModuleContract): this {
    this.registerMany([feature])
    return this
  }

  registerMany(features: readonly FeatureModuleContract[]): this {
    const prepared = features.map((feature) => prepareFeature(feature))
    const pending = new Set<FeatureId>()
    for (const feature of prepared) {
      if (this.#features.has(feature.id) || pending.has(feature.id)) {
        throw new CompositionContractError(
          'duplicate_feature_id',
          `Feature "${feature.id}" is already registered`,
          { featureId: feature.id },
        )
      }
      pending.add(feature.id)
    }
    for (const feature of prepared) this.#features.set(feature.id, feature)
    return this
  }

  finalize(
    options: FinalizeContributionRegistryOptions = {},
  ): ContributionRegistrySnapshot {
    const capabilities = normalizeCapabilities(options.capabilities ?? [])
    const knownCapabilities = new Set(normalizeCapabilities([
      ...(options.knownCapabilities ?? []),
      ...capabilities,
    ]))
    const availableCapabilities = new Set(capabilities)
    const features = resolveFeatureOrder([...this.#features.values()])
    const dependencyClosures = buildDependencyClosures(features)
    const featureCapabilities = validateFeatureCapabilities(
      features,
      knownCapabilities,
      availableCapabilities,
    )

    return collectContributions(
      features,
      dependencyClosures,
      knownCapabilities,
      featureCapabilities,
    )
  }
}

export function createContributionRegistrar(
  features: readonly FeatureModuleContract[] = [],
): ContributionRegistrar {
  return new ContributionRegistrar().registerMany(features)
}

function prepareFeature(feature: FeatureModuleContract): FeatureModuleContract {
  assertNamespacedId(feature.id, 'invalid_feature_id', 'Feature')
  if (feature.apiVersion !== UI_COMPOSITION_API_VERSION) {
    throw new CompositionContractError(
      'unsupported_feature_api_version',
      `Feature "${feature.id}" uses unsupported API version ${String(feature.apiVersion)}`,
      {
        featureId: feature.id,
        expected: UI_COMPOSITION_API_VERSION,
        actual: feature.apiVersion,
      },
    )
  }
  assertFiniteOrder(feature.order, `Feature "${feature.id}"`)

  const dependsOn = Object.freeze([...new Set(feature.dependsOn ?? [])].sort())
  for (const dependency of dependsOn) {
    assertNamespacedId(dependency, 'invalid_feature_id', 'Feature dependency')
    if (dependency === feature.id) {
      throw new CompositionContractError(
        'feature_dependency_cycle',
        `Feature "${feature.id}" cannot depend on itself`,
        { cycle: [feature.id, feature.id] },
      )
    }
  }

  return Object.freeze({
    ...feature,
    dependsOn,
    requiredCapabilities: freezeCapabilities(feature.requiredCapabilities),
    optionalCapabilities: freezeCapabilities(feature.optionalCapabilities),
    contributions: freezeContributions(feature.contributions),
  })
}

function freezeCapabilities(
  capabilities: readonly CapabilityId[] | undefined,
): readonly CapabilityId[] {
  return normalizeCapabilities(capabilities ?? [])
}

function freezeContributions(
  contributions: FeatureContributions | undefined,
): FeatureContributions {
  return Object.freeze({
    pages: freezeContributionList(contributions?.pages),
    routes: freezeContributionList(contributions?.routes),
    navigation: freezeContributionList(contributions?.navigation),
    state: freezeContributionList(contributions?.state),
  })
}

function freezeContributionList<T extends object>(
  contributions: readonly T[] | undefined,
): readonly T[] {
  return Object.freeze((contributions ?? []).map((contribution) => {
    const requirements = 'requirements' in contribution
      ? (contribution as { requirements?: ContributionRequirements }).requirements
      : undefined
    const metadata = 'metadata' in contribution
      ? (contribution as { metadata?: Readonly<Record<string, unknown>> }).metadata
      : undefined
    return Object.freeze({
      ...contribution,
      ...(requirements
        ? {
            requirements: Object.freeze({
              ...requirements,
              gatewayScopes: Object.freeze([
                ...new Set(requirements.gatewayScopes ?? []),
              ].sort()),
              capabilities: Object.freeze([
                ...new Set(requirements.capabilities ?? []),
              ].sort()),
            }),
          }
        : {}),
      ...(metadata
        ? { metadata: Object.freeze({ ...metadata }) }
        : {}),
    } as T)
  }))
}

function resolveFeatureOrder(
  features: readonly FeatureModuleContract[],
): readonly FeatureModuleContract[] {
  const byId = new Map(features.map((feature) => [feature.id, feature]))
  const indegree = new Map(features.map((feature) => [feature.id, 0]))
  const dependents = new Map(features.map((feature) => [feature.id, [] as FeatureId[]]))

  for (const feature of features) {
    for (const dependency of feature.dependsOn ?? []) {
      if (!byId.has(dependency)) {
        throw new CompositionContractError(
          'unknown_feature_dependency',
          `Feature "${feature.id}" depends on unknown feature "${dependency}"`,
          { featureId: feature.id, dependency },
        )
      }
      indegree.set(feature.id, (indegree.get(feature.id) ?? 0) + 1)
      dependents.get(dependency)?.push(feature.id)
    }
  }

  const compareIds = (left: FeatureId, right: FeatureId): number => {
    const leftFeature = byId.get(left)
    const rightFeature = byId.get(right)
    return compareOrderAndId(
      leftFeature?.order,
      left,
      rightFeature?.order,
      right,
    )
  }
  const ready = features
    .filter((feature) => indegree.get(feature.id) === 0)
    .map((feature) => feature.id)
    .sort(compareIds)
  const resolved: FeatureModuleContract[] = []

  while (ready.length > 0) {
    const id = ready.shift()
    if (id === undefined) break
    const feature = byId.get(id)
    if (feature === undefined) continue
    resolved.push(feature)
    for (const dependent of (dependents.get(id) ?? []).sort(compareIds)) {
      const next = (indegree.get(dependent) ?? 0) - 1
      indegree.set(dependent, next)
      if (next === 0) {
        ready.push(dependent)
        ready.sort(compareIds)
      }
    }
  }

  if (resolved.length !== features.length) {
    const cycle = features
      .filter((feature) => (indegree.get(feature.id) ?? 0) > 0)
      .map((feature) => feature.id)
      .sort()
    throw new CompositionContractError(
      'feature_dependency_cycle',
      `Feature dependency cycle detected: ${cycle.join(', ')}`,
      { cycle },
    )
  }
  return Object.freeze(resolved)
}

function buildDependencyClosures(
  features: readonly FeatureModuleContract[],
): ReadonlyMap<FeatureId, ReadonlySet<FeatureId>> {
  const closures = new Map<FeatureId, ReadonlySet<FeatureId>>()
  for (const feature of features) {
    const closure = new Set<FeatureId>()
    const visit = (featureId: FeatureId): void => {
      for (const dependency of (
        features.find((candidate) => candidate.id === featureId)?.dependsOn ?? []
      )) {
        if (closure.has(dependency)) continue
        closure.add(dependency)
        visit(dependency)
      }
    }
    visit(feature.id)
    closures.set(feature.id, closure)
  }
  return closures
}

function validateFeatureCapabilities(
  features: readonly FeatureModuleContract[],
  knownCapabilities: ReadonlySet<CapabilityId>,
  availableCapabilities: ReadonlySet<CapabilityId>,
): readonly FeatureCapabilityStatus[] {
  return Object.freeze(features.map((feature) => {
    for (const capability of [
      ...(feature.requiredCapabilities ?? []),
      ...(feature.optionalCapabilities ?? []),
    ]) {
      assertKnownCapability(capability, feature.id, knownCapabilities)
    }
    const missingRequired = (feature.requiredCapabilities ?? [])
      .filter((capability) => !availableCapabilities.has(capability))
    if (missingRequired.length > 0) {
      throw new CompositionContractError(
        'missing_required_capability',
        `Feature "${feature.id}" is missing required capabilities: ${missingRequired.join(', ')}`,
        { featureId: feature.id, capabilities: missingRequired },
      )
    }
    return Object.freeze({
      featureId: feature.id,
      missingOptionalCapabilities: Object.freeze(
        (feature.optionalCapabilities ?? [])
          .filter((capability) => !availableCapabilities.has(capability)),
      ),
    })
  }))
}

function collectContributions(
  features: readonly FeatureModuleContract[],
  dependencyClosures: ReadonlyMap<FeatureId, ReadonlySet<FeatureId>>,
  knownCapabilities: ReadonlySet<CapabilityId>,
  featureCapabilities: readonly FeatureCapabilityStatus[],
): ContributionRegistrySnapshot {
  const contributionOwners = new Map<ContributionId, FeatureId>()
  const pages: RegisteredPage[] = []
  const routes: RegisteredRoute[] = []
  const navigation: RegisteredNavigation[] = []
  const state: RegisteredState[] = []

  for (const feature of features) {
    collectType(feature, feature.contributions?.pages, pages, contributionOwners, 'Page')
    collectType(feature, feature.contributions?.routes, routes, contributionOwners, 'Route')
    collectType(
      feature,
      feature.contributions?.navigation,
      navigation,
      contributionOwners,
      'Navigation',
    )
    collectType(feature, feature.contributions?.state, state, contributionOwners, 'State')
  }

  validatePages(pages)
  validateRoutes(routes, pages, dependencyClosures, knownCapabilities)
  validateNavigation(navigation, routes, dependencyClosures, knownCapabilities)
  validateState(state)

  const compareRegistered = (
    left: RegisteredPage | RegisteredRoute | RegisteredNavigation | RegisteredState,
    right: RegisteredPage | RegisteredRoute | RegisteredNavigation | RegisteredState,
  ): number => {
    const featureOrder = features.findIndex((feature) => feature.id === left.featureId)
      - features.findIndex((feature) => feature.id === right.featureId)
    return featureOrder || compareOrderAndId(
      left.contribution.order,
      left.contributionId,
      right.contribution.order,
      right.contributionId,
    )
  }

  return Object.freeze({
    apiVersion: UI_COMPOSITION_API_VERSION,
    features,
    pages: Object.freeze(pages.sort(compareRegistered)),
    routes: Object.freeze(routes.sort(compareRegistered)),
    navigation: Object.freeze(navigation.sort(compareRegistered)),
    state: Object.freeze(state.sort(compareRegistered)),
    featureCapabilities,
  })
}

function collectType<
  TContribution extends { readonly id: ContributionId; readonly order?: number },
  TRegistered extends {
    readonly featureId: FeatureId
    readonly contributionId: ContributionId
    readonly contribution: TContribution
  },
>(
  feature: FeatureModuleContract,
  contributions: readonly TContribution[] | undefined,
  target: TRegistered[],
  owners: Map<ContributionId, FeatureId>,
  label: string,
): void {
  for (const contribution of contributions ?? []) {
    assertContributionId(feature.id, contribution.id, label)
    assertFiniteOrder(contribution.order, `${label} "${contribution.id}"`)
    const owner = owners.get(contribution.id)
    if (owner !== undefined) {
      throw new CompositionContractError(
        'duplicate_contribution_id',
        `Contribution "${contribution.id}" is already registered by feature "${owner}"`,
        { contributionId: contribution.id, featureId: feature.id, existingFeatureId: owner },
      )
    }
    owners.set(contribution.id, feature.id)
    target.push(Object.freeze({
      featureId: feature.id,
      contributionId: contribution.id,
      contribution,
    }) as TRegistered)
  }
}

function validatePages(pages: readonly RegisteredPage[]): void {
  for (const page of pages) {
    if (typeof page.contribution.load !== 'function') {
      throw new CompositionContractError(
        'unknown_page',
        `Page "${page.contributionId}" must provide a loader`,
        { pageId: page.contributionId },
      )
    }
  }
}

function validateRoutes(
  routes: readonly RegisteredRoute[],
  pages: readonly RegisteredPage[],
  dependencyClosures: ReadonlyMap<FeatureId, ReadonlySet<FeatureId>>,
  knownCapabilities: ReadonlySet<CapabilityId>,
): void {
  const paths = new Map<string, ContributionId>()
  const names = new Map<string, ContributionId>()
  const pagesById = new Map(pages.map((page) => [page.contributionId, page]))
  for (const route of routes) {
    const contribution = route.contribution
    if (
      !contribution.path.startsWith('/')
      || contribution.path.includes('\\')
      || contribution.path.includes('//')
      || contribution.path.includes('?')
      || contribution.path.includes('#')
      || (contribution.path.length > 1 && contribution.path.endsWith('/'))
      || /\s/.test(contribution.path)
    ) {
      throw new CompositionContractError(
        'invalid_route',
        `Route "${contribution.id}" has invalid path "${contribution.path}"`,
        { routeId: contribution.id, path: contribution.path },
      )
    }
    if (!ROUTE_NAME_PATTERN.test(contribution.name)) {
      throw new CompositionContractError(
        'invalid_route',
        `Route "${contribution.id}" has invalid name "${contribution.name}"`,
        { routeId: contribution.id, name: contribution.name },
      )
    }
    assertUniqueValue(
      paths,
      contribution.path,
      contribution.id,
      'duplicate_route_path',
      'path',
    )
    assertUniqueValue(
      names,
      contribution.name,
      contribution.id,
      'duplicate_route_name',
      'name',
    )
    const page = pagesById.get(contribution.pageId)
    if (page === undefined) {
      throw new CompositionContractError(
        'unknown_page',
        `Route "${contribution.id}" references unknown page "${contribution.pageId}"`,
        { routeId: contribution.id, pageId: contribution.pageId },
      )
    }
    assertDeclaredFeatureReference(
      route.featureId,
      page.featureId,
      contribution.id,
      contribution.pageId,
      dependencyClosures,
    )
    validateRequirements(contribution.requirements, route.featureId, knownCapabilities)
  }
}

function validateNavigation(
  navigation: readonly RegisteredNavigation[],
  routes: readonly RegisteredRoute[],
  dependencyClosures: ReadonlyMap<FeatureId, ReadonlySet<FeatureId>>,
  knownCapabilities: ReadonlySet<CapabilityId>,
): void {
  const routesById = new Map(routes.map((route) => [route.contributionId, route]))
  for (const item of navigation) {
    if (!item.contribution.label.trim()) {
      throw new CompositionContractError(
        'unknown_route',
        `Navigation "${item.contributionId}" must provide a label`,
        { navigationId: item.contributionId },
      )
    }
    const route = routesById.get(item.contribution.routeId)
    if (route === undefined) {
      throw new CompositionContractError(
        'unknown_route',
        `Navigation "${item.contributionId}" references unknown route "${item.contribution.routeId}"`,
        {
          navigationId: item.contributionId,
          routeId: item.contribution.routeId,
        },
      )
    }
    assertDeclaredFeatureReference(
      item.featureId,
      route.featureId,
      item.contributionId,
      item.contribution.routeId,
      dependencyClosures,
    )
    validateRequirements(item.contribution.requirements, item.featureId, knownCapabilities)
  }
}

function validateState(state: readonly RegisteredState[]): void {
  const namespaces = new Map<string, ContributionId>()
  for (const entry of state) {
    const namespace = entry.contribution.namespace
    if (
      !NAMESPACED_ID_PATTERN.test(namespace)
      || (namespace !== entry.featureId && !namespace.startsWith(`${entry.featureId}.`))
    ) {
      throw new CompositionContractError(
        'invalid_state_namespace',
        `State namespace "${namespace}" must belong to feature "${entry.featureId}"`,
        {
          featureId: entry.featureId,
          stateId: entry.contributionId,
          namespace,
        },
      )
    }
    const existing = namespaces.get(namespace)
    if (existing !== undefined) {
      throw new CompositionContractError(
        'duplicate_state_namespace',
        `State namespace "${namespace}" is already owned by "${existing}"`,
        {
          namespace,
          stateId: entry.contributionId,
          existingStateId: existing,
        },
      )
    }
    namespaces.set(namespace, entry.contributionId)
    if (typeof entry.contribution.create !== 'function') {
      throw new CompositionContractError(
        'invalid_state_namespace',
        `State "${entry.contributionId}" must provide a factory`,
        { stateId: entry.contributionId },
      )
    }
  }
}

function validateRequirements(
  requirements: RouteContribution['requirements'] | NavigationContribution['requirements'],
  featureId: FeatureId,
  knownCapabilities: ReadonlySet<CapabilityId>,
): void {
  for (const capability of requirements?.capabilities ?? []) {
    assertCapabilityId(capability)
    assertKnownCapability(capability, featureId, knownCapabilities)
  }
  for (const scope of requirements?.gatewayScopes ?? []) {
    if (!scope.trim() || /\s/.test(scope)) {
      throw new CompositionContractError(
        'invalid_route',
        `Gateway scope "${scope}" declared by feature "${featureId}" is invalid`,
        { featureId, scope },
      )
    }
  }
}

function assertKnownCapability(
  capability: CapabilityId,
  featureId: FeatureId,
  knownCapabilities: ReadonlySet<CapabilityId>,
): void {
  assertCapabilityId(capability)
  if (!knownCapabilities.has(capability)) {
    throw new CompositionContractError(
      'unknown_capability',
      `Feature "${featureId}" declares unknown capability "${capability}"`,
      { featureId, capability },
    )
  }
}

function assertDeclaredFeatureReference(
  sourceFeatureId: FeatureId,
  targetFeatureId: FeatureId,
  contributionId: ContributionId,
  targetId: ContributionId,
  dependencyClosures: ReadonlyMap<FeatureId, ReadonlySet<FeatureId>>,
): void {
  if (
    sourceFeatureId !== targetFeatureId
    && !dependencyClosures.get(sourceFeatureId)?.has(targetFeatureId)
  ) {
    throw new CompositionContractError(
      'undeclared_feature_reference',
      `Contribution "${contributionId}" references "${targetId}" without depending on feature "${targetFeatureId}"`,
      { sourceFeatureId, targetFeatureId, contributionId, targetId },
    )
  }
}

function assertUniqueValue(
  values: Map<string, ContributionId>,
  value: string,
  contributionId: ContributionId,
  code: 'duplicate_route_path' | 'duplicate_route_name',
  label: 'path' | 'name',
): void {
  const existing = values.get(value)
  if (existing !== undefined) {
    throw new CompositionContractError(
      code,
      `Route ${label} "${value}" is already owned by "${existing}"`,
      { [label]: value, routeId: contributionId, existingRouteId: existing },
    )
  }
  values.set(value, contributionId)
}

function assertContributionId(
  featureId: FeatureId,
  contributionId: ContributionId,
  label: string,
): void {
  if (
    !NAMESPACED_ID_PATTERN.test(contributionId)
    || !contributionId.startsWith(`${featureId}.`)
  ) {
    throw new CompositionContractError(
      'invalid_contribution_id',
      `${label} ID "${contributionId}" must be namespaced under feature "${featureId}"`,
      { featureId, contributionId },
    )
  }
}

function assertNamespacedId(
  value: string,
  code: 'invalid_feature_id',
  label: string,
): void {
  if (!NAMESPACED_ID_PATTERN.test(value)) {
    throw new CompositionContractError(
      code,
      `${label} "${value}" must be a lowercase namespaced identifier`,
      { value },
    )
  }
}

function assertFiniteOrder(order: number | undefined, label: string): void {
  if (order !== undefined && (!Number.isSafeInteger(order) || order < 0)) {
    throw new CompositionContractError(
      'invalid_contribution_id',
      `${label} order must be a non-negative safe integer`,
      { order },
    )
  }
}

function compareOrderAndId(
  leftOrder: number | undefined,
  leftId: string,
  rightOrder: number | undefined,
  rightId: string,
): number {
  return (leftOrder ?? 0) - (rightOrder ?? 0) || leftId.localeCompare(rightId)
}
