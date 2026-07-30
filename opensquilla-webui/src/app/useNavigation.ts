import { computed, inject } from 'vue'
import { getNavigationItems, getWorkNavigationSection } from '@/router/nav'
import { PUBLIC_WEB_UI_COMPOSITION_KEY } from '@/composition/root'

export function useNavigation() {
  const composition = inject(PUBLIC_WEB_UI_COMPOSITION_KEY, undefined)
  const bottomRoutes = computed(() => getNavigationItems('bottom', composition))
  // Flat primary rows, single-sourced from route metadata so the desktop rail,
  // mobile drawer, and command palette stay in the same order.
  const workNav = computed(() => getWorkNavigationSection(composition))

  return {
    bottomRoutes,
    workNav,
  }
}
