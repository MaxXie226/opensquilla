import { execFileSync } from 'node:child_process'
import { readFile, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  apiReportPath,
  readJson,
  repositoryRoot,
  stableJson,
} from './ui_package_api.mjs'

export function parseSemver(value) {
  const match = /^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$/.exec(value)
  if (!match) throw new Error(`Invalid semantic version: ${value}`)
  return {
    raw: value,
    major: Number(match[1]),
    minor: Number(match[2]),
    patch: Number(match[3]),
    prerelease: match[4] ?? null,
  }
}

function compareVersion(left, right) {
  for (const key of ['major', 'minor', 'patch']) {
    if (left[key] !== right[key]) return left[key] < right[key] ? -1 : 1
  }
  if (left.prerelease === right.prerelease) return 0
  if (left.prerelease === null) return 1
  if (right.prerelease === null) return -1
  return left.prerelease.localeCompare(right.prerelease)
}

function requiredBump(oldVersion, newVersion, severity) {
  if (severity === 'unchanged') return compareVersion(newVersion, oldVersion) >= 0
  if (severity === 'additive') {
    return (
      newVersion.major > oldVersion.major
      || (
        newVersion.major === oldVersion.major
        && newVersion.minor > oldVersion.minor
      )
    )
  }
  if (oldVersion.major === 0) {
    return (
      newVersion.major > 0
      || (
        newVersion.major === 0
        && newVersion.minor > oldVersion.minor
      )
    )
  }
  return newVersion.major > oldVersion.major
}

function changesBetween(oldValues, newValues, kind) {
  const previous = new Set(oldValues)
  const current = new Set(newValues)
  return [
    ...[...previous].filter((value) => !current.has(value)).map((value) => ({
      severity: 'breaking',
      code: `${kind}-removed`,
      target: value,
    })),
    ...[...current].filter((value) => !previous.has(value)).map((value) => ({
      severity: 'additive',
      code: `${kind}-added`,
      target: value,
    })),
  ]
}

function compareSignatures(previous, current) {
  const oldValues = new Map(previous.map((entry) => [entry.key, entry.signature]))
  const newValues = new Map(current.map((entry) => [entry.key, entry.signature]))
  const changes = []
  for (const [key, signature] of oldValues) {
    if (!newValues.has(key)) {
      changes.push({ severity: 'breaking', code: 'declaration-removed', target: key })
    } else if (newValues.get(key) !== signature) {
      changes.push({ severity: 'breaking', code: 'declaration-changed', target: key })
    }
  }
  for (const key of newValues.keys()) {
    if (!oldValues.has(key)) {
      changes.push({ severity: 'additive', code: 'declaration-added', target: key })
    }
  }
  return changes
}

function removalAllowed(previous, current, exportName) {
  const deprecation = previous.deprecations.find((entry) => entry.export === exportName)
  if (!deprecation) return false
  return compareVersion(
    parseSemver(current.version),
    parseSemver(deprecation.removeAfter),
  ) >= 0
}

function packageSeverity(changes) {
  if (changes.some((change) => change.severity === 'breaking')) return 'breaking'
  if (changes.some((change) => change.severity === 'additive')) return 'additive'
  return 'unchanged'
}

export function compareApiReports(previous, current) {
  if (!previous) {
    return {
      schemaVersion: 1,
      policy: current.compatibilityPolicy,
      status: 'bootstrap',
      blocking: false,
      packages: current.packages.map((entry) => ({
        name: entry.name,
        previousVersion: null,
        currentVersion: entry.version,
        severity: 'bootstrap',
        changes: [],
        errors: [],
      })),
      errors: [],
    }
  }
  if (previous.schemaVersion !== 1 || current.schemaVersion !== 1) {
    throw new Error('Unsupported UI package API report schema')
  }
  const previousPackages = new Map(previous.packages.map((entry) => [entry.name, entry]))
  const currentPackages = new Map(current.packages.map((entry) => [entry.name, entry]))
  const packageReports = []
  const errors = []

  for (const [name, oldPackage] of previousPackages) {
    const newPackage = currentPackages.get(name)
    if (!newPackage) {
      const message = `${name} was removed from the public release train`
      errors.push(message)
      packageReports.push({
        name,
        previousVersion: oldPackage.version,
        currentVersion: null,
        severity: 'breaking',
        changes: [{ severity: 'breaking', code: 'package-removed', target: name }],
        errors: [message],
      })
      continue
    }
    const changes = [
      ...changesBetween(oldPackage.publicExports, newPackage.publicExports, 'package-path'),
      ...changesBetween(oldPackage.apiExports, newPackage.apiExports, 'api-export'),
      ...changesBetween(oldPackage.runtimeExports, newPackage.runtimeExports, 'runtime-export'),
      ...compareSignatures(oldPackage.declarations, newPackage.declarations),
    ]
    const reportErrors = []
    for (const change of changes) {
      if (
        change.severity === 'breaking'
        && ['api-export-removed', 'runtime-export-removed'].includes(change.code)
        && !removalAllowed(oldPackage, newPackage, change.target)
      ) {
        reportErrors.push(
          `${name} removed ${change.target} without a completed deprecation window`,
        )
      }
    }
    const severity = packageSeverity(changes)
    const oldVersion = parseSemver(oldPackage.version)
    const newVersion = parseSemver(newPackage.version)
    if (compareVersion(newVersion, oldVersion) < 0) {
      reportErrors.push(`${name} version moved backwards`)
    } else if (!requiredBump(oldVersion, newVersion, severity)) {
      reportErrors.push(
        `${name} ${severity} API change requires a larger semantic-version bump`,
      )
    }
    packageReports.push({
      name,
      previousVersion: oldPackage.version,
      currentVersion: newPackage.version,
      severity,
      changes,
      errors: reportErrors,
    })
    errors.push(...reportErrors)
  }

  for (const [name, entry] of currentPackages) {
    if (previousPackages.has(name)) continue
    packageReports.push({
      name,
      previousVersion: null,
      currentVersion: entry.version,
      severity: 'additive',
      changes: [{ severity: 'additive', code: 'package-added', target: name }],
      errors: [],
    })
  }

  const severities = packageReports.map((entry) => entry.severity)
  const status = severities.includes('breaking')
    ? 'breaking'
    : severities.includes('additive')
      ? 'additive'
      : 'unchanged'
  return {
    schemaVersion: 1,
    policy: current.compatibilityPolicy,
    status,
    blocking: errors.length > 0,
    packages: packageReports.sort((left, right) => left.name.localeCompare(right.name)),
    errors,
  }
}

function loadGitReport(ref) {
  try {
    execFileSync(
      'git',
      ['-C', repositoryRoot, 'rev-parse', '--verify', `${ref}^{commit}`],
      { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] },
    )
  } catch {
    throw new Error(`Unable to resolve UI package API baseline ref ${ref}`)
  }
  try {
    const content = execFileSync(
      'git',
      ['-C', repositoryRoot, 'show', `${ref}:contracts/ui-foundation/v1/api-report.json`],
      { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] },
    )
    return JSON.parse(content)
  } catch (error) {
    if (
      error.status === 128
      && /(?:does not exist in|exists on disk, but not in)/.test(error.stderr ?? '')
    ) {
      return null
    }
    throw error
  }
}

function parseArgs(argv) {
  const options = {
    allowBootstrap: false,
    baseline: null,
    baselineRef: null,
    candidate: apiReportPath,
    output: null,
  }
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index]
    if (value === '--allow-bootstrap') options.allowBootstrap = true
    else if (value === '--baseline') options.baseline = path.resolve(argv[++index])
    else if (value === '--baseline-ref') options.baselineRef = argv[++index]
    else if (value === '--candidate') options.candidate = path.resolve(argv[++index])
    else if (value === '--output') options.output = path.resolve(argv[++index])
    else throw new Error(`Unknown argument: ${value}`)
  }
  if (options.baseline && options.baselineRef) {
    throw new Error('--baseline and --baseline-ref are mutually exclusive')
  }
  return options
}

export async function main(argv = process.argv.slice(2)) {
  const options = parseArgs(argv)
  const current = await readJson(options.candidate)
  const previous = options.baseline
    ? await readJson(options.baseline)
    : options.baselineRef
      ? loadGitReport(options.baselineRef)
      : null
  if (!previous && !options.allowBootstrap) {
    throw new Error('A UI package API baseline is required unless --allow-bootstrap is explicit')
  }
  const report = compareApiReports(previous, current)
  if (options.output) await writeFile(options.output, stableJson(report))
  console.log(
    `UI package compatibility: ${report.status}; blocking=${String(report.blocking)}`,
  )
  if (report.blocking) {
    for (const error of report.errors) console.error(`ERROR: ${error}`)
    process.exitCode = 2
  }
  return report
}

if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) {
  main().catch((error) => {
    console.error(`UI package compatibility check failed: ${error.message}`)
    process.exitCode = 1
  })
}
