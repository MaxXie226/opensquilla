import assert from 'node:assert/strict'

import {
  HandshakeError,
  normalizeHelloFrame,
} from '@opensquilla/client-sdk'

const current = normalizeHelloFrame(
  {
    type: 'hello-ok',
    id: 'connect-current',
    protocol: 3,
    protocolRange: { min: 3, max: 3 },
    contract: {
      schemaVersion: 1,
      digest: 'sha256:9819d28398fd37609c8c86ae9b5bbf8133d122ae07f06b75817a4d5b3c30a79e',
      generatedFrom: 'gateway',
    },
    runtime: { coreVersion: '0.5.2', platform: 'linux', arch: 'x64' },
    capabilities: ['gateway.rpc', 'gateway.sessions'],
    extensions: [],
    features: { methods: ['chat.send'], events: ['agent'] },
    policy: {},
    server: { version: '0.5.2', conn_id: 'fixture-current' },
    snapshot: {},
  },
  'connect-current',
)
assert.equal(current.contractStatus, 'advertised')
assert.deepEqual(current.capabilities, ['gateway.rpc', 'gateway.sessions'])

const legacy = normalizeHelloFrame(
  {
    type: 'hello-ok',
    protocol: 3,
    features: {
      methods: ['chat.history', 'chat.send', 'sessions.list', 'sessions.resolve'],
      events: [],
    },
    policy: {},
    server: { version: '0.4.0', conn_id: 'fixture-legacy' },
    snapshot: {},
  },
  'connect-legacy',
)
assert.equal(legacy.contractStatus, 'legacy-contract')
assert.deepEqual(legacy.protocolRange, { min: 3, max: 3 })
assert.ok(legacy.capabilities.includes('gateway.sessions'))

assert.throws(
  () => normalizeHelloFrame(
    {
      type: 'hello-ok',
      protocol: 2,
      features: { methods: [], events: [] },
      policy: {},
      server: { version: '0.3.0', conn_id: 'fixture-incompatible' },
      snapshot: {},
    },
    'connect-incompatible',
  ),
  HandshakeError,
)
