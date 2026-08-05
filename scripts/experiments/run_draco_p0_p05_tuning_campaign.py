#!/usr/bin/env python3
"""Serial, restart-safe controller for the P0/P0.5 DRACO-mini matrix.

This controller deliberately contains no credential handling and never calls a model
unless the explicit ``run`` command is used.  The formal launcher remains the
sole owner of preflight, the OpenRouter account window, generation/Judge calls,
settlement, and finalization.

The campaign is intentionally split into two freezes:

* campaign-plan.json freezes the matrix and every derivation/gating algorithm;
* derived-plan.json is written after the first complete E0 and freezes the
  Analyzer replay artifact, the type-7 p99-derived P0.5-06 values, and the
  authenticated offline-effect decisions before any dependent arm is launched.

The controller uses the production main runner for dry replay, the production
request-budget and compatibility helpers for request-visible projections, and
a separately hash-frozen terminal reporter.  Template ``TODO_FREEZE_*`` values
must be replaced before the live ``run`` command is accepted.
"""

from __future__ import annotations

import argparse
import ast
import copy
import ctypes
import fcntl
import hashlib
import importlib.util
import inspect
import io
import json
import math
import os
import random
import re
import stat
import subprocess
import sys
import tarfile
import textwrap
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PLAN_SCHEMA = "opensquilla.draco-p0-p05-campaign-plan/v1"
STATUS_SCHEMA = "opensquilla.draco-p0-p05-controller-status/v1"
ANALYZER_ARTIFACT_SCHEMA = "opensquilla.draco-frozen-task-analysis-source/v2"
FROZEN_TASK_ANALYSIS_SCHEMA_V1 = "opensquilla.draco.frozen-task-analysis/v1"
FROZEN_TASK_ANALYSIS_SCHEMA_V2 = "opensquilla.draco.frozen-task-analysis/v2"
FROZEN_TASK_ANALYSIS_SCHEMAS = frozenset(
    {FROZEN_TASK_ANALYSIS_SCHEMA_V1, FROZEN_TASK_ANALYSIS_SCHEMA_V2}
)
ANALYZER_SOURCE_POLICY_SCHEMA = "opensquilla.draco-analyzer-source-policy/v1"
PREEXISTING_SOURCE_SCHEMA = "opensquilla.draco-preexisting-analyzer-source/v1"
PREEXISTING_SOURCE_PACKAGE_SCHEMA = "opensquilla.draco-preexisting-analyzer-source-package/v1"
DERIVED_SCHEMA = "opensquilla.draco-p0-p05-derived-plan/v1"
CONFIRMATORY_SCHEDULE_SCHEMA = "opensquilla.draco-p0-p05-confirmatory-schedule/v1"
CONFIRMATORY_COHORT_MANIFEST_SCHEMA = (
    "opensquilla.draco-confirmatory-cohort-manifest/v1"
)
CONFIRMATORY_REPORT_INPUT_INDEX_SCHEMA = (
    "opensquilla.draco-confirmatory-report-input-index/v1"
)
CONFIRMATORY_BRIDGE_SCHEMA = "opensquilla.draco-p0-p05-confirmatory-bridge/v1"
CONFIRMATORY_BRIDGE_SOURCE_SCHEMA = (
    "opensquilla.draco-p0-p05-authenticated-screening-source/v1"
)
CONFIRMATORY_BRIDGE_DERIVED_SCHEMA = (
    "opensquilla.draco-p0-p05-confirmatory-bridge-derived/v1"
)
CONFIRMATORY_BRIDGE_ANALYZER_SCHEMA = (
    "opensquilla.draco-p0-p05-confirmatory-bridge-analyzer/v1"
)
CONFIRMATORY_BRIDGE_P99_SCHEMA = (
    "opensquilla.draco-p0-p05-confirmatory-bridge-analyzer-p99/v1"
)
CONFIRMATORY_BRIDGE_REPORT_SCHEMA = (
    "opensquilla.draco-p0-p05-confirmatory-bridge-report/v1"
)
TERMINAL_STATUS_INPUT_SCHEMA = "opensquilla.draco-p0-p05-terminal-status-input/v1"
TERMINAL_REPORT_RECEIPT_SCHEMA = "opensquilla.draco-p0-p05-terminal-report/v1"
SCREENING_REPORT_SCHEMA = "opensquilla.draco-p0-p05-comprehensive-report/v1"
RECEIPT_SCHEMA = "opensquilla.draco-offline-effect-receipt/v1"
AGGREGATOR_PROMPT_SCHEMA = "opensquilla.router-dynamic-aggregator-prompt/v1"
AGGREGATOR_PROMPT_VERSIONS = frozenset(
    {
        "aggregator-v1-current",
        "aggregator-v2-verify-first",
        "aggregator-v3-preserve-best",
    }
)
EXPECTED_TASK_COUNT = 10
CONFIRMATORY_TASK_CONCURRENCY = 6
CONFIRMATORY_ORDER_COUNT_PER_ROLE = EXPECTED_TASK_COUNT // 2
CONFIRMATORY_TRANCHE_SIZES = (6, 4)
MIN_ANALYZER_P99_LIVE_OBSERVATIONS = 8
EXPECTED_OFFLINE_UNIQUE_REPLAY_OVERLAYS = 57
SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
PLACEHOLDER_PREFIX = "TODO_"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PHYSICAL_ATTEMPT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
PRODUCTION_BUDGET_GATE_EXPERIMENTS = frozenset({"P0.5-10", "P0.5-38", "P0.5-39"})
REQUIRED_REPLICATE_ARMS = frozenset(
    {
        "common-E0-R1",
        "common-E0-R2",
        "common-E0-R3",
        "P0.5-11-E1-R1",
        "P0.5-11-E1-R2",
        "P0.5-11-E1-R3",
        "P0.5-36-E1-R1",
        "P0.5-36-E1-R2",
        "P0.5-36-E1-R3",
    }
)
ANALYZER_SOURCE_ARM_ID = "common-E0-source"
REPLAY_CONTROL_ARM_IDS = (
    "common-E0-R1",
    "common-E0-R2",
    "common-E0-R3",
)
LIVE_ANALYZER_CANDIDATE_ARM_IDS = (
    "P0-03-E1",
    "P0.5-05-E1",
    "P0.5-06-E1",
    "P0.5-06-E2",
)
REPLAY_TRANCHES = {
    "common-E0-R1": (
        "P0-20-E3",
        "P0-20-E2",
        "P0.5-11-E1-R1",
        "P0.5-36-E1-R1",
        "P0-12-E1",
        "P0-12-E2",
        "P0-12-E3",
        "P0-22-E1",
        "P0-22-E2",
        "P0-23-E1",
        "P0-23-E2",
        "P0-32-E1",
        "P0-32-E2",
        "P0-35-E1",
        "P0-35-E2",
        "P0.5-10-E1",
        "P0.5-10-E2",
        "P0.5-13-E1",
        "P0.5-13-E2",
        "P0.5-28-E1",
    ),
    "common-E0-R2": (
        "P0.5-11-E1-R2",
        "P0.5-36-E1-R2",
        "P0.5-14-E1",
        "P0.5-14-E2",
        "P0.5-16-E1",
        "P0.5-16-E2",
        "P0.5-17-E1",
        "P0.5-17-E2",
        "P0.5-18-E1",
        "P0.5-18-E2",
        "P0.5-19-E1",
        "P0.5-19-E2",
        "P0.5-21-E1",
        "P0.5-21-E2",
        "P0.5-24-E1",
        "P0.5-24-E2",
        "P0.5-25-E1",
        "P0.5-25-E2",
    ),
    "common-E0-R3": (
        "P0.5-11-E1-R3",
        "P0.5-36-E1-R3",
        "P0.5-26-E1",
        "P0.5-26-E2",
        "P0.5-27-E1",
        "P0.5-27-E2",
        "P0.5-29-E1",
        "P0.5-29-E2",
        "P0.5-30-E1",
        "P0.5-30-E2",
        "P0.5-33-E1",
        "P0.5-33-E2",
        "P0.5-34-E1",
        "P0.5-34-E2",
        "P0.5-37-E1",
        "P0.5-37-E2",
        "P0.5-38-E1",
        "P0.5-38-E2",
        "P0.5-39-E1",
        "P0.5-39-E2",
    ),
}
EXPECTED_SCHEDULE_ARM_ORDER = (
    ANALYZER_SOURCE_ARM_ID,
    *LIVE_ANALYZER_CANDIDATE_ARM_IDS,
    *(
        arm_id
        for anchor_id in REPLAY_CONTROL_ARM_IDS
        for arm_id in (anchor_id, *REPLAY_TRANCHES[anchor_id])
    ),
)
EXPECTED_SCHEDULE_ANCHORS = {
    ANALYZER_SOURCE_ARM_ID: ANALYZER_SOURCE_ARM_ID,
    **{arm_id: ANALYZER_SOURCE_ARM_ID for arm_id in LIVE_ANALYZER_CANDIDATE_ARM_IDS},
    **{
        arm_id: anchor_id
        for anchor_id in REPLAY_CONTROL_ARM_IDS
        for arm_id in (anchor_id, *REPLAY_TRANCHES[anchor_id])
    },
}
EXPECTED_ARM_CONTROL_OVERRIDES = {
    arm_id: anchor_id
    for arm_id, anchor_id in EXPECTED_SCHEDULE_ANCHORS.items()
    if arm_id not in {ANALYZER_SOURCE_ARM_ID, *REPLAY_CONTROL_ARM_IDS}
}
UINT64_MAX = (1 << 64) - 1
RUN_DECISIONS = frozenset(
    {
        "run",
        "run_required_replicate",
        "run_conservative_projection_uncertain",
        "run_conservative_unproven_budget_binding",
    }
)


class ControllerError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class BoundJson:
    """One JSON document parsed and hashed from one immutable byte snapshot."""

    path: Path
    payload: bytes
    file_sha256: str
    value: Any


def _open_absolute_nofollow(
    path: Path,
    *,
    flags: int = os.O_RDONLY,
    mode: int = 0o600,
    create_parents: bool = False,
) -> tuple[int, int, str]:
    """Open an absolute leaf through directory fds without following symlinks."""

    if not path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts[1:]):
        raise ControllerError(f"secure path must be canonical and absolute: {path}")
    directory_fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:-1]:
            if create_parents:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=directory_fd)
                except FileExistsError:
                    pass
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = next_fd
        leaf = path.parts[-1]
        fd = os.open(
            leaf,
            flags | getattr(os, "O_NOFOLLOW", 0),
            mode,
            dir_fd=directory_fd,
        )
        return fd, directory_fd, leaf
    except BaseException:
        os.close(directory_fd)
        raise


def _secure_existing_path(path: Path, *, directory: bool, label: str) -> Path:
    flags = os.O_RDONLY | (os.O_DIRECTORY if directory else 0)
    try:
        fd, parent_fd, _ = _open_absolute_nofollow(path, flags=flags)
    except OSError as exc:
        raise ControllerError(f"{label} is unavailable or traverses a symlink: {path}") from exc
    try:
        info = os.fstat(fd)
        expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        if not expected:
            raise ControllerError(f"{label} has the wrong file type: {path}")
        return path
    finally:
        os.close(fd)
        os.close(parent_fd)


def secure_existing_directory(path: Path, *, label: str) -> Path:
    return _secure_existing_path(path, directory=True, label=label)


def secure_future_absolute_path(path: Path, *, label: str) -> Path:
    """Reject symlinks in every existing parent of a future absolute path."""

    if not path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts[1:]):
        raise ControllerError(f"{label} must be canonical and absolute")
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise ControllerError(f"{label} path component is unavailable: {current}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise ControllerError(f"{label} must not traverse a symlink: {current}")
    return path


def secure_mkdir_absolute(path: Path) -> None:
    """Create an absolute directory hierarchy using mkdirat/openat no-follow steps."""

    if not path.is_absolute():
        raise ControllerError(f"secure directory must be absolute: {path}")
    sentinel = path / ".secure-directory-probe"
    try:
        _, parent_fd, _ = _open_absolute_nofollow(
            sentinel,
            flags=os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            create_parents=True,
        )
    except FileExistsError as exc:
        raise ControllerError(f"secure directory probe unexpectedly exists: {path}") from exc
    else:
        try:
            os.unlink(sentinel.name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)


def secure_atomic_write_bytes(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    """Atomically publish bytes through one no-follow parent directory fd."""

    temporary = f".{path.name}.tmp.{os.getpid()}.{hashlib.sha256(os.urandom(16)).hexdigest()[:12]}"
    probe = path.with_name(temporary)
    fd = parent_fd = -1
    try:
        fd, parent_fd, leaf = _open_absolute_nofollow(
            probe,
            flags=os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            mode=mode,
            create_parents=True,
        )
        with os.fdopen(fd, "wb", closefd=True) as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            existing = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and stat.S_ISLNK(existing.st_mode):
            raise ControllerError(f"refusing to replace symlink destination: {path}")
        os.replace(leaf, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    except BaseException:
        if parent_fd >= 0:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        raise
    finally:
        if fd >= 0:
            os.close(fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def secure_atomic_write_json(path: Path, value: Any, *, mode: int = 0o600) -> None:
    payload = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n"
    ).encode("utf-8")
    secure_atomic_write_bytes(path, payload, mode=mode)


def require_regular_file(path: Path) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ControllerError(f"expected regular non-symlink file: {path}")


def load_json(path: Path) -> Any:
    require_regular_file(path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ControllerError(f"cannot load JSON {path}: {exc}") from exc


def atomic_write_json(path: Path, value: Any, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    )
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(temporary, flags, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        finally:
            raise


def atomic_write_bytes(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    """Publish immutable evidence bytes without following the destination leaf."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(temporary, flags, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        finally:
            raise


def stable_regular_file_bytes(path: Path) -> bytes:
    """Read one non-symlink file from a single descriptor and reject mutation."""

    try:
        fd, parent_fd, _ = _open_absolute_nofollow(path)
    except OSError as exc:
        raise ControllerError(f"cannot open frozen evidence file {path}: {exc}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ControllerError(f"frozen evidence is not a regular file: {path}")
        chunks: list[bytes] = []
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        after = os.fstat(fd)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if identity_before != identity_after:
            raise ControllerError(f"frozen evidence changed while being read: {path}")
        payload = b"".join(chunks)
        if len(payload) != before.st_size:
            raise ControllerError(f"frozen evidence size changed while being read: {path}")
        return payload
    finally:
        os.close(fd)
        os.close(parent_fd)


def load_bound_json(path: Path, *, label: str) -> BoundJson:
    payload = stable_regular_file_bytes(path)
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControllerError(f"cannot load {label} JSON {path}: {exc}") from exc
    return BoundJson(
        path=path,
        payload=payload,
        file_sha256=hashlib.sha256(payload).hexdigest(),
        value=value,
    )


def regular_directory_tree_sha256(root: Path) -> str:
    """Hash every regular file in a controller-owned extracted snapshot."""

    rows: list[dict[str, Any]] = []
    for directory, directory_names, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        for name in [*directory_names, *filenames]:
            path = directory_path / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ControllerError(f"frozen snapshot package contains a symlink: {path}")
        for filename in sorted(filenames):
            path = directory_path / filename
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise ControllerError(
                    f"frozen snapshot package contains a non-regular file: {path}"
                )
            rows.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "mode": stat.S_IMODE(info.st_mode),
                    "size_bytes": info.st_size,
                    "sha256": file_sha256(path),
                }
            )
    return canonical_sha256(sorted(rows, key=lambda row: row["path"]))


def extract_regular_git_archive(payload: bytes, destination: Path) -> None:
    """Extract only ordinary files/directories from a git archive."""

    destination.mkdir(mode=0o700)
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
            members = archive.getmembers()
            for member in members:
                parts = Path(member.name).parts
                if (
                    not parts
                    or Path(member.name).is_absolute()
                    or any(part in {"", ".", ".."} for part in parts)
                    or not (member.isdir() or member.isreg())
                ):
                    raise ControllerError(
                        f"source snapshot git archive has an unsafe member: {member.name}"
                    )
                target = destination.joinpath(*parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                    continue
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise ControllerError(f"source snapshot git archive cannot read: {member.name}")
                mode = 0o700 if member.mode & 0o111 else 0o600
                atomic_write_bytes(target, extracted.read(), mode=mode)
    except (OSError, tarfile.TarError) as exc:
        raise ControllerError(f"cannot extract source snapshot git archive: {exc}") from exc


def run_text(command: Sequence[str], *, cwd: Path | None = None) -> str:
    result = subprocess.run(
        list(command),
        cwd=cwd,
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ControllerError(f"command failed ({result.returncode}): {detail}")
    return result.stdout.strip()


def git_identity(snapshot: Path) -> dict[str, str]:
    return {
        "commit": run_text(["git", "rev-parse", "HEAD"], cwd=snapshot),
        "tree": run_text(["git", "rev-parse", "HEAD^{tree}"], cwd=snapshot),
        "status": run_text(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=snapshot,
        ),
    }


def require_frozen_sha256(
    value: Any,
    *,
    label: str,
    allow_placeholders: bool,
) -> str:
    normalized = str(value or "").strip()
    if allow_placeholders and normalized.startswith(PLACEHOLDER_PREFIX):
        return normalized
    if SHA256_RE.fullmatch(normalized) is None:
        raise ControllerError(f"{label} must be one frozen lowercase SHA-256")
    return normalized


def require_frozen_text(
    value: Any,
    *,
    label: str,
    allow_placeholders: bool,
) -> str:
    normalized = str(value or "").strip()
    if allow_placeholders and normalized.startswith(PLACEHOLDER_PREFIX):
        return normalized
    if not normalized:
        raise ControllerError(f"{label} must be frozen")
    return normalized


def deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = copy.deepcopy(dict(base))
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def set_path(target: dict[str, Any], path: Sequence[str], value: Any) -> None:
    if not path or any(not isinstance(item, str) or not item for item in path):
        raise ControllerError(f"invalid JSON path: {path!r}")
    cursor = target
    for key in path[:-1]:
        existing = cursor.get(key)
        if existing is None:
            existing = {}
            cursor[key] = existing
        if not isinstance(existing, dict):
            raise ControllerError(f"JSON path crosses a scalar at {key!r}")
        cursor = existing
    cursor[path[-1]] = copy.deepcopy(value)


@dataclass(frozen=True)
class Arm:
    arm_id: str
    experiment_id: str
    directory_name: str
    variant: str
    replicate: int
    analyzer_mode: str
    override: dict[str, Any]
    dynamic: dict[str, Any] | None
    wire_gate: str | None
    output_name: str
    control_arm_id: str | None


def expand_arms(plan: Mapping[str, Any]) -> list[Arm]:
    run_id = str(plan["run_id"])
    controls = plan.get("comparison_controls")
    if isinstance(controls, Mapping):
        default_control_arm_id = str(controls.get("default_control_arm_id") or "common-E0-R1")
        raw_control_overrides = controls.get("arm_control_overrides")
        control_overrides = (
            dict(raw_control_overrides) if isinstance(raw_control_overrides, Mapping) else {}
        )
    else:
        default_control_arm_id = "common-E0-R1"
        control_overrides = {}
    arms: list[Arm] = []
    for row in plan["common_e0"]:
        arm_id = str(row["arm_id"])
        arms.append(
            Arm(
                arm_id=arm_id,
                experiment_id="common-E0",
                directory_name="common",
                variant=str(row["variant"]),
                replicate=int(row["replicate"]),
                analyzer_mode=str(row["analyzer_mode"]),
                override=copy.deepcopy(row.get("override") or {}),
                dynamic=None,
                wire_gate=None,
                output_name=f"{arm_id}-{run_id}",
                control_arm_id=None,
            )
        )
    for experiment in plan["experiments"]:
        experiment_id = str(experiment["id"])
        directory_name = str(experiment["directory_name"])
        for variant in experiment.get("variants", []):
            repetitions = int(variant.get("replicates", 1))
            base_override = variant.get("override") or {}
            if not isinstance(base_override, Mapping):
                raise ControllerError(f"{experiment_id} variant override must be an object")
            replicate_overrides = variant.get("replicate_overrides")
            if replicate_overrides is not None:
                if repetitions < 2:
                    raise ControllerError(
                        f"{experiment_id} replicate_overrides requires replicates >= 2"
                    )
                if not isinstance(replicate_overrides, list):
                    raise ControllerError(f"{experiment_id} replicate_overrides must be a list")
                if len(replicate_overrides) != repetitions:
                    raise ControllerError(
                        f"{experiment_id} replicate_overrides must match repetitions"
                    )
                if any(not isinstance(item, Mapping) for item in replicate_overrides):
                    raise ControllerError(
                        f"{experiment_id} replicate_overrides entries must be objects"
                    )
            for replicate in range(1, repetitions + 1):
                suffix = f"-R{replicate}" if repetitions > 1 else ""
                arm_id = f"{experiment_id}-{variant['id']}{suffix}"
                control_ids = experiment.get("control_arm_ids")
                if isinstance(control_ids, list) and repetitions > 1:
                    if len(control_ids) != repetitions:
                        raise ControllerError(
                            f"{experiment_id} control_arm_ids must match repetitions"
                        )
                    default_control = control_ids[replicate - 1]
                else:
                    default_control = experiment.get("control_arm_id", default_control_arm_id)
                control = control_overrides.get(
                    arm_id,
                    variant.get("control_arm_id") or default_control,
                )
                arm_override = copy.deepcopy(dict(base_override))
                if replicate_overrides is not None:
                    arm_override = deep_merge(
                        arm_override,
                        replicate_overrides[replicate - 1],
                    )
                arms.append(
                    Arm(
                        arm_id=arm_id,
                        experiment_id=experiment_id,
                        directory_name=directory_name,
                        variant=str(variant["id"]),
                        replicate=replicate,
                        analyzer_mode=str(variant.get("analyzer_mode", "frozen_replay")),
                        override=arm_override,
                        dynamic=copy.deepcopy(variant.get("dynamic")),
                        wire_gate=(
                            str(variant["wire_gate"])
                            if variant.get("wire_gate") is not None
                            else None
                        ),
                        output_name=f"{arm_id}-{run_id}",
                        control_arm_id=str(control) if control is not None else None,
                    )
                )
    return arms


def scheduled_arms(plan: Mapping[str, Any], arms: Sequence[Arm]) -> list[Arm]:
    execution = plan.get("execution")
    schedule = execution.get("schedule") if isinstance(execution, Mapping) else None
    if not isinstance(schedule, Mapping):
        raise ControllerError("execution.schedule is missing")
    if schedule.get("mode") != "anchored_serial":
        raise ControllerError("execution schedule mode must be anchored_serial")
    if schedule.get("strict_task_interleaving") is not False:
        raise ControllerError("execution schedule must freeze strict_task_interleaving=false")
    arm_order = schedule.get("arm_order")
    if not isinstance(arm_order, list) or any(not isinstance(item, str) for item in arm_order):
        raise ControllerError("execution schedule arm_order must be a string list")
    if tuple(arm_order) != EXPECTED_SCHEDULE_ARM_ORDER:
        raise ControllerError("execution schedule arm_order differs from the frozen matrix")
    anchors = schedule.get("anchor_by_arm_id")
    if not isinstance(anchors, Mapping) or dict(anchors) != EXPECTED_SCHEDULE_ANCHORS:
        raise ControllerError("execution schedule anchor mapping differs from the frozen matrix")
    by_id = {arm.arm_id: arm for arm in arms}
    if len(by_id) != len(arms) or set(by_id) != set(arm_order):
        raise ControllerError("execution schedule does not cover the exact expanded arm set")
    ordered = [by_id[arm_id] for arm_id in arm_order]
    current_anchor: str | None = None
    for arm in ordered:
        if arm.arm_id in {ANALYZER_SOURCE_ARM_ID, *REPLAY_CONTROL_ARM_IDS}:
            current_anchor = arm.arm_id
        if current_anchor is None or anchors.get(arm.arm_id) != current_anchor:
            raise ControllerError(f"{arm.arm_id} is not bound to its nearest schedule anchor")
        anchor = by_id[current_anchor]
        if arm.analyzer_mode != anchor.analyzer_mode:
            raise ControllerError(f"{arm.arm_id} Analyzer mode differs from schedule anchor")
    return ordered


def all_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from all_strings(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            yield from all_strings(item)


def analyzer_source_policy(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Return the explicit Analyzer-source policy; absence stays strict."""

    runtime = plan.get("runtime_contract")
    raw = runtime.get("analyzer_source") if isinstance(runtime, Mapping) else None
    if raw is None:
        return {
            "schema": ANALYZER_SOURCE_POLICY_SCHEMA,
            "allow_deterministic_router_fallback": False,
        }
    if (
        not isinstance(raw, Mapping)
        or set(raw) != {"schema", "allow_deterministic_router_fallback"}
        or raw.get("schema") != ANALYZER_SOURCE_POLICY_SCHEMA
        or type(raw.get("allow_deterministic_router_fallback")) is not bool
    ):
        raise ControllerError("runtime_contract.analyzer_source is invalid")
    return copy.deepcopy(dict(raw))


def frozen_replay_contract(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Return the exact replay projection promised by the frozen plan."""

    runtime = plan.get("runtime_contract")
    raw = runtime.get("frozen_replay") if isinstance(runtime, Mapping) else None
    fields = {
        "mode_path",
        "mode_value",
        "payload_path",
        "artifact_projection_key",
        "schema",
        "expected_physical_analyzer_requests",
    }
    if (
        not isinstance(raw, Mapping)
        or set(raw) != fields
        or raw.get("mode_path") != ["g1_routing", "task_analysis_execution", "mode"]
        or raw.get("mode_value") != "frozen_replay"
        or raw.get("payload_path") != ["g1_routing", "task_analysis_execution"]
        or raw.get("artifact_projection_key") != "replay_payload"
        or raw.get("schema") not in FROZEN_TASK_ANALYSIS_SCHEMAS
        or raw.get("expected_physical_analyzer_requests") != 0
    ):
        raise ControllerError("runtime_contract.frozen_replay is invalid")
    return copy.deepcopy(dict(raw))


def preexisting_source_contract(plan: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return an explicitly hash-bound source import, never an implicit path."""

    runtime = plan.get("runtime_contract")
    raw = runtime.get("preexisting_source") if isinstance(runtime, Mapping) else None
    if raw is None:
        return None
    fields = {
        "schema",
        "enabled",
        "source_plan_path",
        "source_plan_raw_sha256",
        "source_plan_canonical_sha256",
        "source_snapshot_path",
        "source_snapshot_commit",
        "source_snapshot_tree",
        "source_output_dir",
        "source_manifest_sha256",
        "source_results_sha256",
        "source_trace_sha256",
    }
    if (
        not isinstance(raw, Mapping)
        or set(raw) != fields
        or raw.get("schema") != PREEXISTING_SOURCE_SCHEMA
        or raw.get("enabled") is not True
    ):
        raise ControllerError("runtime_contract.preexisting_source is invalid")
    return copy.deepcopy(dict(raw))


def validate_plan(plan: Mapping[str, Any], *, allow_placeholders: bool) -> list[Arm]:
    if plan.get("schema") != PLAN_SCHEMA:
        raise ControllerError("campaign plan schema differs")
    if plan.get("benchmark", {}).get("task_count") != EXPECTED_TASK_COUNT:
        raise ControllerError("campaign must freeze exactly 10 DRACO-mini tasks")
    task_ids = plan.get("benchmark", {}).get("task_ids")
    if (
        not isinstance(task_ids, list)
        or len(task_ids) != EXPECTED_TASK_COUNT
        or any(not isinstance(task_id, str) or not task_id for task_id in task_ids)
        or len(set(task_ids)) != EXPECTED_TASK_COUNT
    ):
        raise ControllerError("benchmark must freeze ten unique non-empty task ids")
    if plan.get("benchmark", {}).get("groups") != ["G1"]:
        raise ControllerError("campaign must run G1 only")
    execution = plan.get("execution")
    if not isinstance(execution, Mapping):
        raise ControllerError("execution contract is missing")
    if execution.get("serial_arms") is not True:
        raise ControllerError("arms must be serialized")
    if execution.get("task_concurrency") != 6:
        raise ControllerError("task concurrency must be exactly 6")
    if execution.get("judge_concurrency") != 6:
        raise ControllerError("Judge concurrency must be exactly 6")
    if execution.get("generation_max_attempts") != 3:
        raise ControllerError("generation max attempts must be exactly 3")
    source_policy = analyzer_source_policy(plan)
    replay_contract = frozen_replay_contract(plan)
    if (
        source_policy["allow_deterministic_router_fallback"] is True
        and replay_contract["schema"] != FROZEN_TASK_ANALYSIS_SCHEMA_V2
    ):
        raise ControllerError(
            "deterministic Analyzer fallback replay requires frozen replay schema v2"
        )
    imported_source = preexisting_source_contract(plan)
    if imported_source is not None:
        for key in (
            "source_plan_path",
            "source_snapshot_path",
            "source_output_dir",
            "source_snapshot_commit",
            "source_snapshot_tree",
        ):
            require_frozen_text(
                imported_source.get(key),
                label=f"runtime_contract.preexisting_source.{key}",
                allow_placeholders=allow_placeholders,
            )
        for key in (
            "source_plan_raw_sha256",
            "source_plan_canonical_sha256",
            "source_manifest_sha256",
            "source_results_sha256",
            "source_trace_sha256",
        ):
            require_frozen_sha256(
                imported_source.get(key),
                label=f"runtime_contract.preexisting_source.{key}",
                allow_placeholders=allow_placeholders,
            )

    freeze = plan.get("freeze")
    if not isinstance(freeze, Mapping):
        raise ControllerError("freeze contract is missing")
    require_frozen_text(
        freeze.get("snapshot_commit"),
        label="freeze.snapshot_commit",
        allow_placeholders=allow_placeholders,
    )
    require_frozen_text(
        freeze.get("snapshot_tree"),
        label="freeze.snapshot_tree",
        allow_placeholders=allow_placeholders,
    )
    inputs = freeze.get("inputs")
    sources = freeze.get("sources")
    registry = freeze.get("model_registry")
    ranking = freeze.get("ranking_config")
    for label, section in (
        ("freeze.inputs", inputs),
        ("freeze.sources", sources),
        ("freeze.model_registry", registry),
        ("freeze.ranking_config", ranking),
    ):
        if not isinstance(section, Mapping):
            raise ControllerError(f"{label} is missing")
    assert isinstance(inputs, Mapping)
    assert isinstance(sources, Mapping)
    assert isinstance(registry, Mapping)
    assert isinstance(ranking, Mapping)
    for key in ("benchmark_input_raw_sha256", "reference_config_raw_sha256"):
        require_frozen_sha256(
            inputs.get(key),
            label=f"freeze.inputs.{key}",
            allow_placeholders=allow_placeholders,
        )
    for key in (
        "launcher_raw_sha256",
        "controller_raw_sha256",
        "reporter_raw_sha256",
        "main_runner_raw_sha256",
        "resume_runner_raw_sha256",
    ):
        require_frozen_sha256(
            sources.get(key),
            label=f"freeze.sources.{key}",
            allow_placeholders=allow_placeholders,
        )
    for key in (
        "raw_sha256",
        "full_canonical_sha256",
        "formal_canonical_sha256",
        "full_identities_sha256",
        "formal_identities_sha256",
    ):
        require_frozen_sha256(
            registry.get(key),
            label=f"freeze.model_registry.{key}",
            allow_placeholders=allow_placeholders,
        )
    for key in (
        "full_snapshot_version",
        "formal_snapshot_version",
    ):
        require_frozen_text(
            registry.get(key),
            label=f"freeze.model_registry.{key}",
            allow_placeholders=allow_placeholders,
        )
    for key in ("model_count", "full_model_count", "formal_model_count"):
        if registry.get(key) != 79:
            raise ControllerError(f"freeze.model_registry.{key} must contain exactly 79 models")
    for key in (
        "raw_sha256",
        "packaged_canonical_sha256",
        "formal_canonical_sha256",
    ):
        require_frozen_sha256(
            ranking.get(key),
            label=f"freeze.ranking_config.{key}",
            allow_placeholders=allow_placeholders,
        )
    for key in (
        "packaged_schema_version",
        "packaged_config_version",
        "formal_schema_version",
        "formal_config_version",
    ):
        require_frozen_text(
            ranking.get(key),
            label=f"freeze.ranking_config.{key}",
            allow_placeholders=allow_placeholders,
        )
    if freeze.get("ranking_thinking_assignment_enabled") is not False:
        raise ControllerError("this campaign freezes ranking thinking assignment OFF")

    reporting = plan.get("reporting")
    if not isinstance(reporting, Mapping):
        raise ControllerError("reporting contract is missing")
    terminal_hook = reporting.get("terminal_hook")
    if (
        not isinstance(terminal_hook, Mapping)
        or terminal_hook.get("enabled") is not True
        or terminal_hook.get("strict") is not True
        or terminal_hook.get("mode") != "frozen_python_reporter"
        or reporting.get("mini_is_diagnostic_only") is not True
        or reporting.get("automatic_winner_promotion") is not False
        or reporting.get("independent_safety_gate_available") is not False
    ):
        raise ControllerError("terminal reporting/promotion contract differs")
    require_frozen_text(
        plan.get("paths", {}).get("reporter"),
        label="paths.reporter",
        allow_placeholders=allow_placeholders,
    )

    if (
        inputs.get("benchmark_input_raw_sha256") != plan.get("benchmark", {}).get("input_sha256")
        and not allow_placeholders
    ):
        raise ControllerError("benchmark hash differs between plan and freeze contract")
    expected_exclusions = {"P0-01", "P0-02", "P0.5-31", "P0-15"}
    exclusions = {str(row.get("id")) for row in plan.get("excluded", [])}
    if exclusions != expected_exclusions:
        raise ControllerError(f"excluded experiment set differs: {sorted(exclusions)}")

    arms = expand_arms(plan)
    arm_ids = [arm.arm_id for arm in arms]
    if len(arm_ids) != len(set(arm_ids)):
        raise ControllerError("expanded arm ids are not unique")
    common_rows = plan.get("common_e0", [])
    if not isinstance(common_rows, list) or [row.get("arm_id") for row in common_rows] != [
        ANALYZER_SOURCE_ARM_ID,
        *REPLAY_CONTROL_ARM_IDS,
    ]:
        raise ControllerError("plan must contain the live source then three replay controls")
    if len(arms) != 66:
        raise ControllerError(f"expected 66 candidate live arms before gates, got {len(arms)}")
    experiment_ids = {arm.experiment_id for arm in arms if arm.experiment_id != "common-E0"}
    if len(experiment_ids) != 31:
        raise ControllerError(f"expected 31 live experiment groups, got {len(experiment_ids)}")
    by_id = {arm.arm_id: arm for arm in arms}
    source_arm = by_id.get(ANALYZER_SOURCE_ARM_ID)
    if (
        source_arm is None
        or source_arm.analyzer_mode != "live"
        or source_arm.override
        or source_arm.control_arm_id is not None
    ):
        raise ControllerError("common-E0-source must be the unmodified live Analyzer source")
    for control_arm_id in REPLAY_CONTROL_ARM_IDS:
        control_arm = by_id.get(control_arm_id)
        if (
            control_arm is None
            or control_arm.analyzer_mode != "frozen_replay"
            or control_arm.override
            or control_arm.control_arm_id is not None
        ):
            raise ControllerError(f"{control_arm_id} must be an unmodified replay control")
    controls = plan.get("comparison_controls")
    if not isinstance(controls, Mapping):
        raise ControllerError("comparison_controls is missing")
    if (
        controls.get("source_arm_id") != ANALYZER_SOURCE_ARM_ID
        or controls.get("live_control_arm_id") != ANALYZER_SOURCE_ARM_ID
        or controls.get("default_control_arm_id") != REPLAY_CONTROL_ARM_IDS[0]
        or controls.get("replay_control_arm_ids") != list(REPLAY_CONTROL_ARM_IDS)
        or controls.get("require_same_analyzer_mode") is not True
    ):
        raise ControllerError("comparison control identities or mode contract differs")
    raw_control_overrides = controls.get("arm_control_overrides")
    if (
        not isinstance(raw_control_overrides, Mapping)
        or dict(raw_control_overrides) != EXPECTED_ARM_CONTROL_OVERRIDES
    ):
        raise ControllerError("comparison arm-control mapping differs from the frozen matrix")
    for arm_id, expected_control_id in EXPECTED_ARM_CONTROL_OVERRIDES.items():
        arm = by_id[arm_id]
        control = by_id[expected_control_id]
        if arm.control_arm_id != expected_control_id:
            raise ControllerError(f"{arm_id} is not paired with its frozen control")
        if arm.analyzer_mode != control.analyzer_mode:
            raise ControllerError(f"{arm_id} Analyzer mode differs from its paired control")
    if set(LIVE_ANALYZER_CANDIDATE_ARM_IDS) != {
        arm.arm_id
        for arm in arms
        if arm.experiment_id != "common-E0" and arm.analyzer_mode == "live"
    }:
        raise ControllerError("live Analyzer candidate set differs from the frozen matrix")
    noop_rows = plan.get("no_op_experiments", [])
    if not isinstance(noop_rows, list):
        raise ControllerError("no_op_experiments must be a list")
    noop_ids = [str(row.get("id")) for row in noop_rows if isinstance(row, Mapping)]
    if len(noop_ids) != len(noop_rows) or len(noop_ids) != len(set(noop_ids)):
        raise ControllerError("predeclared no-op experiment ids must be unique")
    if "P0.5-07" not in noop_ids:
        raise ControllerError("P0.5-07 must remain a predeclared no-op experiment")
    temperature_noop = next(
        row for row in noop_rows if isinstance(row, Mapping) and row.get("id") == "P0.5-07"
    )
    if (
        temperature_noop.get("provider_kind") != "openrouter"
        or temperature_noop.get("model") != "anthropic/claude-opus-4.8"
        or temperature_noop.get("requested_values") != [0.0, 0.2]
        or temperature_noop.get("control_arm_id") != ANALYZER_SOURCE_ARM_ID
    ):
        raise ControllerError("P0.5-07 official-host wire gate contract differs")
    gated = {arm.experiment_id for arm in arms if arm.wire_gate}
    if gated != PRODUCTION_BUDGET_GATE_EXPERIMENTS:
        raise ControllerError(f"wire-effect gate set differs: {sorted(gated)}")
    offline_contract = plan.get("runtime_contract", {}).get("offline_effect")
    if not isinstance(offline_contract, Mapping):
        raise ControllerError("runtime_contract.offline_effect is missing")
    if offline_contract.get("runner_mode") != "main_runner_dry_run_frozen_replay":
        raise ControllerError("offline effect gate must use the production main runner")
    if set(offline_contract.get("production_budget_gate_experiments") or []) != set(
        PRODUCTION_BUDGET_GATE_EXPERIMENTS
    ):
        raise ControllerError("offline production-budget gate set differs")
    if set(offline_contract.get("required_replicate_arm_ids") or []) != set(
        REQUIRED_REPLICATE_ARMS
    ):
        raise ControllerError("offline required-replicate arm set differs")
    shuffle_arms = [by_id[f"P0.5-36-E1-R{replicate}"] for replicate in range(1, 4)]
    shuffle_seeds: list[int] = []
    for arm in shuffle_arms:
        ensemble = arm.override.get("ensemble")
        if not isinstance(ensemble, Mapping) or ensemble.get("shuffle_candidates") is not True:
            raise ControllerError(f"{arm.arm_id} must enable candidate shuffle")
        seed = ensemble.get("candidate_order_seed")
        if type(seed) is not int or not 0 <= seed <= UINT64_MAX:
            raise ControllerError(f"{arm.arm_id} candidate_order_seed must be uint64")
        shuffle_seeds.append(seed)
    if shuffle_seeds != [0, 1, 4] or len(set(shuffle_seeds)) != len(shuffle_seeds):
        raise ControllerError("P0.5-36 replicate seeds must be the unique frozen values 0/1/4")
    for arm in arms:
        if not SAFE_COMPONENT_RE.fullmatch(arm.arm_id):
            raise ControllerError(f"unsafe arm id: {arm.arm_id}")
        if not SAFE_COMPONENT_RE.fullmatch(arm.directory_name):
            raise ControllerError(f"unsafe directory name: {arm.directory_name}")
        if not SAFE_COMPONENT_RE.fullmatch(arm.output_name):
            raise ControllerError(f"unsafe output name: {arm.output_name}")
        if arm.analyzer_mode not in {"live", "frozen_replay"}:
            raise ControllerError(f"unknown analyzer mode for {arm.arm_id}")
    if not allow_placeholders:
        placeholders = sorted(
            {item for item in all_strings(plan) if item.startswith(PLACEHOLDER_PREFIX)}
        )
        if placeholders:
            raise ControllerError(
                "unresolved plan placeholders prevent live execution: " + ", ".join(placeholders)
            )
    if plan.get("confirmatory_bridge") is not None:
        validate_confirmatory_bridge_contract(plan, scheduled_arms(plan, arms))
    return scheduled_arms(plan, arms)


def _bridge_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _bridge_median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2.0


def _verify_prefixed_document_hash(
    document: Mapping[str, Any], *, field: str, label: str
) -> None:
    payload = dict(document)
    recorded = payload.pop(field, None)
    if recorded != "sha256:" + canonical_sha256(payload):
        raise ControllerError(f"{label} self-hash differs")


def _bridge_artifact_hashes(root: Path, *, label: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in (
        "manifest.json",
        "results.jsonl",
        "trace.jsonl",
        "audit.json",
        "openrouter-non-byok-campaign-proof.json",
    ):
        path = root / name
        result[name] = hashlib.sha256(stable_regular_file_bytes(path)).hexdigest()
    if len(result) != 5:
        raise ControllerError(f"{label} formal artifact set is incomplete")
    return result


def _bridge_archived_source_hashes(root: Path, *, label: str) -> dict[str, str]:
    """Reinspect every archive file declared by the authenticated root manifest."""

    manifest_bound = load_bound_json(root / "manifest.json", label=f"{label} manifest")
    manifest = manifest_bound.value
    if not isinstance(manifest, Mapping):
        raise ControllerError(f"{label} manifest is not an object")
    sources = manifest.get("source_manifests")
    if not isinstance(sources, list) or not sources:
        raise ControllerError(f"{label} source manifest inventory is missing")
    archive_root = root / "archive"
    result: dict[str, str] = {}
    for index, source in enumerate(sources):
        if not isinstance(source, Mapping):
            raise ControllerError(f"{label} source manifest {index} is malformed")
        for path_field, hash_field, kind in (
            ("path", "sha256", "manifest"),
            ("result_path", "result_sha256", "result"),
        ):
            path = absolute_path_without_symlinks(
                source.get(path_field), label=f"{label} archived {kind} {index}"
            )
            try:
                path.relative_to(archive_root)
            except ValueError as exc:
                raise ControllerError(
                    f"{label} archived {kind} {index} escapes archive root"
                ) from exc
            payload = stable_regular_file_bytes(path)
            actual = hashlib.sha256(payload).hexdigest()
            if source.get(hash_field) != actual or str(path) in result:
                raise ControllerError(
                    f"{label} archived {kind} {index} hash/path differs"
                )
            result[str(path)] = actual
    return result


def _account_window_identity_hashes(root: Path, *, label: str) -> list[str]:
    """Derive canonical per-window identities from the formal manifest."""

    manifest = load_bound_json(root / "manifest.json", label=f"{label} manifest").value
    attribution = (
        manifest.get("cost_attribution")
        if isinstance(manifest, Mapping)
        and isinstance(manifest.get("cost_attribution"), Mapping)
        else None
    )
    windows = attribution.get("account_windows") if attribution is not None else None
    if not isinstance(windows, list) or not windows:
        raise ControllerError(f"{label} account-window identity evidence is missing")
    expected = {
        "account_before",
        "account_after",
        "account_reconciliation",
        "runtime_environment",
    }
    identities: list[str] = []
    for index, window in enumerate(windows):
        source_hashes = (
            window.get("source_sha256") if isinstance(window, Mapping) else None
        )
        if (
            not isinstance(source_hashes, Mapping)
            or set(source_hashes) != expected
            or any(
                SHA256_RE.fullmatch(str(value or "")) is None
                for value in source_hashes.values()
            )
        ):
            raise ControllerError(
                f"{label} account-window {index} identity evidence differs"
            )
        projection = {
            "account_before_sha256": source_hashes["account_before"],
            "account_after_sha256": source_hashes["account_after"],
            "account_reconciliation_sha256": source_hashes[
                "account_reconciliation"
            ],
            "runtime_environment_sha256": source_hashes["runtime_environment"],
        }
        identities.append(canonical_sha256(projection))
    if len(identities) != len(set(identities)):
        raise ControllerError(f"{label} repeats one account-window identity")
    return sorted(identities)


def _screening_cost_identity_scope(
    report: Mapping[str, Any],
) -> tuple[list[str], list[str]]:
    """Bind account identities to every formal arm included in report costs."""

    report_arms = report.get("arms")
    if not isinstance(report_arms, Mapping):
        raise ControllerError("screening report arm inventory is missing")
    costed_arm_ids = sorted(
        str(arm_id)
        for arm_id, report_arm in report_arms.items()
        if isinstance(report_arm, Mapping)
        and report_arm.get("formal_evidence_valid") is True
        and bool(report_arm.get("rows"))
    )
    account_identity_owners: dict[str, str] = {}
    for arm_id in costed_arm_ids:
        report_arm = report_arms[arm_id]
        root = absolute_path_without_symlinks(
            report_arm.get("output_dir"),
            label=f"screening costed arm {arm_id} output directory",
        )
        if not root.is_dir():
            raise ControllerError(
                f"screening costed arm {arm_id} output root is not a directory"
            )
        for identity in _account_window_identity_hashes(root, label=arm_id):
            prior_owner = account_identity_owners.get(identity)
            if prior_owner is not None and prior_owner != arm_id:
                raise ControllerError(
                    "screening costed arms reuse one account-window identity: "
                    f"{prior_owner!r} and {arm_id!r}"
                )
            account_identity_owners[identity] = arm_id
    return costed_arm_ids, sorted(account_identity_owners)


def _completion_artifact_hashes(value: Any, *, label: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ControllerError(f"{label} completion evidence is missing")
    hashes = value.get("artifact_sha256")
    expected_keys = {"manifest", "results", "trace", "audit", "proof"}
    if (
        not isinstance(hashes, Mapping)
        or set(hashes) != expected_keys
        or any(SHA256_RE.fullmatch(str(item or "")) is None for item in hashes.values())
    ):
        raise ControllerError(f"{label} completion artifact hash set differs")
    return {str(key): str(item) for key, item in hashes.items()}


def _assert_formal_artifact_evidence_closure(
    *,
    arm_id: str,
    actual: Mapping[str, str],
    status_arm: Mapping[str, Any],
    report_arm: Mapping[str, Any],
) -> None:
    expected_actual = {
        "manifest": actual["manifest.json"],
        "results": actual["results.jsonl"],
        "trace": actual["trace.jsonl"],
        "audit": actual["audit.json"],
        "proof": actual["openrouter-non-byok-campaign-proof.json"],
    }
    declared = {
        "terminal status": _completion_artifact_hashes(
            status_arm.get("completion_evidence"), label=f"{arm_id} terminal status"
        ),
        "report completion": _completion_artifact_hashes(
            report_arm.get("completion_evidence"), label=f"{arm_id} report"
        ),
        "report controller reinspection": _completion_artifact_hashes(
            (
                report_arm.get("controller_reinspection", {}).get("evidence")
                if isinstance(report_arm.get("controller_reinspection"), Mapping)
                else None
            ),
            label=f"{arm_id} report controller reinspection",
        ),
    }
    for label, hashes in declared.items():
        if hashes != expected_actual:
            raise ControllerError(
                f"{arm_id} formal artifact hashes differ from {label}"
            )


def _assert_group_report_artifact_closure(
    *,
    experiment_id: str,
    group_root: Path,
    report: Mapping[str, Any],
) -> dict[str, Any]:
    declared_groups = report.get("group_report_artifacts")
    declared_group = (
        declared_groups.get(experiment_id)
        if isinstance(declared_groups, Mapping)
        else None
    )
    if not isinstance(declared_group, Mapping) or set(declared_group) != {
        "markdown",
        "json",
    }:
        raise ControllerError(
            f"screening group report artifacts are missing for {experiment_id}"
        )
    group_reports: dict[str, Any] = {}
    for name, descriptor_key in (
        ("EXPERIMENT_RESULTS.md", "markdown"),
        ("EXPERIMENT_RESULTS.json", "json"),
    ):
        path = group_root / name
        payload = stable_regular_file_bytes(path)
        actual = {
            "path": str(path),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
        }
        if dict(declared_group[descriptor_key]) != actual:
            raise ControllerError(
                f"screening group report {name} differs from comprehensive report"
            )
        group_reports[name] = actual
    return group_reports


def _assert_screening_derived_evidence_closure(
    *,
    status: Mapping[str, Any],
    report: Mapping[str, Any],
    derived_path: Path,
    derived: Mapping[str, Any],
    analyzer_descriptor: Mapping[str, Any],
    p99: Mapping[str, Any],
) -> None:
    status_derived = (
        status.get("derived_plan")
        if isinstance(status.get("derived_plan"), Mapping)
        else None
    )
    report_derived = (
        report.get("derived") if isinstance(report.get("derived"), Mapping) else None
    )
    report_paths = report.get("paths") if isinstance(report.get("paths"), Mapping) else {}
    if (
        not isinstance(status_derived, Mapping)
        or Path(str(status_derived.get("path") or "")) != derived_path
        or str(status_derived.get("sha256") or "").removeprefix("sha256:")
        != str(derived.get("derived_plan_sha256") or "").removeprefix("sha256:")
        or not isinstance(report_derived, Mapping)
        or report_derived.get("valid") is not True
        or report_derived.get("derived_plan_sha256")
        != derived.get("derived_plan_sha256")
        or report_derived.get("p0_5_06") != p99
        or report_derived.get("frozen_analyzer_artifact") != analyzer_descriptor
        or Path(str(report_paths.get("derived_plan") or "")) != derived_path
    ):
        raise ControllerError(
            "screening derived/Analyzer/p99 evidence is not jointly bound to "
            "terminal status and report"
        )


def _bridge_arm_completion_gate(
    *,
    arm: Arm,
    control: Arm,
    source_plan: Mapping[str, Any],
    terminal_status: Mapping[str, Any],
    report: Mapping[str, Any],
) -> dict[str, Any]:
    report_arms = report.get("arms")
    status_arms = terminal_status.get("arms")
    if not isinstance(report_arms, Mapping) or not isinstance(status_arms, Mapping):
        raise ControllerError("screening report/status arm maps are missing")
    arm_report = report_arms.get(arm.arm_id)
    control_report = report_arms.get(control.arm_id)
    arm_status = status_arms.get(arm.arm_id)
    control_status = status_arms.get(control.arm_id)
    if any(
        not isinstance(value, Mapping)
        for value in (arm_report, control_report, arm_status, control_status)
    ):
        raise ControllerError(f"screening pair evidence is missing for {arm.arm_id}")
    assert isinstance(arm_report, Mapping)
    assert isinstance(control_report, Mapping)
    assert isinstance(arm_status, Mapping)
    assert isinstance(control_status, Mapping)
    reasons: list[str] = []
    for label, state, arm_value in (
        ("candidate", arm_status, arm_report),
        ("control", control_status, control_report),
    ):
        if state.get("state") != "succeeded":
            reasons.append(f"{label}_terminal_state_not_succeeded")
        if arm_value.get("state") != "succeeded":
            reasons.append(f"{label}_report_state_not_succeeded")
        if arm_value.get("formal_evidence_valid") is not True:
            reasons.append(f"{label}_formal_evidence_invalid")
        statuses = arm_value.get("statuses")
        if not isinstance(statuses, Mapping) or statuses.get("execution") != "pass":
            reasons.append(f"{label}_execution_not_pass")
        metrics = arm_value.get("metrics")
        if not isinstance(metrics, Mapping):
            reasons.append(f"{label}_metrics_missing")
            continue
        if metrics.get("row_count") != EXPECTED_TASK_COUNT:
            reasons.append(f"{label}_row_count_not_10")
        if metrics.get("done_count") != EXPECTED_TASK_COUNT:
            reasons.append(f"{label}_done_count_not_10")
        if metrics.get("execution_success_count") != EXPECTED_TASK_COUNT:
            reasons.append(f"{label}_execution_success_count_not_10")
        if metrics.get("judge_complete_count") != EXPECTED_TASK_COUNT:
            reasons.append(f"{label}_judge_complete_count_not_10")
    candidate_metrics = arm_report.get("metrics")
    control_metrics = control_report.get("metrics")
    candidate_judge_errors = (
        int(candidate_metrics.get("judge_error_count") or 0)
        if isinstance(candidate_metrics, Mapping)
        else 0
    )
    control_judge_errors = (
        int(control_metrics.get("judge_error_count") or 0)
        if isinstance(control_metrics, Mapping)
        else 0
    )
    if candidate_judge_errors > control_judge_errors:
        reasons.append("judge_errors_increased")
    expected_candidate_dir = output_dir(source_plan, arm).resolve()
    expected_control_dir = output_dir(source_plan, control).resolve()
    for label, expected_dir, status_value, report_value in (
        ("candidate", expected_candidate_dir, arm_status, arm_report),
        ("control", expected_control_dir, control_status, control_report),
    ):
        declared_status_dir = Path(str(status_value.get("output_dir") or ""))
        declared_report_dir = Path(str(report_value.get("output_dir") or ""))
        if (
            not declared_status_dir.is_absolute()
            or not declared_report_dir.is_absolute()
            or declared_status_dir.resolve() != expected_dir
            or declared_report_dir.resolve() != expected_dir
        ):
            reasons.append(f"{label}_output_dir_binding_differs")
    return {
        "pass": not reasons,
        "reasons": reasons,
        "candidate_done_count": (
            candidate_metrics.get("done_count")
            if isinstance(candidate_metrics, Mapping)
            else None
        ),
        "control_done_count": (
            control_metrics.get("done_count")
            if isinstance(control_metrics, Mapping)
            else None
        ),
        "candidate_judge_error_count": candidate_judge_errors,
        "control_judge_error_count": control_judge_errors,
        "candidate_output_dir": str(expected_candidate_dir),
        "control_output_dir": str(expected_control_dir),
    }


def _bridge_comparison_for_arm(
    report: Mapping[str, Any], arm: Arm, control: Arm
) -> Mapping[str, Any]:
    experiments = report.get("experiments")
    experiment = experiments.get(arm.experiment_id) if isinstance(experiments, Mapping) else None
    comparisons = experiment.get("comparisons") if isinstance(experiment, Mapping) else None
    matches = [
        value
        for value in comparisons or []
        if isinstance(value, Mapping)
        and value.get("variant_arm_id") == arm.arm_id
        and value.get("control_arm_id") == control.arm_id
    ]
    if len(matches) != 1:
        raise ControllerError(f"screening comparison is not unique for {arm.arm_id}")
    return matches[0]


def _bridge_quality_projection(
    *,
    task_deltas: Mapping[str, float],
    label: str,
) -> dict[str, Any]:
    if len(task_deltas) != EXPECTED_TASK_COUNT:
        raise ControllerError(f"{label} does not cover all ten paired tasks")
    deltas = [float(task_deltas[task_id]) for task_id in sorted(task_deltas)]
    mean_delta = sum(deltas) / len(deltas)
    median_delta = _bridge_median(deltas)
    wins = sum(value > 1e-12 for value in deltas)
    ties = sum(abs(value) <= 1e-12 for value in deltas)
    losses = sum(value < -1e-12 for value in deltas)
    minimum = min(deltas)
    gates = {
        "mean_delta_nonnegative": mean_delta >= -1e-12,
        "median_delta_nonnegative": median_delta is not None and median_delta >= -1e-12,
        "wins_not_less_than_losses": wins >= losses,
        "no_task_delta_below_minus_10": minimum >= -10.0 - 1e-12,
    }
    return {
        "task_count": len(deltas),
        "task_deltas": {task_id: task_deltas[task_id] for task_id in sorted(task_deltas)},
        "mean_delta_quality": mean_delta,
        "median_delta_quality": median_delta,
        "wins": wins,
        "ties": ties,
        "losses": losses,
        "minimum_task_delta_quality": minimum,
        "gates": gates,
        "pass": all(gates.values()),
    }


def _bridge_individual_quality_gate(
    report: Mapping[str, Any], arm: Arm, control: Arm
) -> dict[str, Any]:
    comparison = _bridge_comparison_for_arm(report, arm, control)
    rows = comparison.get("task_rows")
    if not isinstance(rows, list) or len(rows) != EXPECTED_TASK_COUNT:
        raise ControllerError(f"screening comparison rows are incomplete for {arm.arm_id}")
    task_deltas: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ControllerError(f"screening comparison row is malformed for {arm.arm_id}")
        task_id = str(row.get("task_id") or "")
        delta = _bridge_number(row.get("delta_quality"))
        control_quality = _bridge_number(row.get("control_quality"))
        variant_quality = _bridge_number(row.get("variant_quality"))
        if (
            not task_id
            or task_id in task_deltas
            or delta is None
            or control_quality is None
            or variant_quality is None
            or not math.isclose(variant_quality - control_quality, delta, abs_tol=1e-9)
        ):
            raise ControllerError(f"screening comparison row arithmetic differs for {arm.arm_id}")
        task_deltas[task_id] = delta
    projected = _bridge_quality_projection(task_deltas=task_deltas, label=arm.arm_id)
    if (
        comparison.get("pair_count") != EXPECTED_TASK_COUNT
        or comparison.get("complete_task_id_pairing") is not True
        or _bridge_number(comparison.get("mean_delta_quality")) is None
        or not math.isclose(
            float(comparison["mean_delta_quality"]),
            float(projected["mean_delta_quality"]),
            abs_tol=1e-9,
        )
        or comparison.get("wins") != projected["wins"]
        or comparison.get("ties") != projected["ties"]
        or comparison.get("losses") != projected["losses"]
    ):
        raise ControllerError(f"screening comparison summary differs for {arm.arm_id}")
    return projected


def _bridge_candidate_groups(arms: Sequence[Arm]) -> dict[tuple[str, str], list[Arm]]:
    groups: dict[tuple[str, str], list[Arm]] = {}
    for arm in arms:
        if arm.experiment_id == "common-E0" or arm.control_arm_id is None:
            continue
        groups.setdefault((arm.experiment_id, arm.variant), []).append(arm)
    for members in groups.values():
        members.sort(key=lambda value: (value.replicate, value.arm_id))
    return groups


def _bridge_group_gate(
    *,
    members: Sequence[Arm],
    by_id: Mapping[str, Arm],
    source_plan: Mapping[str, Any],
    terminal_status: Mapping[str, Any],
    report: Mapping[str, Any],
) -> dict[str, Any]:
    completion: dict[str, Any] = {}
    individual: dict[str, Any] = {}
    for arm in members:
        control = by_id.get(str(arm.control_arm_id or ""))
        if control is None:
            raise ControllerError(f"screening control is missing for {arm.arm_id}")
        completion[arm.arm_id] = _bridge_arm_completion_gate(
            arm=arm,
            control=control,
            source_plan=source_plan,
            terminal_status=terminal_status,
            report=report,
        )
        individual[arm.arm_id] = _bridge_individual_quality_gate(report, arm, control)
    if len(members) == 1:
        quality = copy.deepcopy(individual[members[0].arm_id])
        quality_basis = "individual_task_pairing"
    else:
        task_sets = [set(value["task_deltas"]) for value in individual.values()]
        if not task_sets or any(task_set != task_sets[0] for task_set in task_sets[1:]):
            raise ControllerError("replicate screening task coverage differs")
        averaged = {
            task_id: sum(
                float(individual[arm.arm_id]["task_deltas"][task_id]) for arm in members
            )
            / len(members)
            for task_id in sorted(task_sets[0])
        }
        quality = _bridge_quality_projection(
            task_deltas=averaged,
            label=f"{members[0].experiment_id}-{members[0].variant}-replicate-mean",
        )
        experiments = report.get("experiments")
        experiment = (
            experiments.get(members[0].experiment_id)
            if isinstance(experiments, Mapping)
            else None
        )
        repeated = experiment.get("repeated_pairing") if isinstance(experiment, Mapping) else None
        if not isinstance(repeated, Mapping):
            raise ControllerError("replicate screening report lacks repeated_pairing")
        recorded = repeated.get("per_task_mean_delta")
        if (
            repeated.get("replicate_count") != len(members)
            or repeated.get("task_count") != EXPECTED_TASK_COUNT
            or repeated.get("complete_task_id_pairing") is not True
            or not isinstance(recorded, Mapping)
            or set(recorded) != set(averaged)
            or any(
                _bridge_number(recorded.get(task_id)) is None
                or not math.isclose(float(recorded[task_id]), averaged[task_id], abs_tol=1e-9)
                for task_id in averaged
            )
            or _bridge_number(repeated.get("mean_delta_quality")) is None
            or not math.isclose(
                float(repeated["mean_delta_quality"]),
                float(quality["mean_delta_quality"]),
                abs_tol=1e-9,
            )
            or repeated.get("wins") != quality["wins"]
            or repeated.get("ties") != quality["ties"]
            or repeated.get("losses") != quality["losses"]
        ):
            raise ControllerError("replicate screening summary differs from task rows")
        quality_basis = "replicate_task_mean_pairing"
    operational_pass = all(value["pass"] for value in completion.values())
    return {
        "experiment_id": members[0].experiment_id,
        "variant": members[0].variant,
        "candidate_arm_ids": [arm.arm_id for arm in members],
        "quality_basis": quality_basis,
        "quality": quality,
        "completion": completion,
        "individual_quality": individual,
        "operational_pass": operational_pass,
        "pass": operational_pass and quality["pass"] is True,
    }


def _bridge_report_completion_is_valid(report: Mapping[str, Any]) -> bool:
    completion = report.get("completion")
    schedule = report.get("schedule_evidence")
    return bool(
        report.get("schema") == SCREENING_REPORT_SCHEMA
        and report.get("controller_phase") == "succeeded"
        and isinstance(completion, Mapping)
        and completion.get("status") == "complete"
        and completion.get("controller_terminal") is True
        and completion.get("all_arms_terminal") is True
        and completion.get("formal_evidence_valid") is True
        and completion.get("derived_evidence_valid") is True
        and completion.get("schedule_evidence_valid") is True
        and completion.get("comparison_evidence_valid") is True
        and completion.get("confirmatory_evidence_valid") is True
        and isinstance(schedule, Mapping)
        and schedule.get("valid") is True
        and schedule.get("strict_task_interleaving") is False
    )


def authenticate_confirmatory_bridge_screening_source(
    *,
    source_plan_path: Path,
    terminal_status_path: Path,
    terminal_report_receipt_path: Path,
    requested_survivor_arm_ids: Sequence[str] | None,
) -> dict[str, Any]:
    """Authenticate one terminal screening campaign and recompute survivor gates."""

    bound_inputs: dict[str, BoundJson] = {}
    for path, label in (
        (source_plan_path, "screening plan"),
        (terminal_status_path, "screening terminal status"),
        (terminal_report_receipt_path, "screening terminal report receipt"),
    ):
        if not path.is_absolute():
            raise ControllerError(f"{label} path must be absolute")
        absolute_path_without_symlinks(path, label=label)
        bound_inputs[label] = load_bound_json(path, label=label)
    source_plan_bound = bound_inputs["screening plan"]
    status_bound = bound_inputs["screening terminal status"]
    receipt_bound = bound_inputs["screening terminal report receipt"]
    source_plan = source_plan_bound.value
    if not isinstance(source_plan, Mapping):
        raise ControllerError("screening plan is not an object")
    if source_plan.get("confirmatory_bridge") is not None:
        raise ControllerError("a confirmatory bridge cannot import another bridge")
    arms = validate_plan(source_plan, allow_placeholders=False)
    source_plan_sha = canonical_sha256(source_plan)
    source_run_root = Path(str(source_plan.get("paths", {}).get("run_root") or "")).resolve()
    if source_plan_path.resolve() != source_run_root / "campaign-plan.json":
        raise ControllerError("screening plan is not the frozen run_root/campaign-plan.json")
    if terminal_status_path.resolve() != source_run_root / "terminal-status-input.json":
        raise ControllerError("screening terminal status is outside its fixed run root")
    if terminal_report_receipt_path.resolve() != source_run_root / "terminal-report-receipt.json":
        raise ControllerError("screening terminal report receipt is outside its fixed run root")
    status = status_bound.value
    if not isinstance(status, Mapping):
        raise ControllerError("screening terminal status is not an object")
    if status.get("schema") != STATUS_SCHEMA:
        raise ControllerError("screening terminal status schema differs")
    verify_bare_document_self_hash(
        status,
        field="terminal_status_input_sha256",
        label="screening terminal status",
    )
    terminal_freeze = status.get("terminal_freeze")
    if (
        status.get("phase") != "succeeded"
        or status.get("campaign_plan_sha256") != source_plan_sha
        or status.get("run_id") != source_plan.get("run_id")
        or not isinstance(terminal_freeze, Mapping)
        or terminal_freeze.get("schema") != TERMINAL_STATUS_INPUT_SCHEMA
        or terminal_freeze.get("campaign_plan_sha256") != source_plan_sha
        or terminal_freeze.get("run_id") != source_plan.get("run_id")
        or terminal_freeze.get("phase") != "succeeded"
    ):
        raise ControllerError("screening terminal status is not a bound successful terminal input")
    receipt = receipt_bound.value
    if not isinstance(receipt, Mapping):
        raise ControllerError("screening terminal report receipt is not an object")
    if receipt.get("schema") != TERMINAL_REPORT_RECEIPT_SCHEMA:
        raise ControllerError("screening terminal report receipt schema differs")
    verify_bare_document_self_hash(
        receipt,
        field="receipt_sha256",
        label="screening terminal report receipt",
    )
    if (
        receipt.get("status") != "complete"
        or receipt.get("returncode") != 0
        or receipt.get("strict") is not True
        or receipt.get("terminal_status_input_path") != str(terminal_status_path.resolve())
        or receipt.get("terminal_status_input_file_sha256") != status_bound.file_sha256
        or receipt.get("reporter_raw_sha256")
        != source_plan.get("freeze", {}).get("sources", {}).get("reporter_raw_sha256")
        or Path(str(receipt.get("reporter_path") or "")).resolve()
        != Path(str(source_plan.get("paths", {}).get("reporter") or "")).resolve()
    ):
        raise ControllerError("screening terminal report receipt is incomplete or unbound")
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, Mapping) or set(artifacts) != {
        "EXPERIMENT_RESULTS.md",
        "EXPERIMENT_RESULTS.json",
    }:
        raise ControllerError("screening terminal report artifact set differs")
    report_paths: dict[str, Path] = {}
    report_artifacts: dict[str, Any] = {}
    source_report_root = Path(str(source_plan.get("paths", {}).get("report_root") or ""))
    for name in ("EXPERIMENT_RESULTS.md", "EXPERIMENT_RESULTS.json"):
        descriptor = artifacts.get(name)
        if not isinstance(descriptor, Mapping):
            raise ControllerError(f"screening report descriptor is missing for {name}")
        path = absolute_path_without_symlinks(descriptor.get("path"), label=name)
        if path != source_report_root.resolve() / name:
            raise ControllerError(f"screening report {name} path differs from the plan")
        payload = stable_regular_file_bytes(path)
        payload_sha = hashlib.sha256(payload).hexdigest()
        if (
            descriptor.get("sha256") != payload_sha
            or descriptor.get("size_bytes") != len(payload)
        ):
            raise ControllerError(f"screening report {name} raw hash differs")
        report_paths[name] = path
        report_artifacts[name] = {
            "path": str(path),
            "sha256": payload_sha,
            "size_bytes": len(payload),
            "payload": payload,
        }
    try:
        report = json.loads(report_artifacts["EXPERIMENT_RESULTS.json"].pop("payload"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControllerError("cannot parse screening comprehensive report") from exc
    report_artifacts["EXPERIMENT_RESULTS.md"].pop("payload")
    if not isinstance(report, Mapping):
        raise ControllerError("screening comprehensive report is not an object")
    _verify_prefixed_document_hash(report, field="report_sha256", label="screening report")
    if (
        report.get("campaign_plan_sha256") != source_plan_sha
        or report.get("campaign_plan_file_sha256") != source_plan_bound.file_sha256
        or report.get("terminal_status_input_file_sha256") != status_bound.file_sha256
        or report.get("terminal_status_input_sha256")
        != status.get("terminal_status_input_sha256")
        or not _bridge_report_completion_is_valid(report)
    ):
        raise ControllerError("screening comprehensive report is incomplete or unbound")

    by_id = {arm.arm_id: arm for arm in arms}
    groups = _bridge_candidate_groups(arms)
    group_gates: dict[tuple[str, str], dict[str, Any]] = {}
    for key, members in groups.items():
        try:
            group_gates[key] = _bridge_group_gate(
                members=members,
                by_id=by_id,
                source_plan=source_plan,
                terminal_status=status,
                report=report,
            )
        except ControllerError as exc:
            group_gates[key] = {
                "experiment_id": key[0],
                "variant": key[1],
                "candidate_arm_ids": [member.arm_id for member in members],
                "pass": False,
                "operational_pass": False,
                "error": str(exc),
            }
    passing_ids = sorted(
        arm_id
        for gate in group_gates.values()
        if gate.get("pass") is True
        for arm_id in gate["candidate_arm_ids"]
    )
    if requested_survivor_arm_ids is None:
        survivors = passing_ids
        selection_mode = "all_recomputed_gate_passing"
    else:
        requested = {str(value) for value in requested_survivor_arm_ids}
        if not requested:
            raise ControllerError("explicit survivor set must not be empty")
        unknown = requested - set(by_id)
        if unknown:
            raise ControllerError("unknown screening survivor arms: " + ", ".join(sorted(unknown)))
        expanded = set(requested)
        for members in groups.values():
            member_ids = {member.arm_id for member in members}
            if requested & member_ids:
                expanded.update(member_ids)
        if expanded - set(passing_ids):
            raise ControllerError(
                "requested screening survivors do not pass the recomputed whole-group gate: "
                + ", ".join(sorted(expanded - set(passing_ids)))
            )
        survivors = sorted(expanded)
        selection_mode = "explicit_recomputed_gate_passing_with_replicate_expansion"
    if not survivors:
        raise ControllerError("screening produced no confirmatory survivor")
    survivor_groups = {
        f"{key[0]}::{key[1]}": copy.deepcopy(gate)
        for key, gate in sorted(group_gates.items())
        if set(gate.get("candidate_arm_ids") or []) & set(survivors)
    }
    bound_arm_ids = sorted(
        set(survivors)
        | {str(by_id[arm_id].control_arm_id) for arm_id in survivors}
    )
    arm_artifacts: dict[str, Any] = {}
    for arm_id in bound_arm_ids:
        arm = by_id[arm_id]
        report_arm = report.get("arms", {}).get(arm_id)
        status_arm = status.get("arms", {}).get(arm_id)
        if not isinstance(report_arm, Mapping) or not isinstance(status_arm, Mapping):
            raise ControllerError(f"screening formal arm binding is missing for {arm_id}")
        root = output_dir(source_plan, arm).resolve()
        group_root = source_report_root.resolve() / arm.directory_name
        group_reports: dict[str, Any] = {}
        if arm.experiment_id != "common-E0":
            group_reports = _assert_group_report_artifact_closure(
                experiment_id=arm.experiment_id,
                group_root=group_root,
                report=report,
            )
        formal_artifacts = _bridge_artifact_hashes(root, label=arm_id)
        archived_sources = _bridge_archived_source_hashes(root, label=arm_id)
        account_identities = _account_window_identity_hashes(root, label=arm_id)
        _assert_formal_artifact_evidence_closure(
            arm_id=arm_id,
            actual=formal_artifacts,
            status_arm=status_arm,
            report_arm=report_arm,
        )
        arm_artifacts[arm_id] = {
            "arm_id": arm_id,
            "experiment_id": arm.experiment_id,
            "variant": arm.variant,
            "replicate": arm.replicate,
            "control_arm_id": arm.control_arm_id,
            "analyzer_mode": arm.analyzer_mode,
            "output_dir": str(root),
            "terminal_state": status_arm.get("state"),
            "formal_evidence_valid": report_arm.get("formal_evidence_valid"),
            "formal_artifacts": formal_artifacts,
            "archived_sources": archived_sources,
            "account_evidence_identities": account_identities,
            "group_reports": group_reports,
        }
    costed_arm_ids, screening_account_identities = _screening_cost_identity_scope(
        report
    )
    derived_path = Path(str(source_plan["paths"]["run_root"])) / "derived-plan.json"
    analyzer_path: Path | None = None
    derived_provenance: dict[str, Any] | None = None
    if derived_path.exists() or derived_path.is_symlink():
        derived_bound = load_bound_json(derived_path, label="screening derived plan")
        old_derived = derived_bound.value
        if not isinstance(old_derived, Mapping):
            raise ControllerError("screening derived plan is not an object")
        verify_bare_document_self_hash(
            old_derived,
            field="derived_plan_sha256",
            label="screening derived plan",
        )
        descriptor = old_derived.get("frozen_analyzer_artifact")
        if not isinstance(descriptor, Mapping):
            raise ControllerError("screening derived plan lacks Analyzer artifact")
        analyzer_path = absolute_path_without_symlinks(
            descriptor.get("path"), label="screening Analyzer artifact"
        )
        analyzer_bound = load_bound_json(
            analyzer_path, label="screening Analyzer artifact"
        )
        old_artifact = analyzer_bound.value
        if not isinstance(old_artifact, Mapping):
            raise ControllerError("screening Analyzer artifact is not an object")
        verify_bare_document_self_hash(
            old_artifact,
            field="artifact_sha256",
            label="screening Analyzer artifact",
        )
        if (
            old_derived.get("campaign_plan_sha256") != source_plan_sha
            or descriptor.get("file_sha256") != analyzer_bound.file_sha256
            or descriptor.get("artifact_sha256") != old_artifact.get("artifact_sha256")
        ):
            raise ControllerError("screening derived/Analyzer provenance differs")
        p99_path = derived_path.parent / "receipts" / "P0.5-06-analyzer-p99.json"
        p99_bound = load_bound_json(p99_path, label="screening Analyzer p99 receipt")
        old_p99 = p99_bound.value
        if not isinstance(old_p99, Mapping):
            raise ControllerError("screening Analyzer p99 receipt is not an object")
        verify_bare_document_self_hash(
            old_p99,
            field="receipt_sha256",
            label="screening Analyzer p99 receipt",
        )
        if old_derived.get("p0_5_06") != old_p99:
            raise ControllerError("screening Analyzer p99 receipt differs from derived plan")
        _assert_screening_derived_evidence_closure(
            status=status,
            report=report,
            derived_path=derived_path,
            derived=old_derived,
            analyzer_descriptor=descriptor,
            p99=old_p99,
        )
        derived_provenance = {
            "derived_path": str(derived_path),
            "derived_file_sha256": derived_bound.file_sha256,
            "derived_plan_sha256": old_derived.get("derived_plan_sha256"),
            "analyzer_path": str(analyzer_path),
            "analyzer_file_sha256": analyzer_bound.file_sha256,
            "analyzer_artifact_sha256": old_artifact.get("artifact_sha256"),
            "p99_path": str(p99_path),
            "p99_file_sha256": p99_bound.file_sha256,
            "p99_receipt_sha256": old_p99.get("receipt_sha256"),
        }
    if any(by_id[arm_id].analyzer_mode == "frozen_replay" for arm_id in survivors):
        if derived_provenance is None:
            raise ControllerError(
                "frozen-replay survivor lacks authenticated screening derived evidence"
            )
    source: dict[str, Any] = {
        "schema": CONFIRMATORY_BRIDGE_SOURCE_SCHEMA,
        "selection_mode": selection_mode,
        "survivor_arm_ids": survivors,
        "survivor_groups": survivor_groups,
        "immutable_roots": {
            "run_root": str(
                absolute_path_without_symlinks(
                    source_plan["paths"]["run_root"],
                    label="screening run root",
                )
            ),
            "report_root": str(
                absolute_path_without_symlinks(
                    source_plan["paths"]["report_root"],
                    label="screening report root",
                )
            ),
            "snapshot": str(
                absolute_path_without_symlinks(
                    source_plan["paths"]["snapshot"],
                    label="screening snapshot",
                )
            ),
        },
        "screening_plan": {
            "path": str(source_plan_path),
            "file_sha256": source_plan_bound.file_sha256,
            "canonical_sha256": source_plan_sha,
            "run_id": source_plan.get("run_id"),
            "snapshot_commit": source_plan.get("freeze", {}).get("snapshot_commit"),
            "snapshot_tree": source_plan.get("freeze", {}).get("snapshot_tree"),
            "launcher_raw_sha256": source_plan.get("freeze", {})
            .get("sources", {})
            .get("launcher_raw_sha256"),
        },
        "terminal_status": {
            "path": str(terminal_status_path),
            "file_sha256": status_bound.file_sha256,
            "semantic_sha256": status.get("terminal_status_input_sha256"),
            "phase": status.get("phase"),
        },
        "terminal_report_receipt": {
            "path": str(terminal_report_receipt_path),
            "file_sha256": receipt_bound.file_sha256,
            "receipt_sha256": receipt.get("receipt_sha256"),
            "status": receipt.get("status"),
            "returncode": receipt.get("returncode"),
        },
        "comprehensive_report": {
            "artifacts": report_artifacts,
            "report_sha256": report.get("report_sha256"),
            "completion_status": report.get("completion", {}).get("status"),
        },
        "derived_provenance": derived_provenance,
        "arms": arm_artifacts,
        "screening_account_evidence_identities": screening_account_identities,
        "screening_costed_arm_ids": costed_arm_ids,
        "generation_calls_during_prepare": 0,
    }
    source["source_evidence_sha256"] = canonical_sha256(source)
    return source


def validate_confirmatory_bridge_contract(plan: Mapping[str, Any], arms: Sequence[Arm]) -> None:
    bridge = plan.get("confirmatory_bridge")
    if not isinstance(bridge, Mapping):
        raise ControllerError("confirmatory bridge contract is malformed")
    verify_bare_document_self_hash(
        bridge,
        field="bridge_sha256",
        label="confirmatory bridge contract",
    )
    expected_fields = {
        "schema",
        "mode",
        "screening_source",
        "survivor_arm_ids",
        "snapshot_contract",
        "launcher_contract",
        "derived_contract",
        "reporting_contract",
        "schedule_seed_prefix",
        "bridge_sha256",
    }
    if set(bridge) != expected_fields:
        raise ControllerError("confirmatory bridge field set differs")
    if (
        bridge.get("schema") != CONFIRMATORY_BRIDGE_SCHEMA
        or bridge.get("mode") != "confirmatory_only"
    ):
        raise ControllerError("confirmatory bridge schema/mode differs")
    if not isinstance(bridge.get("schedule_seed_prefix"), str) or not str(
        bridge.get("schedule_seed_prefix")
    ):
        raise ControllerError("confirmatory bridge schedule seed prefix is empty")
    source = bridge.get("screening_source")
    if not isinstance(source, Mapping) or source.get("schema") != CONFIRMATORY_BRIDGE_SOURCE_SCHEMA:
        raise ControllerError("confirmatory bridge screening source is malformed")
    verify_bare_document_self_hash(
        source,
        field="source_evidence_sha256",
        label="confirmatory bridge screening source",
    )
    survivors = bridge.get("survivor_arm_ids")
    if (
        not isinstance(survivors, list)
        or not survivors
        or survivors != sorted(set(survivors))
        or survivors != source.get("survivor_arm_ids")
    ):
        raise ControllerError("confirmatory bridge survivor set differs")
    by_id = {arm.arm_id: arm for arm in arms}
    if any(
        arm_id not in by_id
        or by_id[arm_id].experiment_id == "common-E0"
        or by_id[arm_id].control_arm_id is None
        for arm_id in survivors
    ):
        raise ControllerError("confirmatory bridge survivor is not a controlled candidate")
    selected_groups = source.get("survivor_groups")
    if not isinstance(selected_groups, Mapping) or any(
        not isinstance(gate, Mapping) or gate.get("pass") is not True
        for gate in selected_groups.values()
    ):
        raise ControllerError("confirmatory bridge survivor gate is not passing")
    all_selected = sorted(
        arm_id
        for gate in selected_groups.values()
        for arm_id in gate.get("candidate_arm_ids") or []
    )
    if all_selected != survivors:
        raise ControllerError("confirmatory bridge replicate-group survivor coverage differs")
    snapshot = bridge.get("snapshot_contract")
    if (
        not isinstance(snapshot, Mapping)
        or set(snapshot) != {"path", "commit", "tree"}
        or snapshot.get("path") != plan.get("paths", {}).get("snapshot")
        or snapshot.get("commit") != plan.get("freeze", {}).get("snapshot_commit")
        or snapshot.get("tree") != plan.get("freeze", {}).get("snapshot_tree")
    ):
        raise ControllerError("confirmatory bridge snapshot contract differs")
    launcher = bridge.get("launcher_contract")
    if (
        not isinstance(launcher, Mapping)
        or set(launcher)
        != {
            "relative_path",
            "raw_sha256",
            "schedule_schema",
            "flag",
            "fd_flag",
            "support_required",
        }
        or launcher.get("relative_path") != plan.get("paths", {}).get("launcher_relative")
        or launcher.get("raw_sha256")
        != plan.get("freeze", {}).get("sources", {}).get("launcher_raw_sha256")
        or launcher.get("schedule_schema") != CONFIRMATORY_SCHEDULE_SCHEMA
        or launcher.get("flag") != "--confirmatory-schedule"
        or launcher.get("fd_flag") != "--confirmatory-schedule-fd"
        or launcher.get("support_required") is not True
    ):
        raise ControllerError("confirmatory bridge launcher contract differs")
    old_launcher_sha = source.get("screening_plan", {}).get("launcher_raw_sha256")
    if not old_launcher_sha or old_launcher_sha == launcher.get("raw_sha256"):
        raise ControllerError("confirmatory bridge did not move off the screening launcher")
    run_root = Path(str(plan.get("paths", {}).get("run_root") or "")).resolve()
    report_root = Path(str(plan.get("paths", {}).get("report_root") or "")).resolve()
    derived = bridge.get("derived_contract")
    if (
        not isinstance(derived, Mapping)
        or derived.get("schema") != CONFIRMATORY_BRIDGE_DERIVED_SCHEMA
        or derived.get("path") != str(run_root / "bridge-derived-plan.json")
        or derived.get("analyzer_path") != str(run_root / "bridge-frozen-analyzer.json")
        or derived.get("p99_path") != str(run_root / "bridge-analyzer-p99.json")
        or derived.get("schedule_root") != str(run_root / "confirmatory-schedules")
        or derived.get("must_regenerate_from_provenance") is not True
        or derived.get("reuse_old_receipt_as_current") is not False
    ):
        raise ControllerError("confirmatory bridge derived contract differs")
    reporting = bridge.get("reporting_contract")
    if (
        not isinstance(reporting, Mapping)
        or reporting.get("output_root") != str(report_root)
        or reporting.get("source_screening_is_immutable_reference") is not True
        or reporting.get("one_complete_cohort_per_survivor") is not True
        or reporting.get("shared_account_delta_once_per_cohort") is not True
        or reporting.get("selected_generation_and_judge_per_role") is not True
        or reporting.get("partial_or_extra_cohort_fails_strict") is not True
        or reporting.get("mini_is_diagnostic_only") is not True
        or reporting.get("automatic_winner_promotion") is not False
    ):
        raise ControllerError("confirmatory bridge reporting contract differs")
    old_run_root = Path(str(source.get("screening_plan", {}).get("path") or "")).parent.resolve()
    old_report_root = Path(
        str(source.get("comprehensive_report", {}).get("artifacts", {})
            .get("EXPERIMENT_RESULTS.json", {})
            .get("path") or "")
    ).parent.resolve()
    if paths_overlap(run_root, old_run_root) or paths_overlap(report_root, old_report_root):
        raise ControllerError("confirmatory bridge writable roots overlap screening evidence")


def validate_confirmatory_bridge_source(plan: Mapping[str, Any]) -> dict[str, Any]:
    bridge = plan.get("confirmatory_bridge")
    if not isinstance(bridge, Mapping):
        raise ControllerError("plan is not a confirmatory bridge")
    source = bridge.get("screening_source")
    if not isinstance(source, Mapping):
        raise ControllerError("confirmatory bridge screening source is missing")
    rebuilt = authenticate_confirmatory_bridge_screening_source(
        source_plan_path=Path(str(source.get("screening_plan", {}).get("path") or "")),
        terminal_status_path=Path(str(source.get("terminal_status", {}).get("path") or "")),
        terminal_report_receipt_path=Path(
            str(source.get("terminal_report_receipt", {}).get("path") or "")
        ),
        requested_survivor_arm_ids=list(bridge.get("survivor_arm_ids") or []),
    )
    # Re-authentication uses an explicit selection mode; normalize only that
    # descriptive field before byte-for-byte evidence comparison.
    rebuilt["selection_mode"] = source.get("selection_mode")
    if rebuilt != dict(source):
        raise ControllerError("confirmatory bridge screening source evidence drifted")
    return rebuilt


def output_dir(plan: Mapping[str, Any], arm: Arm) -> Path:
    imported = preexisting_source_contract(plan)
    if arm.arm_id == ANALYZER_SOURCE_ARM_ID and imported is not None:
        return Path(str(imported["source_output_dir"]))
    return Path(str(plan["paths"]["report_root"])) / arm.directory_name / arm.output_name


def verify_document_self_hash(
    document: Mapping[str, Any],
    *,
    field: str,
    label: str,
) -> None:
    claimed = str(document.get(field) or "")
    detached = copy.deepcopy(dict(document))
    detached.pop(field, None)
    expected = "sha256:" + canonical_sha256(detached)
    if claimed != expected:
        raise ControllerError(f"{label} self-hash differs")


def verify_bare_document_self_hash(
    document: Mapping[str, Any],
    *,
    field: str,
    label: str,
) -> None:
    claimed = str(document.get(field) or "")
    detached = copy.deepcopy(dict(document))
    detached.pop(field, None)
    if SHA256_RE.fullmatch(claimed) is None or claimed != canonical_sha256(detached):
        raise ControllerError(f"{label} self-hash differs")


def verify_result_row_evidence(row: Mapping[str, Any]) -> None:
    schema = row.get("result_evidence_schema")
    claimed = str(row.get("result_evidence_sha256") or "")
    if (
        not isinstance(schema, str)
        or not schema
        or re.fullmatch(r"sha256:[0-9a-f]{64}", claimed) is None
    ):
        raise ControllerError("result row lacks authenticated evidence schema/hash")
    detached = {key: value for key, value in row.items() if key != "result_evidence_sha256"}
    expected = "sha256:" + canonical_sha256({"schema": schema, "result": detached})
    if claimed != expected:
        raise ControllerError("result row evidence hash differs")


def verify_published_artifact_bindings(
    directory: Path,
    manifest: Mapping[str, Any],
    *,
    names: Sequence[str],
) -> dict[str, Path]:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise ControllerError("manifest lacks artifact bindings")
    resolved: dict[str, Path] = {}
    for name in names:
        record = artifacts.get(name)
        if not isinstance(record, Mapping) or record.get("path") != name:
            raise ControllerError(f"manifest lacks exact binding for {name}")
        path = directory / name
        require_regular_file(path)
        size = path.stat().st_size
        if record.get("size_bytes") != size or record.get("sha256") != file_sha256(path):
            raise ControllerError(f"manifest artifact size/hash differs for {name}")
        resolved[name] = path
    return resolved


def authenticate_published_arm_artifacts(
    directory: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Path]]:
    """Authenticate the formal publication envelope before consuming any row."""

    paths = {
        "manifest.json": directory / "manifest.json",
        "results.jsonl": directory / "results.jsonl",
        "trace.jsonl": directory / "trace.jsonl",
        "audit.json": directory / "audit.json",
        "openrouter-non-byok-campaign-proof.json": (
            directory / "openrouter-non-byok-campaign-proof.json"
        ),
    }
    for path in paths.values():
        require_regular_file(path)
    manifest = load_json(paths["manifest.json"])
    audit = load_json(paths["audit.json"])
    proof = load_json(paths["openrouter-non-byok-campaign-proof.json"])
    if not all(isinstance(value, dict) for value in (manifest, audit, proof)):
        raise ControllerError("formal publication documents must be JSON objects")
    verify_document_self_hash(manifest, field="manifest_sha256", label="manifest")
    verify_document_self_hash(audit, field="audit_sha256", label="audit")
    verify_document_self_hash(
        proof,
        field="proof_sha256",
        label="non-BYOK proof",
    )
    bound = verify_published_artifact_bindings(
        directory,
        manifest,
        names=(
            "results.jsonl",
            "trace.jsonl",
            "audit.json",
            "openrouter-non-byok-campaign-proof.json",
        ),
    )
    if manifest.get("audit_sha256") != audit.get("audit_sha256"):
        raise ControllerError("manifest/audit semantic hash binding differs")
    if manifest.get("openrouter_non_byok_campaign_proof_sha256") != proof.get("proof_sha256"):
        raise ControllerError("manifest/non-BYOK proof semantic binding differs")
    return (
        manifest,
        audit,
        proof,
        {
            "manifest.json": paths["manifest.json"],
            **bound,
        },
    )


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def absolute_path_without_symlinks(value: Any, *, label: str) -> Path:
    """Resolve an existing absolute path only after rejecting every symlink."""

    raw = Path(str(value))
    if not raw.is_absolute():
        raise ControllerError(f"{label} must be an absolute path")
    current = Path(raw.anchor)
    for component in raw.parts[1:]:
        current /= component
        try:
            info = current.lstat()
        except OSError as exc:
            raise ControllerError(f"{label} path component is unavailable: {current}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise ControllerError(f"{label} must not traverse a symlink: {current}")
    try:
        return raw.resolve(strict=True)
    except OSError as exc:
        raise ControllerError(f"{label} cannot be resolved: {raw}") from exc


def paths_overlap(left: Path, right: Path) -> bool:
    """Return whether either resolved path contains the other."""

    return _is_within(left, right) or _is_within(right, left)


def validate_confirmatory_root_isolation(
    *,
    run_root: Path,
    report_root: Path,
    snapshot: Path,
    screening_source: Mapping[str, Any],
) -> None:
    """Keep every writable bridge root outside all immutable evidence roots."""

    run_root = secure_future_absolute_path(run_root, label="confirmatory run root")
    report_root = secure_future_absolute_path(report_root, label="confirmatory report root")
    snapshot = secure_existing_directory(snapshot, label="confirmatory snapshot")
    source_plan_path = absolute_path_without_symlinks(
        screening_source.get("screening_plan", {}).get("path"),
        label="screening plan",
    )
    source_plan_bound = load_bound_json(source_plan_path, label="screening plan")
    source_plan = source_plan_bound.value
    if not isinstance(source_plan, Mapping):
        raise ControllerError("screening plan is not an object")
    declared_roots = screening_source.get("immutable_roots")
    if not isinstance(declared_roots, Mapping) or set(declared_roots) != {
        "run_root",
        "report_root",
        "snapshot",
    }:
        raise ControllerError("screening immutable root contract is missing")
    source_run_root = secure_existing_directory(
        Path(str(declared_roots["run_root"])), label="screening run root"
    )
    source_report_root = secure_existing_directory(
        Path(str(declared_roots["report_root"])), label="screening report root"
    )
    source_snapshot = secure_existing_directory(
        Path(str(declared_roots["snapshot"])), label="screening snapshot"
    )
    observed_roots = {
        key: str(Path(str(source_plan.get("paths", {}).get(key) or "")))
        for key in ("run_root", "report_root", "snapshot")
    }
    if observed_roots != {key: str(value) for key, value in declared_roots.items()}:
        raise ControllerError("screening plan roots differ from immutable root contract")
    immutable_roots = {
        "confirmatory snapshot": snapshot,
        "screening run root": source_run_root,
        "screening report root": source_report_root,
        "screening snapshot": source_snapshot,
    }
    if paths_overlap(run_root, report_root):
        raise ControllerError("confirmatory bridge run/report roots must be disjoint")
    for writable_label, writable in (
        ("confirmatory run root", run_root),
        ("confirmatory report root", report_root),
    ):
        for immutable_label, immutable in immutable_roots.items():
            if paths_overlap(writable, immutable):
                raise ControllerError(
                    f"{writable_label} overlaps immutable {immutable_label}"
                )


def verify_arm_publication_identity(
    directory: Path,
    manifest: Mapping[str, Any],
    *,
    expected: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind a formal package to this exact arm, snapshot, and merged config."""

    if directory.resolve() != Path(str(expected["output_dir"])).resolve():
        raise ControllerError("formal output directory differs from expected arm")
    source_bindings = manifest.get("source_manifests")
    if not isinstance(source_bindings, list) or not source_bindings:
        raise ControllerError("manifest has no source-manifest identity bindings")
    verified_sources: list[dict[str, Any]] = []
    for index, binding in enumerate(source_bindings):
        if not isinstance(binding, Mapping):
            raise ControllerError("manifest has malformed source-manifest binding")
        source_path = Path(str(binding.get("path") or ""))
        result_path = Path(str(binding.get("result_path") or ""))
        if not _is_within(source_path, directory) or not _is_within(result_path, directory):
            raise ControllerError("source wave does not belong to expected arm directory")
        require_regular_file(source_path)
        require_regular_file(result_path)
        if binding.get("sha256") != file_sha256(source_path) or binding.get(
            "result_sha256"
        ) != file_sha256(result_path):
            raise ControllerError("source wave raw hash binding differs")
        source = load_json(source_path)
        if not isinstance(source, Mapping):
            raise ControllerError("source wave manifest is not an object")
        args = source.get("args")
        command = source.get("command")
        provenance = source.get("source_provenance")
        input_validation = source.get("benchmark_input_validation")
        artifacts = source.get("artifacts")
        if not all(
            isinstance(value, Mapping)
            for value in (args, command, provenance, input_validation, artifacts)
        ):
            raise ControllerError("source wave lacks execution identity evidence")
        assert isinstance(args, Mapping)
        assert isinstance(command, Mapping)
        assert isinstance(provenance, Mapping)
        assert isinstance(input_validation, Mapping)
        assert isinstance(artifacts, Mapping)
        runner_path = Path(str(provenance.get("runner_path") or "")).resolve()
        runner_identities = expected.get("runner_identities")
        if not isinstance(runner_identities, Mapping):
            raise ControllerError("expected runner identity set is missing")
        expected_runner_sha = runner_identities.get(str(runner_path))
        if (
            Path(str(command.get("cwd") or "")).resolve()
            != Path(str(expected["snapshot"])).resolve()
            or provenance.get("git_head") != expected["snapshot_commit"]
            or provenance.get("git_dirty") is not False
            or provenance.get("git_tracked_dirty") is not False
            or expected_runner_sha is None
            or provenance.get("runner_sha256") != expected_runner_sha
        ):
            raise ControllerError("source wave snapshot/runner identity differs")
        is_resume = runner_path.name == "run_draco_routing_experiment_resume.py"
        if (index == 0 and is_resume) or (index > 0 and not is_resume):
            raise ControllerError("formal source wave runner order differs")
        scheduled_pairs = binding.get("resume_scheduled_pairs")
        schedule_verified = binding.get("resume_schedule_contract_verified")
        if is_resume:
            if (
                schedule_verified is not True
                or not isinstance(scheduled_pairs, list)
                or not scheduled_pairs
            ):
                raise ControllerError("resume wave lacks a verified non-empty schedule")
            seen_schedule: set[tuple[str, str]] = set()
            for scheduled in scheduled_pairs:
                if not isinstance(scheduled, Mapping):
                    raise ControllerError("resume wave schedule row is malformed")
                pair = (
                    str(scheduled.get("group") or ""),
                    str(scheduled.get("task_id") or ""),
                )
                if (
                    pair[0] != "G1"
                    or pair[1] not in expected["task_ids"]
                    or scheduled.get("action")
                    not in {
                        "regenerate",
                        "model_regenerate",
                        "judge_only",
                        "metadata_only",
                        "audit_only",
                    }
                    or pair in seen_schedule
                ):
                    raise ControllerError("resume wave schedule is outside the arm contract")
                seen_schedule.add(pair)
        elif schedule_verified is not False or scheduled_pairs not in ([], None):
            raise ControllerError("main wave unexpectedly carries a resume schedule")
        if (
            Path(str(args.get("input") or "")).resolve()
            != Path(str(expected["benchmark_path"])).resolve()
            or Path(str(args.get("config") or "")).resolve()
            != Path(str(expected["reference_config_path"])).resolve()
            or args.get("groups") != "G1"
            or args.get("max_tasks") != EXPECTED_TASK_COUNT
            or args.get("concurrency") != expected["task_concurrency"]
            or args.get("judge_concurrency") != expected["judge_concurrency"]
            or args.get("generation_max_attempts") != expected["generation_max_attempts"]
            or args.get("dry_run") is not False
            or args.get("require_openrouter_non_byok") is not True
            or args.get("require_clean_source") is not True
            or not _is_within(Path(str(args.get("output_dir") or "")), directory)
        ):
            raise ControllerError("source wave invocation differs from arm contract")
        if (
            input_validation.get("actual_sha256") != expected["benchmark_sha256"]
            or input_validation.get("actual_task_count") != EXPECTED_TASK_COUNT
            or input_validation.get("task_ids_match") is not True
            or input_validation.get("status") != "matched"
        ):
            raise ControllerError("source wave benchmark identity differs")
        effective_path = Path(str(artifacts.get("experiment_config_effective_json") or ""))
        if not _is_within(effective_path, directory):
            raise ControllerError("source wave effective config is outside arm directory")
        effective = load_json(effective_path)
        effective_judge = effective.get("judge") if isinstance(effective, Mapping) else None
        effective_generation = (
            effective.get("generation") if isinstance(effective, Mapping) else None
        )
        if (
            not isinstance(effective_judge, Mapping)
            or effective_judge.get("concurrency") != expected["judge_concurrency"]
            or not isinstance(effective_generation, Mapping)
            or effective_generation.get("max_attempts") != expected["generation_max_attempts"]
        ):
            raise ControllerError(
                "source wave effective Judge/retry policy differs from arm contract"
            )
        if canonical_sha256(effective) != expected["effective_config_sha256"]:
            raise ControllerError("source wave effective config differs from arm override")
        verified_sources.append(
            {
                "source_index": index,
                "runner_kind": "resume" if is_resume else "main",
                "manifest_sha256": file_sha256(source_path),
                "result_sha256": file_sha256(result_path),
                "effective_config_sha256": canonical_sha256(effective),
            }
        )
    return {
        "arm_id": expected["arm_id"],
        "output_name": expected["output_name"],
        "run_id": expected["run_id"],
        "override_sha256": expected["override_sha256"],
        "effective_config_sha256": expected["effective_config_sha256"],
        "source_manifests": verified_sources,
    }


def candidate_order_seed_execution_evidence(
    rows: Sequence[Mapping[str, Any]],
    *,
    expected: Any,
) -> dict[str, Any]:
    """Validate P0.5-36 seed evidence only for calls that reached aggregation."""

    if not isinstance(expected, Mapping) or expected.get("required") is not True:
        return {"required": False, "pass": True, "status": "not_required"}
    expected_configured = expected.get("configured_candidate_order_seed")
    expected_effective = expected.get("effective_candidate_order_seed")
    failures: list[dict[str, Any]] = []
    aggregation_call_count = 0
    not_applicable_call_count = 0
    for row in rows:
        task_id = str(row.get("task_id") or "")
        root_trace = row.get("ensemble_trace")
        if not isinstance(root_trace, Mapping):
            continue
        raw_calls = root_trace.get("calls")
        calls = (
            [call for call in raw_calls if isinstance(call, Mapping)]
            if isinstance(raw_calls, list)
            else [root_trace]
        )
        for call_index, call in enumerate(calls, start=1):
            aggregator_recovery = call.get("aggregator_recovery")
            aggregator_attempts = (
                aggregator_recovery.get("attempts")
                if isinstance(aggregator_recovery, Mapping)
                else None
            )
            aggregator_request_started = bool(
                isinstance(aggregator_attempts, list)
                and any(
                    isinstance(attempt, Mapping) and attempt.get("request_started") is True
                    for attempt in aggregator_attempts
                )
            )
            entered_aggregation = (
                call.get("execution_mode") != "aggregator_only"
                and type(call.get("total_candidates")) is int
                and call["total_candidates"] > 0
                and (
                    call.get("final_request_role") == "aggregator"
                    or aggregator_request_started
                    or bool(call.get("candidate_display_order"))
                )
            )
            if not entered_aggregation:
                not_applicable_call_count += 1
                continue
            aggregation_call_count += 1
            plan = call.get("selection_plan")
            candidates = call.get("candidates")
            selected_indexes = (
                [
                    candidate.get("index")
                    for candidate in candidates
                    if isinstance(candidate, Mapping)
                    and candidate.get("selected_for_aggregation") is True
                ]
                if isinstance(candidates, list)
                else []
            )
            expected_display_order = list(selected_indexes)
            if all(type(index) is int for index in expected_display_order):
                random.Random(expected_effective).shuffle(expected_display_order)
            display_order = call.get("candidate_display_order")
            call_failures: list[str] = []
            if call.get("shuffle_candidates") is not True:
                call_failures.append("shuffle_candidates")
            if (
                type(call.get("configured_candidate_order_seed")) is not int
                or call.get("configured_candidate_order_seed") != expected_configured
            ):
                call_failures.append("configured_candidate_order_seed")
            if (
                type(call.get("candidate_order_seed")) is not int
                or call.get("candidate_order_seed") != expected_effective
            ):
                call_failures.append("candidate_order_seed")
            if call.get("candidate_order_seed_source") != "configured":
                call_failures.append("candidate_order_seed_source")
            if (
                not isinstance(plan, Mapping)
                or type(plan.get("configured_candidate_order_seed")) is not int
                or plan.get("configured_candidate_order_seed") != expected_configured
                or type(plan.get("effective_candidate_order_seed")) is not int
                or plan.get("effective_candidate_order_seed") != expected_effective
            ):
                call_failures.append("selection_plan_seed")
            if (
                type(call.get("selected_candidate_count")) is not int
                or call.get("selected_candidate_count") != len(selected_indexes)
                or not selected_indexes
                or len(set(selected_indexes)) != len(selected_indexes)
            ):
                call_failures.append("selected_candidates")
            if (
                not isinstance(display_order, list)
                or any(type(index) is not int for index in display_order)
                or display_order != expected_display_order
            ):
                call_failures.append("candidate_display_order")
            if call_failures:
                failures.append(
                    {
                        "task_id": task_id,
                        "call_index": call_index,
                        "fields": call_failures,
                    }
                )
    return {
        "required": True,
        "pass": not failures,
        "status": (
            "mismatched" if failures else "matched" if aggregation_call_count else "not_applicable"
        ),
        "configured_candidate_order_seed": expected_configured,
        "effective_candidate_order_seed": expected_effective,
        "aggregation_call_count": aggregation_call_count,
        "not_applicable_call_count": not_applicable_call_count,
        "failures": failures,
    }


def inspect_complete_arm(
    directory: Path,
    *,
    expected_task_ids: set[str],
    expected_task_concurrency: int,
    expected_identity: Mapping[str, Any] | None = None,
) -> tuple[bool, dict[str, Any]]:
    required = {
        "manifest": directory / "manifest.json",
        "results": directory / "results.jsonl",
        "trace": directory / "trace.jsonl",
        "audit": directory / "audit.json",
        "proof": directory / "openrouter-non-byok-campaign-proof.json",
    }
    if not directory.exists():
        return False, {"reason": "output_absent"}
    if directory.is_symlink() or not directory.is_dir():
        return False, {"reason": "unsafe_output_directory"}
    seed_evidence: dict[str, Any] = {
        "required": False,
        "pass": True,
        "status": "not_required",
    }
    try:
        manifest, audit, proof, _ = authenticate_published_arm_artifacts(directory)
        if expected_identity is None:
            raise ControllerError("expected arm publication identity is unavailable")
        arm_identity = verify_arm_publication_identity(
            directory,
            manifest,
            expected=expected_identity,
        )
        rows: list[dict[str, Any]] = []
        with required["results"].open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ControllerError(f"invalid result line {line_number}") from exc
                if not isinstance(row, dict):
                    raise ControllerError(f"non-object result line {line_number}")
                verify_result_row_evidence(row)
                rows.append(row)
        trace_rows: list[dict[str, Any]] = []
        with required["trace"].open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                try:
                    trace_row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ControllerError(f"invalid trace line {line_number}") from exc
                if not isinstance(trace_row, dict):
                    raise ControllerError(f"non-object trace line {line_number}")
                trace_schema = trace_row.get("trace_schema")
                if trace_schema is not None and trace_schema != trace_row.get(
                    "result_evidence_schema",
                    "opensquilla.draco.result-evidence/v1",
                ):
                    raise ControllerError(
                        f"trace line {line_number} has an unknown evidence schema"
                    )
                trace_result_hash = str(trace_row.get("result_evidence_sha256") or "")
                if re.fullmatch(r"sha256:[0-9a-f]{64}", trace_result_hash) is None:
                    raise ControllerError(
                        f"trace line {line_number} lacks a result evidence binding"
                    )
                trace_rows.append(trace_row)
        seed_evidence = candidate_order_seed_execution_evidence(
            rows,
            expected=expected_identity.get("candidate_order_seed_evidence"),
        )
    except (ControllerError, OSError) as exc:
        return False, {"reason": "artifact_validation_failed", "detail": str(exc)}
    actual_task_ids = {str(row.get("task_id") or "") for row in rows}
    results_by_task = {str(row.get("task_id") or ""): row for row in rows}
    trace_by_task = {str(row.get("task_id") or ""): row for row in trace_rows}
    trace_binding_ok = (
        len(trace_rows) == len(trace_by_task) == 10
        and set(trace_by_task) == actual_task_ids
        and all(
            trace_by_task[task_id].get("result_evidence_sha256")
            == results_by_task[task_id].get("result_evidence_sha256")
            for task_id in actual_task_ids
        )
    )
    groups = {str(row.get("group") or "") for row in rows}
    source_manifests = manifest.get("source_manifests")
    scheduling_ok = bool(source_manifests) and all(
        isinstance(source, Mapping)
        and isinstance(source.get("execution_scheduling"), Mapping)
        and source["execution_scheduling"].get("task_concurrency") == expected_task_concurrency
        for source in source_manifests or []
    )
    checks = {
        "manifest_status_complete": manifest.get("status") == "complete",
        "manifest_result_count": manifest.get("result_count") == 10,
        "manifest_task_count": manifest.get("task_count") == 10,
        "manifest_groups": manifest.get("groups") == ["G1"],
        "manifest_execution_pass": manifest.get("execution_pass") is True,
        "results_row_count": len(rows) == 10,
        "results_group": groups == {"G1"},
        "results_unique_tasks": actual_task_ids == expected_task_ids,
        "trace_result_evidence_bindings": trace_binding_ok,
        "task_concurrency": scheduling_ok,
        "audit_execution_pass": audit.get("execution_pass") is True,
        "proof_execution_pass": proof.get("execution_pass") is True,
        "candidate_order_seed_evidence": seed_evidence["pass"] is True,
    }
    evidence = {
        "reason": "complete" if all(checks.values()) else "completion_contract_failed",
        "checks": checks,
        "manifest_status": manifest.get("status"),
        "execution_pass": manifest.get("execution_pass"),
        "policy_pass": manifest.get("policy_pass"),
        "audit_pass": manifest.get("audit_pass"),
        "proof_pass": proof.get("pass"),
        "audit_warnings": copy.deepcopy(audit.get("warnings") or []),
        "proof_warnings": copy.deepcopy(proof.get("warnings") or []),
        "manifest_policy_pass": manifest.get("policy_pass"),
        "manifest_audit_pass": manifest.get("audit_pass"),
        "artifact_sha256": {key: file_sha256(path) for key, path in required.items()},
        "arm_identity": arm_identity,
        "candidate_order_seed_evidence": seed_evidence,
    }
    # Audit/policy findings are retained, but do not erase a complete answer or
    # cause a costly duplicate rerun.  That separation is intentional.
    return all(checks.values()), evidence


def _selection_plans(row: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    plans: list[Mapping[str, Any]] = []
    routing_trace = row.get("routing_trace")
    if isinstance(routing_trace, Mapping) and isinstance(
        routing_trace.get("selection_plan"), Mapping
    ):
        plans.append(routing_trace["selection_plan"])
    trace = row.get("ensemble_trace")
    if isinstance(trace, Mapping) and isinstance(trace.get("selection_plan"), Mapping):
        plans.append(trace["selection_plan"])
    calls = trace.get("calls") if isinstance(trace, Mapping) else None
    if isinstance(calls, list):
        plans.extend(
            call["selection_plan"]
            for call in calls
            if isinstance(call, Mapping) and isinstance(call.get("selection_plan"), Mapping)
        )
    if not plans:
        raise ControllerError("trace row has no selection plan")
    return plans


def _first_selection_plan(row: Mapping[str, Any]) -> Mapping[str, Any]:
    plans = _selection_plans(row)
    profile_hashes = {canonical_sha256(plan.get("task_profile_pre_escalation")) for plan in plans}
    if len(profile_hashes) != 1:
        raise ControllerError("task has multiple pre-escalation Analyzer profiles")
    # routing_trace.selection_plan is the original provider-build decision and
    # therefore precedes any execution retry route. _selection_plans orders it
    # first when present.
    return plans[0]


def _analyzer_metadata_without_usage(analyzer: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(analyzer))
    result.pop("usage", None)
    return result


def _validated_analyzer_attempt_ledger(
    *,
    task_id: str,
    analyzer: Mapping[str, Any],
    expected_config: Mapping[str, Any],
    allow_zero_attempts: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    """Authenticate every known/unknown physical Analyzer attempt."""

    expected_provider = str(expected_config.get("provider") or "").strip().lower()
    expected_model = str(expected_config.get("model") or "").strip().lower()
    if (
        str(analyzer.get("provider") or "").strip().lower() != expected_provider
        or str(analyzer.get("model") or "").strip().lower() != expected_model
    ):
        raise ControllerError(f"E0 task {task_id} Analyzer identity differs from config")
    warnings = analyzer.get("normalization_warnings")
    if (
        not isinstance(warnings, list)
        or any(not isinstance(item, str) or not item.strip() for item in warnings)
        or len(warnings) != len(set(warnings))
    ):
        raise ControllerError(f"E0 task {task_id} has invalid Analyzer warnings")
    usage = analyzer.get("usage")
    if not isinstance(usage, Mapping):
        raise ControllerError(f"E0 task {task_id} lacks Analyzer usage")
    usage_copy = copy.deepcopy(dict(usage))
    raw_attempts = usage_copy.get("physical_attempts")
    if not isinstance(raw_attempts, list) or (not raw_attempts and not allow_zero_attempts):
        raise ControllerError(f"E0 task {task_id} lacks physical Analyzer attempts")
    declared_count = usage_copy.get("attempt_count")
    if (
        isinstance(declared_count, bool)
        or not isinstance(declared_count, int)
        or declared_count != len(raw_attempts)
    ):
        raise ControllerError(f"E0 task {task_id} Analyzer attempt count differs")
    if "physical_request_count" in usage_copy:
        physical_count = usage_copy.get("physical_request_count")
        if (
            isinstance(physical_count, bool)
            or not isinstance(physical_count, int)
            or physical_count != len(raw_attempts)
        ):
            raise ControllerError(f"E0 task {task_id} Analyzer physical request count differs")

    attempts: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    unknown_count = 0
    token_fields = (
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cached_tokens",
        "cache_write_tokens",
    )
    token_totals = {field: 0 for field in token_fields}
    cost_total = 0.0
    for ordinal, raw_attempt in enumerate(raw_attempts, start=1):
        if not isinstance(raw_attempt, Mapping):
            raise ControllerError(f"E0 task {task_id} has malformed Analyzer attempt")
        attempt = copy.deepcopy(dict(raw_attempt))
        attempt_ordinal = attempt.get("attempt")
        if (
            isinstance(attempt_ordinal, bool)
            or not isinstance(attempt_ordinal, int)
            or attempt_ordinal != ordinal
        ):
            raise ControllerError(f"E0 task {task_id} Analyzer attempt order is invalid")
        attempt_id = str(attempt.get("physical_attempt_id") or "").strip().lower()
        if PHYSICAL_ATTEMPT_ID_RE.fullmatch(attempt_id) is None or attempt_id in seen_ids:
            raise ControllerError(f"E0 task {task_id} Analyzer attempt identity is invalid")
        seen_ids.add(attempt_id)
        if (
            str(attempt.get("requested_provider") or "").strip().lower() != expected_provider
            or str(attempt.get("requested_model") or "").strip().lower() != expected_model
        ):
            raise ControllerError(f"E0 task {task_id} Analyzer physical request identity differs")
        provider_usage = attempt.get("provider_usage")
        if not isinstance(provider_usage, Mapping):
            raise ControllerError(
                f"E0 task {task_id} Analyzer attempt lacks provider usage evidence"
            )
        mirrored_id = str(provider_usage.get("physical_attempt_id") or "").strip().lower()
        if mirrored_id != attempt_id:
            raise ControllerError(
                f"E0 task {task_id} Analyzer physical attempt identity mirror differs"
            )
        reported_ids = attempt.get("reported_physical_attempt_ids")
        nested_reported_ids = provider_usage.get("reported_physical_attempt_ids")
        for raw_reported in (reported_ids, nested_reported_ids):
            if raw_reported in (None, []):
                continue
            if not isinstance(raw_reported, list) or raw_reported != [attempt_id]:
                raise ControllerError(
                    f"E0 task {task_id} Analyzer attempt reports conflicting identities"
                )
        for field in token_fields:
            value = attempt.get(field, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ControllerError(f"E0 task {task_id} Analyzer attempt {field} is invalid")
            token_totals[field] += value
        raw_cost = attempt.get("billed_cost", 0.0)
        if (
            isinstance(raw_cost, bool)
            or not isinstance(raw_cost, int | float)
            or not math.isfinite(float(raw_cost))
            or float(raw_cost) < 0.0
        ):
            raise ControllerError(f"E0 task {task_id} Analyzer attempt cost is invalid")
        cost_total += float(raw_cost)
        usage_unknown = attempt.get("usage_unknown") is True
        if usage_unknown:
            unknown_count += 1
            unknown_reason = str(attempt.get("unknown_reason") or "").strip()
            nested_reason = str(provider_usage.get("unknown_reason") or "").strip()
            if (
                str(attempt.get("provider") or "").strip()
                or str(attempt.get("model") or "").strip()
                or str(provider_usage.get("provider") or "").strip()
                or str(provider_usage.get("model") or "").strip()
                or any(attempt.get(field, 0) != 0 for field in token_fields)
                or any(
                    provider_usage.get(field) not in (None, 0, 0.0)
                    for field in (
                        "prompt_tokens",
                        "completion_tokens",
                        "input_tokens",
                        "output_tokens",
                        "reasoning_tokens",
                        "cached_tokens",
                        "cache_read_tokens",
                        "cache_write_tokens",
                        "cost",
                        "billed_cost",
                    )
                )
                or float(raw_cost) != 0.0
                or str(attempt.get("cost_source") or "none").strip().lower()
                not in {"none", "unavailable"}
                or provider_usage.get("usage_unknown") is not True
                or not unknown_reason
                or nested_reason != unknown_reason
            ):
                raise ControllerError(
                    f"E0 task {task_id} Analyzer unknown attempt is contradictory"
                )
        elif (
            provider_usage.get("usage_unknown") is True
            or str(attempt.get("provider") or "").strip().lower() != expected_provider
            or str(attempt.get("model") or "").strip().lower() != expected_model
        ):
            raise ControllerError(f"E0 task {task_id} Analyzer physical response identity differs")
        attempts.append(attempt)

    if "usage_unknown_count" in usage_copy:
        declared_unknown = usage_copy.get("usage_unknown_count")
        if (
            isinstance(declared_unknown, bool)
            or not isinstance(declared_unknown, int)
            or declared_unknown != unknown_count
        ):
            raise ControllerError(f"E0 task {task_id} Analyzer unknown count differs")
    for field, total in token_totals.items():
        if field in usage_copy:
            aggregate_value = usage_copy.get(field)
            if (
                isinstance(aggregate_value, bool)
                or not isinstance(aggregate_value, int)
                or aggregate_value != total
            ):
                raise ControllerError(f"E0 task {task_id} Analyzer aggregate {field} differs")
    if "billed_cost" in usage_copy:
        aggregate_cost = usage_copy.get("billed_cost")
        if (
            isinstance(aggregate_cost, bool)
            or not isinstance(aggregate_cost, int | float)
            or not math.isfinite(float(aggregate_cost))
            or not math.isclose(float(aggregate_cost), cost_total, rel_tol=0.0, abs_tol=1e-12)
        ):
            raise ControllerError(f"E0 task {task_id} Analyzer aggregate cost differs")
    return usage_copy, attempts, unknown_count


def _validated_live_analyzer_evidence(
    *,
    task_id: str,
    analyzer: Mapping[str, Any],
    expected_config: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], int, int]:
    """Return usage, terminal successful attempt, tokens, and unknown count."""

    if (
        analyzer.get("source") != "llm_provider"
        or analyzer.get("schema_valid") is not True
        or str(analyzer.get("fallback_reason") or "")
    ):
        raise ControllerError(f"E0 task {task_id} Analyzer is not a valid live result")
    usage, attempts, unknown_count = _validated_analyzer_attempt_ledger(
        task_id=task_id,
        analyzer=analyzer,
        expected_config=expected_config,
        allow_zero_attempts=False,
    )
    final_attempt = attempts[-1]
    final_output_tokens = final_attempt.get("output_tokens")
    if (
        final_attempt.get("usage_unknown") is True
        or isinstance(final_output_tokens, bool)
        or not isinstance(final_output_tokens, int)
        or final_output_tokens <= 0
    ):
        raise ControllerError(
            f"E0 task {task_id} final Analyzer attempt has no known positive output tokens"
        )
    return usage, final_attempt, final_output_tokens, unknown_count


def _validated_router_fallback_evidence(
    *,
    task_id: str,
    analyzer: Mapping[str, Any],
    expected_config: Mapping[str, Any],
) -> tuple[dict[str, Any], int]:
    if (
        analyzer.get("source") != "router_fallback"
        or analyzer.get("schema_valid") is not False
        or not str(analyzer.get("fallback_reason") or "").strip()
    ):
        raise ControllerError(f"E0 task {task_id} Analyzer fallback provenance is invalid")
    usage, _, unknown_count = _validated_analyzer_attempt_ledger(
        task_id=task_id,
        analyzer=analyzer,
        expected_config=expected_config,
        allow_zero_attempts=True,
    )
    return usage, unknown_count


def register_analyzer_attempt_owners(
    owners: dict[str, str], *, task_id: str, usage: Mapping[str, Any]
) -> None:
    """Reject one physical Analyzer request being attributed to multiple tasks."""

    attempts = usage.get("physical_attempts")
    if not isinstance(attempts, list):
        raise ControllerError(f"E0 task {task_id} lacks physical Analyzer attempts")
    for attempt in attempts:
        if not isinstance(attempt, Mapping):
            raise ControllerError(f"E0 task {task_id} has malformed Analyzer attempt")
        attempt_id = str(attempt.get("physical_attempt_id") or "")
        prior_owner = owners.get(attempt_id)
        if prior_owner is not None and prior_owner != task_id:
            raise ControllerError(
                "Analyzer physical attempt identity is reused across tasks: "
                f"{prior_owner}/{task_id}"
            )
        owners[attempt_id] = task_id


def extract_analyzer_artifact(
    *,
    source_arm: Arm,
    source_dir: Path,
    destination: Path,
    expected_task_ids: set[str],
    snapshot: Path,
    snapshot_identity: Mapping[str, str],
    plan_sha256: str,
    replay_schema: str,
    allow_deterministic_router_fallback: bool,
    source_import_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if replay_schema not in FROZEN_TASK_ANALYSIS_SCHEMAS:
        raise ControllerError("unsupported frozen Analyzer replay schema")
    manifest, _, _, bound_paths = authenticate_published_arm_artifacts(source_dir)
    manifest_path = bound_paths["manifest.json"]
    trace_path = bound_paths["trace.jsonl"]
    results_path = bound_paths["results.jsonl"]
    result_evidence_by_task: dict[str, str] = {}
    with results_path.open(encoding="utf-8") as results_handle:
        for line_number, line in enumerate(results_handle, start=1):
            try:
                result_row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ControllerError(f"invalid source result row {line_number}") from exc
            if not isinstance(result_row, Mapping):
                raise ControllerError(f"source result row {line_number} is not an object")
            verify_result_row_evidence(result_row)
            result_task_id = str(result_row.get("task_id") or "")
            if result_task_id in result_evidence_by_task:
                raise ControllerError(f"duplicate source result task {result_task_id}")
            result_evidence_by_task[result_task_id] = str(
                result_row.get("result_evidence_sha256") or ""
            )
    if set(result_evidence_by_task) != expected_task_ids:
        raise ControllerError("source result evidence coverage differs")
    profiles: dict[str, Any] = {}
    source_task_analyzer_config: dict[str, Any] | None = None
    physical_attempt_owners: dict[str, str] = {}
    source_snapshot = (
        Path(str(source_import_evidence["source_snapshot_package_dir"]))
        if source_import_evidence is not None
        else snapshot
    )
    with trace_path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ControllerError(f"invalid E0 trace row {line_number}") from exc
            if not isinstance(row, Mapping):
                raise ControllerError(f"E0 trace row {line_number} is not an object")
            task_id = str(row.get("task_id") or "")
            if row.get("group") != "G1" or task_id not in expected_task_ids:
                raise ControllerError(f"E0 trace row {line_number} is outside frozen G1 tasks")
            if task_id in profiles:
                raise ControllerError(f"duplicate E0 trace task: {task_id}")
            if row.get("error") not in (None, "", {}):
                raise ControllerError(f"E0 task {task_id} is not execution-complete")
            task_input_sha256 = str(row.get("task_input_sha256") or "").strip()
            task_prompt_sha256 = str(row.get("prompt_sha256") or "").strip()
            result_evidence_sha256 = str(row.get("result_evidence_sha256") or "").strip()
            if re.fullmatch(r"sha256:[0-9a-f]{64}", task_input_sha256) is None:
                raise ControllerError(f"E0 task {task_id} lacks valid task input hash")
            if SHA256_RE.fullmatch(task_prompt_sha256) is None:
                raise ControllerError(f"E0 task {task_id} lacks valid prompt hash")
            if re.fullmatch(r"sha256:[0-9a-f]{64}", result_evidence_sha256) is None:
                raise ControllerError(f"E0 task {task_id} lacks valid result evidence hash")
            if result_evidence_by_task.get(task_id) != result_evidence_sha256:
                raise ControllerError(f"E0 task {task_id} trace/result evidence binding differs")
            selection = _first_selection_plan(row)
            profile = selection.get("task_profile_pre_escalation")
            analyzer = selection.get("task_analyzer")
            if not isinstance(profile, Mapping) or not isinstance(analyzer, Mapping):
                raise ControllerError(f"E0 task {task_id} lacks Analyzer evidence")
            profile_copy = copy.deepcopy(dict(profile))
            metadata = _analyzer_metadata_without_usage(analyzer)
            ranking_parameters = selection.get("ranking_parameters")
            current_analyzer_config = (
                ranking_parameters.get("task_analyzer")
                if isinstance(ranking_parameters, Mapping)
                else None
            )
            if not isinstance(current_analyzer_config, Mapping):
                raise ControllerError(f"E0 task {task_id} lacks effective Analyzer config")
            current_config_copy = copy.deepcopy(dict(current_analyzer_config))
            if source_task_analyzer_config is None:
                source_task_analyzer_config = current_config_copy
            elif source_task_analyzer_config != current_config_copy:
                raise ControllerError("E0 rows do not share one effective Analyzer config")
            origin_outcome: str
            final_attempt: dict[str, Any] | None = None
            output_tokens: int | None = None
            fallback_validation: dict[str, Any] | None = None
            if (
                analyzer.get("source") == "llm_provider"
                and analyzer.get("schema_valid") is True
                and not str(analyzer.get("fallback_reason") or "")
            ):
                usage, final_attempt, output_tokens, unknown_count = (
                    _validated_live_analyzer_evidence(
                        task_id=task_id,
                        analyzer=analyzer,
                        expected_config=current_config_copy,
                    )
                )
                origin_outcome = "live_success"
            elif allow_deterministic_router_fallback:
                usage, unknown_count = _validated_router_fallback_evidence(
                    task_id=task_id,
                    analyzer=analyzer,
                    expected_config=current_config_copy,
                )
                request_context = selection.get("request_context")
                routed_tier = str(selection.get("routed_tier") or "")
                if not isinstance(request_context, Mapping) or not routed_tier:
                    raise ControllerError(f"E0 task {task_id} lacks deterministic fallback inputs")
                source_validation = validate_fallback_profile_isolated(
                    source_snapshot,
                    profile=profile_copy,
                    routed_tier=routed_tier,
                    request_context=request_context,
                    ranking_config=ranking_parameters,
                )
                replay_validation = (
                    copy.deepcopy(source_validation)
                    if source_snapshot.resolve() == snapshot.resolve()
                    else validate_fallback_profile_isolated(
                        snapshot,
                        profile=profile_copy,
                        routed_tier=routed_tier,
                        request_context=request_context,
                        ranking_config=ranking_parameters,
                    )
                )
                analyzer_version = str(analyzer.get("analyzer_version") or "")
                if any(
                    validation.get("schema_valid") is not True
                    or validation.get("normalized") != profile_copy
                    or validation.get("derived") != profile_copy
                    or str(validation.get("analyzer_version") or "") != analyzer_version
                    for validation in (source_validation, replay_validation)
                ):
                    raise ControllerError(
                        f"E0 task {task_id} router fallback profile is not deterministic"
                    )
                fallback_validation = {
                    "source_snapshot": {
                        key: source_validation[key]
                        for key in ("analyzer_version", "module_path", "module_sha256")
                    },
                    "replay_snapshot": {
                        key: replay_validation[key]
                        for key in ("analyzer_version", "module_path", "module_sha256")
                    },
                }
                origin_outcome = "deterministic_router_fallback"
            else:
                raise ControllerError(f"E0 task {task_id} Analyzer is not a valid live result")
            register_analyzer_attempt_owners(
                physical_attempt_owners,
                task_id=task_id,
                usage=usage,
            )
            profile_sha256 = canonical_sha256(profile_copy)
            if (
                str(selection.get("task_profile_hash") or "")
                not in {
                    "",
                    profile_sha256,
                }
                and selection.get("task_profile_post_escalation") == profile
            ):
                raise ControllerError(f"E0 task {task_id} profile hash is contradictory")
            # Every call/retry must carry the exact same frozen Analyzer
            # profile and provenance. Route changes are allowed, Analyzer
            # drift is not.
            expected_analyzer_hash = canonical_sha256(analyzer)
            for repeated_plan in _selection_plans(row):
                if (
                    canonical_sha256(repeated_plan.get("task_profile_pre_escalation"))
                    != profile_sha256
                ):
                    raise ControllerError(f"E0 task {task_id} Analyzer profile drifted")
                repeated_analyzer = repeated_plan.get("task_analyzer")
                if (
                    not isinstance(repeated_analyzer, Mapping)
                    or canonical_sha256(repeated_analyzer) != expected_analyzer_hash
                ):
                    raise ControllerError(f"E0 task {task_id} Analyzer provenance drifted")
            profile_row: dict[str, Any] = {
                "task_id": task_id,
                "task_input_sha256": task_input_sha256,
                "task_prompt_sha256": task_prompt_sha256,
                "task_profile_pre_escalation": profile_copy,
                "task_profile_pre_escalation_sha256": profile_sha256,
                "original_analyzer": metadata,
                "original_analyzer_sha256": canonical_sha256(metadata),
                "origin_outcome": origin_outcome,
                "original_analyzer_usage": usage,
                "original_analyzer_usage_sha256": canonical_sha256(usage),
                "original_analyzer_physical_attempt_count": len(usage["physical_attempts"]),
                "original_analyzer_usage_unknown_count": unknown_count,
                "source_trace_row_sha256": canonical_sha256(row),
                "source_result_evidence_sha256": result_evidence_sha256,
            }
            if final_attempt is not None and output_tokens is not None:
                profile_row.update(
                    {
                        "final_successful_physical_attempt_sha256": canonical_sha256(final_attempt),
                        "final_successful_physical_attempt_id": final_attempt[
                            "physical_attempt_id"
                        ],
                        "final_successful_physical_attempt_output_tokens": output_tokens,
                        "original_analyzer_output_tokens": output_tokens,
                    }
                )
            if fallback_validation is not None:
                profile_row["fallback_validation"] = fallback_validation
            profiles[task_id] = profile_row
    if set(profiles) != expected_task_ids:
        raise ControllerError(
            "E0 Analyzer profile coverage differs: "
            f"missing={sorted(expected_task_ids - set(profiles))}, "
            f"extra={sorted(set(profiles) - expected_task_ids)}"
        )
    assert source_task_analyzer_config is not None
    if replay_schema == FROZEN_TASK_ANALYSIS_SCHEMA_V1 and any(
        row["origin_outcome"] != "live_success" for row in profiles.values()
    ):
        raise ControllerError("frozen replay schema v1 cannot encode fallback Analyzer origin")
    replay_entries: dict[str, Any] = {}
    for task_id, row in sorted(profiles.items()):
        replay_entry = {
            "task_input_sha256": row["task_input_sha256"],
            "prompt_sha256": row["task_prompt_sha256"],
            "task_profile_pre_escalation": row["task_profile_pre_escalation"],
            "task_profile_pre_escalation_sha256": row["task_profile_pre_escalation_sha256"],
            "task_analyzer": {
                "source": "frozen_replay",
                "schema_valid": row["original_analyzer"].get("schema_valid") is True,
                "confidence": row["original_analyzer"].get("confidence"),
                "analyzer_version": row["original_analyzer"].get("analyzer_version"),
                "provider": row["original_analyzer"].get("provider"),
                "model": row["original_analyzer"].get("model"),
                "fallback_reason": row["original_analyzer"].get("fallback_reason", ""),
                "usage": {},
                "normalization_warnings": copy.deepcopy(
                    row["original_analyzer"].get("normalization_warnings") or []
                ),
            },
        }
        if replay_schema == FROZEN_TASK_ANALYSIS_SCHEMA_V2:
            replay_entry["origin_outcome"] = row["origin_outcome"]
        replay_entries[task_id] = replay_entry
    replay_payload = {
        "schema": replay_schema,
        "mode": "frozen_replay",
        "source_experiment": source_arm.arm_id,
        "source_manifest_sha256": file_sha256(manifest_path),
        "source_results_sha256": file_sha256(results_path),
        "source_task_analyzer_config": source_task_analyzer_config,
        "source_task_analyzer_config_sha256": canonical_sha256(source_task_analyzer_config),
        "entries": replay_entries,
        "entries_sha256": canonical_sha256(replay_entries),
    }
    artifact: dict[str, Any] = {
        "schema": ANALYZER_ARTIFACT_SCHEMA,
        "created_at": utc_now(),
        "source": {
            "arm_id": source_arm.arm_id,
            "output_dir": str(source_dir),
            "original_output_dir": (
                source_import_evidence["source_output_dir"]
                if source_import_evidence is not None
                else str(source_dir)
            ),
            "manifest_sha256": file_sha256(manifest_path),
            "trace_sha256": file_sha256(trace_path),
            "snapshot_commit": (
                source_import_evidence["source_snapshot_commit"]
                if source_import_evidence is not None
                else snapshot_identity["commit"]
            ),
            "snapshot_tree": (
                source_import_evidence["source_snapshot_tree"]
                if source_import_evidence is not None
                else snapshot_identity["tree"]
            ),
            "replay_snapshot_commit": snapshot_identity["commit"],
            "replay_snapshot_tree": snapshot_identity["tree"],
            "campaign_plan_sha256": plan_sha256,
            "preexisting_source_import_receipt_sha256": (
                source_import_evidence["receipt_sha256"]
                if source_import_evidence is not None
                else None
            ),
        },
        "task_count": len(profiles),
        "task_ids": sorted(profiles),
        "profiles": profiles,
        # This is the only portion embedded into the experiment overlay.  Its
        # exact wrapper/path is controlled by runtime_contract.frozen_replay.
        "replay_payload": replay_payload,
    }
    artifact["profiles_sha256"] = canonical_sha256(replay_entries)
    artifact["artifact_sha256"] = canonical_sha256(artifact)
    atomic_write_json(destination, artifact)
    return artifact


def linear_type7_quantile(values: Sequence[int], q: float) -> float:
    if not values:
        raise ControllerError("cannot calculate a quantile of an empty sequence")
    if not 0.0 <= q <= 1.0:
        raise ControllerError("quantile must be between zero and one")
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    h = (len(ordered) - 1) * q
    lower = math.floor(h)
    upper = math.ceil(h)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (h - lower) * (ordered[upper] - ordered[lower])


def derive_analyzer_p99_receipt(
    artifact: Mapping[str, Any],
    *,
    destination: Path,
    plan_sha256: str,
) -> dict[str, Any]:
    profiles = artifact.get("profiles")
    if not isinstance(profiles, Mapping):
        raise ControllerError("Analyzer artifact profiles are missing")
    eligible_task_ids = sorted(
        str(task_id)
        for task_id, row in profiles.items()
        if isinstance(row, Mapping) and row.get("origin_outcome") == "live_success"
    )
    excluded_task_ids = sorted(set(str(task_id) for task_id in profiles) - set(eligible_task_ids))
    values = [
        int(profiles[task_id]["final_successful_physical_attempt_output_tokens"])
        for task_id in eligible_task_ids
    ]
    if len(values) < MIN_ANALYZER_P99_LIVE_OBSERVATIONS:
        raise ControllerError(
            "Analyzer p99 has too few eligible live-success observations: "
            f"need {MIN_ANALYZER_P99_LIVE_OBSERVATIONS}, got {len(values)}"
        )
    p99 = linear_type7_quantile(values, 0.99)
    derived = {
        "P0.5-06-E1": math.floor(0.8 * p99),
        "P0.5-06-E2": math.ceil(1.1 * p99),
    }
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "kind": "analyzer-output-token-p99-derivation",
        "created_at": utc_now(),
        "campaign_plan_sha256": plan_sha256,
        "source_artifact_sha256": artifact["artifact_sha256"],
        "method": "Hyndman-Fan linear type 7; h=(n-1)*q; q=0.99",
        "observation_unit": "final successful physical Analyzer attempt per task",
        "eligibility": {
            "required_origin_outcome": "live_success",
            "minimum_eligible_denominator": MIN_ANALYZER_P99_LIVE_OBSERVATIONS,
            "source_task_count": len(profiles),
            "eligible_denominator": len(eligible_task_ids),
            "excluded_denominator": len(excluded_task_ids),
            "eligible_task_ids": eligible_task_ids,
            "excluded_task_ids": excluded_task_ids,
            "excluded_reasons_by_task": {
                task_id: str(profiles[task_id].get("origin_outcome") or "unknown")
                for task_id in excluded_task_ids
            },
        },
        "ordered_output_tokens": sorted(values),
        "p99": p99,
        "formulae": {
            "P0.5-06-E1": "floor(0.8 * p99)",
            "P0.5-06-E2": "ceil(1.1 * p99)",
        },
        "derived_max_output_tokens": derived,
    }
    receipt["receipt_sha256"] = canonical_sha256(receipt)
    atomic_write_json(destination, receipt)
    return receipt


def make_replay_overlay(plan: Mapping[str, Any], artifact: Mapping[str, Any]) -> dict[str, Any]:
    contract = frozen_replay_contract(plan)
    mode_path = contract["mode_path"]
    payload_path = contract["payload_path"]
    payload_key = str(contract.get("artifact_projection_key", "replay_payload"))
    if payload_key not in artifact:
        raise ControllerError(f"frozen artifact lacks projection {payload_key!r}")
    payload = artifact[payload_key]
    if not isinstance(payload, Mapping) or payload.get("schema") != contract["schema"]:
        raise ControllerError("frozen artifact replay schema differs from campaign plan")
    overlay: dict[str, Any] = {}
    set_path(overlay, payload_path, payload)
    set_path(overlay, mode_path, contract["mode_value"])
    return overlay


def make_noop_temperature_receipt(
    plan: Mapping[str, Any],
    *,
    snapshot: Path,
    snapshot_identity: Mapping[str, str],
    destination: Path,
    plan_sha256: str,
) -> dict[str, Any]:
    base_config = snapshot / str(plan["paths"]["experiment_config_relative"])
    sys.path.insert(0, str(snapshot / "src"))
    try:
        from opensquilla.provider.compat_policy import compat_policy_for_kind  # type: ignore
        from opensquilla.provider.ensemble import (  # type: ignore
            openrouter_static_capabilities,
        )
        from opensquilla.provider.openai import _should_send_temperature  # type: ignore
        from opensquilla.provider.ranking_router import ranking_config_resolution  # type: ignore
        from opensquilla.provider.registry import get_provider_spec  # type: ignore
        from opensquilla.provider.types import ChatConfig  # type: ignore
    finally:
        sys.path.pop(0)
    noop = next(row for row in plan["no_op_experiments"] if row["id"] == "P0.5-07")
    model = str(noop["model"])
    provider_kind = str(noop["provider_kind"])
    policy = compat_policy_for_kind(provider_kind)
    ranking_config_path = snapshot / "src/opensquilla/provider/router_dynamic_ranking_config.json"
    provider_spec = get_provider_spec(provider_kind)
    base_url = str(provider_spec.default_base_url or "").strip()
    official_host = str(policy.official_host or "").strip().lower()
    resolved_host = str(urlsplit(base_url).hostname or "").strip().lower()
    if (
        provider_kind != "openrouter"
        or official_host != "openrouter.ai"
        or resolved_host != official_host
    ):
        raise ControllerError("P0.5-07 must remain bound to OpenRouter official host")
    projected: list[dict[str, Any]] = []
    analyzer_configs: list[dict[str, Any]] = []
    for requested_temperature in noop["requested_values"]:
        experiment = load_effective_experiment_config(
            snapshot,
            base_config,
            {
                "router_dynamic_ranking_override": {
                    "task_analyzer": {"temperature": requested_temperature}
                }
            },
        )
        resolution = ranking_config_resolution(
            thinking_assignment_enabled=False,
            override=(experiment.router_dynamic_ranking_override or None),
        )
        effective = resolution.get("effective_config")
        analyzer_config = effective.get("task_analyzer") if isinstance(effective, Mapping) else None
        if not isinstance(analyzer_config, Mapping):
            raise ControllerError("effective ranking config has no task_analyzer")
        analyzer_copy = copy.deepcopy(dict(analyzer_config))
        if (
            analyzer_copy.get("provider") != provider_kind
            or analyzer_copy.get("model") != model
            or float(analyzer_copy.get("temperature")) != float(requested_temperature)
        ):
            raise ControllerError("P0.5-07 overlay differs from effective Analyzer")
        chat_config = ChatConfig(
            temperature=float(requested_temperature),
            thinking=bool(analyzer_copy.get("thinking")),
        )
        sends_temperature = _should_send_temperature(
            policy,
            base_url,
            model,
            chat_config,
            openrouter_static_capabilities(model),
        )
        projected.append(
            {
                "requested_temperature": requested_temperature,
                "production_should_send_temperature": sends_temperature,
                "wire_temperature": requested_temperature if sends_temperature else None,
            }
        )
        analyzer_configs.append(analyzer_copy)
    if any(row["production_should_send_temperature"] for row in projected):
        raise ControllerError("P0.5-07 is no longer a production wire no-op")
    policy_path = snapshot / "src/opensquilla/provider/compat_policy.py"
    openai_path = snapshot / "src/opensquilla/provider/openai.py"
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "kind": "wire-no-op",
        "experiment_id": "P0.5-07",
        "created_at": utc_now(),
        "decision": "deleted_no_live_run",
        "reason_code": "openrouter_compat_omits_temperature",
        "provider_kind": provider_kind,
        "model": model,
        "official_base_url": base_url,
        "official_host": official_host,
        "resolved_base_url_host": resolved_host,
        "requested_values": noop["requested_values"],
        "production_projection": projected,
        "snapshot_commit": snapshot_identity["commit"],
        "snapshot_tree": snapshot_identity["tree"],
        "compat_policy_source_sha256": file_sha256(policy_path),
        "openai_payload_source_sha256": file_sha256(openai_path),
        "ranking_config_source_sha256": file_sha256(ranking_config_path),
        "effective_task_analyzer_configs_sha256": canonical_sha256(analyzer_configs),
        "campaign_plan_sha256": plan_sha256,
    }
    receipt["receipt_sha256"] = canonical_sha256(receipt)
    atomic_write_json(destination, receipt)
    return receipt


def load_effective_experiment_config(
    snapshot: Path,
    base_config: Path,
    overlay: Mapping[str, Any],
) -> Any:
    sys.path.insert(0, str(snapshot / "src"))
    try:
        from opensquilla.eval.draco_experiment_config import (  # type: ignore
            load_draco_experiment_config,
        )

        return load_draco_experiment_config(
            base_config,
            inline_overlay_json=json.dumps(overlay, separators=(",", ":"), sort_keys=True),
        ).config
    finally:
        sys.path.pop(0)


def _isolated_snapshot_json(
    snapshot: Path,
    *,
    program: str,
    payload: Mapping[str, Any],
    label: str,
) -> dict[str, Any]:
    """Run snapshot-sensitive imports in a fresh interpreter."""

    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    for name in list(environment):
        if name.endswith("_API_KEY") or name.endswith("_TOKEN"):
            environment.pop(name, None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", program, str(snapshot.resolve())],
        cwd=snapshot,
        env=environment,
        input=json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False),
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout or "isolated helper failed").strip()
        raise ControllerError(f"{label} failed in frozen snapshot: {detail[-1000:]}")
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ControllerError(f"{label} returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise ControllerError(f"{label} returned a non-object")
    return value


_ISOLATED_EFFECTIVE_CONFIG_PROGRAM = r"""
import hashlib
import json
import sys
from pathlib import Path

snapshot = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(snapshot / "src"))
from opensquilla.eval import draco_experiment_config as module

module_path = Path(module.__file__).resolve()
module_path.relative_to((snapshot / "src").resolve())
request = json.loads(sys.stdin.read())
loaded = module.load_draco_experiment_config(
    Path(request["base_config"]),
    inline_overlay_json=json.dumps(
        request["overlay"], separators=(",", ":"), sort_keys=True
    ),
).config
print(json.dumps({
    "config": loaded.model_dump(mode="json"),
    "module_path": str(module_path),
    "module_sha256": hashlib.sha256(module_path.read_bytes()).hexdigest(),
}, sort_keys=True, separators=(",", ":"), allow_nan=False))
"""


def load_effective_experiment_config_isolated(
    snapshot: Path,
    base_config: Path,
    overlay: Mapping[str, Any],
) -> dict[str, Any]:
    result = _isolated_snapshot_json(
        snapshot,
        program=_ISOLATED_EFFECTIVE_CONFIG_PROGRAM,
        payload={"base_config": str(base_config.resolve()), "overlay": dict(overlay)},
        label="effective experiment config",
    )
    config = result.get("config")
    if not isinstance(config, Mapping):
        raise ControllerError("isolated effective experiment config is malformed")
    return copy.deepcopy(dict(config))


_ISOLATED_FALLBACK_VALIDATION_PROGRAM = r"""
import hashlib
import json
import sys
from pathlib import Path

snapshot = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(snapshot / "src"))
from opensquilla.provider import ranking_router as module

module_path = Path(module.__file__).resolve()
module_path.relative_to((snapshot / "src").resolve())
request = json.loads(sys.stdin.read())
derived = module.fallback_task_profile(
    routed_tier=request["routed_tier"],
    request_context=request["request_context"],
    ranking_config=request["ranking_config"],
)
normalized, schema_valid, warnings = module.normalize_task_profile(
    request["profile"],
    routed_tier=request["routed_tier"],
    request_context=request["request_context"],
    ranking_config=request["ranking_config"],
)
print(json.dumps({
    "derived": derived,
    "normalized": normalized,
    "schema_valid": schema_valid,
    "normalization_warnings": warnings,
    "analyzer_version": module.TASK_ANALYZER_VERSION,
    "module_path": str(module_path),
    "module_sha256": hashlib.sha256(module_path.read_bytes()).hexdigest(),
}, sort_keys=True, separators=(",", ":"), allow_nan=False))
"""


def validate_fallback_profile_isolated(
    snapshot: Path,
    *,
    profile: Mapping[str, Any],
    routed_tier: str,
    request_context: Mapping[str, Any],
    ranking_config: Mapping[str, Any],
) -> dict[str, Any]:
    return _isolated_snapshot_json(
        snapshot,
        program=_ISOLATED_FALLBACK_VALIDATION_PROGRAM,
        payload={
            "profile": dict(profile),
            "routed_tier": routed_tier,
            "request_context": dict(request_context),
            "ranking_config": dict(ranking_config),
        },
        label="deterministic Analyzer fallback validation",
    )


_RUNTIME_HELPERS: dict[str, dict[str, Any]] = {}


def _runtime_helpers(snapshot: Path) -> dict[str, Any]:
    cache_key = str(snapshot.resolve())
    if cache_key in _RUNTIME_HELPERS:
        return _RUNTIME_HELPERS[cache_key]
    sys.path.insert(0, str(snapshot / "src"))
    try:
        from opensquilla.provider.aggregator_prompt import (  # type: ignore
            aggregator_prompt_version_evidence,
        )
        from opensquilla.provider.compat_policy import compat_policy_for_kind  # type: ignore
        from opensquilla.provider.ensemble import (  # type: ignore
            EnsembleMemberConfig,
            _aggregator_chat_config,
            _member_max_tokens,
            _member_model_capabilities,
            _proposer_chat_config,
            build_ensemble_provider_from_config,
        )
        from opensquilla.provider.model_catalog import shared_catalog  # type: ignore
        from opensquilla.provider.openai import _should_send_temperature  # type: ignore
        from opensquilla.provider.ranking_router import (  # type: ignore
            FROZEN_TASK_ANALYSIS_SCHEMAS,
            TASK_ANALYZER_VERSION,
            fallback_task_profile,
            normalize_task_profile,
        )
        from opensquilla.provider.registry import get_provider_spec  # type: ignore
        from opensquilla.provider.selector import ProviderConfig  # type: ignore
        from opensquilla.provider.types import ChatConfig  # type: ignore

        module_name = "_p0_p05_draco_runner_" + hashlib.sha256(cache_key.encode()).hexdigest()[:12]
        runner_path = snapshot / "scripts/run_draco_routing_experiment.py"
        spec = importlib.util.spec_from_file_location(module_name, runner_path)
        if spec is None or spec.loader is None:
            raise ControllerError("cannot load production DRACO runner helpers")
        runner = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = runner
        spec.loader.exec_module(runner)
    finally:
        sys.path.pop(0)
    required_signatures = {
        "_aggregator_chat_config": (
            _aggregator_chat_config,
            {
                "base",
                "member",
                "max_tokens_cap",
                "visible_answer_reserve_tokens",
                "recovery",
                "request_budget_binding",
            },
        ),
        "_proposer_chat_config": (
            _proposer_chat_config,
            {
                "base",
                "member",
                "max_tokens_cap",
                "visible_answer_reserve_tokens",
                "max_tokens_cap_explicit",
                "request_budget_binding",
            },
        ),
        "_should_send_temperature": (
            _should_send_temperature,
            {"policy", "base_url", "model", "cfg", "caps"},
        ),
    }
    for label, (function, expected_parameters) in required_signatures.items():
        actual_parameters = set(inspect.signature(function).parameters)
        if not expected_parameters.issubset(actual_parameters):
            raise ControllerError(
                f"production helper {label} signature drifted: {sorted(actual_parameters)}"
            )
    build_signature = inspect.signature(build_ensemble_provider_from_config)
    rebinding_parameter = build_signature.parameters.get("_enable_member_request_budget_rebinding")
    if rebinding_parameter is None or rebinding_parameter.default is not False:
        raise ControllerError("production member request-budget binding default drifted")
    build_source = inspect.getsource(runner.build_experiment_provider)
    build_tree = ast.parse(textwrap.dedent(build_source))
    builder_calls = [
        node
        for node in ast.walk(build_tree)
        if isinstance(node, ast.Call)
        and (
            isinstance(node.func, ast.Name)
            and node.func.id == "build_ensemble_provider_from_config"
            or isinstance(node.func, ast.Attribute)
            and node.func.attr == "build_ensemble_provider_from_config"
        )
    ]
    if not builder_calls:
        raise ControllerError("production DRACO runner no longer invokes the ensemble builder")
    draco_binding_disabled = True
    for call in builder_calls:
        keyword = next(
            (
                item
                for item in call.keywords
                if item.arg == "_enable_member_request_budget_rebinding"
            ),
            None,
        )
        if keyword is not None and not (
            isinstance(keyword.value, ast.Constant) and keyword.value.value is False
        ):
            draco_binding_disabled = False
    helpers = {
        "EnsembleMemberConfig": EnsembleMemberConfig,
        "ProviderConfig": ProviderConfig,
        "ChatConfig": ChatConfig,
        "aggregator_chat_config": _aggregator_chat_config,
        "proposer_chat_config": _proposer_chat_config,
        "member_max_tokens": _member_max_tokens,
        "member_model_capabilities": _member_model_capabilities,
        "shared_catalog": shared_catalog,
        "aggregator_prompt_version_evidence": (aggregator_prompt_version_evidence),
        "should_send_temperature": _should_send_temperature,
        "compat_policy_for_kind": compat_policy_for_kind,
        "get_provider_spec": get_provider_spec,
        "task_analyzer_version": TASK_ANALYZER_VERSION,
        "frozen_task_analysis_schemas": FROZEN_TASK_ANALYSIS_SCHEMAS,
        "fallback_task_profile": fallback_task_profile,
        "normalize_task_profile": normalize_task_profile,
        "runner": runner,
        "draco_request_budget_rebinding_disabled": draco_binding_disabled,
        "ensemble_source_sha256": file_sha256(snapshot / "src/opensquilla/provider/ensemble.py"),
        "openai_source_sha256": file_sha256(snapshot / "src/opensquilla/provider/openai.py"),
        "runner_source_sha256": file_sha256(snapshot / "scripts/run_draco_routing_experiment.py"),
        "aggregator_prompt_source_sha256": file_sha256(
            snapshot / "src/opensquilla/provider/aggregator_prompt.py"
        ),
        "ranking_router_source_sha256": file_sha256(
            snapshot / "src/opensquilla/provider/ranking_router.py"
        ),
    }
    _RUNTIME_HELPERS[cache_key] = helpers
    return helpers


def _source_selection_plans(
    trace_path: Path,
    *,
    expected_task_ids: set[str],
    require_dry_replay: bool,
) -> dict[str, Mapping[str, Any]]:
    require_regular_file(trace_path)
    plans: dict[str, Mapping[str, Any]] = {}
    with trace_path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ControllerError(f"invalid routing trace row {line_number}") from exc
            if not isinstance(row, Mapping) or row.get("group") != "G1":
                raise ControllerError(f"routing trace row {line_number} is not G1")
            task_id = str(row.get("task_id") or "")
            if task_id not in expected_task_ids or task_id in plans:
                raise ControllerError(f"routing trace has invalid/duplicate task {task_id!r}")
            routing = row.get("routing_trace")
            if not isinstance(routing, Mapping):
                raise ControllerError(f"routing trace task {task_id} lacks routing evidence")
            if require_dry_replay and routing.get("dry_run") is not True:
                raise ControllerError(f"routing trace task {task_id} is not a dry run")
            selection = routing.get("selection_plan")
            if not isinstance(selection, Mapping):
                raise ControllerError(f"routing trace task {task_id} lacks selection plan")
            if require_dry_replay:
                analyzer = selection.get("task_analyzer")
                replay = analyzer.get("replay") if isinstance(analyzer, Mapping) else None
                if (
                    not isinstance(analyzer, Mapping)
                    or analyzer.get("source") != "frozen_replay"
                    or analyzer.get("usage") != {}
                    or not isinstance(replay, Mapping)
                    or replay.get("physical_request_count") != 0
                ):
                    raise ControllerError(
                        f"dry trace task {task_id} did not materialize zero-call replay"
                    )
            plans[task_id] = copy.deepcopy(dict(selection))
    if set(plans) != expected_task_ids:
        raise ControllerError("routing trace task coverage differs")
    return plans


def _dry_run_output_artifacts(directory: Path) -> tuple[Path, Path]:
    traces = sorted(directory.glob("draco_run_*.trace.jsonl"))
    manifests = sorted(directory.glob("draco_run_*.manifest.json"))
    if len(traces) != 1 or len(manifests) != 1:
        raise ControllerError("offline dry run did not publish one trace and manifest")
    require_regular_file(traces[0])
    require_regular_file(manifests[0])
    manifest = load_json(manifests[0])
    if manifest.get("status") != "complete" or manifest.get("dry_run") is not True:
        # Older manifests carry dry_run only under run_compatibility. The
        # trace-level proof remains mandatory; accept status complete here.
        if manifest.get("status") != "complete":
            raise ControllerError("offline dry run manifest is not complete")
    return traces[0], manifests[0]


def run_main_dry_replay(
    plan: Mapping[str, Any],
    *,
    snapshot: Path,
    snapshot_identity: Mapping[str, str],
    overlay: Mapping[str, Any],
    expected_task_ids: set[str],
    label: str,
) -> tuple[dict[str, Mapping[str, Any]], dict[str, Any]]:
    """Run the production main runner in its zero-network dry-run mode."""

    validate_runtime_freeze(
        plan,
        snapshot=snapshot,
        expected_snapshot_identity=snapshot_identity,
    )
    overlay_sha = canonical_sha256(overlay)
    safe_label = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-") or "overlay"
    output_dir = (
        Path(str(plan["paths"]["run_root"]))
        / "offline-dry-runs"
        / f"{safe_label}-{overlay_sha[:16]}-{os.getpid()}-{datetime.now().strftime('%H%M%S%f')}"
    )
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    reference_repo = Path(str(plan["paths"]["reference_repo"])).resolve()
    command = [
        str(plan["paths"]["python"]),
        str(snapshot / "scripts/run_draco_routing_experiment.py"),
        "--input",
        str(reference_repo / "data/draco/mini.jsonl"),
        "--config",
        str(reference_repo / ".local-state/config.toml"),
        "--experiment-config",
        str(snapshot / str(plan["paths"]["experiment_config_relative"])),
        "--experiment-config-override-json",
        json.dumps(overlay, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        "--experiment-config-set",
        "runner.concurrency=6",
        "--groups",
        "G1",
        "--max-tasks",
        "10",
        "--output-dir",
        str(output_dir),
        "--dry-run",
        "--require-openrouter-non-byok",
        "--require-clean-source",
    ]
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONPATH": str(snapshot / "src"),
            "OPENSQUILLA_REPO": str(snapshot),
            "OPENSQUILLA_REFERENCE_REPO": str(reference_repo),
            "OPENSQUILLA_TRUST_ENV": "0",
            # A deliberately non-secret offline sentinel makes deployment
            # availability match the paid OpenRouter route without ever being
            # sent: --dry-run constructs only DryProvider instances.
            "OPENROUTER_API_KEY": "sk-or-v1-" + ("0" * 64),
            "BRAVE_API_KEY": "",
            "BRAVE_SEARCH_API_KEY": "",
            "FIRECRAWL_API_KEY": "",
        }
    )
    completed = subprocess.run(
        command,
        cwd=snapshot,
        env=environment,
        check=False,
        text=True,
        capture_output=True,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()[-2000:]
        raise ControllerError(
            f"offline main-runner dry replay failed ({completed.returncode}): {detail}"
        )
    trace_path, manifest_path = _dry_run_output_artifacts(output_dir)
    plans = _source_selection_plans(
        trace_path,
        expected_task_ids=expected_task_ids,
        require_dry_replay=True,
    )
    evidence = {
        "status": "complete",
        "attempted": True,
        "output_dir": str(output_dir),
        "overlay_sha256": overlay_sha,
        "trace_path": str(trace_path),
        "trace_raw_sha256": file_sha256(trace_path),
        "manifest_path": str(manifest_path),
        "manifest_raw_sha256": file_sha256(manifest_path),
        "command_projection_sha256": canonical_sha256(
            [item for item in command if not item.startswith("sk-or-v1-")]
        ),
        "network_contract": "main runner --dry-run; zero Analyzer/provider/Judge calls",
        "task_count": len(plans),
    }
    return plans, evidence


def _parse_identity(identity: Any) -> tuple[str, str]:
    provider, separator, model = str(identity or "").strip().partition(":")
    if separator != ":" or not provider or not model:
        raise ControllerError(f"invalid selected model identity: {identity!r}")
    return provider, model


def _ordered_member_refs(selection: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    raw_selected_p = selection.get("selected_P")
    raw_backup_p = selection.get("backup_P")
    raw_aggregator_candidates = selection.get("aggregator_candidates")
    if not isinstance(raw_selected_p, list) or not all(
        isinstance(value, str) and value for value in raw_selected_p
    ):
        raise ControllerError("selection plan lacks ordered selected_P")
    if not isinstance(raw_backup_p, list) or not all(
        isinstance(value, str) and value for value in raw_backup_p
    ):
        raise ControllerError("selection plan lacks ordered backup_P")
    if not isinstance(raw_aggregator_candidates, list) or not all(
        isinstance(value, str) and value for value in raw_aggregator_candidates
    ):
        raise ControllerError("selection plan lacks ordered aggregator_candidates")
    selected_p = list(raw_selected_p)
    selected_a = str(selection.get("selected_A") or "")
    backup_p = list(raw_backup_p)
    aggregator_candidates = list(raw_aggregator_candidates)
    if not selected_p or not selected_a:
        raise ControllerError("selection plan lacks selected P/A")
    if not aggregator_candidates or aggregator_candidates[0] != selected_a:
        raise ControllerError("aggregator recovery order does not start with selected_A")
    rows: list[tuple[str, str, str]] = []
    for index, identity in enumerate(selected_p, start=1):
        provider, model = _parse_identity(identity)
        rows.append(("proposer", provider, model))
    provider, model = _parse_identity(selected_a)
    rows.append(("aggregator", provider, model))
    for identity in backup_p:
        provider, model = _parse_identity(identity)
        rows.append(("proposer_backup", provider, model))
    for identity in aggregator_candidates[1:]:
        provider, model = _parse_identity(identity)
        rows.append(("aggregator_fallback", provider, model))
    return rows


def _validated_aggregator_prompt(
    selection: Mapping[str, Any],
    *,
    helpers: Mapping[str, Any],
) -> dict[str, Any]:
    evidence = selection.get("aggregator_prompt")
    ranking_parameters = selection.get("ranking_parameters")
    aggregator_policy = (
        ranking_parameters.get("aggregator") if isinstance(ranking_parameters, Mapping) else None
    )
    version = str(evidence.get("version") or "") if isinstance(evidence, Mapping) else ""
    if version not in AGGREGATOR_PROMPT_VERSIONS or not isinstance(evidence, Mapping):
        raise ControllerError("selection plan lacks a versioned Aggregator prompt")
    if isinstance(aggregator_policy, Mapping) and "prompt_version" in aggregator_policy:
        declared_version = str(aggregator_policy.get("prompt_version") or "")
        if declared_version != version:
            raise ControllerError(
                "selection plan Aggregator prompt version differs from ranking parameters"
            )
    expected = helpers["aggregator_prompt_version_evidence"](version)
    if (
        set(evidence) != {"schema", "version", "description", "additional_instructions", "sha256"}
        or dict(evidence) != expected
        or evidence.get("schema") != AGGREGATOR_PROMPT_SCHEMA
        or SHA256_RE.fullmatch(str(evidence.get("sha256") or "")) is None
    ):
        raise ControllerError("Aggregator prompt evidence differs from production code")
    detached = {key: value for key, value in evidence.items() if key != "sha256"}
    if evidence.get("sha256") != canonical_sha256(detached):
        raise ControllerError("Aggregator prompt evidence hash differs")
    return copy.deepcopy(dict(evidence))


def _selection_int(
    selection: Mapping[str, Any],
    key: str,
    *,
    minimum: int = 0,
) -> int:
    value = selection.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ControllerError(f"selection plan has invalid {key}")
    return value


def _selected_proposer_cardinality(
    selection: Mapping[str, Any],
) -> tuple[list[str], int, int, int]:
    raw_selected = selection.get("selected_P")
    if not isinstance(raw_selected, list) or not all(
        isinstance(identity, str) and identity for identity in raw_selected
    ):
        raise ControllerError("selection plan lacks ordered selected_P")
    selected = list(raw_selected)
    normalized = [
        (provider.lower(), model.lower())
        for provider, model in (_parse_identity(identity) for identity in selected)
    ]
    if len(normalized) != len(set(normalized)):
        raise ControllerError("selection plan selected_P identities must be unique")
    n_min = _selection_int(selection, "N_min", minimum=1)
    n_max = _selection_int(selection, "N_max", minimum=n_min)
    proposer_count = _selection_int(selection, "proposer_count", minimum=1)
    if len(selected) != proposer_count:
        raise ControllerError("selection plan selected_P/proposer_count differs")
    if not n_min <= proposer_count <= n_max:
        raise ControllerError("selection plan proposer_count is outside N_min/N_max")
    return selected, n_min, n_max, proposer_count


def _normalized_declared_member_generation(
    selection: Mapping[str, Any],
) -> list[dict[str, Any]] | None:
    """Normalize optional live-plan member declarations for cross-checking.

    The production dry provider does not instantiate generation members, so
    its trace may omit these rows.  A live plan that does publish them is
    checked against the helper-derived request projection below.
    """

    primary = selection.get("member_generation")
    recovery = selection.get("recovery_member_generation")
    if primary is None and recovery is None:
        return None
    if not isinstance(primary, list) or not isinstance(recovery, list):
        raise ControllerError("selection plan member generation roster is incomplete")
    expanded: list[dict[str, Any]] = []
    for raw in [*primary, *recovery]:
        if not isinstance(raw, Mapping):
            raise ControllerError("selection plan has malformed member generation row")
        role = str(raw.get("role") or "")
        provider = str(raw.get("provider") or "")
        model = str(raw.get("model") or "")
        k = raw.get("k", 1)
        if (
            role not in {"proposer", "aggregator", "proposer_backup", "aggregator_fallback"}
            or not provider
            or not model
            or isinstance(k, bool)
            or not isinstance(k, int)
            or k <= 0
        ):
            raise ControllerError("selection plan member generation identity is invalid")
        for _ in range(k):
            expanded.append(
                {
                    "role": role,
                    "identity": f"{provider}:{model}",
                    "temperature": raw.get("temperature"),
                    "max_tokens": raw.get("max_tokens"),
                    "thinking": raw.get("thinking"),
                    "thinking_budget_tokens": raw.get("thinking_budget_tokens"),
                }
            )
    return expanded


def _generation_policy(config: Any, runner: Any) -> dict[str, Any]:
    generation = config.generation
    return {
        "generation_thinking": runner.DEFAULT_GENERATION_THINKING,
        "temperature": generation.temperature,
        "thinking_enabled": bool(generation.thinking_enabled),
        "thinking_level": "model-specific",
        "default_thinking_level": str(generation.default_thinking_level),
        "thinking_budget_tokens": int(generation.thinking_budget_tokens),
        "max_thinking_budget_tokens": int(generation.thinking_budget_tokens),
        "max_tokens": int(generation.max_tokens),
        "max_tokens_overridden": True,
        "model_thinking_levels": dict(generation.model_thinking_levels),
        "require_highest_thinking": bool(generation.require_highest_thinking),
        "applies_to": "single baselines and ensemble members",
    }


def _model_capabilities_projection(value: Any) -> dict[str, Any]:
    return {
        "supports_reasoning": bool(getattr(value, "supports_reasoning", False)),
        "supports_tools": bool(getattr(value, "supports_tools", False)),
        "supports_streaming": bool(getattr(value, "supports_streaming", False)),
        "supports_vision": bool(getattr(value, "supports_vision", False)),
        "reasoning_format": str(getattr(value, "reasoning_format", "none") or "none"),
    }


def _candidate_order_seed_projection(
    ensemble: Any,
    selection: Mapping[str, Any],
    *,
    task_id: str,
) -> dict[str, int | None]:
    configured = ensemble.candidate_order_seed
    effective = configured if ensemble.shuffle_candidates else None
    if (
        selection.get("configured_candidate_order_seed") != configured
        or selection.get("effective_candidate_order_seed") != effective
    ):
        raise ControllerError(f"task {task_id} candidate order seed differs from production config")
    return {
        "configured_candidate_order_seed": configured,
        "effective_candidate_order_seed": effective,
    }


def _aggregator_budget_projection(
    *,
    helpers: Mapping[str, Any],
    base: Any,
    member: Any,
    resolved: Any,
    configured_cap: int,
    configured_reserve: int,
    recovery: bool,
) -> dict[str, Any]:
    """Expose the inputs and result of production `_aggregator_chat_config`."""

    member_max = max(0, int(member.max_tokens or 0))
    default_request_max = max(1, int(helpers["ChatConfig"]().max_tokens))
    base_max = max(0, int(getattr(base, "max_tokens", 0) or 0))
    explicit_request_max = base_max if member_max <= 0 and base_max > default_request_max else 0
    configured_max = max(
        1,
        int(helpers["member_max_tokens"](member)),
        explicit_request_max,
    )
    capability_max = configured_max
    capability_source = "configured"
    try:
        catalog_max, catalog_source = helpers["shared_catalog"]().resolve_max_tokens_with_source(
            member.provider_config.model,
            user_override=0,
            provider=member.provider_config.provider,
        )
        if catalog_source in {"catalog", "override"} and int(catalog_max or 0) > 0:
            capability_max = int(catalog_max)
            capability_source = str(catalog_source)
    except Exception:  # noqa: BLE001 - mirrors the production conservative branch
        pass
    expansion_cap = max(1, min(max(1, configured_cap), capability_max))
    normalized_reserve = min(
        max(1, configured_reserve),
        max(1, expansion_cap - 1),
    )
    effective_reserve = min(
        normalized_reserve,
        max(1, int(resolved.max_tokens) // 2),
    )
    return {
        "configured_member_max_tokens": member_max,
        "configured_generation_max_tokens": base_max,
        "configured_max_tokens": configured_max,
        "configured_max_tokens_cap": configured_cap,
        "capability_max_tokens": capability_max,
        "capability_source": capability_source,
        "trusted_catalog_ceiling": capability_source in {"catalog", "override"},
        "configured_visible_answer_reserve_tokens": configured_reserve,
        "normalized_visible_answer_reserve_tokens": normalized_reserve,
        "effective_visible_answer_reserve_tokens": effective_reserve,
        "effective_max_tokens": int(resolved.max_tokens),
        "effective_thinking_budget_tokens": (
            int(resolved.thinking_budget_tokens or 0) if resolved.thinking else 0
        ),
        "recovery": recovery,
    }


def request_visible_selection_projection(
    *,
    snapshot: Path,
    config: Any,
    selections: Mapping[str, Mapping[str, Any]],
    max_tokens_cap_explicit: bool,
) -> dict[str, Any]:
    cardinalities: dict[str, tuple[list[str], int, int, int]] = {}
    for task_id, selection in sorted(selections.items()):
        if not isinstance(selection, Mapping):
            raise ControllerError(f"task {task_id} has no selection mapping")
        cardinalities[task_id] = _selected_proposer_cardinality(selection)
    helpers = _runtime_helpers(snapshot)
    runner = helpers["runner"]
    policy = _generation_policy(config, runner)
    projected: dict[str, Any] = {}
    for task_id, selection in sorted(selections.items()):
        if not isinstance(selection, Mapping):
            raise ControllerError(f"task {task_id} has no selection mapping")
        prompt_evidence = _validated_aggregator_prompt(selection, helpers=helpers)
        member_refs = _ordered_member_refs(selection)
        selected_p, n_min, n_max, proposer_count = cardinalities[task_id]
        effective_quorum = _selection_int(
            selection,
            "effective_min_successful_proposers",
            minimum=1,
        )
        recovery_policy = selection.get("proposer_recovery_policy")
        if not isinstance(recovery_policy, Mapping):
            raise ControllerError("selection plan lacks proposer recovery policy")
        formal_policy = runner.formal_proposer_recovery_policy_for_plan(selection)
        provider_native = formal_policy is not None and dict(recovery_policy) == formal_policy
        legal_quorum = 2 if provider_native else int(runner.legal_proposer_quorum(len(selected_p)))
        if effective_quorum != legal_quorum:
            raise ControllerError("selection plan effective/legal quorum differs")
        if selection.get("legal_min_successful_proposers") not in (None, legal_quorum):
            raise ControllerError("selection plan declared legal quorum differs")
        legal_quorum_policy = "fixed_2_provider_native" if provider_native else "ceil(2*n/3)"
        if selection.get("legal_quorum_policy") not in (None, legal_quorum_policy):
            raise ControllerError("selection plan legal quorum policy differs")
        declared_generation = _normalized_declared_member_generation(selection)
        member_rows: list[dict[str, Any]] = []
        declared_projection: list[dict[str, Any]] = []
        for index, (role, provider, model) in enumerate(member_refs, start=1):
            base = runner.generation_chat_config(policy, model=model)
            thinking = runner.generation_thinking_for_model(model, policy)
            spec = helpers["get_provider_spec"](provider)
            member = helpers["EnsembleMemberConfig"](
                provider_config=helpers["ProviderConfig"](
                    provider=provider,
                    model=model,
                    base_url=spec.default_base_url,
                ),
                label=f"{role}_{index}",
                temperature=config.generation.temperature,
                max_tokens=int(config.generation.max_tokens),
                thinking=thinking,
                k=1,
            )
            if role.startswith("aggregator"):
                configured_cap = int(config.ensemble.aggregator_max_tokens_cap)
                configured_reserve = int(config.ensemble.aggregator_visible_answer_reserve_tokens)
                resolved = helpers["aggregator_chat_config"](
                    base,
                    member,
                    max_tokens_cap=configured_cap,
                    visible_answer_reserve_tokens=configured_reserve,
                    recovery=role == "aggregator_fallback",
                    request_budget_binding=None,
                    record_budget_rebound=False,
                )
                budget = _aggregator_budget_projection(
                    helpers=helpers,
                    base=base,
                    member=member,
                    resolved=resolved,
                    configured_cap=configured_cap,
                    configured_reserve=configured_reserve,
                    recovery=role == "aggregator_fallback",
                )
            else:
                resolved, budget = helpers["proposer_chat_config"](
                    base,
                    member,
                    max_tokens_cap=int(config.ensemble.proposer_max_tokens_cap),
                    visible_answer_reserve_tokens=int(
                        config.ensemble.proposer_visible_answer_reserve_tokens
                    ),
                    max_tokens_cap_explicit=max_tokens_cap_explicit,
                    request_budget_binding=None,
                )
            compat_policy = helpers["compat_policy_for_kind"](provider)
            base_url = str(spec.default_base_url or "")
            sends_temperature = helpers["should_send_temperature"](
                compat_policy,
                base_url,
                model,
                resolved,
                helpers["member_model_capabilities"](member),
            )
            capabilities = helpers["member_model_capabilities"](member)
            declared_projection.append(
                {
                    "role": role,
                    "identity": f"{provider}:{model}",
                    "temperature": member.temperature,
                    "max_tokens": member.max_tokens,
                    "thinking": member.thinking,
                    "thinking_budget_tokens": int(base.thinking_budget_tokens or 0),
                }
            )
            member_rows.append(
                {
                    "ordinal": index,
                    "role": role,
                    "identity": f"{provider}:{model}",
                    "max_tokens": int(resolved.max_tokens),
                    "thinking": bool(resolved.thinking),
                    "thinking_level": (
                        str(resolved.thinking_level)
                        if resolved.thinking_level is not None
                        else None
                    ),
                    "thinking_budget_tokens": int(
                        resolved.thinking_budget_tokens if resolved.thinking else 0
                    ),
                    "thinking_budget_explicit": bool(resolved.thinking_budget_explicit),
                    "request_visible_temperature": resolved.temperature,
                    "temperature_parameter_sent": bool(sends_temperature),
                    "wire_temperature": (resolved.temperature if sends_temperature else None),
                    "tools_enabled": bool(
                        config.ensemble.proposer_tools
                        if role.startswith("proposer")
                        else config.ensemble.aggregator_tools
                    ),
                    "base_url_host": str(urlsplit(base_url).hostname or "").lower(),
                    "compat_official_host": str(compat_policy.official_host or "").lower(),
                    "model_capabilities": _model_capabilities_projection(capabilities),
                    "member_request_budget_rebinding": False,
                    "effective_context_window_tokens": None,
                    "effective_context_window_source": "unbound",
                    "provider_request_max_chars": int(
                        getattr(resolved, "provider_request_max_chars", 0) or 0
                    ),
                    "provider_request_max_chars_source": "inherited",
                    "budget": copy.deepcopy(dict(budget)),
                }
            )
        if declared_generation is not None and declared_generation != declared_projection:
            raise ControllerError(
                f"task {task_id} declared member generation differs from production policy"
            )
        seed_projection = _candidate_order_seed_projection(
            config.ensemble,
            selection,
            task_id=task_id,
        )
        projected[task_id] = {
            "N_min": n_min,
            "N_max": n_max,
            "selected_N": len(selected_p),
            "proposer_count": proposer_count,
            "proposer_sample_count": selection.get("proposer_sample_count"),
            "selected_P": selected_p,
            "selected_A": selection["selected_A"],
            "backup_P": list(selection["backup_P"]),
            "configured_proposer_backup_count": selection.get("configured_proposer_backup_count"),
            "effective_proposer_backup_count": len(selection["backup_P"]),
            "aggregator_candidates": list(selection["aggregator_candidates"]),
            "configured_aggregator_candidate_count": selection.get(
                "configured_aggregator_candidate_count"
            ),
            "effective_aggregator_candidate_count": len(selection["aggregator_candidates"]),
            "effective_min_successful_proposers": effective_quorum,
            "legal_min_successful_proposers": legal_quorum,
            "legal_quorum_policy": legal_quorum_policy,
            "proposer_recovery_policy": copy.deepcopy(dict(recovery_policy)),
            "aggregator_recovery_mode": selection.get("aggregator_recovery_mode"),
            "aggregator_recovery_top_k": selection.get("aggregator_recovery_top_k"),
            "wait_for_all_proposers": selection.get("wait_for_all_proposers"),
            "quorum_grace_seconds": selection.get("quorum_grace_seconds"),
            # This is the actual execution behavior changed by P0.5-36.
            "effective_shuffle_candidates": selection.get("effective_shuffle_candidates"),
            **seed_projection,
            "effective_proposer_timeout_seconds": selection.get(
                "effective_proposer_timeout_seconds"
            ),
            "effective_aggregator_timeout_seconds": selection.get(
                "effective_aggregator_timeout_seconds"
            ),
            "aggregator_serving_chain_timeout_seconds": selection.get(
                "aggregator_serving_chain_timeout_seconds"
            ),
            "all_failed_policy": selection.get("all_failed_policy"),
            "candidate_max_chars": selection.get("candidate_max_chars"),
            "proposer_tools": selection.get("proposer_tools"),
            "aggregator_tools": selection.get("aggregator_tools"),
            "record_candidates": selection.get("record_candidates"),
            "provider_state_replay": selection.get("provider_state_replay"),
            "aggregator_prompt": prompt_evidence,
            "member_request_budget_rebinding": False,
            "proposer_max_tokens_cap_explicit": max_tokens_cap_explicit,
            "member_requests": member_rows,
        }
    return projected


def compare_behavior_projections(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    expected_task_count: int | None = None,
) -> dict[str, Any]:
    if set(baseline) != set(candidate):
        raise ControllerError("baseline/candidate projection task coverage differs")
    if expected_task_count is not None and len(baseline) != expected_task_count:
        raise ControllerError(
            f"behavior projection must contain exactly {expected_task_count} tasks"
        )
    changes = [
        {
            "task_id": task_id,
            "before_sha256": canonical_sha256(baseline[task_id]),
            "after_sha256": canonical_sha256(candidate[task_id]),
        }
        for task_id in sorted(baseline)
        if canonical_bytes(baseline[task_id]) != canonical_bytes(candidate[task_id])
    ]
    identical_count = len(baseline) - len(changes)
    return {
        "changed_task_count": len(changes),
        "byte_identical_task_count": identical_count,
        "expected_task_count": expected_task_count,
        "all_tasks_byte_identical": (
            identical_count == len(baseline)
            and (expected_task_count is None or identical_count == expected_task_count)
        ),
        "changed_tasks": changes,
        "baseline_projection_sha256": canonical_sha256(baseline),
        "candidate_projection_sha256": canonical_sha256(candidate),
    }


def offline_effect_decision(
    *,
    comparisons: Mapping[str, Mapping[str, Any]],
    arm_ids: Sequence[str],
    budget_gated: bool,
    production_budget_projection_complete: bool,
    projection_uncertain: bool,
) -> str:
    """Delete only a proven ten-task byte-identical candidate."""

    if projection_uncertain or not comparisons:
        return "run_conservative_projection_uncertain"
    byte_identical = all(
        comparison.get("all_tasks_byte_identical") is True
        and comparison.get("byte_identical_task_count") == EXPECTED_TASK_COUNT
        for comparison in comparisons.values()
    )
    if not byte_identical:
        return "run"
    if any(arm_id in REQUIRED_REPLICATE_ARMS for arm_id in arm_ids):
        return "run_required_replicate"
    if budget_gated and not production_budget_projection_complete:
        return "run_conservative_unproven_budget_binding"
    return "deleted_no_live_run"


def initialize_status(
    plan: Mapping[str, Any],
    arms: Sequence[Arm],
    *,
    plan_sha256: str,
    snapshot_identity: Mapping[str, str],
) -> dict[str, Any]:
    schedule = plan["execution"]["schedule"]
    schedule_sha256 = canonical_sha256(schedule)
    anchor_by_arm_id = schedule["anchor_by_arm_id"]
    return {
        "schema": STATUS_SCHEMA,
        "run_id": plan["run_id"],
        "campaign_plan_sha256": plan_sha256,
        "snapshot_commit": snapshot_identity["commit"],
        "snapshot_tree": snapshot_identity["tree"],
        "schedule_sha256": schedule_sha256,
        "schedule_mode": schedule["mode"],
        "strict_task_interleaving": schedule["strict_task_interleaving"],
        "phase": "prepared",
        "active_arm": None,
        "active_schedule_ordinal": None,
        "started_at": None,
        "completed_at": None,
        "updated_at": utc_now(),
        "derived_plan": None,
        "arms": {
            arm.arm_id: {
                "experiment_id": arm.experiment_id,
                "variant": arm.variant,
                "replicate": arm.replicate,
                "analyzer_mode": arm.analyzer_mode,
                "control_arm_id": arm.control_arm_id,
                "schedule_ordinal": ordinal,
                "anchor_arm_id": anchor_by_arm_id[arm.arm_id],
                "state": "pending",
                "attempts": [],
                "output_dir": str(output_dir(plan, arm)),
            }
            for ordinal, arm in enumerate(arms, start=1)
        },
        "no_op_experiments": {
            str(row["id"]): {"state": "pending", "receipt": None}
            for row in plan.get("no_op_experiments", [])
        },
        "offline_no_op_arms": {},
    }


def validate_snapshot(plan: Mapping[str, Any]) -> tuple[Path, dict[str, str]]:
    snapshot = Path(str(plan["paths"]["snapshot"])).resolve()
    identity = git_identity(snapshot)
    expected_commit = str(plan["freeze"]["snapshot_commit"])
    expected_tree = str(plan["freeze"]["snapshot_tree"])
    if identity["commit"] != expected_commit:
        raise ControllerError(f"snapshot commit differs: {identity['commit']} != {expected_commit}")
    if identity["tree"] != expected_tree:
        raise ControllerError(f"snapshot tree differs: {identity['tree']} != {expected_tree}")
    if identity["status"]:
        raise ControllerError("formal campaign snapshot is dirty")
    return snapshot, identity


def registry_identities(
    models: Sequence[Any],
    *,
    label: str,
) -> list[str]:
    identities: list[str] = []
    for index, raw in enumerate(models):
        if not isinstance(raw, Mapping):
            raise ControllerError(f"{label} registry model {index} is malformed")
        facts = raw.get("registry_facts")
        source = facts if isinstance(facts, Mapping) else raw
        explicit_identity = str(source.get("identity") or "").strip().lower()
        provider = str(source.get("provider") or "").strip().lower()
        model = str(source.get("model_id") or source.get("model") or "").strip().lower()
        identity = explicit_identity or (f"{provider}:{model}" if provider and model else "")
        try:
            parsed_provider, parsed_model = _parse_identity(identity)
        except ControllerError as exc:
            raise ControllerError(
                f"{label} registry model {index} has no canonical identity"
            ) from exc
        identities.append(f"{parsed_provider.lower()}:{parsed_model.lower()}")
    if len(identities) != len(set(identities)):
        raise ControllerError(f"{label} registry identities are not unique")
    return sorted(identities)


def compute_runtime_freeze_identity(
    plan: Mapping[str, Any],
    *,
    snapshot: Path,
) -> dict[str, Any]:
    """Recompute every mutable input/source identity without exposing content."""

    reference_repo = Path(str(plan["paths"]["reference_repo"])).resolve()
    benchmark_path = reference_repo / "data/draco/mini.jsonl"
    reference_config_path = reference_repo / ".local-state/config.toml"
    launcher_path = snapshot / str(plan["paths"]["launcher_relative"])
    main_runner_path = snapshot / "scripts/run_draco_routing_experiment.py"
    resume_runner_path = snapshot / "scripts/run_draco_routing_experiment_resume.py"
    controller_path = Path(__file__).resolve()
    reporter_path = Path(str(plan["paths"]["reporter"])).resolve()
    registry_contract = plan["freeze"]["model_registry"]
    ranking_contract = plan["freeze"]["ranking_config"]
    registry_path = snapshot / str(registry_contract["path"])
    ranking_path = snapshot / str(ranking_contract["path"])
    for path in (
        benchmark_path,
        reference_config_path,
        launcher_path,
        main_runner_path,
        resume_runner_path,
        controller_path,
        reporter_path,
        registry_path,
        ranking_path,
    ):
        require_regular_file(path)

    sys.path.insert(0, str(snapshot / "src"))
    try:
        from opensquilla.provider.ranking_router import (  # type: ignore
            _legacy_registry_snapshot_projection,
            load_model_registry_snapshot,
            ranking_config_resolution,
        )

        full_registry = load_model_registry_snapshot()
        formal_registry = _legacy_registry_snapshot_projection(full_registry)
        formal_ranking_resolution = ranking_config_resolution(
            thinking_assignment_enabled=False,
        )
    finally:
        sys.path.pop(0)
    packaged_ranking = load_json(ranking_path)
    full_models = full_registry.get("models")
    formal_models = formal_registry.get("models")
    if not isinstance(full_models, list) or not isinstance(formal_models, list):
        raise ControllerError("packaged model registry is malformed")
    full_identities = registry_identities(full_models, label="full")
    formal_identities = registry_identities(formal_models, label="formal")
    formal_ranking = formal_ranking_resolution.get("effective_config")
    if not isinstance(formal_ranking, Mapping):
        raise ControllerError("formal ranking projection is unavailable")
    return {
        "inputs": {
            "benchmark_input_raw_sha256": file_sha256(benchmark_path),
            "reference_config_raw_sha256": file_sha256(reference_config_path),
        },
        "sources": {
            "launcher_raw_sha256": file_sha256(launcher_path),
            "controller_raw_sha256": file_sha256(controller_path),
            "reporter_raw_sha256": file_sha256(reporter_path),
            "main_runner_raw_sha256": file_sha256(main_runner_path),
            "resume_runner_raw_sha256": file_sha256(resume_runner_path),
        },
        "model_registry": {
            "path": str(registry_contract["path"]),
            "raw_sha256": file_sha256(registry_path),
            "full_snapshot_version": str(full_registry.get("snapshot_version") or ""),
            "full_canonical_sha256": canonical_sha256(full_registry),
            "formal_snapshot_version": str(formal_registry.get("snapshot_version") or ""),
            "formal_canonical_sha256": canonical_sha256(formal_registry),
            "model_count": len(full_models),
            "full_model_count": len(full_identities),
            "formal_model_count": len(formal_identities),
            "full_identities_sha256": canonical_sha256(full_identities),
            "formal_identities_sha256": canonical_sha256(formal_identities),
        },
        "ranking_config": {
            "path": str(ranking_contract["path"]),
            "raw_sha256": file_sha256(ranking_path),
            "packaged_schema_version": str(packaged_ranking.get("schema_version") or ""),
            "packaged_config_version": str(packaged_ranking.get("config_version") or ""),
            "packaged_canonical_sha256": canonical_sha256(packaged_ranking),
            "formal_schema_version": str(formal_ranking.get("schema_version") or ""),
            "formal_config_version": str(formal_ranking.get("config_version") or ""),
            "formal_canonical_sha256": canonical_sha256(formal_ranking),
        },
    }


def validate_runtime_freeze(
    plan: Mapping[str, Any],
    *,
    snapshot: Path,
    expected_snapshot_identity: Mapping[str, str],
) -> dict[str, Any]:
    """Fail before every arm if the snapshot or external reference drifted."""

    current_git = git_identity(snapshot)
    if current_git != dict(expected_snapshot_identity):
        raise ControllerError("snapshot identity or cleanliness drifted during campaign")
    actual = compute_runtime_freeze_identity(plan, snapshot=snapshot)
    frozen = plan["freeze"]
    for section in ("inputs", "sources", "model_registry", "ranking_config"):
        expected_section = frozen.get(section)
        if not isinstance(expected_section, Mapping):
            raise ControllerError(f"freeze.{section} is missing")
        if actual[section] != dict(expected_section):
            differing = sorted(
                key
                for key in set(actual[section]) | set(expected_section)
                if actual[section].get(key) != expected_section.get(key)
            )
            raise ControllerError(f"freeze.{section} drifted at: {', '.join(differing)}")
    if actual["inputs"]["benchmark_input_raw_sha256"] != plan["benchmark"]["input_sha256"]:
        raise ControllerError("benchmark input differs from benchmark contract")
    return actual


def assert_bridge_snapshot_unchanged(
    snapshot: Path, expected_snapshot_identity: Mapping[str, str]
) -> None:
    current = git_identity(snapshot)
    if current != dict(expected_snapshot_identity):
        raise ControllerError("confirmatory snapshot changed during bridge operation")


def validate_static_overlays(plan: Mapping[str, Any], arms: Sequence[Arm], snapshot: Path) -> None:
    base = snapshot / str(plan["paths"]["experiment_config_relative"])
    # Frozen-replay fields and p99-derived values do not exist until E0.  Every
    # other sparse overlay is validated against the production schema now.
    for arm in arms:
        if arm.dynamic is not None or arm.analyzer_mode == "frozen_replay":
            continue
        load_effective_experiment_config(snapshot, base, arm.override)


def validate_frozen_replay_runtime_support(
    plan: Mapping[str, Any], helpers: Mapping[str, Any]
) -> None:
    declared_schema = frozen_replay_contract(plan)["schema"]
    supported = helpers.get("frozen_task_analysis_schemas")
    if not isinstance(supported, Iterable) or declared_schema not in supported:
        raise ControllerError(f"snapshot production runtime does not support {declared_schema}")


def resolve_arm_override(
    plan: Mapping[str, Any],
    arm: Arm,
    *,
    artifact: Mapping[str, Any] | None,
    p99_receipt: Mapping[str, Any] | None,
) -> dict[str, Any]:
    override = copy.deepcopy(arm.override)
    if arm.dynamic is not None:
        if p99_receipt is None:
            raise ControllerError(f"{arm.arm_id} requires the p99 receipt")
        if arm.dynamic.get("kind") != "analyzer_p99_scale":
            raise ControllerError(f"unknown dynamic derivation for {arm.arm_id}")
        value = p99_receipt["derived_max_output_tokens"][arm.arm_id]
        set_path(override, arm.dynamic["path"], value)
    if arm.analyzer_mode == "frozen_replay":
        if artifact is None:
            raise ControllerError(f"{arm.arm_id} requires frozen Analyzer profiles")
        override = deep_merge(override, make_replay_overlay(plan, artifact))
    return override


def _write_new_or_identical_json(path: Path, value: Mapping[str, Any]) -> None:
    """Create an immutable bridge artifact, or prove an idempotent repeat."""

    if path.exists() or path.is_symlink():
        existing = load_bound_json(path, label="bridge artifact")
        if existing.value != dict(value):
            raise ControllerError(f"refusing to overwrite differing bridge artifact: {path}")
        return
    secure_atomic_write_json(path, value)


def validate_confirmatory_launcher_support(
    plan: Mapping[str, Any], *, snapshot: Path
) -> dict[str, Any]:
    """Prove launcher support before any cohort/report/index path is created."""

    fd, evidence, _ = open_authenticated_confirmatory_launcher(plan, snapshot=snapshot)
    os.close(fd)
    return evidence


def _assert_fd_still_names_path(fd: int, path: Path, *, label: str) -> None:
    """Detect leaf replacement while retaining the already-authenticated fd."""

    try:
        path_info = os.stat(path, follow_symlinks=False)
        fd_info = os.fstat(fd)
    except OSError as exc:
        raise ControllerError(f"{label} path identity became unavailable") from exc
    if (
        stat.S_ISLNK(path_info.st_mode)
        or not stat.S_ISREG(path_info.st_mode)
        or (path_info.st_dev, path_info.st_ino) != (fd_info.st_dev, fd_info.st_ino)
    ):
        raise ControllerError(f"{label} was replaced after authentication")


def _assert_authenticated_fd_bytes(
    fd: int,
    path: Path,
    *,
    label: str,
    expected_sha256: str,
) -> None:
    _assert_fd_still_names_path(fd, path, label=label)
    before = os.fstat(fd)
    offset = 0
    chunks: list[bytes] = []
    while offset < before.st_size:
        block = os.pread(fd, min(1024 * 1024, before.st_size - offset), offset)
        if not block:
            break
        chunks.append(block)
        offset += len(block)
    after = os.fstat(fd)
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ) or hashlib.sha256(b"".join(chunks)).hexdigest() != expected_sha256:
        raise ControllerError(f"{label} bytes changed after authentication")


def sealed_memfd(payload: bytes, *, label: str, executable: bool) -> int:
    """Copy authenticated bytes into a write/resize-sealed anonymous file."""

    flags = int(getattr(os, "MFD_CLOEXEC", 0x0001)) | int(
        getattr(os, "MFD_ALLOW_SEALING", 0x0002)
    )
    if hasattr(os, "memfd_create"):
        fd = os.memfd_create(label, flags)
    else:
        libc = ctypes.CDLL(None, use_errno=True)
        memfd_create = getattr(libc, "memfd_create", None)
        if memfd_create is None:
            raise ControllerError(
                "sealed memfd support is required for confirmatory execution"
            )
        memfd_create.argtypes = [ctypes.c_char_p, ctypes.c_uint]
        memfd_create.restype = ctypes.c_int
        fd = int(memfd_create(label.encode("utf-8"), flags))
        if fd < 0:
            error = ctypes.get_errno()
            raise ControllerError(
                f"cannot create sealed memfd for {label}: {os.strerror(error)}"
            )
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if written <= 0:
                raise ControllerError(f"cannot populate sealed {label}")
            offset += written
        os.fchmod(fd, 0o700 if executable else 0o400)
        os.fsync(fd)
        seals = (
            int(getattr(fcntl, "F_SEAL_WRITE", 0x0008))
            | int(getattr(fcntl, "F_SEAL_GROW", 0x0004))
            | int(getattr(fcntl, "F_SEAL_SHRINK", 0x0002))
            | int(getattr(fcntl, "F_SEAL_SEAL", 0x0001))
        )
        add_seals = int(getattr(fcntl, "F_ADD_SEALS", 1033))
        get_seals = int(getattr(fcntl, "F_GET_SEALS", 1034))
        fcntl.fcntl(fd, add_seals, seals)
        if int(fcntl.fcntl(fd, get_seals)) & seals != seals:
            raise ControllerError(f"sealed {label} did not retain all required seals")
        os.lseek(fd, 0, os.SEEK_SET)
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_authenticated_confirmatory_launcher(
    plan: Mapping[str, Any], *, snapshot: Path
) -> tuple[int, dict[str, Any], bytes]:
    """Authenticate launcher content and return the exact descriptor to execute."""

    bridge = plan.get("confirmatory_bridge")
    if not isinstance(bridge, Mapping):
        raise ControllerError("run-confirmatory requires a confirmatory-only bridge plan")
    launcher_contract = bridge.get("launcher_contract")
    if not isinstance(launcher_contract, Mapping):
        raise ControllerError("confirmatory bridge launcher contract is missing")
    launcher = snapshot / str(plan.get("paths", {}).get("launcher_relative") or "")
    secure_existing_directory(snapshot, label="confirmatory snapshot")
    try:
        fd, parent_fd, _ = _open_absolute_nofollow(launcher)
    except OSError as exc:
        raise ControllerError("cannot open confirmatory launcher without symlinks") from exc
    os.close(parent_fd)
    try:
        info_before = os.fstat(fd)
        if not stat.S_ISREG(info_before.st_mode):
            raise ControllerError("confirmatory launcher is not a regular file")
        chunks: list[bytes] = []
        while block := os.read(fd, 1024 * 1024):
            chunks.append(block)
        info_after = os.fstat(fd)
        if (
            info_before.st_dev,
            info_before.st_ino,
            info_before.st_size,
            info_before.st_mtime_ns,
            info_before.st_ctime_ns,
        ) != (
            info_after.st_dev,
            info_after.st_ino,
            info_after.st_size,
            info_after.st_mtime_ns,
            info_after.st_ctime_ns,
        ):
            raise ControllerError("confirmatory launcher changed while being authenticated")
        payload = b"".join(chunks)
        if len(payload) != info_before.st_size:
            raise ControllerError("confirmatory launcher size changed while being authenticated")
        raw_sha = hashlib.sha256(payload).hexdigest()
        _assert_authenticated_fd_bytes(
            fd,
            launcher,
            label="confirmatory launcher",
            expected_sha256=raw_sha,
        )
        try:
            source = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ControllerError("confirmatory launcher is not UTF-8") from exc
        if (
            raw_sha != launcher_contract.get("raw_sha256")
            or raw_sha != plan.get("freeze", {}).get("sources", {}).get("launcher_raw_sha256")
        ):
            raise ControllerError("confirmatory launcher raw identity differs")
        flag = str(launcher_contract.get("flag") or "")
        fd_flag = str(launcher_contract.get("fd_flag") or "")
        if (
            flag != "--confirmatory-schedule"
            or fd_flag != "--confirmatory-schedule-fd"
            or flag not in source
            or fd_flag not in source
        ):
            raise ControllerError("frozen launcher does not support --confirmatory-schedule")
        if "run_confirmatory_pair" not in source:
            raise ControllerError("frozen launcher lacks the paired confirmatory entry point")
        os.lseek(fd, 0, os.SEEK_SET)
        return fd, {
            "path": str(launcher),
            "raw_sha256": raw_sha,
            "flag": flag,
            "fd_flag": fd_flag,
            "paired_entry_point": "run_confirmatory_pair",
            "schedule_schema": launcher_contract.get("schedule_schema"),
            "device": info_before.st_dev,
            "inode": info_before.st_ino,
        }, payload
    except BaseException:
        os.close(fd)
        raise


def _load_bridge_screening_derived(
    source: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    provenance = source.get("derived_provenance")
    if not isinstance(provenance, Mapping):
        raise ControllerError("screening source lacks derived provenance")
    derived_path = Path(str(provenance.get("derived_path") or ""))
    analyzer_path = Path(str(provenance.get("analyzer_path") or ""))
    p99_path = Path(str(provenance.get("p99_path") or ""))
    for path, raw_field, label in (
        (derived_path, "derived_file_sha256", "screening derived plan"),
        (analyzer_path, "analyzer_file_sha256", "screening Analyzer artifact"),
        (p99_path, "p99_file_sha256", "screening Analyzer p99 receipt"),
    ):
        require_regular_file(path)
        if file_sha256(path) != provenance.get(raw_field):
            raise ControllerError(f"{label} raw hash drifted")
    derived = load_json(derived_path)
    analyzer = load_json(analyzer_path)
    p99 = load_json(p99_path)
    verify_bare_document_self_hash(
        derived, field="derived_plan_sha256", label="screening derived plan"
    )
    verify_bare_document_self_hash(
        analyzer, field="artifact_sha256", label="screening Analyzer artifact"
    )
    verify_bare_document_self_hash(
        p99, field="receipt_sha256", label="screening Analyzer p99 receipt"
    )
    if (
        derived.get("derived_plan_sha256") != provenance.get("derived_plan_sha256")
        or analyzer.get("artifact_sha256") != provenance.get("analyzer_artifact_sha256")
        or p99.get("receipt_sha256") != provenance.get("p99_receipt_sha256")
        or derived.get("p0_5_06") != p99
    ):
        raise ControllerError("screening derived semantic provenance drifted")
    descriptor = derived.get("frozen_analyzer_artifact")
    if (
        not isinstance(descriptor, Mapping)
        or Path(str(descriptor.get("path") or "")).resolve() != analyzer_path.resolve()
        or descriptor.get("file_sha256") != file_sha256(analyzer_path)
        or descriptor.get("artifact_sha256") != analyzer.get("artifact_sha256")
    ):
        raise ControllerError("screening derived Analyzer binding differs")
    return derived, analyzer, p99


def _bridge_resolved_arm_payloads(
    plan: Mapping[str, Any],
    *,
    screening_source: Mapping[str, Any],
    old_analyzer: Mapping[str, Any],
    old_p99: Mapping[str, Any],
    bridge_analyzer: Mapping[str, Any],
    bridge_p99: Mapping[str, Any],
) -> dict[str, Any]:
    source_plan = load_json(
        Path(str(screening_source.get("screening_plan", {}).get("path") or ""))
    )
    source_arms = validate_plan(source_plan, allow_placeholders=False)
    source_by_id = {arm.arm_id: arm for arm in source_arms}
    new_arms = validate_plan(plan, allow_placeholders=False)
    new_by_id = {arm.arm_id: arm for arm in new_arms}
    survivor_ids = list(plan.get("confirmatory_bridge", {}).get("survivor_arm_ids") or [])
    required_ids = sorted(
        set(survivor_ids)
        | {str(new_by_id[arm_id].control_arm_id) for arm_id in survivor_ids}
    )
    snapshot = Path(str(plan["paths"]["snapshot"])).resolve()
    base_config = snapshot / str(plan["paths"]["experiment_config_relative"])
    result: dict[str, Any] = {}
    for arm_id in required_ids:
        old_arm = source_by_id.get(arm_id)
        new_arm = new_by_id.get(arm_id)
        if old_arm is None or new_arm is None or old_arm != new_arm:
            raise ControllerError(f"bridge arm definition drifted from screening: {arm_id}")
        old_override = resolve_arm_override(
            source_plan,
            old_arm,
            artifact=old_analyzer,
            p99_receipt=old_p99,
        )
        override = resolve_arm_override(
            plan,
            new_arm,
            artifact=bridge_analyzer,
            p99_receipt=bridge_p99,
        )
        if canonical_sha256(override) != canonical_sha256(old_override):
            raise ControllerError(
                f"bridge resealed derived values changed the screening override: {arm_id}"
            )
        # This is schema/config validation only. It neither loads credentials nor
        # calls the Analyzer, generation models, or Judge.
        load_effective_experiment_config(snapshot, base_config, override)
        result[arm_id] = {
            "arm_id": arm_id,
            "experiment_id": old_arm.experiment_id,
            "variant": old_arm.variant,
            "replicate": old_arm.replicate,
            "analyzer_mode": old_arm.analyzer_mode,
            "control_arm_id": old_arm.control_arm_id,
            "override": override,
            "override_sha256": canonical_sha256(override),
            "screening_resolved_override_sha256": canonical_sha256(old_override),
        }
    return result


def build_confirmatory_bridge_documents(
    plan: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, dict[str, Any]]]:
    """Regenerate new-plan-bound Analyzer/derived receipts and all schedules."""

    arms = validate_plan(plan, allow_placeholders=False)
    bridge = plan["confirmatory_bridge"]
    source = validate_confirmatory_bridge_source(plan)
    _, old_analyzer, old_p99 = _load_bridge_screening_derived(source)
    plan_sha = canonical_sha256(plan)
    snapshot_contract = bridge["snapshot_contract"]
    old_replay = old_analyzer.get("replay_payload")
    if not isinstance(old_replay, Mapping):
        raise ControllerError("screening Analyzer artifact lacks replay payload")
    bridge_analyzer: dict[str, Any] = {
        "schema": CONFIRMATORY_BRIDGE_ANALYZER_SCHEMA,
        "campaign_plan_sha256": plan_sha,
        "snapshot_commit": snapshot_contract["commit"],
        "snapshot_tree": snapshot_contract["tree"],
        "task_ids": sorted(plan["benchmark"]["task_ids"]),
        "replay_payload": copy.deepcopy(dict(old_replay)),
        "provenance": {
            "kind": "authenticated_screening_replay_payload",
            "screening_source_evidence_sha256": source["source_evidence_sha256"],
            "screening_analyzer_file_sha256": source["derived_provenance"][
                "analyzer_file_sha256"
            ],
            "screening_analyzer_artifact_sha256": source["derived_provenance"][
                "analyzer_artifact_sha256"
            ],
            "reused_old_receipt_as_current": False,
        },
        "generation_calls": 0,
    }
    bridge_analyzer["artifact_sha256"] = canonical_sha256(bridge_analyzer)
    old_values = old_p99.get("derived_max_output_tokens")
    if not isinstance(old_values, Mapping):
        raise ControllerError("screening Analyzer p99 receipt lacks derived token values")
    bridge_p99: dict[str, Any] = {
        "schema": CONFIRMATORY_BRIDGE_P99_SCHEMA,
        "campaign_plan_sha256": plan_sha,
        "snapshot_commit": snapshot_contract["commit"],
        "snapshot_tree": snapshot_contract["tree"],
        "derived_max_output_tokens": copy.deepcopy(dict(old_values)),
        "provenance": {
            "kind": "authenticated_screening_p99_values_resealed_for_bridge",
            "screening_source_evidence_sha256": source["source_evidence_sha256"],
            "screening_p99_file_sha256": source["derived_provenance"]["p99_file_sha256"],
            "screening_p99_receipt_sha256": source["derived_provenance"][
                "p99_receipt_sha256"
            ],
            "reused_old_receipt_as_current": False,
        },
        "generation_calls": 0,
    }
    bridge_p99["receipt_sha256"] = canonical_sha256(bridge_p99)
    resolved = _bridge_resolved_arm_payloads(
        plan,
        screening_source=source,
        old_analyzer=old_analyzer,
        old_p99=old_p99,
        bridge_analyzer=bridge_analyzer,
        bridge_p99=bridge_p99,
    )
    by_id = {arm.arm_id: arm for arm in arms}
    schedules: dict[str, dict[str, Any]] = {}
    seed_prefix = str(bridge.get("schedule_seed_prefix") or "draco-confirmatory-bridge-v1")
    for candidate_id in bridge["survivor_arm_ids"]:
        candidate = by_id[candidate_id]
        control_id = str(candidate.control_arm_id or "")
        control = by_id.get(control_id)
        if control is None:
            raise ControllerError(f"bridge control is missing for {candidate_id}")
        schedule = build_confirmatory_schedule_payload(
            plan,
            control_arm=control,
            candidate_arm=candidate,
            control_override=resolved[control_id]["override"],
            candidate_override=resolved[candidate_id]["override"],
            analyzer_artifact=(
                bridge_analyzer if candidate.analyzer_mode == "frozen_replay" else None
            ),
            seed=f"{seed_prefix}:{candidate_id}",
        )
        validate_confirmatory_schedule(plan, schedule)
        schedules[candidate_id] = schedule
    derived_contract = bridge["derived_contract"]
    derived: dict[str, Any] = {
        "schema": CONFIRMATORY_BRIDGE_DERIVED_SCHEMA,
        "campaign_plan_sha256": plan_sha,
        "snapshot_commit": snapshot_contract["commit"],
        "snapshot_tree": snapshot_contract["tree"],
        "screening_source_evidence_sha256": source["source_evidence_sha256"],
        "generation_calls": 0,
        "old_receipts_reused_as_current": False,
        "frozen_analyzer": {
            "path": derived_contract["analyzer_path"],
            "artifact_sha256": bridge_analyzer["artifact_sha256"],
            "document_sha256": canonical_sha256(bridge_analyzer),
        },
        "analyzer_p99": {
            "path": derived_contract["p99_path"],
            "receipt_sha256": bridge_p99["receipt_sha256"],
            "document_sha256": canonical_sha256(bridge_p99),
        },
        "resolved_arms": resolved,
        "schedules": {
            arm_id: {
                "path": str(Path(derived_contract["schedule_root"]) / f"{arm_id}.json"),
                "cohort_id": schedule["cohort_id"],
                "schedule_sha256": schedule["schedule_sha256"],
                "document_sha256": canonical_sha256(schedule),
            }
            for arm_id, schedule in sorted(schedules.items())
        },
        "provenance": copy.deepcopy(source["derived_provenance"]),
    }
    derived["derived_sha256"] = canonical_sha256(derived)
    return bridge_analyzer, bridge_p99, derived, schedules


def validate_confirmatory_bridge_derived(plan: Mapping[str, Any]) -> dict[str, Any]:
    bridge = plan.get("confirmatory_bridge")
    if not isinstance(bridge, Mapping):
        raise ControllerError("plan is not a confirmatory bridge")
    expected_analyzer, expected_p99, expected_derived, expected_schedules = (
        build_confirmatory_bridge_documents(plan)
    )
    contract = bridge["derived_contract"]
    for path, expected, label in (
        (Path(contract["analyzer_path"]), expected_analyzer, "bridge Analyzer artifact"),
        (Path(contract["p99_path"]), expected_p99, "bridge Analyzer p99 receipt"),
        (Path(contract["path"]), expected_derived, "bridge derived plan"),
    ):
        require_regular_file(path)
        if load_json(path) != expected:
            raise ControllerError(f"{label} differs from recomputed evidence")
    for arm_id, schedule in expected_schedules.items():
        path = Path(expected_derived["schedules"][arm_id]["path"])
        require_regular_file(path)
        if load_json(path) != schedule:
            raise ControllerError(f"bridge schedule differs for {arm_id}")
    schedule_root = Path(contract["schedule_root"])
    actual_names = sorted(
        path.name
        for path in schedule_root.iterdir()
        if path.is_file() and not path.is_symlink()
    )
    expected_names = sorted(f"{arm_id}.json" for arm_id in expected_schedules)
    if actual_names != expected_names:
        raise ControllerError("bridge schedule directory contains missing or extra files")
    return expected_derived


def prepare_confirmatory_bridge(
    *,
    source_plan_path: Path,
    terminal_status_path: Path,
    terminal_report_receipt_path: Path,
    snapshot: Path,
    run_root: Path,
    report_root: Path,
    requested_survivor_arm_ids: Sequence[str] | None,
    run_id: str | None,
    schedule_seed_prefix: str,
) -> dict[str, Any]:
    """Create one deterministic confirmatory-only plan and its offline receipts."""

    source = authenticate_confirmatory_bridge_screening_source(
        source_plan_path=source_plan_path,
        terminal_status_path=terminal_status_path,
        terminal_report_receipt_path=terminal_report_receipt_path,
        requested_survivor_arm_ids=requested_survivor_arm_ids,
    )
    source_plan_bound = load_bound_json(source_plan_path, label="screening plan")
    source_plan = source_plan_bound.value
    if not isinstance(source_plan, Mapping):
        raise ControllerError("screening plan is not an object")
    snapshot = absolute_path_without_symlinks(snapshot, label="confirmatory snapshot")
    snapshot_identity = git_identity(snapshot)
    if snapshot_identity["status"]:
        raise ControllerError("confirmatory bridge snapshot is dirty")
    if not snapshot_identity["commit"] or not snapshot_identity["tree"]:
        raise ControllerError("confirmatory bridge snapshot identity is unavailable")
    run_root = secure_future_absolute_path(run_root, label="confirmatory run root")
    report_root = secure_future_absolute_path(report_root, label="confirmatory report root")
    if not run_root.is_absolute() or not report_root.is_absolute():
        raise ControllerError("confirmatory bridge roots must be absolute")
    validate_confirmatory_root_isolation(
        run_root=run_root,
        report_root=report_root,
        snapshot=snapshot,
        screening_source=source,
    )
    plan = copy.deepcopy(dict(source_plan))
    plan["run_id"] = run_id or (
        f"{source_plan.get('run_id')}-confirmatory-{snapshot_identity['commit'][:12]}"
    )
    if SAFE_COMPONENT_RE.fullmatch(str(plan["run_id"])) is None:
        raise ControllerError("confirmatory bridge run id is unsafe")
    source_terminal_status_bound = load_bound_json(
        terminal_status_path, label="screening terminal status"
    )
    source_terminal_status = source_terminal_status_bound.value
    plan["created_at"] = source_terminal_status.get("completed_at") or source_plan.get(
        "created_at"
    )
    plan["paths"]["run_root"] = str(run_root)
    plan["paths"]["snapshot"] = str(snapshot)
    plan["paths"]["report_root"] = str(report_root)
    plan["paths"]["reporter"] = str(
        snapshot / "scripts/experiments/generate_draco_p0_p05_reports.py"
    )
    plan["freeze"]["snapshot_commit"] = snapshot_identity["commit"]
    plan["freeze"]["snapshot_tree"] = snapshot_identity["tree"]
    derived_contract = {
        "schema": CONFIRMATORY_BRIDGE_DERIVED_SCHEMA,
        "path": str(run_root / "bridge-derived-plan.json"),
        "analyzer_path": str(run_root / "bridge-frozen-analyzer.json"),
        "p99_path": str(run_root / "bridge-analyzer-p99.json"),
        "schedule_root": str(run_root / "confirmatory-schedules"),
        "must_regenerate_from_provenance": True,
        "reuse_old_receipt_as_current": False,
    }
    plan["confirmatory_bridge"] = {
        "schema": CONFIRMATORY_BRIDGE_SCHEMA,
        "mode": "confirmatory_only",
        "screening_source": source,
        "survivor_arm_ids": list(source["survivor_arm_ids"]),
        "snapshot_contract": {
            "path": str(snapshot),
            "commit": snapshot_identity["commit"],
            "tree": snapshot_identity["tree"],
        },
        "launcher_contract": {
            "relative_path": plan["paths"]["launcher_relative"],
            "raw_sha256": file_sha256(
                snapshot / str(plan["paths"]["launcher_relative"])
            ),
            "schedule_schema": CONFIRMATORY_SCHEDULE_SCHEMA,
            "flag": "--confirmatory-schedule",
            "fd_flag": "--confirmatory-schedule-fd",
            "support_required": True,
        },
        "derived_contract": derived_contract,
        "reporting_contract": {
            "output_root": str(report_root),
            "source_screening_is_immutable_reference": True,
            "one_complete_cohort_per_survivor": True,
            "shared_account_delta_once_per_cohort": True,
            "selected_generation_and_judge_per_role": True,
            "partial_or_extra_cohort_fails_strict": True,
            "mini_is_diagnostic_only": True,
            "automatic_winner_promotion": False,
        },
        "schedule_seed_prefix": schedule_seed_prefix,
    }
    plan["freeze"].update(compute_runtime_freeze_identity(plan, snapshot=snapshot))
    plan["confirmatory_bridge"]["launcher_contract"]["raw_sha256"] = plan["freeze"][
        "sources"
    ]["launcher_raw_sha256"]
    plan["confirmatory_bridge"]["bridge_sha256"] = canonical_sha256(
        plan["confirmatory_bridge"]
    )
    validate_plan(plan, allow_placeholders=False)
    validate_runtime_freeze(
        plan,
        snapshot=snapshot,
        expected_snapshot_identity=snapshot_identity,
    )
    assert_bridge_snapshot_unchanged(snapshot, snapshot_identity)
    validate_confirmatory_launcher_support(plan, snapshot=snapshot)
    # Rebuild source evidence after sealing the seed extension to ensure all
    # paths/hashes still authenticate. The validator normalizes the selection
    # mode but intentionally preserves the plan-bound schedule seed extension.
    plan_path = run_root / "campaign-plan.json"
    if plan_path.exists() or plan_path.is_symlink():
        require_regular_file(plan_path)
        if load_json(plan_path) != plan:
            raise ControllerError("existing confirmatory bridge plan differs")
    else:
        secure_atomic_write_json(plan_path, plan)
    analyzer, p99, derived, schedules = build_confirmatory_bridge_documents(plan)
    _write_new_or_identical_json(Path(derived_contract["analyzer_path"]), analyzer)
    _write_new_or_identical_json(Path(derived_contract["p99_path"]), p99)
    for arm_id, schedule in schedules.items():
        _write_new_or_identical_json(
            Path(derived_contract["schedule_root"]) / f"{arm_id}.json",
            schedule,
        )
    _write_new_or_identical_json(Path(derived_contract["path"]), derived)
    validate_confirmatory_bridge_derived(plan)
    validate_runtime_freeze(
        plan,
        snapshot=snapshot,
        expected_snapshot_identity=snapshot_identity,
    )
    return {
        "status": "prepared",
        "plan_path": str(plan_path),
        "campaign_plan_sha256": canonical_sha256(plan),
        "snapshot_commit": snapshot_identity["commit"],
        "snapshot_tree": snapshot_identity["tree"],
        "survivor_arm_ids": list(plan["confirmatory_bridge"]["survivor_arm_ids"]),
        "schedule_count": len(schedules),
        "derived_sha256": derived["derived_sha256"],
        "generation_calls": 0,
    }


def validate_confirmatory_bridge_only(plan_path: Path) -> dict[str, Any]:
    bound = load_bound_json(plan_path, label="confirmatory bridge plan")
    plan = bound.value
    if not isinstance(plan, Mapping):
        raise ControllerError("confirmatory bridge plan is not an object")
    return validate_confirmatory_bridge_document(
        plan,
        plan_path=plan_path,
        plan_file_sha256=bound.file_sha256,
    )


def validate_confirmatory_bridge_document(
    plan: Mapping[str, Any],
    *,
    plan_path: Path,
    plan_file_sha256: str,
) -> dict[str, Any]:
    """Validate the exact in-memory plan parsed from an authenticated byte snapshot."""

    current = load_bound_json(plan_path, label="confirmatory bridge plan")
    if current.file_sha256 != plan_file_sha256 or current.value != dict(plan):
        raise ControllerError("confirmatory bridge plan changed after byte authentication")
    validate_plan(plan, allow_placeholders=False)
    bridge = plan.get("confirmatory_bridge")
    if not isinstance(bridge, Mapping):
        raise ControllerError("plan is not confirmatory-only")
    canonical_path = Path(str(plan["paths"]["run_root"])) / "campaign-plan.json"
    if plan_path.resolve() != canonical_path.resolve():
        raise ControllerError("confirmatory bridge plan must be run_root/campaign-plan.json")
    snapshot, identity = validate_snapshot(plan)
    validate_confirmatory_root_isolation(
        run_root=Path(str(plan["paths"]["run_root"])),
        report_root=Path(str(plan["paths"]["report_root"])),
        snapshot=snapshot,
        screening_source=bridge["screening_source"],
    )
    validate_runtime_freeze(plan, snapshot=snapshot, expected_snapshot_identity=identity)
    launcher = validate_confirmatory_launcher_support(plan, snapshot=snapshot)
    source = validate_confirmatory_bridge_source(plan)
    derived = validate_confirmatory_bridge_derived(plan)
    return {
        "status": "valid",
        "campaign_plan_file_sha256": plan_file_sha256,
        "campaign_plan_sha256": canonical_sha256(plan),
        "survivor_arm_ids": list(bridge["survivor_arm_ids"]),
        "schedule_count": len(derived["schedules"]),
        "snapshot_commit": identity["commit"],
        "snapshot_tree": identity["tree"],
        "launcher": launcher,
        "screening_source_evidence_sha256": source["source_evidence_sha256"],
        "derived_sha256": derived["derived_sha256"],
        "generation_calls": 0,
    }


def _confirmatory_order_key(*, seed: str, candidate_arm_id: str, task_id: str) -> str:
    payload = f"{seed}\0{candidate_arm_id}\0{task_id}".encode()
    return hashlib.sha256(payload).hexdigest()


def _confirmatory_replay_payload(override: Mapping[str, Any]) -> Mapping[str, Any] | None:
    g1 = override.get("g1_routing")
    execution = g1.get("task_analysis_execution") if isinstance(g1, Mapping) else None
    return execution if isinstance(execution, Mapping) else None


def build_confirmatory_schedule_payload(
    plan: Mapping[str, Any],
    *,
    control_arm: Arm,
    candidate_arm: Arm,
    control_override: Mapping[str, Any],
    candidate_override: Mapping[str, Any],
    analyzer_artifact: Mapping[str, Any] | None,
    seed: str,
) -> dict[str, Any]:
    """Freeze one strict task-paired AB/BA confirmatory cohort.

    The production runner already supports exact task-id shards.  This schedule
    freezes the causal ordering above that runner: exactly five tasks execute
    control then candidate (AB), exactly five execute candidate then control
    (BA), and no tranche can admit more than six tasks at once.
    """

    task_ids = plan.get("benchmark", {}).get("task_ids")
    if (
        not isinstance(task_ids, list)
        or len(task_ids) != EXPECTED_TASK_COUNT
        or any(not isinstance(task_id, str) or not task_id for task_id in task_ids)
        or len(set(task_ids)) != EXPECTED_TASK_COUNT
    ):
        raise ControllerError("confirmatory schedule requires the exact ten frozen task IDs")
    if int(plan.get("execution", {}).get("task_concurrency", 0)) != CONFIRMATORY_TASK_CONCURRENCY:
        raise ControllerError("confirmatory schedule requires task_concurrency=6")
    if candidate_arm.experiment_id == "common-E0":
        raise ControllerError("confirmatory candidate must not be a common E0 arm")
    if candidate_arm.control_arm_id != control_arm.arm_id:
        raise ControllerError("confirmatory candidate/control binding differs from the plan")
    if candidate_arm.analyzer_mode != control_arm.analyzer_mode:
        raise ControllerError("confirmatory pair must use the same Analyzer execution mode")
    if not seed:
        raise ControllerError("confirmatory schedule seed must be non-empty")

    ranked = sorted(
        task_ids,
        key=lambda task_id: (
            _confirmatory_order_key(
                seed=seed,
                candidate_arm_id=candidate_arm.arm_id,
                task_id=task_id,
            ),
            task_id,
        ),
    )
    control_first = set(ranked[:CONFIRMATORY_ORDER_COUNT_PER_ROLE])
    task_schedule: list[dict[str, Any]] = []
    for input_ordinal, task_id in enumerate(task_ids, start=1):
        order = "AB" if task_id in control_first else "BA"
        task_schedule.append(
            {
                "task_id": task_id,
                "input_ordinal": input_ordinal,
                "order": order,
                "first_role": "control" if order == "AB" else "candidate",
                "second_role": "candidate" if order == "AB" else "control",
                "assignment_sha256": _confirmatory_order_key(
                    seed=seed,
                    candidate_arm_id=candidate_arm.arm_id,
                    task_id=task_id,
                ),
            }
        )

    tranches: list[dict[str, Any]] = []
    offset = 0
    for tranche_index, tranche_size in enumerate(CONFIRMATORY_TRANCHE_SIZES, start=1):
        tranche_tasks = task_schedule[offset : offset + tranche_size]
        offset += tranche_size
        phases: list[dict[str, Any]] = []
        for leg, role_field in ((1, "first_role"), (2, "second_role")):
            control_tasks = [
                item["task_id"] for item in tranche_tasks if item[role_field] == "control"
            ]
            candidate_tasks = [
                item["task_id"] for item in tranche_tasks if item[role_field] == "candidate"
            ]
            phases.append(
                {
                    "leg": leg,
                    "control_task_ids": control_tasks,
                    "candidate_task_ids": candidate_tasks,
                    "max_inflight_tasks": len(control_tasks) + len(candidate_tasks),
                }
            )
        tranches.append(
            {
                "tranche": tranche_index,
                "task_ids": [item["task_id"] for item in tranche_tasks],
                "phases": phases,
            }
        )
    if offset != EXPECTED_TASK_COUNT:
        raise ControllerError("confirmatory tranche sizes do not cover the frozen mini set")

    frozen_analyzer: dict[str, Any] | None = None
    if candidate_arm.analyzer_mode == "frozen_replay":
        if not isinstance(analyzer_artifact, Mapping):
            raise ControllerError("frozen confirmatory pair requires the Analyzer artifact")
        replay_payload = analyzer_artifact.get("replay_payload")
        entries = replay_payload.get("entries") if isinstance(replay_payload, Mapping) else None
        if not isinstance(entries, Mapping) or set(entries) != set(task_ids):
            raise ControllerError("frozen Analyzer replay must retain all ten entries")
        control_replay = _confirmatory_replay_payload(control_override)
        candidate_replay = _confirmatory_replay_payload(candidate_override)
        if (
            control_replay != replay_payload
            or candidate_replay != replay_payload
            or control_replay.get("mode") != "frozen_replay"
        ):
            raise ControllerError("both confirmatory arms must use the identical replay payload")
        frozen_analyzer = {
            "artifact_sha256": analyzer_artifact.get("artifact_sha256"),
            "replay_payload_sha256": canonical_sha256(replay_payload),
            "entry_count": len(entries),
            "task_ids": sorted(entries),
            "physical_analyzer_requests_per_task": 0,
        }
    elif analyzer_artifact is not None:
        raise ControllerError("live Analyzer confirmatory pairs must not embed replay evidence")

    core: dict[str, Any] = {
        "schema": CONFIRMATORY_SCHEDULE_SCHEMA,
        "campaign_plan_sha256": canonical_sha256(plan),
        "campaign_run_id": str(plan.get("run_id") or ""),
        "snapshot_commit": str(plan.get("freeze", {}).get("snapshot_commit") or ""),
        "snapshot_tree": str(plan.get("freeze", {}).get("snapshot_tree") or ""),
        "benchmark": {
            "name": "DRACO mini",
            "group": "G1",
            "task_count": EXPECTED_TASK_COUNT,
            "task_ids": list(task_ids),
            "input_sha256": plan.get("benchmark", {}).get("input_sha256"),
        },
        "seed": seed,
        "roles": {
            "control": {
                "arm_id": control_arm.arm_id,
                "experiment_id": control_arm.experiment_id,
                "variant": control_arm.variant,
                "replicate": control_arm.replicate,
                "analyzer_mode": control_arm.analyzer_mode,
                "override": copy.deepcopy(dict(control_override)),
            },
            "candidate": {
                "arm_id": candidate_arm.arm_id,
                "experiment_id": candidate_arm.experiment_id,
                "variant": candidate_arm.variant,
                "replicate": candidate_arm.replicate,
                "control_arm_id": candidate_arm.control_arm_id,
                "analyzer_mode": candidate_arm.analyzer_mode,
                "override": copy.deepcopy(dict(candidate_override)),
            },
        },
        "task_schedule": task_schedule,
        "order_balance": {"AB": 5, "BA": 5},
        "tranches": tranches,
        "execution_contract": {
            "global_task_concurrency": CONFIRMATORY_TASK_CONCURRENCY,
            "phase_barrier_between_legs": True,
            "generation_max_attempts": int(
                plan.get("execution", {}).get("generation_max_attempts", 0)
            ),
            "generation_leg_failure_policy": "cohort_incomplete_no_unpaired_retry",
            "allowed_post_pair_repairs": ["judge_only", "metadata_only", "audit_only"],
        },
        "frozen_analyzer": frozen_analyzer,
        "account_window_contract": {
            "scope": "paired_cohort_shared",
            "single_exclusive_lock": True,
            "single_account_before_after": True,
            "companion_ledger_required": True,
            "report_account_delta_once": True,
        },
        "screening_contract": {
            "source_screening_is_diagnostic_only": True,
            "confirmatory_mini_is_diagnostic_only": True,
            "automatic_winner_promotion": False,
        },
    }
    identity_sha256 = canonical_sha256(core)
    cohort_id = (
        f"{candidate_arm.experiment_id}-{candidate_arm.variant}-confirmatory-"
        f"{identity_sha256[:16]}"
    )
    if SAFE_COMPONENT_RE.fullmatch(cohort_id) is None:
        raise ControllerError("derived confirmatory cohort ID is unsafe")
    payload = {
        **core,
        "identity_sha256": identity_sha256,
        "cohort_id": cohort_id,
        "output_name": cohort_id,
    }
    payload["schedule_sha256"] = canonical_sha256(payload)
    return payload


def validate_confirmatory_schedule(
    plan: Mapping[str, Any], schedule: Mapping[str, Any]
) -> None:
    if schedule.get("schema") != CONFIRMATORY_SCHEDULE_SCHEMA:
        raise ControllerError("confirmatory schedule schema differs")
    without_schedule_hash = dict(schedule)
    schedule_sha256 = without_schedule_hash.pop("schedule_sha256", None)
    if schedule_sha256 != canonical_sha256(without_schedule_hash):
        raise ControllerError("confirmatory schedule self-hash differs")
    identity_payload = dict(without_schedule_hash)
    identity_sha256 = identity_payload.pop("identity_sha256", None)
    cohort_id = identity_payload.pop("cohort_id", None)
    output_name = identity_payload.pop("output_name", None)
    if identity_sha256 != canonical_sha256(identity_payload):
        raise ControllerError("confirmatory schedule identity hash differs")
    roles = schedule.get("roles")
    if not isinstance(roles, Mapping) or set(roles) != {"control", "candidate"}:
        raise ControllerError("confirmatory roles differ")
    control = roles["control"]
    candidate = roles["candidate"]
    if not isinstance(control, Mapping) or not isinstance(candidate, Mapping):
        raise ControllerError("confirmatory role descriptors are malformed")
    plan_arms = {arm.arm_id: arm for arm in expand_arms(plan)}
    control_arm = plan_arms.get(str(control.get("arm_id") or ""))
    candidate_arm = plan_arms.get(str(candidate.get("arm_id") or ""))
    if control_arm is None or candidate_arm is None:
        raise ControllerError("confirmatory role is not a frozen plan arm")
    if candidate_arm.control_arm_id != control_arm.arm_id:
        raise ControllerError("confirmatory candidate/control binding differs")
    expected_role_fields = {
        "control": {
            "experiment_id": control_arm.experiment_id,
            "variant": control_arm.variant,
            "replicate": control_arm.replicate,
            "analyzer_mode": control_arm.analyzer_mode,
        },
        "candidate": {
            "experiment_id": candidate_arm.experiment_id,
            "variant": candidate_arm.variant,
            "replicate": candidate_arm.replicate,
            "control_arm_id": candidate_arm.control_arm_id,
            "analyzer_mode": candidate_arm.analyzer_mode,
        },
    }
    for role, expected_fields in expected_role_fields.items():
        actual = roles[role]
        if any(actual.get(field) != value for field, value in expected_fields.items()):
            raise ControllerError(f"confirmatory {role} descriptor differs from the plan")
        if not isinstance(actual.get("override"), Mapping):
            raise ControllerError(f"confirmatory {role} override is malformed")
    expected_cohort_id = (
        f"{candidate_arm.experiment_id}-{candidate_arm.variant}-confirmatory-"
        f"{str(identity_sha256)[:16]}"
    )
    if (
        cohort_id != expected_cohort_id
        or not isinstance(cohort_id, str)
        or SAFE_COMPONENT_RE.fullmatch(cohort_id) is None
        or output_name != cohort_id
    ):
        raise ControllerError("confirmatory cohort identity differs")
    if schedule.get("campaign_plan_sha256") != canonical_sha256(plan):
        raise ControllerError("confirmatory schedule is bound to another campaign plan")
    if (
        schedule.get("snapshot_commit") != plan.get("freeze", {}).get("snapshot_commit")
        or schedule.get("snapshot_tree") != plan.get("freeze", {}).get("snapshot_tree")
    ):
        raise ControllerError("confirmatory schedule snapshot identity differs")
    benchmark = schedule.get("benchmark")
    task_ids = benchmark.get("task_ids") if isinstance(benchmark, Mapping) else None
    if (
        task_ids != plan.get("benchmark", {}).get("task_ids")
        or benchmark.get("name") != "DRACO mini"
        or benchmark.get("task_count") != EXPECTED_TASK_COUNT
        or benchmark.get("group") != "G1"
        or benchmark.get("input_sha256") != plan.get("benchmark", {}).get("input_sha256")
    ):
        raise ControllerError("confirmatory benchmark identity differs")
    seed = schedule.get("seed")
    if not isinstance(seed, str) or not seed:
        raise ControllerError("confirmatory schedule seed is invalid")
    task_schedule = schedule.get("task_schedule")
    if not isinstance(task_schedule, list) or len(task_schedule) != EXPECTED_TASK_COUNT:
        raise ControllerError("confirmatory task schedule is incomplete")
    if [item.get("task_id") for item in task_schedule] != task_ids:
        raise ControllerError("confirmatory task schedule order differs")
    orders = [item.get("order") for item in task_schedule]
    if orders.count("AB") != 5 or orders.count("BA") != 5:
        raise ControllerError("confirmatory schedule is not balanced 5 AB / 5 BA")
    ranked = sorted(
        task_ids,
        key=lambda task_id: (
            _confirmatory_order_key(
                seed=seed,
                candidate_arm_id=candidate_arm.arm_id,
                task_id=task_id,
            ),
            task_id,
        ),
    )
    expected_control_first = set(ranked[:CONFIRMATORY_ORDER_COUNT_PER_ROLE])
    for ordinal, item in enumerate(task_schedule, start=1):
        task_id = item.get("task_id")
        expected_order = "AB" if task_id in expected_control_first else "BA"
        expected_first = "control" if expected_order == "AB" else "candidate"
        expected_second = "candidate" if expected_order == "AB" else "control"
        if (
            item.get("input_ordinal") != ordinal
            or item.get("order") != expected_order
            or item.get("first_role") != expected_first
            or item.get("second_role") != expected_second
            or item.get("assignment_sha256")
            != _confirmatory_order_key(
                seed=seed,
                candidate_arm_id=candidate_arm.arm_id,
                task_id=task_id,
            )
        ):
            raise ControllerError("confirmatory deterministic task assignment differs")
    if schedule.get("order_balance") != {"AB": 5, "BA": 5}:
        raise ControllerError("confirmatory order-balance receipt differs")
    tranches = schedule.get("tranches")
    if not isinstance(tranches, list) or [len(item.get("task_ids", [])) for item in tranches] != [
        *CONFIRMATORY_TRANCHE_SIZES
    ]:
        raise ControllerError("confirmatory tranche partition differs")
    flattened: list[str] = []
    by_task = {item["task_id"]: item for item in task_schedule}
    offset = 0
    for tranche_index, tranche in enumerate(tranches, start=1):
        tranche_task_ids = tranche.get("task_ids")
        phases = tranche.get("phases")
        if (
            not isinstance(tranche_task_ids, list)
            or not isinstance(phases, list)
            or len(phases) != 2
        ):
            raise ControllerError("confirmatory tranche phases are malformed")
        expected_tranche_tasks = task_ids[
            offset : offset + CONFIRMATORY_TRANCHE_SIZES[tranche_index - 1]
        ]
        offset += len(expected_tranche_tasks)
        if (
            tranche.get("tranche") != tranche_index
            or tranche_task_ids != expected_tranche_tasks
        ):
            raise ControllerError("confirmatory tranche identity or order differs")
        flattened.extend(tranche_task_ids)
        for phase_index, phase in enumerate(phases, start=1):
            control_ids = phase.get("control_task_ids")
            candidate_ids = phase.get("candidate_task_ids")
            if not isinstance(control_ids, list) or not isinstance(candidate_ids, list):
                raise ControllerError("confirmatory phase task sets are malformed")
            if phase.get("leg") != phase_index:
                raise ControllerError("confirmatory phase leg identity differs")
            if set(control_ids) & set(candidate_ids) or set(control_ids + candidate_ids) != set(
                tranche_task_ids
            ):
                raise ControllerError("confirmatory phase does not cover its tranche exactly")
            expected_control = {
                task_id
                for task_id in tranche_task_ids
                if by_task[task_id]["first_role" if phase_index == 1 else "second_role"]
                == "control"
            }
            if set(control_ids) != expected_control:
                raise ControllerError("confirmatory phase role assignment differs")
            inflight = len(control_ids) + len(candidate_ids)
            if (
                phase.get("max_inflight_tasks") != inflight
                or inflight > CONFIRMATORY_TASK_CONCURRENCY
            ):
                raise ControllerError("confirmatory global task concurrency exceeds six")
    if flattened != task_ids:
        raise ControllerError("confirmatory tranches do not preserve the frozen input order")
    execution = schedule.get("execution_contract")
    if (
        not isinstance(execution, Mapping)
        or execution.get("global_task_concurrency") != CONFIRMATORY_TASK_CONCURRENCY
        or execution.get("phase_barrier_between_legs") is not True
        or execution.get("generation_max_attempts")
        != plan.get("execution", {}).get("generation_max_attempts")
        or execution.get("generation_leg_failure_policy")
        != "cohort_incomplete_no_unpaired_retry"
        or execution.get("allowed_post_pair_repairs")
        != ["judge_only", "metadata_only", "audit_only"]
    ):
        raise ControllerError("confirmatory execution contract differs")
    account = schedule.get("account_window_contract")
    if (
        not isinstance(account, Mapping)
        or account.get("scope") != "paired_cohort_shared"
        or account.get("single_exclusive_lock") is not True
        or account.get("single_account_before_after") is not True
        or account.get("companion_ledger_required") is not True
        or account.get("report_account_delta_once") is not True
    ):
        raise ControllerError("confirmatory shared account-window contract differs")
    screening = schedule.get("screening_contract")
    if (
        not isinstance(screening, Mapping)
        or screening.get("source_screening_is_diagnostic_only") is not True
        or screening.get("confirmatory_mini_is_diagnostic_only") is not True
        or screening.get("automatic_winner_promotion") is not False
    ):
        raise ControllerError("confirmatory diagnostic-only contract differs")
    analyzer_modes = {value.get("analyzer_mode") for value in roles.values()}
    if len(analyzer_modes) != 1:
        raise ControllerError("confirmatory Analyzer modes differ")
    if analyzer_modes == {"frozen_replay"}:
        frozen = schedule.get("frozen_analyzer")
        if (
            not isinstance(frozen, Mapping)
            or frozen.get("entry_count") != EXPECTED_TASK_COUNT
            or frozen.get("task_ids") != sorted(task_ids)
            or frozen.get("physical_analyzer_requests_per_task") != 0
        ):
            raise ControllerError("confirmatory frozen Analyzer evidence differs")
        replay_values = [
            _confirmatory_replay_payload(value.get("override", {}))
            for value in roles.values()
        ]
        replay = replay_values[0]
        entries = replay.get("entries") if isinstance(replay, Mapping) else None
        if (
            replay_values[0] != replay_values[1]
            or not isinstance(replay, Mapping)
            or replay.get("mode") != "frozen_replay"
            or replay.get("schema") not in FROZEN_TASK_ANALYSIS_SCHEMAS
            or not isinstance(entries, Mapping)
            or set(entries) != set(task_ids)
            or frozen.get("replay_payload_sha256") != canonical_sha256(replay)
        ):
            raise ControllerError("confirmatory roles do not share the replay payload")
    elif schedule.get("frozen_analyzer") is not None:
        raise ControllerError("live Analyzer confirmatory pair embeds frozen replay evidence")


def prepare_confirmatory_schedule(
    plan_path: Path,
    *,
    candidate_arm_id: str,
    output_path: Path,
    seed: str,
) -> dict[str, Any]:
    plan = load_json(plan_path)
    arms = validate_plan(plan, allow_placeholders=False)
    by_id = {arm.arm_id: arm for arm in arms}
    candidate = by_id.get(candidate_arm_id)
    if candidate is None or candidate.control_arm_id is None:
        raise ControllerError("unknown or uncontrolled confirmatory candidate arm")
    control = by_id.get(candidate.control_arm_id)
    if control is None:
        raise ControllerError("confirmatory control arm is missing")
    bridge = plan.get("confirmatory_bridge")
    if isinstance(bridge, Mapping):
        if candidate_arm_id not in bridge.get("survivor_arm_ids", []):
            raise ControllerError("candidate is not an authenticated screening survivor")
        validate_confirmatory_bridge_only(plan_path)
        derived = validate_confirmatory_bridge_derived(plan)
        resolved = derived.get("resolved_arms")
        if not isinstance(resolved, Mapping):
            raise ControllerError("bridge derived plan lacks resolved arms")
        control_payload = resolved.get(control.arm_id)
        candidate_payload = resolved.get(candidate.arm_id)
        if not isinstance(control_payload, Mapping) or not isinstance(
            candidate_payload, Mapping
        ):
            raise ControllerError("bridge derived plan lacks the confirmatory pair")
        artifact = load_json(Path(bridge["derived_contract"]["analyzer_path"]))
        schedule = build_confirmatory_schedule_payload(
            plan,
            control_arm=control,
            candidate_arm=candidate,
            control_override=control_payload["override"],
            candidate_override=candidate_payload["override"],
            analyzer_artifact=(artifact if candidate.analyzer_mode == "frozen_replay" else None),
            seed=f"{bridge['schedule_seed_prefix']}:{candidate.arm_id}",
        )
        validate_confirmatory_schedule(plan, schedule)
        expected_descriptor = derived.get("schedules", {}).get(candidate.arm_id)
        if (
            not isinstance(expected_descriptor, Mapping)
            or expected_descriptor.get("schedule_sha256") != schedule.get("schedule_sha256")
            or Path(str(expected_descriptor.get("path") or "")).resolve()
            != output_path.resolve()
        ):
            raise ControllerError("bridge confirmatory output path or schedule binding differs")
        _write_new_or_identical_json(output_path, schedule)
        return schedule
    plan_sha256 = canonical_sha256(plan)
    derived, artifact = load_derived(plan, plan_sha256)
    if candidate.analyzer_mode == "frozen_replay":
        offline = derived.get("offline_effect", {}).get(candidate.arm_id)
        if not isinstance(offline, Mapping) or offline.get("decision") == "deleted_no_live_run":
            raise ControllerError("offline no-op arm cannot enter confirmatory execution")
    p99_receipt = derived.get("p0_5_06")
    control_override = resolve_arm_override(
        plan,
        control,
        artifact=artifact,
        p99_receipt=p99_receipt,
    )
    candidate_override = resolve_arm_override(
        plan,
        candidate,
        artifact=artifact,
        p99_receipt=p99_receipt,
    )
    schedule = build_confirmatory_schedule_payload(
        plan,
        control_arm=control,
        candidate_arm=candidate,
        control_override=control_override,
        candidate_override=candidate_override,
        analyzer_artifact=(artifact if candidate.analyzer_mode == "frozen_replay" else None),
        seed=seed,
    )
    validate_confirmatory_schedule(plan, schedule)
    if output_path.exists() or output_path.is_symlink():
        raise ControllerError("refusing to overwrite a confirmatory schedule")
    atomic_write_json(output_path, schedule)
    return schedule


def _validate_confirmatory_role_publication(
    *,
    cohort_root: Path,
    role: str,
    schedule: Mapping[str, Any],
    pair_role: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    expected_relative = f"{role}/manifest.json"
    if set(pair_role) != {"path", "sha256"} or pair_role.get("path") != expected_relative:
        raise ControllerError(f"confirmatory {role} pair-manifest binding differs")
    role_root = absolute_path_without_symlinks(
        cohort_root / role,
        label=f"confirmatory {role} publication root",
    )
    if not role_root.is_dir():
        raise ControllerError(f"confirmatory {role} publication root is not a directory")
    required_names = (
        "manifest.json",
        "results.jsonl",
        "trace.jsonl",
        "audit.json",
        "openrouter-non-byok-campaign-proof.json",
    )
    required = {
        name: absolute_path_without_symlinks(
            role_root / name,
            label=f"confirmatory {role} {name}",
        )
        for name in required_names
    }
    if any(not path.is_file() for path in required.values()):
        raise ControllerError(f"confirmatory {role} formal artifacts are incomplete")
    manifest_path = required["manifest.json"]
    if pair_role.get("sha256") != file_sha256(manifest_path):
        raise ControllerError(f"confirmatory {role} manifest raw hash differs")
    manifest = load_json(manifest_path)
    if not isinstance(manifest, Mapping):
        raise ControllerError(f"confirmatory {role} manifest is malformed")
    manifest_without_hash = dict(manifest)
    recorded_manifest_hash = manifest_without_hash.pop("manifest_sha256", None)
    if recorded_manifest_hash != "sha256:" + canonical_sha256(manifest_without_hash):
        raise ControllerError(f"confirmatory {role} manifest self-hash differs")
    if (
        manifest.get("status") != "complete"
        or manifest.get("execution_pass") is not True
        or manifest.get("groups") != ["G1"]
        or manifest.get("task_count") != EXPECTED_TASK_COUNT
        or manifest.get("result_count") != EXPECTED_TASK_COUNT
        or manifest.get("task_ids") != schedule.get("benchmark", {}).get("task_ids")
    ):
        raise ControllerError(f"confirmatory {role} publication is not formally complete")
    cohort = manifest.get("account_window_cohort")
    companion_role = "candidate" if role == "control" else "control"
    if (
        not isinstance(cohort, Mapping)
        or cohort.get("cohort_id") != schedule.get("cohort_id")
        or cohort.get("role") != role
        or cohort.get("companion_role") != companion_role
        or not isinstance(cohort.get("cohort_sha256"), str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", cohort["cohort_sha256"]) is None
    ):
        raise ControllerError(f"confirmatory {role} shared account-window binding differs")
    schedule_role = schedule.get("roles", {}).get(role)
    if not isinstance(schedule_role, Mapping):
        raise ControllerError(f"confirmatory {role} schedule descriptor is missing")
    return (
        {
            "arm_id": schedule_role.get("arm_id"),
            "experiment_id": schedule_role.get("experiment_id"),
            "root": str(role_root),
            "manifest_sha256": file_sha256(manifest_path),
            "publication_status": "complete",
        },
        str(cohort["cohort_sha256"]),
    )


def build_confirmatory_report_input_entry(
    plan: Mapping[str, Any],
    schedule: Mapping[str, Any],
    cohort_root: Path,
) -> dict[str, Any]:
    resolved_root = absolute_path_without_symlinks(
        cohort_root,
        label="confirmatory cohort root",
    )
    if not resolved_root.is_dir():
        raise ControllerError("confirmatory cohort root is not a directory")
    cohort_manifest_path = absolute_path_without_symlinks(
        resolved_root / "cohort-manifest.json",
        label="confirmatory cohort manifest",
    )
    if not cohort_manifest_path.is_file():
        raise ControllerError("confirmatory cohort manifest is missing")
    schedule_copy_path = absolute_path_without_symlinks(
        resolved_root / "archive" / "confirmatory-schedule.json",
        label="confirmatory frozen schedule copy",
    )
    if not schedule_copy_path.is_file() or load_json(schedule_copy_path) != dict(schedule):
        raise ControllerError("confirmatory frozen schedule copy differs")
    cohort_manifest = load_json(cohort_manifest_path)
    if not isinstance(cohort_manifest, Mapping):
        raise ControllerError("confirmatory cohort manifest is malformed")
    manifest_without_hash = dict(cohort_manifest)
    recorded_hash = manifest_without_hash.pop("manifest_sha256", None)
    if recorded_hash != canonical_sha256(manifest_without_hash):
        raise ControllerError("confirmatory cohort manifest self-hash differs")
    if (
        cohort_manifest.get("schema") != CONFIRMATORY_COHORT_MANIFEST_SCHEMA
        or cohort_manifest.get("status") != "complete"
        or cohort_manifest.get("cohort_id") != schedule.get("cohort_id")
        or cohort_manifest.get("schedule_sha256") != schedule.get("schedule_sha256")
        or cohort_manifest.get("account_delta_report_scope") != "paired_cohort_once"
        or cohort_manifest.get("screening_is_diagnostic_only") is not True
    ):
        raise ControllerError("confirmatory cohort manifest contract differs")
    pair_roles = cohort_manifest.get("roles")
    if not isinstance(pair_roles, Mapping) or set(pair_roles) != {"control", "candidate"}:
        raise ControllerError("confirmatory cohort manifest role coverage differs")
    role_entries: dict[str, Any] = {}
    cohort_hashes: set[str] = set()
    for role in ("control", "candidate"):
        pair_role = pair_roles[role]
        if not isinstance(pair_role, Mapping):
            raise ControllerError(f"confirmatory {role} pair binding is malformed")
        role_entry, cohort_hash = _validate_confirmatory_role_publication(
            cohort_root=resolved_root,
            role=role,
            schedule=schedule,
            pair_role=pair_role,
        )
        role_entries[role] = role_entry
        cohort_hashes.add(cohort_hash)
    if len(cohort_hashes) != 1:
        raise ControllerError("confirmatory role publications disagree on account cohort")
    entry: dict[str, Any] = {
        "status": "complete",
        "campaign_plan_sha256": canonical_sha256(plan),
        "cohort_id": schedule["cohort_id"],
        "schedule_sha256": schedule["schedule_sha256"],
        "cohort_root": str(resolved_root),
        "schedule_path": str(schedule_copy_path),
        "schedule_file_sha256": file_sha256(schedule_copy_path),
        "cohort_manifest_path": str(cohort_manifest_path),
        "cohort_manifest_sha256": file_sha256(cohort_manifest_path),
        "roles": role_entries,
        "account_window_cohort_sha256": next(iter(cohort_hashes)),
    }
    entry["entry_sha256"] = canonical_sha256(entry)
    return entry


def _validate_confirmatory_report_input_index(
    plan: Mapping[str, Any],
    index: Mapping[str, Any],
) -> None:
    if index.get("schema") != CONFIRMATORY_REPORT_INPUT_INDEX_SCHEMA:
        raise ControllerError("confirmatory report-input index schema differs")
    without_hash = dict(index)
    recorded_hash = without_hash.pop("index_sha256", None)
    if recorded_hash != canonical_sha256(without_hash):
        raise ControllerError("confirmatory report-input index self-hash differs")
    if index.get("campaign_plan_sha256") != canonical_sha256(plan):
        raise ControllerError("confirmatory report-input index belongs to another plan")
    entries = index.get("entries")
    if not isinstance(entries, list):
        raise ControllerError("confirmatory report-input index entries are malformed")
    cohort_ids: list[str] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ControllerError("confirmatory report-input index entry is malformed")
        entry_without_hash = dict(entry)
        recorded_entry_hash = entry_without_hash.pop("entry_sha256", None)
        if recorded_entry_hash != canonical_sha256(entry_without_hash):
            raise ControllerError("confirmatory report-input entry self-hash differs")
        if (
            entry.get("campaign_plan_sha256") != canonical_sha256(plan)
            or entry.get("status") not in {"complete", "partial"}
            or not isinstance(entry.get("roles"), Mapping)
            or set(entry["roles"]) != {"control", "candidate"}
        ):
            raise ControllerError("confirmatory report-input entry contract differs")
        cohort_id = str(entry.get("cohort_id") or "")
        if SAFE_COMPONENT_RE.fullmatch(cohort_id) is None:
            raise ControllerError("confirmatory report-input cohort ID is unsafe")
        cohort_ids.append(cohort_id)
    if cohort_ids != sorted(set(cohort_ids)):
        raise ControllerError("confirmatory report-input entries are not uniquely sorted")


def _register_confirmatory_report_input_entry(
    plan: Mapping[str, Any],
    entry: Mapping[str, Any],
) -> dict[str, Any]:
    entry = dict(entry)
    entry_without_hash = dict(entry)
    recorded_entry_hash = entry_without_hash.pop("entry_sha256", None)
    if recorded_entry_hash != canonical_sha256(entry_without_hash):
        raise ControllerError("refusing an unsealed confirmatory report-input entry")
    run_root = Path(str(plan["paths"]["run_root"]))
    index_path = run_root / "confirmatory-report-inputs.json"
    if index_path.exists() or index_path.is_symlink():
        index = load_bound_json(
            index_path, label="confirmatory report-input index"
        ).value
        if not isinstance(index, Mapping):
            raise ControllerError("confirmatory report-input index is malformed")
        _validate_confirmatory_report_input_index(plan, index)
        entries = [dict(value) for value in index["entries"]]
    else:
        entries = []
    by_cohort = {str(value["cohort_id"]): value for value in entries}
    existing = by_cohort.get(str(entry["cohort_id"]))
    if existing is not None and existing != entry:
        raise ControllerError("confirmatory cohort is already registered with other evidence")
    by_cohort[str(entry["cohort_id"])] = entry
    payload: dict[str, Any] = {
        "schema": CONFIRMATORY_REPORT_INPUT_INDEX_SCHEMA,
        "campaign_plan_sha256": canonical_sha256(plan),
        "entries": [by_cohort[key] for key in sorted(by_cohort)],
    }
    payload["index_sha256"] = canonical_sha256(payload)
    if (
        not index_path.exists()
        or load_bound_json(
            index_path, label="confirmatory report-input index"
        ).value
        != payload
    ):
        secure_atomic_write_json(index_path, payload)
    return {**entry, "index_path": str(index_path), "index_sha256": payload["index_sha256"]}


def registered_confirmatory_report_input(
    plan: Mapping[str, Any],
    cohort_id: str,
) -> dict[str, Any] | None:
    index_path = Path(str(plan["paths"]["run_root"])) / "confirmatory-report-inputs.json"
    if not index_path.exists() and not index_path.is_symlink():
        return None
    index = load_bound_json(
        index_path, label="confirmatory report-input index"
    ).value
    if not isinstance(index, Mapping):
        raise ControllerError("confirmatory report-input index is malformed")
    _validate_confirmatory_report_input_index(plan, index)
    return next(
        (
            dict(entry)
            for entry in index["entries"]
            if isinstance(entry, Mapping) and entry.get("cohort_id") == cohort_id
        ),
        None,
    )


def register_confirmatory_report_input(
    plan: Mapping[str, Any],
    schedule: Mapping[str, Any],
    cohort_root: Path,
) -> dict[str, Any]:
    return _register_confirmatory_report_input_entry(
        plan,
        build_confirmatory_report_input_entry(plan, schedule, cohort_root),
    )


def register_partial_confirmatory_report_input(
    plan: Mapping[str, Any],
    schedule: Mapping[str, Any],
    cohort_root: Path,
    *,
    reason: str,
    launcher_returncode: int,
) -> dict[str, Any]:
    resolved_root = cohort_root.resolve()
    if cohort_root.is_symlink() or (cohort_root.exists() and not cohort_root.is_dir()):
        raise ControllerError("partial confirmatory cohort root is unsafe")
    role_entries: dict[str, Any] = {}
    for role in ("control", "candidate"):
        role_root = resolved_root / role
        if role_root.is_symlink():
            raise ControllerError(f"partial confirmatory {role} root is unsafe")
        manifest_path = role_root / "manifest.json"
        manifest_hash: str | None = None
        publication_status = "missing"
        if manifest_path.exists() or manifest_path.is_symlink():
            require_regular_file(manifest_path)
            manifest_hash = file_sha256(manifest_path)
            publication_status = "present_unverified"
        schedule_role = schedule.get("roles", {}).get(role)
        if not isinstance(schedule_role, Mapping):
            raise ControllerError(f"confirmatory {role} schedule descriptor is missing")
        role_entries[role] = {
            "arm_id": schedule_role.get("arm_id"),
            "experiment_id": schedule_role.get("experiment_id"),
            "root": str(role_root),
            "manifest_sha256": manifest_hash,
            "publication_status": publication_status,
        }
    cohort_manifest_path = resolved_root / "cohort-manifest.json"
    cohort_manifest_hash: str | None = None
    recorded_cohort_manifest_path: str | None = None
    if cohort_manifest_path.exists() or cohort_manifest_path.is_symlink():
        require_regular_file(cohort_manifest_path)
        recorded_cohort_manifest_path = str(cohort_manifest_path)
        cohort_manifest_hash = file_sha256(cohort_manifest_path)
    schedule_copy_path = resolved_root / "archive" / "confirmatory-schedule.json"
    schedule_path: str | None = None
    schedule_file_hash: str | None = None
    if schedule_copy_path.exists() or schedule_copy_path.is_symlink():
        require_regular_file(schedule_copy_path)
        schedule_path = str(schedule_copy_path)
        schedule_file_hash = file_sha256(schedule_copy_path)
    entry: dict[str, Any] = {
        "status": "partial",
        "campaign_plan_sha256": canonical_sha256(plan),
        "cohort_id": schedule["cohort_id"],
        "schedule_sha256": schedule["schedule_sha256"],
        "cohort_root": str(resolved_root),
        "schedule_path": schedule_path,
        "schedule_file_sha256": schedule_file_hash,
        "cohort_manifest_path": recorded_cohort_manifest_path,
        "cohort_manifest_sha256": cohort_manifest_hash,
        "roles": role_entries,
        "account_window_cohort_sha256": None,
        "failure": {
            "reason": reason,
            "launcher_returncode": launcher_returncode,
        },
    }
    entry["entry_sha256"] = canonical_sha256(entry)
    return _register_confirmatory_report_input_entry(plan, entry)


def launch_confirmatory_schedule(plan_path: Path, schedule_path: Path) -> int:
    plan_bound = load_bound_json(plan_path, label="confirmatory bridge plan")
    plan = plan_bound.value
    if not isinstance(plan, Mapping):
        raise ControllerError("confirmatory bridge plan is not an object")
    validate_plan(plan, allow_placeholders=False)
    if not isinstance(plan.get("confirmatory_bridge"), Mapping):
        raise ControllerError(
            "run-confirmatory refuses a screening/legacy plan; prepare a confirmatory-only bridge"
        )
    snapshot, snapshot_identity = validate_snapshot(plan)
    validate_runtime_freeze(
        plan,
        snapshot=snapshot,
        expected_snapshot_identity=snapshot_identity,
    )
    # All of these checks happen before mkdir, lock creation, report indexing,
    # credentials, account windows, or the launcher subprocess.
    validate_confirmatory_launcher_support(plan, snapshot=snapshot)
    validate_confirmatory_bridge_source(plan)
    derived = validate_confirmatory_bridge_derived(plan)
    schedule_bound = load_bound_json(schedule_path, label="confirmatory schedule")
    schedule = schedule_bound.value
    if not isinstance(schedule, Mapping):
        raise ControllerError("confirmatory schedule is not an object")
    validate_confirmatory_schedule(plan, schedule)
    candidate = schedule.get("roles", {}).get("candidate")
    candidate_id = str(candidate.get("arm_id") or "") if isinstance(candidate, Mapping) else ""
    descriptor = derived.get("schedules", {}).get(candidate_id)
    if (
        candidate_id not in plan["confirmatory_bridge"]["survivor_arm_ids"]
        or not isinstance(descriptor, Mapping)
        or schedule.get("schedule_sha256") != descriptor.get("schedule_sha256")
        or schedule_path.resolve() != Path(str(descriptor.get("path") or "")).resolve()
    ):
        raise ControllerError("confirmatory schedule is not the plan-bound survivor schedule")
    validate_confirmatory_root_isolation(
        run_root=Path(str(plan["paths"]["run_root"])),
        report_root=Path(str(plan["paths"]["report_root"])),
        snapshot=snapshot,
        screening_source=plan["confirmatory_bridge"]["screening_source"],
    )
    report_root = Path(str(plan["paths"]["report_root"])) / "confirmatory"
    cohort_root = report_root / str(schedule["output_name"])
    run_root = Path(str(plan["paths"]["run_root"]))
    secure_mkdir_absolute(run_root)
    lock_path = run_root / "controller.lock"
    lock_fd, lock_parent_fd, _ = _open_absolute_nofollow(
        lock_path,
        flags=os.O_RDWR | os.O_CREAT,
        mode=0o600,
        create_parents=True,
    )
    os.close(lock_parent_fd)
    with os.fdopen(lock_fd, "r+") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ControllerError("another campaign controller is active") from exc
        launcher = snapshot / str(plan["paths"]["launcher_relative"])
        launcher_fd, launcher_evidence, launcher_payload = (
            open_authenticated_confirmatory_launcher(plan, snapshot=snapshot)
        )
        sealed_launcher_fd = sealed_schedule_fd = -1
        try:
            sealed_launcher_fd = sealed_memfd(
                launcher_payload,
                label="draco-confirmatory-launcher",
                executable=True,
            )
            sealed_schedule_fd = sealed_memfd(
                schedule_bound.payload,
                label="draco-confirmatory-schedule",
                executable=False,
            )
            # No bridge evidence/index/report path has been created at this point.
            _assert_authenticated_fd_bytes(
                launcher_fd,
                launcher,
                label="confirmatory launcher",
                expected_sha256=str(launcher_evidence["raw_sha256"]),
            )
            secure_mkdir_absolute(report_root)
            registered = registered_confirmatory_report_input(
                plan,
                str(schedule["cohort_id"]),
            )
            if registered is not None:
                if registered.get("status") == "partial":
                    return 2
                current = build_confirmatory_report_input_entry(plan, schedule, cohort_root)
                if current != registered:
                    raise ControllerError(
                        "registered confirmatory publication no longer matches its evidence"
                    )
                return 0
            if cohort_root.exists() or cohort_root.is_symlink():
                _assert_authenticated_fd_bytes(
                    launcher_fd,
                    launcher,
                    label="confirmatory launcher",
                    expected_sha256=str(launcher_evidence["raw_sha256"]),
                )
                if (cohort_root / "cohort-manifest.json").is_file():
                    try:
                        register_confirmatory_report_input(plan, schedule, cohort_root)
                    except ControllerError:
                        register_partial_confirmatory_report_input(
                            plan,
                            schedule,
                            cohort_root,
                            reason="existing_cohort_publication_invalid",
                            launcher_returncode=2,
                        )
                        return 2
                    else:
                        return 0
                register_partial_confirmatory_report_input(
                    plan,
                    schedule,
                    cohort_root,
                    reason="existing_incomplete_cohort_no_unpaired_retry",
                    launcher_returncode=2,
                )
                return 2
            environment = os.environ.copy()
            environment.update(
                {
                    "DRACO_CAMPAIGN_REPORT_ROOT": str(report_root),
                    "DRACO_CAMPAIGN_REFERENCE_REPO": str(plan["paths"]["reference_repo"]),
                    "DRACO_CAMPAIGN_PYTHON": str(plan["paths"]["python"]),
                    "DRACO_CAMPAIGN_TASK_CONCURRENCY": str(CONFIRMATORY_TASK_CONCURRENCY),
                }
            )
            _assert_authenticated_fd_bytes(
                launcher_fd,
                launcher,
                label="confirmatory launcher",
                expected_sha256=str(launcher_evidence["raw_sha256"]),
            )
            command = [
                f"/proc/self/fd/{sealed_launcher_fd}",
                "--snapshot-repo",
                str(snapshot),
                "--output-name",
                str(schedule["output_name"]),
                "--groups",
                "G1",
                "--confirmatory-schedule-fd",
                str(sealed_schedule_fd),
            ]
            completed = subprocess.run(
                command,
                env=environment,
                check=False,
                pass_fds=(sealed_launcher_fd, sealed_schedule_fd),
            )
            validate_runtime_freeze(
                plan,
                snapshot=snapshot,
                expected_snapshot_identity=snapshot_identity,
            )
            assert_bridge_snapshot_unchanged(snapshot, snapshot_identity)
            _assert_authenticated_fd_bytes(
                launcher_fd,
                launcher,
                label="confirmatory launcher",
                expected_sha256=str(launcher_evidence["raw_sha256"]),
            )
            if completed.returncode == 0:
                try:
                    register_confirmatory_report_input(plan, schedule, cohort_root)
                except ControllerError:
                    register_partial_confirmatory_report_input(
                        plan,
                        schedule,
                        cohort_root,
                        reason="confirmatory_publication_validation_failed",
                        launcher_returncode=2,
                    )
                    return 2
            else:
                register_partial_confirmatory_report_input(
                    plan,
                    schedule,
                    cohort_root,
                    reason="confirmatory_launcher_failed",
                    launcher_returncode=int(completed.returncode),
                )
            return int(completed.returncode)
        finally:
            if sealed_launcher_fd >= 0:
                os.close(sealed_launcher_fd)
            if sealed_schedule_fd >= 0:
                os.close(sealed_schedule_fd)
            os.close(launcher_fd)


def preexisting_source_identity(
    plan: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Authenticate a declared old source plan/snapshot and derive its arm identity."""

    contract = preexisting_source_contract(plan)
    if contract is None:
        return None
    source_plan_path = absolute_path_without_symlinks(
        contract["source_plan_path"], label="preexisting source plan"
    )
    source_snapshot = absolute_path_without_symlinks(
        contract["source_snapshot_path"], label="preexisting source snapshot"
    )
    source_dir = absolute_path_without_symlinks(
        contract["source_output_dir"], label="preexisting source output"
    )
    if not source_snapshot.is_dir() or not source_dir.is_dir():
        raise ControllerError("preexisting source snapshot/output must be directories")
    current_run_root = Path(str(plan.get("paths", {}).get("run_root") or "")).resolve()
    current_report_root = Path(str(plan.get("paths", {}).get("report_root") or "")).resolve()
    if paths_overlap(source_dir, current_run_root) or paths_overlap(
        source_dir, current_report_root
    ):
        raise ControllerError(
            "preexisting source output overlaps the current writable run/report roots"
        )
    require_regular_file(source_plan_path)
    if file_sha256(source_plan_path) != contract["source_plan_raw_sha256"]:
        raise ControllerError("preexisting source plan/output raw identity differs")
    source_plan = load_json(source_plan_path)
    if (
        canonical_sha256(source_plan) != contract["source_plan_canonical_sha256"]
        or source_plan.get("schema") != PLAN_SCHEMA
        or preexisting_source_contract(source_plan) is not None
    ):
        raise ControllerError("preexisting source plan identity or import depth differs")
    source_arms = validate_plan(source_plan, allow_placeholders=False)
    source_arm = next(
        (arm for arm in source_arms if arm.arm_id == ANALYZER_SOURCE_ARM_ID),
        None,
    )
    if source_arm is None or source_arm.override or source_arm.analyzer_mode != "live":
        raise ControllerError("preexisting source plan lacks the canonical live source arm")
    if source_plan.get("benchmark") != plan.get("benchmark"):
        raise ControllerError("preexisting source benchmark contract differs")
    source_snapshot_identity = git_identity(source_snapshot)
    if (
        source_snapshot_identity.get("commit") != contract["source_snapshot_commit"]
        or source_snapshot_identity.get("tree") != contract["source_snapshot_tree"]
        or source_snapshot_identity.get("status")
        or source_plan.get("freeze", {}).get("snapshot_commit")
        != contract["source_snapshot_commit"]
        or source_plan.get("freeze", {}).get("snapshot_tree") != contract["source_snapshot_tree"]
    ):
        raise ControllerError("preexisting source snapshot identity differs")
    if source_dir != output_dir(source_plan, source_arm).resolve():
        raise ControllerError("preexisting source output does not belong to its source plan")
    for field, filename in (
        ("source_manifest_sha256", "manifest.json"),
        ("source_results_sha256", "results.jsonl"),
        ("source_trace_sha256", "trace.jsonl"),
    ):
        path = source_dir / filename
        require_regular_file(path)
        if file_sha256(path) != contract[field]:
            raise ControllerError(f"preexisting source {filename} raw hash differs")
    expected = arm_completion_identity(
        source_plan,
        source_arm,
        snapshot=source_snapshot,
        snapshot_identity=source_snapshot_identity,
        override=source_arm.override,
        isolated_config=True,
    )
    receipt = {
        "schema": PREEXISTING_SOURCE_SCHEMA,
        "source_plan_path": str(source_plan_path),
        "source_plan_raw_sha256": contract["source_plan_raw_sha256"],
        "source_plan_canonical_sha256": contract["source_plan_canonical_sha256"],
        "source_snapshot_path": str(source_snapshot),
        "source_snapshot_commit": source_snapshot_identity["commit"],
        "source_snapshot_tree": source_snapshot_identity["tree"],
        "source_output_dir": str(source_dir),
        "source_manifest_sha256": contract["source_manifest_sha256"],
        "source_results_sha256": contract["source_results_sha256"],
        "source_trace_sha256": contract["source_trace_sha256"],
        "expected_identity_sha256": canonical_sha256(expected),
        "contract_sha256": canonical_sha256(contract),
    }
    return expected, receipt


def launcher_effective_config_projection(
    plan: Mapping[str, Any],
    config_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Project the exact runtime fields frozen by the production launcher."""

    execution = plan.get("execution")
    if not isinstance(execution, Mapping):
        raise ControllerError("execution contract is missing from arm identity")
    projected = copy.deepcopy(dict(config_payload))
    bindings = (
        ("runner", "concurrency", "task_concurrency"),
        ("judge", "concurrency", "judge_concurrency"),
        ("generation", "max_attempts", "generation_max_attempts"),
    )
    for section_name, field_name, execution_name in bindings:
        section = projected.get(section_name)
        expected = execution.get(execution_name)
        current = section.get(field_name) if isinstance(section, Mapping) else None
        if (
            not isinstance(section, Mapping)
            or field_name not in section
            or isinstance(current, bool)
            or not isinstance(current, int)
            or current <= 0
            or isinstance(expected, bool)
            or not isinstance(expected, int)
            or expected <= 0
        ):
            raise ControllerError(
                f"arm identity cannot project {section_name}.{field_name}"
            )
        if section_name != "runner" and current != expected:
            raise ControllerError(
                f"effective {section_name}.{field_name} differs from the launcher contract"
            )
        section_copy = copy.deepcopy(dict(section))
        section_copy[field_name] = expected
        projected[section_name] = section_copy
    return projected


def arm_completion_identity(
    plan: Mapping[str, Any],
    arm: Arm,
    *,
    snapshot: Path,
    snapshot_identity: Mapping[str, str],
    override: Mapping[str, Any],
    isolated_config: bool = False,
) -> dict[str, Any]:
    if arm.arm_id == ANALYZER_SOURCE_ARM_ID:
        imported = preexisting_source_identity(plan)
        if imported is not None:
            return imported[0]
    base_config = snapshot / str(plan["paths"]["experiment_config_relative"])
    if isolated_config:
        config_payload = launcher_effective_config_projection(
            plan,
            load_effective_experiment_config_isolated(
                snapshot,
                base_config,
                override,
            ),
        )
        ensemble_payload = config_payload.get("ensemble")
        if not isinstance(ensemble_payload, Mapping):
            raise ControllerError("isolated effective config lacks ensemble settings")
        configured_candidate_order_seed = ensemble_payload.get("candidate_order_seed")
        effective_candidate_order_seed = (
            configured_candidate_order_seed
            if ensemble_payload.get("shuffle_candidates") is True
            else None
        )
    else:
        config = load_effective_experiment_config(snapshot, base_config, override)
        if not hasattr(config, "model_dump"):
            raise ControllerError("effective experiment config cannot be authenticated")
        config_payload = launcher_effective_config_projection(
            plan,
            config.model_dump(mode="json"),
        )
        configured_candidate_order_seed = config.ensemble.candidate_order_seed
        effective_candidate_order_seed = (
            config.ensemble.candidate_order_seed if config.ensemble.shuffle_candidates else None
        )
    reference_repo = Path(str(plan["paths"]["reference_repo"])).resolve()
    runner_path = snapshot / "scripts/run_draco_routing_experiment.py"
    resume_runner_path = snapshot / "scripts/run_draco_routing_experiment_resume.py"
    return {
        "arm_id": arm.arm_id,
        "output_name": arm.output_name,
        "run_id": str(plan["run_id"]),
        "output_dir": str(output_dir(plan, arm).resolve()),
        "snapshot": str(snapshot.resolve()),
        "snapshot_commit": snapshot_identity["commit"],
        "runner_identities": {
            str(runner_path.resolve()): file_sha256(runner_path),
            str(resume_runner_path.resolve()): file_sha256(resume_runner_path),
        },
        "benchmark_path": str((reference_repo / "data/draco/mini.jsonl").resolve()),
        "reference_config_path": str((reference_repo / ".local-state/config.toml").resolve()),
        "benchmark_sha256": str(plan["benchmark"]["input_sha256"]),
        "task_ids": sorted(plan["benchmark"]["task_ids"]),
        "task_concurrency": int(plan["execution"]["task_concurrency"]),
        "judge_concurrency": int(plan["execution"]["judge_concurrency"]),
        "generation_max_attempts": int(plan["execution"]["generation_max_attempts"]),
        "override_sha256": canonical_sha256(override),
        "effective_config_sha256": canonical_sha256(config_payload),
        "candidate_order_seed_evidence": {
            "required": arm.experiment_id == "P0.5-36",
            "configured_candidate_order_seed": configured_candidate_order_seed,
            "effective_candidate_order_seed": effective_candidate_order_seed,
        },
    }


def portable_arm_publication_identity(identity: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize only snapshot-local paths while retaining content identity."""

    snapshot_value = str(identity.get("snapshot") or "")
    snapshot = Path(snapshot_value)
    runner_identities = identity.get("runner_identities")
    if not snapshot.is_absolute() or not isinstance(runner_identities, Mapping):
        raise ControllerError("arm publication identity has invalid snapshot paths")
    snapshot = snapshot.resolve()
    portable_runners: dict[str, str] = {}
    for raw_path, raw_sha256 in runner_identities.items():
        runner = Path(str(raw_path))
        sha256 = str(raw_sha256 or "")
        if not runner.is_absolute() or SHA256_RE.fullmatch(sha256) is None:
            raise ControllerError("arm publication identity has invalid runner binding")
        try:
            relative = runner.resolve().relative_to(snapshot).as_posix()
        except ValueError as exc:
            raise ControllerError(
                "arm publication runner is outside its frozen snapshot"
            ) from exc
        if not relative or relative in portable_runners:
            raise ControllerError("arm publication runner binding is ambiguous")
        portable_runners[relative] = sha256
    portable = copy.deepcopy(dict(identity))
    portable["snapshot"] = "$SNAPSHOT_ROOT"
    portable["runner_identities"] = dict(sorted(portable_runners.items()))
    return portable


def authenticate_preexisting_source(plan: Mapping[str, Any]) -> dict[str, Any] | None:
    imported = preexisting_source_identity(plan)
    if imported is None:
        return None
    expected, receipt = imported
    source_dir = Path(str(receipt["source_output_dir"]))
    complete, evidence = inspect_complete_arm(
        source_dir,
        expected_task_ids=set(plan["benchmark"]["task_ids"]),
        expected_task_concurrency=int(plan["execution"]["task_concurrency"]),
        expected_identity=expected,
    )
    if not complete:
        raise ControllerError(
            "preexisting Analyzer source publication is not authenticated: "
            + str(evidence.get("reason") or "unknown")
        )
    receipt["expected_publication_identity"] = copy.deepcopy(expected)
    receipt["publication_evidence"] = copy.deepcopy(evidence)
    receipt["publication_evidence_sha256"] = canonical_sha256(evidence)
    receipt["receipt_sha256"] = canonical_sha256(receipt)
    return receipt


def _preexisting_source_package_dir(plan: Mapping[str, Any]) -> Path:
    return Path(str(plan["paths"]["run_root"])) / "preexisting-source-package"


def _verify_preexisting_source_package(
    plan: Mapping[str, Any],
    package_dir: Path,
) -> dict[str, Any]:
    if package_dir.is_symlink() or not package_dir.is_dir():
        raise ControllerError("preexisting source package directory is unsafe")
    receipt_path = package_dir / "import-receipt.json"
    receipt = load_json(receipt_path)
    if not isinstance(receipt, Mapping):
        raise ControllerError("preexisting source package receipt is malformed")
    receipt_copy = copy.deepcopy(dict(receipt))
    verify_bare_document_self_hash(
        receipt_copy,
        field="receipt_sha256",
        label="preexisting source package",
    )
    contract = preexisting_source_contract(plan)
    if contract is None:
        raise ControllerError("preexisting source package exists without an import contract")
    if (
        receipt_copy.get("schema") != PREEXISTING_SOURCE_PACKAGE_SCHEMA
        or Path(str(receipt_copy.get("package_dir") or "")).resolve() != package_dir.resolve()
        or receipt_copy.get("contract_sha256") != canonical_sha256(contract)
    ):
        raise ControllerError("preexisting source package contract differs")
    source_authentication = copy.deepcopy(receipt_copy)
    for field in (
        "receipt_sha256",
        "package_dir",
        "package_artifacts",
        "source_plan_package_sha256",
        "source_snapshot_archive_sha256",
        "source_snapshot_package_dir",
        "source_snapshot_package_tree_sha256",
        "source_authentication_receipt_sha256",
    ):
        source_authentication.pop(field, None)
    source_authentication["schema"] = PREEXISTING_SOURCE_SCHEMA
    if receipt_copy.get("source_authentication_receipt_sha256") != canonical_sha256(
        source_authentication
    ):
        raise ControllerError("preexisting source authentication receipt differs")
    artifact_records = receipt_copy.get("package_artifacts")
    if not isinstance(artifact_records, Mapping):
        raise ControllerError("preexisting source package artifact inventory is missing")
    required_names = {
        "manifest.json",
        "results.jsonl",
        "trace.jsonl",
        "audit.json",
        "openrouter-non-byok-campaign-proof.json",
    }
    if set(artifact_records) != required_names:
        raise ControllerError("preexisting source package artifact inventory differs")
    for name, record in artifact_records.items():
        path = package_dir / name
        require_regular_file(path)
        if (
            not isinstance(record, Mapping)
            or record.get("size_bytes") != path.stat().st_size
            or record.get("sha256") != file_sha256(path)
        ):
            raise ControllerError(f"preexisting source package {name} binding differs")
    source_plan_copy = package_dir / "source-plan.json"
    source_archive = package_dir / "source-snapshot.tar"
    source_snapshot_copy = package_dir / "source-snapshot"
    for path in (source_plan_copy, source_archive):
        require_regular_file(path)
    if source_snapshot_copy.is_symlink() or not source_snapshot_copy.is_dir():
        raise ControllerError("preexisting source snapshot package is unsafe")
    if (
        receipt_copy.get("source_plan_package_sha256") != file_sha256(source_plan_copy)
        or receipt_copy.get("source_plan_package_sha256")
        != receipt_copy.get("source_plan_raw_sha256")
        or receipt_copy.get("source_snapshot_archive_sha256") != file_sha256(source_archive)
        or receipt_copy.get("source_snapshot_package_tree_sha256")
        != regular_directory_tree_sha256(source_snapshot_copy)
        or Path(str(receipt_copy.get("source_snapshot_package_dir") or "")).resolve()
        != source_snapshot_copy.resolve()
    ):
        raise ControllerError("preexisting source plan/snapshot package binding differs")
    authenticate_published_arm_artifacts(package_dir)
    for field, filename in (
        ("source_manifest_sha256", "manifest.json"),
        ("source_results_sha256", "results.jsonl"),
        ("source_trace_sha256", "trace.jsonl"),
    ):
        if receipt_copy.get(field) != file_sha256(package_dir / filename):
            raise ControllerError(f"preexisting source package {filename} source hash differs")
    expected_identity = receipt_copy.get("expected_publication_identity")
    publication_evidence = receipt_copy.get("publication_evidence")
    if (
        not isinstance(expected_identity, Mapping)
        or receipt_copy.get("expected_identity_sha256") != canonical_sha256(expected_identity)
        or not isinstance(publication_evidence, Mapping)
        or receipt_copy.get("publication_evidence_sha256") != canonical_sha256(publication_evidence)
    ):
        raise ControllerError("preexisting source package publication receipt differs")
    return receipt_copy


def materialize_preexisting_source(plan: Mapping[str, Any]) -> dict[str, Any] | None:
    """Freeze an authenticated old publication into the controller-owned run root."""

    if preexisting_source_contract(plan) is None:
        return None
    package_dir = _preexisting_source_package_dir(plan)
    if package_dir.exists() or package_dir.is_symlink():
        return _verify_preexisting_source_package(plan, package_dir)
    authenticated = authenticate_preexisting_source(plan)
    assert authenticated is not None
    source_dir = Path(str(authenticated["source_output_dir"]))
    run_root = Path(str(plan["paths"]["run_root"])).resolve()
    report_root = Path(str(plan["paths"]["report_root"])).resolve()
    if paths_overlap(source_dir, run_root) or paths_overlap(source_dir, report_root):
        raise ControllerError(
            "preexisting source output overlaps the current writable run/report roots"
        )
    staging_dir = package_dir.with_name(
        f".{package_dir.name}.tmp.{os.getpid()}.{os.urandom(4).hex()}"
    )
    staging_dir.mkdir(mode=0o700)
    required_names = (
        "manifest.json",
        "results.jsonl",
        "trace.jsonl",
        "audit.json",
        "openrouter-non-byok-campaign-proof.json",
    )
    package_artifacts: dict[str, Any] = {}
    for name in required_names:
        payload = stable_regular_file_bytes(source_dir / name)
        destination = staging_dir / name
        atomic_write_bytes(destination, payload)
        package_artifacts[name] = {
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    source_plan_payload = stable_regular_file_bytes(Path(str(authenticated["source_plan_path"])))
    atomic_write_bytes(staging_dir / "source-plan.json", source_plan_payload)
    source_snapshot = Path(str(authenticated["source_snapshot_path"]))
    before_snapshot_identity = git_identity(source_snapshot)
    archive_result = subprocess.run(
        [
            "git",
            "archive",
            "--format=tar",
            str(authenticated["source_snapshot_commit"]),
        ],
        cwd=source_snapshot,
        check=False,
        capture_output=True,
    )
    after_snapshot_identity = git_identity(source_snapshot)
    if (
        archive_result.returncode
        or before_snapshot_identity != after_snapshot_identity
        or before_snapshot_identity.get("commit") != authenticated["source_snapshot_commit"]
        or before_snapshot_identity.get("tree") != authenticated["source_snapshot_tree"]
        or before_snapshot_identity.get("status")
    ):
        raise ControllerError("preexisting source snapshot changed while being frozen")
    source_snapshot_archive = bytes(archive_result.stdout)
    atomic_write_bytes(staging_dir / "source-snapshot.tar", source_snapshot_archive)
    source_snapshot_package_staging = staging_dir / "source-snapshot"
    extract_regular_git_archive(source_snapshot_archive, source_snapshot_package_staging)
    packaged_source_plan = load_json(staging_dir / "source-plan.json")
    if not isinstance(packaged_source_plan, Mapping):
        raise ControllerError("packaged preexisting source plan is malformed")
    packaged_source_arms = validate_plan(packaged_source_plan, allow_placeholders=False)
    packaged_source_arm = next(
        (arm for arm in packaged_source_arms if arm.arm_id == ANALYZER_SOURCE_ARM_ID),
        None,
    )
    if packaged_source_arm is None:
        raise ControllerError("packaged preexisting source plan lacks its source arm")
    packaged_expected_identity = arm_completion_identity(
        packaged_source_plan,
        packaged_source_arm,
        snapshot=source_snapshot_package_staging,
        snapshot_identity={
            "commit": str(authenticated["source_snapshot_commit"]),
            "tree": str(authenticated["source_snapshot_tree"]),
        },
        override=packaged_source_arm.override,
        isolated_config=True,
    )
    authenticated_identity = authenticated.get("expected_publication_identity")
    if not isinstance(authenticated_identity, Mapping) or portable_arm_publication_identity(
        packaged_expected_identity
    ) != portable_arm_publication_identity(authenticated_identity):
        raise ControllerError(
            "packaged source plan/snapshot publication identity differs from authentication"
        )
    authenticate_published_arm_artifacts(staging_dir)
    for field, filename in (
        ("source_manifest_sha256", "manifest.json"),
        ("source_results_sha256", "results.jsonl"),
        ("source_trace_sha256", "trace.jsonl"),
    ):
        if authenticated[field] != package_artifacts[filename]["sha256"]:
            raise ControllerError(f"preexisting source changed while freezing {filename}")
    source_authentication_receipt_sha256 = authenticated["receipt_sha256"]
    receipt = copy.deepcopy(dict(authenticated))
    receipt.pop("receipt_sha256", None)
    receipt.update(
        {
            "schema": PREEXISTING_SOURCE_PACKAGE_SCHEMA,
            "package_dir": str(package_dir.resolve()),
            "package_artifacts": package_artifacts,
            "source_plan_package_sha256": hashlib.sha256(source_plan_payload).hexdigest(),
            "source_snapshot_archive_sha256": hashlib.sha256(source_snapshot_archive).hexdigest(),
            "source_snapshot_package_dir": str((package_dir / "source-snapshot").resolve()),
            "source_snapshot_package_tree_sha256": regular_directory_tree_sha256(
                source_snapshot_package_staging
            ),
            "source_authentication_receipt_sha256": (source_authentication_receipt_sha256),
        }
    )
    receipt["receipt_sha256"] = canonical_sha256(receipt)
    atomic_write_json(staging_dir / "import-receipt.json", receipt)
    os.rename(staging_dir, package_dir)
    directory_fd = os.open(package_dir.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return _verify_preexisting_source_package(plan, package_dir)


def launch_arm(
    plan: Mapping[str, Any],
    arm: Arm,
    *,
    snapshot: Path,
    override: Mapping[str, Any],
) -> int:
    arm_report_root = Path(str(plan["paths"]["report_root"])) / arm.directory_name
    arm_report_root.mkdir(parents=True, exist_ok=True)
    launcher = snapshot / str(plan["paths"]["launcher_relative"])
    command = [
        str(launcher),
        "--snapshot-repo",
        str(snapshot),
        "--output-name",
        arm.output_name,
        "--groups",
        "G1",
        "--experiment-config-override-json",
        json.dumps(override, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    ]
    environment = os.environ.copy()
    environment.update(
        {
            "DRACO_CAMPAIGN_REPORT_ROOT": str(arm_report_root),
            "DRACO_CAMPAIGN_REFERENCE_REPO": str(plan["paths"]["reference_repo"]),
            "DRACO_CAMPAIGN_PYTHON": str(plan["paths"]["python"]),
            "DRACO_CAMPAIGN_TASK_CONCURRENCY": "6",
        }
    )
    print(f"[{utc_now()}] START {arm.arm_id}", flush=True)
    completed = subprocess.run(command, env=environment, check=False)
    print(f"[{utc_now()}] END {arm.arm_id} rc={completed.returncode}", flush=True)
    return int(completed.returncode)


def status_path(plan: Mapping[str, Any]) -> Path:
    return Path(str(plan["paths"]["run_root"])) / "status.json"


def load_or_initialize_status(
    plan: Mapping[str, Any],
    arms: Sequence[Arm],
    *,
    plan_sha256: str,
    snapshot_identity: Mapping[str, str],
) -> dict[str, Any]:
    path = status_path(plan)
    if path.exists():
        status_payload = load_json(path)
        if status_payload.get("schema") != STATUS_SCHEMA:
            raise ControllerError("status schema differs")
        if status_payload.get("campaign_plan_sha256") != plan_sha256:
            raise ControllerError("status is bound to a different campaign plan")
        if status_payload.get("snapshot_commit") != snapshot_identity["commit"]:
            raise ControllerError("status is bound to a different snapshot commit")
        schedule = plan["execution"]["schedule"]
        if status_payload.get("schedule_sha256") != canonical_sha256(schedule):
            raise ControllerError("status is bound to a different execution schedule")
        status_arms = status_payload.get("arms")
        if not isinstance(status_arms, Mapping):
            raise ControllerError("status arm inventory is malformed")
        for ordinal, arm in enumerate(arms, start=1):
            state = status_arms.get(arm.arm_id)
            if (
                not isinstance(state, Mapping)
                or state.get("schedule_ordinal") != ordinal
                or state.get("anchor_arm_id") != schedule["anchor_by_arm_id"][arm.arm_id]
            ):
                raise ControllerError(f"status schedule binding differs for {arm.arm_id}")
        return status_payload
    payload = initialize_status(
        plan,
        arms,
        plan_sha256=plan_sha256,
        snapshot_identity=snapshot_identity,
    )
    atomic_write_json(path, payload)
    return payload


def update_status(path: Path, payload: dict[str, Any]) -> None:
    payload["updated_at"] = utc_now()
    atomic_write_json(path, payload)


def schedule_anchor_launch_gate(
    plan: Mapping[str, Any],
    arm: Arm,
    *,
    status: Mapping[str, Any],
    authenticated_anchor_ids: set[str],
) -> tuple[bool, dict[str, Any]]:
    """Fail closed unless this run already authenticated the frozen anchor."""

    anchor_id = str(plan["execution"]["schedule"]["anchor_by_arm_id"][arm.arm_id])
    if anchor_id == arm.arm_id:
        return True, {}
    status_arms = status.get("arms")
    anchor_state = (
        status_arms.get(anchor_id)
        if isinstance(status_arms, Mapping) and isinstance(status_arms.get(anchor_id), Mapping)
        else {}
    )
    recorded_state = str(anchor_state.get("state") or "unknown")
    failure = {
        "reason": "anchor_not_succeeded",
        "anchor_arm_id": anchor_id,
        "anchor_state": recorded_state,
        "anchor_authenticated": False,
    }
    if recorded_state != "succeeded" or anchor_id not in authenticated_anchor_ids:
        return False, failure
    return True, {
        "anchor_arm_id": anchor_id,
        "anchor_state": recorded_state,
        "anchor_authenticated": True,
    }


def validate_offline_unique_replay_overlay_count(
    unique_overlays: Mapping[str, Any],
) -> None:
    if len(unique_overlays) != EXPECTED_OFFLINE_UNIQUE_REPLAY_OVERLAYS:
        raise ControllerError(
            "offline unique replay overlay count differs from the frozen matrix: "
            f"expected {EXPECTED_OFFLINE_UNIQUE_REPLAY_OVERLAYS}, "
            f"got {len(unique_overlays)}"
        )


def prepare_derived(
    plan: Mapping[str, Any],
    arms: Sequence[Arm],
    *,
    snapshot: Path,
    snapshot_identity: Mapping[str, str],
    plan_sha256: str,
    source_arm: Arm,
    source_dir: Path,
) -> dict[str, Any]:
    run_root = Path(str(plan["paths"]["run_root"]))
    receipt_root = run_root / "receipts"
    source_import = materialize_preexisting_source(plan)
    if (
        source_import is not None
        and Path(str(source_import["source_output_dir"])).resolve() != source_dir.resolve()
    ):
        raise ControllerError("preexisting source import/output directory differs")
    source_consumption_dir = (
        Path(str(source_import["package_dir"])) if source_import is not None else source_dir
    )
    artifact = extract_analyzer_artifact(
        source_arm=source_arm,
        source_dir=source_consumption_dir,
        destination=run_root / "frozen-analyzer-profiles.json",
        expected_task_ids=set(plan["benchmark"]["task_ids"]),
        snapshot=snapshot,
        snapshot_identity=snapshot_identity,
        plan_sha256=plan_sha256,
        replay_schema=str(frozen_replay_contract(plan)["schema"]),
        allow_deterministic_router_fallback=bool(
            analyzer_source_policy(plan)["allow_deterministic_router_fallback"]
        ),
        source_import_evidence=source_import,
    )
    p99 = derive_analyzer_p99_receipt(
        artifact,
        destination=receipt_root / "P0.5-06-analyzer-p99.json",
        plan_sha256=plan_sha256,
    )
    noop = make_noop_temperature_receipt(
        plan,
        snapshot=snapshot,
        snapshot_identity=snapshot_identity,
        destination=receipt_root / "P0.5-07-no-op.json",
        plan_sha256=plan_sha256,
    )

    base_config = snapshot / str(plan["paths"]["experiment_config_relative"])
    expected_task_ids = set(plan["benchmark"]["task_ids"])
    baseline_overlay = make_replay_overlay(plan, artifact)
    baseline_dry_plans, baseline_dry_evidence = run_main_dry_replay(
        plan,
        snapshot=snapshot,
        snapshot_identity=snapshot_identity,
        overlay=baseline_overlay,
        expected_task_ids=expected_task_ids,
        label="baseline-frozen-replay",
    )
    live_plans = _source_selection_plans(
        source_consumption_dir / "trace.jsonl",
        expected_task_ids=expected_task_ids,
        require_dry_replay=False,
    )
    baseline_config = load_effective_experiment_config(
        snapshot,
        base_config,
        baseline_overlay,
    )
    helpers = _runtime_helpers(snapshot)
    baseline_ensemble_fields = set(
        getattr(baseline_config.ensemble, "model_fields_set", set()) or set()
    )
    production_cap_explicit = "proposer_max_tokens_cap" in baseline_ensemble_fields
    production_cap_key = "explicit" if production_cap_explicit else "implicit"
    baseline_projection_by_explicit: dict[str, dict[str, Any]] = {}
    baseline_equivalence: dict[str, Any] = {}
    for explicit in (False, True):
        key = "explicit" if explicit else "implicit"
        live_projection = request_visible_selection_projection(
            snapshot=snapshot,
            config=baseline_config,
            selections=live_plans,
            max_tokens_cap_explicit=explicit,
        )
        dry_projection = request_visible_selection_projection(
            snapshot=snapshot,
            config=baseline_config,
            selections=baseline_dry_plans,
            max_tokens_cap_explicit=explicit,
        )
        comparison = compare_behavior_projections(
            live_projection,
            dry_projection,
            expected_task_count=EXPECTED_TASK_COUNT,
        )
        if comparison["changed_task_count"] != 0:
            raise ControllerError(
                "baseline frozen replay dry route differs from live E0 selection projection"
            )
        baseline_projection_by_explicit[key] = dry_projection
        baseline_equivalence[key] = comparison

    unique_overlays: dict[str, dict[str, Any]] = {}
    for arm in arms:
        if arm.analyzer_mode != "frozen_replay":
            continue
        candidate_override = resolve_arm_override(
            plan,
            arm,
            artifact=artifact,
            p99_receipt=p99,
        )
        overlay_sha = canonical_sha256(candidate_override)
        unique = unique_overlays.setdefault(
            overlay_sha,
            {"overlay": candidate_override, "arm_ids": []},
        )
        unique["arm_ids"].append(arm.arm_id)
    validate_offline_unique_replay_overlay_count(unique_overlays)

    offline_by_arm: dict[str, Any] = {}
    unique_receipts: dict[str, Any] = {}
    baseline_overlay_sha = canonical_sha256(baseline_overlay)
    for overlay_sha, unique in sorted(unique_overlays.items()):
        candidate_overlay = unique["overlay"]
        arm_ids = sorted(unique["arm_ids"])
        experiments = {
            next(arm.experiment_id for arm in arms if arm.arm_id == arm_id) for arm_id in arm_ids
        }
        budget_gated = bool(experiments & PRODUCTION_BUDGET_GATE_EXPERIMENTS)
        comparisons: dict[str, Any] = {}
        candidate_projection_by_explicit: dict[str, dict[str, Any]] = {}
        projection_error: dict[str, Any] | None = None
        try:
            if overlay_sha == baseline_overlay_sha:
                candidate_plans = baseline_dry_plans
                dry_evidence = baseline_dry_evidence
            else:
                candidate_plans, dry_evidence = run_main_dry_replay(
                    plan,
                    snapshot=snapshot,
                    snapshot_identity=snapshot_identity,
                    overlay=candidate_overlay,
                    expected_task_ids=expected_task_ids,
                    label=arm_ids[0],
                )
            candidate_config = load_effective_experiment_config(
                snapshot,
                base_config,
                candidate_overlay,
            )
            candidate_fields = set(
                getattr(candidate_config.ensemble, "model_fields_set", set()) or set()
            )
            if ("proposer_max_tokens_cap" in candidate_fields) != production_cap_explicit:
                raise ControllerError(
                    "candidate proposer-cap explicitness differs from production baseline"
                )
            for explicit in (False, True):
                key = "explicit" if explicit else "implicit"
                candidate_projection = request_visible_selection_projection(
                    snapshot=snapshot,
                    config=candidate_config,
                    selections=candidate_plans,
                    max_tokens_cap_explicit=explicit,
                )
                candidate_projection_by_explicit[key] = candidate_projection
                comparisons[key] = compare_behavior_projections(
                    baseline_projection_by_explicit[key],
                    candidate_projection,
                    expected_task_count=EXPECTED_TASK_COUNT,
                )
        except Exception as exc:  # noqa: BLE001 - uncertainty forces a paid run
            error_message = str(exc)
            projection_error = {
                "error_class": type(exc).__name__,
                "message_sha256": hashlib.sha256(
                    error_message.encode("utf-8", errors="replace")
                ).hexdigest(),
            }
            dry_evidence = {
                "status": "projection_failed",
                "attempted": overlay_sha != baseline_overlay_sha,
                "overlay_sha256": overlay_sha,
                "error": projection_error,
            }
        complete_budget_proof = bool(
            projection_error is None and helpers["draco_request_budget_rebinding_disabled"]
        )
        decision = offline_effect_decision(
            comparisons=comparisons,
            arm_ids=arm_ids,
            budget_gated=budget_gated,
            production_budget_projection_complete=complete_budget_proof,
            projection_uncertain=projection_error is not None,
        )
        temperature_analysis_scope: dict[str, Any] | None = None
        if "P0.5-11" in experiments and projection_error is None:
            temperature_tasks = []
            for task_id, task_projection in sorted(
                candidate_projection_by_explicit[production_cap_key].items()
            ):
                members = [
                    {
                        "role": row["role"],
                        "model": row["identity"].split(":", 1)[1],
                        "temperature_parameter_sent": row["temperature_parameter_sent"],
                        "wire_temperature": row["wire_temperature"],
                    }
                    for row in task_projection["member_requests"]
                ]
                temperature_tasks.append(
                    {
                        "task_id": task_id,
                        "members": members,
                        "has_wire_temperature_member": any(
                            row["temperature_parameter_sent"] for row in members
                        ),
                    }
                )
            temperature_analysis_scope = {
                "schema": "opensquilla.draco.temperature-wire-analysis-scope/v1",
                "status": "exact_production_compat_projection",
                "tasks": temperature_tasks,
                "analyzable_task_ids": [
                    row["task_id"]
                    for row in temperature_tasks
                    if row["has_wire_temperature_member"]
                ],
            }
        receipt: dict[str, Any] = {
            "schema": RECEIPT_SCHEMA,
            "kind": "offline-main-runner-behavior-effect",
            "created_at": utc_now(),
            "arm_ids": arm_ids,
            "experiment_ids": sorted(experiments),
            "campaign_plan_sha256": plan_sha256,
            "source_artifact_sha256": artifact["artifact_sha256"],
            "overlay_sha256": overlay_sha,
            "decision": decision,
            "dry_run": dry_evidence,
            "comparison_by_proposer_cap_explicitness": comparisons,
            "production_proposer_cap_explicitness": production_cap_key,
            "projection_uncertainty": projection_error,
            "actual_sent_temperature_contract": (
                "production openai._should_send_temperature with official provider host"
            ),
            "shuffle_behavior_field": "effective_shuffle_candidates",
            "production_budget_gate": budget_gated,
            "production_budget_projection_complete": complete_budget_proof,
            "member_request_budget_rebinding": False,
            "member_request_budget_rebinding_proven_disabled": bool(
                helpers["draco_request_budget_rebinding_disabled"]
            ),
            "context_cap_contract": (
                "member request-budget rebinding is false; production context-window "
                "bindings would only rederive provider_request_max_chars, not output max_tokens"
            ),
            "request_budget_binding": (
                "disabled by DRACO build_experiment_provider and production builder default"
                if complete_budget_proof
                else "unproven; arm forced live"
            ),
            "helper_source_sha256": {
                "ensemble": helpers["ensemble_source_sha256"],
                "openai": helpers["openai_source_sha256"],
                "runner": helpers["runner_source_sha256"],
                "aggregator_prompt": helpers["aggregator_prompt_source_sha256"],
            },
        }
        if temperature_analysis_scope is not None:
            receipt["temperature_analysis_scope"] = temperature_analysis_scope
        receipt["receipt_sha256"] = canonical_sha256(receipt)
        destination = receipt_root / f"offline-overlay-{overlay_sha}.json"
        atomic_write_json(destination, receipt)
        unique_receipts[overlay_sha] = {
            **receipt,
            "path": str(destination),
            "file_sha256": file_sha256(destination),
        }
        for arm_id in arm_ids:
            offline_by_arm[arm_id] = {
                "decision": decision,
                "overlay_sha256": overlay_sha,
                "receipt_path": str(destination),
                "receipt_sha256": receipt["receipt_sha256"],
            }
            if temperature_analysis_scope is not None:
                offline_by_arm[arm_id]["temperature_analysis_scope"] = temperature_analysis_scope
                # Exact reporter-facing convenience schema required by the
                # P0.5-11 contract. The richer scope above remains stable.
                offline_by_arm[arm_id]["tasks"] = copy.deepcopy(temperature_analysis_scope["tasks"])

    derived: dict[str, Any] = {
        "schema": DERIVED_SCHEMA,
        "created_at": utc_now(),
        "campaign_plan_sha256": plan_sha256,
        "snapshot_commit": snapshot_identity["commit"],
        "snapshot_tree": snapshot_identity["tree"],
        "source_arm_id": source_arm.arm_id,
        "source_output_dir": str(source_dir),
        "source_consumption_dir": str(source_consumption_dir),
        "preexisting_source_import": source_import,
        "frozen_analyzer_artifact": {
            "path": str(run_root / "frozen-analyzer-profiles.json"),
            "artifact_sha256": artifact["artifact_sha256"],
            "file_sha256": file_sha256(run_root / "frozen-analyzer-profiles.json"),
        },
        "p0_5_06": p99,
        "p0_5_07": noop,
        "baseline_dry_replay": {
            **baseline_dry_evidence,
            "equivalence": baseline_equivalence,
        },
        "offline_effect": offline_by_arm,
        "offline_unique_overlays": unique_receipts,
        "offline_unique_overlay_count": len(unique_receipts),
    }
    derived["derived_plan_sha256"] = canonical_sha256(derived)
    atomic_write_json(run_root / "derived-plan.json", derived)
    return derived


def load_derived(
    plan: Mapping[str, Any], plan_sha256: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    run_root = Path(str(plan["paths"]["run_root"]))
    derived = load_json(run_root / "derived-plan.json")
    if derived.get("schema") != DERIVED_SCHEMA:
        raise ControllerError("derived plan schema differs")
    if derived.get("campaign_plan_sha256") != plan_sha256:
        raise ControllerError("derived plan is bound to a different campaign plan")
    descriptor = derived["frozen_analyzer_artifact"]
    artifact_path = Path(str(descriptor["path"]))
    artifact = load_json(artifact_path)
    if descriptor.get("file_sha256") != file_sha256(artifact_path):
        raise ControllerError("frozen Analyzer artifact raw hash differs")
    if descriptor.get("artifact_sha256") != artifact.get("artifact_sha256"):
        raise ControllerError("frozen Analyzer artifact semantic hash differs")
    artifact_without_hash = dict(artifact)
    recorded_artifact_hash = artifact_without_hash.pop("artifact_sha256", None)
    if recorded_artifact_hash != canonical_sha256(artifact_without_hash):
        raise ControllerError("frozen Analyzer artifact self-hash differs")
    derived_without_hash = dict(derived)
    recorded_derived_hash = derived_without_hash.pop("derived_plan_sha256", None)
    if recorded_derived_hash != canonical_sha256(derived_without_hash):
        raise ControllerError("derived plan self-hash differs")
    if (
        derived.get("snapshot_commit") != plan["freeze"]["snapshot_commit"]
        or derived.get("snapshot_tree") != plan["freeze"]["snapshot_tree"]
        or derived.get("source_arm_id") != ANALYZER_SOURCE_ARM_ID
    ):
        raise ControllerError("derived plan source/snapshot identity differs")
    expected_import = materialize_preexisting_source(plan)
    if derived.get("preexisting_source_import") != expected_import:
        raise ControllerError("derived preexisting source import receipt differs")
    if artifact.get("task_ids") != sorted(plan["benchmark"]["task_ids"]):
        raise ControllerError("frozen Analyzer artifact task identity differs")
    source = artifact.get("source")
    if not isinstance(source, Mapping):
        raise ControllerError("frozen Analyzer artifact source binding is missing")
    expected_source_commit = (
        expected_import["source_snapshot_commit"]
        if expected_import is not None
        else plan["freeze"]["snapshot_commit"]
    )
    expected_source_tree = (
        expected_import["source_snapshot_tree"]
        if expected_import is not None
        else plan["freeze"]["snapshot_tree"]
    )
    if (
        source.get("snapshot_commit") != expected_source_commit
        or source.get("snapshot_tree") != expected_source_tree
        or source.get("replay_snapshot_commit") != plan["freeze"]["snapshot_commit"]
        or source.get("replay_snapshot_tree") != plan["freeze"]["snapshot_tree"]
        or source.get("preexisting_source_import_receipt_sha256")
        != (expected_import.get("receipt_sha256") if expected_import is not None else None)
    ):
        raise ControllerError("frozen Analyzer source/replay snapshot binding differs")
    source_dir = Path(str(derived.get("source_output_dir") or ""))
    source_consumption_dir = Path(str(derived.get("source_consumption_dir") or ""))
    if (
        source_consumption_dir.resolve() != Path(str(source.get("output_dir") or "")).resolve()
        or source_dir.resolve() != Path(str(source.get("original_output_dir") or "")).resolve()
    ):
        raise ControllerError("derived/source Analyzer output directory differs")
    if expected_import is not None:
        if source_consumption_dir.resolve() != Path(str(expected_import["package_dir"])).resolve():
            raise ControllerError("derived Analyzer source package directory differs")
    else:
        source_arm = next(arm for arm in expand_arms(plan) if arm.arm_id == ANALYZER_SOURCE_ARM_ID)
        snapshot = Path(str(plan["paths"]["snapshot"])).resolve()
        source_identity = arm_completion_identity(
            plan,
            source_arm,
            snapshot=snapshot,
            snapshot_identity={
                "commit": str(plan["freeze"]["snapshot_commit"]),
                "tree": str(plan["freeze"]["snapshot_tree"]),
            },
            override=source_arm.override,
        )
        source_complete, source_evidence = inspect_complete_arm(
            source_dir,
            expected_task_ids=set(plan["benchmark"]["task_ids"]),
            expected_task_concurrency=int(plan["execution"]["task_concurrency"]),
            expected_identity=source_identity,
        )
        if not source_complete:
            raise ControllerError(
                "derived Analyzer source arm is no longer authenticated: "
                + str(source_evidence.get("reason") or "unknown")
            )
    for field, filename in (
        ("manifest_sha256", "manifest.json"),
        ("trace_sha256", "trace.jsonl"),
    ):
        if source.get(field) != file_sha256(source_consumption_dir / filename):
            raise ControllerError(f"frozen Analyzer source {filename} binding differs")
    replay = artifact.get("replay_payload")
    if (
        not isinstance(replay, Mapping)
        or replay.get("schema") != frozen_replay_contract(plan)["schema"]
        or replay.get("source_results_sha256")
        != file_sha256(source_consumption_dir / "results.jsonl")
    ):
        raise ControllerError("frozen Analyzer source results binding differs")

    for label, filename in (
        ("p0_5_06", "P0.5-06-analyzer-p99.json"),
        ("p0_5_07", "P0.5-07-no-op.json"),
    ):
        embedded = derived.get(label)
        if not isinstance(embedded, Mapping):
            raise ControllerError(f"derived {label} receipt is missing")
        verify_bare_document_self_hash(
            embedded,
            field="receipt_sha256",
            label=label,
        )
        receipt_path = run_root / "receipts" / filename
        if load_json(receipt_path) != embedded:
            raise ControllerError(f"derived {label} receipt file differs")

    unique = derived.get("offline_unique_overlays")
    offline = derived.get("offline_effect")
    if not isinstance(unique, Mapping) or not isinstance(offline, Mapping):
        raise ControllerError("derived offline-effect receipts are missing")
    expected_replay_arms = {
        arm.arm_id for arm in expand_arms(plan) if arm.analyzer_mode == "frozen_replay"
    }
    if set(offline) != expected_replay_arms:
        raise ControllerError("derived offline-effect arm coverage differs")
    if derived.get("offline_unique_overlay_count") != len(unique):
        raise ControllerError("derived unique overlay count differs")
    for overlay_sha, embedded_with_path in unique.items():
        if not isinstance(embedded_with_path, Mapping):
            raise ControllerError("derived offline receipt is malformed")
        embedded = dict(embedded_with_path)
        receipt_path = Path(str(embedded.pop("path", "")))
        recorded_file_sha = embedded.pop("file_sha256", None)
        if (
            str(overlay_sha) != embedded.get("overlay_sha256")
            or recorded_file_sha != file_sha256(receipt_path)
            or load_json(receipt_path) != embedded
        ):
            raise ControllerError("derived offline receipt raw binding differs")
        verify_bare_document_self_hash(
            embedded,
            field="receipt_sha256",
            label=f"offline overlay {overlay_sha}",
        )
    for arm_id, descriptor_row in offline.items():
        if not isinstance(descriptor_row, Mapping):
            raise ControllerError(f"derived offline descriptor {arm_id} is malformed")
        receipt = unique.get(descriptor_row.get("overlay_sha256"))
        if (
            not isinstance(receipt, Mapping)
            or descriptor_row.get("receipt_path") != receipt.get("path")
            or descriptor_row.get("receipt_sha256") != receipt.get("receipt_sha256")
            or descriptor_row.get("decision") not in {*RUN_DECISIONS, "deleted_no_live_run"}
        ):
            raise ControllerError(f"derived offline descriptor {arm_id} differs")
    return derived, artifact


def reconcile_status_from_derived(
    status: dict[str, Any],
    derived: Mapping[str, Any],
) -> None:
    """Idempotently close the derived-plan/status crash window."""

    status["derived_plan"] = {
        "path": str(
            Path(str(derived["frozen_analyzer_artifact"]["path"])).parent / "derived-plan.json"
        ),
        "sha256": derived["derived_plan_sha256"],
    }
    status["no_op_experiments"]["P0.5-07"] = {
        "state": "no_op_deleted",
        "receipt": copy.deepcopy(derived["p0_5_07"]),
    }
    for arm_id, descriptor in derived["offline_effect"].items():
        if descriptor.get("decision") != "deleted_no_live_run":
            continue
        arm_state = status["arms"][arm_id]
        arm_state.update(
            {
                "state": "no_op_deleted",
                "completed_at": arm_state.get("completed_at") or utc_now(),
                "offline_effect_receipt": descriptor["receipt_path"],
            }
        )
        experiment_id = arm_state["experiment_id"]
        status.setdefault("offline_no_op_arms", {})[arm_id] = {
            "experiment_id": experiment_id,
            "receipt": descriptor["receipt_path"],
            "receipt_sha256": descriptor["receipt_sha256"],
        }
        experiment = status["no_op_experiments"].setdefault(
            experiment_id,
            {
                "state": "contains_offline_no_op_arm",
                "receipt": None,
                "arm_ids": [],
            },
        )
        experiment["state"] = "contains_offline_no_op_arm"
        arm_ids = experiment.setdefault("arm_ids", [])
        if arm_id not in arm_ids:
            arm_ids.append(arm_id)
        arm_ids.sort()


def no_op_status_is_terminal(status: Mapping[str, Any]) -> bool:
    rows = status.get("no_op_experiments")
    if not isinstance(rows, Mapping) or not rows:
        return False
    return all(
        isinstance(row, Mapping)
        and row.get("state") in {"no_op_deleted", "contains_offline_no_op_arm"}
        for row in rows.values()
    )


def campaign_terminal_phase(
    status: Mapping[str, Any],
    *,
    reporting_complete: bool | None = None,
) -> str:
    arms = status.get("arms")
    terminal_states = (
        {row.get("state") for row in arms.values() if isinstance(row, Mapping)}
        if isinstance(arms, Mapping)
        else {"invalid"}
    )
    complete = (
        bool(terminal_states)
        and terminal_states <= {"succeeded", "no_op_deleted"}
        and no_op_status_is_terminal(status)
        and reporting_complete is not False
    )
    return "succeeded" if complete else "completed_with_failures"


def publish_terminal_status_input(
    plan: Mapping[str, Any],
    status: Mapping[str, Any],
) -> dict[str, Any]:
    """Publish the immutable status evidence consumed by the reporter."""

    frozen = copy.deepcopy(dict(status))
    frozen.pop("reporting", None)
    frozen.pop("terminal_status_input", None)
    frozen["terminal_freeze"] = {
        "schema": "opensquilla.draco-p0-p05-terminal-status-input/v1",
        "campaign_plan_sha256": frozen.get("campaign_plan_sha256"),
        "run_id": frozen.get("run_id"),
        "phase": frozen.get("phase"),
    }
    frozen["terminal_status_input_sha256"] = canonical_sha256(frozen)
    destination = Path(str(plan["paths"]["run_root"])) / "terminal-status-input.json"
    secure_atomic_write_json(destination, frozen)
    return {
        "schema": "opensquilla.draco-p0-p05-terminal-status-input/v1",
        "path": str(destination),
        "semantic_sha256": frozen["terminal_status_input_sha256"],
        "file_sha256": file_sha256(destination),
        "campaign_plan_sha256": frozen["campaign_plan_sha256"],
    }


def validate_terminal_report_closure(
    plan: Mapping[str, Any],
    *,
    terminal_status_path: Path,
) -> dict[str, Any]:
    """Authenticate every artifact consumed by a successful terminal report."""

    status_bound = load_bound_json(
        terminal_status_path, label="terminal status input"
    )
    status = status_bound.value
    if not isinstance(status, Mapping):
        raise ControllerError("terminal status input is not an object")
    report_root = Path(str(plan["paths"]["report_root"]))
    markdown_path = report_root / "EXPERIMENT_RESULTS.md"
    json_path = report_root / "EXPERIMENT_RESULTS.json"
    markdown_payload = stable_regular_file_bytes(markdown_path)
    report_bound = load_bound_json(json_path, label="terminal comprehensive report")
    report = report_bound.value
    if not isinstance(report, Mapping):
        raise ControllerError("terminal comprehensive report is not an object")
    _verify_prefixed_document_hash(
        report, field="report_sha256", label="terminal comprehensive report"
    )
    completion = report.get("completion")
    if (
        report.get("campaign_plan_sha256") != canonical_sha256(plan)
        or report.get("terminal_status_input_file_sha256") != status_bound.file_sha256
        or report.get("terminal_status_input_sha256")
        != status.get("terminal_status_input_sha256")
        or not isinstance(completion, Mapping)
        or completion.get("status") != "complete"
    ):
        raise ControllerError("terminal comprehensive report is incomplete or unbound")
    expected_markdown = {
        "path": str(markdown_path),
        "sha256": hashlib.sha256(markdown_payload).hexdigest(),
        "size_bytes": len(markdown_payload),
    }
    if report.get("root_artifacts", {}).get("markdown") != expected_markdown:
        raise ControllerError("terminal root Markdown descriptor differs")
    report_arms = report.get("arms")
    status_arms = status.get("arms")
    if not isinstance(report_arms, Mapping) or not isinstance(status_arms, Mapping):
        raise ControllerError("terminal report/status arm maps are missing")
    succeeded_completion: dict[str, Any] = {}
    for arm_id, status_arm in status_arms.items():
        if not isinstance(status_arm, Mapping) or status_arm.get("state") != "succeeded":
            continue
        report_arm = report_arms.get(arm_id)
        if not isinstance(report_arm, Mapping):
            raise ControllerError(f"terminal report omits succeeded arm {arm_id}")
        root = absolute_path_without_symlinks(
            status_arm.get("output_dir"), label=f"{arm_id} output directory"
        )
        if not root.is_dir():
            raise ControllerError(f"{arm_id} output root is not a directory")
        actual = _bridge_artifact_hashes(root, label=str(arm_id))
        archived_sources = _bridge_archived_source_hashes(
            root, label=str(arm_id)
        )
        _assert_formal_artifact_evidence_closure(
            arm_id=str(arm_id),
            actual=actual,
            status_arm=status_arm,
            report_arm=report_arm,
        )
        succeeded_completion[str(arm_id)] = {
            "completion_evidence": report_arm.get("completion_evidence"),
            "archived_sources": archived_sources,
        }
    experiments = report.get("experiments")
    declared_groups = report.get("group_report_artifacts")
    if (
        not isinstance(experiments, Mapping)
        or not isinstance(declared_groups, Mapping)
        or set(declared_groups) != set(experiments)
    ):
        raise ControllerError("terminal group report artifact coverage differs")
    for experiment_id, experiment in experiments.items():
        if not isinstance(experiment, Mapping):
            raise ControllerError("terminal experiment report is malformed")
        directory_name = str(experiment.get("directory_name") or "")
        _assert_group_report_artifact_closure(
            experiment_id=str(experiment_id),
            group_root=report_root / directory_name,
            report=report,
        )
        group_json = load_bound_json(
            report_root / directory_name / "EXPERIMENT_RESULTS.json",
            label=f"{experiment_id} group report",
        ).value
        if not isinstance(group_json, Mapping):
            raise ControllerError(f"{experiment_id} group report is not an object")
        _verify_prefixed_document_hash(
            group_json,
            field="group_report_sha256",
            label=f"{experiment_id} group report",
        )
    closure = {
        "report_sha256": report.get("report_sha256"),
        "root_markdown": expected_markdown,
        "group_report_artifacts": report.get("group_report_artifacts"),
        "succeeded_arm_completion": succeeded_completion,
    }
    return {
        "EXPERIMENT_RESULTS.md": expected_markdown,
        "EXPERIMENT_RESULTS.json": {
            "path": str(json_path),
            "sha256": report_bound.file_sha256,
            "size_bytes": len(report_bound.payload),
        },
        "closure_sha256": canonical_sha256(closure),
    }


def run_terminal_report(
    plan: Mapping[str, Any],
    *,
    plan_path: Path,
    terminal_status_path: Path,
) -> tuple[dict[str, Any], bool]:
    """Run the frozen reporter after arm execution reaches a terminal phase."""

    run_root = Path(str(plan["paths"]["run_root"]))
    reporter = absolute_path_without_symlinks(
        plan["paths"]["reporter"], label="frozen reporter"
    )
    reporter_payload = stable_regular_file_bytes(reporter)
    reporter_raw_sha256 = hashlib.sha256(reporter_payload).hexdigest()
    sealed_reporter_fd = sealed_memfd(
        reporter_payload, label="draco-terminal-reporter", executable=False
    )
    command = [
        str(plan["paths"]["python"]),
        f"/proc/self/fd/{sealed_reporter_fd}",
        "--plan",
        str(plan_path.resolve()),
        "--status",
        str(terminal_status_path.resolve()),
        "--strict",
    ]
    error: dict[str, Any] | None = None
    returncode: int | None = None
    stdout = ""
    stderr = ""
    try:
        if reporter_raw_sha256 != plan["freeze"]["sources"]["reporter_raw_sha256"]:
            raise ControllerError("frozen reporter raw hash differs")
        completed = subprocess.run(
            command,
            check=False,
            text=True,
            capture_output=True,
            pass_fds=(sealed_reporter_fd,),
        )
        returncode = int(completed.returncode)
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
    except Exception as exc:  # noqa: BLE001 - preserve a terminal report receipt
        message = str(exc)
        error = {
            "error_class": type(exc).__name__,
            "message_sha256": hashlib.sha256(message.encode("utf-8", errors="replace")).hexdigest(),
        }
    finally:
        os.close(sealed_reporter_fd)
    artifacts: dict[str, Any] = {}
    closure_sha256: str | None = None
    if error is None and returncode == 0:
        try:
            first_closure = validate_terminal_report_closure(
                plan, terminal_status_path=terminal_status_path
            )
            second_closure = validate_terminal_report_closure(
                plan, terminal_status_path=terminal_status_path
            )
            if first_closure != second_closure:
                raise ControllerError("terminal report closure changed before receipt")
            closure_sha256 = str(first_closure.pop("closure_sha256"))
            artifacts = first_closure
        except Exception as exc:  # noqa: BLE001 - fail the receipt closed
            message = str(exc)
            error = {
                "error_class": type(exc).__name__,
                "message_sha256": hashlib.sha256(
                    message.encode("utf-8", errors="replace")
                ).hexdigest(),
            }
    success = error is None and returncode == 0 and len(artifacts) == 2
    partial = error is None and returncode == 2 and len(artifacts) == 2
    receipt: dict[str, Any] = {
        "schema": "opensquilla.draco-p0-p05-terminal-report/v1",
        "created_at": utc_now(),
        "reporter_path": str(reporter),
        "reporter_raw_sha256": reporter_raw_sha256,
        "report_closure_sha256": closure_sha256,
        "command_sha256": canonical_sha256(command),
        "strict": True,
        "terminal_status_input_path": str(terminal_status_path.resolve()),
        "terminal_status_input_file_sha256": file_sha256(terminal_status_path),
        "returncode": returncode,
        "status": "complete" if success else "partial" if partial else "failed",
        "stdout_sha256": hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr.encode("utf-8")).hexdigest(),
        "error": error,
        "artifacts": artifacts,
    }
    receipt["receipt_sha256"] = canonical_sha256(receipt)
    destination = run_root / "terminal-report-receipt.json"
    secure_atomic_write_json(destination, receipt)
    return {**receipt, "path": str(destination), "file_sha256": file_sha256(destination)}, success


def run_campaign(plan_path: Path) -> int:
    plan = load_json(plan_path)
    arms = validate_plan(plan, allow_placeholders=False)
    if isinstance(plan.get("confirmatory_bridge"), Mapping):
        raise ControllerError(
            "confirmatory-only bridge refuses ordinary campaign run; use run-confirmatory"
        )
    plan_sha256 = canonical_sha256(plan)
    snapshot, snapshot_identity = validate_snapshot(plan)
    validate_runtime_freeze(
        plan,
        snapshot=snapshot,
        expected_snapshot_identity=snapshot_identity,
    )
    validate_static_overlays(plan, arms, snapshot)
    validate_frozen_replay_runtime_support(plan, _runtime_helpers(snapshot))
    run_root = Path(str(plan["paths"]["run_root"]))
    run_root.mkdir(parents=True, exist_ok=True)
    lock_path = run_root / "controller.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(lock_fd, "r+") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ControllerError("another campaign controller is active") from exc
        status = load_or_initialize_status(
            plan,
            arms,
            plan_sha256=plan_sha256,
            snapshot_identity=snapshot_identity,
        )
        status_file = status_path(plan)
        status["phase"] = "running"
        status["started_at"] = status.get("started_at") or utc_now()
        status["completed_at"] = None
        status["preexisting_source_import"] = materialize_preexisting_source(plan)
        update_status(status_file, status)

        expected_task_ids = set(plan["benchmark"]["task_ids"])
        source_arm = next(arm for arm in arms if arm.arm_id == ANALYZER_SOURCE_ARM_ID)
        derived: dict[str, Any] | None = None
        artifact: dict[str, Any] | None = None
        derived_path = run_root / "derived-plan.json"
        if derived_path.exists():
            derived, artifact = load_derived(plan, plan_sha256)
            reconcile_status_from_derived(status, derived)
            update_status(status_file, status)

        any_failure = False
        authenticated_anchor_ids: set[str] = set()
        anchor_by_arm_id = plan["execution"]["schedule"]["anchor_by_arm_id"]
        for arm in arms:
            arm_state = status["arms"][arm.arm_id]
            directory = output_dir(plan, arm)
            override: dict[str, Any] | None = None
            expected_identity: dict[str, Any] | None = None
            try:
                override = resolve_arm_override(
                    plan,
                    arm,
                    artifact=artifact,
                    p99_receipt=(derived["p0_5_06"] if derived is not None else None),
                )
                expected_identity = arm_completion_identity(
                    plan,
                    arm,
                    snapshot=snapshot,
                    snapshot_identity=snapshot_identity,
                    override=override,
                )
            except Exception:  # noqa: BLE001 - handled as a prerequisite below
                override = None
                expected_identity = None
            complete, evidence = inspect_complete_arm(
                directory,
                expected_task_ids=expected_task_ids,
                expected_task_concurrency=6,
                expected_identity=expected_identity,
            )
            if complete:
                if anchor_by_arm_id[arm.arm_id] == arm.arm_id:
                    authenticated_anchor_ids.add(arm.arm_id)
                arm_state.update(
                    {
                        "state": "succeeded",
                        "completion_evidence": evidence,
                        "completed_at": arm_state.get("completed_at") or utc_now(),
                    }
                )
                update_status(status_file, status)
                if arm.arm_id == source_arm.arm_id and derived is None:
                    try:
                        derived = prepare_derived(
                            plan,
                            arms,
                            snapshot=snapshot,
                            snapshot_identity=snapshot_identity,
                            plan_sha256=plan_sha256,
                            source_arm=source_arm,
                            source_dir=directory,
                        )
                        derived, artifact = load_derived(plan, plan_sha256)
                        reconcile_status_from_derived(status, derived)
                    except Exception as exc:  # noqa: BLE001 - later live arms still run
                        message = str(exc)
                        derived = None
                        artifact = None
                        status["derived_failure"] = {
                            "error_class": type(exc).__name__,
                            "message_sha256": hashlib.sha256(
                                message.encode("utf-8", errors="replace")
                            ).hexdigest(),
                        }
                        any_failure = True
                    update_status(status_file, status)
                continue

            if arm.analyzer_mode == "frozen_replay" and derived is not None:
                offline = derived["offline_effect"].get(arm.arm_id)
                if not isinstance(offline, Mapping):
                    raise ControllerError(f"derived plan lacks offline receipt for {arm.arm_id}")
                decision = offline["decision"]
                if decision == "deleted_no_live_run":
                    arm_state.update(
                        {
                            "state": "no_op_deleted",
                            "completed_at": utc_now(),
                            "offline_effect_receipt": offline["receipt_path"],
                        }
                    )
                    status.setdefault("offline_no_op_arms", {})[arm.arm_id] = {
                        "experiment_id": arm.experiment_id,
                        "receipt": offline["receipt_path"],
                        "receipt_sha256": offline["receipt_sha256"],
                    }
                    experiment_noop = status["no_op_experiments"].setdefault(
                        arm.experiment_id,
                        {
                            "state": "contains_offline_no_op_arm",
                            "receipt": None,
                            "arm_ids": [],
                        },
                    )
                    no_op_arm_ids = experiment_noop.setdefault("arm_ids", [])
                    if arm.arm_id not in no_op_arm_ids:
                        no_op_arm_ids.append(arm.arm_id)
                    update_status(status_file, status)
                    continue
                if decision not in RUN_DECISIONS:
                    raise ControllerError(
                        f"unsupported offline decision for {arm.arm_id}: {decision!r}"
                    )

            if directory.exists():
                arm_state.update(
                    {
                        "state": "failed",
                        "completed_at": utc_now(),
                        "failure": {
                            "reason": "preexisting_incomplete_output",
                            "completion_evidence": evidence,
                            "policy": "never overwrite or append to an unverified formal output",
                        },
                    }
                )
                any_failure = True
                update_status(status_file, status)
                continue

            try:
                if override is None:
                    override = resolve_arm_override(
                        plan,
                        arm,
                        artifact=artifact,
                        p99_receipt=(derived["p0_5_06"] if derived is not None else None),
                    )
                expected_identity = arm_completion_identity(
                    plan,
                    arm,
                    snapshot=snapshot,
                    snapshot_identity=snapshot_identity,
                    override=override,
                )
            except Exception as exc:  # noqa: BLE001 - schema/import drift blocks only this arm
                arm_state.update(
                    {
                        "state": "blocked_prerequisite",
                        "completed_at": utc_now(),
                        "failure": {"reason": str(exc)},
                    }
                )
                any_failure = True
                update_status(status_file, status)
                continue

            anchor_ready, anchor_failure = schedule_anchor_launch_gate(
                plan,
                arm,
                status=status,
                authenticated_anchor_ids=authenticated_anchor_ids,
            )
            if not anchor_ready:
                arm_state.update(
                    {
                        "state": "blocked_prerequisite",
                        "completed_at": utc_now(),
                        "failure": anchor_failure,
                    }
                )
                any_failure = True
                update_status(status_file, status)
                continue

            # The snapshot is immutable, but the benchmark/reference config
            # lives outside it. Re-freeze immediately before the launcher can
            # open a paid account window.
            validate_runtime_freeze(
                plan,
                snapshot=snapshot,
                expected_snapshot_identity=snapshot_identity,
            )
            status["active_arm"] = arm.arm_id
            status["active_schedule_ordinal"] = arm_state["schedule_ordinal"]
            arm_state["state"] = "running"
            arm_state["started_at"] = utc_now()
            attempt = {
                "started_at": arm_state["started_at"],
                "schedule_sha256": status["schedule_sha256"],
                "schedule_ordinal": arm_state["schedule_ordinal"],
                "anchor_arm_id": arm_state["anchor_arm_id"],
                "override_sha256": canonical_sha256(override),
                "output_dir": str(directory),
            }
            arm_state["attempts"].append(attempt)
            update_status(status_file, status)
            launch_error: dict[str, Any] | None = None
            try:
                rc: int | None = launch_arm(
                    plan,
                    arm,
                    snapshot=snapshot,
                    override=override,
                )
            except Exception as exc:  # noqa: BLE001 - one launcher failure is arm-local
                message = str(exc)
                rc = None
                launch_error = {
                    "error_class": type(exc).__name__,
                    "message_sha256": hashlib.sha256(
                        message.encode("utf-8", errors="replace")
                    ).hexdigest(),
                }
            attempt["completed_at"] = utc_now()
            attempt["rc"] = rc
            if launch_error is not None:
                attempt["launcher_error"] = launch_error
            status["active_arm"] = None
            status["active_schedule_ordinal"] = None
            complete, evidence = inspect_complete_arm(
                directory,
                expected_task_ids=expected_task_ids,
                expected_task_concurrency=6,
                expected_identity=expected_identity,
            )
            arm_state["completion_evidence"] = evidence
            arm_state["completed_at"] = utc_now()
            if complete:
                arm_state["state"] = "succeeded"
                if anchor_by_arm_id[arm.arm_id] == arm.arm_id:
                    authenticated_anchor_ids.add(arm.arm_id)
            else:
                arm_state["state"] = "failed"
                arm_state["failure"] = {
                    "reason": "launcher_or_completion_contract_failed",
                    "rc": rc,
                    "launcher_error": launch_error,
                }
                any_failure = True
            update_status(status_file, status)

            if complete and arm.arm_id == source_arm.arm_id and derived is None:
                try:
                    derived = prepare_derived(
                        plan,
                        arms,
                        snapshot=snapshot,
                        snapshot_identity=snapshot_identity,
                        plan_sha256=plan_sha256,
                        source_arm=source_arm,
                        source_dir=directory,
                    )
                    derived, artifact = load_derived(plan, plan_sha256)
                    reconcile_status_from_derived(status, derived)
                except Exception as exc:  # noqa: BLE001 - later live arms still run
                    message = str(exc)
                    derived = None
                    artifact = None
                    status["derived_failure"] = {
                        "error_class": type(exc).__name__,
                        "message_sha256": hashlib.sha256(
                            message.encode("utf-8", errors="replace")
                        ).hexdigest(),
                    }
                    any_failure = True
                update_status(status_file, status)

        if not no_op_status_is_terminal(status):
            any_failure = True
        status["phase"] = campaign_terminal_phase(status)
        status["completed_at"] = utc_now()
        status["active_arm"] = None
        status["active_schedule_ordinal"] = None
        update_status(status_file, status)
        terminal_status = publish_terminal_status_input(plan, status)
        status["terminal_status_input"] = terminal_status
        update_status(status_file, status)
        validate_runtime_freeze(
            plan,
            snapshot=snapshot,
            expected_snapshot_identity=snapshot_identity,
        )
        report_receipt, report_complete = run_terminal_report(
            plan,
            plan_path=plan_path,
            terminal_status_path=Path(str(terminal_status["path"])),
        )
        status["reporting"] = report_receipt
        status["phase"] = campaign_terminal_phase(
            status,
            reporting_complete=report_complete,
        )
        if not report_complete:
            any_failure = True
        update_status(status_file, status)
        return 1 if any_failure else 0


def validate_only(plan_path: Path) -> dict[str, Any]:
    """Read-only production-interface and freeze validation; no output/model calls."""

    plan = load_json(plan_path)
    arms = validate_plan(plan, allow_placeholders=False)
    snapshot, snapshot_identity = validate_snapshot(plan)
    freeze_identity = validate_runtime_freeze(
        plan,
        snapshot=snapshot,
        expected_snapshot_identity=snapshot_identity,
    )
    validate_static_overlays(plan, arms, snapshot)
    helpers = _runtime_helpers(snapshot)
    validate_frozen_replay_runtime_support(plan, helpers)
    imported_source = authenticate_preexisting_source(plan)
    return {
        "status": "valid",
        "candidate_live_arm_count": len(arms),
        "live_experiment_group_count": len(
            {arm.experiment_id for arm in arms if arm.experiment_id != "common-E0"}
        ),
        "frozen_replay_arm_count": sum(arm.analyzer_mode == "frozen_replay" for arm in arms),
        "live_analyzer_arm_count": sum(arm.analyzer_mode == "live" for arm in arms),
        "snapshot_commit": snapshot_identity["commit"],
        "snapshot_tree": snapshot_identity["tree"],
        "freeze_identity_sha256": canonical_sha256(freeze_identity),
        "production_helper_sources": {
            "ensemble": helpers["ensemble_source_sha256"],
            "openai": helpers["openai_source_sha256"],
            "runner": helpers["runner_source_sha256"],
        },
        "draco_request_budget_rebinding_disabled": helpers[
            "draco_request_budget_rebinding_disabled"
        ],
        "preexisting_source_import": (
            {
                "status": "authenticated",
                "receipt_sha256": imported_source["receipt_sha256"],
            }
            if imported_source is not None
            else {"status": "not_declared"}
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate-plan")
    validate_parser.add_argument("plan", type=Path)
    validate_parser.add_argument("--allow-placeholders", action="store_true")
    expand_parser = subparsers.add_parser("expand-plan")
    expand_parser.add_argument("plan", type=Path)
    expand_parser.add_argument("--allow-placeholders", action="store_true")
    validate_only_parser = subparsers.add_parser("validate-only")
    validate_only_parser.add_argument("plan", type=Path)
    prepare_bridge_parser = subparsers.add_parser("prepare-confirmatory-bridge")
    prepare_bridge_parser.add_argument("--screening-plan", required=True, type=Path)
    prepare_bridge_parser.add_argument("--terminal-status", required=True, type=Path)
    prepare_bridge_parser.add_argument(
        "--terminal-report-receipt", required=True, type=Path
    )
    prepare_bridge_parser.add_argument("--snapshot", required=True, type=Path)
    prepare_bridge_parser.add_argument("--run-root", required=True, type=Path)
    prepare_bridge_parser.add_argument("--report-root", required=True, type=Path)
    prepare_bridge_parser.add_argument("--run-id")
    prepare_bridge_parser.add_argument(
        "--survivor-arm-id", action="append", dest="survivor_arm_ids"
    )
    prepare_bridge_parser.add_argument(
        "--schedule-seed-prefix", default="draco-confirmatory-bridge-v1"
    )
    validate_bridge_parser = subparsers.add_parser("validate-confirmatory-bridge")
    validate_bridge_parser.add_argument("plan", type=Path)
    prepare_confirmatory_parser = subparsers.add_parser("prepare-confirmatory")
    prepare_confirmatory_parser.add_argument("plan", type=Path)
    prepare_confirmatory_parser.add_argument("--candidate-arm-id", required=True)
    prepare_confirmatory_parser.add_argument("--output", required=True, type=Path)
    prepare_confirmatory_parser.add_argument("--seed", default="draco-confirmatory-v1")
    validate_confirmatory_parser = subparsers.add_parser("validate-confirmatory")
    validate_confirmatory_parser.add_argument("plan", type=Path)
    validate_confirmatory_parser.add_argument("schedule", type=Path)
    run_confirmatory_parser = subparsers.add_parser("run-confirmatory")
    run_confirmatory_parser.add_argument("plan", type=Path)
    run_confirmatory_parser.add_argument("schedule", type=Path)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("plan", type=Path)
    args = parser.parse_args()

    if args.command in {"validate-plan", "expand-plan"}:
        plan = load_json(args.plan)
        arms = validate_plan(plan, allow_placeholders=args.allow_placeholders)
        if args.command == "expand-plan":
            print(json.dumps([asdict(arm) for arm in arms], ensure_ascii=False, indent=2))
        else:
            print(
                json.dumps(
                    {
                        "status": "valid",
                        "candidate_live_arm_count": len(arms),
                        "live_experiment_group_count": len(
                            {arm.experiment_id for arm in arms if arm.experiment_id != "common-E0"}
                        ),
                        "common_e0_count": sum(arm.experiment_id == "common-E0" for arm in arms),
                        "campaign_plan_sha256": canonical_sha256(plan),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        return 0
    if args.command == "validate-only":
        print(json.dumps(validate_only(args.plan), ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "prepare-confirmatory-bridge":
        prepared = prepare_confirmatory_bridge(
            source_plan_path=args.screening_plan,
            terminal_status_path=args.terminal_status,
            terminal_report_receipt_path=args.terminal_report_receipt,
            snapshot=args.snapshot,
            run_root=args.run_root,
            report_root=args.report_root,
            requested_survivor_arm_ids=args.survivor_arm_ids,
            run_id=args.run_id,
            schedule_seed_prefix=args.schedule_seed_prefix,
        )
        print(json.dumps(prepared, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "validate-confirmatory-bridge":
        print(
            json.dumps(
                validate_confirmatory_bridge_only(args.plan),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "prepare-confirmatory":
        schedule = prepare_confirmatory_schedule(
            args.plan,
            candidate_arm_id=args.candidate_arm_id,
            output_path=args.output,
            seed=args.seed,
        )
        print(json.dumps(schedule, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "validate-confirmatory":
        plan = load_json(args.plan)
        validate_plan(plan, allow_placeholders=False)
        schedule = load_json(args.schedule)
        validate_confirmatory_schedule(plan, schedule)
        print(
            json.dumps(
                {
                    "status": "valid",
                    "cohort_id": schedule["cohort_id"],
                    "schedule_sha256": schedule["schedule_sha256"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "run-confirmatory":
        return launch_confirmatory_schedule(args.plan, args.schedule)
    return run_campaign(args.plan)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ControllerError as exc:
        print(f"controller error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
