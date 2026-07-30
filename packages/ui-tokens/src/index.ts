export const REQUIRED_THEME_TOKEN_NAMES = [
  'bg',
  'bg-surface',
  'bg-surface-2',
  'bg-elevated',
  'bg-hover',
  'text',
  'text-muted',
  'text-dim',
  'border',
  'border-strong',
  'border-focus',
  'card',
  'hairline',
  'accent',
  'accent-hover',
  'accent-deep',
  'accent-secondary',
  'accent-foreground',
  'ok',
  'warn',
  'danger',
  'info',
  'queued',
  'syntax-comment',
  'syntax-keyword',
  'syntax-string',
  'syntax-literal',
  'syntax-title',
  'syntax-attr',
] as const

export const DERIVED_THEME_TOKEN_NAMES = [
  'elev-highlight',
  'elev-1',
  'elev-1-hover',
  'elev-2',
  'elev-3',
  'ok-fill',
  'warn-fill',
  'danger-fill',
  'info-fill',
  'queued-fill',
  'chart-2',
  'shadow',
  'shadow-color',
  'scrim',
  'grain-opacity',
  'msg-bubble',
  'msg-obj-border',
  'surface-2',
  'surface-3',
  'color-green',
  'color-red',
  'color-blue',
  'text-secondary',
  'sidebar-bg',
  'sidebar-control-bg',
  'sidebar-control-hover',
  'sidebar-item-hover',
  'sidebar-item-active',
  'sidebar-text-strong',
  'sidebar-text',
  'sidebar-text-soft',
  'sidebar-border',
] as const

export const THEME_TOKEN_NAMES = [
  ...REQUIRED_THEME_TOKEN_NAMES,
  ...DERIVED_THEME_TOKEN_NAMES,
] as const

export const PUBLIC_THEME_IDS = [
  'arctic',
  'crt-green',
  'dark',
  'ember',
  'light',
  'miami',
  'synthwave',
  'terminal',
  'vapor',
] as const

export type RequiredThemeTokenName = (typeof REQUIRED_THEME_TOKEN_NAMES)[number]
export type DerivedThemeTokenName = (typeof DERIVED_THEME_TOKEN_NAMES)[number]
export type ThemeTokenName = (typeof THEME_TOKEN_NAMES)[number]
export type PublicThemeId = (typeof PUBLIC_THEME_IDS)[number]
export type ThemeTokenValues = Partial<Record<ThemeTokenName, string>>
export type ResolvedThemeTokenValues = Record<ThemeTokenName, string>
export type ThemeCssVariableName = `--${ThemeTokenName}`

export type ThemeTokenDiagnosticCode =
  | 'missing-required-token'
  | 'unknown-token'

export interface ThemeTokenDiagnostic {
  code: ThemeTokenDiagnosticCode
  token: string
  message: string
  fallback?: string
}

export type ThemeTokenDiagnosticReporter = (
  diagnostic: ThemeTokenDiagnostic,
) => void

const THEME_TOKEN_SET: ReadonlySet<string> = new Set(THEME_TOKEN_NAMES)
const REQUIRED_THEME_TOKEN_SET: ReadonlySet<string> = new Set(
  REQUIRED_THEME_TOKEN_NAMES,
)

function defaultReporter(diagnostic: ThemeTokenDiagnostic): void {
  console.warn(`[ui-tokens] ${diagnostic.message}`)
}

function requireTokenValue(
  token: ThemeTokenName,
  candidate: Readonly<Record<string, string | undefined>>,
  fallback: Readonly<Record<ThemeTokenName, string>>,
  report: ThemeTokenDiagnosticReporter,
): string {
  const value = candidate[token]?.trim()
  if (value) return value

  const fallbackValue = fallback[token]?.trim()
  if (!fallbackValue) {
    throw new TypeError(`Theme fallback must define a non-empty value for "${token}".`)
  }
  if (REQUIRED_THEME_TOKEN_SET.has(token)) {
    report({
      code: 'missing-required-token',
      token,
      fallback: fallbackValue,
      message: `Theme token "${token}" is missing; using the diagnostic fallback.`,
    })
  }
  return fallbackValue
}

/**
 * Resolve an untrusted or partial theme against a complete, product-owned
 * fallback. Unknown names are ignored with a diagnostic; missing required
 * names remain usable through the supplied fallback.
 */
export function resolveThemeTokens(
  candidate: Readonly<Record<string, string | undefined>>,
  fallback: Readonly<Record<ThemeTokenName, string>>,
  report: ThemeTokenDiagnosticReporter = defaultReporter,
): ResolvedThemeTokenValues {
  for (const token of Object.keys(candidate)) {
    if (THEME_TOKEN_SET.has(token)) continue
    report({
      code: 'unknown-token',
      token,
      message: `Unknown theme token "${token}" was ignored.`,
    })
  }

  return Object.fromEntries(
    THEME_TOKEN_NAMES.map((token) => [
      token,
      requireTokenValue(token, candidate, fallback, report),
    ]),
  ) as ResolvedThemeTokenValues
}

export function themeTokensToCssVariables(
  tokens: Readonly<ResolvedThemeTokenValues>,
): Record<ThemeCssVariableName, string> {
  return Object.fromEntries(
    THEME_TOKEN_NAMES.map((token) => [`--${token}`, tokens[token]]),
  ) as Record<ThemeCssVariableName, string>
}
