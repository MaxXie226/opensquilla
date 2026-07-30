import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'

const scriptDir = dirname(fileURLToPath(import.meta.url))
const packageRoot = resolve(scriptDir, '..')
const repoRoot = resolve(packageRoot, '..', '..')
const runtimeGatewayDir = join(packageRoot, 'runtime', 'gateway')
const pyinstallerWorkDir = join(packageRoot, '.pyinstaller')
const controlUiDistDir = join(repoRoot, 'src', 'opensquilla', 'gateway', 'static', 'dist')

// Desktop remains an explicit product bundle during the transition. The public
// builder verifies this UI input before embedding it; public Runtime release
// jobs omit --ui-artifact and therefore remain headless.
const args = [
  'run',
  '--no-dev',
  '--extra',
  'recommended',
  '--extra',
  'mcp',
  '--extra',
  'msg',
  '--extra',
  'matrix',
  '--extra',
  'document-extras',
  '--with',
  'pyinstaller',
  'python',
  '-m',
  'scripts.gateway_runtime.build',
  '--bundle-root',
  runtimeGatewayDir,
  '--work-root',
  pyinstallerWorkDir,
  '--ui-artifact',
  controlUiDistDir,
  '--created-by',
  'desktop/electron/scripts/build-gateway.mjs',
]

const result = spawnSync('uv', args, {
  cwd: repoRoot,
  env: {
    ...process.env,
    PYTHONUNBUFFERED: '1',
    PYTHONUTF8: '1',
    PYTHONIOENCODING: 'utf-8:replace',
  },
  stdio: 'inherit',
  windowsHide: true,
})

if (result.error) throw result.error
if (result.status !== 0) process.exit(result.status ?? 1)
