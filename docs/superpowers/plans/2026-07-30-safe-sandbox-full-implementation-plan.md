# Safe Sandbox and Full Access Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the three-mode sandbox product with canonical Safe/Full behavior, preserve direct-update compatibility, add configurable file/command/network/runtime policy, provide guest-safe and named-token LAN access, and fix packaged child-process startup across supported desktop platforms.

**Architecture:** Existing sandbox backends remain the OS enforcement layer. A canonical Safe/Full compatibility codec, principal capability resolver, immutable policy snapshot, structured file broker, backup vault, command/network policy engines, and frozen-aware runtime launcher are inserted at the existing Gateway/RPC/tool boundaries. Legacy names are accepted only by migration and protocol adapters; all new storage, REST v2, WS protocol 4, UI state, and events use `safe` or `full`.

**Tech Stack:** Python 3.12, Starlette, Pydantic v2, SQLite/yoyo migrations, Typer, Vue 3, TypeScript, Vitest, Playwright, Electron, PyInstaller, platform sandbox backends.

## Global Constraints

- Canonical run modes are exactly `safe` and `full`; old names remain input/output adapters only.
- Fresh installs default to Safe; direct updates preserve the old effective preference using the design truth table.
- Safe policy is deny-write only for user files; ordinary authenticated Safe reads remain allowed.
- OpenSquilla authority, token, upgrade snapshot, one-time grant, and Backup Vault roots remain unreadable from Safe.
- Guest-safe never mounts host HOME, host project paths, authority data, or sensitive host environment values.
- No Token and an invalid Token have identical guest-safe execution authority; invalid Token state is still rate-limited and audited.
- Full requires `host.execute`; an explicit Full request never silently degrades to guest-safe.
- File/command/network rules and recursive-delete backup apply only to Safe/guest-safe; Full bypasses them.
- Recursive-delete backup defaults on with quota `3221225472` bytes and evicts oldest committed entries.
- Command decision precedence is auto-allow prefix, approval prefix, built-in high-risk, then default auto-run.
- Domain precedence is most-specific match, with deny winning equal-specificity ties.
- Safe uses bundled runtimes first; Full uses host `PATH` first with bundled fallback.
- Supported packaged matrix is Windows x64, macOS arm64/x64, and Linux x64 with glibc baseline no higher than 2.28.
- LAN transport remains HTTP/WS without TLS in this release and trusts only the actual socket peer.
- Running Safe work is never replayed automatically on the host after a backend failure.
- Upgrade snapshots are retained for manual recovery; automatic upgrade rollback is not implemented.
- Existing untracked user files are not staged, rewritten, deleted, or moved.

---

## File Structure

### Compatibility and execution identity

- `src/opensquilla/sandbox/run_mode.py`: canonical `RunMode`, new-value validation, legacy entry delegates.
- `src/opensquilla/sandbox/legacy_codec.py`: isolated old-name truth table and per-protocol encode/decode.
- `src/opensquilla/sandbox/mode_resolver.py`: desired/effective mode resolution against principal and capability.
- `src/opensquilla/sandbox/run_mode_policy.py`: principal-facing policy payload in canonical or legacy form.
- `src/opensquilla/gateway/protocol.py`: protocol 4 negotiation and sandbox schema field.
- `src/opensquilla/gateway/websocket.py`: retain negotiated protocol on each connection and encode per connection.
- `src/opensquilla/gateway/routing.py`, `src/opensquilla/scheduler/ops.py`: consume canonical values after decode.

### Child launch and packaged runtimes

- `src/opensquilla/sandbox/runtime_launcher.py`: frozen/source internal child roles and user runtime PATH resolution.
- `desktop/electron/scripts/gateway-entry.py`: fail-closed `--internal-child` dispatch before Typer.
- `src/opensquilla/sandbox/backend/windows_default.py`, `bubblewrap.py`, `linux_filesystem.py`, `seatbelt.py`: request child roles through the launcher.
- `src/opensquilla/sandbox/runtime_manifest.py`: validate pinned bundled runtime inventory.
- `desktop/electron/runtime/runtime-manifest.json`: platform/arch asset pins, checksums, licenses, SBOM references.
- `desktop/electron/scripts/fetch-bundled-runtimes.mjs`: reproducible acquisition and verification.
- `desktop/electron/scripts/verify-package.mjs`: installed-layout runtime and child-role gates.

### Authentication and migration

- `src/opensquilla/gateway/auth.py`: capability-bearing principals, guest-safe outcome, constant-time verification.
- `src/opensquilla/gateway/token_store.py`: named and legacy token records plus failure limiter.
- `migrations/V029__sandbox_policy_tokens.py`: policy and token storage.
- `src/opensquilla/sandbox/upgrade_migration.py`: pre-Gateway inventory, snapshot, journal, canonical rewrite.
- `src/opensquilla/gateway/boot.py`, `src/opensquilla/cli/main.py`, `desktop/electron/src/gateway-lifecycle.ts`: run the coordinator before writers/Gateway startup.

### Safe policy

- `src/opensquilla/sandbox/policy_models.py`: versioned file/command/network/runtime settings.
- `src/opensquilla/sandbox/policy_store.py`: compare-and-swap persistence and immutable snapshots.
- `src/opensquilla/sandbox/file_policy.py`: built-in/custom deny-write matching and authority deny-read.
- `src/opensquilla/sandbox/file_mutation_broker.py`: exact approved structured mutations with identity recheck.
- `src/opensquilla/sandbox/backup_vault.py`: staged recursive backup, commit, quota eviction, restore metadata.
- `src/opensquilla/sandbox/command_policy.py`: tokenized prefix/high-risk/system-tool decisions.
- `src/opensquilla/sandbox/network_guard.py`: public-by-default domain decision with non-overridable SSRF checks.
- `src/opensquilla/sandbox/capability_service.py`: real backend canaries, cache fingerprint, retry/invalidation.
- Existing integration, escalation, filesystem, shell, network, and backend modules call these focused units.

### API and UI

- `src/opensquilla/gateway/rpc_sandbox.py`: canonical preference/policy/capability/token RPCs plus legacy handlers.
- `src/opensquilla/gateway/app.py`: `/api/v2/sandbox/*` and `/api/v1` codec routing.
- `opensquilla-webui/src/types/sandbox.ts`: canonical client types and one-way localStorage legacy decode.
- `opensquilla-webui/src/composables/chat/useChatRunModePreference.ts`: Safe default and desired/effective handling.
- `opensquilla-webui/src/components/chat/ChatComposerRunMode.vue`: two choices, disabled Safe without extra status.
- `opensquilla-webui/src/components/settings/SandboxSettingsPanel.vue`: settings module.
- `opensquilla-webui/src/composables/settings/useSandboxSettings.ts`: independent save boundaries and conflicts.
- `opensquilla-webui/src/composables/setup/settingsSections.ts`, `SettingsDialog.vue`: register the module.
- `opensquilla-webui/src/locales/*.json`: user-visible copy.
- `desktop/electron/src/main.ts`, `preload.cts`: startup warning suppression and restart handoff.

---

### Task 1: Canonical Safe/Full Vocabulary and Legacy Codec

**Files:**
- Create: `src/opensquilla/sandbox/legacy_codec.py`
- Modify: `src/opensquilla/sandbox/run_mode.py`
- Modify: `src/opensquilla/sandbox/config.py`
- Modify: `src/opensquilla/scheduler/ops.py`
- Test: `tests/test_sandbox/test_run_modes.py`
- Test: `tests/test_sandbox/test_legacy_codec.py`

**Interfaces:**
- Produces: `RunMode.SAFE`, `RunMode.FULL`, `decode_legacy_run_mode(value, context)`, `encode_run_mode_for_protocol(mode, protocol)`.
- Consumes: legacy fields from config, CLI, scheduler, stored events, REST v1, and WS 1-3.

- [ ] **Step 1: Write failing canonical and truth-table tests**

```python
def test_canonical_run_mode_values_are_safe_and_full() -> None:
    assert [mode.value for mode in RunMode] == ["safe", "full"]

@pytest.mark.parametrize(
    ("value", "expected"),
    [("standard", RunMode.SAFE), ("trusted", RunMode.SAFE), ("managed", RunMode.SAFE),
     ("full", RunMode.FULL), ("bypass", RunMode.FULL)],
)
def test_legacy_aliases_decode_one_way(value: str, expected: RunMode) -> None:
    assert decode_legacy_run_mode(value, context=LegacyModeContext.EXPLICIT) is expected
```

- [ ] **Step 2: Run tests and confirm legacy enum failures**

Run: `python -m pytest tests/test_sandbox/test_run_modes.py tests/test_sandbox/test_legacy_codec.py -q`
Expected: FAIL because `SAFE`, `LegacyModeContext`, and codec functions do not exist.

- [ ] **Step 3: Implement the isolated codec and canonical enum**

```python
class RunMode(StrEnum):
    SAFE = "safe"
    FULL = "full"

class LegacyModeContext(StrEnum):
    EXPLICIT = "explicit"
    CONFIG = "config"
    WIRE_V1 = "wire_v1"

def decode_legacy_run_mode(value: object, *, context: LegacyModeContext) -> RunMode:
    key = str(value).strip().lower()
    if key in {"standard", "standard-sandbox", "standard_sandbox", "trusted",
               "trusted-sandbox", "trusted_sandbox", "trust", "managed", "on", "off"}:
        return RunMode.SAFE
    if key in {"full", "full-host-access", "full_host_access", "bypass"}:
        return RunMode.FULL
    raise LegacyModeDecodeError(value=value, context=context)
```

- [ ] **Step 4: Update internal consumers to branch only on Safe/Full**

```python
def execution_target(mode: object) -> Literal["sandbox", "host"]:
    return "host" if normalize_run_mode(mode) is RunMode.FULL else "sandbox"
```

- [ ] **Step 5: Run focused tests**

Run: `python -m pytest tests/test_sandbox/test_run_modes.py tests/test_sandbox/test_legacy_codec.py tests/test_sandbox/test_cli_run_modes.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/opensquilla/sandbox/run_mode.py src/opensquilla/sandbox/legacy_codec.py src/opensquilla/sandbox/config.py src/opensquilla/scheduler/ops.py tests/test_sandbox
git commit -m "refactor: canonicalize safe and full run modes"
```

### Task 2: Frozen-Aware Internal Child Launcher

**Files:**
- Create: `src/opensquilla/sandbox/runtime_launcher.py`
- Modify: `desktop/electron/scripts/gateway-entry.py`
- Modify: `src/opensquilla/sandbox/backend/windows_default.py`
- Modify: `src/opensquilla/sandbox/backend/bubblewrap.py`
- Modify: `src/opensquilla/sandbox/backend/linux_filesystem.py`
- Modify: `src/opensquilla/sandbox/backend/seatbelt.py`
- Modify: `src/opensquilla/gateway/rpc_sandbox.py`
- Test: `tests/test_sandbox/test_runtime_launcher.py`
- Test: `tests/test_sandbox/test_windows_default_backend.py`
- Test: `tests/test_desktop/test_electron_startup_contract.py`

**Interfaces:**
- Produces: `ChildRole`, `internal_child_argv(role, args=())`, `dispatch_internal_child(argv)`.
- Consumes: `sys.frozen`, `sys.executable`, fixed role registry.

- [ ] **Step 1: Write source/frozen argv and unknown-role tests**

```python
def test_frozen_worker_uses_internal_child(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert internal_child_argv(ChildRole.FILESYSTEM_WORKER)[:3] == (
        sys.executable, "--internal-child", "filesystem-worker")

def test_source_worker_uses_python_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert internal_child_argv(ChildRole.FILESYSTEM_WORKER)[:3] == (
        sys.executable, "-m", "opensquilla.sandbox.filesystem_worker")
```

- [ ] **Step 2: Run and confirm failure**

Run: `python -m pytest tests/test_sandbox/test_runtime_launcher.py tests/test_sandbox/test_windows_default_backend.py -q`
Expected: FAIL because Windows still builds `sys.executable -m`.

- [ ] **Step 3: Implement fixed child-role registry**

```python
class ChildRole(StrEnum):
    FILESYSTEM_WORKER = "filesystem-worker"
    LINUX_HELPER = "linux-helper"
    DIRECTORY_PICKER = "directory-picker"
    CAPABILITY_PROBE = "capability-probe"

_ROLE_MODULES = {
    ChildRole.FILESYSTEM_WORKER: "opensquilla.sandbox.filesystem_worker",
    ChildRole.LINUX_HELPER: "opensquilla.sandbox.backend.linux_helper",
    ChildRole.DIRECTORY_PICKER: "opensquilla.gateway.windows_directory_picker",
}
```

- [ ] **Step 4: Dispatch before importing Typer**

```python
if len(sys.argv) >= 3 and sys.argv[1] == "--internal-child":
    from opensquilla.sandbox.runtime_launcher import dispatch_internal_child
    raise SystemExit(dispatch_internal_child(sys.argv[2:]))
```

- [ ] **Step 5: Replace backend argv construction**

```python
helper_argv = internal_child_argv(
    ChildRole.FILESYSTEM_WORKER,
    args=("--payload-env",),
)
```

- [ ] **Step 6: Run focused and frozen-contract tests**

Run: `python -m pytest tests/test_sandbox/test_runtime_launcher.py tests/test_sandbox/test_windows_default_backend.py tests/test_desktop/test_electron_startup_contract.py -q`
Expected: PASS and no frozen path contains `gateway.exe -m`.

- [ ] **Step 7: Commit**

```bash
git add src/opensquilla/sandbox/runtime_launcher.py src/opensquilla/sandbox/backend desktop/electron/scripts/gateway-entry.py src/opensquilla/gateway/rpc_sandbox.py tests
git commit -m "fix: launch packaged sandbox workers by child role"
```

### Task 3: Named Tokens, Capabilities, Guest-Safe Authentication, and Rate Limiting

**Files:**
- Create: `src/opensquilla/gateway/token_store.py`
- Modify: `src/opensquilla/gateway/auth.py`
- Modify: `src/opensquilla/gateway/scopes.py`
- Modify: `src/opensquilla/gateway/websocket.py`
- Modify: `src/opensquilla/gateway/config.py`
- Create: `migrations/V029__sandbox_policy_tokens.py`
- Test: `tests/test_gateway/test_auth_guest_safe.py`
- Test: `tests/test_gateway/test_named_tokens.py`
- Test: `tests/test_gateway/test_auth_rate_limit.py`

**Interfaces:**
- Produces: `Principal.capabilities`, `Principal.auth_state`, `TokenStore.verify()`, `AuthFailureLimiter`.
- Consumes: existing opaque config/environment tokens and real socket peer.

- [ ] **Step 1: Write guest-equivalence and explicit-Full rejection tests**

```python
def test_missing_and_invalid_token_have_same_guest_capabilities(resolver) -> None:
    missing = resolver.resolve_guest(auth_params={}, peer_ip="192.168.1.7")
    invalid = resolver.resolve_guest(auth_params={"token": "wrong"}, peer_ip="192.168.1.7")
    assert missing.capabilities == invalid.capabilities == frozenset({"guest.safe"})
    assert missing.auth_state == "guest"
    assert invalid.auth_state == "invalid"
```

- [ ] **Step 2: Run tests and confirm current connection-close behavior**

Run: `python -m pytest tests/test_gateway/test_auth_guest_safe.py tests/test_gateway/test_named_tokens.py tests/test_gateway/test_auth_rate_limit.py -q`
Expected: FAIL because invalid tokens resolve to `None`.

- [ ] **Step 3: Implement immutable capability principal**

```python
@dataclass(frozen=True)
class Principal:
    role: str
    scopes: frozenset[str]
    capabilities: frozenset[str]
    is_owner: bool
    authenticated: bool
    auth_state: Literal["authenticated", "guest", "invalid"]
    token_public_id: str | None = None
```

- [ ] **Step 4: Implement constant-time digest verification and named records**

```python
def verify_secret(record: TokenRecord, secret: str) -> bool:
    supplied = hashlib.sha256(secret.encode("utf-8")).digest()
    return secrets.compare_digest(record.secret_digest, supplied)
```

- [ ] **Step 5: Implement peer/public-id limiter**

```python
delay_seconds = min(30, (1, 2, 4, 8, 16, 30)[min(failures_after_burst, 5)])
```

- [ ] **Step 6: Keep invalid/missing LAN peers connected as guest-safe**

```python
principal = resolve_auth(
    config,
    auth_params=auth_params,
    role_claim=role_claim,
    peer_ip=peer_ip,
    token_store=token_store,
    failure_limiter=limiter,
)
if principal.auth_state == "invalid":
    await limiter.wait(peer_ip, principal.token_public_id)
conn.principal = principal
```

- [ ] **Step 7: Run auth tests**

Run: `python -m pytest tests/test_gateway/test_auth_guest_safe.py tests/test_gateway/test_named_tokens.py tests/test_gateway/test_auth_rate_limit.py tests/test_gateway/test_auth_debug_scope.py -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/opensquilla/gateway/auth.py src/opensquilla/gateway/token_store.py src/opensquilla/gateway/scopes.py src/opensquilla/gateway/websocket.py src/opensquilla/gateway/config.py migrations/V029__sandbox_policy_tokens.py tests/test_gateway
git commit -m "feat: add named tokens and guest-safe authentication"
```

### Task 4: Mode Resolver, Capability Lifecycle, and Soft Landing

**Files:**
- Create: `src/opensquilla/sandbox/mode_resolver.py`
- Create: `src/opensquilla/sandbox/capability_service.py`
- Modify: `src/opensquilla/sandbox/run_mode_policy.py`
- Modify: `src/opensquilla/sandbox/setup_runtime.py`
- Modify: `src/opensquilla/gateway/rpc_sandbox.py`
- Test: `tests/test_sandbox/test_mode_resolver.py`
- Test: `tests/test_sandbox/test_capability_service.py`

**Interfaces:**
- Produces: `ResolvedMode(desired_mode, effective_mode, fallback_reason, confirmation_required)`, `CapabilityReport`.
- Consumes: principal capabilities, guest status, cached real backend probe.

- [ ] **Step 1: Write principal × desired mode × availability tests**

```python
@pytest.mark.parametrize(
    ("guest", "host_execute", "available", "desired", "effective", "error"),
    [(False, True, False, "safe", "full", None),
     (True, False, False, "safe", None, "sandbox_unavailable_for_guest"),
     (True, False, True, "full", None, "host_capability_required")],
)
def test_mode_matrix(
    guest: bool,
    host_execute: bool,
    available: bool,
    desired: str,
    effective: str | None,
    error: str | None,
) -> None:
    principal = principal_fixture(
        auth_state="guest" if guest else "authenticated",
        capabilities={"host.execute"} if host_execute else {"guest.safe"},
    )
    report = capability_report(available=available)
    if error:
        with pytest.raises(ModeResolutionError, match=error):
            resolve_mode(desired, principal, report)
        return
    resolved = resolve_mode(desired, principal, report)
    assert resolved.effective_mode.value == effective
```

- [ ] **Step 2: Run and confirm missing resolver**

Run: `python -m pytest tests/test_sandbox/test_mode_resolver.py tests/test_sandbox/test_capability_service.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement probe report and cache key**

```python
@dataclass(frozen=True)
class CapabilityReport:
    available: bool
    backend: str
    platform: str
    code: str
    reason: str
    setup_supported: bool
    restart_required: bool
    probe_version: int
    capabilities: frozenset[str]
```

- [ ] **Step 4: Probe backend process, worker, read/write, authority deny-read, deny-write, and network**

```python
required = {"process", "filesystem-worker", "denyWriteCarveout", "authorityDenyRead"}
available = report.canaries_passed and required.issubset(report.capabilities)
```

- [ ] **Step 5: Implement soft-landing decision without mutating desired mode**

```python
if desired is RunMode.SAFE and not capability.available:
    if principal.has("host.execute"):
        return ResolvedMode(desired, RunMode.FULL, capability.code, True)
    raise ModeResolutionError("sandbox_unavailable_for_guest")
```

- [ ] **Step 6: Run tests**

Run: `python -m pytest tests/test_sandbox/test_mode_resolver.py tests/test_sandbox/test_capability_service.py tests/test_sandbox/test_setup_runtime.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/opensquilla/sandbox/mode_resolver.py src/opensquilla/sandbox/capability_service.py src/opensquilla/sandbox/run_mode_policy.py src/opensquilla/sandbox/setup_runtime.py src/opensquilla/gateway/rpc_sandbox.py tests/test_sandbox
git commit -m "feat: resolve safe mode against real sandbox capability"
```

### Task 5: Versioned Sandbox Policy Store and API

**Files:**
- Create: `src/opensquilla/sandbox/policy_models.py`
- Create: `src/opensquilla/sandbox/policy_store.py`
- Modify: `src/opensquilla/gateway/rpc_sandbox.py`
- Modify: `src/opensquilla/gateway/app.py`
- Test: `tests/test_sandbox/test_policy_store.py`
- Test: `tests/test_sandbox/test_rpc_policy.py`
- Test: `tests/test_gateway/test_sandbox_v2_routes.py`

**Interfaces:**
- Produces: `SandboxPolicy`, `SandboxPolicyStore.read()`, `compare_and_swap(base_version, policy)`.
- Consumes: SQLite migration from Task 3 and current runtime config.

- [ ] **Step 1: Write default, validation, and conflict tests**

```python
def test_default_policy_has_three_gib_backup_quota() -> None:
    assert SandboxPolicy().files.backup_quota_bytes == 3 * 1024**3

def test_compare_and_swap_rejects_stale_version(store) -> None:
    first = store.read()
    store.compare_and_swap(first.policy_version, first)
    with pytest.raises(PolicyVersionConflict):
        store.compare_and_swap(first.policy_version, first)
```

- [ ] **Step 2: Run and confirm failure**

Run: `python -m pytest tests/test_sandbox/test_policy_store.py tests/test_sandbox/test_rpc_policy.py tests/test_gateway/test_sandbox_v2_routes.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement strict models**

```python
class SandboxPolicy(BaseModel):
    schema_version: Literal[2] = 2
    policy_version: int = 0
    files: FilePolicySettings = Field(default_factory=FilePolicySettings)
    commands: CommandPolicySettings = Field(default_factory=CommandPolicySettings)
    network: NetworkPolicySettings = Field(default_factory=NetworkPolicySettings)
    runtimes: RuntimePolicySettings = Field(default_factory=RuntimePolicySettings)
```

- [ ] **Step 4: Implement one-row compare-and-swap transaction**

```sql
UPDATE sandbox_policy
SET policy_version = policy_version + 1, policy_json = ?, updated_at = ?
WHERE singleton_id = 1 AND policy_version = ?
```

- [ ] **Step 5: Add canonical RPC and REST v2**

```python
@_d.method("sandbox.policy.update")
async def sandbox_policy_update(ctx, params):
    return store.compare_and_swap(params["basePolicyVersion"], params["policy"]).to_public_dict()
```

- [ ] **Step 6: Run tests**

Run: `python -m pytest tests/test_sandbox/test_policy_store.py tests/test_sandbox/test_rpc_policy.py tests/test_gateway/test_sandbox_v2_routes.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/opensquilla/sandbox/policy_models.py src/opensquilla/sandbox/policy_store.py src/opensquilla/gateway/rpc_sandbox.py src/opensquilla/gateway/app.py tests
git commit -m "feat: persist versioned sandbox policy"
```

### Task 6: File Deny-Write Policy and Authority Deny-Read

**Files:**
- Create: `src/opensquilla/sandbox/file_policy.py`
- Modify: `src/opensquilla/sandbox/sensitive_paths.py`
- Modify: `src/opensquilla/sandbox/permissions.py`
- Modify: `src/opensquilla/sandbox/platform_permissions.py`
- Modify: platform backend permission compilers.
- Test: `tests/test_sandbox/test_file_policy.py`
- Test: `tests/test_sandbox/test_authority_paths.py`

**Interfaces:**
- Produces: `FileDecision`, platform built-in deny-write path expansion, authority root enumeration.
- Consumes: immutable policy snapshot and normalized stable paths.

- [ ] **Step 1: Write platform list and authenticated read/write tests**

```python
def test_windows_builtin_deny_write_contains_ssh(monkeypatch) -> None:
    monkeypatch.setenv("USERPROFILE", r"C:\Users\alice")
    roots = builtin_deny_write_paths("win32")
    assert Path(r"C:\Users\alice\.ssh") in roots

def test_safe_read_is_allowed_but_authority_read_is_denied(policy, authority_root) -> None:
    assert decide_file_access("read", "/ordinary/file", policy).allowed
    assert decide_file_access("read", authority_root / "tokens.db", policy).code == \
        "sandbox_authority_read_denied"
```

- [ ] **Step 2: Run and confirm failure**

Run: `python -m pytest tests/test_sandbox/test_file_policy.py tests/test_sandbox/test_authority_paths.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement built-in and custom deny-write matching**

```python
@dataclass(frozen=True)
class FileDecision:
    allowed: bool
    approval_required: bool
    code: str | None
    matched_path: Path | None
```

- [ ] **Step 4: Compile authority roots as non-overridable deny-read/deny-write**

```python
authority_roots = (token_store_root, backup_vault_root, upgrade_snapshot_root, grant_store_root)
```

- [ ] **Step 5: Route blacklisted native writes to deny and structured writes to approval**

```python
if decision.approval_required and operation.structured:
    return await approval_service.request_exact(operation)
raise SandboxPolicyDenied(decision.code)
```

- [ ] **Step 6: Run platform policy tests**

Run: `python -m pytest tests/test_sandbox/test_file_policy.py tests/test_sandbox/test_authority_paths.py tests/test_sandbox/test_permission_profiles.py tests/test_sandbox/test_platform_permissions.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/opensquilla/sandbox tests/test_sandbox
git commit -m "feat: enforce safe file deny-write policy"
```

### Task 7: Structured File Mutation Broker and Recursive Backup Vault

**Files:**
- Create: `src/opensquilla/sandbox/file_mutation_broker.py`
- Create: `src/opensquilla/sandbox/backup_vault.py`
- Modify: `src/opensquilla/sandbox/filesystem_worker.py`
- Modify: `src/opensquilla/sandbox/integration.py`
- Modify: `src/opensquilla/tools/builtin/file_authoring.py`
- Test: `tests/test_sandbox/test_file_mutation_broker.py`
- Test: `tests/test_sandbox/test_backup_vault.py`

**Interfaces:**
- Produces: `MutationPlan`, `ObjectIdentity`, `BackupReceipt`, exact one-use approval execution.
- Consumes: FileDecision, approval ID, policy snapshot.

- [ ] **Step 1: Write TOCTOU, backup, quota, and oversize tests**

```python
def test_broker_rejects_identity_change_after_approval(broker, approved_plan) -> None:
    approved_plan.target.unlink()
    approved_plan.target.symlink_to("other")
    with pytest.raises(ObjectIdentityChanged):
        broker.execute(approved_plan)

def test_quota_evicts_oldest_committed_backup(vault) -> None:
    first = vault.commit_bytes("first", b"a" * 8)
    second = vault.commit_bytes("second", b"b" * 8)
    vault.enforce_quota(8)
    assert not vault.exists(first)
    assert vault.exists(second)
```

- [ ] **Step 2: Run and confirm failure**

Run: `python -m pytest tests/test_sandbox/test_file_mutation_broker.py tests/test_sandbox/test_backup_vault.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement handle/identity capture and parent recheck**

```python
@dataclass(frozen=True)
class ObjectIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int
```

- [ ] **Step 4: Implement staging then atomic vault publish**

```python
staged = vault.stage(target)
staged.verify_complete()
receipt = staged.publish()
broker.delete_exact(plan)
```

- [ ] **Step 5: Add strong recursive-delete approval copy and second confirmation for oversize**

```python
message = "递归删除会永久删除目录及其中全部内容，无法撤回。OpenSquilla 将先创建备份。"
```

- [ ] **Step 6: Run broker/integration tests**

Run: `python -m pytest tests/test_sandbox/test_file_mutation_broker.py tests/test_sandbox/test_backup_vault.py tests/test_sandbox/test_filesystem_worker.py tests/test_tools/test_file_authoring_tools.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/opensquilla/sandbox src/opensquilla/tools/builtin/file_authoring.py tests
git commit -m "feat: back up and broker recursive safe mutations"
```

### Task 8: Command Policy

**Files:**
- Create: `src/opensquilla/sandbox/command_policy.py`
- Modify: `src/opensquilla/tools/builtin/shell.py`
- Modify: `src/opensquilla/sandbox/integration.py`
- Test: `tests/test_sandbox/test_command_policy.py`
- Test: `tests/test_tools/test_shell_approval_policy.py`

**Interfaces:**
- Produces: `CommandDecision.AUTO`, `APPROVAL`, `DENY`, parsed token prefix.
- Consumes: policy snapshot, platform system-tool catalog, compound shell segments.

- [ ] **Step 1: Write precedence and compound command tests**

```python
def test_auto_allow_beats_approval_and_builtin_high_risk(policy) -> None:
    policy.commands.auto_allow_prefixes = [["git", "push"]]
    policy.commands.require_approval_prefixes = [["git"]]
    assert decide_command(["git", "push", "origin", "main"], policy).action == "auto"

def test_git_push_requires_approval_by_default(policy) -> None:
    assert decide_command(["git", "push"], policy).action == "approval"
```

- [ ] **Step 2: Run and confirm failure**

Run: `python -m pytest tests/test_sandbox/test_command_policy.py tests/test_tools/test_shell_approval_policy.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement tokenized matching and system-tool tri-state**

```python
def decide_command(argv, policy, platform):
    if matches_any(argv, policy.auto_allow_prefixes): return AUTO
    if matches_any(argv, policy.require_approval_prefixes): return APPROVAL
    if is_builtin_high_risk(argv): return APPROVAL
    if is_system_tool(argv, platform): return system_tool_decision(policy.system_tools)
    return AUTO
```

- [ ] **Step 4: Parse shell wrappers and segments fail-closed for rule matching**

```python
segments = parse_shell_segments(command)
return combine_decisions(decide_command(segment.argv, policy, platform) for segment in segments)
```

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_sandbox/test_command_policy.py tests/test_tools/test_shell_approval_policy.py tests/test_tools/test_shell_policy_windows.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/opensquilla/sandbox/command_policy.py src/opensquilla/sandbox/integration.py src/opensquilla/tools/builtin/shell.py tests
git commit -m "feat: apply configurable safe command approvals"
```

### Task 9: Public-by-Default Managed Network Policy

**Files:**
- Modify: `src/opensquilla/sandbox/network_guard.py`
- Modify: `src/opensquilla/sandbox/domain_validation.py`
- Modify: `src/opensquilla/sandbox/network_proxy.py`
- Modify: `src/opensquilla/sandbox/network_runtime.py`
- Modify: `src/opensquilla/sandbox/managed_proxy_env.py`
- Test: `tests/test_sandbox/test_network_guard.py`
- Test: `tests/test_sandbox/test_network_proxy.py`
- Test: `tests/test_sandbox/test_network_runtime.py`

**Interfaces:**
- Produces: most-specific allow/deny decision and managed HTTP CONNECT/SOCKS5 TCP enforcement.
- Consumes: `block_all_network`, allow/deny patterns, non-overridable SSRF classifier.

- [ ] **Step 1: Write default-open, specificity, tie-deny, redirect, and metadata tests**

```python
def test_equal_specificity_deny_wins(policy) -> None:
    policy.network.allow_domains = ["api.example.com"]
    policy.network.deny_domains = ["api.example.com"]
    assert decide_network_access("api.example.com", policy).allowed is False

def test_default_open_never_allows_metadata(policy) -> None:
    assert decide_network_access("169.254.169.254", policy).code == "metadata_blocked"
```

- [ ] **Step 2: Run and confirm failures**

Run: `python -m pytest tests/test_sandbox/test_network_guard.py tests/test_sandbox/test_network_proxy.py tests/test_sandbox/test_network_runtime.py -q`
Expected: FAIL where current allowlist defaults deny.

- [ ] **Step 3: Implement rule scoring and public default**

```python
score = (1 if exact else 0, len(normalized_suffix))
winner = max(matches, key=lambda item: (item.score, item.kind == "deny"))
```

- [ ] **Step 4: Enforce each redirect and post-DNS address**

```python
for address in await resolver.resolve(host):
    enforce_public_address(address)
```

- [ ] **Step 5: Keep raw outbound denied and inject npm/pip/Git proxy configuration**

```python
env.update({"HTTP_PROXY": proxy_url, "HTTPS_PROXY": proxy_url, "ALL_PROXY": socks_url})
```

- [ ] **Step 6: Run network tests**

Run: `python -m pytest tests/test_sandbox/test_network_guard.py tests/test_sandbox/test_network_proxy.py tests/test_sandbox/test_network_runtime.py tests/test_sandbox/test_managed_network_backends.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/opensquilla/sandbox tests/test_sandbox
git commit -m "feat: make safe public network policy configurable"
```

### Task 10: Guest Workspace and Environment Isolation

**Files:**
- Create: `src/opensquilla/sandbox/guest_profile.py`
- Modify: `src/opensquilla/sandbox/run_context_service.py`
- Modify: `src/opensquilla/gateway/routing.py`
- Modify: `src/opensquilla/gateway/rpc_sessions.py`
- Modify: `src/opensquilla/tools/types.py`
- Test: `tests/test_sandbox/test_guest_profile.py`
- Test: `tests/test_gateway/test_guest_safe_sessions.py`

**Interfaces:**
- Produces: temporary guest workspace, scrubbed environment, guest task cleanup.
- Consumes: Principal.auth_state/capabilities and runtime policy.

- [ ] **Step 1: Write host HOME/project/env denial tests**

```python
def test_guest_profile_mounts_only_temp_and_bundled_runtime(profile) -> None:
    assert profile.host_home_mounted is False
    assert all(root.kind in {"workspace", "bundled-runtime"} for root in profile.mounts)
    assert "AWS_SECRET_ACCESS_KEY" not in profile.environment
```

- [ ] **Step 2: Run and confirm failure**

Run: `python -m pytest tests/test_sandbox/test_guest_profile.py tests/test_gateway/test_guest_safe_sessions.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement guest context at routing boundary**

```python
if principal.has("guest.safe"):
    run_context = GuestProfileFactory.create(task_id, policy_snapshot)
```

- [ ] **Step 4: Reject explicit Full before any task materialization**

```python
if requested_mode is RunMode.FULL and not principal.has("host.execute"):
    raise RpcHandlerError("HOST_CAPABILITY_REQUIRED", "Full access requires a valid token.")
```

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_sandbox/test_guest_profile.py tests/test_gateway/test_guest_safe_sessions.py tests/test_gateway/test_project_workspace_execution.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/opensquilla/sandbox/guest_profile.py src/opensquilla/sandbox/run_context_service.py src/opensquilla/gateway src/opensquilla/tools/types.py tests
git commit -m "feat: isolate unauthenticated LAN tasks in guest safe"
```

### Task 11: Protocol 4, Per-Connection Codec, REST v2, and Legacy Clients

**Files:**
- Modify: `src/opensquilla/gateway/protocol.py`
- Modify: `src/opensquilla/gateway/websocket.py`
- Modify: `src/opensquilla/gateway/event_bridge.py`
- Modify: `src/opensquilla/gateway/rpc_sandbox.py`
- Modify: `src/opensquilla/gateway/app.py`
- Test: `tests/test_gateway/test_sandbox_protocol_codec.py`
- Test: `tests/test_gateway/test_sandbox_v2_routes.py`

**Interfaces:**
- Produces: connection protocol field, canonical schema v2 payload, legacy `trusted/full` encoding.
- Consumes: Task 1 codec and Task 4 resolved mode.

- [ ] **Step 1: Write mixed-connection broadcast test**

```python
async def test_broadcast_encodes_mode_per_connection(registry) -> None:
    legacy = connection(protocol=3)
    canonical = connection(protocol=4)
    await registry.broadcast("sandbox.mode.changed", {"runMode": "safe"})
    assert legacy.last_payload["runMode"] == "trusted"
    assert canonical.last_payload["runMode"] == "safe"
```

- [ ] **Step 2: Run and confirm global-payload failure**

Run: `python -m pytest tests/test_gateway/test_sandbox_protocol_codec.py tests/test_gateway/test_sandbox_v2_routes.py -q`
Expected: FAIL.

- [ ] **Step 3: Store negotiated protocol and sandbox schema on connection/RPC context**

```python
conn.protocol = negotiated
ctx.sandbox_schema_version = 2 if negotiated >= 4 else 1
```

- [ ] **Step 4: Encode immediately before each send**

```python
payload = codec.encode_payload(event, raw_payload, protocol=conn.protocol)
await conn.send_event(event, payload)
```

- [ ] **Step 5: Preserve WS 1-3 host fallback safety**

```python
if (
    protocol < 4
    and resolution.confirmation_required
    and not legacy_preauthorized(
        token_public_id=principal.token_public_id,
        failure_fingerprint=resolution.fallback_reason,
    )
):
    raise RpcHandlerError(
        "HOST_FALLBACK_CONFIRMATION_REQUIRED",
        "The sandbox is unavailable and this legacy client cannot confirm host fallback.",
    )
```

- [ ] **Step 6: Run protocol tests**

Run: `python -m pytest tests/test_gateway/test_sandbox_protocol_codec.py tests/test_gateway/test_sandbox_v2_routes.py tests/test_cli/test_gateway_rpc.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/opensquilla/gateway src/opensquilla/sandbox/legacy_codec.py tests
git commit -m "feat: add canonical sandbox protocol and legacy codec"
```

### Task 12: Direct-Update Migration Coordinator and Manual Recovery

**Files:**
- Create: `src/opensquilla/sandbox/upgrade_migration.py`
- Modify: `src/opensquilla/recovery/engine.py`
- Modify: `src/opensquilla/recovery/config_patch.py`
- Modify: `src/opensquilla/gateway/boot.py`
- Modify: `src/opensquilla/cli/main.py`
- Modify: `desktop/electron/src/gateway-lifecycle.ts`
- Test: `tests/test_migration/test_sandbox_direct_update.py`
- Test: `tests/test_recovery/test_sandbox_upgrade_journal.py`

**Interfaces:**
- Produces: idempotent prepared/committed journal, one retained snapshot, manual recovery report.
- Consumes: all released config fixtures and full database copies.

- [ ] **Step 1: Write released-fixture and interrupted-retry tests**

```python
@pytest.mark.parametrize("fixture", RELEASED_DESKTOP_FIXTURES)
def test_direct_update_preserves_mode_and_unknown_fields(fixture, coordinator) -> None:
    result = coordinator.run(fixture.home)
    assert result.canonical_mode in {"safe", "full"}
    assert result.unknown_fields_preserved
```

- [ ] **Step 2: Run and confirm missing coordinator**

Run: `python -m pytest tests/test_migration/test_sandbox_direct_update.py tests/test_recovery/test_sandbox_upgrade_journal.py -q`
Expected: FAIL.

- [ ] **Step 3: Inventory config, desktop preferences, SQLite, grants, approvals, scheduler, and tokens**

```python
stores = inventory_sandbox_stores(home)
snapshot = create_upgrade_snapshot(stores)
journal.prepare(snapshot, stores)
```

- [ ] **Step 4: Migrate idempotently and preserve unknown fields**

```python
patched = lossless_patch_sandbox_fields(original, canonical_mode=decoded.value)
```

- [ ] **Step 5: Gate Gateway startup and expose manual recovery only**

```python
report = ensure_sandbox_upgrade_migrated(home)
if not report.ok:
    raise GatewayStartupBlocked("migration_failed_manual_recovery_required")
```

- [ ] **Step 6: Run migration/recovery tests**

Run: `python -m pytest tests/test_migration/test_sandbox_direct_update.py tests/test_recovery/test_sandbox_upgrade_journal.py tests/test_migration/test_legacy_config_fixtures.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/opensquilla/sandbox/upgrade_migration.py src/opensquilla/recovery src/opensquilla/gateway/boot.py src/opensquilla/cli/main.py desktop/electron/src/gateway-lifecycle.ts tests
git commit -m "feat: migrate legacy sandbox state before gateway startup"
```

### Task 13: Bundled Node, Python, Git and Bash

**Files:**
- Create: `src/opensquilla/sandbox/runtime_manifest.py`
- Create: `desktop/electron/runtime/runtime-manifest.json`
- Create: `desktop/electron/scripts/fetch-bundled-runtimes.mjs`
- Modify: `desktop/electron/scripts/build-gateway.mjs`
- Modify: `desktop/electron/scripts/verify-package.mjs`
- Modify: `desktop/electron/package.json`
- Modify: `src/opensquilla/sandbox/runtime_launcher.py`
- Test: `tests/test_sandbox/test_runtime_manifest.py`
- Test: `desktop/electron/scripts/test-bundled-runtimes.mjs`

**Interfaces:**
- Produces: `BundledRuntimeResolver`, pinned manifest validation, Safe/Full PATH construction.
- Consumes: platform/arch, policy runtime toggles, packaged resource root.

- [ ] **Step 1: Write manifest and PATH precedence tests**

```python
def test_safe_path_puts_bundled_tools_first(manifest, host_path) -> None:
    path = BundledRuntimeResolver(manifest).path_for(RunMode.SAFE, host_path)
    assert path[0].name == "python"

def test_full_path_keeps_host_first(manifest, host_path) -> None:
    assert BundledRuntimeResolver(manifest).path_for(RunMode.FULL, host_path)[0] == host_path[0]
```

- [ ] **Step 2: Run and confirm failure**

Run: `python -m pytest tests/test_sandbox/test_runtime_manifest.py -q`
Expected: FAIL.

- [ ] **Step 3: Add schema-validated pinned manifest**

```json
{"schemaVersion":1,"runtimeSet":"2026-07-30","assets":{"windows-x64":{"node":{},"python":{},"gitBash":{}}}}
```

- [ ] **Step 4: Implement checksum-verified fetch without first-use downloads**

```javascript
if (sha256(bytes) !== asset.sha256) throw new Error(`checksum mismatch: ${asset.id}`)
```

- [ ] **Step 5: Add package verification and size gates**

```javascript
await smoke(runtime.python, ['--version'])
await smoke(runtime.node, ['--version'])
await smoke(runtime.git, ['--version'])
await smoke(runtime.bash, ['--version'])
```

- [ ] **Step 6: Run manifest, Electron build, and package verifier unit tests**

Run: `python -m pytest tests/test_sandbox/test_runtime_manifest.py -q`
Run: `npm run build && node scripts/test-bundled-runtimes.mjs`
Working directory: `desktop/electron`
Expected: PASS without network.

- [ ] **Step 7: Commit**

```bash
git add src/opensquilla/sandbox/runtime_manifest.py src/opensquilla/sandbox/runtime_launcher.py desktop/electron tests
git commit -m "feat: package pinned developer runtimes"
```

### Task 14: Two-Mode Composer and Sandbox Settings UI

**Files:**
- Modify: `opensquilla-webui/src/types/sandbox.ts`
- Modify: `opensquilla-webui/src/composables/chat/useChatRunModePreference.ts`
- Modify: `opensquilla-webui/src/components/chat/ChatComposerRunMode.vue`
- Create: `opensquilla-webui/src/composables/settings/useSandboxSettings.ts`
- Create: `opensquilla-webui/src/components/settings/SandboxSettingsPanel.vue`
- Modify: `opensquilla-webui/src/composables/setup/settingsSections.ts`
- Modify: `opensquilla-webui/src/components/settings/SettingsDialog.vue`
- Modify: `opensquilla-webui/src/locales/*.json`
- Test: `opensquilla-webui/src/types/sandbox.test.ts`
- Test: `opensquilla-webui/src/components/chat/ChatComposerRunMode.test.ts`
- Test: `opensquilla-webui/src/components/settings/SandboxSettingsPanel.test.ts`

**Interfaces:**
- Produces: canonical client type, two-option selector, settings editor with independent save states.
- Consumes: canonical policy/capability/token RPCs and legacy localStorage values.

- [ ] **Step 1: Write two-mode and legacy-localStorage tests**

```ts
expect(SANDBOX_RUN_MODES).toEqual(['safe', 'full'])
expect(normalizeSandboxRunMode('trusted')).toBe('safe')
expect(wrapper.findAll('[role="radio"]')).toHaveLength(2)
```

- [ ] **Step 2: Run and confirm three-mode failures**

Run: `npm run test:unit -- src/types/sandbox.test.ts src/components/chat/ChatComposerRunMode.test.ts src/components/settings/SandboxSettingsPanel.test.ts`
Working directory: `opensquilla-webui`
Expected: FAIL.

- [ ] **Step 3: Implement canonical types and one-way legacy read**

```ts
export type SandboxRunMode = 'safe' | 'full'
export function normalizeSandboxRunMode(value: unknown): SandboxRunMode {
  return value === 'full' || value === 'bypass' ? 'full' : 'safe'
}
```

- [ ] **Step 4: Render only Safe and Full; disable Safe quietly**

```ts
const options = [
  { value: 'safe', label: t('chat.composer.runModeSafe') },
  { value: 'full', label: t('chat.composer.runModeFull') },
]
```

- [ ] **Step 5: Implement file, command, network, runtimes, LAN, and token cards**

```ts
const policySave = await rpc.call('sandbox.policy.update', {
  basePolicyVersion: baseline.value.policyVersion,
  policy: draft.value,
})
```

- [ ] **Step 6: Run unit, type, architecture, and i18n checks**

Run: `npm run test:unit -- src/types/sandbox.test.ts src/components/chat/ChatComposerRunMode.test.ts src/components/settings/SandboxSettingsPanel.test.ts`
Run: `npm run typecheck`
Working directory: `opensquilla-webui`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add opensquilla-webui
git commit -m "feat: add safe and full sandbox settings UI"
```

### Task 15: Desktop Startup Warning, Suppression, and Safe-Unavailable UX

**Files:**
- Modify: `desktop/electron/src/main.ts`
- Modify: `desktop/electron/src/preload.cts`
- Modify: `opensquilla-webui/src/composables/chat/useSandboxSetupRecovery.ts`
- Modify: `opensquilla-webui/src/components/chat/SandboxSetupBanner.vue`
- Modify: `opensquilla-webui/src/locales/*.json`
- Test: `desktop/electron/scripts/test-sandbox-startup-warning.mjs`
- Test: `opensquilla-webui/src/components/chat/ChatComposerRunMode.test.ts`

**Interfaces:**
- Produces: Electron preference `sandboxUnavailableWarningSuppressed`, one startup prompt, settings-only diagnostics.
- Consumes: capability report and desired/effective mode.

- [ ] **Step 1: Write suppression and quiet-selector tests**

```javascript
assert.equal(await warningCount(firstLaunch), 1)
assert.equal(await warningCount(afterDontRemind), 0)
```

- [ ] **Step 2: Run and confirm failure**

Run: `npm run build && node scripts/test-sandbox-startup-warning.mjs`
Working directory: `desktop/electron`
Expected: FAIL.

- [ ] **Step 3: Implement one startup dialog with two actions**

```ts
buttons: [desktopT('sandbox.unavailable.acknowledge'), desktopT('sandbox.unavailable.suppress')]
```

- [ ] **Step 4: Remove normal chat banner/status and keep diagnostics in Settings**

```vue
<button :disabled="mode === 'safe' && !safeAvailable">安全模式</button>
```

- [ ] **Step 5: Run desktop and WebUI tests**

Run: `npm run build && node scripts/test-sandbox-startup-warning.mjs`
Working directory: `desktop/electron`
Run: `npm run test:unit -- src/components/chat/ChatComposerRunMode.test.ts`
Working directory: `opensquilla-webui`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add desktop/electron opensquilla-webui
git commit -m "feat: soft land unavailable sandbox without persistent UI"
```

### Task 16: Packaged Matrix, Upgrade Matrix, Documentation, and Final Review/Fix Pass

**Files:**
- Modify: `.github/workflows/desktop-fault-injection.yml`
- Modify: `.github/workflows/ci.yml`
- Create: `.github/scripts/verify-sandbox-package.mjs`
- Modify: `docs/tools-and-sandbox.md`
- Modify: `docs/configuration.md`
- Modify: `docs/approvals-and-permissions.md`
- Modify: `CHANGELOG.md`
- Test: `tests/test_sandbox/test_release_contract.py`
- Test: packaged smoke scripts and WebUI Playwright specs.

**Interfaces:**
- Produces: release-blocking platform/upgrade/runtime gates and accurate user documentation.
- Consumes: all previous task artifacts.

- [ ] **Step 1: Add release-contract tests for every completion criterion**

```python
def test_product_sources_do_not_expose_legacy_mode_names() -> None:
    assert no_legacy_names_outside_codec_and_fixtures()

def test_runtime_manifest_covers_required_matrix() -> None:
    assert required_targets() <= manifest_targets()
```

- [ ] **Step 2: Run full focused suite**

Run: `python -m pytest tests/test_sandbox tests/test_gateway/test_auth_guest_safe.py tests/test_gateway/test_named_tokens.py tests/test_migration/test_sandbox_direct_update.py tests/test_recovery/test_sandbox_upgrade_journal.py tests/test_tools/test_shell_approval_policy.py -q`
Expected: PASS.

- [ ] **Step 3: Run full Python static and non-live test gates**

Run: `python -m ruff check src tests`
Run: `python -m mypy src`
Run: `python -m pytest -m "not llm and not live_channel and not webui_browser and not tui_real_terminal" -q`
Expected: PASS.

- [ ] **Step 4: Run WebUI and Electron gates**

Run: `npm run typecheck && npm run test:unit && npm run build`
Working directory: `opensquilla-webui`
Run: `npm run verify:package && npm run test:gateway-lifecycle && npm run test:cli-invocation`
Working directory: `desktop/electron`
Expected: PASS.

- [ ] **Step 5: Build and smoke the available Windows packaged artifact**

Run: `npm run pack:local`
Working directory: `desktop/electron`
Expected: packaged gateway starts internal child roles and bundled Node/Python/Git/Bash smoke passes.

- [ ] **Step 6: Perform requirement-by-requirement self-review**

```text
Map every requirement in sections 2-18 of the design spec to code plus a passing test.
Record every contradiction, missing test, unsafe fallback, legacy-name leak, and UI mismatch.
```

- [ ] **Step 7: Automatically fix every review finding once**

```text
For each recorded finding: add a regression test that fails, implement the correction,
run the focused test, then rerun the affected subsystem suite.
```

- [ ] **Step 8: Run final verification after the repair pass**

Run: `git diff --check`
Run: `python -m pytest tests/test_sandbox tests/test_gateway tests/test_migration tests/test_recovery -q`
Run: `npm run typecheck && npm run test:unit`
Working directory: `opensquilla-webui`
Expected: PASS with no unresolved review findings.

- [ ] **Step 9: Commit**

```bash
git add .github docs CHANGELOG.md tests src opensquilla-webui desktop/electron
git commit -m "test: verify safe sandbox delivery matrix"
```
