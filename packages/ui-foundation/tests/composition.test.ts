import { describe, expect, it } from 'vitest'

import {
  CompositionContractError,
  NATIVE_CAPABILITY_API_VERSION,
  UI_COMPOSITION_API_VERSION,
  createNativeCapabilityAdapter,
  createOpenSquillaApp,
  createWebNativeCapabilityAdapter,
  type FeatureModuleContract,
  type NativeCapabilityAdapter,
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

function counterFeature(
  id: string,
  initial: number,
  disposed: string[],
): FeatureModuleContract {
  return feature(id, {
    contributions: {
      state: [{
        id: `${id}.state.counter`,
        namespace: `${id}.counter`,
        create: () => ({
          value: { count: initial },
          dispose: () => {
            disposed.push(id)
          },
        }),
      }],
      pages: [{
        id: `${id}.page.counter`,
        load: ({ getOwnState }) => getOwnState(`${id}.counter`),
      }],
      routes: [{
        id: `${id}.route.counter`,
        path: `/${id.replace('.', '-')}`,
        name: `${id}.counter`,
        pageId: `${id}.page.counter`,
      }],
      navigation: [{
        id: `${id}.navigation.counter`,
        routeId: `${id}.route.counter`,
        slot: 'primary',
        label: id,
      }],
    },
  })
}

describe('createOpenSquillaApp', () => {
  it('builds isolated compositions with different page sets', async () => {
    const firstDisposed: string[] = []
    const secondDisposed: string[] = []
    const first = await createOpenSquillaApp({
      features: [counterFeature('community.alpha', 1, firstDisposed)],
    })
    const second = await createOpenSquillaApp({
      features: [counterFeature('community.beta', 2, secondDisposed)],
    })

    expect(first.registry.routes.map(({ contributionId }) => contributionId)).toEqual([
      'community.alpha.route.counter',
    ])
    expect(second.registry.routes.map(({ contributionId }) => contributionId)).toEqual([
      'community.beta.route.counter',
    ])
    expect(first.registry.navigation.map(({ contributionId }) => contributionId)).toEqual([
      'community.alpha.navigation.counter',
    ])
    expect(second.registry.navigation.map(({ contributionId }) => contributionId)).toEqual([
      'community.beta.navigation.counter',
    ])
    expect(await first.loadPage<{ count: number }>('community.alpha.page.counter'))
      .toEqual({ count: 1 })
    expect(await second.loadPage<{ count: number }>('community.beta.page.counter'))
      .toEqual({ count: 2 })

    first.getState<{ count: number }>('community.alpha.counter').count = 7
    expect(second.getState<{ count: number }>('community.beta.counter').count).toBe(2)

    await first.dispose()
    await second.dispose()
    expect(firstDisposed).toEqual(['community.alpha'])
    expect(secondDisposed).toEqual(['community.beta'])
  })

  it('prevents a page from reading another feature state', async () => {
    const target = counterFeature('community.target', 1, [])
    const source = feature('community.source', {
      dependsOn: ['community.target'],
      contributions: {
        pages: [{
          id: 'community.source.page.main',
          load: ({ getOwnState }) => getOwnState('community.target.counter'),
        }],
      },
    })
    const app = await createOpenSquillaApp({ features: [source, target] })

    await expect(app.loadPage('community.source.page.main')).rejects.toMatchObject({
      code: 'unknown_state_namespace',
    })
    await app.dispose()
  })

  it('disposes state in reverse initialization order and is idempotent', async () => {
    const disposed: string[] = []
    const app = await createOpenSquillaApp({
      features: [
        counterFeature('community.first', 1, disposed),
        counterFeature('community.second', 2, disposed),
      ],
    })

    await app.dispose()
    await app.dispose()

    expect(disposed).toEqual(['community.second', 'community.first'])
    expect(() => app.getState('community.first.counter')).toThrowError(
      expect.objectContaining({ code: 'composition_disposed' }),
    )
  })

  it('rolls back initialized state when a later factory fails', async () => {
    const disposed: string[] = []
    await expect(createOpenSquillaApp({
      features: [
        counterFeature('community.first', 1, disposed),
        feature('community.second', {
          contributions: {
            state: [{
              id: 'community.second.state.fail',
              namespace: 'community.second.fail',
              create: () => {
                throw new Error('synthetic failure')
              },
            }],
          },
        }),
      ],
    })).rejects.toMatchObject({
      code: 'state_initialization_failed',
    })
    expect(disposed).toEqual(['community.first'])
  })

  it('reports optional presentation gaps without treating them as authorization', async () => {
    const app = await createOpenSquillaApp({
      features: [],
      knownCapabilities: ['gateway.sessions', 'native.files'],
      capabilities: ['gateway.sessions'],
      gatewayScopes: ['sessions.read'],
    })

    expect(app.availability({
      capabilities: ['native.files'],
      gatewayScopes: ['sessions.read', 'sessions.write'],
    })).toEqual({
      available: false,
      reasons: ['missing_capability', 'missing_gateway_scope'],
      missingCapabilities: ['native.files'],
      missingGatewayScopes: ['sessions.write'],
    })
    await app.dispose()
  })

  it('uses a structured unsupported adapter in browser hosts', async () => {
    const native = createWebNativeCapabilityAdapter()
    expect(native.apiVersion).toBe(NATIVE_CAPABILITY_API_VERSION)
    expect(await native.invoke({
      capability: 'native.files',
      request: { path: '/tmp/example' },
    })).toEqual({
      ok: false,
      error: {
        code: 'unsupported',
        capability: 'native.files',
        message: 'Capability "native.files" is not supported by this host',
      },
    })
  })

  it('contains host failures and rejects unsupported operations before dispatch', async () => {
    let calls = 0
    const native = createNativeCapabilityAdapter({
      bridgeVersion: '1.2.0',
      capabilities: ['native.files'],
      async invoke() {
        calls += 1
        throw new Error('host failed')
      },
    })

    await expect(native.invoke({ capability: 'native.files' })).resolves.toMatchObject({
      ok: false,
      error: { code: 'failed', capability: 'native.files' },
    })
    await expect(native.invoke({ capability: 'native.updates' })).resolves.toMatchObject({
      ok: false,
      error: { code: 'unsupported', capability: 'native.updates' },
    })
    expect(calls).toBe(1)
  })

  it('negotiates required native capabilities before state initialization', async () => {
    let initialized = false
    const declaration = feature('community.files', {
      requiredCapabilities: ['native.files'],
      contributions: {
        state: [{
          id: 'community.files.state.main',
          namespace: 'community.files.main',
          create: () => {
            initialized = true
            return { value: {} }
          },
        }],
      },
    })

    await expect(createOpenSquillaApp({
      features: [declaration],
      knownCapabilities: ['native.files'],
    })).rejects.toMatchObject({ code: 'missing_required_capability' })
    expect(initialized).toBe(false)

    const native = createNativeCapabilityAdapter({
      bridgeVersion: '1.0.0',
      capabilities: ['native.files'],
      async invoke() {
        return { ok: true, value: undefined }
      },
    })
    const app = await createOpenSquillaApp({
      features: [declaration],
      native,
    })
    expect(initialized).toBe(true)
    await app.dispose()
  })

  it('rejects an incompatible native adapter before initializing state', async () => {
    const native = {
      apiVersion: 2,
      bridgeVersion: '2.0.0',
      capabilities: [],
      invoke: async () => ({ ok: false as const, error: {
        code: 'unsupported' as const,
        capability: 'native.files',
        message: 'unsupported',
      } }),
    } as unknown as NativeCapabilityAdapter

    await expect(createOpenSquillaApp({
      features: [],
      native,
    })).rejects.toMatchObject({
      code: 'unsupported_native_adapter_version',
    })
  })

  it('exposes structured unknown page and state failures', async () => {
    const app = await createOpenSquillaApp({ features: [] })

    expect(() => app.getState('community.none')).toThrow(CompositionContractError)
    await expect(app.loadPage('community.none.page')).rejects.toMatchObject({
      code: 'unknown_page_id',
    })
    await app.dispose()
  })
})
