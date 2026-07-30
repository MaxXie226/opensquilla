import type { ErrorShape } from './generated.js'

function messageForError(error: Partial<ErrorShape> | string | null | undefined): string {
  if (typeof error === 'string' && error) return error
  if (error && typeof error === 'object') {
    if (typeof error.message === 'string' && error.message) return error.message
    if (typeof error.code === 'string' && error.code) return error.code
  }
  return 'Gateway request failed'
}

export class GatewayClientError extends Error {
  readonly code?: string
  readonly details?: unknown
  readonly retryable?: boolean
  readonly retryAfterMs?: number
  readonly accepted?: boolean

  constructor(
    error: Partial<ErrorShape> | string | null | undefined,
    options?: ErrorOptions,
  ) {
    super(messageForError(error), options)
    this.name = 'GatewayClientError'
    if (error && typeof error === 'object') {
      if (typeof error.code === 'string') this.code = error.code
      if (Object.prototype.hasOwnProperty.call(error, 'details')) this.details = error.details
      if (typeof error.retryable === 'boolean') this.retryable = error.retryable
      if (typeof error.retry_after_ms === 'number') this.retryAfterMs = error.retry_after_ms
      if (typeof error.accepted === 'boolean') this.accepted = error.accepted
    }
  }
}

export class HandshakeError extends Error {
  constructor(message: string, options?: ErrorOptions) {
    super(message, options)
    this.name = 'HandshakeError'
  }
}

export class ConnectionClosedError extends Error {
  constructor(message = 'Gateway connection closed', options?: ErrorOptions) {
    super(message, options)
    this.name = 'ConnectionClosedError'
  }
}

export class RequestTimeoutError extends Error {
  readonly code = 'RPC_TIMEOUT'
  readonly requestId: string
  readonly method: string
  readonly timeoutMs: number

  constructor(method: string, timeoutMs: number)
  constructor(requestId: string, method: string, timeoutMs: number)
  constructor(
    requestIdOrMethod: string,
    methodOrTimeout: string | number,
    optionalTimeout?: number,
  ) {
    const requestId = optionalTimeout === undefined ? '' : requestIdOrMethod
    const method = optionalTimeout === undefined
      ? requestIdOrMethod
      : methodOrTimeout as string
    const timeoutMs = optionalTimeout === undefined
      ? methodOrTimeout as number
      : optionalTimeout
    super(`Gateway request ${method} timed out after ${timeoutMs}ms`)
    this.name = 'RequestTimeoutError'
    this.requestId = requestId
    this.method = method
    this.timeoutMs = timeoutMs
  }
}

export class RequestAbortError extends Error {
  readonly code = 'RPC_ABORTED'
  readonly method: string

  constructor(method: string) {
    super(`${method} was aborted`)
    this.name = 'RequestAbortError'
    this.method = method
  }
}

export class SequenceGapError extends Error {
  readonly expected: number
  readonly actual: number
  readonly event?: string

  constructor(expected: number, actual: number, event?: string) {
    super(`Gateway event sequence gap: expected ${expected}, received ${actual}`)
    this.name = 'SequenceGapError'
    this.expected = expected
    this.actual = actual
    if (event !== undefined) this.event = event
  }
}

export class GatewayHttpError extends GatewayClientError {
  readonly status: number
  readonly responseBody: unknown

  constructor(status: number, responseBody: unknown, fallbackMessage: string) {
    const candidate =
      responseBody && typeof responseBody === 'object'
        ? (
            'error' in responseBody
              ? (responseBody as { error?: unknown }).error
              : responseBody
          )
        : fallbackMessage
    super(
      typeof candidate === 'string' || (candidate && typeof candidate === 'object')
        ? (candidate as Partial<ErrorShape> | string)
        : fallbackMessage,
    )
    this.name = 'GatewayHttpError'
    this.status = status
    this.responseBody = responseBody
  }
}
