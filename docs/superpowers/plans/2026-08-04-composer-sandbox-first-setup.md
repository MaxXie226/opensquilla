# Composer Safe Mode First-Setup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Full Access the truthful initial composer state and let users configure Safe mode from the composer through a shared, timed first-setup dialog.

**Architecture:** Keep the backend preference as the remembered choice, derive a separate effective composer mode from authoritative setup readiness, and centralize the privileged setup/verification result in a shared coordinator. Extract the existing settings-only dialog into a reusable presentation component consumed by both Settings and ChatView.

**Tech Stack:** Vue 3 Composition API, TypeScript, Pinia RPC store, vue-i18n, Vitest with happy-dom, Electron packaged Windows runtime.

## Global Constraints

- A fresh installation starts in Full Access.
- A successfully selected mode is remembered for later new tasks.
- Safe is persisted only after setup and live capability verification succeed.
- The first-setup dialog says administrator approval is required and setup normally takes about 20–30 seconds.
- UAC cancellation or any setup/verification failure leaves Full Access effective.
- Do not rename or delete existing configuration keys or remove legacy mode normalization.
- Do not show a fake percentage; show elapsed time and phase text.

---

### Task 1: Truthful composer-effective mode

**Files:**
- Modify: `opensquilla-webui/src/composables/chat/useChatRunModePreference.ts`
- Modify: `opensquilla-webui/src/composables/chat/useChatRunModePreference.test.ts`
- Create: `opensquilla-webui/src/composables/chat/composerRunMode.ts`
- Create: `opensquilla-webui/src/composables/chat/composerRunMode.test.ts`
- Modify: `opensquilla-webui/src/views/ChatView.vue`

**Interfaces:**
- Consumes: `SandboxRunMode`, `SandboxSetupStatusPayload`, the remembered `globalRunMode`, and an optional active-task lock.
- Produces: `effectiveComposerRunMode(preference, setupStatus, activeLock): SandboxRunMode` and a Full-first `useChatRunModePreference` initial state.

- [ ] **Step 1: Write failing tests for Full-first initialization and stale Safe reconciliation**

```ts
it('starts in Full Access before backend preference hydration', () => {
  const api = mountPreference({ defaultRunMode: 'full' })
  expect(api.runMode.value).toBe('full')
})

it('soft-lands a stale Safe preference in Full Access when setup is not ready', () => {
  expect(effectiveComposerRunMode('safe', { state: 'not_setup' }, null)).toBe('full')
  expect(effectiveComposerRunMode('safe', { state: 'failed' }, null)).toBe('full')
})

it('keeps Safe when setup is ready and preserves an active task lock', () => {
  expect(effectiveComposerRunMode('safe', { state: 'ready' }, null)).toBe('safe')
  expect(effectiveComposerRunMode('full', { state: 'not_setup' }, 'safe')).toBe('safe')
})
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `npm --prefix opensquilla-webui run test:unit -- src/composables/chat/useChatRunModePreference.test.ts src/composables/chat/composerRunMode.test.ts`

Expected: FAIL because the preference starts as Safe and `effectiveComposerRunMode` does not exist.

- [ ] **Step 3: Implement the pure effective-mode resolver and Full-first state**

```ts
export function effectiveComposerRunMode(
  preference: SandboxRunMode,
  setupStatus: Pick<SandboxSetupStatusPayload, 'state'> | null,
  activeLock: SandboxRunMode | null,
): SandboxRunMode {
  if (activeLock) return activeLock
  if (setupStatus && setupStatus.state !== 'ready' && preference === 'safe') return 'full'
  return preference
}
```

Initialize `useChatRunModePreference` with `ref<SandboxRunMode>('full')`, use Full as the missing policy default, and prefer Full when it is allowed. In `ChatView`, replace the direct `activeRunModeLock ?? globalRunMode` computation with the resolver.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the command from Step 2.

Expected: PASS.

- [ ] **Step 5: Commit Task 1**

```text
git add opensquilla-webui/src/composables/chat/useChatRunModePreference.ts opensquilla-webui/src/composables/chat/useChatRunModePreference.test.ts opensquilla-webui/src/composables/chat/composerRunMode.ts opensquilla-webui/src/composables/chat/composerRunMode.test.ts opensquilla-webui/src/views/ChatView.vue
git commit -m "fix: show the effective composer access mode"
```

### Task 2: Shared setup coordinator

**Files:**
- Create: `opensquilla-webui/src/composables/sandboxSetupCoordinator.ts`
- Create: `opensquilla-webui/src/composables/sandboxSetupCoordinator.test.ts`
- Modify: `opensquilla-webui/src/composables/settings/useSandboxSettings.ts`
- Modify: `opensquilla-webui/src/composables/chat/useSandboxSetupRecovery.ts`
- Modify: `opensquilla-webui/src/composables/settings/useSandboxSettings.test.ts`
- Modify: `opensquilla-webui/src/composables/chat/useSandboxSetupRecovery.test.ts`

**Interfaces:**
- Consumes: `call(method, params)` for `sandbox.setup.ensure` and `sandbox.capability.status`.
- Produces: `ensureSandboxReady(call): Promise<{ ready: boolean; status: SandboxSetupStatusPayload | null; outcome: SandboxSetupOutcome }>` and exported `normalizeSandboxSetupStatus(payload)`.

- [ ] **Step 1: Write failing coordinator tests**

```ts
it('does not report ready until live capability verification passes', async () => {
  const call = vi.fn()
    .mockResolvedValueOnce({ state: 'ready', platform: 'win32' })
    .mockResolvedValueOnce({ available: false })
  await expect(ensureSandboxReady(call)).resolves.toMatchObject({
    ready: false,
    outcome: 'verification_failed',
  })
  expect(call).toHaveBeenNthCalledWith(2, 'sandbox.capability.status', { refresh: true })
})

it('classifies UAC cancellation and never runs capability verification', async () => {
  const call = vi.fn().mockResolvedValue({ state: 'failed', detail: 'cancelled_by_user' })
  await expect(ensureSandboxReady(call)).resolves.toMatchObject({
    ready: false,
    outcome: 'cancelled',
  })
  expect(call).toHaveBeenCalledOnce()
})
```

- [ ] **Step 2: Run coordinator and existing setup tests and verify RED**

Run: `npm --prefix opensquilla-webui run test:unit -- src/composables/sandboxSetupCoordinator.test.ts src/composables/settings/useSandboxSettings.test.ts src/composables/chat/useSandboxSetupRecovery.test.ts`

Expected: FAIL because the shared coordinator does not exist.

- [ ] **Step 3: Implement one setup/verification decision path**

```ts
export async function ensureSandboxReady(call: SandboxSetupCall): Promise<SandboxSetupResult> {
  try {
    const status = normalizeSandboxSetupStatus(await call('sandbox.setup.ensure'))
    if (!status) return { ready: false, status: null, outcome: 'failed' }
    if (status.state !== 'ready') {
      return {
        ready: false,
        status,
        outcome: status.detail?.toLowerCase().includes('cancel') ? 'cancelled' : 'failed',
      }
    }
    const report = await call('sandbox.capability.status', { refresh: true }) as { available?: unknown }
    return report?.available === true
      ? { ready: true, status, outcome: 'ready' }
      : { ready: false, status, outcome: 'verification_failed' }
  } catch {
    return { ready: false, status: null, outcome: 'failed' }
  }
}
```

Replace the duplicate settings logic with this function. Extend `useSandboxSetupRecovery` to expose `outcome` and have `ensureSetup()` return `Promise<boolean>` from the same coordinator.

- [ ] **Step 4: Run the tests from Step 2 and verify GREEN**

Expected: PASS, including cancellation and verification-failure cases.

- [ ] **Step 5: Commit Task 2**

```text
git add opensquilla-webui/src/composables/sandboxSetupCoordinator.ts opensquilla-webui/src/composables/sandboxSetupCoordinator.test.ts opensquilla-webui/src/composables/settings/useSandboxSettings.ts opensquilla-webui/src/composables/chat/useSandboxSetupRecovery.ts opensquilla-webui/src/composables/settings/useSandboxSettings.test.ts opensquilla-webui/src/composables/chat/useSandboxSetupRecovery.test.ts
git commit -m "refactor: share sandbox setup verification"
```

### Task 3: Reusable first-setup dialog

**Files:**
- Create: `opensquilla-webui/src/components/sandbox/SandboxSetupDialog.vue`
- Create: `opensquilla-webui/src/components/sandbox/SandboxSetupDialog.test.ts`
- Modify: `opensquilla-webui/src/components/settings/SandboxSettingsPanel.vue`
- Modify: `opensquilla-webui/src/components/settings/SandboxSettingsPanel.test.ts`
- Modify: `opensquilla-webui/src/locales/en.json`
- Modify: `opensquilla-webui/src/locales/zh-Hans.json`

**Interfaces:**
- Consumes props `open: boolean`, `pending: boolean`, `outcome: SandboxSetupOutcome`.
- Emits `cancel` and `confirm`.
- Produces an accessible modal with elapsed-time phase messaging and exact 20–30 second expectation copy.

- [ ] **Step 1: Write failing dialog tests**

```ts
it('explains UAC and the measured setup duration before confirmation', () => {
  const el = mountDialog({ open: true, pending: false, outcome: 'idle' })
  expect(el.textContent).toContain('20–30 seconds')
  expect(el.textContent).toContain('administrator')
})

it('shows honest elapsed feedback without a percentage', async () => {
  vi.useFakeTimers()
  const el = mountDialog({ open: true, pending: true, outcome: 'idle' })
  await vi.advanceTimersByTimeAsync(6_000)
  expect(el.querySelector('[data-testid="sandbox-setup-progress"]')?.textContent).toContain('6')
  expect(el.textContent).not.toMatch(/\d+%/)
})
```

- [ ] **Step 2: Run dialog and settings-panel tests and verify RED**

Run: `npm --prefix opensquilla-webui run test:unit -- src/components/sandbox/SandboxSetupDialog.test.ts src/components/settings/SandboxSettingsPanel.test.ts`

Expected: FAIL because the shared component and duration copy do not exist.

- [ ] **Step 3: Extract the shared component and update copy**

The component starts a one-second interval only while `open && pending`, resets on close/unmount, renders `settings.sandbox.setup.descriptionWithDuration`, and emits no RPC itself. Replace the settings panel's Teleport, elapsed timer, and dialog CSS with `<SandboxSetupDialog>`.

Add localized copy:

```json
"descriptionWithDuration": "OpenSquilla needs one administrator approval to create an isolated account and configure protection. First-time setup normally takes about 20–30 seconds. Keep OpenSquilla open during setup."
```

```json
"descriptionWithDuration": "OpenSquilla 需要一次管理员授权来创建隔离账户并配置安全保护。首次设置通常需要约 20–30 秒，期间请保持 OpenSquilla 打开。"
```

- [ ] **Step 4: Run the tests from Step 2 and verify GREEN**

Expected: PASS with no timer leaks after unmount.

- [ ] **Step 5: Commit Task 3**

```text
git add opensquilla-webui/src/components/sandbox/SandboxSetupDialog.vue opensquilla-webui/src/components/sandbox/SandboxSetupDialog.test.ts opensquilla-webui/src/components/settings/SandboxSettingsPanel.vue opensquilla-webui/src/components/settings/SandboxSettingsPanel.test.ts opensquilla-webui/src/locales/en.json opensquilla-webui/src/locales/zh-Hans.json
git commit -m "feat: share the Safe mode setup dialog"
```

### Task 4: Configure Safe mode from the composer

**Files:**
- Modify: `opensquilla-webui/src/components/chat/ChatComposerRunMode.vue`
- Modify: `opensquilla-webui/src/components/chat/ChatComposerRunMode.test.ts`
- Modify: `opensquilla-webui/src/components/chat/ChatComposer.vue`
- Modify: `opensquilla-webui/src/components/chat/ChatComposerControls.test.ts`
- Modify: `opensquilla-webui/src/views/ChatView.vue`
- Create: `opensquilla-webui/src/views/ChatView.sandbox-setup.test.ts`

**Interfaces:**
- `ChatComposerRunMode` adds `safeSetupAvailable: boolean`; Safe is clickable when either allowed or setup is requestable.
- `ChatView` opens `SandboxSetupDialog`, calls `sandboxSetupRecovery.ensureSetup()`, then calls `setGlobalRunMode('safe')` only when the result is true.

- [ ] **Step 1: Write failing interaction tests**

```ts
it('lets repairable Safe emit a selection without pretending it is already active', async () => {
  const { el, emitted } = mountRunMode({ allowedRunModes: ['full'], safeSetupAvailable: true })
  el.querySelectorAll<HTMLButtonElement>('[role="radio"]')[0].click()
  expect(emitted()).toContainEqual(['safe'])
})

it('requires confirmation before starting setup and persists Safe only after success', async () => {
  const view = mountChatWithSetupState('not_setup')
  await view.selectSafe()
  expect(view.setupDialog()).toBeTruthy()
  expect(view.rpcCalls('sandbox.setup.ensure')).toHaveLength(0)
  await view.confirmSetup({ result: 'ready' })
  expect(view.rpcCalls('sandbox.run_mode.preference.set')).toEqual([
    { runMode: 'safe' },
  ])
})

it.each(['cancelled', 'failed', 'verification_failed'])('%s keeps Full Access', async (result) => {
  const view = mountChatWithSetupState('not_setup')
  await view.selectSafe()
  await view.confirmSetup({ result })
  expect(view.visibleRunMode()).toBe('full')
  expect(view.rpcCalls('sandbox.run_mode.preference.set')).toHaveLength(0)
})
```

- [ ] **Step 2: Run composer tests and verify RED**

Run: `npm --prefix opensquilla-webui run test:unit -- src/components/chat/ChatComposerRunMode.test.ts src/components/chat/ChatComposerControls.test.ts src/views/ChatView.sandbox-setup.test.ts`

Expected: FAIL because Safe is disabled and ChatView has no setup dialog flow.

- [ ] **Step 3: Wire the composer to the shared flow**

Pass `safeSetupAvailable` through `ChatView -> ChatComposer -> ChatComposerRunMode`. In `setComposerRunMode`:

```ts
async function setComposerRunMode(mode: SandboxRunMode) {
  if (runModeLocked.value) return
  if (mode === 'safe' && sandboxSetupStatus.value?.state !== 'ready') {
    if (sandboxSetupRecovery.canSetup.value) sandboxSetupConfirmOpen.value = true
    return
  }
  await persistComposerRunMode(mode)
}

async function confirmComposerSandboxSetup() {
  const ready = await sandboxSetupRecovery.ensureSetup()
  if (!ready) return
  sandboxSetupConfirmOpen.value = false
  await persistComposerRunMode('safe')
}
```

Render the shared dialog once in `ChatView`. Keep it open with retry feedback after failure and close it after success or non-pending cancellation.

- [ ] **Step 4: Run the tests from Step 2 and verify GREEN**

Expected: PASS for first setup, ready fast path, cancellation, failures, and mode lock.

- [ ] **Step 5: Commit Task 4**

```text
git add opensquilla-webui/src/components/chat/ChatComposerRunMode.vue opensquilla-webui/src/components/chat/ChatComposerRunMode.test.ts opensquilla-webui/src/components/chat/ChatComposer.vue opensquilla-webui/src/components/chat/ChatComposerControls.test.ts opensquilla-webui/src/views/ChatView.vue opensquilla-webui/src/views/ChatView.sandbox-setup.test.ts
git commit -m "feat: configure Safe mode from the composer"
```

### Task 5: Regression, package, and real Windows verification

**Files:**
- Modify only if a failing test demonstrates a product defect.
- Write reports under `artifacts/first-sandbox-setup/` and `artifacts/local-upgrade/`; do not stage them.

**Interfaces:**
- Consumes the completed UI and packaged Electron application.
- Produces passing test output, a Windows installer, an upgrade report, and real UAC first-setup evidence.

- [ ] **Step 1: Run focused and full WebUI verification**

```text
npm --prefix opensquilla-webui run test:unit -- src/composables/chat/useChatRunModePreference.test.ts src/composables/chat/composerRunMode.test.ts src/composables/sandboxSetupCoordinator.test.ts src/composables/chat/useSandboxSetupRecovery.test.ts src/composables/settings/useSandboxSettings.test.ts src/components/sandbox/SandboxSetupDialog.test.ts src/components/settings/SandboxSettingsPanel.test.ts src/components/chat/ChatComposerRunMode.test.ts src/components/chat/ChatComposerControls.test.ts src/views/ChatView.sandbox-setup.test.ts
npm --prefix opensquilla-webui run typecheck
npm --prefix opensquilla-webui run test:unit
```

Expected: all commands exit 0 with no new warnings.

- [ ] **Step 2: Build WebUI and Electron package**

Run the repository's existing desktop packaging command used for `0.5.3-local.9`, incrementing the local build suffix so the generated installer cannot be confused with the previous artifact.

Expected: WebUI verification, bundled runtime checks, and NSIS packaging all exit 0.

- [ ] **Step 3: Verify official-version upgrade compatibility**

Seed the real existing profile with the official `0.5.2` installer, upgrade using the new local installer, and run `desktop/electron/scripts/verify-upgrade-profile.py` through the existing upgrade harness.

Expected: configuration, tasks, named tokens, loopback binding, new-task route, and mode compatibility all report `ok: true`.

- [ ] **Step 4: Verify real first setup and UI responsiveness**

Reset only the `OpenSquillaSandbox` local account through the existing elevated reset helper. Launch the packaged app, verify the composer initially displays Full Access, select Safe, confirm the 20–30 second dialog, approve the real UAC prompt, and record elapsed time and phase transitions. During setup, interact with a harmless non-security UI control to prove the renderer remains responsive.

Expected: setup reaches `ready`, the shield turns Safe only after live verification, the new sandbox SID matches the setup marker, and a cold restart preserves Safe.

- [ ] **Step 5: Run final source and worktree audit**

```text
git diff --check
git status --short
```

Expected: no unstaged source edits, only intentionally untracked/modified artifact reports, and no running OpenSquilla process after verification.

- [ ] **Step 6: Commit any verification-only test adjustments**

Commit only source or test changes proven necessary by Step 1–4. Never stage installers, user profiles, logs, tokens, or generated reports.
