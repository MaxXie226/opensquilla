import assert from 'node:assert/strict'
import test from 'node:test'

import {
  GatewayClient,
  GatewayClientError,
  RequestAbortError,
  RequestTimeoutError,
  SequenceGapError,
} from '../dist/index.js'

class FakeWebSocket {
  static OPEN = 1

  readyState = 0
  sent = []
  onopen = null
  onmessage = null
  onclose = null
  onerror = null

  constructor(url) {
    this.url = url
  }

  open() {
    this.readyState = FakeWebSocket.OPEN
    this.onopen?.()
  }

  send(data) {
    this.sent.push(data)
  }

  receive(frame) {
    this.onmessage?.({ data: JSON.stringify(frame) })
  }

  close() {
    if (this.readyState !== 3) {
      this.readyState = 3
      this.onclose?.()
    }
  }
}

function fixture(options = {}) {
  const sockets = []
  const client = new GatewayClient({
    endpoint: 'ws://gateway.test/ws',
    token: '<synthetic>',
    reconnect: { enabled: false },
    keepAliveIntervalMs: 0,
    webSocketFactory: (url) => {
      const socket = new FakeWebSocket(url)
      sockets.push(socket)
      return socket
    },
    ...options,
  })
  return { client, sockets }
}

async function connectFixture(options = {}) {
  const result = fixture(options)
  const connected = result.client.connect()
  const socket = result.sockets[0]
  socket.open()
  socket.receive({
    type: 'event',
    event: 'connect.challenge',
    payload: { nonce: '<synthetic>' },
  })
  const request = JSON.parse(socket.sent[0])
  socket.receive({
    type: 'hello-ok',
    id: request.id,
    protocol: 3,
    server: { version: '0.5.0', conn_id: 'synthetic-connection' },
    policy: { tick_interval_ms: 60_000 },
    features: {
      methods: ['sessions.list'],
      events: ['session.message'],
    },
    contract: {
      schemaVersion: 1,
      digest: `sha256:${'a'.repeat(64)}`,
      generatedFrom: 'gateway',
    },
    runtime: {
      coreVersion: '0.5.0',
      buildCommit: null,
      platform: 'linux',
      arch: 'x86_64',
    },
    protocolRange: { min: 3, max: 3 },
    capabilities: ['gateway.rpc'],
    extensions: [],
  })
  const hello = await connected
  return { ...result, socket, request, hello }
}

test('negotiates Hello and resolves typed RPC responses', async () => {
  const { client, socket, request, hello } = await connectFixture()

  assert.deepEqual(request, {
    type: 'req',
    id: '1',
    method: 'connect',
    params: {
      minProtocol: 3,
      maxProtocol: 3,
      client: { name: 'opensquilla-sdk' },
      auth: { token: '<synthetic>' },
    },
  })
  assert.equal(hello.contractStatus, 'advertised')
  assert.equal(hello.capabilitySource, 'hello')
  assert.deepEqual(hello.capabilities, ['gateway.rpc'])

  const response = client.call('sessions.list', { limit: 5 })
  const rpcRequest = JSON.parse(socket.sent.at(-1))
  socket.receive({ type: 'res', id: rpcRequest.id, ok: true, payload: { sessions: [] } })
  assert.deepEqual(await response, { sessions: [] })
  client.disconnect()
})

test('preserves structured Gateway errors', async () => {
  const { client, socket } = await connectFixture()
  const response = client.call('sessions.list')
  const rpcRequest = JSON.parse(socket.sent.at(-1))
  socket.receive({
    type: 'res',
    id: rpcRequest.id,
    ok: false,
    error: {
      code: 'STORAGE_BUSY',
      message: 'Storage is busy',
      retryable: true,
      retry_after_ms: 250,
      accepted: false,
      details: { operation: 'list' },
    },
  })

  await assert.rejects(response, (error) => {
    assert.ok(error instanceof GatewayClientError)
    assert.equal(error.code, 'STORAGE_BUSY')
    assert.equal(error.retryable, true)
    assert.equal(error.retryAfterMs, 250)
    assert.equal(error.accepted, false)
    assert.deepEqual(error.details, { operation: 'list' })
    return true
  })
  client.disconnect()
})

test('rejects a structured connect error instead of leaving the handshake pending', async () => {
  const { client, sockets } = fixture()
  const connected = client.connect()
  const socket = sockets[0]
  socket.open()
  socket.receive({ type: 'event', event: 'connect.challenge' })
  const request = JSON.parse(socket.sent[0])
  socket.receive({
    type: 'res',
    id: request.id,
    ok: false,
    error: {
      code: 'UNAUTHORIZED',
      message: 'Token rejected',
      retryable: false,
    },
  })

  await assert.rejects(connected, (error) => {
    assert.ok(error instanceof GatewayClientError)
    assert.equal(error.code, 'UNAUTHORIZED')
    return true
  })
  assert.equal(client.state, 'disconnected')
})

test('times out a handshake that never receives a challenge or Hello', async () => {
  const diagnostics = []
  const { client, sockets } = fixture({
    connectTimeoutMs: 5,
    onDiagnostic: (error) => diagnostics.push(error),
  })
  const connected = client.connect()
  sockets[0].open()

  await assert.rejects(connected, /Gateway handshake timed out after 5ms/)
  assert.equal(client.state, 'disconnected')
  assert.match(diagnostics[0].message, /handshake timed out/)
})

test('dispatches exact and wildcard events and detects sequence gaps', async () => {
  const diagnostics = []
  const { client, socket } = await connectFixture({
    onDiagnostic: (error) => diagnostics.push(error),
  })
  const exact = []
  const family = []
  client.on('session.event.text_delta', (frame) => exact.push(frame))
  client.on('session.event.*', (frame) => family.push(frame))

  socket.receive({
    type: 'event',
    event: 'session.event.text_delta',
    seq: 1,
    payload: { text: 'a' },
  })
  socket.receive({
    type: 'event',
    event: 'session.event.text_delta',
    seq: 3,
    payload: { text: 'b' },
  })

  assert.equal(exact.length, 1)
  assert.equal(family.length, 1)
  assert.equal(client.state, 'disconnected')
  assert.equal(diagnostics.length, 1)
  assert.ok(diagnostics[0] instanceof SequenceGapError)
  assert.equal(diagnostics[0].expected, 2)
})

test('times out unresolved calls without leaking the late response', async () => {
  const { client } = await connectFixture({ requestTimeoutMs: 5 })
  await assert.rejects(
    client.call('sessions.list'),
    (error) => error instanceof RequestTimeoutError && error.method === 'sessions.list',
  )
  client.disconnect()
})

test('reconnects with bounded backoff after an unexpected close', async () => {
  const { client, sockets } = fixture({
    reconnect: { enabled: true, initialDelayMs: 1, maxDelayMs: 2, factor: 2 },
  })
  const connecting = client.connect()
  sockets[0].open()
  sockets[0].close()
  await assert.rejects(connecting)
  await new Promise((resolve) => setTimeout(resolve, 10))
  assert.equal(sockets.length, 2)
  assert.equal(client.state, 'connecting')
  client.disconnect()
})

test('aborts a request without retaining pending state', async () => {
  const { client, socket } = await connectFixture()
  const controller = new AbortController()
  const response = client.call(
    'sessions.list',
    {},
    { signal: controller.signal },
  )
  controller.abort()

  await assert.rejects(
    response,
    (error) => error instanceof RequestAbortError && error.method === 'sessions.list',
  )
  assert.equal(client.pendingRequestCount, 0)

  const request = JSON.parse(socket.sent.at(-1))
  socket.receive({ type: 'res', id: request.id, ok: true, payload: 'late' })
  assert.equal(client.pendingRequestCount, 0)
  client.disconnect()
})

test('reconnect actions retire only the current transport and deliver the next Hello', async () => {
  const hellos = []
  const { client, socket, sockets } = await connectFixture({
    onHello: (hello) => hellos.push(hello),
    reconnect: { enabled: true, initialDelayMs: 500, maxDelayMs: 500 },
  })
  const staleClose = socket.onclose
  const controller = new AbortController()
  const response = client.call(
    'sessions.list',
    {},
    { signal: controller.signal, abortAction: 'reconnect' },
  )
  controller.abort()

  await assert.rejects(response, RequestAbortError)
  await new Promise((resolve) => setTimeout(resolve, 5))
  assert.equal(sockets.length, 2, 'explicit reconnect actions must not wait for backoff')

  const replacement = sockets[1]
  replacement.open()
  replacement.receive({ type: 'event', event: 'connect.challenge' })
  const connectRequest = JSON.parse(replacement.sent[0])
  replacement.receive({
    type: 'hello-ok',
    id: connectRequest.id,
    protocol: 3,
    policy: { tick_interval_ms: 60_000 },
    features: { methods: ['sessions.list'] },
  })
  assert.equal(client.state, 'connected')
  assert.equal(hellos.length, 2)

  staleClose?.()
  assert.equal(client.state, 'connected', 'a retired transport cannot close its replacement')
  client.disconnect()
})

test('removes state listeners after disposal', async () => {
  const states = []
  const { client, sockets } = fixture()
  const off = client.onState((state) => states.push(state))
  const connected = client.connect()
  assert.deepEqual(states, ['connecting'])
  off()

  const socket = sockets[0]
  socket.open()
  socket.receive({ type: 'event', event: 'connect.challenge' })
  const request = JSON.parse(socket.sent[0])
  socket.receive({ type: 'hello-ok', id: request.id, protocol: 3 })
  await connected

  assert.deepEqual(states, ['connecting'])
  client.disconnect()
})

test('isolates observer failures from connection and event delivery', async () => {
  const diagnostics = []
  const delivered = []
  const { client, socket } = await connectFixture({
    onStateChange: (state) => {
      if (state === 'connecting') throw new Error('synthetic state observer failure')
    },
    onHello: () => {
      throw new Error('synthetic Hello observer failure')
    },
    onDiagnostic: (error) => diagnostics.push(error),
  })
  client.on('session.message', () => {
    throw new Error('synthetic event observer failure')
  })
  client.on('session.message', (frame) => delivered.push(frame))

  socket.receive({
    type: 'event',
    event: 'session.message',
    seq: 1,
    payload: { text: 'still delivered' },
  })

  assert.equal(client.state, 'connected')
  assert.equal(delivered.length, 1)
  assert.deepEqual(
    diagnostics.map((error) => error.message),
    [
      'synthetic state observer failure',
      'synthetic Hello observer failure',
      'synthetic event observer failure',
    ],
  )
  client.disconnect()
})
