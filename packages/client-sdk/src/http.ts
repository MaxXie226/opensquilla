import { GatewayHttpError } from './errors.js'

export type FetchLike = (
  input: string | URL | Request,
  init?: RequestInit,
) => Promise<Response>

export interface GatewayHttpClientOptions {
  baseUrl: string
  fetch?: FetchLike
  headers?: HeadersInit
}

async function responseBody(response: Response): Promise<unknown> {
  const contentType = response.headers.get('content-type') ?? ''
  if (contentType.includes('application/json')) {
    try {
      return await response.json()
    } catch {
      return null
    }
  }
  try {
    return await response.text()
  } catch {
    return null
  }
}

export class GatewayHttpClient {
  private readonly baseUrl: string
  private readonly fetcher: FetchLike
  private readonly headers: Headers

  constructor(options: GatewayHttpClientOptions) {
    this.baseUrl = options.baseUrl.replace(/\/+$/, '')
    const fetcher = options.fetch ?? globalThis.fetch
    if (typeof fetcher !== 'function') {
      throw new TypeError('A fetch implementation is required')
    }
    this.fetcher = fetcher.bind(globalThis)
    this.headers = new Headers(options.headers)
  }

  async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers = new Headers(this.headers)
    new Headers(init.headers).forEach((value, key) => headers.set(key, value))
    const url = `${this.baseUrl}/${path.replace(/^\/+/, '')}`
    const response = await this.fetcher(url, { ...init, headers })
    const body = await responseBody(response)
    if (!response.ok) {
      throw new GatewayHttpError(
        response.status,
        body,
        response.statusText || `Gateway HTTP ${response.status}`,
      )
    }
    return body as T
  }

  get<T>(path: string, init: Omit<RequestInit, 'method'> = {}): Promise<T> {
    return this.request<T>(path, { ...init, method: 'GET' })
  }

  post<T>(
    path: string,
    body: unknown,
    init: Omit<RequestInit, 'body' | 'method'> = {},
  ): Promise<T> {
    const headers = new Headers(init.headers)
    if (!headers.has('content-type')) headers.set('content-type', 'application/json')
    return this.request<T>(path, {
      ...init,
      method: 'POST',
      headers,
      body: JSON.stringify(body),
    })
  }
}
