import { createRouter, createWebHistory } from 'vue-router'
import type {
  RouteComponent,
  RouteLocationNormalized,
  RouteRecordRaw,
  Router,
} from 'vue-router'
import type { OpenSquillaAppComposition } from '@opensquilla/ui-foundation'
import { getPlatform, type Platform } from '@/platform'
import { createPublicWebUiRedirectRoutes } from '@/composition/catalog'
import i18n from '@/i18n'
import { captureContentScroll, contentScrollBehavior } from './scrollMemory'
import { saveLastRoute } from './lastRoute'
import { legacyChannelHashRedirect } from './legacyRedirects'
import {
  clearPrimedSessionBootstrapAdmission,
  primeSessionBootstrapAdmission,
} from '@/composables/chat/sessionBootstrapAdmission'

function basePath(): string {
  const el = document.getElementById('opensquilla-data')
  const raw = el?.dataset.basePath || '/control'
  return raw.endsWith('/') ? raw : raw + '/'
}

function webUiOrder(metadata: Readonly<Record<string, unknown>> | undefined): number {
  return typeof metadata?.webUiOrder === 'number' ? metadata.webUiOrder : 500
}

export function createPublicWebUiRoutes(
  composition: OpenSquillaAppComposition,
  platform: Platform = getPlatform(),
): RouteRecordRaw[] {
  const pageLoaders = new Map<string, () => Promise<RouteComponent>>()
  const registeredRoutes = composition.registry.routes
    .filter(({ contribution }) => composition.availability(
      contribution.requirements,
    ).available)
    .map(({ contribution }) => {
      let component = pageLoaders.get(contribution.pageId)
      if (!component) {
        component = () => composition.loadPage<RouteComponent>(contribution.pageId)
        pageLoaders.set(contribution.pageId, component)
      }
      const { webUiOrder: _webUiOrder, ...meta } = contribution.metadata ?? {}
      return {
        order: webUiOrder(contribution.metadata),
        record: {
          path: contribution.path,
          name: contribution.name,
          component,
          meta,
        } satisfies RouteRecordRaw,
      }
    })
  const redirects = createPublicWebUiRedirectRoutes(platform).map(({ record, order }) => ({
    order,
    record,
  }))
  return [...registeredRoutes, ...redirects]
    .sort((left, right) => left.order - right.order)
    .map(({ record }) => record)
}

export function createPublicWebUiRouter(
  composition: OpenSquillaAppComposition,
  platform: Platform = getPlatform(),
): Router {
  const router = createRouter({
    history: createWebHistory(basePath()),
    routes: createPublicWebUiRoutes(composition, platform),
    scrollBehavior: contentScrollBehavior,
  })
  installRouterLifecycle(router)
  return router
}

function isChatRoutePath(path: string): boolean {
  return path === '/chat' || path === '/chat/new'
}

// ChatView is lazy-loaded, while App/Sidebar mounted hooks can run as soon as
// the root shell exists. Prime a singleton admission hold before resolving the
// lazy route so optional shell RPCs cannot enter the Gateway's serialized
// dispatcher ahead of session subscribe/snapshot/history. Query-only chat
// navigation reuses the mounted view and owns its hold through the coordinator.
function installRouterLifecycle(router: Router): void {
  router.beforeEach((to, from) => {
    const enteringChat = isChatRoutePath(to.path) && !isChatRoutePath(from.path)
    if (enteringChat) {
      primeSessionBootstrapAdmission()
    } else if (!isChatRoutePath(to.path)) {
      clearPrimedSessionBootstrapAdmission()
    }
  })

  // Capture the leaving route's content scroll offset so back/forward can restore it.
  router.beforeEach((_to, from) => {
    captureContentScroll(from)
  })

  // Stale channel-setup bookmarks (#channel-… hashes) land on the workspace.
  router.beforeEach((to) => legacyChannelHashRedirect(to) ?? true)

  router.afterEach((to, _from, failure) => {
    if (failure || !isChatRoutePath(to.path)) {
      clearPrimedSessionBootstrapAdmission()
    }
    document.title = `${routeTitle(to)} — OpenSquilla`
    // Remember the current view (path only) so the next launch reopens here.
    saveLastRoute(to.path)
  })

  router.onError(() => {
    clearPrimedSessionBootstrapAdmission()
  })
}

// Localize the document title from the route name token (e.g. `nav.sessions`),
// falling back to the English meta.title. `applyRouteTitle` is also re-run when
// the locale changes (App.vue watches the store) since afterEach does not
// re-fire without a navigation.
export function routeTitle(route: RouteLocationNormalized): string {
  const explicitKey = route.meta?.titleKey
  if (explicitKey) {
    const translated = i18n.global.t(explicitKey)
    if (translated !== explicitKey) return translated
  }
  const name = typeof route.name === 'string' ? route.name : ''
  if (name) {
    const key = `nav.${name}`
    const translated = i18n.global.t(key)
    if (translated !== key) return translated
  }
  return (route.meta?.title as string) || 'OpenSquilla'
}
