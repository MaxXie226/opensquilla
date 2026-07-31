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
from opensquilla.sandbox.path_validation import decide_path_access
from opensquilla.sandbox.permissions import FileSystemAccess
from opensquilla.sandbox.policy_models import FilePolicySettings, SandboxPolicy
from opensquilla.tools.builtin import filesystem, shell
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
    assert any(
        entry.access is FileSystemAccess.WRITE
        and entry.path == tmp_path.resolve(strict=False)
        for entry in profile.entries
    )
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
    protected = workspace / "protected"
    protected.mkdir(parents=True)
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
    assert protected_write.status != "allowed"
    assert ordinary_write.status == "allowed"
    assert outside_write.status != "allowed"
    assert sensitive_read.status == "blocked"
    assert ordinary_read.status == "allowed"


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
    monkeypatch.setattr(shell, "gate_elevated_action", lambda *_a, **_k: _PendingGate())
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
    assert payload["irreversible"] is True
    assert "无法撤回" in payload["warning"]
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
