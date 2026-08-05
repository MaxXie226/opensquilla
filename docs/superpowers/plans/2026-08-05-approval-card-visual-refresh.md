# Approval Card Visual Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refresh the in-chat approval card into a compact, premium system confirmation without changing approval behavior or safety wording.

**Architecture:** Keep the change inside the existing `ApprovalCard.vue` boundary. Add only semantic wrappers needed to style the target and risk notice, then replace the heavy warning surfaces with neutral token-based CSS. Preserve the existing computed approval state, events, live-region behavior, and responsive breakpoint.

**Tech Stack:** Vue 3, TypeScript, scoped CSS, Vitest, happy-dom, Vue I18n, Electron packaged-app smoke tooling.

## Global Constraints

- Use `DESIGN_VARIANCE: 4`, `MOTION_INTENSITY: 2`, and `VISUAL_DENSITY: 3`.
- Add no dependency, gradient, glass effect, decorative animation, or new user-facing copy.
- Preserve approval data, wording, decisions, backup behavior, timers, accessibility announcements, and RPC behavior.
- Use existing theme tokens and retain readable contrast in light and dark themes.
- Keep the 768px stacked-action mobile behavior.

---

### Task 1: Add semantic structure for the compact target and risk rows

**Files:**
- Modify: `opensquilla-webui/src/components/chat/ApprovalCard.contracts.test.ts`
- Modify: `opensquilla-webui/src/components/chat/ApprovalCard.vue`

**Interfaces:**
- Consumes: `ChatApprovalItem.backupState`, `ChatApprovalItem.displayTarget`, and the existing `riskTitle`, `riskBody`, and `riskClass` computed values.
- Produces: `.approval-card__target`, `.approval-card__risk-icon`, and `.approval-card__risk-copy` elements for stable semantic styling.

- [ ] **Step 1: Write the failing structure test**

Add this test inside `describe('ApprovalCard safe context')`:

```ts
it('presents destructive target and backup status as compact semantic rows', async () => {
  const { app, root } = await mountCard(approval({
    displayKind: 'delete_path',
    displayTarget: '/workspace/archive',
    destructive: true,
    irreversible: true,
    backupState: 'enabled',
  }))

  expect(root.querySelector('.approval-card__target')?.textContent).toBe('/workspace/archive')
  expect(root.querySelector('.approval-card__risk-icon')).not.toBeNull()
  expect(root.querySelector('.approval-card__risk-copy')?.textContent).toContain('recoverable backup')
  app.unmount()
})
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `npm run test:unit -- src/components/chat/ApprovalCard.contracts.test.ts`

Expected: the new test fails because the three compact semantic classes do not exist.

- [ ] **Step 3: Add the minimal semantic markup**

Render the target value in a code element:

```vue
<dd><code class="approval-card__target">{{ approval.displayTarget }}</code></dd>
```

Render the risk row as an icon plus copy:

```vue
<span class="approval-card__risk-icon" aria-hidden="true">
  <Icon :name="riskIcon" :size="14" />
</span>
<div class="approval-card__risk-copy">
  <strong>{{ riskTitle }}</strong>
  <p v-if="riskBody">{{ riskBody }}</p>
  <p v-if="riskSecondary">{{ riskSecondary }}</p>
</div>
```

Add a computed icon name without changing behavior:

```ts
const riskIcon = computed(() =>
  props.approval.backupState === 'enabled' ? 'check' : 'info')
```

- [ ] **Step 4: Run the focused test and verify GREEN**

Run: `npm run test:unit -- src/components/chat/ApprovalCard.contracts.test.ts`

Expected: all ApprovalCard contract tests pass.

---

### Task 2: Apply the lightweight system-confirmation visual treatment

**Files:**
- Modify: `opensquilla-webui/src/components/chat/ApprovalCard.vue`

**Interfaces:**
- Consumes: the semantic classes introduced in Task 1 and the existing CSS variables such as `--bg-surface`, `--border`, `--hairline`, `--text`, `--text-muted`, `--accent`, `--ok`, and `--danger`.
- Produces: a neutral outer card, compact header, code-like target row, quiet status row, right-aligned desktop actions, and unchanged stacked mobile actions.

- [ ] **Step 1: Replace the heavy outer treatment**

Update `.approval-card` to use a neutral border, a 16px-compatible existing radius token, and a restrained two-layer shadow. Remove permanent warning/danger border colors; destructive state remains explicit through copy and the status row.

```css
.approval-card {
  background: color-mix(in srgb, var(--bg-surface) 97%, var(--bg));
  border: 1px solid color-mix(in srgb, var(--border) 82%, transparent);
  box-shadow: 0 1px 2px rgb(0 0 0 / 4%), 0 10px 32px rgb(0 0 0 / 5%);
}
```

- [ ] **Step 2: Tighten the header and target hierarchy**

Reduce icon size and padding, remove uppercase styling from the eyebrow, and style `.approval-card__context-row` as a compact neutral inset surface. Keep long targets wrapping with the mono font.

- [ ] **Step 3: Convert risk blocks into semantic status rows**

Use a grid with a 22px icon column and copy column. Backup state receives only a subtle `--ok` wash and danger state only a subtle `--danger` wash; neither uses a saturated large panel or heavy colored outline.

- [ ] **Step 4: Quiet the footer and align actions**

Keep the sticky footer and hairline separation, reduce vertical padding, and set `.approval-card__actions { justify-content: flex-end; }`. Keep the existing primary button class. Make refusal neutral at rest and danger-colored on hover/focus. Preserve the mobile column layout.

- [ ] **Step 5: Run static and focused verification**

Run:

```bash
npm run test:unit -- src/components/chat/ApprovalCard.contracts.test.ts
npm run typecheck
```

Expected: both commands exit 0 with no architecture, theme-token, radius, motion, i18n, or TypeScript failures.

---

### Task 3: Verify the rendered states and ship the stable Review build

**Files:**
- Verify: `opensquilla-webui/src/components/chat/ApprovalCard.vue`
- Build output: `dist/desktop-electron/mac-arm64/OpenSquilla.app`
- Stable copy: `/Applications/OpenSquilla Sandbox Review 2026-08-05.app`

**Interfaces:**
- Consumes: the refreshed Web bundle and existing packaged approval flow.
- Produces: tested screenshots and a stable signed local Review application.

- [ ] **Step 1: Run the complete Web regression suite**

Run:

```bash
npm run test:unit
npm run build
```

Expected: all unit tests, typecheck, architecture checks, and bundle verification pass.

- [ ] **Step 2: Run the design pre-flight audit**

Confirm the card uses one theme/token system, one accent, the existing radius scale, no gradient or decorative motion, readable button contrast, no wrapped desktop button label, explicit mobile stacking, visible focus, and explicit non-color risk text.

- [ ] **Step 3: Build and verify the Electron application**

From `desktop/electron`, run:

```bash
npm run pack:local
npm run verify:package
npm run verify:gateway-smoke
npm run test:bundled-runtimes
npm run test:sandbox-default
```

Expected: package, gateway, bundled runtime, and sandbox policy verifiers all exit 0.

- [ ] **Step 4: Render a real destructive approval**

Launch the packaged app with the existing isolated Review profile, enable Safe mode, request deletion of a disposable `/private/tmp` target, and stop at the approval decision. Capture desktop and narrow viewport screenshots. Verify the exact path, backup state, refusal action, focus state, and primary action are visible and that no internal tool name appears.

- [ ] **Step 5: Install and sign the stable Review copy**

Close the previous Review process, copy the verified bundle to `/Applications/OpenSquilla Sandbox Review 2026-08-05.app`, ad-hoc sign it recursively, and run:

```bash
codesign --verify --deep --strict --verbose=2 '/Applications/OpenSquilla Sandbox Review 2026-08-05.app'
```

Expected: the application is valid on disk and satisfies its designated requirement.

- [ ] **Step 6: Commit the implementation**

```bash
git add opensquilla-webui/src/components/chat/ApprovalCard.vue \
  opensquilla-webui/src/components/chat/ApprovalCard.contracts.test.ts
git commit -m "style(webui): refine approval card"
```
