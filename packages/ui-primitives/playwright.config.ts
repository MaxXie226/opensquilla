import { defineConfig } from '@playwright/test'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const packageRoot = path.dirname(fileURLToPath(import.meta.url))
const repositoryRoot = path.resolve(packageRoot, '..', '..')

export default defineConfig({
  testDir: path.join(packageRoot, 'browser'),
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: 'list',
  snapshotPathTemplate:
    '{testDir}/__snapshots__/{testFilePath}/{arg}-{projectName}{ext}',
  use: {
    baseURL: 'http://127.0.0.1:4187',
    screenshot: 'only-on-failure',
    trace: 'on-first-retry',
  },
  webServer: {
    command:
      'npm run build:ui-packages && node node_modules/vite/bin/vite.js --config packages/ui-primitives/vite.fixture.config.ts',
    cwd: repositoryRoot,
    url: 'http://127.0.0.1:4187',
    reuseExistingServer: false,
    timeout: 120_000,
  },
  projects: [
    { name: 'chromium', use: { browserName: 'chromium' } },
    { name: 'firefox', use: { browserName: 'firefox' } },
    { name: 'webkit', use: { browserName: 'webkit' } },
  ],
})
