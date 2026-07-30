import assert from 'node:assert/strict'
import { readFile, readdir } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const packageRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const repositoryRoot = path.resolve(packageRoot, '..', '..')
const sourceRoot = path.join(packageRoot, 'src')
const tokenSourceRoot = path.join(repositoryRoot, 'packages', 'ui-tokens', 'src')

async function walk(root) {
  const entries = await readdir(root, { withFileTypes: true })
  const files = []
  for (const entry of entries) {
    const target = path.join(root, entry.name)
    if (entry.isDirectory()) files.push(...await walk(target))
    else files.push(target)
  }
  return files
}

const sourceFiles = (await walk(sourceRoot)).filter((file) =>
  /\.(?:ts|vue|css)$/.test(file),
)
const sources = await Promise.all(
  sourceFiles.map(async (file) => ({
    file,
    text: await readFile(file, 'utf8'),
  })),
)

const forbidden = [
  ['Vue Router', /(?:from\s+['"]vue-router['"]|useRouter\s*\()/],
  ['Pinia', /(?:from\s+['"]pinia['"]|defineStore\s*\()/],
  ['application alias', /from\s+['"]@\//],
  ['Gateway client', /(?:GatewayClient|RpcClient|\/ws\b|\/api\b)/],
  ['native bridge', /(?:opensquillaDesktop|electron|nativeBridge)/i],
  ['persistent storage', /(?:localStorage|sessionStorage|indexedDB)/],
]
const failures = []
for (const { file, text } of sources) {
  for (const [label, pattern] of forbidden) {
    if (pattern.test(text)) {
      failures.push(
        `${path.relative(repositoryRoot, file)}: forbidden ${label} dependency`,
      )
    }
  }
  if (/\b(?:rgb|hsl)a?\s*\(|#[\da-f]{3,8}\b/i.test(text)) {
    failures.push(
      `${path.relative(repositoryRoot, file)}: raw color literal bypasses semantic UI tokens`,
    )
  }
}

const tokenFiles = (await walk(tokenSourceRoot)).filter((file) =>
  /\.(?:css|ts)$/.test(file),
)
const definedTokens = new Set()
for (const file of tokenFiles) {
  const text = await readFile(file, 'utf8')
  for (const match of text.matchAll(/--([\w-]+)\s*:/g)) definedTokens.add(match[1])
  for (const match of text.matchAll(/['"]([\w-]+)['"]/g)) {
    if (/^(?:bg|text|border|card|hairline|accent|ok|warn|danger|info|queued|syntax|elev|shadow|scrim|grain|msg|surface|color|sidebar)-?/.test(match[1])) {
      definedTokens.add(match[1])
    }
  }
}

for (const { file, text } of sources) {
  for (const match of text.matchAll(/var\(\s*--([\w-]+)/g)) {
    if (!definedTokens.has(match[1])) {
      failures.push(
        `${path.relative(repositoryRoot, file)}: undefined token --${match[1]}`,
      )
    }
  }
}

const packageJson = JSON.parse(
  await readFile(path.join(packageRoot, 'package.json'), 'utf8'),
)
assert.deepEqual(
  Object.keys(packageJson.peerDependencies ?? {}),
  ['vue'],
  'ui-primitives may expose only Vue as a runtime peer',
)
assert.deepEqual(
  Object.keys(packageJson.dependencies ?? {}),
  ['@opensquilla/ui-tokens'],
  'ui-primitives may depend only on public UI tokens',
)

if (failures.length) {
  throw new Error(`UI primitive boundary check failed:\n${failures.join('\n')}`)
}

console.log(
  `Verified ${sourceFiles.length} primitive source files against application and token boundaries`,
)
