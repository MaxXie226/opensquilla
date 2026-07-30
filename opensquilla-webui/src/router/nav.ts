import { getPlatform } from '@/platform'
import type { PlatformId } from '@/platform'
import type { IconName } from '@/utils/icons'
import i18n from '@/i18n'
import { createPublicWebUiRegistry } from '@/composition/catalog'
import type {
  ContributionRegistrySnapshot,
  OpenSquillaAppComposition,
} from '@opensquilla/ui-foundation'

type NavigationSlot = 'primary' | 'bottom'

export interface NavigationItem {
  path: string
  title: string
  icon: IconName
}

function routePlatforms(platforms: unknown): PlatformId[] {
  if (!Array.isArray(platforms)) return ['web', 'desktop']
  return platforms.filter((item): item is PlatformId => item === 'web' || item === 'desktop')
}

// Localize a nav row title from its route name token (e.g. `nav.sessions`),
// falling back to the English meta.title literal when no key exists. Called
// inside the useNavigation() computeds, so reading the reactive i18n locale here
// makes the rail/drawer/palette re-render on a language switch.
function navTitle(
  name: string,
  label: string,
  metadata: Readonly<Record<string, unknown>> | undefined,
): string {
  const explicitKey = metadata?.navLabelKey
  if (explicitKey) {
    const translated = i18n.global.t(String(explicitKey))
    if (translated !== explicitKey) return translated
  }
  if (name) {
    const key = `nav.${name}`
    const translated = i18n.global.t(key)
    if (translated !== key) return translated
  }
  return label
}

function navigationRegistry(
  composition: OpenSquillaAppComposition | undefined,
): ContributionRegistrySnapshot {
  return composition?.registry ?? createPublicWebUiRegistry(getPlatform())
}

export function getNavigationItems(
  slot: NavigationSlot,
  composition?: OpenSquillaAppComposition,
): NavigationItem[] {
  const platform = getPlatform()
  const registry = navigationRegistry(composition)
  const routes = new Map(
    registry.routes.map(({ contribution }) => [contribution.id, contribution]),
  )
  const foundationSlot = slot === 'bottom' ? 'footer' : 'primary'
  return registry.navigation
    .filter(({ contribution }) => contribution.slot === foundationSlot)
    .filter(({ contribution }) => (
      routePlatforms(contribution.metadata?.platforms).includes(platform.id)
    ))
    .sort((left, right) => (
      Number(left.contribution.order ?? 0) - Number(right.contribution.order ?? 0)
    ))
    .map(({ contribution }) => {
      const route = routes.get(contribution.routeId)
      if (!route) {
        throw new Error(`Navigation "${contribution.id}" references an unknown route`)
      }
      return {
        path: route.path,
        title: navTitle(route.name, contribution.label, contribution.metadata),
        icon: (contribution.metadata?.icon || 'home') as IconName,
        group: String(contribution.metadata?.group || 'Operate'),
      }
    })
    .map(({ group: _group, ...item }) => item)
}

// The flat, always-visible destinations shared by the desktop rail, mobile
// drawer, and command palette. Chat is excluded because the dedicated New-chat
// action owns that destination.
export function getWorkNavigationSection(
  composition?: OpenSquillaAppComposition,
): NavigationItem[] {
  const registry = navigationRegistry(composition)
  const routes = new Map(
    registry.routes.map(({ contribution }) => [contribution.id, contribution]),
  )
  return registry.navigation
    .filter(({ contribution }) => contribution.slot === 'primary')
    .filter(({ contribution }) => contribution.metadata?.group === 'Work')
    .sort((left, right) => (
      Number(left.contribution.order ?? 0) - Number(right.contribution.order ?? 0)
    ))
    .map(({ contribution }) => {
      const route = routes.get(contribution.routeId)
      if (!route) {
        throw new Error(`Navigation "${contribution.id}" references an unknown route`)
      }
      return {
        path: route.path,
        title: navTitle(route.name, contribution.label, contribution.metadata),
        icon: (contribution.metadata?.icon || 'home') as IconName,
      }
    })
    .filter((item) => item.path !== '/chat')
}
