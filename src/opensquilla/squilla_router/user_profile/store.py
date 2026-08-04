"""Versioned on-disk store for offline-produced user profiles.

Layout under the per-agent router data dir (reusing ``self_learning.store``'s
root and agent-id sanitizer, never the repo, never the decision log)::

    ~/.opensquilla/router/data/<agent_id>/profiles/
        user_profile.2026-07-10.1.json   # a produced version (kept, never overwritten)
        user_profile.2026-07-09.1.json
        active                           # one line: the active version filename

The ``active`` pointer here is *independent* of ``self_learning``'s
``router/active`` bundle pointer — a different artifact with a different
lifecycle. Version files are immutable and the active-pointer update is atomic;
reads fail open to ``None`` so a missing/corrupt profile degrades to the mock
baseline.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from opensquilla.profile_operation_lock import (
    ProfileOperationBusyError,
    acquire_profile_operation_lock,
)
from opensquilla.squilla_router.data_paths import agent_data_dir

PROFILE_PREFIX = "user_profile"
ACTIVE_POINTER = "active"
PROFILE_STATE_FILENAME = ".profile_state.json"
PROFILE_PUBLICATION_LOCK_TIMEOUT_SECONDS = 0.25

log = structlog.get_logger(__name__)

# ``user_profile.<YYYY-MM-DD>.<N>.json`` — the version is the date + a per-day
# sequence, matching the offline doc's filename scheme (§1.6).
_VERSION_FILE_RE = re.compile(r"^user_profile\.(?P<date>\d{4}-\d{2}-\d{2})\.(?P<seq>\d+)\.json$")


def profiles_dir(agent_id: str, home: Path | None = None) -> Path:
    """The per-agent directory holding produced profile versions."""

    return agent_data_dir(agent_id, home) / "profiles"


def active_pointer_path(agent_id: str, home: Path | None = None) -> Path:
    return profiles_dir(agent_id, home) / ACTIVE_POINTER


def profile_state_path(agent_id: str, home: Path | None = None) -> Path:
    return profiles_dir(agent_id, home) / PROFILE_STATE_FILENAME


def version_filename(version: str) -> str:
    return f"{PROFILE_PREFIX}.{version}.json"


def next_version(day: str, agent_id: str, home: Path | None = None) -> str:
    """Return ``<day>.<N>`` where N is the next unused sequence for ``day``.

    Scans existing files so a same-day re-run bumps the sequence rather than
    overwriting a prior version (§1.6: history is never clobbered).
    """

    directory = profiles_dir(agent_id, home)
    highest = 0
    if directory.is_dir():
        for path in directory.glob(f"{PROFILE_PREFIX}.{day}.*.json"):
            match = _VERSION_FILE_RE.match(path.name)
            if match is None or match.group("date") != day:
                continue
            highest = max(highest, int(match.group("seq")))
    return f"{day}.{highest + 1}"


def write_profile_version(
    payload: dict,
    version: str,
    agent_id: str,
    *,
    home: Path | None = None,
) -> Path:
    """Write one immutable version file; return its path. Never overwrites."""

    directory = profiles_dir(agent_id, home)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / version_filename(version)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
    return path


def write_active_atomic(version: str, agent_id: str, *, home: Path | None = None) -> None:
    """Atomically point ``active`` at ``user_profile.<version>.json``."""

    path = active_pointer_path(agent_id, home)
    write_text_atomic(path, version_filename(version))


class ProfilePublicationBusyError(RuntimeError):
    """Raised when another writer owns the per-agent profile publication lock."""


@dataclass(frozen=True)
class ProfilePublicationResult:
    version: str
    version_path: Path
    active_path: Path
    state_path: Path
    state_committed: bool


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _unique_tmp_path(target: Path) -> Path:
    suffix = f"{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    return target.with_name(f".{target.name}.{suffix}")


def _write_file_exclusive(path: Path, text: str) -> Path:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if fd >= 0:
            os.close(fd)
    _fsync_dir(path.parent)
    return path


def _prepare_atomic_text(target: Path, text: str) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = _unique_tmp_path(target)
    try:
        return _write_file_exclusive(tmp, text)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _replace_prepared_text(tmp: Path, target: Path) -> None:
    os.replace(tmp, target)
    _fsync_dir(target.parent)


def write_text_atomic(path: Path, text: str) -> Path:
    """Atomically replace one text file using a unique same-directory temp file."""

    tmp = _prepare_atomic_text(path, text)
    try:
        _replace_prepared_text(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return path


def publish_profile(
    *,
    agent_id: str,
    day: str,
    build_payload: Callable[[str], Mapping[str, Any]],
    state_payload: Mapping[str, Any] | Callable[[str], Mapping[str, Any]],
    home: Path | None = None,
    lock_timeout: float = PROFILE_PUBLICATION_LOCK_TIMEOUT_SECONDS,
) -> ProfilePublicationResult:
    """Allocate, write, and publish one profile version under one profile lock.

    ``active`` replacement is the publication commit point. If the post-commit
    state replacement fails, the active pointer is intentionally left on the new
    immutable version and the caller receives ``state_committed=False``.
    """

    directory = profiles_dir(agent_id, home)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        with acquire_profile_operation_lock(directory, timeout=lock_timeout):
            version = next_version(day, agent_id, home)
            payload = dict(build_payload(version))
            payload_text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
            state = state_payload(version) if callable(state_payload) else state_payload
            state_text = json.dumps(dict(state), ensure_ascii=False, indent=2, allow_nan=False)

            version_path = directory / version_filename(version)
            _write_file_exclusive(version_path, payload_text)

            active_path = active_pointer_path(agent_id, home)
            state_path = profile_state_path(agent_id, home)
            active_tmp: Path | None = None
            state_tmp: Path | None = None
            try:
                active_tmp = _prepare_atomic_text(active_path, version_filename(version))
                state_tmp = _prepare_atomic_text(state_path, state_text)
                _replace_prepared_text(active_tmp, active_path)
                active_tmp = None
                try:
                    _replace_prepared_text(state_tmp, state_path)
                    state_tmp = None
                    state_committed = True
                except Exception as exc:  # noqa: BLE001 - post-commit state is repairable
                    state_committed = False
                    log.warning(
                        "user_profile.state_commit_failed",
                        agent_id=agent_id,
                        version=version,
                        error_category=type(exc).__name__,
                    )
                return ProfilePublicationResult(
                    version=version,
                    version_path=version_path,
                    active_path=active_path,
                    state_path=state_path,
                    state_committed=state_committed,
                )
            finally:
                for tmp in (active_tmp, state_tmp):
                    if tmp is not None:
                        try:
                            tmp.unlink()
                        except OSError:
                            pass
    except ProfileOperationBusyError as exc:
        raise ProfilePublicationBusyError("user profile publication is busy") from exc


def read_active_name(agent_id: str, home: Path | None = None) -> str | None:
    path = active_pointer_path(agent_id, home)
    if not path.is_file():
        return None
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def load_active_profile(agent_id: str, home: Path | None = None) -> dict | None:
    """The active produced profile as a raw dict (with ``_meta``), or ``None``.

    ``None`` covers every failure — no pointer, dangling pointer, malformed
JSON, wrong shape — so callers can degrade to their own defaults. Never raises:
a broken produced profile must not fail a turn.
    """

    name = read_active_name(agent_id, home)
    if not name:
        return None
    # The pointer names a bare filename inside profiles/; reject anything with a
    # path separator so a corrupt pointer cannot escape the directory.
    if name != Path(name).name:
        return None
    path = profiles_dir(agent_id, home) / name
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


__all__ = [
    "ACTIVE_POINTER",
    "PROFILE_PREFIX",
    "PROFILE_PUBLICATION_LOCK_TIMEOUT_SECONDS",
    "PROFILE_STATE_FILENAME",
    "ProfilePublicationBusyError",
    "ProfilePublicationResult",
    "active_pointer_path",
    "load_active_profile",
    "next_version",
    "profile_state_path",
    "profiles_dir",
    "publish_profile",
    "read_active_name",
    "version_filename",
    "write_active_atomic",
    "write_profile_version",
    "write_text_atomic",
]
