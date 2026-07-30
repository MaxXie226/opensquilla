import { describe, expect, it } from 'vitest'

import {
  CompositionContractError,
  UI_COMPOSITION_API_VERSION,
  createContributionRegistrar,
  type FeatureModuleContract,
} from '../src/index.js'

function feature(
  id: string,
  patch: Partial<FeatureModuleContract> = {},
): FeatureModuleContract {
  return {
    id,
    apiVersion: UI_COMPOSITION_API_VERSION,
    ...patch,
  }
}

function errorCode(error: unknown): string | undefined {
  return error instanceof CompositionContractError ? error.code : undefined
}

describe('ContributionRegistrar', () => {
  it('resolves dependencies before dependents with deterministic tie-breaking', () => {
    const declarations = [
      feature('community.chat', {
        dependsOn: ['community.core'],
        order: 1,
        contributions: {
          pages: [
            {
              id: 'community.chat.page.main',
              order: 20,
              load: () => 'chat',
            },
          ],
          routes: [
            {
              id: 'community.chat.route.main',
              path: '/chat',
              name: 'chat',
              pageId: 'community.chat.page.main',
            },
          ],
        },
      }),
      feature('community.core', {
        order: 100,
        contributions: {
          pages: [
            {
              id: 'community.core.page.home',
              load: () => 'home',
            },
          ],
          routes: [
            {
              id: 'community.core.route.home',
              path: '/',
              name: 'home',
              pageId: 'community.core.page.home',
            },
          ],
        },
      }),
      feature('community.usage', { order: 10 }),
    ]

    const snapshots = [
      declarations,
      [...declarations].reverse(),
      [declarations[1]!, declarations[2]!, declarations[0]!],
    ].map((input) => createContributionRegistrar(input).finalize())

    for (const snapshot of snapshots) {
      expect(snapshot.features.map(({ id }) => id)).toEqual([
        'community.usage',
        'community.core',
        'community.chat',
      ])
      expect(snapshot.routes.map(({ contributionId }) => contributionId)).toEqual([
        'community.core.route.home',
        'community.chat.route.main',
      ])
    }
  })

  it('rejects duplicate IDs without partially registering a batch', () => {
    const registrar = createContributionRegistrar([feature('community.core')])

    expect(() => registrar.registerMany([
      feature('community.chat'),
      feature('community.chat'),
    ])).toThrowError(expect.objectContaining({ code: 'duplicate_feature_id' }))

    expect(registrar.finalize().features.map(({ id }) => id)).toEqual([
      'community.core',
    ])
  })

  it('rejects missing dependencies and cycles', () => {
    expect(() => createContributionRegistrar([
      feature('community.chat', { dependsOn: ['community.missing'] }),
    ]).finalize()).toThrowError(expect.objectContaining({
      code: 'unknown_feature_dependency',
    }))

    expect(() => createContributionRegistrar([
      feature('community.alpha', { dependsOn: ['community.beta'] }),
      feature('community.beta', { dependsOn: ['community.alpha'] }),
    ]).finalize()).toThrowError(expect.objectContaining({
      code: 'feature_dependency_cycle',
    }))
  })

  it('rejects unsupported versions and malformed namespaces', () => {
    expect(() => createContributionRegistrar([
      {
        id: 'community.core',
        apiVersion: 2,
      } as unknown as FeatureModuleContract,
    ])).toThrowError(expect.objectContaining({
      code: 'unsupported_feature_api_version',
    }))

    expect(() => createContributionRegistrar([
      feature('core'),
    ])).toThrowError(expect.objectContaining({ code: 'invalid_feature_id' }))

    expect(() => createContributionRegistrar([
      feature('community.core', {
        contributions: {
          pages: [{ id: 'other.page.home', load: () => 'home' }],
        },
      }),
    ]).finalize()).toThrowError(expect.objectContaining({
      code: 'invalid_contribution_id',
    }))
  })

  it('rejects route identity collisions and missing page references', () => {
    const page = {
      id: 'community.core.page.home',
      load: () => 'home',
    }
    expect(() => createContributionRegistrar([
      feature('community.core', {
        contributions: {
          pages: [page],
          routes: [
            {
              id: 'community.core.route.home',
              path: '/',
              name: 'home',
              pageId: page.id,
            },
            {
              id: 'community.core.route.alias',
              path: '/',
              name: 'alias',
              pageId: page.id,
            },
          ],
        },
      }),
    ]).finalize()).toThrowError(expect.objectContaining({
      code: 'duplicate_route_path',
    }))

    expect(() => createContributionRegistrar([
      feature('community.core', {
        contributions: {
          routes: [
            {
              id: 'community.core.route.home',
              path: '/',
              name: 'home',
              pageId: 'community.core.page.missing',
            },
          ],
        },
      }),
    ]).finalize()).toThrowError(expect.objectContaining({ code: 'unknown_page' }))
  })

  it('requires an explicit dependency for cross-feature references', () => {
    const core = feature('community.core', {
      contributions: {
        pages: [{ id: 'community.core.page.home', load: () => 'home' }],
      },
    })
    const extension = feature('community.extension', {
      contributions: {
        routes: [{
          id: 'community.extension.route.home',
          path: '/extension',
          name: 'extension',
          pageId: 'community.core.page.home',
        }],
      },
    })

    expect(() => createContributionRegistrar([core, extension]).finalize())
      .toThrowError(expect.objectContaining({
        code: 'undeclared_feature_reference',
      }))

    const snapshot = createContributionRegistrar([
      core,
      { ...extension, dependsOn: ['community.core'] },
    ]).finalize()
    expect(snapshot.routes).toHaveLength(1)
  })

  it('rejects orphan navigation and duplicate or foreign state namespaces', () => {
    expect(() => createContributionRegistrar([
      feature('community.core', {
        contributions: {
          navigation: [{
            id: 'community.core.navigation.home',
            routeId: 'community.core.route.home',
            slot: 'primary',
            label: 'Home',
          }],
        },
      }),
    ]).finalize()).toThrowError(expect.objectContaining({ code: 'unknown_route' }))

    expect(() => createContributionRegistrar([
      feature('community.core', {
        contributions: {
          state: [{
            id: 'community.core.state.main',
            namespace: 'other.state',
            create: () => ({ value: {} }),
          }],
        },
      }),
    ]).finalize()).toThrowError(expect.objectContaining({
      code: 'invalid_state_namespace',
    }))
  })

  it('fails on unknown or missing required capabilities and records optional gaps', () => {
    const declaration = feature('community.core', {
      requiredCapabilities: ['gateway.sessions'],
      optionalCapabilities: ['native.files'],
    })

    expect(() => createContributionRegistrar([declaration]).finalize({
      knownCapabilities: ['gateway.sessions'],
    })).toThrowError(expect.objectContaining({ code: 'unknown_capability' }))

    expect(() => createContributionRegistrar([declaration]).finalize({
      knownCapabilities: ['gateway.sessions', 'native.files'],
    })).toThrowError(expect.objectContaining({
      code: 'missing_required_capability',
    }))

    const snapshot = createContributionRegistrar([declaration]).finalize({
      knownCapabilities: ['gateway.sessions', 'native.files'],
      capabilities: ['gateway.sessions'],
    })
    expect(snapshot.featureCapabilities).toEqual([
      {
        featureId: 'community.core',
        missingOptionalCapabilities: ['native.files'],
      },
    ])
  })

  it('uses structured contract errors', () => {
    try {
      createContributionRegistrar([
        feature('community.core', { dependsOn: ['community.missing'] }),
      ]).finalize()
    } catch (error) {
      expect(errorCode(error)).toBe('unknown_feature_dependency')
    }
  })
})
