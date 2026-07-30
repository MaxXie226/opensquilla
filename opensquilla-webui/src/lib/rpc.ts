/**
 * Public WebUI compatibility entry for the product-neutral Gateway client.
 *
 * Keep application imports on this module while the implementation and public
 * contract live in @opensquilla/ui-foundation.
 */
export {
  RpcAbortError,
  RpcClient,
  RpcTimeoutError,
  capabilitiesForMethods,
  normalizeHelloFrame,
} from '@opensquilla/ui-foundation'

export type {
  ConnectionState,
  RpcCallOptions,
  RpcClientError,
  RpcConnectionWaitOptions,
  RpcContractInfo,
  RpcErrorDetail,
  RpcEventHandler,
  RpcFrame,
  RpcProtocolRange,
  RpcRuntimeInfo,
  RpcTerminationAction,
} from '@opensquilla/ui-foundation'
