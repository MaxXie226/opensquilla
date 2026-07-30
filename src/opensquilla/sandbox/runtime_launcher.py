"""Frozen-aware launch and dispatch for trusted OpenSquilla child roles."""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from enum import StrEnum


class ChildRole(StrEnum):
    """Fixed internal roles that a packaged Gateway may execute."""

    FILESYSTEM_WORKER = "filesystem-worker"
    LINUX_HELPER = "linux-helper"
    WINDOWS_DEFAULT_RUNNER = "windows-default-runner"
    DIRECTORY_PICKER = "directory-picker"


class InternalChildDispatchError(ValueError):
    """Raised when an internal-child request does not name a fixed role."""


_ROLE_MODULES: dict[ChildRole, str] = {
    ChildRole.FILESYSTEM_WORKER: "opensquilla.sandbox.filesystem_worker",
    ChildRole.LINUX_HELPER: "opensquilla.sandbox.backend.linux_helper",
    ChildRole.WINDOWS_DEFAULT_RUNNER: "opensquilla.sandbox.backend.windows_default_runner",
    ChildRole.DIRECTORY_PICKER: "opensquilla.gateway.windows_directory_picker",
}


def _coerce_role(role: ChildRole | str) -> ChildRole:
    if isinstance(role, ChildRole):
        return role
    try:
        return ChildRole(str(role))
    except ValueError as exc:
        raise ValueError(f"unknown internal child role: {role!r}") from exc


def internal_child_argv(
    role: ChildRole | str,
    *,
    args: Sequence[str] = (),
) -> tuple[str, ...]:
    """Build argv for an internal child in source and frozen runtimes."""

    child_role = _coerce_role(role)
    child_args = tuple(str(arg) for arg in args)
    executable = str(sys.executable)
    if bool(getattr(sys, "frozen", False)):
        return (
            executable,
            "--internal-child",
            child_role.value,
            *child_args,
        )
    return (
        executable,
        "-m",
        _ROLE_MODULES[child_role],
        *child_args,
    )


def _run_filesystem_worker(args: Sequence[str]) -> int:
    from opensquilla.sandbox.filesystem_worker import main

    main(args)
    return 0


def _run_linux_helper(args: Sequence[str]) -> int:
    from opensquilla.sandbox.backend.linux_helper import main

    return int(main(list(args)))


def _run_windows_default_runner(args: Sequence[str]) -> int:
    from opensquilla.sandbox.backend.windows_default_runner import main

    main(args)
    return 0


def _run_directory_picker(args: Sequence[str]) -> int:
    from opensquilla.gateway.windows_directory_picker import main

    return int(main(args))


_ROLE_HANDLERS: dict[ChildRole, Callable[[Sequence[str]], int]] = {
    ChildRole.FILESYSTEM_WORKER: _run_filesystem_worker,
    ChildRole.LINUX_HELPER: _run_linux_helper,
    ChildRole.WINDOWS_DEFAULT_RUNNER: _run_windows_default_runner,
    ChildRole.DIRECTORY_PICKER: _run_directory_picker,
}


def dispatch_internal_child(argv: Sequence[str]) -> int:
    """Dispatch a packaged internal child without entering the public CLI."""

    args = tuple(str(arg) for arg in argv)
    if not args:
        raise InternalChildDispatchError("missing internal child role")
    try:
        role = ChildRole(args[0])
    except ValueError as exc:
        raise InternalChildDispatchError(f"unknown internal child role: {args[0]!r}") from exc
    return _ROLE_HANDLERS[role](args[1:])


__all__ = [
    "ChildRole",
    "InternalChildDispatchError",
    "dispatch_internal_child",
    "internal_child_argv",
]
