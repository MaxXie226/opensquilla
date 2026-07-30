import { spawnSync } from 'node:child_process'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const webuiRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const repositoryRoot = path.resolve(webuiRoot, '..')
const npmEntry = process.env.npm_execpath

if (!npmEntry) {
  throw new Error('Run the UI Foundation build through npm so npm_execpath is available')
}

const executablePath = path.join(webuiRoot, 'node_modules', '.bin')
const env = {
  ...process.env,
  PATH: [executablePath, process.env.PATH].filter(Boolean).join(path.delimiter),
}

function run(label, command, args) {
  const result = spawnSync(command, args, {
    cwd: webuiRoot,
    encoding: 'utf8',
    env,
    stdio: 'inherit',
  })
  if (result.status !== 0) {
    throw new Error(`Failed to build public package ${label}`)
  }
}

run(
  'client-sdk',
  process.execPath,
  [
    path.join(webuiRoot, 'node_modules', 'typescript', 'bin', 'tsc'),
    '-p',
    path.join(repositoryRoot, 'packages', 'client-sdk', 'tsconfig.json'),
  ],
)

const tokenPackageRoot = path.join(repositoryRoot, 'packages', 'ui-tokens')
run(
  'ui-tokens',
  process.execPath,
  [npmEntry, '--prefix', tokenPackageRoot, 'run', 'build'],
)

// WebUI must remain independently installable with `cd opensquilla-webui &&
// npm ci`. Local file dependencies do not install their development toolchain,
// so build the Vue package with the WebUI-owned Vite/vue-tsc binaries and bridge
// configs instead of relying on a repository-root node_modules directory.
run(
  'ui-primitives bundle',
  process.execPath,
  [
    path.join(webuiRoot, 'node_modules', 'vite', 'bin', 'vite.js'),
    'build',
    '--config',
    path.join(webuiRoot, 'scripts', 'vite-ui-primitives.config.ts'),
  ],
)
run(
  'ui-primitives declarations',
  process.execPath,
  [
    path.join(webuiRoot, 'node_modules', 'vue-tsc', 'bin', 'vue-tsc.js'),
    '-p',
    path.join(webuiRoot, 'tsconfig.ui-primitives.json'),
    '--declaration',
    '--emitDeclarationOnly',
  ],
)
run(
  'ui-foundation',
  process.execPath,
  [
    path.join(webuiRoot, 'node_modules', 'typescript', 'bin', 'tsc'),
    '-p',
    path.join(webuiRoot, 'tsconfig.ui-foundation.json'),
  ],
)
