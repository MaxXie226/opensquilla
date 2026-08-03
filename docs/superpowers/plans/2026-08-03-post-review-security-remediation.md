# Post-review Security Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make anonymous Web access session-isolated and make Safe-mode readiness fail closed while preserving Full-by-default owner behavior, confined Windows guest file operations, responsive Settings, and private upgrades.

**Architecture:** A central guest RPC policy combines an invisible per-browser guest key with server-owned session namespaces. Native capability probes exercise the sandbox identity directly. Preference, Windows ACL projection, UI retry, and migration hardening changes are independent, testable tasks.

**Tech Stack:** Python 3.12+, asyncio, SQLite/session storage, Vue 3, TypeScript, Vitest, pytest, Windows ACL/WFP helpers, Electron packaging.

## Global Constraints

- Missing and invalid named Tokens must have identical restricted Safe-mode authority.
- Guest session keys are invisible and can never grant host execution, setup, configuration, approval authority, or file access beyond the guest's managed temporary workspace. Bundled read/execute runtimes are available only on platforms whose native backend enforces explicit read and write mounts; Windows guest process execution is unavailable.
- Desktop startup and remote Web access must never trigger UAC.
- Existing owner/token sessions and direct-update data remain compatible.
- Every security denial is enforced server-side; hiding UI is not authorization.

---

### Task 1: Guest RPC and session ownership boundary

**Files:**
- Create: `src/opensquilla/gateway/guest_rpc_policy.py`
- Modify: `src/opensquilla/gateway/auth.py`
- Modify: `src/opensquilla/gateway/rpc/registry.py`
- Modify: `src/opensquilla/gateway/websocket.py`
- Modify: `src/opensquilla/gateway/rpc_chat.py`
- Modify: `src/opensquilla/gateway/rpc_sessions.py`
- Modify: `opensquilla-webui/src/lib/rpc.ts`
- Test: `tests/test_gateway/test_guest_rpc_policy.py`
- Test: relevant auth/WebSocket/chat tests and `opensquilla-webui/src/lib/rpc.test.ts`

**Interfaces:**
- Produces `GuestRpcPolicy.authorize(method, params, ctx)`, `guest_owned_session_key(...)`, and a shared browser `guestSessionKey` handshake field.
- Consumes `Principal.auth_state`, `Principal.capabilities`, canonical Web chat session keys, and session storage.

- [ ] Write failing tests proving a guest cannot call global sessions/history/logs/memory/agent/config/setup methods, cannot adopt an owner session, and can create/read only its own guest-namespaced session; invalid Token behavior must match no Token.
- [ ] Verify the new tests fail against the current `REMOTE_OPERATOR_SCOPES` behavior.
- [ ] Implement the guest key handshake, guest owner id, central allowlist/key guard, server-side new-session rewrite, and filtered guest session list.
- [ ] Add Web client persistence tests and focused Gateway/Web tests; require all to pass.
- [ ] Request independent security review and commit `fix: isolate anonymous Web control plane`.

### Task 2: Native fail-closed capability canaries

**Files:**
- Modify: `src/opensquilla/sandbox/setup_runtime.py`
- Modify: `src/opensquilla/sandbox/capability_service.py`
- Test: `tests/test_sandbox/test_setup_runtime.py`
- Test: packaged Windows first-setup probe.

**Interfaces:**
- Produces native `denyWriteCarveout`, `authorityDenyRead`, and Windows network readiness capabilities only from expected process results.

- [ ] Write failing fake-backend tests where protected operations raise transport errors and prove `available` must be false.
- [ ] Replace `except Exception == denial` with native sandbox-identity command canaries and exact result checks.
- [ ] Require current Windows identity/storage/proxy-WFP setup support in the capability set and retain the 30-second hard timeout.
- [ ] Run setup/capability/backend tests, request review, and commit `fix: fail closed on sandbox capability probes`.

### Task 3: Full default and persisted preference convergence

**Files:**
- Modify: `src/opensquilla/sandbox/config.py`
- Modify: `src/opensquilla/sandbox/run_context.py`
- Modify: `src/opensquilla/gateway/rpc_sandbox.py`
- Test: `tests/test_sandbox/test_run_context.py`
- Test: `tests/test_sandbox/test_run_mode_routing.py`

**Interfaces:**
- Produces one persisted `sandbox.run_mode` resolver used by new task contexts.

- [ ] Write failing tests for fresh owner Full, stored Safe owner, guest Safe coercion, and no-hint CLI/background task creation.
- [ ] Set the fresh config default to Full and resolve storage preference before config fallback.
- [ ] Run run-mode/routing/migration tests, request review, and commit `fix: unify default run mode preference`.

### Task 4: Guest temporary-workspace and fail-closed Windows process boundary

**Files:**
- Modify: `src/opensquilla/sandbox/guest_profile.py`
- Modify: `src/opensquilla/sandbox/file_policy.py`
- Modify: `src/opensquilla/sandbox/integration.py`
- Modify: `src/opensquilla/gateway/rpc_sessions.py`
- Modify: `src/opensquilla/gateway/task_runtime.py`
- Modify: `src/opensquilla/sandbox/backend/windows_default.py`
- Test: `tests/test_gateway/test_guest_safe_sessions.py`
- Test: `tests/test_gateway/test_rpc_sessions.py`
- Test: `tests/test_sandbox/test_guest_profile.py`
- Test: `tests/test_sandbox/test_file_policy.py`
- Test: `tests/test_sandbox/test_shell_safe_policy_integration.py`
- Test: `tests/test_sandbox/test_windows_default_backend.py`
- Test: `tests/test_sandbox/test_windows_default_process_smoke.py`

**Interfaces:**
- Produces a fresh guest-managed temporary workspace per runtime task, bundled runtime mounts only on native backends with explicit read isolation, and an early stable Windows guest-process rejection while retaining confined trusted filesystem-worker operations.

- [ ] Write failing profile/payload tests proving the managed guest workspace is RW, Windows guest profiles contain no runtime mounts or runtime `PATH`, guest process requests are rejected before launch, and arbitrary host/user paths are DENY.
- [ ] Scrub execution-scoped guest roots, HOME/temp, mounts, and environment from reusable route envelopes; materialize and clean a distinct profile for every internal follow-up task.
- [ ] Prevent guest `sessions.bootstrap` from resolving or returning configured workspace, workspace id, or project snapshot data.
- [ ] Run the opt-in Windows smoke proving guest processes never launch while trusted filesystem-worker writes remain inside the managed workspace and arbitrary host reads are rejected; request review and commit the fail-closed boundary.

### Task 5: Stop automatic capability retry churn

**Files:**
- Modify: `opensquilla-webui/src/composables/settings/useSandboxSettings.ts`
- Test: `opensquilla-webui/src/composables/settings/useSandboxSettings.test.ts`

**Interfaces:**
- Produces one background check per mounted Settings scope; explicit retry/setup may force another.

- [ ] Write a fake-timer test proving an unavailable report causes no call after ten seconds.
- [ ] Remove the retry timer while preserving explicit `loadCapability(true)` after setup.
- [ ] Run focused/full frontend tests and checks, request review, and commit `perf: stop sandbox capability retry loop`.

### Task 6: Private direct-update snapshots

**Files:**
- Modify: `src/opensquilla/sandbox/upgrade_migration.py`
- Test: `tests/test_migration/test_sandbox_direct_update.py`

**Interfaces:**
- Produces a staging/final snapshot private to the current owner plus Windows SYSTEM/Administrators.

- [ ] Write POSIX mode tests and mocked Windows ACL failure/success tests.
- [ ] Harden staging before copy, each created file, and final snapshot; fail closed on hardening errors.
- [ ] Run migration/recovery/official-update tests, request review, and commit `fix: protect upgrade snapshots`.

### Task 7: Final packaged verification

**Files:**
- Update ignored reports/artifacts only.

**Interfaces:**
- Produces a final installer, official-update report, guest Web security report, and fresh Windows UAC performance report.

- [ ] Run full backend/frontend/static/build gates and final whole-branch review.
- [ ] Build a new local installer and rerun official `0.5.2` update preservation.
- [ ] Verify no/invalid Token guest RPC matrix and isolated chat from a second browser profile.
- [ ] Reset the Windows sandbox account/profile once, verify startup has no UAC, approve first Safe setup, record elapsed time, save, and relaunch.
- [ ] Open the final app on Settings > Sandbox for user inspection.
