import assert from 'node:assert/strict'
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const manifestPath = path.join(repositoryRoot, 'packages', 'ui-package-manifest.json')
const temporaryRoot = await mkdtemp(path.join(tmpdir(), 'opensquilla-ui-packages-'))
const npmEntry = process.env.npm_execpath

if (!npmEntry) {
  throw new Error('Run package verification through npm so npm_execpath is available')
}

function run(command, args, cwd, extraEnv = {}) {
  const result = spawnSync(command, args, {
    cwd,
    encoding: 'utf8',
    env: {
      ...process.env,
      npm_config_audit: 'false',
      npm_config_fund: 'false',
      ...extraEnv,
    },
  })
  if (result.status !== 0) {
    throw new Error(
      [
        `${path.basename(command)} ${args.join(' ')} failed`,
        result.stdout,
        result.stderr,
      ].filter(Boolean).join('\n'),
    )
  }
  return result.stdout
}

function npm(args, cwd) {
  return run(process.execPath, [npmEntry, ...args], cwd)
}

function assertPackageMetadata(record, packageJson) {
  assert.equal(packageJson.name, record.name)
  assert.equal(packageJson.version, record.version)
  assert.equal(packageJson.private, undefined, `${record.name} must remain publishable`)
  assert.equal(packageJson.license, 'Apache-2.0')
  assert.equal(packageJson.sideEffects, false)
  assert.deepEqual(Object.keys(packageJson.exports), record.publicExports)
  assert.equal(packageJson.exports['.'].types, './dist/index.d.ts')
  assert.equal(packageJson.exports['.'].import, './dist/index.js')
  assert.equal(packageJson.publishConfig.access, 'public')
  assert.equal(packageJson.publishConfig.provenance, true)
  assert.equal(packageJson.engines.node, '>=22.12.0')
  assert.match(packageJson.version, /^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/)
}

function isAllowedPackedFile(record, file) {
  if (['CHANGELOG.md', 'LICENSE', 'README.md', 'package.json'].includes(file)) return true
  if (record.name === '@opensquilla/ui-foundation') {
    return /^dist\/(?:[\w.-]+\/)*[\w.-]+\.(?:d\.ts|js)$/.test(file)
  }
  if (record.name === '@opensquilla/ui-tokens') {
    return [
      'dist/index.d.ts',
      'dist/index.js',
      'dist/foundation.css',
      'dist/themes.css',
      'dist/theme-contract.json',
    ].includes(file) || /^dist\/themes\/[^/]+\/tokens\.css$/.test(file)
  }
  if (record.name === '@opensquilla/ui-primitives') {
    return [
      'dist/index.d.ts',
      'dist/index.js',
      'dist/styles.css',
    ].includes(file) || /^dist\/components\/[\w.-]+\.d\.ts$/.test(file)
  }
  return false
}

function assertPackedFiles(record, packed) {
  assert.ok(packed.filename, `${record.name}: npm pack did not report a tarball`)
  assert.match(packed.integrity, /^sha512-/)
  assert.match(packed.shasum, /^[0-9a-f]{40}$/)

  const paths = packed.files.map((entry) => entry.path).sort()
  for (const entry of paths) {
    assert.ok(
      isAllowedPackedFile(record, entry),
      `${record.name}: unexpected packaged file ${entry}`,
    )
    assert.ok(!entry.startsWith('src/'), `${record.name}: source directory leaked into package`)
    assert.ok(!entry.endsWith('.map'), `${record.name}: source map leaked into package`)
  }
  assert.ok(paths.includes('README.md'), `${record.name}: README is missing`)
  assert.ok(paths.includes('dist/index.js'), `${record.name}: JavaScript entry is missing`)
  assert.ok(paths.includes('dist/index.d.ts'), `${record.name}: type entry is missing`)
}

function assertCleanArtifactText(record, text) {
  const forbidden = [
    repositoryRoot,
    temporaryRoot,
    '/private/tmp/',
    '/Users/',
    'C:\\Users\\',
    'OPENSQUILLA_PRIVATE',
    'PRIVATE_CLIENT_REPOSITORY',
  ]
  for (const value of forbidden) {
    assert.ok(!text.includes(value), `${record.name}: artifact contains forbidden text ${value}`)
  }
  assert.ok(
    !/(?:sk|ghp|xox[baprs])-[A-Za-z0-9_-]{16,}/.test(text),
    `${record.name}: artifact contains a secret-shaped value`,
  )
}

try {
  const manifest = JSON.parse(await readFile(manifestPath, 'utf8'))
  assert.equal(manifest.schemaVersion, 2)
  assert.equal(manifest.releaseTrain, 'public-ui-foundation')
  assert.equal(manifest.versionPolicy, 'release-groups')
  assert.equal(manifest.compatibilityPolicy, 'current-and-previous-minor')

  const rootPackage = JSON.parse(
    await readFile(path.join(repositoryRoot, 'package.json'), 'utf8'),
  )
  assert.equal(rootPackage.private, true)
  assert.equal(rootPackage.packageManager, 'npm@10.9.0')
  assert.equal(rootPackage.engines.node, '>=22.12.0')

  const workspaceRecords = manifest.packages.filter((record) => record.workspace)
  assert.deepEqual(
    [...rootPackage.workspaces].sort(),
    workspaceRecords.map((record) => record.path).sort(),
  )

  const packedTarballs = []
  const dependencyTarballs = []
  for (const record of manifest.packages) {
    const packageRoot = path.join(repositoryRoot, record.path)
    const packageJson = JSON.parse(
      await readFile(path.join(packageRoot, 'package.json'), 'utf8'),
    )
    assert.equal(packageJson.name, record.name)
    assert.equal(packageJson.version, record.version)

    if (!record.workspace) {
      if (record.name === '@opensquilla/client-sdk') {
        const output = npm(
          [
            'pack',
            '--json',
            '--ignore-scripts',
            '--pack-destination',
            temporaryRoot,
            packageRoot,
          ],
          repositoryRoot,
        )
        const [packed] = JSON.parse(output)
        assert.ok(packed.filename, `${record.name}: npm pack did not report a tarball`)
        dependencyTarballs.push(path.join(temporaryRoot, packed.filename))
      }
      continue
    }

    assertPackageMetadata(record, packageJson)
    const output = npm(
      [
        'pack',
        '--json',
        '--ignore-scripts',
        '--pack-destination',
        temporaryRoot,
        packageRoot,
      ],
      repositoryRoot,
    )
    const [packed] = JSON.parse(output)
    assertPackedFiles(record, packed)
    packedTarballs.push({
      ...record,
      packedFiles: packed.files.map((entry) => entry.path),
      tarball: path.join(temporaryRoot, packed.filename),
    })
  }

  const consumer = path.join(temporaryRoot, 'consumer')
  await mkdir(consumer)
  const installedVuePackage = JSON.parse(
    await readFile(path.join(repositoryRoot, 'node_modules', 'vue', 'package.json'), 'utf8'),
  )
  const vuePeerRoot = path.join(temporaryRoot, 'vue-peer')
  await mkdir(vuePeerRoot)
  await writeFile(
    path.join(vuePeerRoot, 'package.json'),
    JSON.stringify({
      name: 'vue',
      version: installedVuePackage.version,
      type: 'module',
      exports: {
        '.': {
          types: './index.d.ts',
          import: './index.js',
        },
      },
      files: ['index.d.ts', 'index.js'],
    }),
  )
  await writeFile(
    path.join(vuePeerRoot, 'index.js'),
    [
      'const passthrough = (...values) => values[0] ?? {}',
      'export const Teleport = {}',
      'export const Transition = {}',
      'export const computed = passthrough',
      'export const createBlock = passthrough',
      'export const createCommentVNode = passthrough',
      'export const createElementBlock = passthrough',
      'export const createElementVNode = passthrough',
      'export const createTextVNode = passthrough',
      'export const createVNode = passthrough',
      'export const defineComponent = passthrough',
      'export const getCurrentScope = () => undefined',
      'export const getCurrentInstance = () => undefined',
      'export const mergeProps = passthrough',
      'export const nextTick = passthrough',
      'export const normalizeClass = passthrough',
      'export const onBeforeUnmount = passthrough',
      'export const onMounted = passthrough',
      'export const onScopeDispose = passthrough',
      'export const openBlock = passthrough',
      'export const readonly = passthrough',
      'export const ref = passthrough',
      'export const renderSlot = passthrough',
      'export const resolveDynamicComponent = passthrough',
      'export const shallowRef = passthrough',
      'export const toDisplayString = passthrough',
      'export const unref = passthrough',
      'export const useAttrs = passthrough',
      'export const useId = passthrough',
      'export const watch = passthrough',
      'export const withCtx = passthrough',
      'export const withKeys = passthrough',
      'export const withModifiers = passthrough',
      '',
    ].join('\n'),
  )
  await writeFile(
    path.join(vuePeerRoot, 'index.d.ts'),
    [
      'export type ComponentOptionsMixin = Record<string, never>',
      'export type ComponentProvideOptions = Record<PropertyKey, unknown>',
      'export type PublicProps = Record<string, unknown>',
      'export type DeepReadonly<T> = Readonly<T>',
      'export interface ComputedRef<T> { readonly value: T }',
      'export interface ShallowRef<T> { value: T }',
      'export function getCurrentScope(): unknown',
      'export function getCurrentInstance(): unknown',
      'export function onScopeDispose(callback: () => void): void',
      'export function onMounted(callback: () => void): void',
      'export function readonly<T>(value: T): DeepReadonly<T>',
      'export function shallowRef<T>(value: T): ShallowRef<T>',
      'export type DefineComponent<',
      '  A = unknown, B = unknown, C = unknown, D = unknown, E = unknown,',
      '  F = unknown, G = unknown, H = unknown, I = unknown, J = unknown,',
      '  K = unknown, L = unknown, M = unknown, N = unknown, O = unknown,',
      '  P = unknown, Q = unknown, R = unknown, S = unknown, T = unknown,',
      '> = unknown',
      '',
    ].join('\n'),
  )
  const [packedVue] = JSON.parse(
    npm(
      [
        'pack',
        '--json',
        '--ignore-scripts',
        '--pack-destination',
        temporaryRoot,
        vuePeerRoot,
      ],
      repositoryRoot,
    ),
  )
  assert.ok(packedVue.filename, 'Vue peer tarball is missing')
  const vueTarball = path.join(temporaryRoot, packedVue.filename)
  await writeFile(
    path.join(consumer, 'package.json'),
    JSON.stringify({
      name: 'external-ui-package-smoke',
      private: true,
      type: 'module',
    }),
  )
  npm(
    [
      'install',
      '--ignore-scripts',
      '--offline',
      '--no-package-lock',
      vueTarball,
      ...dependencyTarballs,
      ...packedTarballs.map((record) => record.tarball),
    ],
    consumer,
  )

  const imports = packedTarballs
    .map((record) => `import * as ${record.role.replaceAll('-', '_')} from '${record.name}'`)
  const typeUses = packedTarballs
    .map((record) => `void ${record.role.replaceAll('-', '_')}`)
  await writeFile(
    path.join(consumer, 'smoke.ts'),
    [...imports, '', ...typeUses, ''].join('\n'),
  )
  await writeFile(
    path.join(consumer, 'tsconfig.json'),
    JSON.stringify(
      {
        compilerOptions: {
          exactOptionalPropertyTypes: true,
          module: 'ESNext',
          moduleResolution: 'Bundler',
          noEmit: true,
          strict: true,
          target: 'ES2022',
        },
        include: ['smoke.ts'],
      },
      null,
      2,
    ),
  )
  run(
    process.execPath,
    [path.join(repositoryRoot, 'node_modules', 'typescript', 'bin', 'tsc'), '-p', 'tsconfig.json'],
    consumer,
  )

  await writeFile(
    path.join(consumer, 'smoke.mjs'),
    [
      "import assert from 'node:assert/strict'",
      `const packageNames = ${JSON.stringify(packedTarballs.map((record) => record.name))}`,
      `const expectedExports = ${JSON.stringify(
        Object.fromEntries(
          packedTarballs.map((record) => [record.name, record.runtimeExports]),
        ),
      )}`,
      'for (const packageName of packageNames) {',
      '  const publicEntry = await import(packageName)',
      '  assert.deepEqual(',
      '    Object.keys(publicEntry).sort(),',
      '    [...expectedExports[packageName]].sort(),',
      "    `${packageName}: runtime exports differ from the public API report`,",
      '  )',
      '  await assert.rejects(',
      "    import(`${packageName}/src/index.js`),",
      "    error => error?.code === 'ERR_PACKAGE_PATH_NOT_EXPORTED',",
      '  )',
      '}',
      '',
    ].join('\n'),
  )
  run(process.execPath, ['smoke.mjs'], consumer)

  for (const record of packedTarballs) {
    const installedRoot = path.join(
      consumer,
      'node_modules',
      ...record.name.split('/'),
    )
    const artifactText = (
      await Promise.all(
        record.packedFiles
          .filter((entry) => /\.(?:css|d\.ts|js|json|md)$/.test(entry))
          .map((entry) => readFile(path.join(installedRoot, entry), 'utf8')),
      )
    ).join('\n')
    assertCleanArtifactText(record, artifactText)
  }

  const sbom = JSON.parse(
    npm(
      ['sbom', '--package-lock-only', '--omit=dev', '--sbom-format=spdx'],
      repositoryRoot,
    ),
  )
  assert.equal(sbom.spdxVersion, 'SPDX-2.3')
  const sbomNames = new Set(sbom.packages.map((entry) => entry.name))
  for (const record of workspaceRecords) {
    assert.ok(sbomNames.has(record.name), `${record.name}: missing from workspace SBOM`)
  }

  console.log(
    `Verified ${packedTarballs.length} public UI packages in an external consumer`,
  )
} finally {
  await rm(temporaryRoot, { recursive: true, force: true })
}
