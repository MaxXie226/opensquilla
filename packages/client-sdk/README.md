# OpenSquilla Client SDK

Product-neutral TypeScript client for the OpenSquilla Gateway protocol. It
contains generated protocol-v3 envelopes and catalogs, Hello negotiation,
structured errors, a reconnecting WebSocket transport, and a small HTTP
transport.

```ts
import { GatewayClient } from '@opensquilla/client-sdk'

const client = new GatewayClient({
  endpoint: 'ws://127.0.0.1:18791/ws',
  token: process.env.OPENSQUILLA_TOKEN,
})

await client.connect()
const sessions = await client.call('sessions.list')
client.disconnect()
```

The package has no runtime dependencies and contains no Vue, Pinia, Electron,
or product UI code. `contract-coverage.json` records which parts of the
Gateway surface are already typed and which payload families remain deferred.

Regenerate committed contract artifacts after changing the Gateway surface:

```sh
uv run python scripts/generate_client_contracts.py
uv run python scripts/generate_client_contracts.py --check
```
