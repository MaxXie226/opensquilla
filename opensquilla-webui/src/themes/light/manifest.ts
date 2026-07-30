import type { ThemeManifest } from '../types'

// Value theme "light". Applied token values live in @opensquilla/ui-tokens
// (single source of truth, validated by check-theme-contract.mjs).
const light: ThemeManifest = {
  id: 'light',
  name: 'theme.light',
  kind: 'value',
  icon: 'sun',
  capabilities: { colorScheme: 'light', userSelectable: true },
}

export default light
