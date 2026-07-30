from __future__ import annotations

from pathlib import Path, PureWindowsPath

from opensquilla.sandbox.file_policy import (
    builtin_deny_write_paths,
    compile_safe_file_profile,
    decide_file_access,
)
from opensquilla.sandbox.permissions import FileSystemAccess
from opensquilla.sandbox.policy_models import SandboxPolicy


def test_windows_builtin_deny_write_contains_requested_credentials() -> None:
    env = {
        "USERPROFILE": r"C:\Users\alice",
        "APPDATA": r"C:\Users\alice\AppData\Roaming",
        "LOCALAPPDATA": r"C:\Users\alice\AppData\Local",
    }

    roots = builtin_deny_write_paths("win32", env=env)

    assert PureWindowsPath(r"C:\Users\alice\.ssh") in roots
    assert PureWindowsPath(r"C:\Users\alice\.aws") in roots
    assert PureWindowsPath(r"C:\Users\alice\.kube\config") in roots
    assert PureWindowsPath(r"C:\Users\alice\.docker\config.json") in roots
    assert PureWindowsPath(r"C:\Users\alice\.config\gh\hosts.yml") in roots
    assert PureWindowsPath(r"C:\Users\alice\.terraform.d\credentials.tfrc.json") in roots


def test_safe_ordinary_read_and_write_are_automatic(tmp_path: Path) -> None:
    policy = SandboxPolicy()
    target = tmp_path / "ordinary.txt"

    read = decide_file_access("read", target, policy, platform="linux")
    write = decide_file_access("write", target, policy, platform="linux")

    assert read.allowed is True and read.approval_required is False
    assert write.allowed is True and write.approval_required is False


def test_custom_deny_write_requires_approval_but_read_stays_allowed(
    tmp_path: Path,
) -> None:
    protected = tmp_path / "protected"
    target = protected / "nested" / "credential.txt"
    policy = SandboxPolicy.model_validate(
        {"files": {"customDenyWritePaths": [f"{protected}/**"]}}
    )

    read = decide_file_access("read", target, policy, platform="linux")
    write = decide_file_access("write", target, policy, platform="linux")

    assert read.allowed is True
    assert write.allowed is False
    assert write.approval_required is True
    assert write.code == "sensitive_file_mutation_requires_approval"
    assert write.rule_source == "custom"


def test_builtin_rules_cannot_be_removed_by_empty_custom_policy(tmp_path: Path) -> None:
    home = tmp_path / "home"
    target = home / ".ssh" / "config"

    decision = decide_file_access(
        "delete",
        target,
        SandboxPolicy(),
        platform="linux",
        home=home,
    )

    assert decision.approval_required is True
    assert decision.rule_source == "builtin"


def test_safe_profile_compiles_write_baseline_and_read_only_carveouts(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    authority = tmp_path / "state"
    custom = tmp_path / "custom-secret"
    profile = compile_safe_file_profile(
        SandboxPolicy.model_validate(
            {"files": {"customDenyWritePaths": [f"{custom}/**"]}}
        ),
        authority_roots=(authority,),
        platform="linux",
        home=home,
        env={"HOME": str(home)},
    )

    assert profile.resolve(tmp_path / "ordinary.txt") is FileSystemAccess.WRITE
    assert profile.resolve(home / ".ssh" / "config") is FileSystemAccess.READ
    assert profile.resolve(custom / "credential") is FileSystemAccess.READ
    assert profile.resolve(authority / "sessions.db") is FileSystemAccess.DENY
