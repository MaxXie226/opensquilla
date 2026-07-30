import type { Pinia } from 'pinia'
import type { InjectionKey } from 'vue'
import {
  createOpenSquillaApp,
  type OpenSquillaAppComposition,
} from '@opensquilla/ui-foundation'
import type { Platform } from '@/platform'
import { createPublicWebUiFeatures } from './catalog'
import {
  APP_STATE_NAMESPACE,
  PUBLIC_WEB_UI_PINIA_SERVICE,
  PUBLIC_WEB_UI_PLATFORM_SERVICE,
  RPC_STATE_NAMESPACE,
  WORKBENCH_STATE_NAMESPACE,
  type PublicWebUiAppStore,
  type PublicWebUiRpcStore,
  type PublicWebUiWorkbenchState,
} from './state'
import {
  PUBLIC_WEB_UI_NATIVE_CAPABILITIES,
  createPublicWebUiNativeAdapter,
} from './nativeAdapter'

export const PUBLIC_WEB_UI_COMPOSITION_KEY: InjectionKey<OpenSquillaAppComposition> = (
  Symbol('opensquilla.public-webui.composition')
)

export interface CreatePublicWebUiCompositionOptions {
  readonly pinia: Pinia
  readonly platform: Platform
  readonly gatewayScopes?: readonly string[]
}

export interface PublicWebUiRuntimeState {
  readonly appStore: PublicWebUiAppStore
  readonly rpcStore: PublicWebUiRpcStore
  readonly workbench: PublicWebUiWorkbenchState
}

export async function createPublicWebUiComposition(
  options: CreatePublicWebUiCompositionOptions,
): Promise<OpenSquillaAppComposition> {
  const native = createPublicWebUiNativeAdapter(options.platform)
  return await createOpenSquillaApp({
    features: createPublicWebUiFeatures(options.platform),
    knownCapabilities: PUBLIC_WEB_UI_NATIVE_CAPABILITIES,
    capabilities: native.capabilities,
    gatewayScopes: options.gatewayScopes,
    native,
    services: {
      [PUBLIC_WEB_UI_PINIA_SERVICE]: options.pinia,
      [PUBLIC_WEB_UI_PLATFORM_SERVICE]: options.platform,
    },
  })
}

export function getPublicWebUiRuntimeState(
  composition: OpenSquillaAppComposition,
): PublicWebUiRuntimeState {
  return {
    appStore: composition.getState<PublicWebUiAppStore>(APP_STATE_NAMESPACE),
    rpcStore: composition.getState<PublicWebUiRpcStore>(RPC_STATE_NAMESPACE),
    workbench: composition.getState<PublicWebUiWorkbenchState>(
      WORKBENCH_STATE_NAMESPACE,
    ),
  }
}
