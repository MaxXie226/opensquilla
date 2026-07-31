"""Deny-write-only Safe file policy with non-overridable authority roots."""

from __future__ import annotations

import fnmatch
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Literal

from opensquilla.sandbox.policy_models import SandboxPolicy

if TYPE_CHECKING:
    from opensquilla.sandbox.permissions import FileSystemPermissionProfile

FileOperation = Literal[
    "read",
    "write",
    "delete",
    "move",
    "rename",
    "link",
    "chmod",
]

_WINDOWS_PATTERNS = (
    r"%USERPROFILE%\.ssh\**",
    r"%USERPROFILE%\.aws\**",
    r"%USERPROFILE%\.kube\config",
    r"%USERPROFILE%\.docker\config.json",
    r"%USERPROFILE%\.docker\daemon.json",
    r"%USERPROFILE%\.netrc",
    r"%USERPROFILE%\.npmrc",
    r"%USERPROFILE%\.pypirc",
    r"%USERPROFILE%\.gem\credentials",
    r"%USERPROFILE%\.config\gh\hosts.yml",
    r"%USERPROFILE%\.git-credentials",
    r"%USERPROFILE%\.config\gcloud\**",
    r"%USERPROFILE%\.azure\**",
    r"%USERPROFILE%\.terraform.d\credentials.tfrc.json",
    r"%APPDATA%\Microsoft\Protect\**",
    r"%APPDATA%\Microsoft\Credentials\**",
    r"%LOCALAPPDATA%\Microsoft\Credentials\**",
)

_POSIX_HOME_PATTERNS = (
    "$HOME/.ssh/**",
    "$HOME/.aws/**",
    "$HOME/.kube/config",
    "$HOME/.docker/config.json",
    "$HOME/.netrc",
    "$HOME/.npmrc",
    "$HOME/.pypirc",
    "$HOME/.gem/credentials",
    "$HOME/.config/gh/hosts.yml",
    "$HOME/.git-credentials",
    "$HOME/.config/gcloud/**",
    "$HOME/.azure/**",
    "$HOME/.terraform.d/credentials.tfrc.json",
    "$HOME/.gnupg/**",
    "$HOME/.password-store/**",
)

_MACOS_PATTERNS = (
    *_POSIX_HOME_PATTERNS,
    "$HOME/Library/Keychains/**",
    "/Library/Keychains/**",
    "/etc/docker/daemon.json",
    "/etc/sudoers",
    "/etc/sudoers.d/**",
    "/etc/ssh/**",
    "/etc/pam.d/**",
    "/Library/LaunchDaemons/**",
)

_LINUX_PATTERNS = (
    *_POSIX_HOME_PATTERNS,
    "$HOME/.local/share/keyrings/**",
    "$HOME/.config/containers/auth.json",
    "/etc/docker/daemon.json",
    "/etc/shadow",
    "/etc/gshadow",
    "/etc/sudoers",
    "/etc/sudoers.d/**",
    "/etc/ssh/**",
    "/etc/pam.d/**",
    "/root/**",
)

_WINDOWS_ENV_RE = re.compile(r"%([^%]+)%")


@dataclass(frozen=True)
class FileDecision:
    allowed: bool
    approval_required: bool
    code: str | None = None
    matched_path: PurePath | None = None
    rule_source: Literal["authority", "builtin", "custom"] | None = None


def _platform_name(platform: str | None) -> Literal["windows", "macos", "linux"]:
    value = str(platform or sys.platform).lower()
    if value.startswith("win"):
        return "windows"
    if value in {"darwin", "macos"}:
        return "macos"
    return "linux"


def _expand_pattern(
    pattern: str,
    *,
    platform: Literal["windows", "macos", "linux"],
    env: Mapping[str, str],
    home: PurePath,
) -> str:
    raw = str(pattern).strip()
    if platform == "windows":
        raw = _WINDOWS_ENV_RE.sub(
            lambda match: str(env.get(match.group(1), match.group(0))),
            raw,
        )
        if raw.startswith("~"):
            raw = str(home) + raw[1:]
        return raw
    raw = raw.replace("$HOME", str(home))
    if raw.startswith("~"):
        raw = str(home) + raw[1:]
    return raw


def _patterns_for_platform(
    platform: Literal["windows", "macos", "linux"],
) -> tuple[str, ...]:
    if platform == "windows":
        return _WINDOWS_PATTERNS
    if platform == "macos":
        return _MACOS_PATTERNS
    return _LINUX_PATTERNS


def _pure_path(
    value: str | os.PathLike[str],
    *,
    platform: Literal["windows", "macos", "linux"],
) -> PurePath:
    return PureWindowsPath(value) if platform == "windows" else PurePosixPath(value)


def _pattern_root(pattern: str) -> str:
    normalized = pattern.replace("\\", "/")
    if normalized.endswith("/**"):
        return normalized[:-3].rstrip("/")
    return normalized


def builtin_deny_write_paths(
    platform: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    home: str | PurePath | None = None,
) -> tuple[PurePath, ...]:
    target_platform = _platform_name(platform)
    environment = dict(os.environ if env is None else env)
    if home is None:
        if target_platform == "windows":
            home = environment.get("USERPROFILE") or str(Path.home())
        else:
            home = environment.get("HOME") or str(Path.home())
    pure_home = _pure_path(str(home), platform=target_platform)
    result: list[PurePath] = []
    for raw in _patterns_for_platform(target_platform):
        expanded = _expand_pattern(
            raw,
            platform=target_platform,
            env=environment,
            home=pure_home,
        )
        path = _pure_path(_pattern_root(expanded), platform=target_platform)
        if path not in result:
            result.append(path)
    return tuple(result)


def authority_roots_for_state(state_dir: str | Path) -> tuple[Path, ...]:
    state = Path(state_dir).expanduser().resolve(strict=False)
    return (
        state,
        state / "backup-vault",
        state / "upgrade-snapshots",
        state / "sandbox-grants",
    )


def _normalized_text(
    path: str | os.PathLike[str] | PurePath,
    *,
    platform: Literal["windows", "macos", "linux"],
) -> str:
    if platform == "windows":
        return PureWindowsPath(str(path)).as_posix().rstrip("/").casefold()
    try:
        return Path(path).expanduser().resolve(strict=False).as_posix().rstrip("/")
    except (OSError, RuntimeError):
        return PurePosixPath(str(path)).as_posix().rstrip("/")


def _matches_pattern(
    candidate: str,
    pattern: str,
    *,
    platform: Literal["windows", "macos", "linux"],
) -> bool:
    normalized = pattern.replace("\\", "/").rstrip("/")
    if platform == "windows":
        normalized = normalized.casefold()
    if normalized.endswith("/**"):
        root = normalized[:-3].rstrip("/")
        return candidate == root or candidate.startswith(f"{root}/")
    if any(character in normalized for character in "*?["):
        return fnmatch.fnmatchcase(candidate, normalized)
    return candidate == normalized


def _matched_rule(
    path: str | os.PathLike[str] | PurePath,
    patterns: Sequence[str],
    *,
    platform: Literal["windows", "macos", "linux"],
    env: Mapping[str, str],
    home: PurePath,
) -> PurePath | None:
    candidate = _normalized_text(path, platform=platform)
    for pattern in patterns:
        expanded = _expand_pattern(
            pattern,
            platform=platform,
            env=env,
            home=home,
        )
        normalized_pattern = _normalized_text(
            _pattern_root(expanded),
            platform=platform,
        )
        match_pattern = (
            f"{normalized_pattern}/**"
            if expanded.replace("\\", "/").endswith("/**")
            else normalized_pattern
        )
        if _matches_pattern(candidate, match_pattern, platform=platform):
            return _pure_path(_pattern_root(expanded), platform=platform)
    return None


def decide_file_access(
    operation: FileOperation | str,
    path: str | os.PathLike[str] | PurePath,
    policy: SandboxPolicy,
    *,
    authority_roots: Sequence[str | os.PathLike[str] | PurePath] = (),
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
    home: str | PurePath | None = None,
) -> FileDecision:
    target_platform = _platform_name(platform)
    environment = dict(os.environ if env is None else env)
    if home is None:
        home = (
            environment.get("USERPROFILE")
            if target_platform == "windows"
            else environment.get("HOME")
        ) or str(Path.home())
    pure_home = _pure_path(str(home), platform=target_platform)
    candidate = _normalized_text(path, platform=target_platform)

    for root in authority_roots:
        normalized_root = _normalized_text(root, platform=target_platform)
        if candidate == normalized_root or candidate.startswith(f"{normalized_root}/"):
            return FileDecision(
                allowed=False,
                approval_required=False,
                code="sandbox_authority_read_denied",
                matched_path=_pure_path(str(root), platform=target_platform),
                rule_source="authority",
            )

    if str(operation).lower() == "read":
        return FileDecision(allowed=True, approval_required=False)

    builtin = _matched_rule(
        path,
        _patterns_for_platform(target_platform),
        platform=target_platform,
        env=environment,
        home=pure_home,
    )
    if builtin is not None:
        return FileDecision(
            allowed=False,
            approval_required=True,
            code="sensitive_file_mutation_requires_approval",
            matched_path=builtin,
            rule_source="builtin",
        )
    custom = _matched_rule(
        path,
        policy.files.custom_deny_write_paths,
        platform=target_platform,
        env=environment,
        home=pure_home,
    )
    if custom is not None:
        return FileDecision(
            allowed=False,
            approval_required=True,
            code="sensitive_file_mutation_requires_approval",
            matched_path=custom,
            rule_source="custom",
        )
    return FileDecision(allowed=True, approval_required=False)


def compile_safe_file_profile(
    policy: SandboxPolicy,
    *,
    authority_roots: Sequence[str | os.PathLike[str] | PurePath] = (),
    writable_roots: Sequence[str | os.PathLike[str] | PurePath] = (),
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
    home: str | PurePath | None = None,
) -> FileSystemPermissionProfile:
    """Compile Safe's host-write baseline with read-only deny-write carveouts."""

    from opensquilla.sandbox.permissions import (
        FileSystemAccess,
        FileSystemPermissionEntry,
        FileSystemPermissionProfile,
    )

    target_platform = _platform_name(platform)
    environment = dict(os.environ if env is None else env)
    if home is None:
        home = (
            environment.get("USERPROFILE")
            if target_platform == "windows"
            else environment.get("HOME")
        ) or str(Path.home())
    pure_home = _pure_path(str(home), platform=target_platform)
    entries: list[FileSystemPermissionEntry] = []
    if target_platform == "windows":
        # The native Windows backend grants capability SIDs to concrete ACL
        # roots.  An implicit full-disk WRITE baseline cannot be projected
        # without granting a filesystem root, so express the ordinary desktop
        # write surface as the user's home plus the active workspace/mounts.
        # More-specific READ/DENY entries below still carve out credentials and
        # OpenSquilla's own authority state.
        default_access = FileSystemAccess.READ
        entries.extend(
            FileSystemPermissionEntry(root, FileSystemAccess.WRITE)
            for root in dict.fromkeys(
                (
                    pure_home,
                    *(
                        _pure_path(str(root), platform=target_platform)
                        for root in writable_roots
                    ),
                )
            )
        )
    else:
        default_access = FileSystemAccess.WRITE
        entries.append(
            FileSystemPermissionEntry(PurePosixPath("/"), FileSystemAccess.WRITE)
        )

    deny_write_roots = list(
        builtin_deny_write_paths(
            target_platform,
            env=environment,
            home=pure_home,
        )
    )
    for raw in policy.files.custom_deny_write_paths:
        expanded = _expand_pattern(
            raw,
            platform=target_platform,
            env=environment,
            home=pure_home,
        )
        deny_write_roots.append(
            _pure_path(_pattern_root(expanded), platform=target_platform)
        )
    entries.extend(
        FileSystemPermissionEntry(root, FileSystemAccess.READ)
        for root in dict.fromkeys(deny_write_roots)
    )
    entries.extend(
        FileSystemPermissionEntry(
            _pure_path(str(root), platform=target_platform),
            FileSystemAccess.DENY,
        )
        for root in authority_roots
    )
    return FileSystemPermissionProfile(
        entries=tuple(entries),
        default_access=default_access,
    )


__all__ = [
    "FileDecision",
    "authority_roots_for_state",
    "builtin_deny_write_paths",
    "compile_safe_file_profile",
    "decide_file_access",
]
