# OpenSquilla UI Foundation

Public, product-neutral Gateway bindings, shared UI data infrastructure, and
static application-composition contracts.

The package exposes injectable Gateway WebSocket and HTTP client factories,
isolated state-store factories, a reusable query lifecycle, browser-safe
connection settings storage, and small Vue state/query adapters. It does not
own product routes, pages, manifests, native bridges, or private application
state.

Feature declarations can contribute framework-neutral pages, routes,
navigation, and scoped state factories. The registrar validates namespaced
identifiers, dependency cycles, route collisions, state ownership, capability
names, and deterministic ordering before any state factory runs. Each product
chooses its own statically linked feature set; this package does not fetch or
execute remote modules.

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

```ts
import {
  UI_COMPOSITION_API_VERSION,
  createOpenSquillaApp,
} from '@opensquilla/ui-foundation'

const app = await createOpenSquillaApp({
  features: [{
    id: 'community.status',
    apiVersion: UI_COMPOSITION_API_VERSION,
    contributions: {
      pages: [{
        id: 'community.status.page.main',
        load: () => import('./StatusPage.js'),
      }],
      routes: [{
        id: 'community.status.route.main',
        path: '/status',
        name: 'status',
        pageId: 'community.status.page.main',
      }],
    },
  }],
})
```

Native operations cross a versioned `NativeCapabilityAdapter`. Browser hosts
use `createWebNativeCapabilityAdapter()`, which always returns a structured
`unsupported` result. Capability and scope availability only controls
presentation; Gateway and host authorization remain authoritative.

Only the package root is public. Imports from `src/` or other internal paths
are unsupported.
