# User-facing Approval Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make approvals user-facing, make an explicit denial end the current task, and give destructive file approvals an honest backup and irreversibility contract.

**Architecture:** The sandbox elevation layer will carry a typed, fingerprint-bound semantic display contract that the Gateway projects through an allowlist and the Web UI localizes. The agent approval continuation loop will treat explicit denial as terminal while allowing a second backup-override confirmation. The backup vault and structured file tools will back up existing targets, evict oldest complete entries when needed, and require a distinct no-backup approval when backup remains unavailable.

**Tech Stack:** Python 3.12, pytest, Pydantic, Vue 3, TypeScript, Vitest, Playwright, Electron.

## Global Constraints

- Never render raw tool names, namespaces, action kinds, reviewer fields, fingerprints, or canonical action payloads in the approval UI.
- An explicit human denial ends only the current task; the chat session remains usable.
- Creating a new file does not create a backup.
- Backups are opt-in through the existing compatible sandbox file setting, enabled by default for new and missing configurations.
- Space pressure evicts oldest complete backups before retrying the current backup.
- Persistent backup failure requires a second explicit no-backup, irreversible confirmation.
- Preserve fresh-install and upgraded-install compatibility on macOS, Windows, Linux, and Web.

---

### Task 1: Typed semantic approval display contract

**Files:**
- Modify: `src/opensquilla/sandbox/elevation.py`
- Modify: `tests/test_sandbox/test_elevation.py`
- Modify: `src/opensquilla/gateway/approval_events.py`
- Modify: `tests/test_gateway/test_approval_event_push.py`

**Interfaces:**
- Produces: `ApprovalDisplay(kind, target, destructive, irreversible, backup_state)` with `canonical_payload()` and `from_canonical_payload()`.
- Produces: `approval_display_fields()` fields `display_kind`, `display_target`, `destructive`, `irreversible`, and `backup_state`.
- Consumes: `ElevationAction.display` from later destructive tool tasks.

- [ ] **Step 1: Add failing elevation round-trip tests**

```python
def test_elevation_action_round_trips_semantic_display() -> None:
    display = ApprovalDisplay(
        kind="delete",
        target="/tmp/archive",
        destructive=True,
        irreversible=False,
        backup_state="enabled",
    )
    action = _action(display=display)
    restored = ElevationAction.from_canonical_payload(action.canonical_payload())
    assert restored.display == display


def test_elevation_display_rejects_unknown_public_kind() -> None:
    with pytest.raises(ValueError, match="invalid_approval_display_kind"):
        ApprovalDisplay.from_canonical_payload({"kind": "exec_command"})
```

- [ ] **Step 2: Verify the tests fail because `ApprovalDisplay` does not exist**

Run: `uv run pytest -q tests/test_sandbox/test_elevation.py -k 'semantic_display or public_kind'`

Expected: collection or assertion failure naming the missing display contract.

- [ ] **Step 3: Implement the typed canonical display contract**

```python
ApprovalDisplayKind = Literal[
    "delete", "modify", "create", "run_command", "run_code",
    "network_access", "path_access", "plugin_permission", "sensitive_operation",
]
BackupState = Literal[
    "not_applicable", "enabled", "disabled", "unavailable_requires_confirmation",
]

@dataclass(frozen=True)
class ApprovalDisplay:
    kind: ApprovalDisplayKind
    target: str = ""
    destructive: bool = False
    irreversible: bool = False
    backup_state: BackupState = "not_applicable"

    def canonical_payload(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "target": self.target,
            "destructive": self.destructive,
            "irreversible": self.irreversible,
            "backup_state": self.backup_state,
        }
```

Add `display: ApprovalDisplay | None = None` to `ElevationAction`, serialize it inside the canonical payload, and validate it on restore. Because the display is fingerprint-bound, the approval cannot be relabeled after creation.

- [ ] **Step 4: Add failing Gateway projection tests**

```python
def test_elevation_projection_exposes_semantics_not_internal_names() -> None:
    info = _elevation_info(
        action_kind="fs.recursive_delete",
        tool_name="exec_command",
        display={
            "kind": "delete",
            "target": "/Users/example/archive",
            "destructive": True,
            "irreversible": False,
            "backup_state": "enabled",
        },
    )
    payload = build_approval_event_payload(info)
    assert payload["display_kind"] == "delete"
    assert payload["display_target"] == "/Users/example/archive"
    assert "exec_command" not in json.dumps(payload)
    assert "fs.recursive_delete" not in json.dumps(payload)


def test_unknown_approval_projects_safe_generic_display() -> None:
    payload = build_approval_event_payload(_unknown_internal_approval())
    assert payload["display_kind"] == "sensitive_operation"
    assert payload["tool_name"] == ""
```

- [ ] **Step 5: Verify Gateway tests fail on missing semantic fields and raw fallback**

Run: `uv run pytest -q tests/test_gateway/test_approval_event_push.py -k 'semantics_not_internal or safe_generic'`

Expected: missing `display_kind` and/or raw internal identifier assertion failure.

- [ ] **Step 6: Implement the allowlisted Gateway projection**

Parse only the canonical action's `display` mapping through `ApprovalDisplay.from_canonical_payload()`. Emit safe empty defaults for malformed/legacy actions:

```python
{
    "display_kind": "sensitive_operation",
    "display_target": "",
    "destructive": False,
    "irreversible": False,
    "backup_state": "not_applicable",
}
```

Keep redacted command display for genuine command approvals, but stop using `approvalKind` or nested action identifiers as `tool_name` fallbacks. Add identical fields to snapshot and WebSocket payloads.

- [ ] **Step 7: Run Task 1 tests and commit**

Run: `uv run pytest -q tests/test_sandbox/test_elevation.py tests/test_gateway/test_approval_event_push.py`

Expected: all selected tests pass.

Commit only Task 1 files with message: `fix: project approvals as user-facing actions`.

---

### Task 2: Explicit denial terminates the current task and chained approval continues safely

**Files:**
- Modify: `src/opensquilla/engine/agent.py`
- Modify: `tests/test_engine/test_interactive_approval_retry.py`
- Modify: `tests/test_engine/turn_runner/test_stream_consumer_stage_unit.py`

**Interfaces:**
- Produces: a terminal `ToolResult` for queue resolution `denied`.
- Produces: an approval continuation loop capable of waiting for a second approval returned by a resumed tool.

- [ ] **Step 1: Replace the existing continuation expectation with failing terminal-denial assertions**

```python
@pytest.mark.asyncio
async def test_explicit_denial_ends_turn_without_second_provider_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # Drive one approval, resolve it False, and exhaust the turn.
    assert len(provider.calls) == 1
    assert not provider.alternate_delete_called
    assert any(
        isinstance(event, ToolResultEvent)
        and json.loads(event.result)["status"] == "approval_denied"
        for event in events
    )
```

Also add a test where the first resumed execution returns a second
`approval_required` payload; approve it and assert the same task resumes exactly once.

- [ ] **Step 2: Verify denial test fails with two provider calls**

Run: `uv run pytest -q tests/test_engine/test_interactive_approval_retry.py -k 'explicit_denial_ends or chained_approval'`

Expected: denial test reports two calls under current behavior and chained approval stops early.

- [ ] **Step 3: Implement terminal denial and a bounded approval chain**

Refactor the single approval wait into a loop bound by the existing iteration/tool deadline. On explicit denial create:

```python
ToolResult(
    tool_use_id=tc.tool_use_id,
    tool_name=tc.tool_name,
    content=json.dumps(denial_payload, ensure_ascii=False),
    is_error=False,
    terminates_turn=True,
)
```

Set `turn_yielded` for any explicit denial, do not append the denial as a new provider prompt, and preserve the `ToolResultEvent` for UI/audit. If resumed execution returns another pending approval, suspend the updated call, wait for that exact queue ID, and repeat. Expiry remains non-denial and retains its existing outcome.

- [ ] **Step 4: Run engine tests and commit**

Run: `uv run pytest -q tests/test_engine/test_interactive_approval_retry.py tests/test_engine/turn_runner/test_stream_consumer_stage_unit.py`

Expected: all selected tests pass and terminal denial makes only one provider request.

Commit only Task 2 files with message: `fix: end tasks after approval denial`.

---

### Task 3: Backup vault evicts oldest complete entries before retry

**Files:**
- Modify: `src/opensquilla/sandbox/backup_vault.py`
- Modify: `tests/test_sandbox/test_backup_vault.py`
- Modify: `src/opensquilla/sandbox/file_mutation_broker.py`
- Modify: `tests/test_sandbox/test_file_mutation_broker.py`

**Interfaces:**
- Produces: `BackupVault.backup_many(targets, quota_bytes) -> tuple[BackupReceipt, ...]`.
- Produces: `BackupUnavailable` carrying a sanitized reason plus optional size/quota.
- Preserves: complete-entry manifests and oldest-first eviction.

- [ ] **Step 1: Add failing capacity and multi-target tests**

```python
def test_backup_many_evicts_oldest_before_current_backup(tmp_path: Path) -> None:
    vault = BackupVault(tmp_path / "vault")
    old = _backup_bytes(vault, "old", b"o" * 8, quota=16, created_at=1)
    first = tmp_path / "first.txt"; first.write_bytes(b"a" * 6)
    second = tmp_path / "second.txt"; second.write_bytes(b"b" * 6)
    receipts = vault.backup_many((first, second), quota_bytes=16)
    assert not old.entry_path.exists()
    assert [item.original_path for item in receipts] == [str(first), str(second)]


def test_oversize_current_backup_removes_old_entries_then_reports_unavailable(
    tmp_path: Path,
) -> None:
    with pytest.raises(BackupUnavailable) as raised:
        vault.backup_many((oversize,), quota_bytes=8)
    assert vault.list_receipts() == ()
    assert raised.value.size_bytes > raised.value.quota_bytes
```

- [ ] **Step 2: Verify tests fail because `backup_many` and `BackupUnavailable` are absent**

Run: `uv run pytest -q tests/test_sandbox/test_backup_vault.py -k 'backup_many or oversize_current'`

- [ ] **Step 3: Implement pre-eviction and grouped backup**

Compute exact target sizes without following symlink contents, deduplicate targets, evict oldest receipts until `existing + required <= quota`, and stage/publish the new backups. If `required > quota`, evict old entries then raise `BackupUnavailable`. Convert persistent staging/publish `OSError` into `BackupUnavailable` after cleaning incomplete staging; never expose raw exception text to approval payloads.

- [ ] **Step 4: Update recursive deletion to use the generalized failure contract**

`FileMutationBroker.execute()` uses `backup_many((plan.target,), ...)`. Extend the exact one-use no-backup override to bind both oversize and persistent backup-unavailable failures while keeping target identity validation.

- [ ] **Step 5: Run vault and broker tests and commit**

Run: `uv run pytest -q tests/test_sandbox/test_backup_vault.py tests/test_sandbox/test_file_mutation_broker.py`

Expected: all selected tests pass.

Commit only Task 3 files with message: `feat: evict old backups before destructive changes`.

---

### Task 4: Back up approved destructive file mutations and require no-backup reconfirmation

**Files:**
- Create: `src/opensquilla/sandbox/destructive_backup.py`
- Create: `tests/test_sandbox/test_destructive_backup.py`
- Modify: `src/opensquilla/tools/builtin/filesystem.py`
- Modify: `src/opensquilla/tools/builtin/patch.py`
- Modify: `src/opensquilla/tools/builtin/shell.py`
- Modify: `tests/test_tools/test_filesystem_tools.py`
- Modify: `tests/test_tools/test_apply_patch_gates.py`
- Modify: `tests/test_sandbox/test_shell_safe_policy_integration.py`
- Modify: `opensquilla-webui/src/components/settings/SandboxSettingsPanel.vue`
- Modify: `opensquilla-webui/src/locales/en.json`
- Modify: `opensquilla-webui/src/locales/zh-Hans.json`
- Modify: `opensquilla-webui/src/locales/ja.json`
- Modify: `opensquilla-webui/src/locales/fr.json`
- Modify: `opensquilla-webui/src/locales/de.json`
- Modify: `opensquilla-webui/src/locales/es.json`

**Interfaces:**
- Produces: `DestructiveBackupGate.evaluate(action, approval_id, targets, policy, state_dir)` returning an envelope, execution authority, and backup receipts.
- Consumes: `ApprovalDisplay` and `BackupVault.backup_many`.

- [ ] **Step 1: Add failing gate tests for enabled, disabled, and unavailable backup states**

```python
def test_enabled_mutation_backs_up_existing_target_after_first_approval(
    tmp_path: Path,
) -> None:
    first = gate.evaluate(action, None, (target,), policy=enabled, state_dir=state)
    queue.resolve(first.approval_id, True)
    resumed = gate.evaluate(action, first.approval_id, (target,), policy=enabled, state_dir=state)
    assert resumed.allowed is True
    assert resumed.receipts[0].original_path == str(target)


def test_backup_failure_requests_distinct_irreversible_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resumed = _approve_first_with_forced_backup_failure()
    assert resumed.envelope["status"] == "approval_required"
    pending = queue.get(resumed.envelope["approval_id"])
    assert pending.params["action"]["display"]["backup_state"] == "unavailable_requires_confirmation"
    assert pending.params["action"]["display"]["irreversible"] is True
```

- [ ] **Step 2: Verify the new gate tests fail**

Run: `uv run pytest -q tests/test_sandbox/test_destructive_backup.py`

- [ ] **Step 3: Implement the fingerprint-bound destructive backup gate**

The first action uses `backup_state=enabled` or `disabled`. After its approval:

- a new target returns no receipt and executes;
- disabled policy executes with an irreversible display already acknowledged;
- enabled policy backs up every existing target;
- `BackupUnavailable` creates a second action with the same tool, targets, content digest, and a distinct `_without_backup` action kind/display;
- the second approval is reconstructed from the current exact tool call and consumed without in-memory authority.

- [ ] **Step 4: Add failing structured-tool integration tests**

Cover approved overwrite/edit/delete and patch update/delete. Assert the old bytes exist in the vault before the target changes. Assert Add File creates no backup. Assert a denied second approval leaves targets unchanged.

Run each new test alone and confirm it fails before integration code is changed.

- [ ] **Step 5: Integrate structured tools and recursive delete display**

Use the destructive gate only for existing exact targets that already require approval. Attach semantic displays to recursive delete, protected writes/edits, patch operations, safe command, and code execution. Return backup receipt summaries in the tool result without exposing vault authority paths.

Rename only the user-facing setting copy from “recursive deletion” to “destructive file changes”; retain `recursiveDeleteBackupEnabled` on the wire for upgrade compatibility.

- [ ] **Step 6: Run Task 4 tests and commit**

Run: `uv run pytest -q tests/test_sandbox/test_destructive_backup.py tests/test_tools/test_filesystem_tools.py tests/test_tools/test_apply_patch_gates.py tests/test_sandbox/test_shell_safe_policy_integration.py`

Expected: all selected tests pass.

Commit only Task 4 files with message: `feat: back up approved destructive file changes`.

---

### Task 5: Localized user-facing approval card

**Files:**
- Modify: `opensquilla-webui/src/types/parts.ts`
- Modify: `opensquilla-webui/src/composables/chat/useChatApprovals.ts`
- Modify: `opensquilla-webui/src/components/chat/ApprovalCard.vue`
- Modify: `opensquilla-webui/src/components/chat/ApprovalCard.contracts.test.ts`
- Modify: `opensquilla-webui/e2e/approval-card.spec.ts`
- Modify: `opensquilla-webui/src/locales/en.json`
- Modify: `opensquilla-webui/src/locales/zh-Hans.json`
- Modify: `opensquilla-webui/src/locales/ja.json`
- Modify: `opensquilla-webui/src/locales/fr.json`
- Modify: `opensquilla-webui/src/locales/de.json`
- Modify: `opensquilla-webui/src/locales/es.json`

**Interfaces:**
- Consumes: Gateway semantic display fields from Task 1.
- Produces: localized approval title, target, irreversibility warning, and backup-state message.

- [ ] **Step 1: Add failing component and E2E tests**

```ts
it('renders recursive deletion without internal identifiers', async () => {
  const { root } = await mountCard(approval({
    toolName: 'sandbox_elevation',
    displayKind: 'delete',
    displayTarget: '/Users/example/archive',
    destructive: true,
    irreversible: true,
    backupState: 'disabled',
  }))
  expect(root.textContent).toContain('Delete files or folders')
  expect(root.textContent).toContain('cannot be recovered')
  expect(root.textContent).toContain('Settings')
  expect(root.textContent).not.toContain('sandbox_elevation')
  expect(root.textContent).not.toContain('exec_command')
})
```

Also assert `namespace`, agent ID, and generic JSON arguments are absent.

- [ ] **Step 2: Verify UI tests fail on raw tool rendering**

Run: `cd opensquilla-webui && npm test -- --run src/components/chat/ApprovalCard.contracts.test.ts`

- [ ] **Step 3: Implement semantic parsing and rendering**

Extend snapshot/push types and `InterruptApprovalData`. Legacy payloads get
`displayKind='sensitive_operation'`; never derive a display title from
`toolName`. `ApprovalCard.vue` localizes `displayKind`, uses the exact public
target, renders strong destructive/irreversible styling, and displays:

- enabled: a backup will be made and oldest backups may be removed;
- disabled: no backup will be made, recovery may be impossible, with the Settings path;
- unavailable: this execution has no backup and requires explicit confirmation.

Keep the exact redacted command only for `run_command`.

- [ ] **Step 4: Add translations and run UI tests**

Run: `cd opensquilla-webui && npm test -- --run src/components/chat/ApprovalCard.contracts.test.ts src/composables/chat/useChatApprovals.resolution.test.ts`

Run: `cd opensquilla-webui && npx playwright test e2e/approval-card.spec.ts`

Expected: selected unit and E2E tests pass.

- [ ] **Step 5: Commit Task 5**

Commit only Task 5 files with message: `fix: present approvals in user language`.

---

### Task 6: Compatibility, build, full verification, and packaged macOS proof

**Files:**
- Modify: `desktop/electron/scripts/probe-local-official-upgrade.mjs`
- Modify: `opensquilla-webui/e2e/behavior-contracts.spec.ts`
- Modify: `docs/sandbox-security.md`

**Interfaces:**
- Verifies all previous tasks as one release candidate.

- [ ] **Step 1: Add fresh/upgrade compatibility assertions**

Assert a missing legacy backup field loads enabled, an existing false value remains false, and the updated label/approval payload works in both fresh and upgraded profiles.

- [ ] **Step 2: Run focused Python and Web suites**

Run:

```bash
uv run pytest -q \
  tests/test_gateway/test_approval_event_push.py \
  tests/test_engine/test_interactive_approval_retry.py \
  tests/test_sandbox/test_backup_vault.py \
  tests/test_sandbox/test_file_mutation_broker.py \
  tests/test_sandbox/test_destructive_backup.py \
  tests/test_tools/test_filesystem_tools.py \
  tests/test_tools/test_apply_patch_gates.py
cd opensquilla-webui && npm test -- --run
```

- [ ] **Step 3: Run full Python suite and Web build**

Run: `uv run pytest -q`

Run: `cd opensquilla-webui && npm run build`

Expected: zero failures and `gateway/static/dist/index.html` exists.

- [ ] **Step 4: Run desktop verification and package probes**

Run:

```bash
cd desktop/electron
npm run build
npm run verify:package
npm run test:bundled-runtimes
```

After `npm run pack:local`, invoke `node scripts/probe-local-official-upgrade.mjs`
twice with the packaged executable and explicit temporary `--user-data-dir`,
`--home`, and `--state-dir` paths: once with empty directories and
`--expect-sandbox-mode safe`, and once after
`scripts/seed-upgrade-profile.py` with the legacy setting set to false.

- [ ] **Step 5: Run isolated packaged macOS scenario**

Use a temporary userData/state root and disposable target directory:

1. open Settings and verify backup copy;
2. request recursive deletion;
3. verify the card shows the semantic operation, exact target, irreversibility, and backup state without internal identifiers;
4. deny and verify the task becomes idle with no second provider request or approval;
5. send a new message and verify the session remains usable;
6. approve a disposable deletion with backup enabled and verify a backup receipt exists;
7. force backup-unavailable, verify the second strong confirmation, deny it, and verify the target remains.

- [ ] **Step 6: Review diff and commit compatibility/docs changes**

Run: `git diff --check` and inspect `git diff --stat` plus every modified hunk. Preserve the pre-existing 16 uncommitted sandbox reliability files and stage only files belonging to this implementation.

Commit message: `test: cover approval safety across fresh and upgraded clients`.
