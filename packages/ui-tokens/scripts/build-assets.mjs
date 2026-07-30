import { cp, mkdir, readdir, rm } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const packageRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const sourceRoot = path.join(packageRoot, 'src')
const outputRoot = path.join(packageRoot, 'dist')

await rm(outputRoot, { recursive: true, force: true })
await mkdir(outputRoot, { recursive: true })
await cp(
  path.join(sourceRoot, 'foundation.css'),
  path.join(outputRoot, 'foundation.css'),
)
await cp(
  path.join(sourceRoot, 'themes.css'),
  path.join(outputRoot, 'themes.css'),
)
await cp(
  path.join(sourceRoot, 'theme-contract.json'),
  path.join(outputRoot, 'theme-contract.json'),
)

const themes = await readdir(path.join(sourceRoot, 'themes'), {
  withFileTypes: true,
})
for (const theme of themes) {
  if (!theme.isDirectory()) continue
  const target = path.join(outputRoot, 'themes', theme.name)
  await mkdir(target, { recursive: true })
  await cp(
    path.join(sourceRoot, 'themes', theme.name, 'tokens.css'),
    path.join(target, 'tokens.css'),
  )
}
