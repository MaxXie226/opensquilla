import assert from 'node:assert/strict'
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

const packageRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const temporaryRoot = await mkdtemp(path.join(tmpdir(), 'opensquilla-client-sdk-'))
const npmEntry = process.env.npm_execpath

if (!npmEntry) {
  throw new Error('Run package verification through npm so npm_execpath is available')
}

function npm(args, cwd) {
  const result = spawnSync(process.execPath, [npmEntry, ...args], {
    cwd,
    encoding: 'utf8',
    env: {
      ...process.env,
      npm_config_audit: 'false',
      npm_config_cache: path.join(temporaryRoot, 'npm-cache'),
      npm_config_fund: 'false',
    },
  })
  if (result.status !== 0) {
    throw new Error(
      [`npm ${args.join(' ')} failed`, result.stdout, result.stderr].filter(Boolean).join('\n'),
    )
  }
  return result.stdout
}

try {
  const packOutput = npm(['pack', '--json', '--pack-destination', temporaryRoot], packageRoot)
  const [packed] = JSON.parse(packOutput)
  assert.ok(packed?.filename, 'npm pack did not report a tarball')
  const paths = packed.files.map((entry) => entry.path)
  const allowed = [
    'README.md',
    'contract-coverage.json',
    'package.json',
  ]
  for (const entry of paths) {
    assert.ok(
      allowed.includes(entry) || entry.startsWith('dist/'),
      `unexpected file in client SDK tarball: ${entry}`,
    )
    assert.ok(!entry.endsWith('.map'), `source map leaked into client SDK tarball: ${entry}`)
  }
  assert.ok(paths.includes('dist/index.js'))
  assert.ok(paths.includes('dist/index.d.ts'))
  assert.ok(paths.includes('contract-coverage.json'))

  const tarball = path.join(temporaryRoot, packed.filename)
  const consumer = path.join(temporaryRoot, 'consumer')
  await writeFile(
    path.join(temporaryRoot, 'consumer-package.json'),
    JSON.stringify({ private: true, type: 'module' }),
  )
  await mkdir(consumer)
  await writeFile(
    path.join(consumer, 'package.json'),
    await readFile(path.join(temporaryRoot, 'consumer-package.json'), 'utf8'),
  )
  npm(
    ['install', '--ignore-scripts', '--offline', '--no-package-lock', tarball],
    consumer,
  )
  await writeFile(
    path.join(consumer, 'smoke.mjs'),
    [
      "import { CLIENT_CONTRACT_DIGEST, GatewayClient } from '@opensquilla/client-sdk'",
      "if (!CLIENT_CONTRACT_DIGEST.startsWith('sha256:')) throw new Error('missing digest')",
      "if (typeof GatewayClient !== 'function') throw new Error('missing client')",
      '',
    ].join('\n'),
  )
  const smoke = spawnSync(process.execPath, ['smoke.mjs'], {
    cwd: consumer,
    encoding: 'utf8',
  })
  if (smoke.status !== 0) {
    throw new Error(['external import smoke failed', smoke.stdout, smoke.stderr].join('\n'))
  }

  const packageFiles = await Promise.all(
    paths
      .filter((entry) => entry.endsWith('.js') || entry.endsWith('.d.ts') || entry.endsWith('.json'))
      .map(async (entry) =>
        readFile(
          path.join(consumer, 'node_modules/@opensquilla/client-sdk', entry),
          'utf8',
        )
      ),
  )
  const joined = packageFiles.join('\n')
  assert.ok(!joined.includes(packageRoot), 'client SDK contains its source checkout path')
  assert.ok(!joined.includes('/private/tmp/'), 'client SDK contains a temporary machine path')
  console.log(`Verified external client SDK tarball (${paths.length} files)`)
} finally {
  await rm(temporaryRoot, { recursive: true, force: true })
}
