import assert from 'node:assert/strict'

import {
  CLIENT_CONTRACT_DIGEST,
  CLIENT_MAX_PROTOCOL,
  CLIENT_MIN_PROTOCOL,
} from '@opensquilla/client-sdk'
import {
  NATIVE_CAPABILITY_API_VERSION,
  UI_COMPOSITION_API_VERSION,
  createOpenSquillaApp,
  createWebNativeCapabilityAdapter,
} from '@opensquilla/ui-foundation'
import * as primitives from '@opensquilla/ui-primitives'
import { PUBLIC_THEME_IDS, THEME_TOKEN_NAMES } from '@opensquilla/ui-tokens'

assert.match(CLIENT_CONTRACT_DIGEST, /^sha256:[0-9a-f]{64}$/)
assert.equal(CLIENT_MIN_PROTOCOL, 3)
assert.equal(CLIENT_MAX_PROTOCOL, 3)
assert.equal(UI_COMPOSITION_API_VERSION, 1)
assert.equal(NATIVE_CAPABILITY_API_VERSION, 1)
assert.ok(PUBLIC_THEME_IDS.includes('dark'))
assert.ok(THEME_TOKEN_NAMES.includes('accent'))
assert.deepEqual(
  Object.keys(primitives).sort(),
  ['UiButton', 'UiCard', 'UiDialog', 'UiInput', 'UiStack', 'UiSwitch'],
)

const community = await createOpenSquillaApp({
  features: [{ id: 'community.chat', apiVersion: UI_COMPOSITION_API_VERSION }],
  native: createWebNativeCapabilityAdapter(),
})
const product = await createOpenSquillaApp({
  features: [{ id: 'product.private-example', apiVersion: UI_COMPOSITION_API_VERSION }],
  native: createWebNativeCapabilityAdapter(),
})
assert.deepEqual(community.registry.features.map((feature) => feature.id), ['community.chat'])
assert.deepEqual(
  product.registry.features.map((feature) => feature.id),
  ['product.private-example'],
)
await community.dispose()
await product.dispose()
