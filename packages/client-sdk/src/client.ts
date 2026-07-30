import type {
  ConnectRequest,
  ErrorShape,
  EventFrame,
  RpcMethod,
} from './generated.js'
import {
  ConnectionClosedError,
  GatewayClientError,
  HandshakeError,
  RequestAbortError,
  RequestTimeoutError,
  SequenceGapError,
} from './errors.js'
import {
  CLIENT_MAX_PROTOCOL,
  CLIENT_MIN_PROTOCOL,
  normalizeHelloFrame,
  type ClientProtocolRange,
  type NormalizedHello,
} from './hello.js'

const WEB_SOCKET_OPEN = 1

export type GatewayConnectionState = 'disconnected' | 'connecting' | 'connected'
export type GatewayEventHandler = (frame: EventFrame) => void
export type GatewayTerminationAction = 'reject' | 'reconnect'

export interface WebSocketMessageLike {
  data: unknown
}

export interface WebSocketLike {
  readonly readyState: number
  onopen: (() => void) | null
  onmessage: ((event: WebSocketMessageLike) => void) | null
  onclose: (() => void) | null
  onerror: (() => void) | null
  send(data: string): void
  close(code?: number, reason?: string): void
}

export type WebSocketFactory = (url: string) => WebSocketLike

export interface ReconnectPolicy {
  enabled?: boolean
  initialDelayMs?: number
  maxDelayMs?: number
  factor?: number
}

export interface GatewayCallOptions {
  timeoutMs?: number
  signal?: AbortSignal
  timeoutAction?: GatewayTerminationAction
  abortAction?: GatewayTerminationAction
  onSent?: (connectionGeneration: number) => void
}

export interface GatewayConnectionWaitOptions {
  timeoutAction?: GatewayTerminationAction
  abortAction?: GatewayTerminationAction
}

export interface GatewayClientOptions {
  endpoint: string
  token?: string
  auth?: Record<string, unknown>
  client?: Record<string, unknown>
  scopes?: string[]
  protocolRange?: ClientProtocolRange
  connectTimeoutMs?: number
  requestTimeoutMs?: number
  keepAliveIntervalMs?: number
  reconnect?: ReconnectPolicy
  /**
   * Compatibility escape hatch for legacy clients that sent RPC requests as
   * soon as the transport opened. New clients should wait for Hello.
   */
  allowCallsDuringHandshake?: boolean
  webSocketFactory?: WebSocketFactory
  onStateChange?: (state: GatewayConnectionState) => void
  onHello?: (hello: NormalizedHello) => void
  onDiagnostic?: (error: Error) => void
}

interface PendingRequest {
  method: string
  resolve(value: unknown): void
  reject(error: Error): void
  timeout: ReturnType<typeof setTimeout> | undefined
  signal: AbortSignal | undefined
  abortHandler: (() => void) | undefined
}

interface ConnectWaiter {
  resolve(hello: NormalizedHello): void
  reject(error: Error): void
}

type JsonRecord = Record<string, unknown>

function defaultWebSocketFactory(url: string): WebSocketLike {
  if (typeof WebSocket !== 'function') {
    throw new TypeError('A WebSocket implementation is required')
  }
  return new WebSocket(url) as unknown as WebSocketLike
}

function isRecord(value: unknown): value is JsonRecord {
  return !!value && typeof value === 'object' && !Array.isArray(value)
}

function parseFrame(data: unknown): JsonRecord | null {
  try {
    if (typeof data === 'string') {
      const parsed: unknown = JSON.parse(data)
      return isRecord(parsed) ? parsed : null
    }
    if (data instanceof ArrayBuffer) {
      const parsed: unknown = JSON.parse(new TextDecoder().decode(data))
      return isRecord(parsed) ? parsed : null
    }
    return isRecord(data) ? data : null
  } catch {
    return null
  }
}

function eventMatches(subscription: string, event: string): boolean {
  if (subscription === '*') return true
  if (subscription.endsWith('.*')) {
    return event.startsWith(subscription.slice(0, -1))
  }
  return subscription === event
}

function asErrorShape(value: unknown): Partial<ErrorShape> | string | null {
  if (typeof value === 'string') return value
  return isRecord(value) ? (value as Partial<ErrorShape>) : null
}

export class GatewayClient {
  private readonly options: GatewayClientOptions
  private readonly webSocketFactory: WebSocketFactory
  private readonly protocolRange: ClientProtocolRange
  private readonly connectTimeoutMs: number
  private readonly requestTimeoutMs: number
  private readonly reconnectEnabled: boolean
  private readonly reconnectInitialDelayMs: number
  private readonly reconnectMaxDelayMs: number
  private readonly reconnectFactor: number
  private readonly eventHandlers = new Map<string, Set<GatewayEventHandler>>()
  private readonly stateHandlers = new Set<(state: GatewayConnectionState) => void>()
  private readonly pending = new Map<string, PendingRequest>()

  private socket: WebSocketLike | null = null
  private connectionState: GatewayConnectionState = 'disconnected'
  private requestId = 0
  private connectionGeneration = 0
  private connectRequestId: string | null = null
  private connectWaiter: ConnectWaiter | null = null
  private manualClose = false
  private reconnectDelayMs: number
  private reconnectTimer: ReturnType<typeof setTimeout> | undefined
  private handshakeTimer: ReturnType<typeof setTimeout> | undefined
  private pingTimer: ReturnType<typeof setInterval> | undefined
  private idleTimer: ReturnType<typeof setInterval> | undefined
  private lastFrameAt = 0
  private lastSequence = 0

  constructor(options: GatewayClientOptions) {
    this.options = options
    this.webSocketFactory = options.webSocketFactory ?? defaultWebSocketFactory
    this.protocolRange = options.protocolRange ?? {
      min: CLIENT_MIN_PROTOCOL,
      max: CLIENT_MAX_PROTOCOL,
    }
    if (
      !Number.isInteger(this.protocolRange.min) ||
      !Number.isInteger(this.protocolRange.max) ||
      this.protocolRange.min > this.protocolRange.max
    ) {
      throw new TypeError('protocolRange must be an ordered integer range')
    }
    this.connectTimeoutMs = options.connectTimeoutMs ?? 30_000
    this.requestTimeoutMs = options.requestTimeoutMs ?? 30_000
    this.reconnectEnabled = options.reconnect?.enabled ?? true
    this.reconnectInitialDelayMs = options.reconnect?.initialDelayMs ?? 800
    this.reconnectMaxDelayMs = options.reconnect?.maxDelayMs ?? 15_000
    this.reconnectFactor = options.reconnect?.factor ?? 1.7
    this.reconnectDelayMs = this.reconnectInitialDelayMs
  }

  get state(): GatewayConnectionState {
    return this.connectionState
  }

  get pendingRequestCount(): number {
    return this.pending.size
  }

  connect(): Promise<NormalizedHello> {
    if (this.connectionState !== 'disconnected') {
      return Promise.reject(new Error(`Gateway client is already ${this.connectionState}`))
    }
    this.manualClose = false
    this.clearReconnect()
    return new Promise<NormalizedHello>((resolve, reject) => {
      this.connectWaiter = { resolve, reject }
      this.openSocket()
    })
  }

  disconnect(): void {
    this.manualClose = true
    this.clearReconnect()
    this.stopHandshakeTimer()
    this.stopHealthChecks()
    this.rejectConnect(new ConnectionClosedError('Gateway client disconnected'))
    this.rejectPending(new ConnectionClosedError('Gateway client disconnected'))
    const socket = this.socket
    this.socket = null
    this.connectRequestId = null
    if (socket) {
      try {
        socket.close(1000, 'client disconnect')
      } catch {
        // The transport is already unusable; local teardown still completes.
      }
    }
    this.setState('disconnected')
  }

  call<T = unknown>(
    method: RpcMethod | (string & {}),
    params: Record<string, unknown> = {},
    timeoutOrOptions: number | GatewayCallOptions = this.requestTimeoutMs,
  ): Promise<T> {
    const options: GatewayCallOptions =
      typeof timeoutOrOptions === 'number'
        ? { timeoutMs: timeoutOrOptions }
        : timeoutOrOptions
    const timeoutMs = options.timeoutMs ?? this.requestTimeoutMs
    const socket = this.socket
    const canSend =
      this.connectionState === 'connected'
      || (
        this.options.allowCallsDuringHandshake === true
        && this.connectionState === 'connecting'
      )
    if (!canSend || !socket || socket.readyState !== WEB_SOCKET_OPEN) {
      return Promise.reject(new ConnectionClosedError('Gateway client is not connected'))
    }
    if (options.signal?.aborted) {
      return Promise.reject(new RequestAbortError(method))
    }
    const id = String(++this.requestId)
    return new Promise<T>((resolve, reject) => {
      const terminate = (
        error: Error,
        action: GatewayTerminationAction,
      ): void => {
        if (!this.rejectRequest(id, error)) return
        if (action === 'reconnect') {
          this.recycleConnection(
            new ConnectionClosedError(`Connection recycled after ${method} terminated`),
          )
        }
      }
      const timeout =
        timeoutMs > 0 && Number.isFinite(timeoutMs)
          ? setTimeout(() => {
              terminate(
                new RequestTimeoutError(id, method, timeoutMs),
                options.timeoutAction ?? 'reject',
              )
            }, timeoutMs)
          : undefined
      const request: PendingRequest = {
        method,
        resolve: (value) => resolve(value as T),
        reject,
        timeout,
        signal: options.signal,
        abortHandler: undefined,
      }
      if (options.signal) {
        request.abortHandler = () => {
          terminate(
            new RequestAbortError(method),
            options.abortAction ?? 'reject',
          )
        }
        options.signal.addEventListener('abort', request.abortHandler, { once: true })
      }
      this.pending.set(id, request)
      try {
        socket.send(JSON.stringify({ type: 'req', id, method, params }))
      } catch (error) {
        const failure = error instanceof Error ? error : new Error(`Unable to send ${method}`)
        this.rejectRequest(id, failure)
        this.recycleConnection(failure)
        return
      }
      try {
        options.onSent?.(this.connectionGeneration)
      } catch {
        // Send receipts are observational and must not fail an accepted call.
      }
    })
  }

  on(eventOrPattern: string, handler: GatewayEventHandler): () => void {
    const handlers = this.eventHandlers.get(eventOrPattern) ?? new Set()
    handlers.add(handler)
    this.eventHandlers.set(eventOrPattern, handlers)
    return () => {
      handlers.delete(handler)
      if (handlers.size === 0) this.eventHandlers.delete(eventOrPattern)
    }
  }

  onState(handler: (state: GatewayConnectionState) => void): () => void {
    this.stateHandlers.add(handler)
    return () => this.stateHandlers.delete(handler)
  }

  waitForConnection(
    timeoutMs = 30_000,
    signal?: AbortSignal,
    actions: GatewayConnectionWaitOptions = {},
  ): Promise<void> {
    if (signal?.aborted) {
      return Promise.reject(new RequestAbortError('waitForConnection'))
    }
    if (this.connectionState === 'connected') return Promise.resolve()

    return new Promise((resolve, reject) => {
      let settled = false
      let timeout: ReturnType<typeof setTimeout> | undefined
      let off = (): void => {}
      const cleanup = (): void => {
        if (timeout !== undefined) clearTimeout(timeout)
        off()
        signal?.removeEventListener('abort', onAbort)
      }
      const finish = (
        error?: Error,
        action: GatewayTerminationAction = 'reject',
      ): void => {
        if (settled) return
        settled = true
        cleanup()
        if (!error) {
          resolve()
          return
        }
        reject(error)
        if (action === 'reconnect' && this.connectionState !== 'connected') {
          this.recycleConnection(
            new ConnectionClosedError('Connection recycled after waitForConnection terminated'),
          )
        }
      }
      const onAbort = (): void => {
        finish(
          new RequestAbortError('waitForConnection'),
          actions.abortAction ?? 'reject',
        )
      }

      off = this.onState((state) => {
        if (state === 'connected') finish()
      })
      signal?.addEventListener('abort', onAbort, { once: true })
      if (timeoutMs > 0 && Number.isFinite(timeoutMs)) {
        timeout = setTimeout(() => {
          finish(
            new RequestTimeoutError('', 'waitForConnection', timeoutMs),
            actions.timeoutAction ?? 'reject',
          )
        }, timeoutMs)
      }
    })
  }

  private openSocket(): void {
    this.setState('connecting')
    this.lastFrameAt = Date.now()
    this.lastSequence = 0
    this.connectRequestId = null
    this.stopHealthChecks()
    this.connectionGeneration += 1
    const generation = this.connectionGeneration
    try {
      const socket = this.webSocketFactory(this.options.endpoint)
      this.socket = socket
      socket.onopen = () => {
        if (this.isCurrentSocket(socket, generation)) {
          this.reconnectDelayMs = this.reconnectInitialDelayMs
        }
      }
      socket.onmessage = (event) => {
        if (this.isCurrentSocket(socket, generation)) this.handleMessage(event.data)
      }
      socket.onclose = () => this.handleClose(socket, generation)
      socket.onerror = () => {}
      this.startHandshakeTimer()
    } catch (error) {
      const failure = error instanceof Error ? error : new Error('Unable to open WebSocket')
      this.socket = null
      this.setState('disconnected')
      this.rejectConnect(failure)
      this.scheduleReconnect()
    }
  }

  private handleMessage(rawData: unknown): void {
    const frame = parseFrame(rawData)
    if (!frame) return
    this.lastFrameAt = Date.now()

    if (frame.type === 'event' && frame.event === 'connect.challenge') {
      this.sendConnect()
      return
    }
    if (
      this.connectionState === 'connecting' &&
      (frame.type === 'hello-ok' || frame.protocol !== undefined)
    ) {
      this.acceptHello(frame)
      return
    }
    if (
      this.connectionState === 'connecting' &&
      frame.type === 'res' &&
      frame.id === this.connectRequestId
    ) {
      const error =
        frame.ok === false
          ? new GatewayClientError(asErrorShape(frame.error))
          : new HandshakeError('Connect returned a response without hello-ok')
      this.failHandshake(error)
      return
    }
    if (frame.type === 'res') {
      this.acceptResponse(frame)
      return
    }
    if (frame.type === 'event') {
      this.acceptEvent(frame)
      return
    }
    if (frame.type === 'ping' && this.socket?.readyState === WEB_SOCKET_OPEN) {
      this.socket.send('{"type":"pong"}')
    }
  }

  private sendConnect(): void {
    if (this.connectRequestId || !this.socket || this.socket.readyState !== WEB_SOCKET_OPEN) return
    const id = String(++this.requestId)
    this.connectRequestId = id
    const auth = { ...(this.options.auth ?? {}) }
    if (this.options.token) auth.token = this.options.token
    const params: ConnectRequest['params'] = {
      minProtocol: this.protocolRange.min,
      maxProtocol: this.protocolRange.max,
      client: this.options.client ?? { name: 'opensquilla-sdk' },
    }
    if (Object.keys(auth).length > 0) params.auth = auth
    if (this.options.scopes) params.scopes = [...this.options.scopes]
    const request: ConnectRequest = { type: 'req', id, method: 'connect', params }
    try {
      this.socket.send(JSON.stringify(request))
    } catch (error) {
      this.failHandshake(
        error instanceof Error ? error : new HandshakeError('Unable to send connect request'),
      )
    }
  }

  private acceptHello(frame: JsonRecord): void {
    const requestId = this.connectRequestId
    if (!requestId) {
      this.failHandshake(new HandshakeError('Received hello-ok before connect request'))
      return
    }
    try {
      const hello = normalizeHelloFrame(frame, requestId, this.protocolRange)
      this.stopHandshakeTimer()
      this.connectRequestId = null
      this.setState('connected')
      this.startHealthChecks(hello)
      try {
        this.options.onHello?.(hello)
      } catch (error) {
        this.notifyDiagnostic(
          error instanceof Error ? error : new Error('Gateway Hello observer failed'),
        )
      }
      this.connectWaiter?.resolve(hello)
      this.connectWaiter = null
    } catch (error) {
      this.failHandshake(
        error instanceof Error ? error : new HandshakeError('Invalid hello-ok frame'),
      )
    }
  }

  private acceptResponse(frame: JsonRecord): void {
    const id = typeof frame.id === 'string' ? frame.id : ''
    const pending = this.takeRequest(id)
    if (!pending) return
    if (frame.ok === true) {
      pending.resolve(frame.payload)
    } else {
      pending.reject(new GatewayClientError(asErrorShape(frame.error)))
    }
  }

  private acceptEvent(frame: JsonRecord): void {
    const event = typeof frame.event === 'string' ? frame.event : ''
    if (!event) return
    if (typeof frame.seq === 'number' && Number.isInteger(frame.seq)) {
      if (this.lastSequence > 0 && frame.seq !== this.lastSequence + 1) {
        const error = new SequenceGapError(this.lastSequence + 1, frame.seq, event)
        this.notifyDiagnostic(error)
        try {
          this.socket?.close(1012, 'event sequence gap')
        } catch {
          if (this.socket) this.handleClose(this.socket, this.connectionGeneration)
        }
        return
      }
      this.lastSequence = frame.seq
    }
    const typedFrame = frame as unknown as EventFrame
    for (const [subscription, handlers] of this.eventHandlers) {
      if (!eventMatches(subscription, event)) continue
      for (const handler of handlers) {
        try {
          handler(typedFrame)
        } catch (error) {
          this.notifyDiagnostic(
            error instanceof Error ? error : new Error(`Gateway event observer failed: ${event}`),
          )
        }
      }
    }
  }

  private failHandshake(error: Error): void {
    this.manualClose = true
    this.stopHandshakeTimer()
    this.rejectConnect(error)
    this.setState('disconnected')
    try {
      this.socket?.close(1002, 'invalid hello')
    } catch {
      this.socket = null
    }
  }

  private handleClose(socket: WebSocketLike, generation: number): void {
    if (!this.isCurrentSocket(socket, generation)) return
    this.stopHandshakeTimer()
    this.stopHealthChecks()
    this.socket = null
    this.connectRequestId = null
    this.rejectPending(new ConnectionClosedError())
    this.rejectConnect(new ConnectionClosedError('Gateway closed during handshake'))
    this.setState('disconnected')
    this.scheduleReconnect()
  }

  private startHealthChecks(hello: NormalizedHello): void {
    this.stopHealthChecks()
    const pingMs = this.options.keepAliveIntervalMs ?? 55_000
    if (pingMs > 0 && Number.isFinite(pingMs)) {
      this.pingTimer = setInterval(() => {
        if (this.socket?.readyState === WEB_SOCKET_OPEN) {
          this.socket.send('{"type":"ping"}')
        }
      }, pingMs)
    }
    const tickMs =
      typeof hello.policy.tick_interval_ms === 'number'
        ? hello.policy.tick_interval_ms
        : 30_000
    const idleTimeoutMs = Math.max(10_000, tickMs * 2.5)
    this.idleTimer = setInterval(() => {
      if (
        this.socket?.readyState === WEB_SOCKET_OPEN &&
        Date.now() - this.lastFrameAt > idleTimeoutMs
      ) {
        this.notifyDiagnostic(new ConnectionClosedError('Gateway tick timed out'))
        this.socket.close(1012, 'tick timeout')
      }
    }, Math.min(tickMs, 10_000))
  }

  private stopHealthChecks(): void {
    if (this.pingTimer !== undefined) clearInterval(this.pingTimer)
    if (this.idleTimer !== undefined) clearInterval(this.idleTimer)
    this.pingTimer = undefined
    this.idleTimer = undefined
  }

  private startHandshakeTimer(): void {
    this.stopHandshakeTimer()
    if (this.connectTimeoutMs <= 0 || !Number.isFinite(this.connectTimeoutMs)) return
    this.handshakeTimer = setTimeout(() => {
      const error = new HandshakeError(
        `Gateway handshake timed out after ${this.connectTimeoutMs}ms`,
      )
      this.notifyDiagnostic(error)
      this.rejectConnect(error)
      try {
        this.socket?.close(1002, 'handshake timeout')
      } catch {
        if (this.socket) this.handleClose(this.socket, this.connectionGeneration)
      }
    }, this.connectTimeoutMs)
  }

  private stopHandshakeTimer(): void {
    if (this.handshakeTimer !== undefined) clearTimeout(this.handshakeTimer)
    this.handshakeTimer = undefined
  }

  private scheduleReconnect(immediate = false): void {
    if (this.manualClose || !this.reconnectEnabled || this.reconnectTimer !== undefined) return
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = undefined
      this.openSocket()
    }, immediate ? 0 : this.reconnectDelayMs)
    if (immediate) return
    this.reconnectDelayMs = Math.min(
      this.reconnectDelayMs * this.reconnectFactor,
      this.reconnectMaxDelayMs,
    )
  }

  private recycleConnection(reason: Error): void {
    const socket = this.socket
    if (!socket) {
      this.setState('disconnected')
      this.scheduleReconnect(true)
      return
    }
    this.stopHandshakeTimer()
    this.stopHealthChecks()
    this.socket = null
    this.connectRequestId = null
    this.rejectPending(reason)
    this.rejectConnect(reason)
    this.setState('disconnected')
    try {
      socket.close(1012, 'client reconnect')
    } catch {}
    this.scheduleReconnect(true)
  }

  private clearReconnect(): void {
    if (this.reconnectTimer !== undefined) clearTimeout(this.reconnectTimer)
    this.reconnectTimer = undefined
  }

  private isCurrentSocket(socket: WebSocketLike, generation: number): boolean {
    return this.socket === socket && this.connectionGeneration === generation
  }

  private rejectConnect(error: Error): void {
    this.connectWaiter?.reject(error)
    this.connectWaiter = null
  }

  private rejectPending(error: Error): void {
    for (const id of [...this.pending.keys()]) {
      this.rejectRequest(id, error)
    }
  }

  private takeRequest(id: string): PendingRequest | undefined {
    const request = this.pending.get(id)
    if (!request) return undefined
    this.pending.delete(id)
    if (request.timeout !== undefined) clearTimeout(request.timeout)
    if (request.signal && request.abortHandler) {
      request.signal.removeEventListener('abort', request.abortHandler)
    }
    return request
  }

  private rejectRequest(id: string, error: Error): boolean {
    const request = this.takeRequest(id)
    if (!request) return false
    request.reject(error)
    return true
  }

  private setState(state: GatewayConnectionState): void {
    if (this.connectionState === state) return
    this.connectionState = state
    try {
      this.options.onStateChange?.(state)
    } catch (error) {
      this.notifyDiagnostic(
        error instanceof Error ? error : new Error('Gateway state observer failed'),
      )
    }
    for (const handler of this.stateHandlers) {
      try {
        handler(state)
      } catch (error) {
        this.notifyDiagnostic(
          error instanceof Error ? error : new Error('Gateway state observer failed'),
        )
      }
    }
  }

  private notifyDiagnostic(error: Error): void {
    try {
      this.options.onDiagnostic?.(error)
    } catch {
      // Diagnostics are observational and cannot alter transport lifecycle.
    }
  }
}
