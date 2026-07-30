import type { ThemeManifest } from '../types'

// Value theme "vapor" -- a dreamy vaporwave world (magenta / cyan / lilac on a
// deep grape ground). Its palette lives in @opensquilla/ui-tokens (contract +
// contrast guards); the world layer -- a scrolling neon perspective grid, a slow aurora drift, and a
// chrome sheen sweeping the headings -- is ./world.css, loaded lazily on
// activation so other-theme users download none of it.
const vapor: ThemeManifest = {
  id: 'vapor',
  name: 'Vapor',
  kind: 'value',
  icon: 'cloud',
  capabilities: { colorScheme: 'dark', userSelectable: true, respectsReducedMotion: true },
  world: { styles: () => import('./world.css') },
}

export default vapor
