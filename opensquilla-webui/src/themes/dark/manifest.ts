import type { ThemeManifest } from '../types'

// Value theme "dark" — the default ground. Its applied token values live in
// @opensquilla/ui-tokens, the source validated by check-theme-contract.mjs;
// they are not duplicated here.
const dark: ThemeManifest = {
  id: 'dark',
  name: 'theme.dark',
  kind: 'value',
  icon: 'moon',
  capabilities: { colorScheme: 'dark', userSelectable: true },
}

export default dark
