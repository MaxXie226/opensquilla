"""Ephemeral workspace and environment boundary for unauthenticated LAN tasks."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from opensquilla.sandbox.run_context import MountGrant, RunContext
from opensquilla.sandbox.run_mode import RunMode


@dataclass(frozen=True)
class GuestMount:
    path: Path
    kind: Literal["workspace", "bundled-runtime"]
    access: Literal["rw", "ro"]


@dataclass
class GuestProfile:
    root: Path
    workspace: Path
    home: Path
    temp: Path
    mounts: tuple[GuestMount, ...]
    environment: dict[str, str]
    cleaned: bool = False

    @property
    def host_home_mounted(self) -> bool:
        return False

    def run_context(self) -> RunContext:
        return RunContext(
            run_mode=RunMode.SAFE,
            workspace=str(self.workspace),
            mounts=tuple(
                MountGrant(path=str(mount.path), access=mount.access, scope="once")
                for mount in self.mounts
                if mount.kind == "bundled-runtime"
            ),
            source="guest_safe",
        )

    def cleanup(self) -> None:
        if self.cleaned:
            return
        cleanup_guest_profile_root(self.root)
        self.cleaned = True


def _safe_task_component(task_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(task_id)).strip(".-")
    return cleaned[:48] or "task"


def _guest_environment(
    *,
    home: Path,
    temp: Path,
    runtime_roots: tuple[Path, ...],
) -> dict[str, str]:
    path_entries = [str(root) for root in runtime_roots]
    environment = {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "TMP": str(temp),
        "TEMP": str(temp),
        "PATH": os.pathsep.join(path_entries),
        "OPENSQUILLA_GUEST_SAFE": "1",
    }
    if os.name == "nt":
        for key in ("SystemRoot", "ComSpec", "PATHEXT", "WINDIR"):
            value = os.environ.get(key)
            if value:
                environment[key] = value
    return environment


class GuestProfileFactory:
    @staticmethod
    def create(
        task_id: str,
        *,
        workspace: str | Path,
        runtime_roots: tuple[str | Path, ...] = (),
    ) -> GuestProfile:
        workspace_path = Path(workspace).expanduser().resolve(strict=False)
        workspace_path.mkdir(parents=True, exist_ok=True)
        parent = workspace_path / ".opensquilla-guest"
        parent.mkdir(parents=True, exist_ok=True)
        root = Path(
            tempfile.mkdtemp(
                prefix=f"opensquilla-guest-{_safe_task_component(task_id)}-",
                dir=str(parent),
            )
        ).resolve(strict=False)
        home = root / "home"
        temp = root / "tmp"
        for directory in (home, temp):
            directory.mkdir()
        resolved_runtimes = tuple(
            Path(runtime_root).expanduser().absolute()
            for runtime_root in runtime_roots
            if Path(runtime_root).expanduser().exists()
        )
        mounts = (
            GuestMount(workspace_path, "workspace", "rw"),
            *(
                GuestMount(runtime_root, "bundled-runtime", "ro")
                for runtime_root in resolved_runtimes
            ),
        )
        return GuestProfile(
            root=root,
            workspace=workspace_path,
            home=home,
            temp=temp,
            mounts=mounts,
            environment=_guest_environment(
                home=home,
                temp=temp,
                runtime_roots=resolved_runtimes,
            ),
        )


def cleanup_guest_profile_root(value: str | Path) -> bool:
    """Remove only a factory-shaped scratch root below ``.opensquilla-guest``."""

    root = Path(value).expanduser().absolute()
    canonical_root = root.resolve(strict=False)
    if (
        not root.name.startswith("opensquilla-guest-")
        or canonical_root.name != root.name
        or canonical_root.parent.name != ".opensquilla-guest"
        or canonical_root.parent == canonical_root
    ):
        return False
    shutil.rmtree(root, ignore_errors=True)
    return not root.exists()


__all__ = [
    "cleanup_guest_profile_root",
    "GuestMount",
    "GuestProfile",
    "GuestProfileFactory",
]
