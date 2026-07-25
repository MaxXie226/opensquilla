"""One-time consolidation of Desktop recovery profiles into the primary home."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import math
import os
import re
import shutil
import stat
import tomllib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from opensquilla.recovery.atomic import (
    native_move_no_replace,
    profile_no_follow_manifest,
)
from opensquilla.recovery.errors import (
    DestinationExistsError,
    RecoveryError,
    UnsafePathError,
)
from opensquilla.recovery.locking import (
    acquire_legacy_gateway_locks,
    acquire_profile_locks,
    move_profile_no_replace,
)
from opensquilla.recovery.session_merge import (
    SessionMergeResult,
    merge_session_database,
    snapshot_session_database,
)
from opensquilla.session.keys import normalize_agent_id

ConsolidationOutcome = Literal["noop", "consolidated", "blocked"]
CredentialAdoptionStatus = Literal["pending", "complete", "not_required"]

_RECOVERY_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_CONFIG_NAMES = ("config.toml", ".env")
_CONTEXT_NAME = "desktop-profile-context.json"
_CREDENTIAL_NAME = "desktop-credential.json"
_JOURNAL_NAME = ".opensquilla-profile-consolidation.json"
_BACKUPS_RELATIVE = Path("backups") / "profile-consolidation"
_STAGING_PREFIX = ".opensquilla.profile-consolidation."
_MAX_TIMESTAMP_NS = (1 << 63) - 1
_EXCLUDED_AUTHORITY_NAMES = frozenset(
    {
        ".opensquilla-imported.json",
        ".opensquilla-layout-v2.json",
        ".opensquilla-layout-v3.json",
        ".opensquilla-migration-pending.json",
        "desktop-layout-v2.json",
        "desktop-layout-v3.json",
        "desktop-migration-pending.json",
        "desktop-recovery-v1.json",
        "migration-pending.json",
    }
)
_DOTENV_PROFILE_SCOPED_KEYS = frozenset(
    {
        "OPENSQUILLA_ATTACHMENTS_MEDIA_ROOT",
        "OPENSQUILLA_CODETASK_AGENT_PYTHON",
        "OPENSQUILLA_CODETASK_RUNS_DIR",
        "OPENSQUILLA_CODETASK_SCRATCH_DIR",
        "OPENSQUILLA_CODETASK_WORKSPACE_DIR",
        "OPENSQUILLA_DESKTOP_GATEWAY_OWNERSHIP_DIR",
        "OPENSQUILLA_DESKTOP_NODE_BIN_DIR",
        "OPENSQUILLA_DESKTOP_PROFILE_KIND",
        "OPENSQUILLA_GATEWAY_ATTACHMENTS__MEDIA_ROOT",
        "OPENSQUILLA_GATEWAY_CONFIG_PATH",
        "OPENSQUILLA_GATEWAY_STATE_DIR",
        "OPENSQUILLA_GATEWAY_WORKSPACE_DIR",
        "OPENSQUILLA_HOME",
        "OPENSQUILLA_HOME_NAMES",
        "OPENSQUILLA_LLM_TRACE_PATH",
        "OPENSQUILLA_LOG_DIR",
        "OPENSQUILLA_MEDIA_DIR",
        "OPENSQUILLA_MEMORY_DB",
        "OPENSQUILLA_MEMORY_DIR",
        "OPENSQUILLA_META_RUNS_DB",
        "OPENSQUILLA_MIGRATIONS_DIR",
        "OPENSQUILLA_NODE_BIN_DIR",
        "OPENSQUILLA_PATCH_EVIDENCE_LEDGER_PATH",
        "OPENSQUILLA_PROFILE",
        "OPENSQUILLA_PROFILE_IN_USE",
        "OPENSQUILLA_PROFILE_KIND",
        "OPENSQUILLA_ROOT",
        "OPENSQUILLA_ROUTER_DECISIONS_DB",
        "OPENSQUILLA_RUNTIME_EVENTS_PATH",
        "OPENSQUILLA_SANDBOX_EXEC_WRAPPER",
        "OPENSQUILLA_SANDBOX_PROXY_UDS",
        "OPENSQUILLA_SCHEDULER_DB",
        "OPENSQUILLA_SESSION_ARCHIVE_DIR",
        "OPENSQUILLA_STATE",
        "OPENSQUILLA_STATE_DIR",
        "OPENSQUILLA_SWEBENCH_ARTIFACTS_DIR",
        "OPENSQUILLA_SWEBENCH_CONFIG_DIR",
        "OPENSQUILLA_SWEBENCH_ENV_PATH",
        "OPENSQUILLA_SWEBENCH_HARNESS_PYTHON",
        "OPENSQUILLA_SWEBENCH_PYTHON_BIN",
        "OPENSQUILLA_SWEBENCH_PYTHON_HOME",
        "OPENSQUILLA_TMUX_SOCKET_DIR",
        "OPENSQUILLA_TURN_CALL_LOG_DIR",
        "OPENSQUILLA_USER_STATE_DIR",
        "OPENSQUILLA_WINDOWS_APPCONTAINER_SITE_DIR",
        "OPENSQUILLA_WINDOWS_SANDBOX_EXPANSION_ROOTS",
        "OPENSQUILLA_WORKSPACE_DIR",
    }
)
_DOTENV_DATA_ROUTE_KEYS = frozenset(
    {
        "OPENSQUILLA_ATTACHMENTS_MEDIA_ROOT",
        "OPENSQUILLA_GATEWAY_ATTACHMENTS__MEDIA_ROOT",
        "OPENSQUILLA_GATEWAY_STATE_DIR",
        "OPENSQUILLA_GATEWAY_WORKSPACE_DIR",
        "OPENSQUILLA_WORKSPACE_DIR",
    }
)


@dataclass(frozen=True)
class ConsolidationResult:
    schema_version: int
    outcome: ConsolidationOutcome
    stable_code: str
    primary_home: Path
    configuration_source_recovery_id: str | None
    configuration_source_credential_path: Path | None
    configuration_source_credential_sha256: str | None
    configuration_source_credential_size: int | None
    consumed_recovery_ids: tuple[str, ...]
    backup_path: Path | None
    receipt_path: Path | None
    credential_adoption_status: CredentialAdoptionStatus
    revision: int
    errors: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "outcome": self.outcome,
            "stable_code": self.stable_code,
            "primary_home": str(self.primary_home),
            "configuration_source_recovery_id": self.configuration_source_recovery_id,
            "configuration_source_credential_path": (
                str(self.configuration_source_credential_path)
                if self.configuration_source_credential_path is not None
                else None
            ),
            "configuration_source_credential_sha256": (self.configuration_source_credential_sha256),
            "configuration_source_credential_size": (self.configuration_source_credential_size),
            "consumed_recovery_ids": list(self.consumed_recovery_ids),
            "backup_path": str(self.backup_path) if self.backup_path is not None else None,
            "receipt_path": str(self.receipt_path) if self.receipt_path is not None else None,
            "credential_adoption_status": self.credential_adoption_status,
            "revision": self.revision,
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class _RecoveryProfile:
    recovery_id: str
    root: Path
    home: Path
    modified_at_ns: int

    @property
    def credential(self) -> Path:
        return self.root / _CREDENTIAL_NAME

    @property
    def dotenv(self) -> Path:
        current = self.home / ".env"
        return current if _lexists(current) else self.home / "state" / ".env"


@dataclass(frozen=True)
class _DataRoute:
    role: str
    path: Path
    origin: Literal["canonical", "derived", "explicit"]
    profile_relative: Path | None

    @property
    def external(self) -> bool:
        return self.profile_relative is None

    def destination(self, staging: Path) -> Path:
        return self.path if self.profile_relative is None else staging / self.profile_relative

    def as_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "path": str(self.path),
            "origin": self.origin,
            "profile_relative": (
                self.profile_relative.as_posix() if self.profile_relative is not None else None
            ),
        }


@dataclass(frozen=True)
class _PrimaryDataRoutes:
    workspace: _DataRoute
    state: _DataRoute
    media: _DataRoute
    agent_workspaces: tuple[tuple[str, _DataRoute], ...]
    external_bindings: tuple[dict[str, object], ...]

    def agent_workspace(self, agent_id: str) -> _DataRoute:
        normalized = normalize_agent_id(agent_id)
        if normalized == "main":
            return self.workspace
        configured = dict(self.agent_workspaces).get(normalized)
        if configured is not None:
            return configured
        path = self.workspace.path / "agents" / normalized
        return _DataRoute(
            role=f"agent:{normalized}",
            path=path,
            origin="derived",
            profile_relative=(
                self.workspace.profile_relative / "agents" / normalized
                if self.workspace.profile_relative is not None
                else None
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "workspace": self.workspace.as_dict(),
            "state": self.state.as_dict(),
            "media": self.media.as_dict(),
            "agent_workspaces": {
                agent_id: route.as_dict() for agent_id, route in self.agent_workspaces
            },
            "external_bindings": list(self.external_bindings),
        }


class _ConsolidationBlockedError(RecoveryError):
    pass


def _absolute(path: str | Path) -> Path:
    return Path(path).expanduser().absolute()


def _blocked(
    primary_home: Path,
    stable_code: str,
    error: BaseException | str,
    *,
    revision: int = 0,
) -> ConsolidationResult:
    return ConsolidationResult(
        schema_version=1,
        outcome="blocked",
        stable_code=stable_code,
        primary_home=primary_home,
        configuration_source_recovery_id=None,
        configuration_source_credential_path=None,
        configuration_source_credential_sha256=None,
        configuration_source_credential_size=None,
        consumed_recovery_ids=(),
        backup_path=None,
        receipt_path=None,
        credential_adoption_status="not_required",
        revision=revision,
        errors=(str(error),),
    )


def _is_link_or_reparse(value: os.stat_result) -> bool:
    return stat.S_ISLNK(value.st_mode) or bool(int(getattr(value, "st_file_attributes", 0)) & 0x400)


def _plain_directory(path: Path, *, label: str) -> os.stat_result:
    try:
        value = path.lstat()
    except OSError as exc:
        raise _ConsolidationBlockedError(
            f"{label} is unavailable: {path}",
            stable_code="profile_consolidation_unsafe_recovery_root",
        ) from exc
    if _is_link_or_reparse(value) or not stat.S_ISDIR(value.st_mode):
        raise _ConsolidationBlockedError(
            f"{label} must be a real directory: {path}",
            stable_code="profile_consolidation_unsafe_recovery_root",
        )
    return value


def _plain_optional_file(path: Path, *, label: str) -> bool:
    try:
        value = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise UnsafePathError(f"{label} cannot be inspected: {path}") from exc
    if _is_link_or_reparse(value) or not stat.S_ISREG(value.st_mode):
        raise UnsafePathError(f"{label} must be a regular file: {path}")
    return True


def _validate_base_paths(user_data: Path, primary_home: Path) -> None:
    _plain_directory(user_data, label="Electron userData")
    expected = user_data / "opensquilla"
    if os.path.normcase(os.path.normpath(str(primary_home))) != os.path.normcase(
        os.path.normpath(str(expected))
    ):
        raise UnsafePathError("primary home must be the canonical userData/opensquilla path")
    try:
        primary_stat = primary_home.lstat()
    except FileNotFoundError:
        pass
    else:
        if _is_link_or_reparse(primary_stat) or not stat.S_ISDIR(primary_stat.st_mode):
            raise UnsafePathError("primary home must be a real directory")
        profile_no_follow_manifest(primary_home)
    _plain_optional_file(user_data / _CONTEXT_NAME, label="Desktop profile context")


def _enumerate_recoveries(user_data: Path) -> tuple[_RecoveryProfile, ...]:
    container = user_data / "recovery-profiles"
    try:
        container.lstat()
    except FileNotFoundError:
        return ()
    _plain_directory(container, label="recovery profile container")
    profiles: list[_RecoveryProfile] = []
    with os.scandir(container) as entries:
        for entry in sorted(entries, key=lambda item: item.name):
            if not _RECOVERY_ID_RE.fullmatch(entry.name):
                raise _ConsolidationBlockedError(
                    f"unexpected entry in recovery profile container: {entry.name}",
                    stable_code="profile_consolidation_unsafe_recovery_root",
                )
            # Match Desktop's exact v4 UUID contract, not merely UUID-shaped names.
            if uuid.UUID(entry.name).version != 4:
                raise _ConsolidationBlockedError(
                    f"unsupported recovery profile id: {entry.name}",
                    stable_code="profile_consolidation_unsafe_recovery_root",
                )
            root = Path(entry.path)
            root_stat = _plain_directory(root, label="recovery profile")
            home = root / "opensquilla"
            try:
                home.lstat()
            except FileNotFoundError:
                pass
            else:
                _plain_directory(home, label="recovery home")
                profile_no_follow_manifest(home)
            for name in (*_CONFIG_NAMES,):
                if home.exists():
                    _plain_optional_file(home / name, label=f"recovery {name}")
            if home.exists():
                _plain_optional_file(
                    home / "state" / ".env",
                    label="recovery legacy .env",
                )
            _plain_optional_file(root / _CREDENTIAL_NAME, label="recovery credential")
            profiles.append(
                _RecoveryProfile(
                    recovery_id=entry.name,
                    root=root,
                    home=home,
                    modified_at_ns=int(root_stat.st_mtime_ns),
                )
            )
    return tuple(profiles)


def _configuration_source(
    user_data: Path,
    primary_home: Path,
    profiles: tuple[_RecoveryProfile, ...],
) -> _RecoveryProfile | None:
    primary_config_path = primary_home / "config.toml"
    primary_config = _plain_optional_file(
        primary_config_path,
        label="primary config.toml",
    )
    primary_dotenv_path = primary_home / ".env"
    primary_dotenv = _plain_optional_file(
        primary_dotenv_path,
        label="primary .env",
    )
    primary_legacy_dotenv_path = primary_home / "state" / ".env"
    primary_legacy_dotenv = _plain_optional_file(
        primary_legacy_dotenv_path,
        label="primary legacy .env",
    )
    primary_credential = _plain_optional_file(
        user_data / _CREDENTIAL_NAME,
        label="primary credential",
    )
    if (
        (primary_config and _primary_config_has_user_configuration(primary_config_path))
        or (primary_dotenv and _dotenv_has_user_configuration(primary_dotenv_path))
        or (primary_legacy_dotenv and _dotenv_has_user_configuration(primary_legacy_dotenv_path))
        or primary_credential
    ):
        return None

    candidates: list[_RecoveryProfile] = []
    for profile in profiles:
        (
            bundle_valid,
            config_bytes,
            dotenv_raw,
            credential_present,
            credential_valid,
        ) = _read_recovery_configuration_bundle(profile)
        if not bundle_valid:
            continue
        if (
            config_bytes is not None
            or (dotenv_raw is not None and _dotenv_text_has_user_configuration(dotenv_raw))
            or (credential_present and credential_valid)
        ):
            candidates.append(profile)
    return max(
        candidates,
        key=lambda profile: (*_configuration_recency(profile), profile.recovery_id),
        default=None,
    )


def _credential_updated_ns(path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return 0
    if not isinstance(payload, dict):
        return 0
    value = payload.get("updatedAt", payload.get("updated_at"))
    if isinstance(value, int | float) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            return 0
        try:
            numeric = int(value)
        except (OverflowError, ValueError):
            return 0
        if numeric <= 0:
            return 0
        if numeric < 10_000_000_000:
            numeric *= 1_000_000_000
        elif numeric < 10_000_000_000_000:
            numeric *= 1_000_000
        return numeric if numeric <= _MAX_TIMESTAMP_NS else 0
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            numeric = int(parsed.timestamp() * 1_000_000_000)
            return numeric if 0 < numeric <= _MAX_TIMESTAMP_NS else 0
        except (OSError, OverflowError, ValueError):
            return 0
    return 0


def _configuration_recency(profile: _RecoveryProfile) -> tuple[int, int]:
    modified = [profile.modified_at_ns]
    for path in (
        profile.home / "config.toml",
        profile.credential,
        profile.dotenv,
        profile.home / "state" / "sessions.db",
    ):
        try:
            modified.append(int(path.lstat().st_mtime_ns))
        except OSError:
            continue
    filesystem_recency = max(modified)
    effective_recency = max(
        filesystem_recency,
        _credential_updated_ns(profile.credential),
    )
    return effective_recency, filesystem_recency


def _revision(profiles: tuple[_RecoveryProfile, ...]) -> int:
    digest = hashlib.sha256()
    for profile in profiles:
        value = profile.root.lstat()
        digest.update(profile.recovery_id.encode("ascii"))
        digest.update(
            f":{value.st_dev}:{value.st_ino}:{value.st_size}:{value.st_mtime_ns}".encode()
        )
    # Electron transports this through a JavaScript Number.
    return int.from_bytes(digest.digest()[:6], "big")


def _metadata_identity(path: Path) -> tuple[int, int, int, int, int] | None:
    try:
        value = path.lstat()
    except FileNotFoundError:
        return None
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_mode),
        int(value.st_size),
        int(value.st_mtime_ns),
    )


def _source_snapshot(
    profile: _RecoveryProfile,
) -> tuple[object | None, tuple[int, int, int, int, int], object | None]:
    home_manifest = profile_no_follow_manifest(profile.home) if profile.home.is_dir() else None
    root_identity = _metadata_identity(profile.root)
    if root_identity is None:
        raise UnsafePathError("recovery profile disappeared during consolidation")
    return home_manifest, root_identity, _metadata_identity(profile.credential)


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _normalized_path(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path.expanduser().absolute())))


def _profile_relative_path(path: Path, primary_home: Path) -> Path | None:
    path_value = _normalized_path(path)
    root_value = _normalized_path(primary_home)
    try:
        if os.path.commonpath((path_value, root_value)) != root_value:
            return None
    except ValueError:
        return None
    relative = os.path.relpath(
        str(path.expanduser().absolute()),
        str(primary_home.expanduser().absolute()),
    )
    if relative in {"", os.curdir}:
        raise UnsafePathError("a primary data root cannot be the profile home itself")
    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        return None
    return Path(relative)


def _route(
    role: str,
    path: Path,
    origin: Literal["canonical", "derived", "explicit"],
    *,
    primary_home: Path,
) -> _DataRoute:
    absolute = path.expanduser().absolute()
    return _DataRoute(
        role=role,
        path=absolute,
        origin=origin,
        profile_relative=_profile_relative_path(absolute, primary_home),
    )


def _configured_profile_path(raw: object, *, name: str, primary_home: Path) -> Path | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise UnsafePathError(f"{name} must be a non-empty path string")
    path = Path(raw.strip()).expanduser()
    if not path.is_absolute():
        path = primary_home / path
    return path.absolute()


def _configured_absolute_path(raw: object, *, name: str) -> Path | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise UnsafePathError(f"{name} must be a non-empty path string")
    path = Path(raw.strip()).expanduser()
    if not path.is_absolute():
        raise UnsafePathError(f"{name} must be absolute so consolidation matches runtime routing")
    return path.absolute()


def _directory_chain_binding(path: Path, *, label: str) -> dict[str, object]:
    candidate = path.expanduser().absolute()
    current = Path(candidate.anchor) if candidate.anchor else Path()
    parts = candidate.parts[1:] if candidate.anchor else candidate.parts
    components: list[dict[str, object]] = []
    missing_suffix: list[str] = []
    if candidate.anchor:
        try:
            anchor_stat = current.lstat()
        except OSError as exc:
            raise UnsafePathError(f"{label} anchor is unavailable: {current}") from exc
        if _is_link_or_reparse(anchor_stat) or not stat.S_ISDIR(anchor_stat.st_mode):
            raise UnsafePathError(f"{label} anchor must be a real directory: {current}")
        components.append(
            {
                "path": str(current),
                "device": int(anchor_stat.st_dev),
                "inode": int(anchor_stat.st_ino),
                "mode": stat.S_IFMT(anchor_stat.st_mode),
                "file_attributes": int(getattr(anchor_stat, "st_file_attributes", 0)),
            }
        )
    for index, part in enumerate(parts):
        current /= part
        try:
            value = current.lstat()
        except FileNotFoundError:
            missing_suffix = list(parts[index:])
            break
        except OSError as exc:
            raise UnsafePathError(f"{label} is unavailable: {current}") from exc
        if _is_link_or_reparse(value) or not stat.S_ISDIR(value.st_mode):
            raise UnsafePathError(f"{label} must be a real directory: {current}")
        components.append(
            {
                "path": str(current),
                "device": int(value.st_dev),
                "inode": int(value.st_ino),
                "mode": stat.S_IFMT(value.st_mode),
                "file_attributes": int(getattr(value, "st_file_attributes", 0)),
            }
        )
    existing_path = Path(str(components[-1]["path"])) if components else Path(candidate.anchor)
    try:
        resolved = existing_path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise UnsafePathError(f"{label} cannot be resolved safely: {candidate}") from exc
    return {
        "path": str(candidate),
        "normalized_path": _normalized_path(candidate),
        "existing_path": str(existing_path),
        "resolved_existing_path": os.path.normcase(os.path.normpath(str(resolved))),
        "components": components,
        "missing_suffix": missing_suffix,
    }


def _validate_directory_chain_binding(binding: object) -> None:
    if not isinstance(binding, dict):
        raise UnsafePathError("external directory binding is invalid")
    path_value = binding.get("path")
    components = binding.get("components")
    missing_suffix = binding.get("missing_suffix")
    if (
        not isinstance(path_value, str)
        or not isinstance(components, list)
        or not isinstance(missing_suffix, list)
        or not all(isinstance(item, str) and item for item in missing_suffix)
    ):
        raise UnsafePathError("external directory binding is invalid")
    for expected in components:
        if not isinstance(expected, dict) or not isinstance(
            expected.get("path"),
            str,
        ):
            raise UnsafePathError("external directory binding is invalid")
        current = Path(str(expected["path"]))
        try:
            value = current.lstat()
        except OSError as exc:
            raise UnsafePathError(f"external directory ancestor changed: {current}") from exc
        observed = {
            "path": str(current),
            "device": int(value.st_dev),
            "inode": int(value.st_ino),
            "mode": stat.S_IFMT(value.st_mode),
            "file_attributes": int(getattr(value, "st_file_attributes", 0)),
        }
        if _is_link_or_reparse(value) or not stat.S_ISDIR(value.st_mode) or observed != expected:
            raise UnsafePathError(f"external directory ancestor identity changed: {current}")
    existing_path = Path(str(binding.get("existing_path")))
    try:
        resolved = os.path.normcase(os.path.normpath(str(existing_path.resolve(strict=True))))
    except (OSError, RuntimeError) as exc:
        raise UnsafePathError(
            f"external directory ancestor cannot be resolved: {existing_path}"
        ) from exc
    if resolved != binding.get("resolved_existing_path"):
        raise UnsafePathError(f"external directory ancestor target changed: {existing_path}")
    current = existing_path
    for part in missing_suffix:
        current /= str(part)
        try:
            value = current.lstat()
        except FileNotFoundError:
            continue
        if _is_link_or_reparse(value) or not stat.S_ISDIR(value.st_mode):
            raise UnsafePathError(f"external directory suffix is unsafe: {current}")
    if _normalized_path(Path(path_value)) != binding.get("normalized_path"):
        raise UnsafePathError("external directory binding path changed")


def _paths_alias_or_nest(first: Path, second: Path) -> bool:
    first_value = _normalized_path(first)
    second_value = _normalized_path(second)
    try:
        if os.path.commonpath((first_value, second_value)) in {
            first_value,
            second_value,
        }:
            return True
    except ValueError:
        return False
    try:
        resolved_first = os.path.normcase(os.path.normpath(str(first.resolve(strict=True))))
        resolved_second = os.path.normcase(os.path.normpath(str(second.resolve(strict=True))))
        return os.path.commonpath((resolved_first, resolved_second)) in {
            resolved_first,
            resolved_second,
        }
    except (OSError, RuntimeError, ValueError):
        return False


def _safe_workspace_state_nesting(
    first: _DataRoute,
    second: _DataRoute,
) -> bool:
    """Allow only the two role-owned, direct-child workspace/state layouts.

    Desktop and gateway configurations commonly put both roots below one
    external application directory.  A direct ``workspace`` child of state is
    safe because state consolidation writes only its reserved state leaves.  A
    direct ``state`` child of workspace is safe only together with the
    workspace merge guard below, which diverts the recovery workspace's
    top-level ``state`` tree to ``recovered-data``.
    """

    routes = {first.role: first, second.role: second}
    if set(routes) != {"workspace", "state"}:
        return False
    workspace = routes["workspace"].path
    state = routes["state"].path
    return _normalized_path(workspace) == _normalized_path(state / "workspace") or _normalized_path(
        state
    ) == _normalized_path(workspace / "state")


def _safe_derived_data_overlap(
    first: _DataRoute,
    second: _DataRoute,
    *,
    workspace: _DataRoute,
    state: _DataRoute,
) -> bool:
    """Recognize only topology created by the runtime's own derivation rules."""

    for derived, other in ((first, second), (second, first)):
        if derived.origin != "derived":
            continue
        if derived.role.startswith("agent:"):
            if other.role == "workspace":
                return True
            if other.role == "state" and _safe_workspace_state_nesting(workspace, state):
                return True
        if derived.role == "media":
            if other.role == "state":
                return True
            if other.role == "workspace" and _safe_workspace_state_nesting(workspace, state):
                return True
    return False


def _file_authority(path: Path, *, label: str) -> dict[str, object]:
    from opensquilla.recovery.config_patch import ConfigSnapshot

    if not label:
        raise ValueError("file authority label must be non-empty")
    snapshot = ConfigSnapshot.capture(path)
    if snapshot.identity is None:
        return {"exists": False}
    identity = snapshot.identity
    return {
        "exists": True,
        "identity": list(identity.metadata_tuple()),
        "size": identity.size,
        "sha256": snapshot.digest.hex(),
    }


def _primary_config_authority(configuration_home: Path) -> dict[str, object]:
    dotenv = configuration_home / ".env"
    if not _lexists(dotenv):
        dotenv = configuration_home / "state" / ".env"
    return {
        "config": _file_authority(
            configuration_home / "config.toml",
            label="primary config.toml",
        ),
        "dotenv_path": (str(dotenv.relative_to(configuration_home)) if _lexists(dotenv) else None),
        "dotenv": _file_authority(dotenv, label="primary .env"),
    }


def _source_snapshot_token(profile: _RecoveryProfile) -> str:
    home_manifest, root_identity, credential_identity = _source_snapshot(profile)
    credential_authority = (
        _file_authority(profile.credential, label="recovery credential")
        if credential_identity is not None
        else None
    )
    authority_identity = (
        credential_authority.get("identity") if credential_authority is not None else None
    )
    if credential_authority is not None and (
        not bool(credential_authority.get("exists"))
        or not isinstance(authority_identity, list)
        or tuple(authority_identity[:5]) != credential_identity
    ):
        raise _ConsolidationBlockedError(
            "recovery credential changed while it was inspected",
            stable_code="profile_consolidation_source_changed",
        )
    encoded_manifest: object = None
    if isinstance(home_manifest, dict):
        encoded_manifest = {
            key: list(value.metadata_tuple()) for key, value in sorted(home_manifest.items())
        }
    payload = {
        "home": encoded_manifest,
        "root": list(root_identity),
        "credential": (list(credential_identity) if credential_identity is not None else None),
        "credential_sha256": (
            credential_authority["sha256"] if credential_authority is not None else None
        ),
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", "surrogatepass")
    ).hexdigest()


def _profile_content_token(root: Path) -> str:
    manifest = profile_no_follow_manifest(root)
    material: list[object] = []
    for relative, identity in sorted(manifest.items()):
        if Path(relative).name in {"gateway.pid", "gateway.pid.lock"}:
            continue
        entry: dict[str, object] = {
            "path": relative,
            "type": stat.S_IFMT(identity.mode),
            "mode": stat.S_IMODE(identity.mode),
            "link_target": identity.link_target,
        }
        if stat.S_ISREG(identity.mode):
            entry["sha256"] = _file_digest(root if relative == "." else root / relative)
        material.append(entry)
    return hashlib.sha256(
        json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", "surrogatepass")
    ).hexdigest()


def _read_primary_config_payload(configuration_home: Path) -> dict[str, Any]:
    path = configuration_home / "config.toml"
    if not _lexists(path):
        return {}
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        # Malformed primary bytes remain authoritative, but they cannot safely
        # nominate external roots. Preserve the prior canonical behavior.
        return {}
    if not isinstance(payload, dict):
        raise UnsafePathError("primary config.toml root must be a table")
    return payload


def _build_primary_data_routes(
    *,
    user_data: Path,
    primary_home: Path,
    profiles: tuple[_RecoveryProfile, ...],
    use_primary_configuration: bool,
    configuration_home: Path | None = None,
) -> _PrimaryDataRoutes:
    route_configuration_home = configuration_home or primary_home
    payload = (
        _read_primary_config_payload(route_configuration_home) if use_primary_configuration else {}
    )
    workspace_env: tuple[str, str] | None = None
    state_env: tuple[str, str] | None = None
    media_env: tuple[str, str] | None = None
    from opensquilla.recovery.config_patch import (
        media_override,
        state_override,
        workspace_override,
    )

    # Configuration-source selection and data routing are independent. A
    # path-only primary dotenv may not provide model/provider settings, but its
    # effective data roots remain authoritative while a recovery contributes
    # the missing user configuration.
    override_home = route_configuration_home
    workspace_env = workspace_override(
        override_home,
        include_legacy_dotenv=True,
        include_process_environment=True,
    )
    state_env = state_override(
        override_home,
        include_legacy_dotenv=True,
        include_process_environment=True,
    )
    media_env = media_override(
        override_home,
        include_legacy_dotenv=True,
        include_process_environment=True,
    )
    configured_workspace = _configured_profile_path(
        workspace_env[1] if workspace_env is not None else payload.get("workspace_dir"),
        name="workspace_dir",
        primary_home=primary_home,
    )
    configured_state = _configured_profile_path(
        state_env[1] if state_env is not None else payload.get("state_dir"),
        name="state_dir",
        primary_home=primary_home,
    )
    workspace_path = configured_workspace or (primary_home / "workspace")
    state_path = configured_state or (primary_home / "state")
    workspace = _route(
        "workspace",
        workspace_path,
        "explicit" if configured_workspace is not None else "canonical",
        primary_home=primary_home,
    )
    state = _route(
        "state",
        state_path,
        "explicit" if configured_state is not None else "canonical",
        primary_home=primary_home,
    )

    attachments = payload.get("attachments")
    raw_media = attachments.get("media_root") if isinstance(attachments, dict) else None
    if raw_media is None and media_env is not None:
        raw_media = media_env[1]
    configured_media = _configured_absolute_path(
        raw_media,
        name="attachments.media_root",
    )
    if configured_state is None:
        derived_media = primary_home / "media"
    elif state_path.name == "state":
        derived_media = state_path.parent / "media"
    else:
        derived_media = state_path / "media"
    media_path = configured_media or derived_media
    media_origin: Literal["canonical", "derived", "explicit"]
    if configured_media is None:
        media_origin = (
            "canonical"
            if _normalized_path(media_path) == _normalized_path(primary_home / "media")
            else "derived"
        )
    elif _normalized_path(configured_media) == _normalized_path(derived_media):
        media_origin = "derived"
    else:
        media_origin = "explicit"
    media = _route(
        "media",
        media_path,
        media_origin,
        primary_home=primary_home,
    )

    agent_routes: dict[str, _DataRoute] = {}
    raw_agents = payload.get("agents")
    entries: list[tuple[str, dict[str, Any]]] = []
    if isinstance(raw_agents, list):
        entries = [
            (str(entry.get("id", "")), entry) for entry in raw_agents if isinstance(entry, dict)
        ]
    elif isinstance(raw_agents, dict):
        entries = [
            (str(agent_id), entry)
            for agent_id, entry in raw_agents.items()
            if isinstance(entry, dict)
        ]
    for raw_agent_id, entry in entries:
        if not bool(entry.get("enabled", True)):
            continue
        normalized_agent_id = normalize_agent_id(raw_agent_id)
        if normalized_agent_id == "main":
            continue
        derived_path = (
            workspace_path
            if normalized_agent_id == "main"
            else workspace_path / "agents" / normalized_agent_id
        )
        if entry.get("workspace") is None:
            configured_path = derived_path
            origin: Literal["derived", "explicit"] = "derived"
        else:
            configured_path = _configured_absolute_path(
                entry.get("workspace"),
                name=f"agents.{normalized_agent_id}.workspace",
            )
            assert configured_path is not None
            origin = (
                "derived"
                if _normalized_path(configured_path) == _normalized_path(derived_path)
                else "explicit"
            )
        route = _route(
            f"agent:{normalized_agent_id}",
            configured_path,
            origin,
            primary_home=primary_home,
        )
        previous = agent_routes.get(normalized_agent_id)
        if previous is not None and previous.as_dict() != route.as_dict():
            raise UnsafePathError(f"multiple agent entries normalize to {normalized_agent_id!r}")
        agent_routes[normalized_agent_id] = route

    # A recovery workspace can contain an agent that is not yet declared in the
    # primary config. It still has an effective derived runtime route and must
    # participate in overlap validation before any external destination is
    # written.
    for profile in profiles:
        source_agents = profile.home / "workspace" / "agents"
        if not _lexists(source_agents):
            continue
        source_agents_stat = source_agents.lstat()
        if _is_link_or_reparse(source_agents_stat) or not stat.S_ISDIR(source_agents_stat.st_mode):
            continue
        with os.scandir(source_agents) as source_entries:
            for source_entry in source_entries:
                normalized_agent_id = normalize_agent_id(source_entry.name)
                if normalized_agent_id == "main" or normalized_agent_id in agent_routes:
                    continue
                agent_routes[normalized_agent_id] = _route(
                    f"agent:{normalized_agent_id}",
                    workspace_path / "agents" / normalized_agent_id,
                    "derived",
                    primary_home=primary_home,
                )

    independent = [
        route
        for route in (workspace, state, media, *agent_routes.values())
        if route.origin == "explicit"
    ]
    if _paths_alias_or_nest(workspace.path, state.path) and not _safe_workspace_state_nesting(
        workspace, state
    ):
        raise UnsafePathError(
            f"primary workspace/state roots overlap: workspace={workspace.path}, state={state.path}"
        )
    effective_routes = (workspace, state, media, *agent_routes.values())
    for index, first in enumerate(effective_routes):
        for second in effective_routes[index + 1 :]:
            if not _paths_alias_or_nest(first.path, second.path):
                continue
            if _safe_workspace_state_nesting(first, second) or _safe_derived_data_overlap(
                first,
                second,
                workspace=workspace,
                state=state,
            ):
                continue
            raise UnsafePathError(
                f"independent primary data roots overlap: "
                f"{first.role}={first.path}, {second.role}={second.path}"
            )

    protected = (
        user_data,
        user_data / "recovery-profiles",
        *(profile.home for profile in profiles),
    )
    for route in independent:
        if route.external and any(
            _paths_alias_or_nest(route.path, protected_path) for protected_path in protected
        ):
            raise UnsafePathError(
                f"external primary data root overlaps recovery authority: {route.role}={route.path}"
            )

    bindings: dict[str, dict[str, object]] = {}

    def bind(path: Path, label: str) -> None:
        binding = _directory_chain_binding(path, label=label)
        bindings[str(binding["normalized_path"])] = binding

    if workspace.external:
        bind(workspace.path, "external workspace root")
    if state.external:
        bind(state.path, "external state root")
    if media.external:
        if media.origin == "derived":
            media_parent = state.path.parent if state.path.name == "state" else state.path
            bind(media_parent, "external derived media parent")
        else:
            bind(media.path, "external media root")
    for agent_id, route in agent_routes.items():
        if route.external and route.origin == "explicit":
            bind(route.path, f"external agent workspace {agent_id}")

    return _PrimaryDataRoutes(
        workspace=workspace,
        state=state,
        media=media,
        agent_workspaces=tuple(sorted(agent_routes.items())),
        external_bindings=tuple(bindings[key] for key in sorted(bindings)),
    )


def _same_leaf(source: Path, destination: Path) -> bool:
    source_stat = source.lstat()
    destination_stat = destination.lstat()
    if _is_link_or_reparse(source_stat) or _is_link_or_reparse(destination_stat):
        if not (_is_link_or_reparse(source_stat) and _is_link_or_reparse(destination_stat)):
            return False
        return os.readlink(source) == os.readlink(destination)
    if stat.S_ISREG(source_stat.st_mode) and stat.S_ISREG(destination_stat.st_mode):
        return source_stat.st_size == destination_stat.st_size and (
            _file_digest(source) == _file_digest(destination)
        )
    return stat.S_ISDIR(source_stat.st_mode) and stat.S_ISDIR(destination_stat.st_mode)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_plain_directory(path: Path) -> None:
    candidate = path.expanduser().absolute()
    current = Path(candidate.anchor) if candidate.anchor else Path()
    parts = candidate.parts[1:] if candidate.anchor else candidate.parts
    for part in parts:
        current /= part
        try:
            value = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            value = current.lstat()
            _fsync_directory(current.parent)
        if _is_link_or_reparse(value) or not stat.S_ISDIR(value.st_mode):
            raise UnsafePathError(f"external merge directory must be real: {current}")


def _transaction_temporary(destination: Path, transaction_id: str) -> Path:
    token = hashlib.sha256(
        f"{transaction_id}\0{destination}".encode("utf-8", "surrogatepass")
    ).hexdigest()[:16]
    return destination.with_name(f".{destination.name}.opensquilla-consolidation-{token}.tmp")


def _atomic_copy_regular(
    source: Path,
    destination: Path,
    *,
    transaction_id: str,
) -> None:
    _ensure_plain_directory(destination.parent)
    temporary = _transaction_temporary(destination, transaction_id)
    if _lexists(temporary):
        temporary_value = temporary.lstat()
        if _is_link_or_reparse(temporary_value) or not stat.S_ISREG(temporary_value.st_mode):
            raise UnsafePathError(f"external merge temporary is unsafe: {temporary}")
        temporary.unlink()
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        stat.S_IMODE(source.lstat().st_mode) or 0o600,
    )
    try:
        with (
            source.open("rb") as source_handle,
            os.fdopen(
                descriptor,
                "wb",
                closefd=False,
            ) as destination_handle,
        ):
            shutil.copyfileobj(source_handle, destination_handle)
            destination_handle.flush()
            os.fsync(destination_handle.fileno())
    finally:
        os.close(descriptor)
    try:
        native_move_no_replace(temporary, destination)
        _fsync_directory(destination.parent)
    except DestinationExistsError:
        if not _same_leaf(source, destination):
            raise
    finally:
        temporary.unlink(missing_ok=True)


def _copy_leaf(
    source: Path,
    destination: Path,
    *,
    durable: bool = False,
    transaction_id: str = "",
) -> None:
    value = source.lstat()
    if durable:
        _ensure_plain_directory(destination.parent)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
    if _is_link_or_reparse(value):
        target_is_directory = bool(int(getattr(value, "st_file_attributes", 0)) & 0x10)
        try:
            os.symlink(
                os.readlink(source),
                destination,
                target_is_directory=target_is_directory,
            )
        except FileExistsError:
            if not _same_leaf(source, destination):
                raise
        if durable:
            _fsync_directory(destination.parent)
    elif stat.S_ISDIR(value.st_mode):
        if durable:
            _ensure_plain_directory(destination)
        else:
            shutil.copytree(source, destination, symlinks=True)
    elif stat.S_ISREG(value.st_mode):
        if durable:
            _atomic_copy_regular(
                source,
                destination,
                transaction_id=transaction_id,
            )
        else:
            shutil.copy2(source, destination, follow_symlinks=False)
    else:
        raise UnsafePathError(f"unsupported recovery workspace entry: {source}")


def _memory_blocks(value: str) -> list[str]:
    return [block.strip() for block in re.split(r"(?:\r?\n){2,}", value) if block.strip()]


def _merge_memory(
    source: Path,
    destination: Path,
    *,
    durable: bool = False,
    transaction_id: str = "",
) -> None:
    before = _metadata_identity(destination)
    current = destination.read_text(encoding="utf-8")
    incoming = source.read_text(encoding="utf-8")
    blocks = _memory_blocks(current)
    known = {block.replace("\r\n", "\n") for block in blocks}
    for block in _memory_blocks(incoming):
        normalized = block.replace("\r\n", "\n")
        if normalized not in known:
            blocks.append(block)
            known.add(normalized)
    merged = "\n\n".join(blocks).rstrip() + "\n"
    if merged == current:
        return
    if not durable:
        destination.write_text(merged, encoding="utf-8")
        return
    if before is None or _metadata_identity(destination) != before:
        raise UnsafePathError(
            f"external MEMORY.md changed while it was being merged: {destination}"
        )
    temporary = _transaction_temporary(destination, transaction_id)
    if _lexists(temporary):
        value = temporary.lstat()
        if _is_link_or_reparse(value) or not stat.S_ISREG(value.st_mode):
            raise UnsafePathError(f"external merge temporary is unsafe: {temporary}")
        temporary.unlink()
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(
            descriptor,
            "w",
            encoding="utf-8",
            closefd=False,
        ) as handle:
            handle.write(merged)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    try:
        if _metadata_identity(destination) != before:
            raise UnsafePathError(f"external MEMORY.md changed before publish: {destination}")
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _preserve_conflict(
    source: Path,
    staging: Path,
    recovery_id: str,
    relative: Path,
    *,
    scope: str,
) -> None:
    destination = staging / "recovered-data" / recovery_id / scope / relative

    def complete_copy(source_path: Path, destination_path: Path) -> None:
        if not _lexists(destination_path):
            _copy_leaf(source_path, destination_path)
            return
        source_stat = source_path.lstat()
        destination_stat = destination_path.lstat()
        if (
            not _is_link_or_reparse(source_stat)
            and not _is_link_or_reparse(destination_stat)
            and stat.S_ISDIR(source_stat.st_mode)
            and stat.S_ISDIR(destination_stat.st_mode)
        ):
            with os.scandir(source_path) as entries:
                for entry in sorted(entries, key=lambda item: item.name):
                    complete_copy(
                        Path(entry.path),
                        destination_path / entry.name,
                    )
            return
        if _same_leaf(source_path, destination_path):
            return
        raise UnsafePathError(f"recovery conflict destination already differs: {destination_path}")

    complete_copy(source, destination)


def _merge_workspace_tree(
    source: Path,
    destination: Path,
    *,
    staging: Path,
    recovery_id: str,
    relative: Path = Path(),
    scope: str = "workspace",
    merge_memory: bool = True,
    durable: bool = False,
    transaction_id: str = "",
) -> None:
    workspace_runtime_state = (
        scope == "workspace"
        and len(relative.parts) >= 2
        and relative.parts[:2] in {("state", "matrix"), ("state", "msteams")}
    )
    if workspace_runtime_state:
        return
    source_stat = source.lstat()
    if _is_link_or_reparse(source_stat):
        if not _lexists(destination):
            _copy_leaf(
                source,
                destination,
                durable=durable,
                transaction_id=transaction_id,
            )
        elif not _same_leaf(source, destination):
            _preserve_conflict(
                source,
                staging,
                recovery_id,
                relative,
                scope=scope,
            )
        return
    force_state_walk = (
        scope == "workspace" and relative.parts == ("state",) and stat.S_ISDIR(source_stat.st_mode)
    )
    if not _lexists(destination) and not force_state_walk:
        if not (durable and stat.S_ISDIR(source_stat.st_mode)):
            _copy_leaf(
                source,
                destination,
                durable=durable,
                transaction_id=transaction_id,
            )
            return
        _ensure_plain_directory(destination)
    if force_state_walk and not _lexists(destination):
        if durable:
            _ensure_plain_directory(destination)
        else:
            destination.mkdir(parents=True)
            shutil.copymode(source, destination, follow_symlinks=False)
    destination_stat = destination.lstat()
    if _is_link_or_reparse(destination_stat):
        if not _same_leaf(source, destination):
            _preserve_conflict(
                source,
                staging,
                recovery_id,
                relative,
                scope=scope,
            )
        return
    if stat.S_ISDIR(source_stat.st_mode) and stat.S_ISDIR(destination_stat.st_mode):
        with os.scandir(source) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                child_relative = relative / entry.name
                _merge_workspace_tree(
                    Path(entry.path),
                    destination / entry.name,
                    staging=staging,
                    recovery_id=recovery_id,
                    relative=child_relative,
                    scope=scope,
                    merge_memory=merge_memory,
                    durable=durable,
                    transaction_id=transaction_id,
                )
        return
    if _same_leaf(source, destination):
        return
    if (
        merge_memory
        and relative.name == "MEMORY.md"
        and stat.S_ISREG(source_stat.st_mode)
        and stat.S_ISREG(destination_stat.st_mode)
    ):
        _merge_memory(
            source,
            destination,
            durable=durable,
            transaction_id=transaction_id,
        )
        return
    _preserve_conflict(
        source,
        staging,
        recovery_id,
        relative,
        scope=scope,
    )


def _clone_primary(primary_home: Path, staging: Path) -> None:
    if primary_home.exists():
        before = profile_no_follow_manifest(primary_home)
        shutil.copytree(primary_home, staging, symlinks=True)
        after = profile_no_follow_manifest(primary_home)
        if before != after:
            raise UnsafePathError("primary profile changed while staging consolidation")
        profile_no_follow_manifest(staging)
        source_db = primary_home / "state" / "sessions.db"
        if source_db.is_file():
            snapshot_session_database(source_db, staging / "state" / "sessions.db")
            for suffix in ("-wal", "-shm", "-journal"):
                (staging / "state" / f"sessions.db{suffix}").unlink(missing_ok=True)
    else:
        staging.mkdir(mode=0o700)
    (staging / "state").mkdir(mode=0o700, exist_ok=True)
    (staging / "workspace").mkdir(mode=0o700, exist_ok=True)
    for runtime_lock in ("gateway.pid", "gateway.pid.lock"):
        (staging / "state" / runtime_lock).unlink(missing_ok=True)


def _transformed_config(raw: bytes, workspace_root: Path) -> bytes:
    original = tomllib.loads(raw.decode("utf-8"))
    transformed = copy.deepcopy(original)
    for key in ("state_dir", "workspace_dir", "media_dir"):
        transformed.pop(key, None)
    attachments = transformed.get("attachments")
    if isinstance(attachments, dict):
        attachments.pop("media_root", None)
    agents = transformed.get("agents")
    agent_entries: list[tuple[str, dict[str, Any]]] = []
    if isinstance(agents, list):
        for entry in agents:
            if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                agent_entries.append((str(entry["id"]), entry))
    elif isinstance(agents, dict):
        for agent_id, entry in agents.items():
            if isinstance(entry, dict):
                agent_entries.append((str(agent_id), entry))
    for agent_id, entry in agent_entries:
        if isinstance(entry.get("workspace"), str) and agent_id.strip():
            normalized_id = normalize_agent_id(agent_id)
            entry["workspace"] = str(
                workspace_root
                if normalized_id == "main"
                else workspace_root / "agents" / normalized_id
            )
    from opensquilla.migration.lossless_toml import patch_import_config

    return patch_import_config(raw, original, transformed)


def _read_recovery_config(path: Path) -> tuple[bool, bytes | None]:
    if not _lexists(path):
        return True, None
    try:
        raw = path.read_bytes()
        parsed = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return False, None
    return True, raw if parsed else None


def _primary_config_has_user_configuration(path: Path) -> bool:
    try:
        raw = path.read_bytes()
        return bool(tomllib.loads(raw.decode("utf-8")))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        # Malformed primary bytes may contain the user's only configuration.
        # Preserve their authority rather than silently replacing them.
        return True


def _dotenv_key(line: str) -> str | None:
    match = re.match(
        r"^\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=",
        line,
    )
    return match.group("key").upper() if match is not None else None


def _dotenv_text_has_user_configuration(raw: str) -> bool:
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key = _dotenv_key(line)
        if key is None or key not in _DOTENV_PROFILE_SCOPED_KEYS:
            return True
    return False


def _dotenv_has_user_configuration(path: Path) -> bool:
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # A non-empty, unparseable file may still contain credentials. Preserve
        # primary authority instead of silently replacing it.
        return path.stat().st_size > 0
    except OSError as exc:
        raise _ConsolidationBlockedError(
            f"primary .env cannot be read: {path}",
            stable_code="profile_consolidation_primary_config_unreadable",
        ) from exc
    return _dotenv_text_has_user_configuration(raw)


def _read_recovery_dotenv(
    profile: _RecoveryProfile,
) -> tuple[bool, str | None]:
    path = profile.dotenv
    if not _lexists(path):
        return True, None
    try:
        return True, path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False, None


def _read_recovery_credential(
    profile: _RecoveryProfile,
) -> tuple[bool, bool]:
    credential = profile.credential
    if not _lexists(credential):
        return True, False
    try:
        payload = json.loads(credential.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False, True
    if not isinstance(payload, dict):
        return False, True

    # Keep source selection aligned with the Desktop adoption parser.  A
    # credential can be intentionally sparse (legacy records are normalized
    # with provider defaults), but fields that are present must have the shape
    # Electron can safely normalize.  Otherwise a semantically invalid newest
    # credential-only profile would win source selection, be skipped later by
    # Electron, and hide an older usable configuration.
    string_fields = {
        "provider",
        "model",
        "baseUrl",
        "apiKeyEnv",
        "encryptedApiKey",
        "modelRoutingMode",
        "routerMode",
        "routerDefaultTier",
        "searchProvider",
        "searchApiKeyEnv",
        "encryptedSearchApiKey",
        "encryption",
        "configAuthority",
        "importTransactionId",
        "createdAt",
        "updatedAt",
    }
    if any(field in payload and not isinstance(payload[field], str) for field in string_fields):
        return False, True
    if any(
        field in payload and isinstance(payload[field], str) and not payload[field].strip()
        for field in ("provider", "model", "baseUrl")
    ):
        return False, True
    if "disableNetworkObservability" in payload and not isinstance(
        payload["disableNetworkObservability"], bool
    ):
        return False, True
    if "routerTiers" in payload and not isinstance(payload["routerTiers"], dict):
        return False, True

    authority = payload.get("configAuthority")
    transaction_id = payload.get("importTransactionId")
    valid_transaction = isinstance(transaction_id, str) and bool(
        _RECOVERY_ID_RE.fullmatch(transaction_id)
    )
    if (authority == "profile" and not valid_transaction) or (
        authority != "profile" and valid_transaction
    ):
        return False, True
    return True, True


def _read_recovery_configuration_bundle(
    profile: _RecoveryProfile,
) -> tuple[bool, bytes | None, str | None, bool, bool]:
    config_valid, config_bytes = _read_recovery_config(profile.home / "config.toml")
    dotenv_valid, dotenv_raw = _read_recovery_dotenv(profile)
    credential_valid, credential_present = _read_recovery_credential(profile)
    return (
        config_valid and dotenv_valid,
        config_bytes,
        dotenv_raw,
        credential_present,
        credential_valid,
    )


def _canonical_only_legacy_homes(
    profiles: tuple[_RecoveryProfile, ...],
) -> tuple[Path, ...]:
    return tuple(profile.home for profile in profiles if not _read_recovery_dotenv(profile)[0])


def _sanitized_dotenv(raw: str) -> str:
    lines = [
        line for line in raw.splitlines() if _dotenv_key(line) not in _DOTENV_PROFILE_SCOPED_KEYS
    ]
    return "\n".join(lines).rstrip() + ("\n" if lines else "")


def _data_route_dotenv(raw: str) -> str:
    lines = [line for line in raw.splitlines() if _dotenv_key(line) in _DOTENV_DATA_ROUTE_KEYS]
    return "\n".join(lines).rstrip() + ("\n" if lines else "")


def _apply_configuration_source(
    staging: Path,
    source: _RecoveryProfile | None,
    *,
    workspace_root: Path,
) -> None:
    if source is None:
        return
    primary_dotenv = staging / ".env"
    if not _lexists(primary_dotenv):
        primary_dotenv = staging / "state" / ".env"
    preserved_route_dotenv = ""
    preserved_route_mode_source: Path | None = None
    if _lexists(primary_dotenv):
        preserved_route_dotenv = _data_route_dotenv(primary_dotenv.read_text(encoding="utf-8"))
        if preserved_route_dotenv:
            preserved_route_mode_source = primary_dotenv
    for destination in (
        staging / "config.toml",
        staging / ".env",
        staging / "state" / ".env",
    ):
        destination.unlink(missing_ok=True)
    config = source.home / "config.toml"
    (
        bundle_valid,
        config_bytes,
        dotenv_raw,
        credential_present,
        credential_valid,
    ) = _read_recovery_configuration_bundle(source)
    if not bundle_valid or not (
        config_bytes is not None
        or (dotenv_raw is not None and _dotenv_text_has_user_configuration(dotenv_raw))
        or (credential_present and credential_valid)
    ):
        raise _ConsolidationBlockedError(
            "selected recovery configuration bundle changed or became unreadable",
            stable_code="profile_consolidation_source_changed",
        )
    if config_bytes is not None:
        destination = staging / "config.toml"
        destination.write_bytes(_transformed_config(config_bytes, workspace_root))
        shutil.copymode(config, destination, follow_symlinks=False)
    recovery_dotenv = (
        _sanitized_dotenv(dotenv_raw)
        if dotenv_raw is not None and _dotenv_text_has_user_configuration(dotenv_raw)
        else ""
    )
    merged_dotenv = preserved_route_dotenv + recovery_dotenv
    if merged_dotenv:
        dotenv = source.dotenv
        destination = staging / ".env"
        destination.write_text(
            merged_dotenv,
            encoding="utf-8",
        )
        mode_source = dotenv if recovery_dotenv else preserved_route_mode_source
        assert mode_source is not None
        shutil.copymode(mode_source, destination, follow_symlinks=False)


def _operational_state_path(relative: Path) -> bool:
    name = relative.name.lower()
    if name in {
        ".env",
        "approval_queue.json",
        "approvals.json",
        "gateway.pid",
        "gateway.pid.lock",
    }:
        return True
    if name.startswith("memory.db"):
        return True
    if name.endswith(
        (
            ".db",
            ".db-journal",
            ".db-shm",
            ".db-wal",
            ".sqlite",
            ".sqlite-journal",
            ".sqlite-shm",
            ".sqlite-wal",
        )
    ):
        return True
    return False


def _merge_filtered_tree(
    source: Path,
    destination: Path,
    *,
    staging: Path,
    recovery_id: str,
    relative: Path,
    scope: str,
    skip_operational_state: bool,
    durable: bool = False,
    transaction_id: str = "",
) -> None:
    if skip_operational_state and _operational_state_path(relative):
        return
    source_stat = source.lstat()
    if not _is_link_or_reparse(source_stat) and stat.S_ISDIR(source_stat.st_mode):
        if _lexists(destination):
            destination_stat = destination.lstat()
            if _is_link_or_reparse(destination_stat) or not stat.S_ISDIR(destination_stat.st_mode):
                destination = staging / "recovered-data" / recovery_id / scope / relative
                durable = False
        if durable:
            _ensure_plain_directory(destination)
        else:
            destination.mkdir(parents=True, exist_ok=True)
        with os.scandir(source) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                child_relative = relative / entry.name
                _merge_filtered_tree(
                    Path(entry.path),
                    destination / entry.name,
                    staging=staging,
                    recovery_id=recovery_id,
                    relative=child_relative,
                    scope=scope,
                    skip_operational_state=skip_operational_state,
                    durable=durable,
                    transaction_id=transaction_id,
                )
        return
    _merge_workspace_tree(
        source,
        destination,
        staging=staging,
        recovery_id=recovery_id,
        relative=relative,
        scope=scope,
        merge_memory=False,
        durable=durable,
        transaction_id=transaction_id,
    )


def _merge_profile_files(
    staging: Path,
    profile: _RecoveryProfile,
    *,
    state_destination: Path,
    state_external: bool,
    transaction_id: str,
) -> None:
    if not _lexists(profile.home):
        return
    excluded = {
        ".env",
        "code-task",
        "config.toml",
        "media",
        "migration",
        "profiles",
        "recovery",
        "recovery-profiles",
        "state",
        "workspace",
    } | _EXCLUDED_AUTHORITY_NAMES
    with os.scandir(profile.home) as entries:
        for entry in sorted(entries, key=lambda item: item.name):
            if entry.name in excluded:
                continue
            relative = Path(entry.name)
            _merge_filtered_tree(
                Path(entry.path),
                staging / relative,
                staging=staging,
                recovery_id=profile.recovery_id,
                relative=relative,
                scope="profile",
                skip_operational_state=False,
            )
    state = profile.home / "state"
    if (
        _lexists(state)
        and not _is_link_or_reparse(state.lstat())
        and stat.S_ISDIR(state.lstat().st_mode)
    ):
        session_archive = state / "session-archive"
        if _lexists(session_archive):
            _merge_filtered_tree(
                session_archive,
                state_destination / "session-archive",
                staging=staging,
                recovery_id=profile.recovery_id,
                relative=Path("state") / "session-archive",
                scope="profile",
                skip_operational_state=True,
                durable=state_external,
                transaction_id=transaction_id,
            )
        agents = state / "agents"
        if (
            _lexists(agents)
            and not _is_link_or_reparse(agents.lstat())
            and stat.S_ISDIR(agents.lstat().st_mode)
        ):
            with os.scandir(agents) as entries:
                for entry in sorted(entries, key=lambda item: item.name):
                    memory = Path(entry.path) / "memory"
                    if not _lexists(memory):
                        continue
                    relative = Path("state") / "agents" / entry.name / "memory"
                    _merge_filtered_tree(
                        memory,
                        state_destination / "agents" / entry.name / "memory",
                        staging=staging,
                        recovery_id=profile.recovery_id,
                        relative=relative,
                        scope="profile",
                        skip_operational_state=True,
                        durable=state_external,
                        transaction_id=transaction_id,
                    )


def _merge_profile_media(
    staging: Path,
    profile: _RecoveryProfile,
    session_result: SessionMergeResult | None,
    *,
    destination_media: Path,
    media_external: bool,
    transaction_id: str,
) -> None:
    source_media = profile.home / "media"
    if not _lexists(source_media):
        return
    source_media_stat = source_media.lstat()
    if _is_link_or_reparse(source_media_stat) or not stat.S_ISDIR(source_media_stat.st_mode):
        return
    with os.scandir(source_media) as entries:
        for entry in sorted(entries, key=lambda item: item.name):
            source = Path(entry.path)
            source_stat = source.lstat()
            if (
                entry.name != "transcripts"
                or _is_link_or_reparse(source_stat)
                or not stat.S_ISDIR(source_stat.st_mode)
            ):
                relative = Path("media") / entry.name
                _merge_filtered_tree(
                    source,
                    destination_media / entry.name,
                    staging=staging,
                    recovery_id=profile.recovery_id,
                    relative=relative,
                    scope="profile",
                    skip_operational_state=False,
                    durable=media_external,
                    transaction_id=transaction_id,
                )
                continue
            with os.scandir(source) as transcript_entries:
                for transcript_entry in sorted(
                    transcript_entries,
                    key=lambda item: item.name,
                ):
                    source_session_id = transcript_entry.name
                    target_session_id = (
                        session_result.remapped_session_ids.get(
                            source_session_id,
                            source_session_id,
                        )
                        if session_result is not None
                        else source_session_id
                    )
                    relative = Path("media") / "transcripts" / target_session_id
                    _merge_filtered_tree(
                        Path(transcript_entry.path),
                        destination_media / "transcripts" / target_session_id,
                        staging=staging,
                        recovery_id=profile.recovery_id,
                        relative=relative,
                        scope="profile",
                        skip_operational_state=False,
                        durable=media_external,
                        transaction_id=transaction_id,
                    )


def _preserve_excluded_profile_data(
    staging: Path,
    profile: _RecoveryProfile,
) -> None:
    state = profile.home / "state"
    if _lexists(state):
        _preserve_conflict(
            state,
            staging,
            profile.recovery_id,
            Path("state"),
            scope="profile",
        )
    workspace = profile.home / "workspace"
    for relative in (
        Path("state") / "matrix",
        Path("state") / "msteams",
    ):
        source = workspace / relative
        if _lexists(source):
            _preserve_conflict(
                source,
                staging,
                profile.recovery_id,
                relative,
                scope="workspace",
            )


def _merge_profile_workspace(
    staging: Path,
    profile: _RecoveryProfile,
    routes: _PrimaryDataRoutes,
    *,
    transaction_id: str,
) -> None:
    source_workspace = profile.home / "workspace"
    if not _lexists(source_workspace):
        return
    source_stat = source_workspace.lstat()
    if _is_link_or_reparse(source_stat) or not stat.S_ISDIR(source_stat.st_mode):
        raise UnsafePathError(
            f"recovery workspace root must be a real directory: {source_workspace}"
        )
    workspace_destination = routes.workspace.destination(staging)
    reserved_workspace_children = {
        route.path.name
        for route in (routes.state, routes.media)
        if _normalized_path(route.path) == _normalized_path(routes.workspace.path / route.path.name)
    }
    with os.scandir(source_workspace) as entries:
        for entry in sorted(entries, key=lambda item: item.name):
            source = Path(entry.path)
            if entry.name in reserved_workspace_children:
                _preserve_conflict(
                    source,
                    staging,
                    profile.recovery_id,
                    Path(entry.name),
                    scope="workspace",
                )
                continue
            if entry.name != "agents":
                _merge_workspace_tree(
                    source,
                    workspace_destination / entry.name,
                    staging=staging,
                    recovery_id=profile.recovery_id,
                    relative=Path(entry.name),
                    durable=routes.workspace.external,
                    transaction_id=transaction_id,
                )
                continue
            agents_stat = source.lstat()
            if _is_link_or_reparse(agents_stat) or not stat.S_ISDIR(agents_stat.st_mode):
                _preserve_conflict(
                    source,
                    staging,
                    profile.recovery_id,
                    Path("agents"),
                    scope="workspace",
                )
                continue
            with os.scandir(source) as agent_entries:
                for agent_entry in sorted(
                    agent_entries,
                    key=lambda item: item.name,
                ):
                    normalized = normalize_agent_id(agent_entry.name)
                    route = routes.agent_workspace(normalized)
                    _merge_workspace_tree(
                        Path(agent_entry.path),
                        route.destination(staging),
                        staging=staging,
                        recovery_id=profile.recovery_id,
                        relative=Path("agents") / normalized,
                        durable=route.external,
                        transaction_id=transaction_id,
                    )


def _merge_session_database_idempotent(
    target: Path,
    source: Path,
    *,
    source_id: str,
    transaction_id: str,
    external: bool,
) -> SessionMergeResult:
    if not external or _lexists(target):
        if _lexists(target):
            value = target.lstat()
            if _is_link_or_reparse(value) or not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
                raise UnsafePathError(f"sessions database target must be a regular file: {target}")
        return merge_session_database(target, source, source_id=source_id)

    _ensure_plain_directory(target.parent)
    temporary = _transaction_temporary(target, f"{transaction_id}-{source_id}")
    for stale in (
        temporary,
        temporary.with_name(f".{temporary.name}.snapshot.tmp"),
    ):
        if not _lexists(stale):
            continue
        value = stale.lstat()
        if _is_link_or_reparse(value) or not stat.S_ISREG(value.st_mode):
            raise UnsafePathError(f"sessions merge temporary is unsafe: {stale}")
        stale.unlink()
    result = merge_session_database(temporary, source, source_id=source_id)
    descriptor = os.open(temporary, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        native_move_no_replace(temporary, target)
        _fsync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return result


def _merge_recovery_data(
    staging: Path,
    profiles: tuple[_RecoveryProfile, ...],
    routes: _PrimaryDataRoutes,
    *,
    transaction_id: str,
) -> tuple[SessionMergeResult, ...]:
    session_results: list[SessionMergeResult] = []
    state_destination = routes.state.destination(staging)
    media_destination = routes.media.destination(staging)
    for profile in profiles:
        _preserve_excluded_profile_data(staging, profile)
        _merge_profile_files(
            staging,
            profile,
            state_destination=state_destination,
            state_external=routes.state.external,
            transaction_id=transaction_id,
        )
        _merge_profile_workspace(
            staging,
            profile,
            routes,
            transaction_id=transaction_id,
        )
        source_db = profile.home / "state" / "sessions.db"
        session_result: SessionMergeResult | None = None
        if _lexists(source_db):
            source_db_stat = source_db.lstat()
            if _is_link_or_reparse(source_db_stat) or not stat.S_ISREG(source_db_stat.st_mode):
                raise UnsafePathError(
                    f"recovery sessions database must be a regular file: {source_db}"
                )
            session_result = _merge_session_database_idempotent(
                state_destination / "sessions.db",
                source_db,
                source_id=profile.recovery_id,
                transaction_id=transaction_id,
                external=routes.state.external,
            )
            session_results.append(session_result)
        _merge_profile_media(
            staging,
            profile,
            session_result,
            destination_media=media_destination,
            media_external=routes.media.external,
            transaction_id=transaction_id,
        )
    return tuple(session_results)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _write_primary_context(user_data: Path) -> None:
    context_path = user_data / _CONTEXT_NAME
    _plain_optional_file(context_path, label="Desktop profile context")
    _write_json_atomic(
        context_path,
        {
            "schema_version": 1,
            "active_profile_kind": "primary",
            "active_recovery_id": None,
            "attention_acknowledgement": None,
            "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        },
    )


def _result_from_payload(
    payload: dict[str, Any], *, outcome: ConsolidationOutcome
) -> ConsolidationResult:
    credential_adoption_status = payload.get("credential_adoption_status")
    if credential_adoption_status not in {"pending", "complete", "not_required"}:
        raise UnsafePathError("profile consolidation credential adoption status is invalid")
    credential_sha256 = payload.get("configuration_source_credential_sha256")
    credential_size = payload.get("configuration_source_credential_size")
    if credential_sha256 is not None and (
        not isinstance(credential_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", credential_sha256) is None
    ):
        raise UnsafePathError("profile consolidation credential digest is invalid")
    if credential_size is not None and (
        not isinstance(credential_size, int)
        or isinstance(credential_size, bool)
        or credential_size < 0
        or credential_size > 2**53 - 1
    ):
        raise UnsafePathError("profile consolidation credential size is invalid")
    return ConsolidationResult(
        schema_version=1,
        outcome=outcome,
        stable_code=str(payload["stable_code"]),
        primary_home=Path(str(payload["primary_home"])),
        configuration_source_recovery_id=payload.get("configuration_source_recovery_id"),
        configuration_source_credential_path=(
            Path(str(payload["configuration_source_credential_path"]))
            if payload.get("configuration_source_credential_path")
            else None
        ),
        configuration_source_credential_sha256=credential_sha256,
        configuration_source_credential_size=credential_size,
        consumed_recovery_ids=tuple(str(item) for item in payload["consumed_recovery_ids"]),
        backup_path=Path(str(payload["backup_path"])) if payload.get("backup_path") else None,
        receipt_path=Path(str(payload["receipt_path"])) if payload.get("receipt_path") else None,
        credential_adoption_status=credential_adoption_status,
        revision=int(payload["revision"]),
        errors=tuple(str(item) for item in payload.get("errors", [])),
    )


def _latest_receipt(user_data: Path, primary_home: Path) -> ConsolidationResult | None:
    root = user_data / _BACKUPS_RELATIVE
    try:
        root_stat = root.lstat()
        if _is_link_or_reparse(root_stat) or not stat.S_ISDIR(root_stat.st_mode):
            return None
        receipts: list[Path] = []
        with os.scandir(root) as entries:
            for entry in entries:
                transaction_root = Path(entry.path)
                value = transaction_root.lstat()
                if _is_link_or_reparse(value) or not stat.S_ISDIR(value.st_mode):
                    continue
                receipt = transaction_root / "receipt.json"
                try:
                    receipt_stat = receipt.lstat()
                except OSError:
                    continue
                if _is_link_or_reparse(receipt_stat) or not stat.S_ISREG(receipt_stat.st_mode):
                    continue
                receipts.append(receipt)
        receipts.sort(
            key=lambda receipt: (receipt.lstat().st_mtime_ns, receipt.parent.name),
            reverse=True,
        )
    except OSError:
        return None
    if not receipts:
        return None
    path = receipts[0]
    try:
        if not _plain_optional_file(path, label="profile consolidation receipt"):
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 1
            or payload.get("transaction_id") != path.parent.name
            or Path(str(payload.get("primary_home"))) != primary_home
            or Path(str(payload.get("receipt_path"))) != path
        ):
            return None
        result = _result_from_payload(payload, outcome="noop")
        transaction_id = _validated_transaction_id(path.parent.name)
        _validate_result_paths(
            result,
            user_data=user_data,
            primary_home=primary_home,
            transaction_id=transaction_id,
        )
        _validate_result_credential(result)
        return ConsolidationResult(
            **{
                **result.__dict__,
                "stable_code": "profile_consolidation_already_complete",
            }
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError, UnsafePathError):
        return None


def _prepare_backup_path(user_data: Path, transaction_id: str) -> Path:
    current = user_data
    for part in _BACKUPS_RELATIVE.parts:
        current = current / part
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        value = current.lstat()
        if _is_link_or_reparse(value) or not stat.S_ISDIR(value.st_mode):
            raise UnsafePathError(f"profile consolidation backup root is unsafe: {current}")
    backup_path = current / transaction_id
    backup_path.mkdir(mode=0o700)
    _plain_directory(backup_path, label="profile consolidation transaction backup")
    return backup_path


def _validated_transaction_id(value: object) -> str:
    if not isinstance(value, str):
        raise UnsafePathError("profile consolidation transaction id is invalid")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise UnsafePathError("profile consolidation transaction id is invalid") from exc
    if parsed.version != 4 or str(parsed) != value.lower():
        raise UnsafePathError("profile consolidation transaction id is invalid")
    return str(parsed)


def _validate_result_paths(
    result: ConsolidationResult,
    *,
    user_data: Path,
    primary_home: Path,
    transaction_id: str,
) -> None:
    backup_path = user_data / _BACKUPS_RELATIVE / transaction_id
    if result.primary_home != primary_home:
        raise UnsafePathError("profile consolidation result primary path is invalid")
    if result.backup_path != backup_path or result.receipt_path != backup_path / "receipt.json":
        raise UnsafePathError("profile consolidation result backup path is invalid")
    for recovery_id in result.consumed_recovery_ids:
        if not _RECOVERY_ID_RE.fullmatch(recovery_id):
            raise UnsafePathError("profile consolidation result recovery id is invalid")
    source_id = result.configuration_source_recovery_id
    source_credential = result.configuration_source_credential_path
    credential_sha256 = result.configuration_source_credential_sha256
    credential_size = result.configuration_source_credential_size
    if source_id is not None and source_id not in result.consumed_recovery_ids:
        raise UnsafePathError("profile consolidation configuration source is invalid")
    if source_credential is not None:
        if source_id is None:
            raise UnsafePathError("profile consolidation credential source is invalid")
        expected = backup_path / "recovery-profiles" / source_id / _CREDENTIAL_NAME
        if source_credential != expected:
            raise UnsafePathError("profile consolidation credential path is invalid")
    if (
        source_credential is None
        and (
            result.credential_adoption_status != "not_required"
            or credential_sha256 is not None
            or credential_size is not None
        )
    ) or (
        source_credential is not None
        and (
            result.credential_adoption_status not in {"pending", "complete"}
            or credential_sha256 is None
            or credential_size is None
        )
    ):
        raise UnsafePathError("profile consolidation credential adoption status is inconsistent")


def _validate_result_credential(result: ConsolidationResult) -> None:
    credential = result.configuration_source_credential_path
    if credential is None or result.credential_adoption_status != "pending":
        return
    authority = _file_authority(
        credential,
        label="archived recovery credential",
    )
    if (
        not bool(authority.get("exists"))
        or authority.get("sha256") != result.configuration_source_credential_sha256
        or authority.get("size") != result.configuration_source_credential_size
    ):
        raise _ConsolidationBlockedError(
            "archived recovery credential changed after consolidation",
            stable_code="profile_consolidation_source_changed",
        )


def _receipt_payload(
    user_data: Path,
    primary_home: Path,
    transaction_id: str,
) -> tuple[dict[str, Any], ConsolidationResult]:
    backups = user_data / "backups"
    consolidation_root = backups / "profile-consolidation"
    transaction_root = consolidation_root / transaction_id
    for path, label in (
        (backups, "profile consolidation backup root"),
        (consolidation_root, "profile consolidation receipt root"),
        (transaction_root, "profile consolidation transaction backup"),
    ):
        _plain_directory(path, label=label)
    receipt_path = transaction_root / "receipt.json"
    if not _plain_optional_file(
        receipt_path,
        label="profile consolidation receipt",
    ):
        raise _ConsolidationBlockedError(
            "profile consolidation receipt is missing",
            stable_code="profile_consolidation_receipt_missing",
        )
    before = _metadata_identity(receipt_path)
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    after = _metadata_identity(receipt_path)
    if before is None or after != before:
        raise UnsafePathError("profile consolidation receipt changed while reading")
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("transaction_id") != transaction_id
        or payload.get("outcome") != "consolidated"
        or Path(str(payload.get("receipt_path"))) != receipt_path
    ):
        raise UnsafePathError("profile consolidation receipt is invalid")
    result = _result_from_payload(payload, outcome="noop")
    _validate_result_paths(
        result,
        user_data=user_data,
        primary_home=primary_home,
        transaction_id=transaction_id,
    )
    _validate_result_credential(result)
    return payload, result


def _journal_payload(
    *,
    transaction_id: str,
    phase: str,
    user_data: Path,
    primary_home: Path,
    staging: Path,
    recovery_root: Path,
    backup_path: Path,
    primary_existed: bool,
    result: ConsolidationResult,
    session_results: tuple[SessionMergeResult, ...],
    primary_config: dict[str, object],
    use_primary_configuration: bool,
    routes: _PrimaryDataRoutes,
    source_snapshots: dict[str, str],
    staging_baseline: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "transaction_id": transaction_id,
        "phase": phase,
        "user_data": str(user_data),
        "primary_home": str(primary_home),
        "staging": str(staging),
        "recovery_root": str(recovery_root),
        "backup_path": str(backup_path),
        "primary_existed": primary_existed,
        "primary_config": primary_config,
        "use_primary_configuration": use_primary_configuration,
        "routes": routes.as_dict(),
        "source_snapshots": source_snapshots,
        "staging_baseline": staging_baseline,
        "staging_merged": None,
        "result": result.as_dict(),
        "session_merges": [item.as_dict() for item in session_results],
    }


def _update_journal(
    path: Path,
    payload: dict[str, Any],
    phase: str,
    **changes: object,
) -> dict[str, Any]:
    updated = {**payload, **changes, "phase": phase}
    _write_json_atomic(path, updated)
    return updated


def _recovery_move_policy(ids: tuple[str, ...]) -> tuple[frozenset[str], frozenset[str]]:
    link_roots: set[str] = set()
    opaque: set[str] = set()
    for recovery_id in ids:
        prefix = f"{recovery_id}/opensquilla"
        link_roots.update({f"{prefix}/workspace", f"{prefix}/state/workspace"})
        opaque.update({f"{prefix}/code-task", f"{prefix}/sandbox"})
    return frozenset(link_roots), frozenset(opaque)


def _configuration_home_for_journal(
    payload: dict[str, Any],
    primary_home: Path,
) -> Path:
    phase = str(payload["phase"])
    parked = Path(str(payload["backup_path"])) / "primary"
    if phase == "primary_parked" and bool(payload["primary_existed"]) and _lexists(parked):
        return parked
    if _lexists(primary_home):
        return primary_home
    if bool(payload["primary_existed"]) and _lexists(parked):
        return parked
    return primary_home


def _validate_journal_authority(
    *,
    user_data: Path,
    primary_home: Path,
    payload: dict[str, Any],
    profiles: tuple[_RecoveryProfile, ...],
    require_staging: bool = True,
    expected_staging_token: str | None = None,
) -> _PrimaryDataRoutes:
    expected_snapshots = payload.get("source_snapshots")
    if (
        not isinstance(expected_snapshots, dict)
        or _profile_snapshot_tokens(profiles) != expected_snapshots
    ):
        raise _ConsolidationBlockedError(
            "recovery profile changed after consolidation was prepared",
            stable_code="profile_consolidation_source_changed",
        )
    configuration_home = _configuration_home_for_journal(payload, primary_home)
    if _primary_config_authority(configuration_home) != payload.get("primary_config"):
        raise _ConsolidationBlockedError(
            "primary config.toml changed after consolidation was prepared",
            stable_code="profile_consolidation_source_changed",
        )
    use_primary_configuration = payload.get("use_primary_configuration")
    if not isinstance(use_primary_configuration, bool):
        raise UnsafePathError("profile consolidation configuration routing flag is invalid")
    routes = _build_primary_data_routes(
        user_data=user_data,
        primary_home=primary_home,
        profiles=profiles,
        use_primary_configuration=use_primary_configuration,
        configuration_home=configuration_home,
    )
    expected_routes = payload.get("routes")
    current_routes = routes.as_dict()
    if not isinstance(expected_routes, dict):
        raise UnsafePathError("profile consolidation routes are invalid")
    expected_bindings = expected_routes.get("external_bindings")
    current_bindings = current_routes.get("external_bindings")
    expected_topology = {
        key: value for key, value in expected_routes.items() if key != "external_bindings"
    }
    current_topology = {
        key: value for key, value in current_routes.items() if key != "external_bindings"
    }
    if (
        current_topology != expected_topology
        or not isinstance(expected_bindings, list)
        or not isinstance(current_bindings, list)
        or {
            str(item.get("normalized_path")) for item in expected_bindings if isinstance(item, dict)
        }
        != {str(item.get("normalized_path")) for item in current_bindings if isinstance(item, dict)}
    ):
        raise UnsafePathError("primary data routing changed after consolidation was prepared")
    for binding in expected_bindings:
        _validate_directory_chain_binding(binding)
    staging = Path(str(payload["staging"]))
    if require_staging:
        if not staging.is_dir():
            raise UnsafePathError("profile consolidation staging directory is missing")
        profile_no_follow_manifest(staging)
        if (
            expected_staging_token is not None
            and _profile_content_token(staging) != expected_staging_token
        ):
            raise UnsafePathError("profile consolidation staging content changed")
    return routes


def _profile_snapshot_tokens(
    profiles: tuple[_RecoveryProfile, ...],
) -> dict[str, str]:
    return {profile.recovery_id: _source_snapshot_token(profile) for profile in profiles}


def _required_state_roots(
    routes: _PrimaryDataRoutes,
    staging: Path,
) -> tuple[Path, ...]:
    return (routes.state.path,) if routes.state.external else (routes.state.destination(staging),)


def _prepare_required_state_roots(
    routes: _PrimaryDataRoutes,
    staging: Path,
) -> None:
    for state_root in _required_state_roots(routes, staging):
        _ensure_plain_directory(state_root)


def _rebuild_staging_from_authority(
    *,
    primary_home: Path,
    payload: dict[str, Any],
    profiles: tuple[_RecoveryProfile, ...],
    routes: _PrimaryDataRoutes,
) -> None:
    staging = Path(str(payload["staging"]))
    if _lexists(staging):
        profile_no_follow_manifest(staging)
        shutil.rmtree(staging)
    source_home = _configuration_home_for_journal(payload, primary_home)
    _clone_primary(source_home, staging)
    result = _result_from_payload(dict(payload["result"]), outcome="consolidated")
    configuration_source = next(
        (
            profile
            for profile in profiles
            if profile.recovery_id == result.configuration_source_recovery_id
        ),
        None,
    )
    if result.configuration_source_recovery_id is not None and configuration_source is None:
        raise UnsafePathError("profile consolidation configuration source disappeared")
    _apply_configuration_source(
        staging,
        configuration_source,
        workspace_root=routes.workspace.path,
    )
    if _profile_content_token(staging) != payload.get("staging_baseline"):
        raise UnsafePathError("rebuilt consolidation staging does not match its journal baseline")


def _merge_prepared_profiles(
    *,
    user_data: Path,
    primary_home: Path,
    journal_path: Path,
    payload: dict[str, Any],
    profiles: tuple[_RecoveryProfile, ...],
    routes: _PrimaryDataRoutes,
) -> dict[str, Any]:
    session_results = _merge_recovery_data(
        Path(str(payload["staging"])),
        profiles,
        routes,
        transaction_id=str(payload["transaction_id"]),
    )
    refreshed_routes = _validate_journal_authority(
        user_data=user_data,
        primary_home=primary_home,
        payload=payload,
        profiles=profiles,
    )
    return _update_journal(
        journal_path,
        payload,
        "external_roots_merged",
        session_merges=[item.as_dict() for item in session_results],
        staging_merged=_profile_content_token(Path(str(payload["staging"]))),
        routes=refreshed_routes.as_dict(),
    )


def _verify_external_roots_merged(
    *,
    user_data: Path,
    primary_home: Path,
    payload: dict[str, Any],
    profiles: tuple[_RecoveryProfile, ...],
    routes: _PrimaryDataRoutes,
) -> None:
    _merge_recovery_data(
        Path(str(payload["staging"])),
        profiles,
        routes,
        transaction_id=str(payload["transaction_id"]),
    )
    _validate_journal_authority(
        user_data=user_data,
        primary_home=primary_home,
        payload=payload,
        profiles=profiles,
    )
    expected = payload.get("staging_merged")
    if (
        not isinstance(expected, str)
        or _profile_content_token(Path(str(payload["staging"]))) != expected
    ):
        raise UnsafePathError("merged consolidation staging no longer matches the journal")


def _commit_primary(
    journal_path: Path,
    payload: dict[str, Any],
) -> dict[str, Any]:
    phase = str(payload["phase"])
    primary = Path(payload["primary_home"])
    staging = Path(payload["staging"])
    backup_path = Path(payload["backup_path"])
    parked = backup_path / "primary"
    if phase == "external_roots_merged":
        if bool(payload["primary_existed"]):
            if primary.exists() and not parked.exists():
                move_profile_no_replace(primary, parked)
            elif primary.exists() or not parked.exists():
                raise UnsafePathError("primary park state is ambiguous")
        payload = _update_journal(journal_path, payload, "primary_parked")
        phase = "primary_parked"
    if phase == "primary_parked":
        if staging.exists() and not primary.exists():
            move_profile_no_replace(staging, primary)
        elif staging.exists() or not primary.exists():
            raise UnsafePathError("primary publish state is ambiguous")
        payload = _update_journal(journal_path, payload, "primary_published")
    return payload


def _archive_and_finish(
    user_data: Path,
    journal_path: Path,
    payload: dict[str, Any],
) -> ConsolidationResult:
    phase = str(payload["phase"])
    result = _result_from_payload(dict(payload["result"]), outcome="consolidated")
    recovery_root = Path(payload["recovery_root"])
    backup_path = Path(payload["backup_path"])
    archived = backup_path / "recovery-profiles"
    if phase == "primary_published":
        if recovery_root.exists() and not archived.exists():
            active_profiles = _enumerate_recoveries(user_data)
            if tuple(
                profile.recovery_id for profile in active_profiles
            ) != result.consumed_recovery_ids or _profile_snapshot_tokens(
                active_profiles
            ) != payload.get("source_snapshots"):
                raise _ConsolidationBlockedError(
                    "recovery profiles changed before archival",
                    stable_code="profile_consolidation_source_changed",
                )
            link_roots, opaque = _recovery_move_policy(result.consumed_recovery_ids)
            move_profile_no_replace(
                recovery_root,
                archived,
                link_leaf_manifest_directories=link_roots,
                opaque_manifest_directories=opaque,
                use_profile_manifest_policy=False,
            )
        elif recovery_root.exists() or not archived.exists():
            raise UnsafePathError("recovery archive state is ambiguous")
        payload = _update_journal(journal_path, payload, "recoveries_archived")
        phase = "recoveries_archived"
    if phase in {"recoveries_archived", "context_written"}:
        _validate_result_credential(result)
    if phase == "recoveries_archived":
        receipt_path = result.receipt_path
        assert receipt_path is not None
        receipt = {
            **result.as_dict(),
            "transaction_id": payload["transaction_id"],
            "session_merges": payload.get("session_merges", []),
            "excluded_operational_state": [
                "scheduler/security/approval/channel-delivery databases",
                "router/turn-error/meta-run/global-usage-cursor rows in sessions.db",
                "derived per-agent memory.db indexes",
            ],
        }
        _write_json_atomic(receipt_path, receipt)
        _write_primary_context(user_data)
        payload = _update_journal(journal_path, payload, "context_written")
        phase = "context_written"
    if phase != "context_written":
        raise UnsafePathError(f"unsupported consolidation journal phase: {phase}")
    journal_path.unlink()
    return result


def _load_journal(path: Path, user_data: Path, primary_home: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if not _plain_optional_file(path, label="profile consolidation journal"):
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    transaction_id = _validated_transaction_id(
        payload.get("transaction_id") if isinstance(payload, dict) else None
    )
    expected_staging = user_data / f"{_STAGING_PREFIX}{transaction_id}.staging"
    expected_backup = user_data / _BACKUPS_RELATIVE / transaction_id
    expected_recovery_root = user_data / "recovery-profiles"
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or Path(str(payload.get("user_data"))) != user_data
        or Path(str(payload.get("primary_home"))) != primary_home
        or Path(str(payload.get("staging"))) != expected_staging
        or Path(str(payload.get("backup_path"))) != expected_backup
        or Path(str(payload.get("recovery_root"))) != expected_recovery_root
        or not isinstance(payload.get("primary_existed"), bool)
        or not isinstance(payload.get("primary_config"), dict)
        or not isinstance(payload.get("use_primary_configuration"), bool)
        or not isinstance(payload.get("routes"), dict)
        or not isinstance(payload.get("source_snapshots"), dict)
        or not isinstance(payload.get("staging_baseline"), str)
        or (
            payload.get("staging_merged") is not None
            and not isinstance(payload.get("staging_merged"), str)
        )
        or not isinstance(payload.get("session_merges"), list)
        or not isinstance(payload.get("result"), dict)
        or payload.get("phase")
        not in {
            "prepared",
            "external_roots_merged",
            "primary_parked",
            "primary_published",
            "recoveries_archived",
            "context_written",
        }
    ):
        raise UnsafePathError("profile consolidation journal is invalid")
    if payload["phase"] != "prepared" and not isinstance(
        payload.get("staging_merged"),
        str,
    ):
        raise UnsafePathError("profile consolidation merged staging token is invalid")
    result = _result_from_payload(dict(payload["result"]), outcome="consolidated")
    _validate_result_paths(
        result,
        user_data=user_data,
        primary_home=primary_home,
        transaction_id=transaction_id,
    )
    return payload


def _resume(
    user_data: Path,
    primary_home: Path,
    journal_path: Path,
    payload: dict[str, Any],
) -> ConsolidationResult:
    result = _result_from_payload(dict(payload["result"]), outcome="consolidated")
    phase = str(payload["phase"])
    profiles: tuple[_RecoveryProfile, ...] = ()
    archived_profiles: tuple[_RecoveryProfile, ...] = ()
    recovery_homes: tuple[Path, ...] = ()
    canonical_only_recovery_homes: tuple[Path, ...] = ()
    if phase not in {"recoveries_archived", "context_written"}:
        active_root = user_data / "recovery-profiles"
        archived_root = Path(payload["backup_path"]) / "recovery-profiles"
        active_exists = _lexists(active_root)
        archived_exists = _lexists(archived_root)
        if active_exists and archived_exists:
            raise UnsafePathError("recovery archive state is ambiguous")
        if active_exists:
            profiles = _enumerate_recoveries(user_data)
            if tuple(profile.recovery_id for profile in profiles) != (result.consumed_recovery_ids):
                raise UnsafePathError(
                    "active recovery profiles do not match the consolidation journal"
                )
            recovery_homes = tuple(profile.home for profile in profiles)
            canonical_only_recovery_homes = _canonical_only_legacy_homes(profiles)
            if _profile_snapshot_tokens(profiles) != payload.get("source_snapshots"):
                raise _ConsolidationBlockedError(
                    "active recovery profile changed after consolidation",
                    stable_code="profile_consolidation_source_changed",
                )
        elif phase == "primary_published" and archived_exists:
            # The container move may have committed before its journal phase did.
            # Validate the archived tree, but never acquire runtime locks inside it.
            archived_profiles = _enumerate_recoveries(Path(payload["backup_path"]))
            if tuple(profile.recovery_id for profile in archived_profiles) != (
                result.consumed_recovery_ids
            ):
                raise UnsafePathError(
                    "archived recovery profiles do not match the consolidation journal"
                )
        else:
            raise UnsafePathError("recovery profiles disappeared before consolidation was archived")
    else:
        archived_root = Path(payload["backup_path"]) / "recovery-profiles"
        if not _lexists(archived_root):
            raise UnsafePathError(
                "archived recovery profiles disappeared before consolidation finished"
            )
        archived_profiles = _enumerate_recoveries(Path(payload["backup_path"]))
        if tuple(profile.recovery_id for profile in archived_profiles) != (
            result.consumed_recovery_ids
        ):
            raise UnsafePathError(
                "archived recovery profiles do not match the consolidation journal"
            )
    if archived_profiles and _profile_snapshot_tokens(archived_profiles) != payload.get(
        "source_snapshots"
    ):
        raise _ConsolidationBlockedError(
            "archived recovery profile changed after consolidation",
            stable_code="profile_consolidation_source_changed",
        )
    with acquire_profile_locks(primary_home, *recovery_homes, timeout=0.0):
        routes: _PrimaryDataRoutes | None = None
        staging_exists = _lexists(Path(str(payload["staging"])))
        staging_path = Path(str(payload["staging"]))
        authority_required = phase in {"prepared", "external_roots_merged"} or (
            phase == "primary_parked" and staging_exists
        )
        if profiles and authority_required:
            routes = _validate_journal_authority(
                user_data=user_data,
                primary_home=primary_home,
                payload=payload,
                profiles=profiles,
                require_staging=False,
            )
            if phase in {"prepared", "external_roots_merged"}:
                _rebuild_staging_from_authority(
                    primary_home=primary_home,
                    payload=payload,
                    profiles=profiles,
                    routes=routes,
                )
            expected_staging_token = (
                str(payload["staging_baseline"])
                if phase in {"prepared", "external_roots_merged"}
                else str(payload["staging_merged"])
            )
            routes = _validate_journal_authority(
                user_data=user_data,
                primary_home=primary_home,
                payload=payload,
                profiles=profiles,
                expected_staging_token=expected_staging_token,
            )
            _prepare_required_state_roots(routes, staging_path)
        legacy_homes = (primary_home, *recovery_homes) if primary_home.is_dir() else recovery_homes
        if phase in {"recoveries_archived", "context_written"}:
            legacy_homes = (primary_home,) if primary_home.is_dir() else ()
        with acquire_legacy_gateway_locks(
            *legacy_homes,
            read_only_homes=recovery_homes,
            canonical_only_homes=canonical_only_recovery_homes,
            required_state_roots=(
                _required_state_roots(routes, staging_path) if routes is not None else ()
            ),
            timeout=0.0,
        ):
            if routes is not None:
                routes = _validate_journal_authority(
                    user_data=user_data,
                    primary_home=primary_home,
                    payload=payload,
                    profiles=profiles,
                    expected_staging_token=(
                        str(payload["staging_baseline"])
                        if phase in {"prepared", "external_roots_merged"}
                        else str(payload["staging_merged"])
                    ),
                )
            if phase == "prepared":
                if routes is None:
                    raise UnsafePathError("prepared consolidation has no active recovery profiles")
                payload = _merge_prepared_profiles(
                    user_data=user_data,
                    primary_home=primary_home,
                    journal_path=journal_path,
                    payload=payload,
                    profiles=profiles,
                    routes=routes,
                )
            elif phase == "external_roots_merged":
                if routes is None:
                    raise UnsafePathError("merged consolidation has no active recovery profiles")
                _verify_external_roots_merged(
                    user_data=user_data,
                    primary_home=primary_home,
                    payload=payload,
                    profiles=profiles,
                    routes=routes,
                )
            payload = _commit_primary(journal_path, payload)
            return _archive_and_finish(user_data, journal_path, payload)


def consolidate_recovery_profiles(
    user_data: str | Path,
    primary_home: str | Path,
) -> ConsolidationResult:
    """Merge all Desktop recovery data and leave only the primary profile active."""

    user_data_path = _absolute(user_data)
    primary_path = _absolute(primary_home)
    journal_path = user_data_path / _JOURNAL_NAME
    try:
        _validate_base_paths(user_data_path, primary_path)
        journal = _load_journal(journal_path, user_data_path, primary_path)
        if journal is not None:
            return _resume(user_data_path, primary_path, journal_path, journal)

        profiles = _enumerate_recoveries(user_data_path)
        recovery_root = user_data_path / "recovery-profiles"
        if not profiles:
            if recovery_root.exists():
                recovery_root.rmdir()
            if _lexists(user_data_path / _CONTEXT_NAME):
                _write_primary_context(user_data_path)
            previous = _latest_receipt(user_data_path, primary_path)
            if previous is not None:
                return previous
            return ConsolidationResult(
                schema_version=1,
                outcome="noop",
                stable_code="no_recovery_profiles",
                primary_home=primary_path,
                configuration_source_recovery_id=None,
                configuration_source_credential_path=None,
                configuration_source_credential_sha256=None,
                configuration_source_credential_size=None,
                consumed_recovery_ids=(),
                backup_path=None,
                receipt_path=None,
                credential_adoption_status="not_required",
                revision=0,
            )

        transaction_id = str(uuid.uuid4())
        backup_path = user_data_path / _BACKUPS_RELATIVE / transaction_id
        receipt_path = backup_path / "receipt.json"
        staging = user_data_path / f"{_STAGING_PREFIX}{transaction_id}.staging"
        consumed = tuple(profile.recovery_id for profile in profiles)
        homes = tuple(profile.home for profile in profiles)
        try:
            with acquire_profile_locks(primary_path, *homes, timeout=0.0):
                locked_profiles = _enumerate_recoveries(user_data_path)
                if tuple(profile.recovery_id for profile in locked_profiles) != consumed:
                    raise _ConsolidationBlockedError(
                        "recovery profiles changed before consolidation acquired its locks",
                        stable_code="profile_consolidation_source_changed",
                    )
                profiles = locked_profiles
                revision = _revision(profiles)
                configuration_source = _configuration_source(
                    user_data_path,
                    primary_path,
                    profiles,
                )
                credential_path = (
                    backup_path
                    / "recovery-profiles"
                    / configuration_source.recovery_id
                    / _CREDENTIAL_NAME
                    if configuration_source is not None
                    and _lexists(configuration_source.credential)
                    else None
                )
                credential_authority = (
                    _file_authority(
                        configuration_source.credential,
                        label="recovery credential",
                    )
                    if credential_path is not None and configuration_source is not None
                    else None
                )
                result = ConsolidationResult(
                    schema_version=1,
                    outcome="consolidated",
                    stable_code="profile_consolidation_complete",
                    primary_home=primary_path,
                    configuration_source_recovery_id=(
                        configuration_source.recovery_id
                        if configuration_source is not None
                        else None
                    ),
                    configuration_source_credential_path=credential_path,
                    configuration_source_credential_sha256=(
                        str(credential_authority["sha256"])
                        if credential_authority is not None
                        else None
                    ),
                    configuration_source_credential_size=(
                        int(credential_authority["size"])
                        if credential_authority is not None
                        else None
                    ),
                    consumed_recovery_ids=consumed,
                    backup_path=backup_path,
                    receipt_path=receipt_path,
                    credential_adoption_status=(
                        "pending" if credential_path is not None else "not_required"
                    ),
                    revision=revision,
                )
                canonical_only_homes = _canonical_only_legacy_homes(profiles)
                primary_existed = primary_path.exists()
                source_snapshots = {
                    profile.recovery_id: _source_snapshot_token(profile) for profile in profiles
                }
                primary_config = _primary_config_authority(primary_path)
                use_primary_configuration = configuration_source is None
                routes = _build_primary_data_routes(
                    user_data=user_data_path,
                    primary_home=primary_path,
                    profiles=profiles,
                    use_primary_configuration=use_primary_configuration,
                )
                _clone_primary(primary_path, staging)
                _apply_configuration_source(
                    staging,
                    configuration_source,
                    workspace_root=routes.workspace.path,
                )
                if {
                    profile.recovery_id: _source_snapshot_token(profile) for profile in profiles
                } != source_snapshots:
                    raise _ConsolidationBlockedError(
                        "recovery profile changed while it was being prepared",
                        stable_code="profile_consolidation_source_changed",
                    )
                if _primary_config_authority(primary_path) != primary_config:
                    raise _ConsolidationBlockedError(
                        "primary config.toml changed while consolidation was prepared",
                        stable_code="profile_consolidation_source_changed",
                    )
                profile_no_follow_manifest(staging)
                prepared_backup = _prepare_backup_path(
                    user_data_path,
                    transaction_id,
                )
                if prepared_backup != backup_path:
                    raise UnsafePathError(
                        "profile consolidation backup path changed during preparation"
                    )
                payload = _journal_payload(
                    transaction_id=transaction_id,
                    phase="prepared",
                    user_data=user_data_path,
                    primary_home=primary_path,
                    staging=staging,
                    recovery_root=recovery_root,
                    backup_path=backup_path,
                    primary_existed=primary_existed,
                    result=result,
                    session_results=(),
                    primary_config=primary_config,
                    use_primary_configuration=use_primary_configuration,
                    routes=routes,
                    source_snapshots=source_snapshots,
                    staging_baseline=_profile_content_token(staging),
                )
                _write_json_atomic(journal_path, payload)
                routes = _validate_journal_authority(
                    user_data=user_data_path,
                    primary_home=primary_path,
                    payload=payload,
                    profiles=profiles,
                    expected_staging_token=str(payload["staging_baseline"]),
                )
                _prepare_required_state_roots(routes, staging)
                legacy_homes = (primary_path, *homes) if primary_existed else homes
                with acquire_legacy_gateway_locks(
                    *legacy_homes,
                    read_only_homes=homes,
                    canonical_only_homes=canonical_only_homes,
                    required_state_roots=_required_state_roots(routes, staging),
                    timeout=0.0,
                ):
                    routes = _validate_journal_authority(
                        user_data=user_data_path,
                        primary_home=primary_path,
                        payload=payload,
                        profiles=profiles,
                        expected_staging_token=str(payload["staging_baseline"]),
                    )
                    payload = _merge_prepared_profiles(
                        user_data=user_data_path,
                        primary_home=primary_path,
                        journal_path=journal_path,
                        payload=payload,
                        profiles=profiles,
                        routes=routes,
                    )
                    payload = _commit_primary(journal_path, payload)
                    return _archive_and_finish(
                        user_data_path,
                        journal_path,
                        payload,
                    )
        except BaseException:
            # Before a journal exists no authoritative path has moved.  Remove
            # only the UUID staging tree created by this invocation.
            if not journal_path.exists() and staging.exists():
                with contextlib.suppress(OSError):
                    profile_no_follow_manifest(staging)
                    shutil.rmtree(staging)
            raise
    except RecoveryError as exc:
        return _blocked(primary_path, exc.stable_code, exc)
    except Exception as exc:
        return _blocked(
            primary_path,
            "profile_consolidation_failed",
            exc,
        )


def acknowledge_profile_credential(
    user_data: str | Path,
    primary_home: str | Path,
    transaction_id: str,
) -> ConsolidationResult:
    """Atomically mark one archived credential adoption as terminal."""

    user_data_path = _absolute(user_data)
    primary_path = _absolute(primary_home)
    try:
        _validate_base_paths(user_data_path, primary_path)
        validated_transaction_id = _validated_transaction_id(transaction_id)
        with acquire_profile_locks(primary_path, timeout=0.0):
            payload, result = _receipt_payload(
                user_data_path,
                primary_path,
                validated_transaction_id,
            )
            if result.credential_adoption_status == "not_required":
                raise _ConsolidationBlockedError(
                    "profile consolidation receipt has no credential adoption to acknowledge",
                    stable_code="profile_credential_adoption_not_pending",
                )
            if result.credential_adoption_status == "pending":
                receipt_path = result.receipt_path
                assert receipt_path is not None
                _write_json_atomic(
                    receipt_path,
                    {
                        **payload,
                        "credential_adoption_status": "complete",
                    },
                )
                _, result = _receipt_payload(
                    user_data_path,
                    primary_path,
                    validated_transaction_id,
                )
                if result.credential_adoption_status != "complete":
                    raise UnsafePathError("profile credential acknowledgement did not commit")
            return ConsolidationResult(
                **{
                    **result.__dict__,
                    "outcome": "noop",
                    "stable_code": "profile_credential_adoption_acknowledged",
                }
            )
    except RecoveryError as exc:
        return _blocked(primary_path, exc.stable_code, exc)
    except Exception as exc:
        return _blocked(
            primary_path,
            "profile_credential_adoption_failed",
            exc,
        )


__all__ = [
    "ConsolidationResult",
    "acknowledge_profile_credential",
    "consolidate_recovery_profiles",
]
