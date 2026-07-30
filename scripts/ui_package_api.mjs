import { createHash } from 'node:crypto'
import { mkdir, readdir, readFile, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { pathToFileURL, fileURLToPath } from 'node:url'

import ts from 'typescript'

export const repositoryRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '..',
)
export const manifestPath = path.join(
  repositoryRoot,
  'packages',
  'ui-package-manifest.json',
)
export const apiReportPath = path.join(
  repositoryRoot,
  'contracts',
  'ui-foundation',
  'v1',
  'api-report.json',
)

export async function readJson(source) {
  return JSON.parse(await readFile(source, 'utf8'))
}

export function stableJson(value) {
  return `${JSON.stringify(value, null, 2)}\n`
}

function semver(value, label) {
  const match = /^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$/.exec(value)
  if (!match) throw new Error(`${label} must use a valid semantic version`)
  return {
    major: Number(match[1]),
    minor: Number(match[2]),
    patch: Number(match[3]),
    prerelease: match[4] ?? null,
  }
}

function compareSemver(left, right) {
  for (const key of ['major', 'minor', 'patch']) {
    if (left[key] !== right[key]) return left[key] < right[key] ? -1 : 1
  }
  if (left.prerelease === right.prerelease) return 0
  if (left.prerelease === null) return 1
  if (right.prerelease === null) return -1
  return left.prerelease.localeCompare(right.prerelease)
}

function assertStringList(value, label) {
  if (!Array.isArray(value) || value.some((item) => typeof item !== 'string')) {
    throw new Error(`${label} must be an array of strings`)
  }
}

export function validatePackageManifest(manifest) {
  if (manifest.schemaVersion !== 2) {
    throw new Error('UI package manifest schemaVersion must be 2')
  }
  if (manifest.releaseTrain !== 'public-ui-foundation') {
    throw new Error('Unexpected UI package release train')
  }
  if (manifest.versionPolicy !== 'release-groups') {
    throw new Error('UI packages must use release-group versioning')
  }
  if (manifest.compatibilityPolicy !== 'current-and-previous-minor') {
    throw new Error('UI package compatibility window must be current-and-previous-minor')
  }
  if (!Array.isArray(manifest.releaseGroups) || manifest.releaseGroups.length !== 2) {
    throw new Error('UI package manifest must declare the SDK and Foundation release groups')
  }
  if (!Array.isArray(manifest.packages) || manifest.packages.length !== 4) {
    throw new Error('UI package manifest must declare all four public packages')
  }

  const records = new Map()
  for (const record of manifest.packages) {
    if (!record || typeof record !== 'object') throw new Error('Invalid package record')
    if (typeof record.name !== 'string' || !record.name.startsWith('@opensquilla/')) {
      throw new Error('Public package names must use the @opensquilla scope')
    }
    if (records.has(record.name)) throw new Error(`Duplicate package ${record.name}`)
    if (typeof record.path !== 'string' || !record.path.startsWith('packages/')) {
      throw new Error(`${record.name} has an invalid package path`)
    }
    semver(record.version, `${record.name} version`)
    assertStringList(record.publicExports, `${record.name} publicExports`)
    if (!Array.isArray(record.deprecations)) {
      throw new Error(`${record.name} must declare its deprecation inventory`)
    }
    for (const deprecation of record.deprecations) {
      if (
        !deprecation
        || typeof deprecation !== 'object'
        || typeof deprecation.export !== 'string'
        || typeof deprecation.since !== 'string'
        || typeof deprecation.removeAfter !== 'string'
        || typeof deprecation.replacement !== 'string'
      ) {
        throw new Error(`${record.name} contains an invalid deprecation record`)
      }
      const since = semver(deprecation.since, `${record.name} deprecation since`)
      const removeAfter = semver(
        deprecation.removeAfter,
        `${record.name} deprecation removeAfter`,
      )
      if (compareSemver(removeAfter, since) <= 0) {
        throw new Error(
          `${record.name} deprecation removeAfter must be later than since`,
        )
      }
    }
    records.set(record.name, record)
  }

  const assigned = new Set()
  for (const group of manifest.releaseGroups) {
    if (!group || typeof group !== 'object' || typeof group.id !== 'string') {
      throw new Error('Invalid release group')
    }
    if (!['fixed', 'independent'].includes(group.versionPolicy)) {
      throw new Error(`${group.id} has an invalid version policy`)
    }
    assertStringList(group.packages, `${group.id} packages`)
    const versions = new Set()
    for (const packageName of group.packages) {
      const record = records.get(packageName)
      if (!record) throw new Error(`${group.id} references unknown package ${packageName}`)
      if (record.releaseGroup !== group.id) {
        throw new Error(`${packageName} releaseGroup does not match ${group.id}`)
      }
      if (assigned.has(packageName)) {
        throw new Error(`${packageName} belongs to more than one release group`)
      }
      assigned.add(packageName)
      versions.add(record.version)
    }
    if (group.versionPolicy === 'fixed' && versions.size !== 1) {
      throw new Error(`${group.id} packages must share one version`)
    }
  }
  if (assigned.size !== records.size) {
    throw new Error('Every public package must belong to exactly one release group')
  }
  return manifest
}

async function declarationFiles(root) {
  const files = []
  async function visit(directory) {
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      const source = path.join(directory, entry.name)
      if (entry.isDirectory()) await visit(source)
      else if (entry.isFile() && entry.name.endsWith('.d.ts')) files.push(source)
    }
  }
  await visit(root)
  return files.sort()
}

function normalizedNodeText(node, sourceFile) {
  return node.getText(sourceFile).replace(/\s+/g, ' ').trim()
}

export function compareStableText(left, right) {
  if (left === right) return 0
  return left < right ? -1 : 1
}

function declarationKey(node, sourceFile, index) {
  if (node.name && typeof node.name.getText === 'function') {
    return `${ts.SyntaxKind[node.kind]}:${node.name.getText(sourceFile)}`
  }
  if (ts.isVariableStatement(node)) {
    const names = node.declarationList.declarations
      .map((declaration) => declaration.name.getText(sourceFile))
      .join(',')
    return `VariableStatement:${names}`
  }
  if (ts.isExportDeclaration(node)) {
    const target = node.moduleSpecifier?.getText(sourceFile) ?? ''
    const clause = node.exportClause?.getText(sourceFile) ?? '*'
    return `ExportDeclaration:${clause}:${target}`
  }
  if (ts.isExportAssignment(node)) return 'ExportAssignment:default'
  return `${ts.SyntaxKind[node.kind]}:${index}`
}

async function extractDeclarations(packageRoot) {
  const distRoot = path.join(packageRoot, 'dist')
  const signatures = []
  for (const source of await declarationFiles(distRoot)) {
    const relative = path.relative(distRoot, source).split(path.sep).join('/')
    const text = await readFile(source, 'utf8')
    const sourceFile = ts.createSourceFile(
      relative,
      text,
      ts.ScriptTarget.ES2022,
      true,
      ts.ScriptKind.TS,
    )
    sourceFile.statements.forEach((node, index) => {
      const signature = normalizedNodeText(node, sourceFile)
      if (!signature) return
      signatures.push({
        key: `${relative}#${declarationKey(node, sourceFile, index)}`,
        signature,
      })
    })
  }
  return signatures.sort((left, right) => compareStableText(left.key, right.key))
}

function compilerOptions() {
  return {
    allowJs: false,
    module: ts.ModuleKind.NodeNext,
    moduleResolution: ts.ModuleResolutionKind.NodeNext,
    skipLibCheck: true,
    target: ts.ScriptTarget.ES2022,
  }
}

function extractApiExports(entrypoint) {
  const program = ts.createProgram([entrypoint], compilerOptions())
  const checker = program.getTypeChecker()
  const source = program.getSourceFile(entrypoint)
  if (!source) throw new Error(`Unable to load declaration entrypoint ${entrypoint}`)
  const symbol = checker.getSymbolAtLocation(source)
  if (!symbol) throw new Error(`Unable to resolve declaration module ${entrypoint}`)
  return checker.getExportsOfModule(symbol)
    .map((entry) => entry.getName())
    .sort()
}

async function extractRuntimeExports(packageRoot) {
  const entrypoint = path.join(packageRoot, 'dist', 'index.js')
  const module = await import(`${pathToFileURL(entrypoint).href}?api-report=1`)
  return Object.keys(module).sort()
}

function digestSignatures(signatures) {
  const hash = createHash('sha256')
  for (const entry of signatures) {
    hash.update(entry.key)
    hash.update('\0')
    hash.update(entry.signature)
    hash.update('\0')
  }
  return `sha256:${hash.digest('hex')}`
}

export async function generateApiReport(root = repositoryRoot) {
  const manifest = validatePackageManifest(
    await readJson(path.join(root, 'packages', 'ui-package-manifest.json')),
  )
  const packages = []
  for (const record of manifest.packages) {
    const packageRoot = path.join(root, record.path)
    const packageJson = await readJson(path.join(packageRoot, 'package.json'))
    if (packageJson.name !== record.name || packageJson.version !== record.version) {
      throw new Error(`${record.name} manifest and package.json metadata differ`)
    }
    const publicExports = Object.keys(packageJson.exports)
    if (JSON.stringify(publicExports) !== JSON.stringify(record.publicExports)) {
      throw new Error(`${record.name} public exports differ from the package manifest`)
    }
    const signatures = await extractDeclarations(packageRoot)
    const apiExports = extractApiExports(path.join(packageRoot, 'dist', 'index.d.ts'))
    const runtimeExports = await extractRuntimeExports(packageRoot)
    for (const deprecation of record.deprecations) {
      if (!apiExports.includes(deprecation.export)) {
        throw new Error(
          `${record.name} deprecates unknown public export ${deprecation.export}`,
        )
      }
    }
    if (
      record.runtimeExports
      && JSON.stringify([...record.runtimeExports].sort()) !== JSON.stringify(runtimeExports)
    ) {
      throw new Error(`${record.name} runtime exports differ from the package manifest`)
    }
    packages.push({
      name: record.name,
      version: record.version,
      releaseGroup: record.releaseGroup,
      publicExports,
      apiExports,
      runtimeExports,
      declarationsDigest: digestSignatures(signatures),
      declarations: signatures,
      deprecations: record.deprecations,
    })
  }
  return {
    schemaVersion: 1,
    generatedBy: 'scripts/ui_package_api.mjs',
    releaseTrain: manifest.releaseTrain,
    versionPolicy: manifest.versionPolicy,
    compatibilityPolicy: manifest.compatibilityPolicy,
    packages,
  }
}

function parseArgs(argv) {
  const options = { check: false, output: apiReportPath }
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index]
    if (value === '--check') options.check = true
    else if (value === '--write') options.check = false
    else if (value === '--output') options.output = path.resolve(argv[++index])
    else throw new Error(`Unknown argument: ${value}`)
  }
  return options
}

export async function main(argv = process.argv.slice(2)) {
  const options = parseArgs(argv)
  const generated = stableJson(await generateApiReport())
  if (options.check) {
    const checkedIn = await readFile(options.output, 'utf8')
    if (checkedIn !== generated) {
      throw new Error(
        `UI package API report is stale; run node scripts/ui_package_api.mjs --write`,
      )
    }
    console.log(`Verified UI package API report: ${path.relative(repositoryRoot, options.output)}`)
    return
  }
  await mkdir(path.dirname(options.output), { recursive: true })
  await writeFile(options.output, generated)
  console.log(`Wrote UI package API report: ${path.relative(repositoryRoot, options.output)}`)
}

if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) {
  main().catch((error) => {
    console.error(`UI package API report failed: ${error.message}`)
    process.exitCode = 1
  })
}
