# Post-review remediation — Task 1 report

## Outcome

Task 1 closes the anonymous Web control-plane and session-ownership gap.
Unauthenticated callers (including an invalid named Token and a client-claimed
`node` role) now pass through one fail-closed guest policy before normal RPC
scope authorization. Browser and HTTP guest credentials are random, stable per
client, server-derived into an owner id, and never grant owner/config/setup
authority.

## Changes

- Added `GuestRpcPolicy.authorize(method, params, ctx)` with a small explicit
  allowlist. Everything else is denied before the existing scope check.
- Added `guest_owned_session_key(...)` and owner checks using a reserved
  `agent:<agent>:webchat:guest:<sha256-owner-id>:<slug>` namespace.
- Added a 256-bit `osqg_...` browser key handshake. The Web client persists it
  in `localStorage`; the server validates it, derives the owner id with SHA-256,
  and returns a generated compatibility key in the WebSocket hello when needed.
- Added `Principal.guest_owner_id` and a repr-hidden
  `Principal.guest_session_key`. Client-supplied owner ids are ignored.
- Normalized guest `chat.send` keys on the server. Read, abort, bootstrap,
  clarification, and message-subscription methods require the derived owner id.
- Filtered `sessions.list` before task/transcript/workspace enrichment so a
  guest receives only rows in its own namespace.
- Removed guest-supplied `taskId`/`task_id` from `chat.abort`; cancellation is
  performed only by the validated owned session key.
- Added an HttpOnly, SameSite=Strict HTTP guest cookie so repeated `/api/chat`
  and history/list requests use a stable guest identity; separate clients remain
  isolated.
- Normalized the externally visible hello payload so missing and invalid named
  Tokens expose identical guest authority while retaining internal invalid-token
  state for rate limiting and audit.
- Updated earlier guest sandbox tests to exercise the internal send handler;
  the public `sessions.send` RPC is intentionally not guest-allowlisted.

## Guest RPC allowlist

The final allowlist is:

1. `chat.send` — server rewrites non-owned/new keys into the guest namespace.
2. `chat.history` — owned session only.
3. `chat.abort` — owned session only; untrusted task ids are discarded.
4. `chat.clarify_submit` — owned session only; the broker also binds request id
   to session.
5. `sessions.list` — filtered to owned guest rows before enrichment.
6. `sessions.bootstrap` — owned session only.
7. `sessions.messages.subscribe` — owned session only.
8. `sessions.messages.hydrate` — owned session only.
9. `sessions.messages.snapshot` — owned session only.
10. `sessions.messages.unsubscribe` — owned session only.

Global session search/preview/resolve/mutation, global subscriptions, logs,
history indexes, memory, agent files, configuration, setup, sandbox capability,
Token, approval, skill management, and all unknown/future methods are denied.

## TDD evidence

The implementation followed RED → GREEN cycles.

- Baseline: backend `17 passed`; Web RPC client `13 passed`.
- Initial RED: backend `28 failed, 10 passed` because guest policy/owner/hello
  behavior did not exist; Web RPC client `3 failed, 13 passed` because the guest
  key was neither sent nor persisted.
- Additional focused RED cases reproduced:
  - open-auth compatibility key and owner id were generated from different
    random values;
  - legacy test contexts were misclassified as guests;
  - a verified `key` alias could be ignored by a chat handler reading
    `sessionKey`;
  - the guest credential appeared in `Principal.__repr__`;
  - missing and invalid named Tokens differed in the browser hello;
  - anonymous `role=node` bypassed the guest policy;
  - guest abort forwarded an arbitrary task id;
  - HTTP guest identity changed per request;
  - clarification submission was unavailable to an otherwise functional guest
    chat.
- Each reproduction was observed failing before the corresponding minimal fix,
  then rerun green.

## Verification

- `uv run pytest` over the focused auth, guest, WebSocket, HTTP chat, chat
  history/clarification, RPC session/fork, registry logging, and storage-busy
  suites: **360 passed**.
- `npm run test:unit -- src/lib/rpc.test.ts`: **16 passed**.
- Targeted `ruff check`: **passed**.
- Targeted `mypy` for guest policy/auth/app/registry/WebSocket:
  **Success: no issues found in 5 source files**.
- `git diff --check` for Task 1 source/tests: **passed**.
- `npm run typecheck` ran all Web architecture/security/theme/i18n guards
  successfully, then stopped in `vue-tsc` on pre-existing missing `node:fs`
  type declarations in unrelated test files (see concerns).

## Independent security review

The first read-only review found no Critical issues and four Important issues:
anonymous node-role bypass, cross-session task-id abort, unstable HTTP guest
identity, and a blocked clarification flow. All four received failing regression
tests and fixes. The focused re-review ran **47 tests**, found no new Critical or
Important regressions, and assessed the task **Ready**.

## Remaining concerns

- Full Web `vue-tsc --noEmit` remains blocked by the repository's existing
  missing Node type declarations for `node:fs` in five unrelated test files:
  `ClarifyCard.source.test.ts`, `RunTrace.spacing.contracts.test.ts`,
  `SetupProviderCredentialCard.test.ts`, `SetupProviderPanel.test.ts`, and
  `SkillsView.cursor.test.ts`. The Task 1 Web unit test and every architecture
  guard pass.
- `sessions.list` filters the storage page after its global recency query. This
  is fail-closed (no foreign rows are enriched or returned), but an old owned
  guest session can be omitted when it falls outside that global page. A future
  storage-level owner-prefix query would improve pagination without changing
  this security boundary.
