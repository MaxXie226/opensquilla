import assert from 'node:assert/strict'
import { readFile, readdir } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const packageRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const sourceRoot = path.join(packageRoot, 'src')
const forbidden = [
  ['application alias', /from\s+['"]@\//],
  ['Pinia', /from\s+['"]pinia['"]/],
  ['Vue Router', /from\s+['"]vue-router['"]/],
  ['native bridge', /opensquillaDesktop|electron|ipcRenderer/i],
  ['ambient persistent storage', /\b(?:localStorage|sessionStorage)\b/],
  ['private product manifest', /ProductManifest|FeatureDecision|entitlement/i],
]

async function files(directory) {
  const entries = await readdir(directory, { withFileTypes: true })
  const nested = await Promise.all(entries.map(async (entry) => {
    const fullPath = path.join(directory, entry.name)
    return entry.isDirectory() ? files(fullPath) : [fullPath]
  }))
  return nested.flat()
}

const sourceFiles = (await files(sourceRoot)).filter((file) => file.endsWith('.ts'))
for (const file of sourceFiles) {
  const source = await readFile(file, 'utf8')
  for (const [label, pattern] of forbidden) {
    assert.ok(
      !pattern.test(source),
      `${path.relative(packageRoot, file)} crosses the UI Foundation boundary: ${label}`,
    )
  }
}

console.log(`Verified ${sourceFiles.length} UI Foundation source files`)
