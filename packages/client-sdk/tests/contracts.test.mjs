import assert from 'node:assert/strict'
import test from 'node:test'

import {
  CLIENT_CONTRACT_DIGEST,
  CLIENT_CONTRACT_SCHEMA_VERSION,
  DECLARED_EVENTS,
  EVENT_PATTERNS,
  RPC_METHOD_SCOPES,
  capabilitiesForMethods,
  normalizeHelloFrame,
} from '../dist/index.js'

test('exports deterministic contract identity and catalogs', () => {
  assert.match(CLIENT_CONTRACT_DIGEST, /^sha256:[0-9a-f]{64}$/)
  assert.equal(CLIENT_CONTRACT_SCHEMA_VERSION, 1)
  assert.equal(RPC_METHOD_SCOPES['sessions.list'], 'operator.read')
  assert.ok(DECLARED_EVENTS.includes('connect.challenge'))
  assert.ok(EVENT_PATTERNS.includes('session.event.*'))
})

test('derives only capabilities proven by the legacy method inventory', () => {
  assert.deepEqual(
    capabilitiesForMethods([
      'chat.history',
      'chat.send',
      'sessions.list',
      'sessions.resolve',
    ]),
    ['gateway.rpc', 'gateway.sessions'],
  )
  assert.deepEqual(capabilitiesForMethods(['sessions.list']), ['gateway.rpc'])
  assert.deepEqual(capabilitiesForMethods([]), [])
})

test('normalizes a legacy Hello without requiring new metadata', () => {
  const hello = normalizeHelloFrame(
    {
      type: 'hello-ok',
      protocol: 3,
      server: { version: '0.4.0' },
      features: {
        methods: ['chat.history', 'chat.send', 'sessions.list', 'sessions.resolve'],
      },
    },
    '1',
  )

  assert.equal(hello.contractStatus, 'legacy-contract')
  assert.equal(hello.capabilitySource, 'features.methods')
  assert.deepEqual(hello.protocolRange, { min: 3, max: 3 })
  assert.deepEqual(hello.runtime, { coreVersion: '0.4.0', buildCommit: null })
  assert.deepEqual(hello.capabilities, ['gateway.rpc', 'gateway.sessions'])
})

test('rejects new Hello metadata without request correlation', () => {
  assert.throws(
    () =>
      normalizeHelloFrame(
        {
          type: 'hello-ok',
          protocol: 3,
          capabilities: ['gateway.rpc'],
        },
        '1',
      ),
    /missing response id/,
  )
})
