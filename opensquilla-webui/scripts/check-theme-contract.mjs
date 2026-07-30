import { readFileSync, readdirSync, existsSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { parseTokenDefinitions } from './lib/css-utils.mjs'

// Contract completeness: every value-theme manifest in the WebUI must resolve
// to a public palette owned by @opensquilla/ui-tokens, and every public palette
// must define the machine-readable required role set.
const root = fileURLToPath(new URL('..', import.meta.url))
const manifestsDir = join(root, 'src', 'themes')
const tokenPackageRoot = join(root, '..', 'packages', 'ui-tokens', 'src')
const themesDir = join(tokenPackageRoot, 'themes')

const contract = JSON.parse(
  readFileSync(join(tokenPackageRoot, 'theme-contract.json'), 'utf8'),
)
const required = contract.required ?? []

const failures = []
let checked = 0

for (const entry of readdirSync(themesDir, { withFileTypes: true })) {
  if (!entry.isDirectory()) continue
  const tokensPath = join(themesDir, entry.name, 'tokens.css')
  if (!existsSync(tokensPath)) continue
  const manifestPath = join(manifestsDir, entry.name, 'manifest.ts')
  if (!existsSync(manifestPath)) {
    failures.push(
      `public palette "${entry.name}" has no WebUI manifest and cannot be selected`,
    )
  } else if (!/kind\s*:\s*['"]value['"]/.test(readFileSync(manifestPath, 'utf8'))) {
    failures.push(
      `public palette "${entry.name}" must map to a kind:'value' WebUI manifest`,
    )
  }
  const css = readFileSync(tokensPath, 'utf8')
  const defined = new Set(parseTokenDefinitions(css).keys())
  const missing = required.filter((role) => !defined.has(role))
  if (missing.length) {
    failures.push(
      `theme "${entry.name}" (packages/ui-tokens/src/themes/${entry.name}/tokens.css) is missing required L1 role(s): ${missing.join(', ')}`,
    )
  }
  checked++
}

if (checked === 0) {
  failures.push('no public value-theme token files found')
}

for (const entry of readdirSync(manifestsDir, { withFileTypes: true })) {
  if (!entry.isDirectory()) continue
  const manifestPath = join(manifestsDir, entry.name, 'manifest.ts')
  if (!existsSync(manifestPath)) continue
  const manifest = readFileSync(manifestPath, 'utf8')
  if (
    /kind\s*:\s*['"]value['"]/.test(manifest)
    && !existsSync(join(themesDir, entry.name, 'tokens.css'))
  ) {
    failures.push(
      `value theme "${entry.name}" has no public @opensquilla/ui-tokens palette`,
    )
  }
}

if (failures.length) {
  console.error('Theme contract check failed:\n' + failures.join('\n'))
  process.exit(1)
}

console.log(
  `Theme contract check passed (${checked} value theme(s) satisfy ${required.length} required roles).`,
)
