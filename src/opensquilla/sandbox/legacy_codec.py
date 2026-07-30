"""Compatibility-only translation for pre-Safe/Full sandbox values.

New domain code must use :class:`opensquilla.sandbox.run_mode.RunMode`
directly.  This module is the only place that understands the retired
Standard/Trusted/Managed spellings and the old boolean truth table.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from opensquilla.sandbox.run_mode import RunMode


class LegacyModeContext(StrEnum):
    EXPLICIT = "explicit"
    CONFIG = "config"
    WIRE_V1 = "wire_v1"
    CLI = "cli"
    STORED_EVENT = "stored_event"


class LegacyModeDecodeError(ValueError):
    def __init__(self, value: object, context: LegacyModeContext) -> None:
        self.value = value
        self.context = context
        super().__init__(f"unsupported legacy run mode {value!r} in {context.value}")


_SAFE_ALIASES: Final[frozenset[str]] = frozenset(
    {
        "safe",
        "standard",
        "standard-sandbox",
        "standard_sandbox",
        "trusted",
        "trusted-sandbox",
        "trusted_sandbox",
        "trust",
        "managed",
        "on",
        "off",
        "restricted",
    }
)
_FULL_ALIASES: Final[frozenset[str]] = frozenset(
    {
        "full",
        "full-host-access",
        "full_host_access",
        "bypass",
    }
)
_UNSET: Final[object] = object()


def decode_legacy_run_mode(
    value: object,
    *,
    context: LegacyModeContext,
) -> RunMode:
    if isinstance(value, RunMode):
        return RunMode(value.value)
    key = str(value).strip().lower()
    if key in _SAFE_ALIASES:
        return RunMode.SAFE
    if key in _FULL_ALIASES:
        return RunMode.FULL
    raise LegacyModeDecodeError(value, context)


def decode_legacy_config_mode(
    *,
    run_mode: object = _UNSET,
    permissions_default_mode: object = _UNSET,
    sandbox_enabled: object = _UNSET,
    grading_enabled: object = _UNSET,
) -> RunMode:
    """Decode one old config record using the documented field priority."""

    if run_mode is not _UNSET and run_mode is not None and str(run_mode).strip():
        return decode_legacy_run_mode(run_mode, context=LegacyModeContext.CONFIG)

    if permissions_default_mode is not _UNSET:
        permission_key = str(permissions_default_mode or "").strip().lower()
        if permission_key in _FULL_ALIASES:
            return RunMode.FULL
        if permission_key in _SAFE_ALIASES or permission_key == "":
            if permission_key:
                return RunMode.SAFE
        else:
            raise LegacyModeDecodeError(
                permissions_default_mode,
                LegacyModeContext.CONFIG,
            )

    if sandbox_enabled is not _UNSET:
        if not isinstance(sandbox_enabled, bool):
            raise LegacyModeDecodeError(sandbox_enabled, LegacyModeContext.CONFIG)
        return RunMode.SAFE if sandbox_enabled else RunMode.FULL

    if grading_enabled is not _UNSET:
        if not isinstance(grading_enabled, bool):
            raise LegacyModeDecodeError(grading_enabled, LegacyModeContext.CONFIG)
        return RunMode.SAFE

    return RunMode.FULL


def encode_run_mode_for_protocol(mode: RunMode, *, protocol: int) -> str:
    canonical = RunMode(mode)
    if protocol < 1:
        raise ValueError("protocol must be a positive integer")
    if protocol < 4 and canonical is RunMode.SAFE:
        return "trusted"
    return canonical.value


__all__ = [
    "LegacyModeContext",
    "LegacyModeDecodeError",
    "decode_legacy_config_mode",
    "decode_legacy_run_mode",
    "encode_run_mode_for_protocol",
]
