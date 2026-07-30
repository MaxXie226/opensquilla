import assert from 'node:assert/strict'
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

import {
  assertRuntimeSetReady,
  downloadVerifiedAsset,
  validateRuntimeManifest,
} from './fetch-bundled-runtimes.mjs'

const root = await mkdtemp(join(tmpdir(), 'opensquilla-runtime-test-'))

try {
  const source = join(root, 'source.bin')
  const destination = join(root, 'download.bin')
  await writeFile(source, 'pinned runtime fixture')
  const sha256 = '20d19703ab25f1f20d069f1c8a30c68338cd36ec977f5462a52d14b48b375483'
  const asset = {
    id: 'fixture',
    version: '1.0.0',
    url: pathToFileURL(source).href,
    sha256,
    archiveType: 'zip',
    installDir: 'fixture',
    stripComponents: 0,
    binDirs: ['.'],
    executables: { fixture: 'fixture.exe' },
  }

  validateRuntimeManifest({
    schemaVersion: 1,
    runtimeSet: 'test',
    assets: { 'windows-x64': { python: asset, node: asset, gitBash: asset } },
  })
  validateRuntimeManifest({
    schemaVersion: 1,
    runtimeSet: 'portable-test',
    assets: { 'linux-x64': { python: asset, node: asset } },
  })
  assert.throws(
    () => validateRuntimeManifest({
      schemaVersion: 1,
      runtimeSet: 'windows-incomplete',
      assets: { 'windows-x64': { python: asset, node: asset } },
    }),
    /gitBash/,
  )

  await downloadVerifiedAsset(asset, destination)
  assert.equal(await readFile(destination, 'utf8'), 'pinned runtime fixture')

  await assert.rejects(
    downloadVerifiedAsset({ ...asset, sha256: '0'.repeat(64) }, join(root, 'bad.bin')),
    /checksum mismatch/,
  )

  await assert.rejects(
    assertRuntimeSetReady({
      manifest: {
        schemaVersion: 1,
        runtimeSet: 'test',
        assets: { 'windows-x64': { python: asset, node: asset, gitBash: asset } },
      },
      runtimeRoot: join(root, 'missing'),
      target: 'windows-x64',
      executeCommands: false,
    }),
    /runtime is missing/,
  )

  assert.throws(
    () => validateRuntimeManifest({
      schemaVersion: 1,
      runtimeSet: 'test',
      assets: { 'windows-x64': { python: { ...asset, installDir: '../escape' } } },
    }),
    /installDir/,
  )
} finally {
  await rm(root, { recursive: true, force: true })
}

console.log('Bundled runtime tests passed.')
