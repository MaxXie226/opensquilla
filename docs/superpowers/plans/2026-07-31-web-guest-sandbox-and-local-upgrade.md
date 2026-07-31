# Web Guest Sandbox and Local Upgrade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the desktop/standalone-Gateway deployment boundary, enforce the approved remote Web guest file policy across every bundled runtime, and produce a locally verified Windows client by upgrading an isolated installation of the current official v0.5.2 release.

**Architecture:** Authentication remains server-computed. Remote Web requests with no or invalid token receive an immutable `guest.safe` execution overlay: Safe mode, the configured default workspace as the only write root, built-in credential and OpenSquilla authority roots denied for reads, and no workspace lifecycle authority. Authenticated Safe mode keeps read-all behavior. The desktop-owned Gateway remains loopback-only; LAN hosting remains a standalone Gateway concern.

**Tech Stack:** Python 3.12+, Pydantic, pytest, Electron 42, TypeScript 6, Vue 3, Vitest, electron-builder/NSIS, PowerShell.

## Global Constraints

- Only unauthenticated or invalid-token **remote Web** executions receive built-in sensitive-path read denial.
- Desktop owners, valid-token Web users, CLI callers, and other authenticated callers keep Safe mode read-all behavior, except existing OpenSquilla authority protections.
- Web guests may write only beneath the Gateway-configured `workspace_dir`; files and ordinary subdirectories inside it are allowed.
- No client-supplied workspace, mount, run mode, approval, or environment value may widen the Web guest boundary.
- Desktop-owned Gateway processes always bind `127.0.0.1`; standalone Gateway keeps `--listen 0.0.0.0`.
- Missing and invalid tokens have identical effective execution permissions.
- Sandbox or workspace setup failure must deny the guest turn without host fallback.
- The local upgrade rehearsal starts from official stable `v0.5.2`, published 2026-07-29, and never publishes artifacts or modifies a remote release.
- Preserve unrelated changes already present in the working tree.

---

### Task 1: Remove Desktop LAN Configuration

**Files:**
- Modify: `opensquilla-webui/src/components/settings/SandboxSettingsPanel.vue`
- Modify: `opensquilla-webui/src/composables/settings/useSandboxSettings.ts`
- Modify: `opensquilla-webui/src/types/sandbox.ts`
- Modify: `opensquilla-webui/src/components/settings/SandboxSettingsPanel.test.ts`
- Modify: `desktop/electron/src/main.ts`
- Modify: `tests/test_desktop/test_electron_startup_contract.py`
- Modify: `docs/configuration.md`

**Interfaces:**
- Consumes: desktop platform capability `ownsGateway`; Gateway CLI `--listen`.
- Produces: desktop UI with no listener/CIDR controls and an invariant desktop child bind of `127.0.0.1`.

- [ ] **Step 1: Write failing UI and desktop contract tests**

Add assertions that the Sandbox settings panel has no `sandbox-listen-lan`
control and never calls `config.patch` with `host` or
`auth.allowed_client_cidrs`. Extend the desktop startup contract to assert the
spawned child receives:

```python
assert "'--bind'," in source
assert "'127.0.0.1'," in source
assert "OPENSQUILLA_GATEWAY_HOST" not in desktop_spawn_block
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run:

```powershell
python -m pytest tests/test_desktop/test_electron_startup_contract.py -q
Set-Location opensquilla-webui
npm test -- --run src/components/settings/SandboxSettingsPanel.test.ts
```

Expected: the Web UI test fails because the LAN controls and `config.patch`
path still exist.

- [ ] **Step 3: Remove the desktop LAN controls and persistence path**

Delete `lanDraft`, `saveLan`, `allowedCidr`, restart messaging, LAN listener
markup, and `SandboxLanSettings`. Keep named-token management in the Sandbox
module. Preserve the desktop spawn:

```ts
args.push('--bind', '127.0.0.1')
```

Document that `config.host` applies to standalone Gateway startup, not the
desktop-owned child.

- [ ] **Step 4: Run focused tests**

Expected: both focused suites pass and static search finds no
`sandbox-listen-lan` in renderer code.

- [ ] **Step 5: Commit the coherent desktop boundary change**

Commit message:

```text
fix: keep desktop gateway loopback only
```

---

### Task 2: Bind Web Guest Authority to the Default Workspace

**Files:**
- Modify: `src/opensquilla/gateway/rpc_sessions.py`
- Modify: `src/opensquilla/sandbox/guest_profile.py`
- Modify: `tests/test_gateway/test_guest_safe_sessions.py`
- Modify: `tests/test_gateway/test_rpc_sessions.py`

**Interfaces:**
- Consumes: `Principal.has("guest.safe")`, `source_hint["caller_kind"]`,
  `resolve_agent_workspace_dir()`.
- Produces: a Web-only `guest_safe` route using the configured default
  workspace and a scrubbed per-turn HOME/TEMP located inside that workspace.

- [ ] **Step 1: Replace ephemeral-workspace expectations with failing default-workspace tests**

Test all of the following:

```python
assert profile.workspace == configured_workspace.resolve()
assert profile.home.is_relative_to(profile.workspace)
assert profile.temp.is_relative_to(profile.workspace)
assert profile.run_context().workspace == str(configured_workspace.resolve())
assert profile.host_home_mounted is False
```

Add an RPC test showing a guest capability on a non-Web caller is not given the
remote-Web read-deny overlay. Add a Web test proving a client-supplied project
workspace is ignored.

- [ ] **Step 2: Run the focused tests and verify failure**

Run:

```powershell
python -m pytest tests/test_gateway/test_guest_safe_sessions.py tests/test_gateway/test_rpc_sessions.py -q
```

Expected: existing `GuestProfileFactory` creates a temporary standalone
workspace and the new assertions fail.

- [ ] **Step 3: Implement an in-workspace guest profile**

Change the factory to accept the authoritative workspace:

```python
GuestProfileFactory.create(
    task_id,
    workspace=configured_workspace,
    runtime_roots=runtime_roots,
)
```

Create only a uniquely named scratch root below
`workspace/.opensquilla-guest/`. Put scrubbed `HOME`, `USERPROFILE`, `TMP`, and
`TEMP` below that root. Cleanup may remove only that generated scratch root,
never the configured workspace.

Compute the overlay only when:

```python
web_guest_safe = (
    source_hint.get("caller_kind") == "web"
    and ctx.principal.has("guest.safe")
    and not principal_has_host_execute(ctx.principal)
)
```

Force `RunMode.SAFE`, remove caller mounts, and ignore caller/project workspace
selection.

- [ ] **Step 4: Run focused tests**

Expected: all guest session tests pass, including missing/invalid-token parity
and mid-run authority immutability.

- [ ] **Step 5: Commit**

Commit message:

```text
fix: pin web guests to the default workspace
```

---

### Task 3: Compile the Non-Bypassable Web Guest File Profile

**Files:**
- Modify: `src/opensquilla/sandbox/file_policy.py`
- Modify: `src/opensquilla/sandbox/integration.py`
- Modify: `src/opensquilla/tools/types.py`
- Modify: `tests/test_sandbox/test_file_policy.py`
- Modify: `tests/test_sandbox/test_shell_safe_policy_integration.py`
- Modify: `tests/test_sandbox/test_path_access.py`

**Interfaces:**
- Produces:

```python
compile_web_guest_file_profile(
    policy: SandboxPolicy,
    *,
    workspace: str | os.PathLike[str] | PurePath,
    authority_roots: Sequence[str | os.PathLike[str] | PurePath] = (),
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
    home: str | PurePath | None = None,
) -> FileSystemPermissionProfile
```

- [ ] **Step 1: Write failing permission-profile tests**

For Windows and POSIX profiles, assert:

```python
assert profile.resolve(ordinary_host_file) is FileSystemAccess.READ
assert profile.resolve(workspace / "new.txt") is FileSystemAccess.WRITE
assert profile.resolve(outside / "new.txt") is FileSystemAccess.READ
assert profile.resolve(home / ".ssh" / "id_ed25519") is FileSystemAccess.DENY
assert profile.resolve(state_dir / "sessions.db") is FileSystemAccess.DENY
assert profile.resolve(custom_deny / "file.txt") is FileSystemAccess.READ
```

Add canonical/lexical tests for `..`, case folding, symlinks, and Windows
junction-equivalent path variants. Add a test rejecting a workspace nested
beneath a built-in sensitive or authority root.

- [ ] **Step 2: Run the tests and verify failure**

Run:

```powershell
python -m pytest tests/test_sandbox/test_file_policy.py tests/test_sandbox/test_path_access.py tests/test_sandbox/test_shell_safe_policy_integration.py -q
```

Expected: the current guest profile is read-only and does not deny built-in
sensitive reads.

- [ ] **Step 3: Implement the profile compiler**

Build a host-readable profile with one write root:

```python
profile = FileSystemPermissionProfile.workspace(
    workspace=workspace_path,
    denied_read_roots=(*builtin_roots, *authority_roots),
    host_root_readonly=True,
    tmp_writable=False,
    tmpdir_env_writable=False,
)
```

Append custom deny-write roots with `READ`, preserving their existing
authenticated Safe mode semantics. Validate the workspace before execution and
return a stable `GUEST_DEFAULT_WORKSPACE_UNSAFE` failure if it intersects or
descends from a sensitive/authority root.

In `active_file_system_profile()`, return this profile directly for
`tool_context.guest_safe`; do not call `as_read_only()` and do not merge a
broader runtime write profile over it.

- [ ] **Step 4: Run focused tests**

Expected: the matrix and path-escape tests pass.

- [ ] **Step 5: Commit**

Commit message:

```text
feat: enforce web guest file boundaries
```

---

### Task 4: Enforce the Same Boundary Across Tools and Bundled Runtimes

**Files:**
- Modify: `src/opensquilla/tools/builtin/filesystem.py`
- Modify: `src/opensquilla/tools/builtin/shell.py`
- Modify: `src/opensquilla/sandbox/backend/windows_default.py`
- Modify: `src/opensquilla/sandbox/backend/windows_default_runner.py`
- Modify: `tests/test_tools/test_shell_sensitive.py`
- Modify: `tests/test_sandbox/test_windows_default_backend.py`
- Modify: `tests/test_sandbox/test_windows_default_runner.py`
- Modify: `tests/test_sandbox/test_windows_native_smoke.py`

**Interfaces:**
- Consumes: `ToolContext.guest_safe` and the compiled
  `FileSystemPermissionProfile`.
- Produces: identical access decisions for structured file tools, Shell,
  bundled Python, bundled Node.js, Git Bash, and child processes.

- [ ] **Step 1: Add failing cross-tool tests**

Exercise direct reads and subprocess reads of `.ssh`, direct and indirect
outside-workspace writes, and allowed workspace writes. Assert stable failures:

```text
GUEST_SENSITIVE_PATH_DENIED
GUEST_WRITE_OUTSIDE_DEFAULT_WORKSPACE
```

Add a process test where a permitted helper is created in the workspace and
then attempts an outside write. Add a symlink/junction retarget test between
approval and execution.

- [ ] **Step 2: Run focused tests and verify failure**

Run:

```powershell
python -m pytest tests/test_tools/test_shell_sensitive.py tests/test_sandbox/test_windows_default_backend.py tests/test_sandbox/test_windows_default_runner.py -q
```

- [ ] **Step 3: Route every access through the compiled profile**

Structured tools check lexical and canonical targets before opening. Shell and
code runtimes receive only the compiled sandbox grants and scrubbed
environment. The side-effect tracker restores or rejects any observed outside
mutation. Guest hard denials never produce an approval request.

- [ ] **Step 4: Run focused and Windows native smoke tests**

Expected: sensitive reads and outside writes fail in every runtime while
workspace reads/writes succeed.

- [ ] **Step 5: Commit**

Commit message:

```text
fix: close web guest runtime escapes
```

---

### Task 5: Preserve Workspace, Approval, and Token UX Contracts

**Files:**
- Modify: `tests/test_gateway/test_rpc_workspaces.py`
- Modify: `tests/test_gateway/test_named_tokens.py`
- Modify: `tests/test_gateway/test_auth_guest_safe.py`
- Modify: `opensquilla-webui/src/stores/rpc.test.ts`
- Modify: `opensquilla-webui/src/locales/zh-Hans.json`
- Modify: `opensquilla-webui/src/locales/en.json`
- Modify: `docs/approvals-and-permissions.md`
- Modify: `docs/configuration.md`

**Interfaces:**
- Consumes: owner-only workspace RPCs and principal metadata in Hello.
- Produces: hidden workspace controls for guests, stable guest-safe copy, and
  documented token transition behavior.

- [ ] **Step 1: Add failing contract tests**

Assert no/invalid token parity, valid named-token `host.execute`, owner-only
workspace RPC rejection, and no guest ability to approve an out-of-boundary
write. Assert the Web UI exposes no project picker to a non-owner principal.

- [ ] **Step 2: Run focused Python and Vitest suites**

- [ ] **Step 3: Align messages and documentation**

Use concise copy:

```text
访客安全模式：可读取普通文件，只能修改默认工作区；凭据文件不可读取。
```

Keep recursive-delete confirmation and backup behavior inside the allowed
workspace. Keep custom deny-write paths mutation-only.

- [ ] **Step 4: Re-run focused tests**

- [ ] **Step 5: Commit**

Commit message:

```text
docs: explain web guest safety boundary
```

---

### Task 6: Security Review and Regression Verification

**Files:**
- Modify only files implicated by review findings.
- Record: `artifacts/local-upgrade/security-review.md`

**Interfaces:**
- Produces: a requirement-by-requirement evidence ledger and fixes for every
  confirmed bypass.

- [ ] **Step 1: Run static and focused verification**

Run:

```powershell
python -m pytest tests/test_gateway/test_auth_guest_safe.py tests/test_gateway/test_guest_safe_sessions.py tests/test_gateway/test_rpc_workspaces.py tests/test_sandbox tests/test_tools/test_shell_sensitive.py -q
Set-Location opensquilla-webui
npm test -- --run
npm run build
Set-Location ..\desktop\electron
npm run build
npm run verify:package
npm run test:bundled-runtimes
```

- [ ] **Step 2: Audit bypass classes**

Review source and tests for: Web provenance spoofing, invalid-token divergence,
client workspace injection, alternate tool paths, subprocess effects, symlink
and junction traversal, TOCTOU retargeting, environment-secret inheritance,
sandbox-unavailable fallback, workspace-inside-sensitive-root, approvals as
elevation, and stale desktop `0.0.0.0`.

- [ ] **Step 3: Write failing tests for each confirmed issue, then fix**

Every fix follows red/green order and is added to the evidence ledger with the
test command and result.

- [ ] **Step 4: Run the complete affected suite again**

- [ ] **Step 5: Commit review fixes**

Commit message:

```text
fix: harden reviewed guest sandbox boundaries
```

---

### Task 7: Build a Local Client and Rehearse the Official v0.5.2 Upgrade

**Files:**
- Create: `desktop/electron/scripts/test-local-official-upgrade.ps1`
- Create: `artifacts/local-upgrade/upgrade-report.json`
- Create: `artifacts/local-upgrade/upgrade-report.md`
- Produce: `dist/desktop-electron/OpenSquilla-0.5.3-local.1-win-x64.exe`

**Interfaces:**
- Consumes: official GitHub v0.5.2 release assets and SHA256SUMS.
- Produces: a local-only installer and evidence that an isolated official
  v0.5.2 installation upgrades without losing profile/task/config data.

- [ ] **Step 1: Implement the isolated upgrade harness**

The script must:

1. Query `https://api.github.com/repos/opensquilla/opensquilla/releases/latest`
   and require tag `v0.5.2`, `draft=false`, `prerelease=false`.
2. Download `SHA256SUMS` and `OpenSquilla-0.5.2-win-x64.exe`.
3. Verify the installer SHA-256 before execution.
4. Create a unique temp install root, user-data root, HOME, and USERPROFILE.
5. Install v0.5.2 silently into the temp install root.
6. Launch it with the isolated user-data directory, seed only synthetic
   configuration/tasks, and record hashes plus the listening sockets.
7. Quit it fully, including the tray-owned Gateway.
8. Install the local `0.5.3-local.1` NSIS package over the same install root.
9. Relaunch with the same isolated profile and verify version, task/config/token
   preservation, default `/control/chat/new`, and desktop loopback-only sockets.
10. Uninstall and remove only the verified temp roots.

- [ ] **Step 2: Build the local-only installer**

Run:

```powershell
Set-Location desktop/electron
npm run build:web
npm run build:gateway
npm run build
npx electron-builder --win nsis --publish never --config.extraMetadata.version=0.5.3-local.1
```

Expected artifact:

```text
dist/desktop-electron/OpenSquilla-0.5.3-local.1-win-x64.exe
```

- [ ] **Step 3: Verify the package**

Run package verification, gateway smoke, bundled runtime tests, and the mock
update UI flow. Record hashes and sizes.

- [ ] **Step 4: Run the isolated official-to-local upgrade rehearsal**

Run:

```powershell
powershell -ExecutionPolicy Bypass -File desktop/electron/scripts/test-local-official-upgrade.ps1
```

Expected: JSON report has `ok: true`, `fromVersion: "0.5.2"`,
`toVersion: "0.5.3-local.1"`, `profilePreserved: true`,
`tasksPreserved: true`, and `desktopLoopbackOnly: true`.

- [ ] **Step 5: Perform final completion audit**

Map every approved design requirement to source, automated test, packaged
runtime evidence, or upgrade report. Do not mark complete if any item is
missing or only inferred.

- [ ] **Step 6: Commit the harness and final fixes**

Commit message:

```text
test: rehearse local upgrade from official release
```
