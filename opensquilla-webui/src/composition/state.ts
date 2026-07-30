import type { Pinia } from 'pinia'
import type {
  StateContribution,
} from '@opensquilla/ui-foundation'
import { useAppStore } from '@/stores/app'
import { useRpcStore } from '@/stores/rpc'
import { createWorkbenchPanelRegistry } from '@/workbench/registry'
import type { WorkbenchPanelRegistry } from '@/workbench/runtime'
import { useWorkbenchStore } from '@/workbench/store'

export const PUBLIC_WEB_UI_PINIA_SERVICE = 'opensquilla.webui.pinia'
export const PUBLIC_WEB_UI_PLATFORM_SERVICE = 'opensquilla.webui.platform'

export const APP_STATE_NAMESPACE = 'opensquilla.webui.shell.app'
export const RPC_STATE_NAMESPACE = 'opensquilla.webui.shell.rpc'
export const WORKBENCH_STATE_NAMESPACE = 'opensquilla.webui.workbench.runtime'

export type PublicWebUiAppStore = ReturnType<typeof useAppStore>
export type PublicWebUiRpcStore = ReturnType<typeof useRpcStore>
export type PublicWebUiWorkbenchStore = ReturnType<typeof useWorkbenchStore>

export interface PublicWebUiWorkbenchState {
  readonly store: PublicWebUiWorkbenchStore
  readonly registry: WorkbenchPanelRegistry
}

function requiredPinia(getService: <T = unknown>(id: string) => T | undefined): Pinia {
  const pinia = getService<Pinia>(PUBLIC_WEB_UI_PINIA_SERVICE)
  if (!pinia) {
    throw new Error('The public WebUI composition requires an isolated Pinia instance')
  }
  return pinia
}

export function createPublicWebUiStateContributions(): readonly StateContribution[] {
  return [
    {
      id: 'opensquilla.webui.shell.state.app',
      namespace: APP_STATE_NAMESPACE,
      order: 10,
      create({ getService }) {
        const store = useAppStore(requiredPinia(getService))
        return {
          value: store,
          dispose() {
            store.destroyTheme()
          },
        }
      },
    },
    {
      id: 'opensquilla.webui.shell.state.rpc',
      namespace: RPC_STATE_NAMESPACE,
      order: 20,
      create({ getService }) {
        const store = useRpcStore(requiredPinia(getService))
        return {
          value: store,
          dispose() {
            store.disconnect()
          },
        }
      },
    },
  ]
}

export function createPublicWebUiWorkbenchStateContribution(): StateContribution {
  return {
    id: 'opensquilla.webui.workbench.state.runtime',
    namespace: WORKBENCH_STATE_NAMESPACE,
    order: 10,
    create({ getService }) {
      const store = useWorkbenchStore(requiredPinia(getService))
      const registry = createWorkbenchPanelRegistry()
      return {
        value: { store, registry } satisfies PublicWebUiWorkbenchState,
        dispose() {
          store.reset()
          registry.clear()
        },
      }
    },
  }
}
