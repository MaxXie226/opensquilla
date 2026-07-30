import { effectScope } from 'vue'
import { describe, expect, it } from 'vitest'

import {
  GatewayDataStore,
  useGatewayState,
} from '../src/index.js'

describe('useGatewayState', () => {
  it('unsubscribes when its Vue scope is disposed', () => {
    const store = new GatewayDataStore()
    const scope = effectScope()
    const state = scope.run(() => useGatewayState(store))

    expect(state?.value.state).toBe('disconnected')
    store.setConnectionState('connecting')
    expect(state?.value.state).toBe('connecting')

    scope.stop()
    store.setConnectionState('connected')
    expect(state?.value.state).toBe('connecting')
  })

  it('fails fast without a disposable Vue scope', () => {
    expect(() => useGatewayState(new GatewayDataStore())).toThrow(
      'useGatewayState requires an active Vue scope',
    )
  })
})
