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
        shutil.rmtree(self.root, ignore_errors=True)
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
        runtime_roots: tuple[str | Path, ...] = (),
        temp_parent: str | Path | None = None,
    ) -> GuestProfile:
        parent = Path(temp_parent).expanduser().absolute() if temp_parent else None
        if parent is not None:
            parent.mkdir(parents=True, exist_ok=True)
        root = Path(
            tempfile.mkdtemp(
                prefix=f"opensquilla-guest-{_safe_task_component(task_id)}-",
                dir=str(parent) if parent is not None else None,
            )
        ).absolute()
        workspace = root / "workspace"
        home = root / "home"
        temp = root / "tmp"
        for directory in (workspace, home, temp):
            directory.mkdir()
        resolved_runtimes = tuple(
            Path(runtime_root).expanduser().absolute()
            for runtime_root in runtime_roots
            if Path(runtime_root).expanduser().exists()
        )
        mounts = (
            GuestMount(workspace, "workspace", "rw"),
            *(
                GuestMount(runtime_root, "bundled-runtime", "ro")
                for runtime_root in resolved_runtimes
            ),
        )
        return GuestProfile(
            root=root,
            workspace=workspace,
            home=home,
            temp=temp,
            mounts=mounts,
            environment=_guest_environment(
                home=home,
                temp=temp,
                runtime_roots=resolved_runtimes,
            ),
        )


__all__ = [
    "GuestMount",
    "GuestProfile",
    "GuestProfileFactory",
]
