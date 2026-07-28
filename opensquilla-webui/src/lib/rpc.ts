/** OpenSquilla Web UI — WebSocket RPC client (TypeScript port). */

import {
  CLIENT_MAX_PROTOCOL,
  CLIENT_MIN_PROTOCOL,
  capabilitiesForMethods,
  normalizeHelloFrame as normalizeSdkHelloFrame,
  type ContractInfo,
  type ErrorShape,
  type NormalizedRuntimeInfo,
  type ProtocolRangeInfo,
} from '@opensquilla/client-sdk';

export { capabilitiesForMethods };
export type RpcErrorDetail = Partial<ErrorShape>;

export interface RpcClientError extends Error {
  code?: string;
  details?: unknown;
  retryable?: boolean;
  retry_after_ms?: number;
  accepted?: boolean;
}

export type RpcContractInfo = ContractInfo;
export type RpcRuntimeInfo = NormalizedRuntimeInfo;
export type RpcProtocolRange = ProtocolRangeInfo;

export interface RpcFrame {
  type?: string;
  id?: string;
  method?: string;
  params?: Record<string, unknown>;
  event?: string;
  payload?: unknown;
  meta?: Record<string, unknown>;
  ok?: boolean;
  error?: string | RpcErrorDetail;
  protocol?: number;
  policy?: Record<string, unknown>;
  features?: {
    methods?: string[];
    events?: string[];
  };
  server?: {
    version?: string;
    conn_id?: string;
  };
  auth?: Record<string, unknown>;
  contract?: Partial<RpcContractInfo>;
  runtime?: Partial<RpcRuntimeInfo>;
  protocolRange?: Partial<RpcProtocolRange>;
  capabilities?: string[];
  extensions?: string[];
  contractStatus?: 'advertised' | 'legacy-contract';
  capabilitySource?: 'hello' | 'features.methods' | 'none';
  seq?: number;
}

export type ConnectionState = 'disconnected' | 'connecting' | 'connected';
export type RpcEventHandler = {
  bivarianceHack(...args: unknown[]): void;
}['bivarianceHack'];

export function normalizeHelloFrame(data: RpcFrame, requestId: string): RpcFrame {
  return normalizeSdkHelloFrame(data, requestId) as unknown as RpcFrame;
}

export class RpcClient {
  private _ws: WebSocket | null = null;
  private _reqId = 0;
  private _pending = new Map<string, { resolve: (v: unknown) => void; reject: (e: Error) => void }>();
  private _listeners = new Map<string, Set<RpcEventHandler>>();
  private _state: ConnectionState = 'disconnected';
  private _url = '';
  private _token: string | null = null;
  private _reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private _reconnectDelay = 800;
  private _maxReconnectDelay = 15000;
  private _reconnectFactor = 1.7;
  private _autoReconnect = true;
  private _pingTimer: ReturnType<typeof setInterval> | null = null;
  private _pingInterval = 55000;
  private _policy: Record<string, unknown> | null = null;
  private _lastSeq = 0;
  private _lastFrameAt = 0;
  private _tickWatchTimer: ReturnType<typeof setInterval> | null = null;
  private _tickTimeoutMs = 60000;
  private _connectRequestId: string | null = null;

  connect(url: string, token?: string): void {
    this._url = url;
    this._token = token || null;
    this._autoReconnect = true;
    this._doConnect();
  }

  disconnect(): void {
    this._autoReconnect = false;
    if (this._reconnectTimer !== null) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
    this._stopPing();
    this._stopTickWatch();
    this._connectRequestId = null;
    if (this._ws) {
      this._ws.close();
      this._ws = null;
    }
    this._setState('disconnected');
  }

  call(method: string, params: Record<string, unknown> = {}): Promise<unknown> {
    return new Promise((resolve, reject) => {
      if (!this._ws || this._ws.readyState !== WebSocket.OPEN) {
        reject(new Error('Not connected'));
        return;
      }
      const id = String(++this._reqId);
      this._pending.set(id, { resolve, reject });
      this._ws.send(JSON.stringify({ type: 'req', id, method, params }));
    });
  }

  on(event: string, handler: RpcEventHandler): () => void {
    if (!this._listeners.has(event)) this._listeners.set(event, new Set());
    this._listeners.get(event)!.add(handler);
    return () => this._listeners.get(event)?.delete(handler);
  }

  get state(): ConnectionState {
    return this._state;
  }

  get policy(): Record<string, unknown> {
    return this._policy || {};
  }

  waitForConnection(timeoutMs: number = 30000): Promise<void> {
    if (this._state === 'connected') return Promise.resolve();
    return new Promise((resolve, reject) => {
      let timer: ReturnType<typeof setTimeout> | null = null;
      const off = this.on('_state', (s: ConnectionState) => {
        if (s === 'connected') {
          if (timer !== null) clearTimeout(timer);
          off();
          resolve();
        }
      });
      if (timeoutMs > 0 && Number.isFinite(timeoutMs)) {
        timer = setTimeout(() => {
          off();
          reject(new Error(`waitForConnection timed out after ${timeoutMs}ms`));
        }, timeoutMs);
      }
    });
  }

  private _doConnect(): void {
    this._setState('connecting');
    this._lastSeq = 0;
    this._lastFrameAt = Date.now();
    this._stopTickWatch();
    this._connectRequestId = null;
    try {
      this._ws = new WebSocket(this._url);
    } catch {
      this._scheduleReconnect();
      return;
    }

    this._ws.onopen = () => {
      this._reconnectDelay = 800;
      // Don't send connect yet — wait for connect.challenge from server
    };

    this._ws.onmessage = (ev: MessageEvent) => {
      let data: RpcFrame;
      try {
        data = JSON.parse(ev.data);
      } catch {
        return;
      }
      if (!this._noteIncomingFrame(data)) return;

      // Handshake: server sends connect.challenge, we reply with connect request
      if (data.type === 'event' && data.event === 'connect.challenge') {
        const authParams = this._token ? { auth: { token: this._token } } : {};
        const id = String(++this._reqId);
        this._connectRequestId = id;
        this._pending.set(id, {
          resolve: () => {},
          reject: (_err: Error) => {
            this._ws?.close();
            this._setState('disconnected');
          },
        });
        this._ws?.send(
          JSON.stringify({
            type: 'req',
            id,
            method: 'connect',
            params: {
              minProtocol: CLIENT_MIN_PROTOCOL,
              maxProtocol: CLIENT_MAX_PROTOCOL,
              client: { name: 'opensquilla-web' },
              ...authParams,
            },
          })
        );
        return;
      }

      // Handshake: only a correlated hello-ok can transition to connected.
      if (
        this._state === 'connecting' &&
        (data.type === 'hello-ok' || data.protocol !== undefined)
      ) {
        const requestId = this._connectRequestId;
        if (!requestId) {
          this._rejectHandshake(new Error('Received hello-ok before connect request'));
          return;
        }
        let hello: RpcFrame;
        try {
          hello = normalizeHelloFrame(data, requestId);
        } catch (error) {
          this._rejectHandshake(error instanceof Error ? error : new Error('Invalid hello-ok'));
          return;
        }
        this._policy = hello.policy || null;
        const pending = this._pending.get(requestId);
        this._pending.delete(requestId);
        this._connectRequestId = null;
        pending?.resolve(hello);
        this._setState('connected');
        const helloHandlers = this._listeners.get('_hello');
        if (helloHandlers) helloHandlers.forEach((h) => h(hello));
        this._startPing();
        this._startTickWatch();
        return;
      }

      if (data.type === 'res') {
        const p = this._pending.get(data.id ?? '');
        if (p) {
          this._pending.delete(data.id!);
          if (data.ok) {
            p.resolve(data.payload);
          } else {
            const err = data.error;
            const message =
              typeof err === 'string'
                ? err
                : (err && (err.message || err.code)) || 'RPC error';
            const error = new Error(message) as RpcClientError;
            if (err && typeof err === 'object') {
              error.code = err.code;
              error.details = err.details;
              error.retryable = err.retryable ?? undefined;
              error.retry_after_ms = err.retry_after_ms ?? undefined;
              error.accepted = err.accepted ?? undefined;
            }
            p.reject(error);
          }
        }
      } else if (data.type === 'event') {
        const meta = data.meta || {};
        const handlers = this._listeners.get(data.event ?? '');
        if (handlers) handlers.forEach((h) => h(data.payload, meta));
        const wild = this._listeners.get('*');
        if (wild) wild.forEach((h) => h(data.event, data.payload, meta));
      }
    };

    this._ws.onclose = () => {
      this._stopPing();
      this._stopTickWatch();
      const pending = [...this._pending.values()];
      this._pending.clear();
      for (const p of pending) p.reject(new Error('Connection closed'));
      this._ws = null;
      this._connectRequestId = null;
      if (this._state !== 'disconnected') {
        this._setState('disconnected');
        this._scheduleReconnect();
      }
    };

    this._ws.onerror = () => {};
  }

  private _rejectHandshake(error: Error): void {
    const requestId = this._connectRequestId;
    if (requestId) {
      const pending = this._pending.get(requestId);
      this._pending.delete(requestId);
      pending?.reject(error);
    }
    this._connectRequestId = null;
    this._autoReconnect = false;
    this._setState('disconnected');
    try {
      this._ws?.close();
    } catch {}
  }

  private _startPing(): void {
    this._stopPing();
    this._pingTimer = setInterval(() => {
      if (this._ws && this._ws.readyState === WebSocket.OPEN) {
        this._ws.send('{"type":"ping"}');
      }
    }, this._pingInterval);
  }

  private _stopPing(): void {
    if (this._pingTimer !== null) {
      clearInterval(this._pingTimer);
      this._pingTimer = null;
    }
  }

  private _noteIncomingFrame(data: RpcFrame): boolean {
    this._lastFrameAt = Date.now();
    if (!data || data.type !== 'event' || typeof data.seq !== 'number') return true;

    const seq = data.seq;
    if (this._lastSeq > 0 && seq !== this._lastSeq + 1) {
      const detail = { expected: this._lastSeq + 1, actual: seq, event: data.event };
      const handlers = this._listeners.get('_gap');
      if (handlers) handlers.forEach((h) => h(detail));
      try {
        this._ws?.close();
      } catch {}
      return false;
    }
    this._lastSeq = seq;
    return true;
  }

  private _startTickWatch(): void {
    this._stopTickWatch();
    const tickMs = (this._policy?.tick_interval_ms as number) || 30000;
    this._tickTimeoutMs = Math.max(10000, tickMs * 2.5);
    this._lastFrameAt = Date.now();
    this._tickWatchTimer = setInterval(() => {
      if (!this._ws || this._ws.readyState !== WebSocket.OPEN) return;
      const idleMs = Date.now() - this._lastFrameAt;
      if (idleMs <= this._tickTimeoutMs) return;
      const handlers = this._listeners.get('_gap');
      if (handlers) handlers.forEach((h) => h({ reason: 'tick_timeout', idleMs }));
      try {
        this._ws.close();
      } catch {}
    }, Math.min(tickMs, 10000));
  }

  private _stopTickWatch(): void {
    if (this._tickWatchTimer !== null) {
      clearInterval(this._tickWatchTimer);
      this._tickWatchTimer = null;
    }
  }

  private _scheduleReconnect(): void {
    if (!this._autoReconnect) return;
    if (this._reconnectTimer !== null) clearTimeout(this._reconnectTimer);
    this._reconnectTimer = setTimeout(() => this._doConnect(), this._reconnectDelay);
    this._reconnectDelay = Math.min(
      this._reconnectDelay * this._reconnectFactor,
      this._maxReconnectDelay
    );
  }

  private _setState(s: ConnectionState): void {
    if (this._state === s) return;
    this._state = s;
    const handlers = this._listeners.get('_state');
    if (handlers) handlers.forEach((h) => h(s));
  }
}
