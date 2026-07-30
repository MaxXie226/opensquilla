import { describe, expect, it, vi } from 'vitest'

import {
  DERIVED_THEME_TOKEN_NAMES,
  REQUIRED_THEME_TOKEN_NAMES,
  THEME_TOKEN_NAMES,
  resolveThemeTokens,
  themeTokensToCssVariables,
  type ResolvedThemeTokenValues,
  type ThemeTokenDiagnostic,
} from '../src/index.js'

const fallback = Object.fromEntries(
  THEME_TOKEN_NAMES.map((token) => [token, `fallback-${token}`]),
) as ResolvedThemeTokenValues

describe('theme token contract', () => {
  it('keeps required and derived names unique', () => {
    expect(new Set(THEME_TOKEN_NAMES).size).toBe(THEME_TOKEN_NAMES.length)
    expect(
      REQUIRED_THEME_TOKEN_NAMES.some((token) =>
        (DERIVED_THEME_TOKEN_NAMES as readonly string[]).includes(token),
      ),
    ).toBe(false)
  })

  it('reports unknown and missing required tokens while applying fallbacks', () => {
    const diagnostics: ThemeTokenDiagnostic[] = []
    const resolved = resolveThemeTokens(
      { accent: '#f60', privateBrand: '#000' },
      fallback,
      (diagnostic) => diagnostics.push(diagnostic),
    )

    expect(resolved.accent).toBe('#f60')
    expect(resolved.bg).toBe('fallback-bg')
    expect(diagnostics).toContainEqual(
      expect.objectContaining({ code: 'unknown-token', token: 'privateBrand' }),
    )
    expect(diagnostics).toContainEqual(
      expect.objectContaining({
        code: 'missing-required-token',
        token: 'bg',
        fallback: 'fallback-bg',
      }),
    )
  })

  it('maps resolved values to CSS custom properties', () => {
    const report = vi.fn()
    const resolved = resolveThemeTokens(fallback, fallback, report)
    const variables = themeTokensToCssVariables(resolved)

    expect(report).not.toHaveBeenCalled()
    expect(variables['--accent']).toBe('fallback-accent')
    expect(Object.keys(variables)).toHaveLength(THEME_TOKEN_NAMES.length)
  })

  it('fails when the product fallback is incomplete', () => {
    expect(() =>
      resolveThemeTokens(
        {},
        { ...fallback, bg: '' },
        () => undefined,
      ),
    ).toThrow('Theme fallback must define a non-empty value for "bg"')
  })
})
