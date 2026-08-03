# On-demand Windows Sandbox Setup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep desktop startup non-elevating in Full Access, and install or repair the Windows sandbox only after the local desktop user intentionally selects Safe mode.

**Architecture:** Gateway boot becomes a read-only readiness inspection that only prewarms an already-ready sandbox. The desktop Settings panel owns the explicit setup interaction: it explains the one-time administrator request, invokes the existing owner-only setup RPC, then performs a forced live capability check before allowing Safe mode to be selected. Remote web clients and the chat composer never initiate setup.

**Tech Stack:** Python 3.11+, asyncio, pytest, Vue 3, TypeScript, Pinia RPC store, vue-i18n, Vitest, Electron/NSIS, Windows UAC.

## Global Constraints

- Default access mode remains `full`; startup must not open a Windows UAC prompt.
- Only the local desktop Settings surface may present the interactive setup path.
- The existing owner check on `sandbox.setup.ensure` remains the security boundary; remote web clients cannot elevate the host.
- The existing Settings save boundary remains: successful setup selects the Safe-mode draft, and the user still saves it explicitly.
- Cancelled or failed setup stays in Full Access and shows a neutral retryable result without raw helper details.
- The chat composer only offers Safe mode when setup state is `ready`; it never triggers UAC.
- Existing configurations and legacy run-mode names remain readable after an in-place update.
- UAC interaction is manual; automated UI tooling must not attempt to click Windows security dialogs.

---

### Task 1: Make gateway boot inspection-only

**Files:**
- Modify: `tests/test_gateway/test_router_boot.py`
- Modify: `src/opensquilla/gateway/boot.py`

**Interfaces:**
- Consumes: `current_sandbox_setup_runtime_status(config) -> SetupResult`
- Produces: `_ensure_sandbox_setup_on_boot(config) -> SetupResult | None`, which never calls `ensure_sandbox_setup_auto` and prewarms capability only for `SetupResult.state == READY`

- [ ] **Step 1: Replace the boot tests with the required non-elevating contract**

```python
@pytest.mark.asyncio
async def test_boot_sandbox_setup_defers_incomplete_setup(monkeypatch):
    status = SetupResult(state=SandboxSetupState.NOT_SETUP, platform="windows", message="not ready", requires_admin=True)
    monkeypatch.setattr("opensquilla.sandbox.setup_runtime.current_sandbox_setup_runtime_status", AsyncMock(return_value=status))
    capability = AsyncMock()
    monkeypatch.setattr("opensquilla.sandbox.setup_runtime.current_sandbox_capability_report", capability)
    result = await boot._ensure_sandbox_setup_on_boot(GatewayConfig(sandbox={"run_mode": "full"}))
    assert result is status
    capability.assert_not_awaited()

@pytest.mark.asyncio
async def test_boot_sandbox_setup_prewarms_ready_setup(monkeypatch):
    status = SetupResult(state=SandboxSetupState.READY, platform="windows", message="ready", requires_admin=False)
    monkeypatch.setattr("opensquilla.sandbox.setup_runtime.current_sandbox_setup_runtime_status", AsyncMock(return_value=status))
    capability = AsyncMock()
    monkeypatch.setattr("opensquilla.sandbox.setup_runtime.current_sandbox_capability_report", capability)
    result = await boot._ensure_sandbox_setup_on_boot(GatewayConfig())
    assert result is status
    capability.assert_awaited_once()
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `python -m pytest tests/test_gateway/test_router_boot.py -k "boot_sandbox_setup" -q`

Expected: the incomplete-setup test fails because boot still calls `ensure_sandbox_setup_auto`, proving the regression is covered.

- [ ] **Step 3: Implement inspection-only startup**

```python
from opensquilla.sandbox.setup_runtime import (
    current_sandbox_capability_report,
    current_sandbox_setup_runtime_status,
)

status = await current_sandbox_setup_runtime_status(config)
if status.state is SandboxSetupState.READY:
    await current_sandbox_capability_report(config)
else:
    logger.info("boot.sandbox_setup_deferred", state=status.state.value)
return status
```

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run: `python -m pytest tests/test_gateway/test_router_boot.py -k "boot_sandbox_setup" -q`

Expected: all selected tests pass and no setup mock is invoked.

- [ ] **Step 5: Commit the boot behavior**

Run: `git add src/opensquilla/gateway/boot.py tests/test_gateway/test_router_boot.py && git commit -m "fix: defer Windows sandbox setup until requested"`

### Task 2: Add the local-desktop on-demand setup state machine

**Files:**
- Modify: `opensquilla-webui/src/composables/settings/useSandboxSettings.ts`
- Modify: `opensquilla-webui/src/components/settings/SandboxSettingsPanel.test.ts`

**Interfaces:**
- Consumes: `sandbox.setup.status`, owner-only `sandbox.setup.ensure`, and `sandbox.capability.status` with `{ refresh: true }`
- Produces: `canRequestSandboxSetup: ComputedRef<boolean>`, `sandboxSetupPending: Ref<boolean>`, `sandboxSetupOutcome: Ref<'idle' | 'ready' | 'cancelled' | 'failed' | 'verification_failed'>`, and `ensureSandboxSetupForSafeMode(): Promise<boolean>`

- [ ] **Step 1: Add failing panel tests for the explicit setup flow**

```typescript
it('does not request setup until the desktop user confirms', async () => {
  const { el, call } = await mountPanel({ setupState: 'not_setup' })
  el.querySelector('[data-testid="sandbox-safe-mode"]')!.click()
  await settle()
  expect(document.body.querySelector('[data-testid="sandbox-setup-confirm"]')).toBeTruthy()
  expect(call).not.toHaveBeenCalledWith('sandbox.setup.ensure')
})

it('forces capability verification after setup and selects the safe draft', async () => {
  const { el, call } = await mountPanel({ setupState: 'not_setup' })
  el.querySelector('[data-testid="sandbox-safe-mode"]')!.click()
  document.body.querySelector('[data-testid="sandbox-setup-continue"]')!.click()
  await settle()
  expect(call).toHaveBeenCalledWith('sandbox.setup.ensure')
  expect(call).toHaveBeenCalledWith('sandbox.capability.status', { refresh: true })
  expect(el.querySelector('[data-testid="sandbox-safe-mode"]')).toHaveClass('is-selected')
})
```

- [ ] **Step 2: Run the focused panel tests and verify RED**

Run: `npm run test:unit -- src/components/settings/SandboxSettingsPanel.test.ts` from `opensquilla-webui`

Expected: tests fail because no explicit setup confirmation or setup state machine exists.

- [ ] **Step 3: Implement status loading and setup verification in the composable**

```typescript
const sandboxSetupStatus = ref<SandboxSetupStatusPayload | null>(null)
const sandboxSetupPending = ref(false)
const sandboxSetupOutcome = ref<SandboxSetupOutcome>('idle')
const canRequestSandboxSetup = computed(() => (
  platform.capabilities.isDesktop
  && capability.value?.setupSupported !== false
  && ['not_setup', 'failed'].includes(sandboxSetupStatus.value?.state ?? '')
))

async function ensureSandboxSetupForSafeMode(): Promise<boolean> {
  sandboxSetupPending.value = true
  sandboxSetupOutcome.value = 'idle'
  try {
    const setup = await rpc.call<SandboxSetupStatusPayload>('sandbox.setup.ensure')
    sandboxSetupStatus.value = setup
    if (setup.state !== 'ready') {
      sandboxSetupOutcome.value = setup.detail?.includes('cancel') ? 'cancelled' : 'failed'
      return false
    }
    const report = await loadCapability(true)
    sandboxSetupOutcome.value = report?.available ? 'ready' : 'verification_failed'
    return Boolean(report?.available)
  } catch {
    sandboxSetupOutcome.value = 'failed'
    return false
  } finally {
    sandboxSetupPending.value = false
  }
}
```

- [ ] **Step 4: Re-run the composable/panel tests**

Run: `npm run test:unit -- src/components/settings/SandboxSettingsPanel.test.ts`

Expected: the state-machine tests still fail only because the panel has not yet rendered the confirmation UI; existing loading and policy tests remain green.

### Task 3: Present the minimal confirmation UI and soft landing

**Files:**
- Modify: `opensquilla-webui/src/components/settings/SandboxSettingsPanel.vue`
- Modify: `opensquilla-webui/src/components/settings/SandboxSettingsPanel.test.ts`
- Modify: `opensquilla-webui/src/locales/en.json`
- Modify: `opensquilla-webui/src/locales/zh-CN.json`
- Modify: `opensquilla-webui/src/locales/zh-TW.json`
- Modify: `opensquilla-webui/src/locales/ja.json`
- Modify: `opensquilla-webui/src/locales/ko.json`
- Modify: `opensquilla-webui/src/locales/ru.json`

**Interfaces:**
- Consumes: `canRequestSandboxSetup` and `ensureSandboxSetupForSafeMode()` from Task 2
- Produces: Safe-mode button behavior that either selects an already-available mode or opens a local confirmation dialog; neutral outcome copy for cancel/failure

- [ ] **Step 1: Extend failing tests for cancel and failure**

```typescript
it('keeps Full Access selected when setup is cancelled', async () => {
  const { el, call } = await mountPanel({ setupState: 'not_setup', ensureState: 'failed', ensureDetail: 'windows_setup_helper_cancelled' })
  el.querySelector('[data-testid="sandbox-safe-mode"]')!.click()
  document.body.querySelector('[data-testid="sandbox-setup-continue"]')!.click()
  await settle()
  expect(el.querySelector('[data-testid="sandbox-full-mode"]')).toHaveClass('is-selected')
  expect(call).not.toHaveBeenCalledWith('sandbox.run_mode.preference.set', expect.anything())
  expect(el.querySelector('[data-testid="sandbox-setup-result"]')!.textContent).not.toContain('windows_setup_helper_cancelled')
})
```

- [ ] **Step 2: Run the panel tests and verify RED**

Run: `npm run test:unit -- src/components/settings/SandboxSettingsPanel.test.ts` from `opensquilla-webui`

Expected: cancel/failure UI assertions fail before implementation.

- [ ] **Step 3: Add the confirmation and soft-landing view**

Use one compact modal with the exact Simplified Chinese copy:

```text
配置安全模式
OpenSquilla 需要一次管理员授权，以创建隔离账户并配置文件与网络保护。仅首次配置或修复时需要。
取消    继续
```

On Continue, call `ensureSandboxSetupForSafeMode`; set `defaultRunMode = 'safe'` only when it returns `true`. On cancel, close the dialog without RPC or draft changes. Render localized neutral text for `cancelled`, `failed`, and `verification_failed`; never render `SetupResult.detail`.

- [ ] **Step 4: Add matching locale keys to all six locale files**

Use native Simplified and Traditional Chinese copy; concise English text may be used as the fallback-equivalent copy for Japanese, Korean, and Russian until product translation review. Keep identical key structure in every locale.

- [ ] **Step 5: Verify unit, i18n, and type contracts**

Run from `opensquilla-webui`:

```text
npm run test:unit -- src/components/settings/SandboxSettingsPanel.test.ts
npm run check:architecture
npm run typecheck
```

Expected: all commands exit 0.

- [ ] **Step 6: Commit the Settings flow**

Run: `git add opensquilla-webui/src/composables/settings/useSandboxSettings.ts opensquilla-webui/src/components/settings/SandboxSettingsPanel.vue opensquilla-webui/src/components/settings/SandboxSettingsPanel.test.ts opensquilla-webui/src/locales && git commit -m "feat: configure Windows sandbox on demand"`

### Task 4: Keep the chat composer non-elevating

**Files:**
- Modify: `opensquilla-webui/src/views/ChatView.vue`
- Modify: `opensquilla-webui/src/components/chat/ChatComposerControls.test.ts`

**Interfaces:**
- Consumes: `sandboxSetupStatus: SandboxSetupStatusPayload | null`
- Produces: `composerAllowedRunModes` without `safe` unless setup state is `ready`

- [ ] **Step 1: Add a failing source contract for all incomplete states**

```typescript
expect(viewSource).toContain("status.state !== 'ready'")
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `npm run test:unit -- src/components/chat/ChatComposerControls.test.ts` from `opensquilla-webui`

Expected: failure because the current code excludes Safe only for `failed` and `unavailable`.

- [ ] **Step 3: Implement the ready-only filter**

```typescript
if (status !== null && status.state !== 'ready') {
  return allowedRunModes.value.filter(mode => mode !== 'safe')
}
```

- [ ] **Step 4: Run the focused and full Web UI suites**

Run from `opensquilla-webui`:

```text
npm run test:unit -- src/components/chat/ChatComposerControls.test.ts src/components/settings/SandboxSettingsPanel.test.ts
npm run test:unit
npm run build
```

Expected: all commands exit 0.

- [ ] **Step 5: Commit composer gating**

Run: `git add opensquilla-webui/src/views/ChatView.vue opensquilla-webui/src/components/chat/ChatComposerControls.test.ts && git commit -m "fix: offer safe mode only after setup"`

### Task 5: Run backend security and migration regressions

**Files:**
- Verify: `tests/test_gateway/test_rpc_sandbox_setup.py`
- Verify: `tests/test_sandbox/test_setup_runtime.py`
- Verify: `tests/test_sandbox/test_migration.py`
- Verify: `tests/test_sandbox/test_release_contract.py`

**Interfaces:**
- Consumes: existing owner guard, setup state, legacy run-mode migration, release asset contracts
- Produces: evidence that the UI change does not weaken remote authorization or legacy compatibility

- [ ] **Step 1: Run focused Python regressions**

Run:

```text
python -m pytest tests/test_gateway/test_router_boot.py tests/test_gateway/test_rpc_sandbox_setup.py tests/test_sandbox/test_setup_runtime.py tests/test_sandbox/test_migration.py tests/test_sandbox/test_release_contract.py -q
```

Expected: all tests pass.

- [ ] **Step 2: Run the broader sandbox suite**

Run: `python -m pytest tests/test_sandbox tests/test_gateway/test_router_boot.py tests/test_gateway/test_rpc_sandbox_setup.py -q`

Expected: all tests pass with no unexpected warnings or hangs.

### Task 6: Build the Windows installer and rehearse an official-version update

**Files:**
- Verify: `desktop/electron/package.json`
- Verify: `desktop/electron/scripts/test-local-official-upgrade.ps1`
- Produce: `artifacts/local-upgrade/OpenSquilla-<local-version>-win-x64.exe`
- Produce: `artifacts/local-upgrade/upgrade-report.json`
- Produce: `artifacts/local-upgrade/upgrade-report.md`

**Interfaces:**
- Consumes: official `0.5.2` installer plus the newly built local installer
- Produces: an isolated update rehearsal preserving existing tasks, settings, authentication data, and legacy sandbox mode names

- [ ] **Step 1: Build Web UI, gateway, unpacked Electron app, and NSIS installer in an isolated build worktree**

Use the repository's pinned build scripts. If Electron executable editing cannot create Windows symlinks, build unpacked with `signAndEditExecutable=false`, apply the cached `rcedit-x64.exe` metadata/icon, then run NSIS with `--prepackaged win-unpacked`.

- [ ] **Step 2: Run the official-old-to-local update rehearsal**

Run `desktop/electron/scripts/test-local-official-upgrade.ps1` with official `0.5.2`, the new local installer, and an isolated temporary user-data directory.

Expected report assertions:

```text
oldVersionStarted=true
localVersionStarted=true
seededTaskPreserved=true
seededTokenPreserved=true
legacyRunModeMigrated=true
gatewayHealthy=true
```

- [ ] **Step 3: Inspect the report and launch the upgraded app once**

Verify no UAC appears during updated-app startup, Full Access remains selected, Settings is responsive, and Safe mode presents the explanation instead of staying permanently disabled.

### Task 7: Rehearse a clean-machine first sandbox setup

**Files:**
- Produce: `artifacts/first-sandbox-setup/first-setup-report.json`
- Produce: `artifacts/first-sandbox-setup/first-setup-report.md`

**Interfaces:**
- Consumes: the newly built installer, an isolated fresh desktop profile, the Windows sandbox setup helper, and manual UAC approval/cancellation
- Produces: proof of no startup elevation, safe cancellation, successful retry, and a live Safe-mode process probe

- [ ] **Step 1: Snapshot and reset only the OpenSquilla sandbox test state**

Stop test instances, preserve the current sandbox marker/state, remove the test `OpenSquillaSandbox` local account through an explicitly elevated test helper, and use a new isolated desktop user-data directory. Do not alter unrelated Windows accounts, firewall rules, or user files.

- [ ] **Step 2: Install and launch with the fresh profile**

Expected: application opens on the new-task screen in Full Access; no UAC appears merely from startup.

- [ ] **Step 3: Test cancellation**

Open Settings → Sandbox → Safe mode, verify the explanation, click Continue, and manually reject the Windows UAC prompt.

Expected: Settings remains responsive, Full Access remains selected, a neutral retry message appears, and no Safe preference is saved.

- [ ] **Step 4: Test successful retry**

Repeat Safe-mode selection, click Continue, and manually approve UAC.

Expected: setup reaches `ready`, forced capability verification returns `available=true`, Safe becomes the unsaved selected draft, and saving persists it.

- [ ] **Step 5: Run a live sandbox task and collect evidence**

Run a harmless command through Safe mode and verify it executes under the isolated sandbox identity, can use the packaged runtime, cannot cross protected file boundaries, and preserves the configured network protections.

- [ ] **Step 6: Restore a healthy developer-machine state**

Keep the successfully configured sandbox account/marker if healthy, otherwise restore the saved state. Re-run capability status and a harmless Safe-mode command. Record all results, package hashes, and any manual UAC action in the report.

### Task 8: Final verification and handoff

**Files:**
- Verify: all committed source and tests
- Verify: `artifacts/local-upgrade/upgrade-report.md`
- Verify: `artifacts/first-sandbox-setup/first-setup-report.md`

**Interfaces:**
- Consumes: automated suites, update rehearsal, and first-setup rehearsal
- Produces: final evidence-backed result without merging into `main`

- [ ] **Step 1: Confirm branch isolation**

Run:

```text
git branch --show-current
git rev-parse main
git rev-parse upstream/main
git status --short
```

Expected: current branch is `sandbox-settings-reliability`; `main` equals `upstream/main`; only known test artifacts are untracked or modified.

- [ ] **Step 2: Run final targeted verification from clean committed code**

Run the Task 4 and Task 5 test/build commands again after packaging and manual testing.

- [ ] **Step 3: Report exact outcomes**

Summarize startup behavior, update preservation, UAC cancel/success behavior, live sandbox capability, installer path/hash, reports, and any remaining limitation. Do not claim success unless the corresponding command/report was observed passing.
