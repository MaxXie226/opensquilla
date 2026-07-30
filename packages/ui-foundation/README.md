# OpenSquilla UI Foundation

Public, product-neutral Gateway client bindings and shared UI data
infrastructure.

The package exposes injectable Gateway WebSocket and HTTP client factories,
isolated state-store factories, a reusable query lifecycle, browser-safe
connection settings storage, and small Vue state/query adapters. It does not
own product routes, pages, manifests, native bridges, or private application
state.

Create one client and one store scope per product composition root. Connection
tokens are session-only when using `createGatewayConnectionStorage`; native
credential storage remains the responsibility of a product adapter.

```ts
import {
  GatewayDataStore,
  createGatewayClient,
  createGatewayQuery,
} from '@opensquilla/ui-foundation'

const gateway = new GatewayDataStore()
const client = createGatewayClient({
  endpoint: 'ws://127.0.0.1:18791/ws',
  bindings: {
    onStateChange: state => gateway.setConnectionState(state),
    onHello: hello => gateway.applyHello(hello),
    onDiagnostic: error => gateway.setDiagnostic(error),
  },
})

await client.connect()
const sessions = createGatewayQuery({
  client,
  method: 'sessions.list',
})
await sessions.execute()
```

Only the package root is public. Imports from `src/` or other internal paths
are unsupported.
