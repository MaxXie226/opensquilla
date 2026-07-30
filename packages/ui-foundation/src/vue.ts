import {
  computed,
  getCurrentInstance,
  getCurrentScope,
  onMounted,
  onScopeDispose,
  readonly,
  shallowRef,
  type ComputedRef,
  type DeepReadonly,
  type ShallowRef,
} from 'vue'

import {
  createGatewayQuery,
  type GatewayQueryOptions,
} from './gateway/query.js'
import type {
  GatewayStateSnapshot,
  GatewayStateSource,
} from './gateway/types.js'

export type GatewayStateRef = DeepReadonly<ShallowRef<GatewayStateSnapshot>>

export function useGatewayState(source: GatewayStateSource): GatewayStateRef {
  if (!getCurrentScope()) {
    throw new TypeError('useGatewayState requires an active Vue scope')
  }
  const value = shallowRef(source.snapshot)
  const unsubscribe = source.subscribe((snapshot) => {
    value.value = snapshot
  }, false)
  onScopeDispose(unsubscribe)
  return readonly(value)
}

export interface UseGatewayQueryOptions<T>
  extends Omit<GatewayQueryOptions<T>, 'client' | 'method' | 'params'> {
  immediate?: boolean
}

export interface GatewayQueryRefs<T> {
  readonly data: ComputedRef<T | null>
  readonly error: ComputedRef<Error | null>
  readonly loading: ComputedRef<boolean>
  execute(): Promise<T | null>
  refresh(): Promise<T | null>
}

export function useGatewayQuery<T>(
  client: GatewayQueryOptions<T>['client'],
  method: string,
  params?: GatewayQueryOptions<T>['params'],
  options: UseGatewayQueryOptions<T> = {},
): GatewayQueryRefs<T> {
  if (!getCurrentScope()) {
    throw new TypeError('useGatewayQuery requires an active Vue scope')
  }
  const { immediate = true, ...queryOptions } = options
  const query = createGatewayQuery<T>({
    ...queryOptions,
    client,
    method,
    ...(params === undefined ? {} : { params }),
  })
  const snapshot = shallowRef(query.snapshot)
  const unsubscribe = query.subscribe((value) => {
    snapshot.value = value
  }, false)
  onScopeDispose(() => {
    unsubscribe()
    query.dispose()
  })
  if (getCurrentInstance()) {
    onMounted(() => {
      if (immediate) void query.execute()
    })
  } else if (immediate) {
    void query.execute()
  }
  return {
    data: computed(() => snapshot.value.data),
    error: computed(() => snapshot.value.error),
    loading: computed(() => snapshot.value.loading),
    execute: () => query.execute(),
    refresh: () => query.refresh(),
  }
}
