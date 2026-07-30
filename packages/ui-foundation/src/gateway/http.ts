import {
  GatewayHttpClient,
  type GatewayHttpClientOptions,
} from '@opensquilla/client-sdk'

export { GatewayHttpClient }
export type { GatewayHttpClientOptions }

export function createGatewayHttpClient(
  options: GatewayHttpClientOptions,
): GatewayHttpClient {
  return new GatewayHttpClient(options)
}
