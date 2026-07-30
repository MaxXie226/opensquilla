"""Runtime wrapper for automatic sandbox setup."""

from __future__ import annotations

import asyncio
from typing import Any

from opensquilla.sandbox.capability_service import (
    CapabilityReport,
    capability_report_from_setup,
)
from opensquilla.sandbox.setup_state import (
    SandboxSetupState,
    SetupResult,
    current_sandbox_setup_status,
    ensure_sandbox_setup,
)

_LOCK = asyncio.Lock()
_SETTING_UP = False
_LAST_RESULT: SetupResult | None = None


async def current_sandbox_setup_runtime_status(config: Any) -> SetupResult:
    if _SETTING_UP:
        return SetupResult(
            state=SandboxSetupState.SETTING_UP,
            platform="auto",
            message="Sandbox setup is running.",
            requires_admin=False,
        )
    if _LAST_RESULT is not None and _LAST_RESULT.state is SandboxSetupState.FAILED:
        return _LAST_RESULT
    return await current_sandbox_setup_status(config)


async def current_sandbox_capability_report(config: Any) -> CapabilityReport:
    setup = await current_sandbox_setup_runtime_status(config)
    backend = str(getattr(getattr(config, "sandbox", None), "backend", "auto"))
    if backend == "auto":
        try:
            from opensquilla.sandbox.integration import get_runtime

            runtime = get_runtime()
            backend = str(getattr(getattr(runtime, "backend", None), "name", "auto"))
        except Exception:  # pragma: no cover - defensive import boundary
            backend = "auto"
    return capability_report_from_setup(setup, backend=backend)


async def ensure_sandbox_setup_auto(config: Any) -> SetupResult:
    global _LAST_RESULT, _SETTING_UP

    async with _LOCK:
        _SETTING_UP = True
        setup_result: SetupResult | None = None
        try:
            setup_result = await ensure_sandbox_setup(config)
            if (
                setup_result.state is SandboxSetupState.READY
                and setup_result.platform == "win32"
            ):
                from opensquilla.sandbox.integration import (
                    refresh_runtime_backend_after_setup,
                )

                refresh_runtime_backend_after_setup()
            _LAST_RESULT = setup_result
            return setup_result
        except Exception as exc:  # noqa: BLE001
            result = SetupResult(
                state=SandboxSetupState.FAILED,
                platform=setup_result.platform if setup_result is not None else "auto",
                message="Sandbox setup failed.",
                requires_admin=(
                    setup_result.requires_admin if setup_result is not None else False
                ),
                detail=str(exc),
            )
            _LAST_RESULT = result
            return result
        finally:
            _SETTING_UP = False


def reset_sandbox_setup_runtime_state() -> None:
    global _LAST_RESULT, _LOCK, _SETTING_UP

    _LOCK = asyncio.Lock()
    _SETTING_UP = False
    _LAST_RESULT = None


__all__ = [
    "current_sandbox_capability_report",
    "current_sandbox_setup_runtime_status",
    "ensure_sandbox_setup_auto",
    "reset_sandbox_setup_runtime_state",
]
