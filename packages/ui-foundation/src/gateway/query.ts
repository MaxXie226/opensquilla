import type {
  GatewayUiCallOptions,
  GatewayUiConnectionWaitOptions,
} from './types.js'

export interface GatewayRequestClient {
  call<T = unknown>(
    method: string,
    params?: Record<string, unknown>,
    options?: GatewayUiCallOptions,
  ): Promise<T>
  waitForConnection(
    timeoutMs?: number,
    signal?: AbortSignal,
    actions?: GatewayUiConnectionWaitOptions,
  ): Promise<void>
}

export interface GatewayQuerySnapshot<T> {
  readonly data: T | null
  readonly error: Error | null
  readonly loading: boolean
}

export type GatewayQueryListener<T> = (
  snapshot: GatewayQuerySnapshot<T>,
) => void

export interface GatewayQueryOptions<T> {
  client: GatewayRequestClient
  method: string
  params?: (
    | Record<string, unknown>
    | (() => Record<string, unknown> | undefined)
  )
  callOptions?: GatewayUiCallOptions | (() => GatewayUiCallOptions | undefined)
  waitTimeoutMs?: number
  waitActions?: GatewayUiConnectionWaitOptions
  onError?: (error: Error) => void
  onSuccess?: (data: T) => void
}

function asError(value: unknown): Error {
  return value instanceof Error ? value : new Error(String(value))
}

function frozenSnapshot<T>(
  data: T | null,
  error: Error | null,
  loading: boolean,
): GatewayQuerySnapshot<T> {
  return Object.freeze({ data, error, loading })
}

export class GatewayQuery<T> {
  private value = frozenSnapshot<T>(null, null, false)
  private readonly listeners = new Set<GatewayQueryListener<T>>()
  private runGeneration = 0
  private disposed = false
  private activeAbort: AbortController | null = null

  constructor(private readonly options: GatewayQueryOptions<T>) {
    if (!options.method) throw new TypeError('Gateway query method is required')
  }

  get snapshot(): GatewayQuerySnapshot<T> {
    return this.value
  }

  subscribe(listener: GatewayQueryListener<T>, emitCurrent = true): () => void {
    if (this.disposed) return () => {}
    this.listeners.add(listener)
    if (emitCurrent) listener(this.value)
    return () => this.listeners.delete(listener)
  }

  execute(): Promise<T | null> {
    return this.run(false)
  }

  refresh(): Promise<T | null> {
    return this.run(true)
  }

  reset(): void {
    if (this.disposed) return
    this.activeAbort?.abort()
    this.activeAbort = null
    this.runGeneration += 1
    this.publish(frozenSnapshot<T>(null, null, false))
  }

  dispose(): void {
    this.disposed = true
    this.activeAbort?.abort()
    this.activeAbort = null
    this.runGeneration += 1
    this.listeners.clear()
  }

  private async run(silent: boolean): Promise<T | null> {
    if (this.disposed) return null
    this.activeAbort?.abort()
    const controller = new AbortController()
    this.activeAbort = controller
    const generation = ++this.runGeneration
    let disposeCombinedSignal = (): void => {}
    this.publish(frozenSnapshot(
      this.value.data,
      null,
      silent ? this.value.loading : true,
    ))
    try {
      await this.options.client.waitForConnection(
        this.options.waitTimeoutMs,
        controller.signal,
        this.options.waitActions,
      )
      const params =
        typeof this.options.params === 'function'
          ? this.options.params()
          : this.options.params
      const callOptions =
        typeof this.options.callOptions === 'function'
          ? this.options.callOptions()
          : this.options.callOptions
      const combined = combineAbortSignals(controller.signal, callOptions?.signal)
      disposeCombinedSignal = combined.dispose
      const result = await this.options.client.call<T>(
        this.options.method,
        params,
        {
          ...callOptions,
          signal: combined.signal,
        },
      )
      if (!this.disposed && generation === this.runGeneration) {
        this.publish(frozenSnapshot(result, null, false))
        try {
          this.options.onSuccess?.(result)
        } catch {
          // Result observers cannot convert a successful request into failure.
        }
      }
      return result
    } catch (value) {
      const error = asError(value)
      if (!this.disposed && generation === this.runGeneration) {
        this.publish(frozenSnapshot(this.value.data, error, false))
        try {
          this.options.onError?.(error)
        } catch {
          // Error observers cannot change the query's null-on-error contract.
        }
      }
      return null
    } finally {
      disposeCombinedSignal()
      if (this.activeAbort === controller) this.activeAbort = null
    }
  }

  private publish(snapshot: GatewayQuerySnapshot<T>): void {
    this.value = snapshot
    for (const listener of this.listeners) listener(snapshot)
  }
}

function combineAbortSignals(
  owned: AbortSignal,
  supplied: AbortSignal | undefined,
): { signal: AbortSignal; dispose(): void } {
  if (!supplied) return { signal: owned, dispose: () => {} }
  const controller = new AbortController()
  const abort = (): void => controller.abort()
  if (owned.aborted || supplied.aborted) {
    controller.abort()
  } else {
    owned.addEventListener('abort', abort, { once: true })
    supplied.addEventListener('abort', abort, { once: true })
  }
  return {
    signal: controller.signal,
    dispose: () => {
      owned.removeEventListener('abort', abort)
      supplied.removeEventListener('abort', abort)
    },
  }
}

export function createGatewayQuery<T>(
  options: GatewayQueryOptions<T>,
): GatewayQuery<T> {
  return new GatewayQuery(options)
}
