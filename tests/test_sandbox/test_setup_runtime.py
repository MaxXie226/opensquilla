from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.sandbox.capability_service import CapabilityReport
from opensquilla.sandbox.setup_state import SandboxSetupState, SetupResult


@pytest.fixture(autouse=True)
def reset_setup_runtime_state():
    from opensquilla.sandbox.setup_runtime import reset_sandbox_setup_runtime_state

    reset_sandbox_setup_runtime_state()
    yield
    reset_sandbox_setup_runtime_state()


def test_live_capability_budget_covers_native_windows_canary_startup() -> None:
    from opensquilla.sandbox import setup_runtime

    assert setup_runtime._CAPABILITY_PROBE_TIMEOUT_SECONDS >= 20
    assert (
        setup_runtime._CAPABILITY_CACHE_TTL_SECONDS
        > setup_runtime._CAPABILITY_PROBE_TIMEOUT_SECONDS
    )


@pytest.mark.asyncio
async def test_status_reports_setting_up_while_auto_setup_is_running(monkeypatch) -> None:
    from opensquilla.sandbox import setup_runtime

    entered = asyncio.Event()
    release = asyncio.Event()
    config = SimpleNamespace()

    async def blocked_setup(setup_config):
        assert setup_config is config
        entered.set()
        await release.wait()
        return SetupResult(
            state=SandboxSetupState.READY,
            platform="linux",
            message="Sandbox setup is ready.",
            requires_admin=False,
        )

    monkeypatch.setattr(setup_runtime, "ensure_sandbox_setup", blocked_setup)

    task = asyncio.create_task(setup_runtime.ensure_sandbox_setup_auto(config))
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    try:
        status = await setup_runtime.current_sandbox_setup_runtime_status(config)

        assert status.state is SandboxSetupState.SETTING_UP
        assert status.platform == "auto"
    finally:
        release.set()

    await task


@pytest.mark.asyncio
async def test_auto_setup_failure_remains_visible_after_setup_finishes(monkeypatch) -> None:
    from opensquilla.sandbox import setup_runtime

    config = SimpleNamespace()

    async def fail_setup(_config):
        raise RuntimeError("setup exploded")

    async def current_probe(_config):
        return SetupResult(
            state=SandboxSetupState.NOT_SETUP,
            platform="linux",
            message="Sandbox setup has not been completed.",
            requires_admin=False,
        )

    monkeypatch.setattr(setup_runtime, "ensure_sandbox_setup", fail_setup)
    monkeypatch.setattr(setup_runtime, "current_sandbox_setup_status", current_probe)

    result = await setup_runtime.ensure_sandbox_setup_auto(config)
    status = await setup_runtime.current_sandbox_setup_runtime_status(config)

    assert result.state is SandboxSetupState.FAILED
    assert result.detail == "setup exploded"
    assert status is result


@pytest.mark.asyncio
async def test_windows_auto_setup_promotes_runtime_backend_after_setup(monkeypatch) -> None:
    from opensquilla.sandbox import integration, setup_runtime

    config = SimpleNamespace()
    promotions = []

    async def ready_setup(_config):
        return SetupResult(
            state=SandboxSetupState.READY,
            platform="win32",
            message="Windows default sandbox is ready.",
            requires_admin=False,
        )

    monkeypatch.setattr(setup_runtime, "ensure_sandbox_setup", ready_setup)
    monkeypatch.setattr(
        integration,
        "refresh_runtime_backend_after_setup",
        lambda: promotions.append("promoted"),
        raising=False,
    )

    result = await setup_runtime.ensure_sandbox_setup_auto(config)

    assert result.state is SandboxSetupState.READY
    assert promotions == ["promoted"]


@pytest.mark.asyncio
async def test_windows_auto_setup_reports_failed_when_runtime_cannot_be_promoted(
    monkeypatch,
) -> None:
    from opensquilla.sandbox import integration, setup_runtime

    async def ready_setup(_config):
        return SetupResult(
            state=SandboxSetupState.READY,
            platform="win32",
            message="Windows default sandbox is ready.",
            requires_admin=False,
        )

    monkeypatch.setattr(setup_runtime, "ensure_sandbox_setup", ready_setup)
    monkeypatch.setattr(
        integration,
        "refresh_runtime_backend_after_setup",
        lambda: (_ for _ in ()).throw(RuntimeError("backend still unavailable")),
        raising=False,
    )

    result = await setup_runtime.ensure_sandbox_setup_auto(SimpleNamespace())

    assert result.state is SandboxSetupState.FAILED
    assert result.platform == "win32"
    assert result.detail == "backend still unavailable"


@pytest.mark.asyncio
async def test_reset_setup_runtime_state_delegates_to_current_probe_again(monkeypatch) -> None:
    from opensquilla.sandbox import setup_runtime

    config = SimpleNamespace()

    async def fail_setup(_config):
        raise RuntimeError("setup exploded")

    async def current_probe(_config):
        return SetupResult(
            state=SandboxSetupState.NOT_SETUP,
            platform="linux",
            message="Sandbox setup has not been completed.",
            requires_admin=False,
        )

    monkeypatch.setattr(setup_runtime, "ensure_sandbox_setup", fail_setup)
    monkeypatch.setattr(setup_runtime, "current_sandbox_setup_status", current_probe)
    await setup_runtime.ensure_sandbox_setup_auto(config)

    setup_runtime.reset_sandbox_setup_runtime_state()
    status = await setup_runtime.current_sandbox_setup_runtime_status(config)

    assert status.state is SandboxSetupState.NOT_SETUP
    assert status.message == "Sandbox setup has not been completed."


@pytest.mark.asyncio
async def test_capability_report_uses_live_setup_and_configured_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox import setup_runtime

    config = SimpleNamespace(sandbox=SimpleNamespace(backend="windows_default"))

    async def current_probe(_config: object) -> SetupResult:
        return SetupResult(
            state=SandboxSetupState.READY,
            platform="win32",
            message="ready",
        )

    monkeypatch.setattr(setup_runtime, "current_sandbox_setup_status", current_probe)
    expected = CapabilityReport.available_for(
        backend="windows_default",
        platform="win32",
        reason="probe",
    )

    async def live_probe(*_args: object, **_kwargs: object) -> CapabilityReport:
        return expected

    monkeypatch.setattr(setup_runtime, "_probe_runtime_capabilities", live_probe)
    monkeypatch.setattr(
        "opensquilla.sandbox.integration.get_runtime",
        lambda: SimpleNamespace(
            backend=SimpleNamespace(name="windows_default"),
        ),
    )
    setup_runtime.reset_sandbox_setup_runtime_state()

    report = await setup_runtime.current_sandbox_capability_report(config)

    assert report.available is True
    assert report.backend == "windows_default"
    assert report.code == "ready"


@pytest.mark.asyncio
async def test_capability_report_force_refresh_bypasses_cached_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox import setup_runtime

    config = SimpleNamespace(sandbox=SimpleNamespace(backend="windows_default"))

    async def current_probe(_config: object) -> SetupResult:
        return SetupResult(
            state=SandboxSetupState.READY,
            platform="win32",
            message="ready",
        )

    calls = 0

    async def live_probe(*_args: object, **_kwargs: object) -> CapabilityReport:
        nonlocal calls
        calls += 1
        return CapabilityReport.available_for(
            backend="windows_default",
            platform="win32",
            reason=f"probe-{calls}",
        )

    monkeypatch.setattr(setup_runtime, "current_sandbox_setup_status", current_probe)
    monkeypatch.setattr(setup_runtime, "_probe_runtime_capabilities", live_probe)
    monkeypatch.setattr(
        "opensquilla.sandbox.integration.get_runtime",
        lambda: SimpleNamespace(
            backend=SimpleNamespace(name="windows_default"),
        ),
    )

    first = await setup_runtime.current_sandbox_capability_report(config)
    cached = await setup_runtime.current_sandbox_capability_report(config)
    refreshed = await setup_runtime.current_sandbox_capability_report(
        config,
        force_refresh=True,
    )

    assert first.reason == cached.reason == "probe-1"
    assert refreshed.reason == "probe-2"
    assert calls == 2


@pytest.mark.asyncio
async def test_concurrent_force_refreshes_share_one_live_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox import setup_runtime

    config = SimpleNamespace(sandbox=SimpleNamespace(backend="windows_default"))

    async def current_probe(_config: object) -> SetupResult:
        return SetupResult(
            state=SandboxSetupState.READY,
            platform="win32",
            message="ready",
        )

    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def live_probe(*_args: object, **_kwargs: object) -> CapabilityReport:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return CapabilityReport.available_for(
            backend="windows_default",
            platform="win32",
            reason=f"probe-{calls}",
        )

    monkeypatch.setattr(setup_runtime, "current_sandbox_setup_status", current_probe)
    monkeypatch.setattr(setup_runtime, "_probe_runtime_capabilities", live_probe)
    monkeypatch.setattr(
        "opensquilla.sandbox.integration.get_runtime",
        lambda: SimpleNamespace(
            backend=SimpleNamespace(name="windows_default"),
        ),
    )
    setup_runtime.reset_sandbox_setup_runtime_state()

    first = asyncio.create_task(
        setup_runtime.current_sandbox_capability_report(config, force_refresh=True)
    )
    await entered.wait()
    second = asyncio.create_task(
        setup_runtime.current_sandbox_capability_report(config, force_refresh=True)
    )
    await asyncio.sleep(0)
    release.set()

    first_report, second_report = await asyncio.gather(first, second)

    assert calls == 1
    assert first_report.reason == second_report.reason == "probe-1"


@pytest.mark.asyncio
async def test_failed_capability_report_expires_before_successful_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox import setup_runtime

    config = SimpleNamespace(sandbox=SimpleNamespace(backend="windows_default"))

    async def current_probe(_config: object) -> SetupResult:
        return SetupResult(
            state=SandboxSetupState.READY,
            platform="win32",
            message="ready",
        )

    clock = [100.0]
    calls = 0

    async def live_probe(*_args: object, **_kwargs: object) -> CapabilityReport:
        nonlocal calls
        calls += 1
        if calls == 1:
            return CapabilityReport(
                available=False,
                backend="windows_default",
                platform="win32",
                code="probe_timeout",
                reason="timed out",
                setup_supported=True,
                restart_required=False,
                probe_version=1,
                capabilities=frozenset(),
            )
        return CapabilityReport.available_for(
            backend="windows_default",
            platform="win32",
            reason="ready",
        )

    monkeypatch.setattr(setup_runtime, "current_sandbox_setup_status", current_probe)
    monkeypatch.setattr(setup_runtime, "_probe_runtime_capabilities", live_probe)
    monkeypatch.setattr(setup_runtime.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        "opensquilla.sandbox.integration.get_runtime",
        lambda: SimpleNamespace(backend=SimpleNamespace(name="windows_default")),
    )

    first = await setup_runtime.current_sandbox_capability_report(config)
    clock[0] += 11.0
    second = await setup_runtime.current_sandbox_capability_report(config)
    clock[0] += 11.0
    cached = await setup_runtime.current_sandbox_capability_report(config)

    assert first.available is False
    assert second.available is cached.available is True
    assert calls == 2


@pytest.mark.asyncio
async def test_live_capability_probe_scopes_file_profile_to_canary_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from opensquilla.sandbox import file_policy, setup_runtime
    from opensquilla.sandbox.config import SandboxSettings
    from opensquilla.sandbox.operation_runtime import SandboxOperationResult
    from opensquilla.sandbox.permissions import FileSystemPermissionProfile
    from opensquilla.sandbox.types import SandboxResult

    captured: dict[str, object] = {}
    operation_ids: list[str] = []

    def compile_profile(*args: object, **kwargs: object) -> FileSystemPermissionProfile:
        captured.update(kwargs)
        return FileSystemPermissionProfile(entries=())

    class _Backend:
        def available(self) -> bool:
            return True

        def operation_domains_supported(self) -> frozenset[str]:
            return frozenset({"filesystem"})

        async def run(self, request: object) -> SandboxResult:
            captured["processActionKind"] = getattr(request, "action_kind", None)
            return SandboxResult(
                returncode=0,
                stdout="opensquilla-safe-probe",
                stderr="",
                wall_time_s=0.0,
                backend_used="windows_default",
            )

        async def run_operation(self, operation: object) -> SandboxOperationResult:
            operation_ids.append(str(getattr(operation, "operation_id", "")))
            path = Path(str(getattr(getattr(operation, "request", None), "path", "")))
            if path.name in {"must-remain.txt", "authority.txt"}:
                raise PermissionError("expected canary denial")
            return SandboxOperationResult(message="worker-ok")

    monkeypatch.setattr(file_policy, "compile_safe_file_profile", compile_profile)
    setup = SetupResult(
        state=SandboxSetupState.READY,
        platform="win32",
        message="ready",
    )
    config = SimpleNamespace(
        sandbox=SandboxSettings(),
        state_dir=str(tmp_path),
    )

    report = await setup_runtime._probe_runtime_capabilities(
        config,
        setup=setup,
        backend="windows_default",
        backend_object=_Backend(),
    )

    assert report.available is True
    assert captured["home"] == captured["writable_roots"][0]
    assert captured["env"]["USERPROFILE"] == str(captured["home"])
    assert captured["env"]["HOME"] == str(captured["home"])
    assert captured["processActionKind"] == "capability.probe"
    assert operation_ids == ["capability-probe"] * 4
