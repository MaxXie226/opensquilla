import {
  UI_COMPOSITION_API_VERSION,
  createContributionRegistrar,
  type ContributionRegistrySnapshot,
  type FeatureModuleContract,
  type NavigationContribution,
  type PageContribution,
  type RouteContribution,
} from '@opensquilla/ui-foundation'
import type { RouteRecordRaw } from 'vue-router'
import type { Platform, PlatformId } from '@/platform'
import { sharedRoutes } from '@/router/sharedRoutes'
import { webRoutes } from '@/router/webRoutes'
import { desktopRoutes } from '@/router/desktopRoutes'
import {
  createPublicWebUiStateContributions,
  createPublicWebUiWorkbenchStateContribution,
} from './state'
import { PUBLIC_WEB_UI_NATIVE_CAPABILITIES } from './nativeAdapter'

const NotFoundView = () => import('@/views/NotFoundView.vue')

export const PUBLIC_WEB_UI_SHELL_FEATURE_ID = 'opensquilla.webui.shell'

const FEATURE_ORDER = new Map<string, number>([
  [PUBLIC_WEB_UI_SHELL_FEATURE_ID, 0],
  ['opensquilla.webui.chat', 10],
  ['opensquilla.webui.sessions', 20],
  ['opensquilla.webui.observability', 30],
  ['opensquilla.webui.agents', 40],
  ['opensquilla.webui.capabilities', 50],
  ['opensquilla.webui.automation', 60],
  ['opensquilla.webui.editorial', 70],
  ['opensquilla.webui.settings', 80],
  ['opensquilla.webui.workbench', 90],
  ['opensquilla.webui.fallback', 100],
])

interface OrderedRoute {
  readonly record: RouteRecordRaw
  readonly order: number
}

export interface PublicWebUiRedirectRoute {
  readonly record: RouteRecordRaw
  readonly order: number
}

function routePlatforms(record: RouteRecordRaw): readonly PlatformId[] {
  const platforms = record.meta?.platforms
  if (!Array.isArray(platforms)) return ['web', 'desktop']
  return platforms.filter(
    (platform): platform is PlatformId => platform === 'web' || platform === 'desktop',
  )
}

function orderedRoutes(platform: Platform): readonly OrderedRoute[] {
  const records: OrderedRoute[] = sharedRoutes.map((record, order) => ({ record, order }))
  if (platform.capabilities.hasWebConfig) {
    records.push(...webRoutes.map((record, index) => ({
      record,
      order: 100 + index,
    })))
  }
  if (platform.capabilities.hasDesktopOnboarding) {
    records.push(...desktopRoutes.map((record, index) => ({
      record,
      order: 200 + index,
    })))
  }
  records.push({
    record: {
      path: '/:pathMatch(.*)*',
      name: 'not-found',
      component: NotFoundView,
      meta: {
        title: 'Not Found',
        platforms: ['web', 'desktop'],
      },
    },
    order: 1_000,
  })
  return records.filter(({ record }) => routePlatforms(record).includes(platform.id))
}

function featureIdForRoute(name: string): string {
  if (name === 'chat' || name === 'chat-new') return 'opensquilla.webui.chat'
  if (name === 'sessions') return 'opensquilla.webui.sessions'
  if (name === 'overview' || name === 'usage' || name === 'logs') {
    return 'opensquilla.webui.observability'
  }
  if (name === 'agents') return 'opensquilla.webui.agents'
  if (name === 'skills' || name === 'channels') return 'opensquilla.webui.capabilities'
  if (name === 'cron') return 'opensquilla.webui.automation'
  if (name === 'changelog') return 'opensquilla.webui.editorial'
  if (name === 'settings' || name === 'settings-section') {
    return 'opensquilla.webui.settings'
  }
  if (name === 'not-found') return 'opensquilla.webui.fallback'
  throw new Error(`Public WebUI route "${name}" has no feature owner`)
}

function contributionSegment(value: string): string {
  const segment = value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9-]+/g, '-')
    .replace(/^-+|-+$/g, '')
  if (!segment || !/^[a-z]/.test(segment)) {
    throw new Error(`Invalid public WebUI contribution segment "${value}"`)
  }
  return segment
}

function routeName(record: RouteRecordRaw): string {
  if (typeof record.name !== 'string' || !record.name) {
    throw new Error(`Public WebUI page route "${record.path}" requires a string name`)
  }
  return record.name
}

function pageKey(record: RouteRecordRaw): string {
  const viewKey = record.meta?.viewKey
  return contributionSegment(typeof viewKey === 'string' && viewKey
    ? viewKey
    : routeName(record))
}

function pageLoader(record: RouteRecordRaw): PageContribution['load'] {
  const component = record.component
  if (!component) {
    throw new Error(`Public WebUI route "${record.path}" requires a component`)
  }
  if (typeof component !== 'function') return async () => component
  return async () => await (
    component as unknown as () => unknown | Promise<unknown>
  )()
}

function featureFromRoutes(
  featureId: string,
  records: readonly OrderedRoute[],
): FeatureModuleContract {
  const pages = new Map<string, PageContribution>()
  const routes: RouteContribution[] = []
  const navigation: NavigationContribution[] = []

  for (const { record, order } of records) {
    const name = routeName(record)
    const routeSegment = contributionSegment(name)
    const pageSegment = pageKey(record)
    const pageId = `${featureId}.page.${pageSegment}`
    const routeId = `${featureId}.route.${routeSegment}`
    if (!pages.has(pageId)) {
      pages.set(pageId, {
        id: pageId,
        order,
        load: pageLoader(record),
      })
    }
    routes.push({
      id: routeId,
      path: record.path,
      name,
      pageId,
      order,
      metadata: {
        ...(record.meta ?? {}),
        webUiOrder: order,
      },
    })
    if (record.meta?.nav === 'primary' || record.meta?.nav === 'bottom') {
      navigation.push({
        id: `${featureId}.navigation.${routeSegment}`,
        routeId,
        slot: record.meta.nav === 'bottom' ? 'footer' : 'primary',
        label: String(record.meta.title || name),
        order: Number(record.meta.navOrder ?? order),
        metadata: {
          ...(record.meta ?? {}),
          path: record.path,
        },
      })
    }
  }

  return {
    id: featureId,
    apiVersion: UI_COMPOSITION_API_VERSION,
    dependsOn: [PUBLIC_WEB_UI_SHELL_FEATURE_ID],
    order: FEATURE_ORDER.get(featureId) ?? 500,
    contributions: {
      pages: [...pages.values()],
      routes,
      navigation,
    },
  }
}

export function createPublicWebUiFeatures(platform: Platform): readonly FeatureModuleContract[] {
  const pageRoutes = orderedRoutes(platform).filter(({ record }) => Boolean(record.component))
  const recordsByFeature = new Map<string, OrderedRoute[]>()
  for (const route of pageRoutes) {
    const name = routeName(route.record)
    const featureId = featureIdForRoute(name)
    const bucket = recordsByFeature.get(featureId) ?? []
    bucket.push(route)
    recordsByFeature.set(featureId, bucket)
  }

  const features: FeatureModuleContract[] = [{
    id: PUBLIC_WEB_UI_SHELL_FEATURE_ID,
    apiVersion: UI_COMPOSITION_API_VERSION,
    order: FEATURE_ORDER.get(PUBLIC_WEB_UI_SHELL_FEATURE_ID),
    contributions: {
      state: createPublicWebUiStateContributions(),
    },
  }, {
    id: 'opensquilla.webui.workbench',
    apiVersion: UI_COMPOSITION_API_VERSION,
    dependsOn: [PUBLIC_WEB_UI_SHELL_FEATURE_ID],
    order: FEATURE_ORDER.get('opensquilla.webui.workbench'),
    optionalCapabilities: ['opensquilla.host.native-workbench'],
    contributions: {
      state: [createPublicWebUiWorkbenchStateContribution()],
    },
  }]
  for (const [featureId, records] of recordsByFeature) {
    features.push(featureFromRoutes(featureId, records))
  }
  return features
}

export function createPublicWebUiRegistry(
  platform: Platform,
): ContributionRegistrySnapshot {
  return createContributionRegistrar(createPublicWebUiFeatures(platform)).finalize({
    knownCapabilities: PUBLIC_WEB_UI_NATIVE_CAPABILITIES,
  })
}

export function createPublicWebUiRedirectRoutes(
  platform: Platform,
): readonly PublicWebUiRedirectRoute[] {
  return orderedRoutes(platform)
    .filter(({ record }) => !record.component && record.redirect !== undefined)
    .map(({ record, order }) => ({ record, order }))
}
