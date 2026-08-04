# Sandbox Auto-save and Background Setup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove Sandbox Save/Discard controls, persist edits automatically, and let first-time Safe-mode configuration continue in the background with one completion toast.

**Architecture:** Add a Pinia application-lifetime setup store that owns the single in-flight setup operation, Safe intent, persistence, and notifications. Keep policy drafting in `useSandboxSettings`, but add debounced/coalesced section saves with per-section rollback and connect Settings plus Chat to the shared setup store.

**Tech Stack:** Vue 3 Composition API, Pinia, TypeScript, Vitest, happy-dom, existing WebSocket RPC and toast singleton.

## Global Constraints

- Full Access remains the initial default on a fresh profile.
- Safe is persisted only after `sandbox.setup.ensure` and forced live capability verification both succeed.
- A later explicit Full Access choice wins over an earlier Safe setup intent.
- Existing tasks retain their pinned mode while setup runs.
- No new dependency or backend wire method is introduced.
- Successful ordinary auto-saves remain silent; failures roll back only the affected value and create one bottom-right toast.
- The Windows setup operation must survive closing its dialog, Settings, or Chat view.

---

### Task 1: Application-lifetime Safe setup manager

**Files:**
- Create: `opensquilla-webui/src/stores/sandboxSetup.ts`
- Create: `opensquilla-webui/src/stores/sandboxSetup.test.ts`
- Modify: `opensquilla-webui/src/locales/en.json`
- Modify: `opensquilla-webui/src/locales/zh-Hans.json`
- Modify: `opensquilla-webui/src/locales/ja.json`
- Modify: `opensquilla-webui/src/locales/fr.json`
- Modify: `opensquilla-webui/src/locales/de.json`
- Modify: `opensquilla-webui/src/locales/es.json`

**Interfaces:**
- Consumes: `useRpcStore()`, `ensureSandboxReady(call, verify, wait)`, and `useToasts().pushToast`.
- Produces: `useSandboxSetupStore()` with refs `ensuring`, `outcome`, `status`; actions `noteRunModeSelection(mode)`, `startSafeSetup()`, and `resetOutcome()`.

- [ ] **Step 1: Write failing store tests**

Cover one shared promise, persistence after verified success, one success toast, failure retaining Full, a later Full selection winning, and completion after the initiating effect scope is stopped.

```ts
const first = store.startSafeSetup()
const second = store.startSafeSetup()
expect(first).toBe(second)
resolveEnsure(readyStatus)
await expect(first).resolves.toBe(true)
expect(rpc.call).toHaveBeenCalledWith('sandbox.run_mode.preference.set', { runMode: 'safe' })
expect(pushToast).toHaveBeenCalledWith('Safe mode is ready.', { tone: 'ok' })
```

- [ ] **Step 2: Run the store test and verify RED**

Run:

```powershell
npm run test:unit -- src/stores/sandboxSetup.test.ts
```

Expected: FAIL because `@/stores/sandboxSetup` does not exist.

- [ ] **Step 3: Implement the store**

Use module-backed Pinia state and one retained promise:

```ts
export const useSandboxSetupStore = defineStore('sandboxSetup', () => {
  const ensuring = ref(false)
  const outcome = ref<SandboxSetupOutcome>('idle')
  const status = ref<SandboxSetupStatusPayload | null>(null)
  const intendedMode = ref<SandboxRunMode>('full')
  let inFlight: Promise<boolean> | null = null

  function noteRunModeSelection(mode: SandboxRunMode) {
    intendedMode.value = mode
  }

  function startSafeSetup(): Promise<boolean> {
    intendedMode.value = 'safe'
    if (inFlight) return inFlight
    ensuring.value = true
    outcome.value = 'idle'
    inFlight = runSetup().finally(() => {
      ensuring.value = false
      inFlight = null
    })
    return inFlight
  }
})
```

`runSetup()` calls `ensureSandboxReady`, persists Safe only when `intendedMode` remains `safe`, and emits exactly one translated success or failure toast.

- [ ] **Step 4: Add concise translations in all six locale files**

Add keys beneath `settings.sandbox.setup` equivalent to:

```json
{
  "runInBackground": "Run in background",
  "readyToast": "Safe mode is ready.",
  "failedToast": "Safe mode setup could not finish. Try again from Safe mode."
}
```

- [ ] **Step 5: Run store and i18n tests and verify GREEN**

Run:

```powershell
npm run test:unit -- src/stores/sandboxSetup.test.ts src/i18n/i18n.test.ts
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add opensquilla-webui/src/stores/sandboxSetup.ts opensquilla-webui/src/stores/sandboxSetup.test.ts opensquilla-webui/src/locales
git commit -m "feat: keep safe setup running in background"
```

### Task 2: Setup dialog pending-state interaction

**Files:**
- Modify: `opensquilla-webui/src/components/sandbox/SandboxSetupDialog.vue`
- Modify: `opensquilla-webui/src/components/sandbox/SandboxSetupDialog.test.ts`

**Interfaces:**
- Consumes: `pending: boolean` and `settings.sandbox.setup.runInBackground`.
- Produces: new `background` event. Existing `cancel` remains pre-start only; existing `confirm` remains Start/Retry.

- [ ] **Step 1: Write failing dialog tests**

```ts
it('replaces Cancel with Run in background while setup is pending', async () => {
  const body = mountDialog(true)
  expect(body.textContent).not.toContain('Cancel')
  const background = body.querySelector('[data-testid="sandbox-setup-background"]')
  expect(background?.textContent).toContain('Run in background')
  expect(body.querySelector('[data-testid="sandbox-setup-continue"]')?.hasAttribute('disabled')).toBe(true)
})
```

Also assert that clicking the background button emits `background`, while overlay clicks during pending do nothing.

- [ ] **Step 2: Run the dialog test and verify RED**

Run:

```powershell
npm run test:unit -- src/components/sandbox/SandboxSetupDialog.test.ts
```

Expected: FAIL because the pending secondary action is still disabled Cancel.

- [ ] **Step 3: Implement the pending action swap**

Render the secondary button as:

```vue
<button
  v-if="pending"
  type="button"
  class="btn"
  data-testid="sandbox-setup-background"
  @click="$emit('background')"
>
  {{ t('settings.sandbox.setup.runInBackground') }}
</button>
<button v-else type="button" class="btn" @click="cancel">
  {{ t('common.cancel') }}
</button>
```

Keep the primary button disabled and labelled Configuring while pending.

- [ ] **Step 4: Run dialog tests and verify GREEN**

Run:

```powershell
npm run test:unit -- src/components/sandbox/SandboxSetupDialog.test.ts
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add opensquilla-webui/src/components/sandbox/SandboxSetupDialog.vue opensquilla-webui/src/components/sandbox/SandboxSetupDialog.test.ts
git commit -m "feat: allow safe setup to continue in background"
```

### Task 3: Sandbox policy auto-save

**Files:**
- Modify: `opensquilla-webui/src/composables/settings/useSandboxSettings.ts`
- Modify: `opensquilla-webui/src/composables/settings/useSandboxSettings.test.ts`
- Modify: `opensquilla-webui/src/components/settings/SandboxSettingsPanel.vue`
- Modify: `opensquilla-webui/src/components/settings/SandboxSettingsPanel.test.ts`

**Interfaces:**
- Produces from `useSandboxSettings`: `setDefaultRunMode(mode)`, `scheduleSectionSave(section)`, and `flushSectionSave(section)`.
- Removes from the panel: `SectionActions`, `saveDefaultRunMode`, `discardDefaultRunMode`, and every explicit save/discard binding.

- [ ] **Step 1: Write failing composable tests**

Test optimistic immediate mode persistence, per-section 500 ms debounce, same-section edit preservation during an in-flight save, ordered policy versions, and failure rollback with one toast.

```ts
settings.draft.value!.network.blockAllNetwork = true
settings.scheduleSectionSave('network')
await vi.advanceTimersByTimeAsync(499)
expect(rpc.call).not.toHaveBeenCalledWith('sandbox.policy.update', expect.anything())
await vi.advanceTimersByTimeAsync(1)
expect(rpc.call).toHaveBeenCalledWith('sandbox.policy.update', expect.anything())
```

- [ ] **Step 2: Run composable tests and verify RED**

Run:

```powershell
npm run test:unit -- src/composables/settings/useSandboxSettings.test.ts
```

Expected: FAIL because the auto-save APIs do not exist.

- [ ] **Step 3: Implement debounced and coalesced saves**

Add one timer per section and preserve edits made after a request snapshot:

```ts
function scheduleSectionSave(section: SandboxPolicySection) {
  clearTimeout(sectionTimers[section])
  sectionTimers[section] = setTimeout(() => void flushSectionSave(section), 500)
}

async function performSectionSave(section: SandboxPolicySection) {
  const submitted = cloneSection(draft.value![section])
  const saved = await rpc.call<SandboxPolicy>('sandbox.policy.update', requestFor(submitted))
  const editedDuringSave = cloneSection(draft.value![section])
  applySavedBaseline(saved)
  if (!equal(editedDuringSave, submitted)) {
    draft.value![section] = editedDuringSave
    scheduleSectionSave(section)
  }
}
```

On failure, restore only `draft[section]` from `baseline[section]`, clear its timer, and push one danger toast. Dispose all timers in `onScopeDispose`.

- [ ] **Step 4: Write failing panel tests**

Assert no `save-sandbox-section` buttons or Save/Discard text, immediate mode save, switch/add/remove flushes, and text/number debounce or blur flushes.

```ts
expect(el.querySelector('[data-testid="save-sandbox-section"]')).toBeNull()
expect(el.textContent).not.toContain('Discard')
el.querySelector('[data-testid="sandbox-safe-mode"]')!.dispatchEvent(new MouseEvent('click'))
expect(call).toHaveBeenCalledWith('sandbox.run_mode.preference.set', { runMode: 'safe' })
```

- [ ] **Step 5: Run panel tests and verify RED**

Run:

```powershell
npm run test:unit -- src/components/settings/SandboxSettingsPanel.test.ts
```

Expected: FAIL because SectionActions are still rendered and controls do not auto-save.

- [ ] **Step 6: Remove SectionActions and wire auto-save events**

- Mode buttons call `setDefaultRunMode`.
- Switch/select/add/remove actions call `flushSectionSave` after mutation.
- Editable text and quota inputs call `scheduleSectionSave` on input and `flushSectionSave` on blur/change.
- Detail navigation does not cancel timers or discard drafts.

- [ ] **Step 7: Run composable and panel tests and verify GREEN**

Run:

```powershell
npm run test:unit -- src/composables/settings/useSandboxSettings.test.ts src/components/settings/SandboxSettingsPanel.test.ts
```

Expected: PASS.

- [ ] **Step 8: Commit**

```powershell
git add opensquilla-webui/src/composables/settings/useSandboxSettings.ts opensquilla-webui/src/composables/settings/useSandboxSettings.test.ts opensquilla-webui/src/components/settings/SandboxSettingsPanel.vue opensquilla-webui/src/components/settings/SandboxSettingsPanel.test.ts
git commit -m "feat: autosave sandbox settings"
```

### Task 4: Share the setup operation across Settings and Chat

**Files:**
- Modify: `opensquilla-webui/src/components/settings/SandboxSettingsPanel.vue`
- Modify: `opensquilla-webui/src/components/settings/SandboxSettingsPanel.test.ts`
- Modify: `opensquilla-webui/src/views/ChatView.vue`
- Modify: `opensquilla-webui/src/views/ChatView.sandboxSetup.test.ts`
- Modify: `opensquilla-webui/src/composables/chat/useChatRunModePreference.ts`
- Modify: `opensquilla-webui/src/composables/chat/useChatRunModePreference.test.ts`

**Interfaces:**
- Consumes: Task 1 `useSandboxSetupStore()` and Task 2 `background` event.
- Settings and Chat call `noteRunModeSelection` for every explicit mode choice and `startSafeSetup` after confirmation.

- [ ] **Step 1: Write failing cross-surface tests**

Test that both dialogs bind to the same `ensuring/outcome`, background closes only the local dialog, duplicate starts make one `sandbox.setup.ensure` call, a success preference event updates the visible mode, and explicit Full during setup prevents Safe persistence.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
npm run test:unit -- src/components/settings/SandboxSettingsPanel.test.ts src/views/ChatView.sandboxSetup.test.ts src/composables/chat/useChatRunModePreference.test.ts
```

Expected: FAIL because both surfaces still own separate setup promises.

- [ ] **Step 3: Wire Settings to the shared store**

```ts
const setup = useSandboxSetupStore()
async function continueSandboxSetup() {
  const ready = await setup.startSafeSetup()
  if (ready) acceptConfirmedDefaultRunMode('safe')
}
function backgroundSandboxSetup() {
  sandboxSetupConfirmOpen.value = false
}
```

Bind dialog pending/outcome to the store and add `@background="backgroundSandboxSetup"`.

- [ ] **Step 4: Wire Chat to the shared store**

Replace local `ensureSetup()` invocation with `setup.startSafeSetup()`, bind pending/outcome to the store, close only the composer dialog on `background`, and call `setup.noteRunModeSelection(mode)` before explicit mode persistence.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run the Step 2 command. Expected: PASS.

- [ ] **Step 6: Run full WebUI verification**

```powershell
npm run test:unit
npm run build
```

Expected: all unit tests, architecture checks, i18n parity, typecheck, and production build pass.

- [ ] **Step 7: Commit**

```powershell
git add opensquilla-webui/src
git commit -m "feat: share safe setup across app surfaces"
```

### Task 5: Packaged Windows acceptance

**Files:**
- Modify only if the probe needs new assertions: `artifacts/first-sandbox-setup/probe-packaged-first-run.mjs`
- Produce: `artifacts/first-sandbox-setup/local14-first-run/first-run-report.json`
- Produce: `artifacts/local-upgrade/OpenSquilla-0.5.3-local.14-win-x64.exe`

**Interfaces:**
- Consumes the completed WebUI and existing packaged Gateway/runtime bundle.
- Produces a local installer and acceptance evidence; artifacts remain uncommitted.

- [ ] **Step 1: Run desktop package verification**

```powershell
npm run build
npm run verify:package
npm run verify:gateway-smoke
npm run test:bundled-runtimes
```

Expected: PASS.

- [ ] **Step 2: Build local.14 with a temporary package version**

Temporarily set both desktop package version fields to `0.5.3-local.14`, run `npm run build:gateway` and `npm run dist`, then restore tracked package files to `0.5.2` before any commit.

- [ ] **Step 3: Run the isolated first-run probe**

Assert Full is initially selected, Safe opens the pre-start confirmation, and no UAC/setup marker is created. The pending Run-in-background state is covered by Task 2 and Task 4 tests because entering that state in a packaged first-run probe would invoke the real Windows UAC helper.

- [ ] **Step 4: Run the official 0.5.2 upgrade rehearsal**

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File desktop/electron/scripts/test-local-official-upgrade.ps1 -LocalInstaller artifacts/local-upgrade/OpenSquilla-0.5.3-local.14-win-x64.exe -TargetVersion 0.5.3-local.14
```

Expected: tasks and named token preserved, `/control/chat/new`, loopback-only, cleanup complete.

- [ ] **Step 5: Launch local.14 and verify live state**

Confirm the normal client opens to New Task, default preference remains Full, and `sandbox.capability.status { refresh: true }` reports `available: true` with backend `windows_default`.

- [ ] **Step 6: Final repository audit**

```powershell
git fetch upstream --prune
git diff --check
git status --porcelain=v1
git rev-parse main
git rev-parse upstream/main
git merge-base HEAD upstream/main
```

Expected: main equals upstream/main, feature branch is based on it, and only local artifacts are uncommitted.
