import { describe, expect, it } from 'vitest'

import {
  GatewayDataStore,
  createGatewayClient,
} from '../src/index.js'

class FakeWebSocket {
  readonly readyState = 1
  readonly sent: string[] = []
  onopen: (() => void) | null = null
  onmessage: ((event: { data: unknown }) => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null

  send(data: string): void {
    this.sent.push(data)
  }

  close(): void {
    this.onclose?.()
  }

  receive(frame: unknown): void {
    this.onmessage?.({ data: JSON.stringify(frame) })
  }
}

function completeHandshake(socket: FakeWebSocket, method: string): void {
  socket.onopen?.()
  socket.receive({ type: 'event', event: 'connect.challenge' })
  const request = JSON.parse(socket.sent[0]) as { id: string }
  socket.receive({
    type: 'hello-ok',
    id: request.id,
    protocol: 3,
    features: { methods: [method] },
  })
}

describe('createGatewayClient', () => {
  it('binds two clients to independent stores in the same process', async () => {
    const firstStore = new GatewayDataStore()
    const secondStore = new GatewayDataStore()
    const firstSocket = new FakeWebSocket()
    const secondSocket = new FakeWebSocket()
    const bind = (store: GatewayDataStore) => ({
      onStateChange: (state: 'disconnected' | 'connecting' | 'connected') => {
        store.setConnectionState(state)
      },
      onHello: (hello: Parameters<GatewayDataStore['applyHello']>[0]) => {
        store.applyHello(hello)
      },
      onDiagnostic: (error: Error) => {
        store.setDiagnostic(error)
      },
    })
    const firstClient = createGatewayClient({
      endpoint: 'ws://first.test/ws',
      reconnect: { enabled: false },
      webSocketFactory: () => firstSocket,
      bindings: bind(firstStore),
    })
    const secondClient = createGatewayClient({
      endpoint: 'ws://second.test/ws',
      reconnect: { enabled: false },
      webSocketFactory: () => secondSocket,
      bindings: bind(secondStore),
    })

    const firstConnected = firstClient.connect()
    const secondConnected = secondClient.connect()
    completeHandshake(firstSocket, 'sessions.list')
    await firstConnected

    expect(firstStore.snapshot.state).toBe('connected')
    expect(firstStore.snapshot.methods).toEqual(['sessions.list'])
    expect(secondStore.snapshot.state).toBe('connecting')
    expect(secondStore.snapshot.methods).toEqual([])

    completeHandshake(secondSocket, 'usage.status')
    await secondConnected
    expect(secondStore.snapshot.methods).toEqual(['usage.status'])
    expect(firstStore.snapshot.methods).toEqual(['sessions.list'])

    firstClient.disconnect()
    secondClient.disconnect()
  })
})
