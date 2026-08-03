# Windows Sandbox First-Setup Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce first Windows Safe-mode setup latency without changing any security rule or verification boundary, and keep the UI visibly responsive while UAC-backed setup runs.

**Architecture:** The elevated helper retains its existing account, firewall, WFP, marker, and ACL sequence. Only firewall command dispatch changes from one PowerShell process per rule to a single fail-fast PowerShell script; the Vue dialog adds local elapsed-time guidance while the existing blocking RPC is pending.

**Tech Stack:** Python 3.12+, pytest, PowerShell, Vue 3, TypeScript, Vitest, Electron packaging.

## Global Constraints

- Preserve every existing firewall rule name, SID scope, protocol, address, and port.
- Preserve UAC, owner-only RPC authorization, marker validation, ACL hardening, WFP installation, and forced live capability verification.
- Remote Web clients must never trigger Windows setup.
- UAC cancellation must keep Full Access selected and remain retryable.

---

### Task 1: Batch firewall installation

**Files:**
- Modify: `tests/test_sandbox/test_windows_default_firewall.py`
- Modify: `src/opensquilla/sandbox/backend/windows_default_firewall.py`

**Interfaces:**
- Consumes: `powershell_firewall_commands(specs) -> tuple[str, ...]`
- Produces: `install_firewall_rules(specs) -> None` with one `subprocess.run` call and unchanged error contract.

- [ ] **Step 1: Write a failing test**

  Add a test that monkeypatches `subprocess.run`, calls `install_firewall_rules` with all generated specs, and asserts exactly one invocation whose `-Command` argument contains every rule name and starts with `$ErrorActionPreference = 'Stop'`.

- [ ] **Step 2: Verify the test fails**

  Run `.venv\Scripts\python.exe -m pytest tests/test_sandbox/test_windows_default_firewall.py -q` and confirm the new test reports nine calls instead of one.

- [ ] **Step 3: Implement the minimal batch**

  Join the existing commands with `"; "`, prefix the script with `$ErrorActionPreference = 'Stop'; `, invoke PowerShell once, and keep the current `firewall_rule_install_failed` exception on a non-zero exit code.

- [ ] **Step 4: Verify green**

  Re-run the focused firewall test file and the Windows network/setup tests.

- [ ] **Step 5: Commit**

  Commit only the Python implementation and tests with `perf: batch Windows firewall setup`.

### Task 2: Responsive setup progress copy

**Files:**
- Modify: `opensquilla-webui/src/components/settings/SandboxSettingsPanel.test.ts`
- Modify: `opensquilla-webui/src/components/settings/SandboxSettingsPanel.vue`
- Modify: the six locale files below `opensquilla-webui/src/i18n/locales/`

**Interfaces:**
- Consumes: `sandboxSetupPending: Ref<boolean>`
- Produces: a neutral `data-testid="sandbox-setup-progress"` line whose text advances on elapsed-time thresholds and is cleared after completion.

- [ ] **Step 1: Write a failing timer test**

  Use Vitest fake timers, keep `sandbox.setup.ensure` pending, click Continue, and assert the progress line advances from authorization to protection guidance without closing the modal.

- [ ] **Step 2: Verify the test fails**

  Run the single panel test and confirm `sandbox-setup-progress` is absent.

- [ ] **Step 3: Implement minimal progress state**

  Start a one-second interval when Continue begins, derive three localized messages from elapsed seconds, clear the interval in `finally` and `onUnmounted`, and keep the Continue button disabled while pending.

- [ ] **Step 4: Verify green and locale parity**

  Run the panel tests, TypeScript checks, architecture checks, and i18n parity checks.

- [ ] **Step 5: Commit**

  Commit the Vue, tests, and locale changes with `feat: show Windows sandbox setup progress`.

### Task 3: Rebuild and end-to-end verification

**Files:**
- Update generated artifacts under `artifacts/local-upgrade/` and `artifacts/first-sandbox-setup/` only.

**Interfaces:**
- Consumes: the packaged Windows installer and existing upgrade/first-setup probes.
- Produces: passing reports for official update compatibility and first UAC setup latency.

- [ ] **Step 1: Run regressions**

  Run focused backend setup tests, full frontend unit tests, type/architecture checks, and production build verification.

- [ ] **Step 2: Build a new local installer**

  Build the gateway, bundled runtimes, Web UI, and Electron NSIS installer with a new local version suffix and record SHA-256.

- [ ] **Step 3: Rehearse official update**

  Run `test-local-official-upgrade.ps1` from official `0.5.2` to the new installer and require preserved profile, tasks, token, loopback-only binding, and `/control/chat/new`.

- [ ] **Step 4: Rehearse first setup**

  Remove the test sandbox account, verify startup creates no account and triggers no UAC, then approve the Settings flow and require a ready marker, matching SID, live capability success, saved Safe mode, and a shorter measured setup time.

- [ ] **Step 5: Open the final build**

  Launch the packaged app visibly on `/control/chat/new`, open Settings > Sandbox, and leave the verified Safe-mode state available for user inspection.
