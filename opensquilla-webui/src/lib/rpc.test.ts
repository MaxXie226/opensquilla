// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { RpcClient, type RpcClientError } from '@/lib/rpc'

class MockWebSocket {
  static readonly OPEN = 1
  static readonly CLOSED = 3
  static instances: MockWebSocket[] = []

  readonly sent: string[] = []
  readyState = MockWebSocket.OPEN
  onopen: (() => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null

  constructor(readonly url: string) {
    MockWebSocket.instances.push(this)
  }

  send(data: string): void {
    this.sent.push(data)
  }

  close(): void {
    this.readyState = MockWebSocket.CLOSED
    this.onclose?.()
  }

  receive(frame: unknown): void {
    this.onmessage?.({ data: JSON.stringify(frame) } as MessageEvent)
  }
}

describe('RpcClient error responses', () => {
  beforeEach(() => {
    MockWebSocket.instances = []
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('sends the frozen protocol-v3 connect frame after the challenge', () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test', '<synthetic>')
    const socket = MockWebSocket.instances[0]

    socket.receive({
      type: 'event',
      event: 'connect.challenge',
      payload: { nonce: 'synthetic-nonce' },
    })

    expect(JSON.parse(socket.sent[0])).toEqual({
      type: 'req',
      id: '1',
      method: 'connect',
      params: {
        minProtocol: 3,
        maxProtocol: 3,
        client: { name: 'opensquilla-web' },
        auth: { token: '<synthetic>' },
      },
    })

    socket.receive({ type: 'hello-ok', protocol: 3, policy: {} })
    client.disconnect()
  })

  it('accepts a correlated new Hello and exposes normalized capability metadata', () => {
    const client = new RpcClient()
    const hellos: unknown[] = []
    client.on('_hello', (hello) => hellos.push(hello))
    client.connect('ws://rpc.test')
    const socket = MockWebSocket.instances[0]

    socket.receive({ type: 'event', event: 'connect.challenge', payload: { nonce: 'n' } })
    socket.receive({
      type: 'hello-ok',
      id: '1',
      protocol: 3,
      server: { version: '0.5.0' },
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
      protocolRange: { min: 1, max: 3 },
      capabilities: ['gateway.rpc', 'gateway.sessions'],
      extensions: [],
      futureField: { ignored: true },
    })

    expect(client.state).toBe('connected')
    expect(hellos).toHaveLength(1)
    expect(hellos[0]).toMatchObject({
      contractStatus: 'advertised',
      capabilitySource: 'hello',
      capabilities: ['gateway.rpc', 'gateway.sessions'],
      extensions: [],
    })
    client.disconnect()
  })

  it('accepts a legacy Hello without an id and derives only proven capabilities', () => {
    const client = new RpcClient()
    const hellos: unknown[] = []
    client.on('_hello', (hello) => hellos.push(hello))
    client.connect('ws://rpc.test')
    const socket = MockWebSocket.instances[0]

    socket.receive({ type: 'event', event: 'connect.challenge' })
    socket.receive({
      type: 'hello-ok',
      protocol: 3,
      server: { version: '0.4.0' },
      features: {
        methods: ['chat.history', 'chat.send', 'sessions.list', 'sessions.resolve'],
      },
    })

    expect(client.state).toBe('connected')
    expect(hellos[0]).toMatchObject({
      contractStatus: 'legacy-contract',
      capabilitySource: 'features.methods',
      capabilities: ['gateway.rpc', 'gateway.sessions'],
      runtime: { coreVersion: '0.4.0', buildCommit: null },
      protocolRange: { min: 3, max: 3 },
    })
    client.disconnect()
  })

  it.each([
    {
      name: 'a protocol-shaped frame with the wrong type',
      frame: { type: 'res', id: '1', protocol: 3 },
    },
    {
      name: 'a new Hello with a mismatched response id',
      frame: {
        type: 'hello-ok',
        id: 'wrong',
        protocol: 3,
        protocolRange: { min: 1, max: 3 },
      },
    },
    {
      name: 'a new Hello with no response id',
      frame: {
        type: 'hello-ok',
        protocol: 3,
        capabilities: ['gateway.rpc'],
      },
    },
    {
      name: 'a Hello advertising a non-overlapping range',
      frame: {
        type: 'hello-ok',
        id: '1',
        protocol: 3,
        protocolRange: { min: 1, max: 2 },
      },
    },
  ])('rejects $name without reconnecting', ({ frame }) => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    const socket = MockWebSocket.instances[0]

    socket.receive({ type: 'event', event: 'connect.challenge' })
    socket.receive(frame)

    expect(client.state).toBe('disconnected')
    expect(socket.readyState).toBe(MockWebSocket.CLOSED)
    expect(MockWebSocket.instances).toHaveLength(1)
  })

  it('preserves structured retry and acceptance metadata on the rejected error', async () => {
    const client = new RpcClient()
    client.connect('ws://rpc.test')
    const socket = MockWebSocket.instances[0]

    const result = client.call('chat.send', { message: 'hello' })
    const request = JSON.parse(socket.sent[0]) as { id: string }
    socket.receive({
      type: 'res',
      id: request.id,
      ok: false,
      error: {
        code: 'STORAGE_BUSY',
        message: 'Storage is temporarily busy',
        retryable: true,
        retry_after_ms: 250,
        accepted: false,
        details: { operation: 'upsert_session', waited_ms: 2000 },
      },
    })

    let caught: RpcClientError | undefined
    try {
      await result
    } catch (error) {
      caught = error as RpcClientError
    } finally {
      client.disconnect()
    }

    expect(caught).toBeInstanceOf(Error)
    expect(caught).toMatchObject({
      message: 'Storage is temporarily busy',
      code: 'STORAGE_BUSY',
      retryable: true,
      retry_after_ms: 250,
      accepted: false,
      details: { operation: 'upsert_session', waited_ms: 2000 },
    })
  })
})
