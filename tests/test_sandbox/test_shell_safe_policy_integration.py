from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.integration import (
    active_file_system_profile,
    active_sandbox_policy,
    configure_runtime,
    reset_runtime,
)
from opensquilla.sandbox.operation_profile import OperationProfile
from opensquilla.sandbox.operation_runtime import SandboxOperationResult
from opensquilla.sandbox.path_validation import decide_path_access
from opensquilla.sandbox.permissions import (
    FileSystemAccess,
    FileSystemPermissionEntry,
    FileSystemPermissionProfile,
)
from opensquilla.sandbox.policy_models import FilePolicySettings, SandboxPolicy
from opensquilla.sandbox.run_context import MountGrant, RunContext
from opensquilla.sandbox.run_mode import RunMode
from opensquilla.tools.builtin import code_exec, filesystem, git, shell
from opensquilla.tools.types import ToolContext, current_tool_context


class _AllowedGate:
    allowed = True
    approval_id = "approved"


class _PendingGate:
    allowed = False
    approval_id = "pending"

    @staticmethod
    def to_envelope() -> dict[str, object]:
        return {"status": "approval_required", "approval_id": "pending"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "process_call",
    [
        lambda workspace: shell.exec_command(
            "Write-Output must-not-run",
            workdir=str(workspace),
            env={"OPENSQUILLA_GUEST_SAFE": "0"},
        ),
        lambda workspace: shell.background_process(
            "Write-Output must-not-run",
            workdir=str(workspace),
        ),
        lambda _workspace: code_exec.execute_code("print('must-not-run')"),
        lambda _workspace: git.git_status(),
    ],
    ids=("shell-env-override", "background", "python-code", "git"),
)
async def test_windows_guest_process_tools_fail_before_runtime_enrichment(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    process_call,
) -> None:
    """Guest authority, not a caller-controlled environment marker, denies launch."""

    runtime = configure_runtime(
        SandboxSettings(
            run_mode="standard",
            backend="noop",
            allow_legacy_mode=True,
        ),
        workspace=tmp_path,
    )
    runtime.backend = SimpleNamespace(name="windows_default")

    def enrichment_must_not_run(*_args, **_kwargs):
        raise AssertionError("Windows guest denial must precede runtime enrichment")

    monkeypatch.setattr(shell, "_runtime_shell_environment", enrichment_must_not_run)
    monkeypatch.setattr(
        shell,
        "_policy_with_windows_shell_runtime_mounts",
        enrichment_must_not_run,
    )
    token = current_tool_context.set(
        ToolContext(
            guest_safe=True,
            run_mode="safe",
            workspace_dir=str(tmp_path),
            environment={"PATH": "", "OPENSQUILLA_GUEST_SAFE": "1"},
        )
    )
    try:
        with pytest.raises(
            Exception,
            match="GUEST_WINDOWS_PROCESS_UNAVAILABLE",
        ):
            await process_call(tmp_path)
    finally:
        current_tool_context.reset(token)
        reset_runtime()


@pytest.mark.asyncio
async def test_windows_guest_file_tools_still_use_filesystem_worker(
    tmp_path,
) -> None:
    """The process fallback must not disable managed-workspace file operations."""

    calls: list[str] = []

    class FilesystemWorkerBackend:
        name = "windows_default"

        @staticmethod
        def operation_domains_supported() -> tuple[str, ...]:
            return ("filesystem",)

        async def run_operation(self, operation):
            calls.append(operation.kind)
            request = operation.request
            assert request.path is not None
            if operation.kind == "write_text":
                request.path.write_text(request.content, encoding="utf-8")
                return SandboxOperationResult(message="written", created=True)
            if operation.kind == "read_file":
                return SandboxOperationResult(
                    message=request.path.read_text(encoding="utf-8")
                )
            raise AssertionError(f"unexpected filesystem operation: {operation.kind}")

        async def run(self, _request):
            raise AssertionError("file tools must not use the process runner")

    runtime = configure_runtime(
        SandboxSettings(
            run_mode="standard",
            backend="noop",
            allow_legacy_mode=True,
        ),
        workspace=tmp_path,
    )
    runtime.backend = FilesystemWorkerBackend()
    target = tmp_path / "guest.txt"
    token = current_tool_context.set(
        ToolContext(
            guest_safe=True,
            run_mode="safe",
            workspace_dir=str(tmp_path),
            sandbox_file_system_profile=FileSystemPermissionProfile(
                entries=(
                    FileSystemPermissionEntry(
                        tmp_path,
                        FileSystemAccess.WRITE,
                    ),
                )
            ),
        )
    )
    try:
        assert await filesystem.write_file(str(target), "guest data") == "written"
        assert await filesystem.read_file(str(target)) == "guest data"
    finally:
        current_tool_context.reset(token)
        reset_runtime()

    assert calls == ["write_text", "read_file"]


def test_active_policy_comes_from_turn_snapshot() -> None:
    snapshot = SandboxPolicy(
        policy_version=7,
        files=FilePolicySettings(backup_quota_bytes=1234),
    )
    token = current_tool_context.set(
        ToolContext(run_mode="safe", sandbox_policy=snapshot)
    )
    try:
        first = active_sandbox_policy()
        first.files.backup_quota_bytes = 9999
        second = active_sandbox_policy()
    finally:
        current_tool_context.reset(token)

    assert first.policy_version == second.policy_version == 7
    assert first.files.backup_quota_bytes == 9999
    assert second.files.backup_quota_bytes == 1234


def test_saved_file_policy_compiles_into_the_live_safe_profile(tmp_path) -> None:
    protected = tmp_path / "protected"
    protected.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    snapshot = SandboxPolicy(
        files=FilePolicySettings(custom_deny_write_paths=[str(protected)])
    )
    token = current_tool_context.set(
        ToolContext(
            run_mode="safe",
            workspace_dir=str(tmp_path),
            sandbox_policy=snapshot,
            sandbox_gateway_config=SimpleNamespace(state_dir=str(state)),
        )
    )
    try:
        profile = active_file_system_profile(tmp_path)
    finally:
        current_tool_context.reset(token)

    assert profile is not None
    assert profile.default_access is FileSystemAccess.WRITE
    protected_write = decide_path_access(
        protected / "secret.txt",
        workspace=tmp_path,
        write=True,
        profile=profile,
    )
    authority_read = decide_path_access(
        state / "sessions.db",
        workspace=tmp_path,
        write=False,
        profile=profile,
    )
    ordinary_write = decide_path_access(
        tmp_path / "ordinary.txt",
        workspace=tmp_path,
        write=True,
        profile=profile,
    )
    assert protected_write.status != "allowed"
    assert authority_read.status == "blocked"
    assert ordinary_write.status == "allowed"


def test_guest_safe_profile_keeps_workspace_boundary_and_protected_carveouts(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    guest_home = tmp_path / "guest-home"
    guest_temp = tmp_path / "guest-temp"
    runtime_root = tmp_path / "runtime"
    protected = workspace / "protected"
    for directory in (protected, guest_home, guest_temp, runtime_root):
        directory.mkdir(parents=True)
    runtime = configure_runtime(
        SandboxSettings(
            run_mode="standard",
            backend="noop",
            allow_legacy_mode=True,
            exclude_slash_tmp=True,
            exclude_tmpdir_env_var=True,
        ),
        workspace=workspace,
    )
    snapshot = SandboxPolicy(
        files=FilePolicySettings(custom_deny_write_paths=[str(protected)])
    )
    token = current_tool_context.set(
        ToolContext(
            run_mode="safe",
            guest_safe=True,
            workspace_dir=str(workspace),
            sandbox_run_context=RunContext(
                run_mode=RunMode.SAFE,
                workspace=str(workspace),
                mounts=(
                    MountGrant(path=str(guest_home), access="rw", scope="once"),
                    MountGrant(path=str(guest_temp), access="rw", scope="once"),
                    MountGrant(path=str(runtime_root), access="ro", scope="once"),
                ),
            ),
            sandbox_policy=snapshot,
            sandbox_gateway_config=SimpleNamespace(state_dir=str(tmp_path / "state")),
        )
    )
    try:
        profile = active_file_system_profile(workspace)
    finally:
        current_tool_context.reset(token)
        reset_runtime()

    assert runtime is not None
    assert profile is not None
    protected_write = decide_path_access(
        protected / "secret.txt",
        workspace=workspace,
        write=True,
        profile=profile,
    )
    ordinary_write = decide_path_access(
        workspace / "ordinary.txt",
        workspace=workspace,
        write=True,
        profile=profile,
    )
    outside_write = decide_path_access(
        tmp_path / "outside.txt",
        workspace=workspace,
        write=True,
        profile=profile,
    )
    sensitive_read = decide_path_access(
        Path.home() / ".ssh" / "id_ed25519",
        workspace=workspace,
        write=False,
        profile=profile,
    )
    ordinary_read = decide_path_access(
        tmp_path / "ordinary-host-file.txt",
        workspace=workspace,
        write=False,
        profile=profile,
    )
    home_write = decide_path_access(
        guest_home / "notes.txt",
        workspace=workspace,
        write=True,
        profile=profile,
    )
    temp_write = decide_path_access(
        guest_temp / "scratch.txt",
        workspace=workspace,
        write=True,
        profile=profile,
    )
    runtime_read = decide_path_access(
        runtime_root / "python.exe",
        workspace=workspace,
        write=False,
        profile=profile,
    )
    assert protected_write.status != "allowed"
    assert ordinary_write.status == "allowed"
    assert outside_write.status != "allowed"
    assert sensitive_read.status == "blocked"
    assert ordinary_read.status != "allowed"
    assert (
        profile.resolve(tmp_path / "ordinary-host-file.txt")
        is FileSystemAccess.DENY
    )
    assert home_write.status == "allowed"
    assert temp_write.status == "allowed"
    assert runtime_read.status == "allowed"


@pytest.mark.asyncio
async def test_guest_safe_outside_write_cannot_request_or_consume_an_approval(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    configure_runtime(
        SandboxSettings(
            run_mode="standard",
            backend="noop",
            allow_legacy_mode=True,
            exclude_slash_tmp=True,
            exclude_tmpdir_env_var=True,
        ),
        workspace=workspace,
    )
    token = current_tool_context.set(
        ToolContext(
            run_mode="safe",
            guest_safe=True,
            workspace_dir=str(workspace),
            sandbox_policy=SandboxPolicy(),
            sandbox_gateway_config=SimpleNamespace(state_dir=str(tmp_path / "state")),
        )
    )
    try:
        file_payload = filesystem._sandbox_path_access_envelope(
            outside,
            write=True,
            approval_id="forged-or-stale-approval",
        )
        file_gate_payload, elevated = await filesystem._gate_out_of_workspace_write(
            "write_file",
            outside,
            str(outside),
            "forged-or-stale-approval",
            sandbox_permissions="require_escalated",
            justification="try to cross the guest boundary",
        )
        payload = shell._sandbox_write_path_access_envelope(
            OperationProfile(
                name="guest-outside-write",
                requested_write_paths=(str(outside),),
            ),
            str(workspace),
            f"write {outside}",
            approval_id="forged-or-stale-approval",
        )
        shell_escalation_payload = json.loads(
            await shell.exec_command(
                "Write-Output guest",
                workdir=str(workspace),
                sandbox_permissions="require_escalated",
                justification="try to leave the sandbox",
                approval_id="forged-or-stale-approval",
            )
        )
    finally:
        current_tool_context.reset(token)
        reset_runtime()

    assert payload is not None
    assert payload["status"] == "blocked"
    assert payload["reason"] == "GUEST_WRITE_OUTSIDE_DEFAULT_WORKSPACE"
    assert "approval_id" not in payload
    assert file_payload is not None
    assert file_payload["status"] == "blocked"
    assert file_payload["reason"] == "GUEST_WRITE_OUTSIDE_DEFAULT_WORKSPACE"
    assert "approval_id" not in file_payload
    assert file_gate_payload is not None
    assert file_gate_payload["status"] == "blocked"
    assert file_gate_payload["reason"] == "GUEST_WRITE_OUTSIDE_DEFAULT_WORKSPACE"
    assert elevated is False
    assert shell_escalation_payload["status"] == "blocked"
    assert shell_escalation_payload["reason"] == "GUEST_HOST_EXECUTION_DENIED"
    assert "approval_id" not in shell_escalation_payload


@pytest.mark.asyncio
async def test_recursive_delete_requires_warning_before_mutation(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(shell, "_PENDING_RECURSIVE_DELETES", {})
    target = tmp_path / "workspace" / "discard"
    target.mkdir(parents=True)
    (target / "data.txt").write_text("keep a backup", encoding="utf-8")
    context = ToolContext(
        run_mode="safe",
        workspace_dir=str(tmp_path / "workspace"),
        session_key="test",
        sandbox_policy=SandboxPolicy(),
        sandbox_gateway_config=SimpleNamespace(state_dir=str(tmp_path / "state")),
    )
    reviewed_actions = []

    def _capture_action(action, **_kwargs):
        reviewed_actions.append(action)
        return _PendingGate()

    monkeypatch.setattr(shell, "gate_elevated_action", _capture_action)
    token = current_tool_context.set(context)
    try:
        result = await shell._gate_recursive_delete(
            f"Remove-Item -Recurse -Force '{target}'",
            cwd=str(tmp_path / "workspace"),
            approval_id=None,
        )
    finally:
        current_tool_context.reset(token)

    payload = json.loads(result or "{}")
    assert payload["status"] == "approval_required"
    assert payload["recursive"] is True
    assert payload["irreversible"] is False
    assert payload["backup_state"] == "enabled"
    assert "无法撤回" in payload["warning"]
    assert reviewed_actions[0].display.kind == "delete"
    assert reviewed_actions[0].display.target == str(target)
    assert reviewed_actions[0].display.backup_state == "enabled"
    assert target.exists()


@pytest.mark.asyncio
async def test_approved_recursive_delete_is_backed_up_then_removed(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(shell, "_PENDING_RECURSIVE_DELETES", {})
    target = tmp_path / "workspace" / "discard"
    target.mkdir(parents=True)
    (target / "data.txt").write_text("keep a backup", encoding="utf-8")
    context = ToolContext(
        run_mode="safe",
        workspace_dir=str(tmp_path / "workspace"),
        session_key="test",
        sandbox_policy=SandboxPolicy(),
        sandbox_gateway_config=SimpleNamespace(state_dir=str(tmp_path / "state")),
    )
    monkeypatch.setattr(shell, "gate_elevated_action", lambda *_a, **_k: _AllowedGate())
    offloaded: list[str] = []

    async def inline_to_thread(function, *args, **kwargs):
        offloaded.append(function.__name__)
        return function(*args, **kwargs)

    monkeypatch.setattr(shell.asyncio, "to_thread", inline_to_thread)
    token = current_tool_context.set(context)
    try:
        result = await shell._gate_recursive_delete(
            f"Remove-Item -Recurse -Force '{target}'",
            cwd=str(tmp_path / "workspace"),
            approval_id=None,
        )
    finally:
        current_tool_context.reset(token)

    payload = json.loads(result or "{}")
    assert payload["status"] == "deleted"
    assert payload["backup"]["sizeBytes"] > 0
    assert not target.exists()
    assert (tmp_path / "state" / "backup-vault" / "entries").is_dir()
    assert offloaded == ["plan_delete", "execute"]


@pytest.mark.asyncio
async def test_recursive_delete_cannot_target_sandbox_authority_path(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(shell, "_PENDING_RECURSIVE_DELETES", {})
    state = tmp_path / "state"
    target = state / "protected"
    target.mkdir(parents=True)
    context = ToolContext(
        run_mode="safe",
        workspace_dir=str(tmp_path / "workspace"),
        session_key="test",
        sandbox_policy=SandboxPolicy(),
        sandbox_gateway_config=SimpleNamespace(state_dir=str(state)),
    )
    token = current_tool_context.set(context)
    try:
        result = await shell._gate_recursive_delete(
            f"Remove-Item -Recurse -Force '{target}'",
            cwd=str(tmp_path),
            approval_id=None,
        )
    finally:
        current_tool_context.reset(token)

    payload = json.loads(result or "{}")
    assert payload["status"] == "blocked"
    assert payload["reason"] == "sandbox_authority_read_denied"
    assert target.exists()


@pytest.mark.asyncio
async def test_recursive_delete_detects_target_change_after_approval(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(shell, "_PENDING_RECURSIVE_DELETES", {})
    target = tmp_path / "workspace" / "discard"
    target.mkdir(parents=True)
    data = target / "data.txt"
    data.write_text("before", encoding="utf-8")
    context = ToolContext(
        run_mode="safe",
        workspace_dir=str(tmp_path / "workspace"),
        session_key="test",
        sandbox_policy=SandboxPolicy(),
        sandbox_gateway_config=SimpleNamespace(state_dir=str(tmp_path / "state")),
    )
    monkeypatch.setattr(shell, "gate_elevated_action", lambda *_a, **_k: _PendingGate())
    token = current_tool_context.set(context)
    try:
        first = await shell._gate_recursive_delete(
            f"Remove-Item -Recurse -Force '{target}'",
            cwd=str(tmp_path / "workspace"),
            approval_id=None,
        )
        assert json.loads(first or "{}")["approval_id"] == "pending"
        data.write_text("changed", encoding="utf-8")
        monkeypatch.setattr(shell, "gate_elevated_action", lambda *_a, **_k: _AllowedGate())
        result = await shell._gate_recursive_delete(
            f"Remove-Item -Recurse -Force '{target}'",
            cwd=str(tmp_path / "workspace"),
            approval_id="pending",
        )
    finally:
        current_tool_context.reset(token)

    payload = json.loads(result or "{}")
    assert payload["status"] == "blocked"
    assert payload["reason"] == "recursive_delete_target_changed"
    assert target.exists()


def test_pending_recursive_delete_cache_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr(shell, "_PENDING_RECURSIVE_DELETES", {})

    for index in range(300):
        shell._remember_pending_recursive_delete(str(index), object())

    assert len(shell._PENDING_RECURSIVE_DELETES) == 256
    assert "0" not in shell._PENDING_RECURSIVE_DELETES
    assert "299" in shell._PENDING_RECURSIVE_DELETES
