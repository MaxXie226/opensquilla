# Sandbox Settings Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Full Access the fresh-profile default, remove visible token management, and keep Safe-mode verification from freezing or permanently disabling Settings.

**Architecture:** Preserve the existing gateway authentication and sandbox-policy boundaries, but separate fast policy loading from the slow live capability probe. Make Windows ACL journals converge by dropping nonexistent retained paths, and use distinct positive and negative capability-cache lifetimes. Keep UI changes inside the existing Sandbox and Settings components without introducing a new application-wide state layer.

**Tech Stack:** Python 3.11+, asyncio, Pydantic v2, pytest, Vue 3, TypeScript, Pinia, Vitest, Electron, PowerShell.

## Global Constraints

- The local loopback desktop client remains the owner and does not require manual token entry.
- Missing and invalid remote tokens remain the same guest authority and restricted Safe-mode policy.
- Named-token storage and owner-guarded RPC methods remain available for upgrade compatibility, but no token issuance or management UI is visible.
- `safe` and `full` are canonical; `sandboxed` and `trusted` continue to decode.
- Safe mode remains fail closed and requires the live capability canary.
- Existing explicit Safe preferences are preserved; only profiles without an explicit preference use the new Full Access default.
- The Chinese Safe-mode description is exactly `在沙箱中运行，并遵循你的安全规则。` and remains one line in the normal desktop layout.

---

### Task 1: Full Access Default and Compatibility

**Files:**
- Modify: `src/opensquilla/sandbox/run_mode_policy.py`
- Modify: `src/opensquilla/gateway/rpc_sandbox.py`
- Modify: `opensquilla-webui/src/composables/chat/useChatRunModePreference.ts`
- Modify: `opensquilla-webui/src/composables/settings/useSandboxSettings.ts`
- Test: `tests/test_sandbox/test_run_mode_policy.py`
- Test: `tests/test_gateway/test_rpc_run_mode_preference.py`
- Test: `opensquilla-webui/src/composables/chat/useChatRunModePreference.test.ts`

**Interfaces:**
- Consumes: `principal_has_host_execute(principal) -> bool`, `config_run_mode(config) -> RunMode`.
- Produces: `default_run_mode_for_principal(principal) -> RunMode`, returning `FULL` only for host-capable principals and `SAFE` for guests.

- [ ] **Step 1: Write failing default-mode tests**

```python
def test_host_capable_principal_defaults_to_full() -> None:
    principal = SimpleNamespace(capabilities=frozenset({'host.execute'}))
    assert default_run_mode_for_principal(principal) is RunMode.FULL

def test_guest_principal_still_defaults_to_safe() -> None:
    principal = SimpleNamespace(capabilities=frozenset())
    assert default_run_mode_for_principal(principal) is RunMode.SAFE
```

Add RPC assertions that a fresh `SandboxSettings()` with no explicitly configured run mode returns `full` for a host-capable principal, while `SandboxSettings(run_mode='safe')` and an RPC response with `source: 'preference', runMode: 'safe'` remain Safe.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `python -m pytest tests/test_sandbox/test_run_mode_policy.py tests/test_gateway/test_rpc_run_mode_preference.py -q`

Run: `npm run test:unit -- src/composables/chat/useChatRunModePreference.test.ts` from `opensquilla-webui`.

Expected: failures report the current Safe fallback.

- [ ] **Step 3: Implement the new default without overwriting preferences**

```python
def default_run_mode_for_principal(principal: Any) -> RunMode:
    return RunMode.FULL if principal_has_host_execute(principal) else RunMode.SAFE
```

Return `FULL` from `default_run_mode_for_principal` only when the principal has `host.execute`. In `sandbox.run_mode.preference.get`, use an explicit stored preference first, an explicitly configured run mode second, and the principal default only when neither exists. Keep the pre-auth frontend state fail-safe until the principal policy arrives.

- [ ] **Step 4: Re-run focused tests and confirm GREEN**

Expected: all Task 1 tests pass, including legacy `trusted` normalization and explicit Safe preservation.

### Task 2: Remove Visible Token Management and Shorten Copy

**Files:**
- Modify: `opensquilla-webui/src/components/settings/SandboxSettingsPanel.vue`
- Modify: `opensquilla-webui/src/composables/settings/useSandboxSettings.ts`
- Modify: `opensquilla-webui/src/components/settings/SandboxSettingsPanel.test.ts`
- Modify: `opensquilla-webui/src/components/chat/ChatComposerControls.test.ts`
- Modify: `opensquilla-webui/src/locales/zh-Hans.json`

**Interfaces:**
- Consumes: the existing `sandbox.tokens.*` gateway methods only for backward compatibility, not from this component.
- Produces: a four-row Sandbox overview and no calls to `sandbox.tokens.list`, `sandbox.tokens.create`, or `sandbox.tokens.revoke`.

- [ ] **Step 1: Replace token tests with absence and call-contract tests**

```ts
expect(el.querySelectorAll('[data-testid^="sandbox-open-"]')).toHaveLength(4)
expect(el.querySelector('[data-testid="sandbox-open-advanced"]')).toBeNull()
expect(call.mock.calls.some(([method]) => String(method).startsWith('sandbox.tokens.'))).toBe(false)
expect(el.textContent).not.toContain('Named Token')
```

Update the composer-copy expectation to `在沙箱中运行，并遵循你的安全规则。`.

- [ ] **Step 2: Run focused Vitest and confirm RED**

Run: `npm run test:unit -- src/components/settings/SandboxSettingsPanel.test.ts src/components/chat/ChatComposerControls.test.ts`.

Expected: the existing fifth row, advanced view, token RPC, and long Chinese copy fail the new assertions.

- [ ] **Step 3: Remove advanced/token UI and token state**

Delete the advanced row/detail template, token form/list handlers and styles, token fields from `useSandboxSettings`, and the eager `sandbox.tokens.list` call. Retain the gateway RPC implementation unchanged. Set `chat.runModeSafeDesc` to the approved Chinese sentence and shorten the Sandbox subtitle so it no longer mentions LAN tokens.

- [ ] **Step 4: Enforce the single-line desktop copy**

```css
.composer-run-mode-option__description {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
```

Use the component's existing description selector rather than creating a new layout primitive.

- [ ] **Step 5: Re-run focused Vitest and confirm GREEN**

Expected: both test files pass and no Sandbox component call references `sandbox.tokens.*`.

### Task 3: Decouple Sandbox Core Loading from Capability Verification

**Files:**
- Modify: `opensquilla-webui/src/composables/settings/useSandboxSettings.ts`
- Modify: `opensquilla-webui/src/components/settings/SandboxSettingsPanel.vue`
- Modify: `opensquilla-webui/src/components/settings/SandboxSettingsPanel.test.ts`
- Modify: `opensquilla-webui/src/components/settings/SettingsDialog.vue`
- Modify: `opensquilla-webui/src/components/settings/SettingsDialog.save-pending.test.ts`

**Interfaces:**
- Produces: `loadCore(): Promise<void>` semantics inside `load()`, plus an independent `capabilityLoading: Ref<boolean>` and `loadCapability(options?: { refresh?: boolean }): Promise<void>`.
- Removes: the public `refreshCapability()` wrapper because the advanced diagnostic page is removed in Task 2.

- [ ] **Step 1: Add deferred-capability and Settings feedback tests**

Use a deferred Promise for `sandbox.capability.status`; resolve policy/default/preference immediately. Assert the Sandbox overview renders before the deferred capability resolves and Safe remains disabled only while capability is unknown. In the Settings test, set `loaded = ref(false)`, select `capabilities`, and assert the panel contains the localized section name and a local loading status instead of only a spinner.

- [ ] **Step 2: Run focused Vitest and confirm RED**

Expected: `Promise.all` blocks the Sandbox overview and the Settings fallback lacks section feedback.

- [ ] **Step 3: Implement progressive Sandbox loading**

```ts
const [policyPayload, defaultsPayload, runModePayload] = await Promise.all([
  rpc.call<SandboxPolicy>('sandbox.policy.get'),
  rpc.call<Partial<SandboxPolicyDefaults>>('sandbox.policy.defaults'),
  rpc.call<{ runMode?: unknown }>('sandbox.run_mode.preference.get'),
])
void loadCapability()
```

Capability errors update only capability state and never set the core `loadError`. Desktop warning preference loading also runs independently.

- [ ] **Step 4: Replace the blank Settings fallback with local feedback**

Render the active section label, a short loading line, and compact skeleton rows inside `.settings-loading`; keep navigation and close controls responsive.

- [ ] **Step 5: Re-run focused Vitest and confirm GREEN**

Expected: Sandbox core and Settings navigation render without waiting for the live canary.

### Task 4: Bound Capability Failure Caching and Windows ACL Growth

**Files:**
- Modify: `src/opensquilla/sandbox/setup_runtime.py`
- Modify: `src/opensquilla/sandbox/backend/windows_default_runner.py`
- Test: `tests/test_sandbox/test_setup_runtime.py`
- Test: `tests/test_sandbox/test_windows_default_runner.py`

**Interfaces:**
- Produces: `_CAPABILITY_FAILURE_CACHE_TTL_SECONDS = 10.0` and cache selection based on `CapabilityReport.available`.
- Preserves: `_CAPABILITY_CACHE_TTL_SECONDS = 3600.0` for successful reports.

- [ ] **Step 1: Add negative-cache and stale-RX tests**

```python
def test_allow_acl_state_drops_missing_retained_rx_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox.backend import windows_default_runner as mod

    state = tmp_path / "allow.json"
    stale = tmp_path / "deleted-capability-probe"
    current = tmp_path / "current-capability-probe"
    current.mkdir()
    state.write_text(json.dumps({
        "version": 1,
        "principals": {"S": [{"path": str(stale), "access": "RX"}]},
    }), encoding="utf-8")
    revoked: list[Path] = []
    monkeypatch.setattr(mod, "_revoke_allow_path_for_sid", lambda path, _sid: revoked.append(path))
    monkeypatch.setattr(mod, "_grant_path_to_sid", lambda *_args: None)

    mod._sync_allow_acl_state(state, "S", {current: "RX"})

    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["principals"]["S"] == [{"access": "RX", "path": str(current)}]
    assert stale not in revoked
```

For the cache test, monkeypatch `setup_runtime.time.monotonic` with a mutable value, return an unavailable report on the first probe and an available report on the second, advance the clock by `11.0`, and assert the probe count changes from one to two. Then advance another `11.0` and assert the successful second report is still cached.

- [ ] **Step 2: Run focused pytest and confirm RED**

Run: `python -m pytest tests/test_sandbox/test_setup_runtime.py tests/test_sandbox/test_windows_default_runner.py -q`.

Expected: failures show one-hour negative caching and retained missing RX entries.

- [ ] **Step 3: Implement cache lifetime selection**

```python
def _capability_cache_ttl(report: CapabilityReport) -> float:
    return (
        _CAPABILITY_CACHE_TTL_SECONDS
        if report.available
        else _CAPABILITY_FAILURE_CACHE_TTL_SECONDS
    )
```

Store the report as today, but validate cached age against its selected TTL.

- [ ] **Step 4: Prune nonexistent retained RX state**

Change `retained_read` so it includes only previous RX paths that still exist. When a previous missing RX entry is excluded, omit it from the rewritten journal and skip ACL revoke because the target no longer exists. Continue revoking stale RWX grants and preserve fail-closed taint behavior.

- [ ] **Step 5: Re-run focused pytest and confirm GREEN**

Expected: cache, taint recovery, ACL rollback, and no-op synchronization tests all pass.

### Task 5: Integration Verification and Real Windows Package

**Files:**
- Modify only if verification exposes defects.
- Inspect: `desktop/electron/package.json`, packaging scripts, installed application logs.

**Interfaces:**
- Produces: an installed local Windows build and runtime evidence for UI responsiveness and live Safe capability.

- [ ] **Step 1: Run all relevant automated checks**

Run:

```powershell
python -m pytest tests/test_sandbox/test_setup_runtime.py tests/test_sandbox/test_windows_default_runner.py tests/test_sandbox/test_run_mode_policy.py tests/test_gateway/test_rpc_run_mode_preference.py tests/test_gateway/test_auth_debug_scope.py -q
Set-Location opensquilla-webui
npm run test:unit -- src/components/settings/SandboxSettingsPanel.test.ts src/components/settings/SettingsDialog.save-pending.test.ts src/components/chat/ChatComposerControls.test.ts src/composables/chat/useChatRunModePreference.test.ts
npm run typecheck
```

Expected: zero failures and zero type/architecture errors.

- [ ] **Step 2: Run formatting/diff safety checks**

Run: `git diff --check` and inspect `git status --short` so unrelated user files and upgrade artifacts remain untouched.

- [ ] **Step 3: Build and install the Windows package**

Use the repository's audited Electron packaging command discovered from `desktop/electron/package.json`. Stop only the currently installed OpenSquilla processes, install the new package non-interactively, and launch the installed executable.

- [ ] **Step 4: Set the current test profile through the normal API**

Call `sandbox.run_mode.preference.set` with `{ "runMode": "full" }` as the local owner. Do not edit `sessions.db` directly.

- [ ] **Step 5: Verify the actual desktop UI and gateway**

Confirm:

- a new-task window opens;
- Full Access is selected;
- the Safe description is one line;
- Sandbox settings have four rows and no token-management entry;
- navigating Settings does not blank or freeze;
- live `sandbox.capability.status` succeeds after ACL state convergence;
- Safe mode becomes selectable and an explicit switch persists.

- [ ] **Step 6: Complete requirement-by-requirement audit**

Compare source, tests, package version, installed files, RPC responses, and screenshots against every item in the approved design. Do not claim completion if any item lacks direct evidence.
