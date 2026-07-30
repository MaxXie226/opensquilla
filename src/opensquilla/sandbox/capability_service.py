"""Immutable Safe capability reports and fingerprinted probe caching."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from opensquilla.sandbox.setup_state import SandboxSetupState, SetupResult

REQUIRED_SAFE_CAPABILITIES = frozenset(
    {
        "process",
        "filesystem-worker",
        "denyWriteCarveout",
        "authorityDenyRead",
    }
)


@dataclass(frozen=True)
class CapabilityReport:
    available: bool
    backend: str
    platform: str
    code: str
    reason: str
    setup_supported: bool
    restart_required: bool
    probe_version: int
    capabilities: frozenset[str]

    @classmethod
    def available_for(
        cls,
        *,
        backend: str,
        platform: str,
        reason: str = "ready",
        capabilities: frozenset[str] = REQUIRED_SAFE_CAPABILITIES,
    ) -> CapabilityReport:
        return cls(
            available=REQUIRED_SAFE_CAPABILITIES.issubset(capabilities),
            backend=backend,
            platform=platform,
            code="ready",
            reason=reason,
            setup_supported=True,
            restart_required=False,
            probe_version=1,
            capabilities=capabilities,
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "available": self.available,
            "backend": self.backend,
            "platform": self.platform,
            "code": self.code,
            "reason": self.reason,
            "setupSupported": self.setup_supported,
            "restartRequired": self.restart_required,
            "probeVersion": self.probe_version,
            "capabilities": sorted(self.capabilities),
        }


def capability_report_from_setup(
    setup: SetupResult,
    *,
    backend: str,
) -> CapabilityReport:
    ready = setup.state is SandboxSetupState.READY
    code = {
        SandboxSetupState.READY: "ready",
        SandboxSetupState.NOT_SETUP: "not_setup",
        SandboxSetupState.SETTING_UP: "setting_up",
        SandboxSetupState.FAILED: "setup_failed",
        SandboxSetupState.UNAVAILABLE: "backend_unavailable",
    }[setup.state]
    capabilities = REQUIRED_SAFE_CAPABILITIES if ready else frozenset()
    return CapabilityReport(
        available=ready and REQUIRED_SAFE_CAPABILITIES.issubset(capabilities),
        backend=str(backend),
        platform=setup.platform,
        code=code,
        reason=setup.detail or setup.message,
        setup_supported=setup.state is not SandboxSetupState.UNAVAILABLE,
        restart_required=False,
        probe_version=1,
        capabilities=capabilities,
    )


class CapabilityService:
    """Deduplicate immutable capability probes by runtime fingerprint."""

    def __init__(
        self,
        probe: Callable[[str], Awaitable[CapabilityReport]],
    ) -> None:
        self._probe = probe
        self._cache: dict[str, CapabilityReport] = {}
        self._lock = asyncio.Lock()

    async def get(self, fingerprint: str) -> CapabilityReport:
        key = str(fingerprint)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        async with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
            report = await self._probe(key)
            self._cache[key] = report
            return report

    def invalidate(self) -> None:
        self._cache.clear()


__all__ = [
    "REQUIRED_SAFE_CAPABILITIES",
    "CapabilityReport",
    "CapabilityService",
    "capability_report_from_setup",
]
