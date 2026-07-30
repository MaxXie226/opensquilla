import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { lstat, readdir, readFile } from 'node:fs/promises'
import path from 'node:path'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const webuiRoot = path.join(repositoryRoot, 'opensquilla-webui')
const npmEntry = process.env.npm_execpath

async function sha256(source) {
  const hash = createHash('sha256')
  hash.update(await readFile(source))
  return hash.digest('hex')
}

function parseArgs(argv) {
  if (argv.length !== 2 || argv[0] !== '--artifacts-dir') {
    throw new Error('usage: install_ui_package_release.mjs --artifacts-dir <directory>')
  }
  return path.resolve(argv[1])
}

async function main(argv = process.argv.slice(2)) {
  if (!npmEntry) throw new Error('Run tarball installation through npm')
  const directory = parseArgs(argv)
  const manifests = (await readdir(directory))
    .filter((entry) => /^opensquilla-ui-foundation-.+\.manifest\.json$/.test(entry))
  assert.equal(manifests.length, 1, 'expected one UI release manifest')
  const manifest = JSON.parse(await readFile(path.join(directory, manifests[0]), 'utf8'))
  assert.equal(manifest.immutable, true)
  const tarballs = []
  for (const entry of manifest.packages) {
    assert.equal(path.basename(entry.tarball), entry.tarball)
    const tarball = path.join(directory, entry.tarball)
    assert.equal(await sha256(tarball), entry.sha256, `${entry.name}: SHA-256 mismatch`)
    tarballs.push(tarball)
  }
  const result = spawnSync(
    process.execPath,
    [
      npmEntry,
      'install',
      '--ignore-scripts',
      '--no-save',
      '--package-lock=false',
      ...tarballs,
    ],
    {
      cwd: webuiRoot,
      encoding: 'utf8',
      env: {
        ...process.env,
        npm_config_audit: 'false',
        npm_config_fund: 'false',
      },
    },
  )
  if (result.status !== 0) {
    throw new Error(
      ['unable to install release tarballs into public WebUI', result.stdout, result.stderr]
        .filter(Boolean)
        .join('\n'),
    )
  }
  for (const entry of manifest.packages) {
    const installedRoot = path.join(webuiRoot, 'node_modules', ...entry.name.split('/'))
    assert.equal((await lstat(installedRoot)).isSymbolicLink(), false)
    const packageJson = JSON.parse(
      await readFile(path.join(installedRoot, 'package.json'), 'utf8'),
    )
    assert.equal(packageJson.version, entry.version)
  }
  console.log(
    `Installed ${manifest.packages.length} immutable release tarballs into the public WebUI`,
  )
}

main().catch((error) => {
  console.error(`UI package release installation failed: ${error.message}`)
  process.exitCode = 1
})
