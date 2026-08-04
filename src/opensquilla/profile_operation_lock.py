"""Layer-neutral access to the hardened profile operation lock.

The recovery package owns the platform-specific lock implementation because it
also coordinates profile moves and legacy gateway leases. Runtime subsystems
that only need writer exclusion import this narrow facade instead of depending
on the recovery package directly.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path

from opensquilla.recovery.errors import ProfileLockBusyError
from opensquilla.recovery.locking import ProfileOperationLock


class ProfileOperationBusyError(RuntimeError):
    """Stable layer-neutral signal that another writer owns the lock."""


@contextlib.contextmanager
def acquire_profile_operation_lock(
    home: str | Path,
    *,
    timeout: float = 0.0,
) -> Iterator[ProfileOperationLock]:
    """Acquire one profile-operation lock without exposing recovery internals."""

    lock = ProfileOperationLock(home, timeout=timeout)
    try:
        lock.acquire()
    except ProfileLockBusyError as exc:
        raise ProfileOperationBusyError("profile operation lock is busy") from exc
    try:
        yield lock
    finally:
        lock.release()


__all__ = [
    "ProfileOperationBusyError",
    "ProfileOperationLock",
    "acquire_profile_operation_lock",
]
