import { readFile, readdir } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

import {
  DERIVED_THEME_TOKEN_NAMES,
  PUBLIC_THEME_IDS,
  REQUIRED_THEME_TOKEN_NAMES,
} from '../src/index.js'

const packageRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '..',
)

describe('published theme assets', () => {
  it('keeps the JSON and TypeScript contracts identical', async () => {
    const contract = JSON.parse(
      await readFile(path.join(packageRoot, 'src', 'theme-contract.json'), 'utf8'),
    )

    expect(contract.required).toEqual(REQUIRED_THEME_TOKEN_NAMES)
    expect(contract.derivedOptional).toEqual(DERIVED_THEME_TOKEN_NAMES)
  })

  it('publishes exactly the declared built-in themes', async () => {
    const directories = (
      await readdir(path.join(packageRoot, 'src', 'themes'), {
        withFileTypes: true,
      })
    )
      .filter((entry) => entry.isDirectory())
      .map((entry) => entry.name)
      .sort()

    expect(directories).toEqual([...PUBLIC_THEME_IDS].sort())
  })

  it.each(PUBLIC_THEME_IDS)(
    '%s defines every required semantic role',
    async (theme) => {
      const css = await readFile(
        path.join(packageRoot, 'src', 'themes', theme, 'tokens.css'),
        'utf8',
      )
      const definitions = new Set(
        [...css.matchAll(/--([\w-]+)\s*:/g)].map((match) => match[1]),
      )

      expect(
        REQUIRED_THEME_TOKEN_NAMES.filter((token) => !definitions.has(token)),
      ).toEqual([])
    },
  )

  it('keeps the aggregate stylesheet in public theme order', async () => {
    const aggregate = await readFile(
      path.join(packageRoot, 'src', 'themes.css'),
      'utf8',
    )
    const imports = [...aggregate.matchAll(/themes\/([^/]+)\/tokens\.css/g)]
      .map((match) => match[1])

    expect(imports).toEqual(PUBLIC_THEME_IDS)
  })
})
