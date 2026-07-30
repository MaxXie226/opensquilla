import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import {
  copyFile,
  lstat,
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  rm,
  stat,
  writeFile,
} from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

import { buildRelease } from './build_ui_package_release.mjs'
import { readJson, repositoryRoot } from './ui_package_api.mjs'

const npmEntry = process.env.npm_execpath
const fixtureRoot = path.join(
  repositoryRoot,
  'tests',
  'fixtures',
  'ui-package-consumer',
)

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
      [`${path.basename(command)} ${args.join(' ')} failed`, result.stdout, result.stderr]
        .filter(Boolean)
        .join('\n'),
    )
  }
  return result.stdout
}

function npm(args, cwd) {
  if (!npmEntry) throw new Error('Run release verification through npm')
  return run(process.execPath, [npmEntry, ...args], cwd)
}

async function sha256(source) {
  const hash = createHash('sha256')
  hash.update(await readFile(source))
  return hash.digest('hex')
}

async function findReleaseManifest(directory) {
  const candidates = (await readdir(directory))
    .filter((entry) => /^opensquilla-ui-foundation-.+\.manifest\.json$/.test(entry))
  assert.equal(candidates.length, 1, 'release directory must contain exactly one manifest')
  return path.join(directory, candidates[0])
}

async function verifyChecksums(directory, manifest) {
  for (const artifact of manifest.artifacts) {
    const source = path.join(directory, artifact.name)
    assert.equal(await sha256(source), artifact.sha256, `${artifact.name}: SHA-256 mismatch`)
    assert.equal((await stat(source)).size, artifact.size, `${artifact.name}: size mismatch`)
  }
  const manifestPath = await findReleaseManifest(directory)
  const sumsPath = path.join(
    directory,
    `SHA256SUMS.ui-foundation-${manifest.releaseVersion}`,
  )
  const expected = new Map(
    (await readFile(sumsPath, 'utf8')).trim().split('\n').map((line) => {
      const match = /^([0-9a-f]{64})  ([^/\\]+)$/.exec(line)
      assert.ok(match, `invalid checksum line: ${line}`)
      return [match[2], match[1]]
    }),
  )
  for (const artifact of [...manifest.artifacts, {
    name: path.basename(manifestPath),
    sha256: await sha256(manifestPath),
  }]) {
    assert.equal(expected.get(artifact.name), artifact.sha256)
  }
  assert.equal(expected.size, manifest.artifacts.length + 1)
}

async function createVuePeer(temporaryRoot) {
  const peerRoot = path.join(temporaryRoot, 'vue-peer')
  await mkdir(peerRoot, { recursive: true })
  await writeFile(
    path.join(peerRoot, 'package.json'),
    JSON.stringify({
      name: 'vue',
      version: '3.5.0',
      type: 'module',
      exports: { '.': { types: './index.d.ts', import: './index.js' } },
      files: ['index.d.ts', 'index.js'],
    }),
  )
  await writeFile(
    path.join(peerRoot, 'index.js'),
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
      'export const getCurrentInstance = () => undefined',
      'export const getCurrentScope = () => undefined',
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
    path.join(peerRoot, 'index.d.ts'),
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
  const [packed] = JSON.parse(
    npm(
      ['pack', '--json', '--ignore-scripts', '--pack-destination', temporaryRoot, peerRoot],
      repositoryRoot,
    ),
  )
  return path.join(temporaryRoot, packed.filename)
}

async function installAndTest(directory, manifest, tarballSelection, label, temporaryRoot) {
  const consumer = path.join(temporaryRoot, label)
  await mkdir(consumer)
  await writeFile(
    path.join(consumer, 'package.json'),
    JSON.stringify({ name: `ui-release-${label}`, private: true, type: 'module' }),
  )
  for (const fixture of ['smoke.ts', 'smoke.mjs', 'gateway-compatibility.mjs']) {
    await copyFile(path.join(fixtureRoot, fixture), path.join(consumer, fixture))
  }
  const vuePeer = await createVuePeer(path.join(temporaryRoot, `peer-${label}`))
  npm(
    [
      'install',
      '--ignore-scripts',
      '--offline',
      '--no-package-lock',
      vuePeer,
      ...tarballSelection.map((entry) => path.join(entry.directory ?? directory, entry.tarball)),
    ],
    consumer,
  )
  await writeFile(
    path.join(consumer, 'tsconfig.json'),
    JSON.stringify({
      compilerOptions: {
        exactOptionalPropertyTypes: true,
        module: 'ESNext',
        moduleResolution: 'Bundler',
        noEmit: true,
        strict: true,
        target: 'ES2022',
      },
      include: ['smoke.ts'],
    }),
  )
  run(
    process.execPath,
    [path.join(repositoryRoot, 'node_modules', 'typescript', 'bin', 'tsc'), '-p', 'tsconfig.json'],
    consumer,
  )
  run(process.execPath, ['smoke.mjs'], consumer)
  run(process.execPath, ['gateway-compatibility.mjs'], consumer)
  for (const entry of tarballSelection) {
    const installedRoot = path.join(consumer, 'node_modules', ...entry.name.split('/'))
    assert.equal((await lstat(installedRoot)).isSymbolicLink(), false)
    const packageJson = await readJson(path.join(installedRoot, 'package.json'))
    assert.equal(packageJson.version, entry.version)
    const packedText = (
      await Promise.all(
        (await readdir(installedRoot, { recursive: true, withFileTypes: true }))
          .filter((item) => item.isFile())
          .map((item) => path.join(item.parentPath, item.name))
          .filter((item) => /\.(?:css|d\.ts|js|json|md)$/.test(item))
          .map((item) => readFile(item, 'utf8')),
      )
    ).join('\n')
    for (const forbidden of [
      repositoryRoot,
      temporaryRoot,
      '/private/tmp/',
      '/Users/',
      'C:\\Users\\',
      'PRIVATE_CLIENT_REPOSITORY',
      'OPENSQUILLA_PRIVATE',
    ]) {
      assert.ok(!packedText.includes(forbidden), `${entry.name} leaked ${forbidden}`)
    }
    assert.ok(
      !/(?:sk|ghp|xox[baprs])-[A-Za-z0-9_-]{16,}/.test(packedText),
      `${entry.name} contains a secret-shaped value`,
    )
  }
  return manifest
}

async function loadRelease(directory) {
  const manifest = await readJson(await findReleaseManifest(directory))
  assert.equal(manifest.schemaVersion, 1)
  assert.equal(manifest.releaseTrain, 'public-ui-foundation')
  assert.equal(manifest.immutable, true)
  await verifyChecksums(directory, manifest)
  return manifest
}

async function verifySupplyChain(directory, manifest) {
  const prefix = `opensquilla-ui-foundation-${manifest.releaseVersion}`
  const sbom = await readJson(path.join(directory, `${prefix}.sbom.spdx.json`))
  assert.equal(sbom.spdxVersion, 'SPDX-2.3')
  assert.deepEqual(
    new Set(sbom.packages.map((entry) => entry.name)),
    new Set(manifest.packages.map((entry) => entry.name)),
  )
  const provenance = await readJson(path.join(directory, `${prefix}.provenance.json`))
  assert.equal(provenance._type, 'https://in-toto.io/Statement/v1')
  assert.equal(provenance.predicateType, 'https://slsa.dev/provenance/v1')
  const provenanceName = `${prefix}.provenance.json`
  assert.deepEqual(
    provenance.subject,
    manifest.artifacts
      .filter((artifact) => artifact.name !== provenanceName)
      .map((artifact) => ({
        name: artifact.name,
        digest: { sha256: artifact.sha256 },
      })),
  )
  for (const artifact of manifest.artifacts) {
    if (artifact.name.endsWith('.tgz')) continue
    const text = await readFile(path.join(directory, artifact.name), 'utf8')
    for (const forbidden of [
      repositoryRoot,
      '/private/tmp/',
      '/Users/',
      'C:\\Users\\',
      'PRIVATE_CLIENT_REPOSITORY',
      'OPENSQUILLA_PRIVATE',
    ]) {
      assert.ok(!text.includes(forbidden), `${artifact.name} leaked ${forbidden}`)
    }
    assert.ok(
      !/(?:sk|ghp|xox[baprs])-[A-Za-z0-9_-]{16,}/.test(text),
      `${artifact.name} contains a secret-shaped value`,
    )
  }
}

function parseArgs(argv) {
  const options = { artifactsDir: null, previousArtifactsDir: null }
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index]
    if (value === '--artifacts-dir') options.artifactsDir = path.resolve(argv[++index])
    else if (value === '--previous-artifacts-dir') {
      options.previousArtifactsDir = path.resolve(argv[++index])
    } else throw new Error(`Unknown argument: ${value}`)
  }
  return options
}

export async function main(argv = process.argv.slice(2)) {
  const options = parseArgs(argv)
  const temporaryRoot = await mkdtemp(path.join(tmpdir(), 'opensquilla-ui-release-verify-'))
  const artifactsDir = options.artifactsDir ?? path.join(temporaryRoot, 'release')
  try {
    if (!options.artifactsDir) {
      await buildRelease({
        outputDir: artifactsDir,
        releaseVersion: null,
        sourceCommit: '0000000',
        sourceRef: 'verification',
      })
      await assert.rejects(
        buildRelease({
          outputDir: artifactsDir,
          releaseVersion: null,
          sourceCommit: '0000000',
          sourceRef: 'verification',
        }),
        /immutable assets will not be overwritten/,
      )
    }
    const current = await loadRelease(artifactsDir)
    await verifySupplyChain(artifactsDir, current)
    await installAndTest(
      artifactsDir,
      current,
      current.packages,
      'current',
      temporaryRoot,
    )
    const matrix = await readJson(
      path.join(repositoryRoot, 'packages', 'ui-compatibility-matrix.json'),
    )
    if (matrix.previous === null) {
      assert.equal(matrix.bootstrap, true)
    } else {
      assert.ok(
        options.previousArtifactsDir,
        'N-1 release assets are required after the bootstrap release',
      )
      const previous = await loadRelease(options.previousArtifactsDir)
      assert.equal(previous.releaseVersion, matrix.previous.releaseVersion)
      const currentSdk = current.packages.find(
        (entry) => entry.name === '@opensquilla/client-sdk',
      )
      const previousSdk = previous.packages.find(
        (entry) => entry.name === '@opensquilla/client-sdk',
      )
      const currentUi = current.packages.filter(
        (entry) => entry.name !== '@opensquilla/client-sdk',
      )
      const previousUi = previous.packages.filter(
        (entry) => entry.name !== '@opensquilla/client-sdk',
      )
      await installAndTest(
        artifactsDir,
        current,
        [
          ...currentUi,
          { ...previousSdk, directory: options.previousArtifactsDir },
        ],
        'current-ui-previous-sdk',
        temporaryRoot,
      )
      await installAndTest(
        artifactsDir,
        current,
        [
          ...previousUi.map((entry) => ({
            ...entry,
            directory: options.previousArtifactsDir,
          })),
          currentSdk,
        ],
        'previous-ui-current-sdk',
        temporaryRoot,
      )
    }
    console.log(
      `Verified UI release ${current.releaseVersion}: immutable assets, supply chain, external consumer, and compatibility matrix`,
    )
  } finally {
    await rm(temporaryRoot, { recursive: true, force: true })
  }
}

if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) {
  main().catch((error) => {
    console.error(`UI package release verification failed: ${error.message}`)
    process.exitCode = 1
  })
}
