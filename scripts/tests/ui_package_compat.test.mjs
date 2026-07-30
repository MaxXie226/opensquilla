import assert from 'node:assert/strict'
import test from 'node:test'

import {
  compareApiReports,
  parseSemver,
} from '../check_ui_package_compat.mjs'

function packageReport(overrides = {}) {
  return {
    name: '@opensquilla/ui-foundation',
    version: '0.1.0',
    releaseGroup: 'ui-foundation',
    publicExports: ['.'],
    apiExports: ['createOpenSquillaApp'],
    runtimeExports: ['createOpenSquillaApp'],
    declarationsDigest: 'sha256:fixture',
    declarations: [
      {
        key: 'index.d.ts#FunctionDeclaration:createOpenSquillaApp',
        signature: 'export declare function createOpenSquillaApp(): unknown;',
      },
    ],
    deprecations: [],
    ...overrides,
  }
}

function report(entry = packageReport()) {
  return {
    schemaVersion: 1,
    generatedBy: 'fixture',
    releaseTrain: 'public-ui-foundation',
    versionPolicy: 'release-groups',
    compatibilityPolicy: 'current-and-previous-minor',
    packages: [entry],
  }
}

test('bootstrap is explicit and non-blocking', () => {
  const result = compareApiReports(null, report())
  assert.equal(result.status, 'bootstrap')
  assert.equal(result.blocking, false)
})

test('unchanged APIs may retain the same version', () => {
  const result = compareApiReports(report(), report())
  assert.equal(result.status, 'unchanged')
  assert.equal(result.blocking, false)
})

test('additive APIs require a minor version bump', () => {
  const current = packageReport({
    apiExports: ['createOpenSquillaApp', 'createProductComposition'],
    runtimeExports: ['createOpenSquillaApp', 'createProductComposition'],
    declarations: [
      ...packageReport().declarations,
      {
        key: 'index.d.ts#FunctionDeclaration:createProductComposition',
        signature: 'export declare function createProductComposition(): unknown;',
      },
    ],
  })
  const blocked = compareApiReports(report(), report(current))
  assert.equal(blocked.status, 'additive')
  assert.equal(blocked.blocking, true)
  assert.match(blocked.errors[0], /semantic-version bump/)

  const accepted = compareApiReports(
    report(),
    report({ ...current, version: '0.2.0' }),
  )
  assert.equal(accepted.blocking, false)
})

test('changed declarations are breaking for pre-1.0 packages', () => {
  const result = compareApiReports(
    report(),
    report(packageReport({
      version: '0.2.0',
      declarations: [
        {
          key: 'index.d.ts#FunctionDeclaration:createOpenSquillaApp',
          signature: 'export declare function createOpenSquillaApp(required: string): unknown;',
        },
      ],
    })),
  )
  assert.equal(result.status, 'breaking')
  assert.equal(result.blocking, false)
})

test('removed exports require a completed deprecation window', () => {
  const previous = packageReport({
    deprecations: [
      {
        export: 'createOpenSquillaApp',
        since: '0.1.0',
        removeAfter: '0.2.0',
        replacement: 'createProductComposition',
      },
    ],
  })
  const current = packageReport({
    version: '0.2.0',
    apiExports: [],
    runtimeExports: [],
    declarations: [],
  })
  const accepted = compareApiReports(report(previous), report(current))
  assert.equal(accepted.status, 'breaking')
  assert.equal(accepted.blocking, false)

  const blocked = compareApiReports(
    report(packageReport()),
    report(current),
  )
  assert.equal(blocked.blocking, true)
  assert.ok(blocked.errors.some((entry) => entry.includes('deprecation window')))
})

test('semantic versions are parsed without accepting loose values', () => {
  assert.deepEqual(parseSemver('1.2.3'), {
    raw: '1.2.3',
    major: 1,
    minor: 2,
    patch: 3,
    prerelease: null,
  })
  assert.throws(() => parseSemver('v1.2.3'), /Invalid semantic version/)
})
