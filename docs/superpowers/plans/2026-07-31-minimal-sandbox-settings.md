# Minimal Sandbox Settings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the long all-at-once Sandbox settings form with a quiet overview and focused category details while preserving every policy and token capability.

**Architecture:** Keep `useSandboxSettings()` as the single data and mutation boundary. `SandboxSettingsPanel.vue` owns a local `activeView` navigation state; the overview derives short summaries from the existing draft and each existing editor is rendered only in its matching detail view. No RPC schema or persisted setting changes.

**Tech Stack:** Vue 3 Composition API, vue-i18n, scoped CSS, Vitest + happy-dom.

## Global Constraints

- The overview shows mode, live availability, Files, Commands, Network, Bundled tools, and Advanced only.
- Named tokens and capability re-detection live under Advanced.
- Desktop LAN binding and CIDR controls remain absent.
- Existing save/discard, backup quota, approval prefix, domain, runtime, and token semantics remain unchanged.
- No new UI dependency or icon package.

---

### Task 1: Lock the overview and navigation contract

**Files:**
- Modify: `opensquilla-webui/src/components/settings/SandboxSettingsPanel.test.ts`

**Interfaces:**
- Consumes: rendered `SandboxSettingsPanel` and mocked `useRpcStore().call`.
- Produces: stable test ids `sandbox-overview`, `sandbox-open-files`, `sandbox-open-advanced`, `sandbox-detail`, and `sandbox-detail-back`.

- [ ] **Step 1: Write failing overview tests**

Add a test that asserts the initial render contains `[data-testid="sandbox-overview"]`, four category buttons plus Advanced, and does not contain the built-in path list or token creation form.

```ts
expect(el.querySelector('[data-testid="sandbox-overview"]')).toBeTruthy()
expect(el.querySelector('[data-testid="builtin-file-rules"]')).toBeNull()
expect(el.querySelector('[data-testid="create-sandbox-token"]')).toBeNull()
```

- [ ] **Step 2: Write failing navigation tests**

Click `sandbox-open-files`, assert the protected path appears, click Back, then open Advanced and assert token creation appears only there.

```ts
el.querySelector<HTMLButtonElement>('[data-testid="sandbox-open-files"]')!.click()
await settle()
expect(el.querySelector('[data-testid="builtin-file-rules"]')).toBeTruthy()
```

- [ ] **Step 3: Run tests and confirm the new contract fails**

Run: `npm run test:unit -- SandboxSettingsPanel.test.ts`

Expected: FAIL because the overview and navigation test ids do not exist.

- [ ] **Step 4: Commit the red tests with the implementation task**

Do not commit a permanently red tree; keep the failing tests local until Task 2 passes.

---

### Task 2: Implement the two-level Sandbox settings surface

**Files:**
- Modify: `opensquilla-webui/src/components/settings/SandboxSettingsPanel.vue`
- Modify: `opensquilla-webui/src/locales/en.json`
- Modify: `opensquilla-webui/src/locales/zh-Hans.json`
- Modify: `opensquilla-webui/src/locales/ja.json`
- Modify: `opensquilla-webui/src/locales/fr.json`
- Modify: `opensquilla-webui/src/locales/de.json`
- Modify: `opensquilla-webui/src/locales/es.json`

**Interfaces:**
- Consumes: the existing `draft`, `capability`, `runtimeVersions`, `tokens`, section save/discard methods, and mode preference methods.
- Produces: `type SandboxView = 'overview' | 'files' | 'commands' | 'network' | 'runtimes' | 'advanced'` and the Task 1 test ids.

- [ ] **Step 1: Add local view state and derived summaries**

```ts
type SandboxView = 'overview' | 'files' | 'commands' | 'network' | 'runtimes' | 'advanced'
const activeView = ref<SandboxView>('overview')
const protectedPathCount = computed(() => builtinDenyWritePaths.value.length + (draft.value?.files.customDenyWritePaths.length ?? 0))
const enabledRuntimeCount = computed(() => draft.value
  ? [draft.value.runtimes.python, draft.value.runtimes.node, draft.value.runtimes.gitBash].filter(Boolean).length
  : 0)
```

- [ ] **Step 2: Replace the initial card stack with overview markup**

Render a segmented two-button mode control, one grouped list with four category rows, and a separate Advanced row. Each row includes one line of muted live summary text and a chevron. Retain `SectionActions` for mode only when its choice is dirty.

- [ ] **Step 3: Gate every existing editor by `activeView`**

Wrap each Files/Commands/Network/Runtimes editor with its matching view and move the token editor plus re-detect/reset-warning actions into Advanced. Add a shared detail header and Back button. Back changes only `activeView`; it never invokes a save method.

- [ ] **Step 4: Add concise locale copy in all six locale files**

Add equal keys under `settings.sandbox.overview` for `filesSummary`, `commandsSummary`, `networkOpen`, `networkBlocked`, `runtimesSummary`, `advanced`, `advancedSummary`, `back`, and `statusReady/statusUnavailable`. Keep Chinese summaries to one ordinary desktop line.

- [ ] **Step 5: Replace card-heavy CSS with grouped-list styling**

Use a single surface, hairline row separators, 12-14 px radii, a restrained segmented control, 44 px minimum row targets, and a below-720 px layout that stacks summaries without exposing horizontal scroll.

- [ ] **Step 6: Run the focused test**

Run: `npm run test:unit -- SandboxSettingsPanel.test.ts`

Expected: all panel tests pass.

- [ ] **Step 7: Commit**

```powershell
git add opensquilla-webui/src/components/settings/SandboxSettingsPanel.vue opensquilla-webui/src/components/settings/SandboxSettingsPanel.test.ts opensquilla-webui/src/locales/*.json
git commit -m "feat: simplify sandbox settings navigation"
```

---

### Task 3: Visual and release verification

**Files:**
- Modify only files implicated by a confirmed visual or test defect.
- Produce: `dist/desktop-electron/OpenSquilla-0.5.3-local.2-win-x64.exe`

**Interfaces:**
- Consumes: the completed overview/detail UI and existing local build pipeline.
- Produces: a visually inspected local client ready for the user.

- [ ] **Step 1: Run complete frontend verification**

Run from `opensquilla-webui`:

```powershell
npm run test:unit
npm run build
```

Expected: 245 test files pass, architecture/i18n/typecheck pass, and the production artifact verifies.

- [ ] **Step 2: Launch the local UI and inspect both levels**

Open Sandbox settings at ordinary desktop width. Verify the overview has no long editors, every row opens the correct detail, Back preserves unsaved draft state without saving, and Advanced contains tokens/re-detection.

- [ ] **Step 3: Fix only confirmed visual defects and rerun focused tests**

Check Chinese wrapping, keyboard focus, 44 px row targets, unavailable Safe mode, and narrow layout. Repeat Step 1 after any fix.

- [ ] **Step 4: Rebuild and verify the new local installer**

Run from `desktop/electron`:

```powershell
npm run build:web
npm run build:gateway
npm run build
node_modules/.bin/electron-builder.cmd --win nsis --publish never --config.extraMetadata.version=0.5.3-local.2
npm run verify:package
npm run verify:gateway-smoke
npm run test:bundled-runtimes
```

Expected: `dist/desktop-electron/OpenSquilla-0.5.3-local.2-win-x64.exe` exists and every verifier exits 0.

- [ ] **Step 5: Commit final evidence**

Commit the UI, tests, updated upgrade evidence, and harness changes without committing downloaded official installers or temporary profiles.
