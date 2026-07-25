from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from opensquilla.engine.agent import (
    _PROVIDER_STREAM_CLEANUP_TIMEOUT_SECONDS,
    Agent,
    _IterationStreamTimeoutError,
)
from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.types import (
    AgentConfig,
    DoneEvent,
)
from opensquilla.engine.types import (
    ErrorEvent as AgentErrorEvent,
)
from opensquilla.engine.types import (
    TextDeltaEvent as AgentTextDeltaEvent,
)
from opensquilla.gateway.config import GatewayConfig
from opensquilla.provider import TextDeltaEvent as ProviderTextDeltaEvent


async def _collect_agent_events(stream: AsyncIterator[Any]) -> list[Any]:
    return [event async for event in stream]


class _SessionConfigManager:
    def __init__(self, config: object | None) -> None:
        self.config = config

    def get_session_config(self, session_key: str) -> object | None:
        return self.config


def test_resolve_agent_iteration_timeout_prefers_explicit_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_AGENT_ITERATION_TIMEOUT", "222")
    runner = TurnRunner(
        provider_selector=None,
        session_manager=_SessionConfigManager(
            SimpleNamespace(agent_iteration_timeout_seconds=111.0)
        ),
        config=GatewayConfig(agent_iteration_timeout_seconds=333.0),
    )

    assert runner._resolve_agent_iteration_timeout("agent:main:test", 444.0) == 444.0


def test_resolve_agent_iteration_timeout_prefers_session_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_AGENT_ITERATION_TIMEOUT", "222")
    runner = TurnRunner(
        provider_selector=None,
        session_manager=_SessionConfigManager(
            SimpleNamespace(agent_iteration_timeout_seconds=111.0)
        ),
        config=GatewayConfig(agent_iteration_timeout_seconds=333.0),
    )

    assert runner._resolve_agent_iteration_timeout("agent:main:test") == 111.0


def test_resolve_agent_iteration_timeout_prefers_env_over_gateway_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_AGENT_ITERATION_TIMEOUT", "222")
    runner = TurnRunner(
        provider_selector=None,
        session_manager=_SessionConfigManager(None),
        config=GatewayConfig(agent_iteration_timeout_seconds=333.0),
    )

    assert runner._resolve_agent_iteration_timeout("agent:main:test") == 222.0


def test_resolve_agent_iteration_timeout_uses_gateway_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENSQUILLA_AGENT_ITERATION_TIMEOUT", raising=False)
    runner = TurnRunner(
        provider_selector=None,
        config=GatewayConfig(agent_iteration_timeout_seconds=333.0),
    )

    assert runner._resolve_agent_iteration_timeout("agent:main:test") == 333.0


def test_resolve_agent_iteration_timeout_uses_agent_default_without_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENSQUILLA_AGENT_ITERATION_TIMEOUT", raising=False)
    runner = TurnRunner(provider_selector=None, config=None)

    assert (
        runner._resolve_agent_iteration_timeout("agent:main:test")
        == AgentConfig().iteration_timeout
    )


def test_resolve_agent_iteration_timeout_invalid_env_falls_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_AGENT_ITERATION_TIMEOUT", "not-a-float")
    runner = TurnRunner(
        provider_selector=None,
        session_manager=_SessionConfigManager(None),
        config=GatewayConfig(agent_iteration_timeout_seconds=333.0),
    )

    assert runner._resolve_agent_iteration_timeout("agent:main:test") == 333.0


def test_resolve_agent_iteration_timeout_floors_to_5400_in_coding_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENSQUILLA_AGENT_ITERATION_TIMEOUT", raising=False)
    cfg = GatewayConfig(agent_iteration_timeout_seconds=600.0)
    cfg.skills.coding_mode = True  # coding mode waits on code-task up to 90 min
    runner = TurnRunner(provider_selector=None, config=cfg)
    # the per-iteration watchdog is floored so a long process(wait) is not clamped
    assert runner._resolve_agent_iteration_timeout("agent:main:test") == 5400.0


def test_resolve_agent_iteration_timeout_no_floor_when_coding_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENSQUILLA_AGENT_ITERATION_TIMEOUT", raising=False)
    cfg = GatewayConfig(agent_iteration_timeout_seconds=600.0)
    runner = TurnRunner(provider_selector=None, config=cfg)
    # coding mode OFF -> the configured small value is preserved (no floor)
    assert runner._resolve_agent_iteration_timeout("agent:main:test") == 600.0


def test_resolve_agent_iteration_timeout_rejects_invalid_explicit_value() -> None:
    runner = TurnRunner(provider_selector=None, config=GatewayConfig())

    with pytest.raises(ValueError, match="iteration_timeout"):
        runner._resolve_agent_iteration_timeout("agent:main:test", -1.0)


@pytest.mark.asyncio
async def test_run_threads_iteration_timeout_into_agent_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: runner.run(iteration_timeout=X) must reach AgentConfig.

    iteration_timeout was previously declared on TurnRunner.run() and
    referenced inside _run_turn() at the resolver call site, but never
    plumbed through _run_turn()'s signature or the two run() -> _run_turn()
    call sites. Every turn would hit NameError before reaching the resolver.
    The existing isolation tests above exercise the resolver directly and
    so would not have caught the threading gap.
    """
    from opensquilla.tools.types import ToolContext

    seen_kwargs: list[dict[str, Any]] = []
    real_agent_config = AgentConfig

    def recording_agent_config(**kwargs: Any) -> AgentConfig:
        seen_kwargs.append(kwargs)
        return real_agent_config(**kwargs)

    monkeypatch.setattr("opensquilla.engine.types.AgentConfig", recording_agent_config)

    provider = MagicMock()
    provider.provider_name = "stub"

    async def _chat(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        yield DoneEvent()

    provider.chat = _chat

    selector = MagicMock()
    selector.resolve.return_value = provider
    selector.clone.return_value = selector
    selector.current_config = MagicMock(model="stub-model")

    session_manager = MagicMock()
    session_manager.get = AsyncMock(return_value=None)
    session_manager.append_message = AsyncMock(return_value=None)
    session_manager.update = AsyncMock(return_value=None)
    session_manager.get_compaction_summary = AsyncMock(return_value=None)
    session_manager.get_transcript = AsyncMock(return_value=[])

    runner = TurnRunner(
        provider_selector=selector,
        session_manager=session_manager,
    )

    tool_ctx = ToolContext(session_key="agent:main:iter-thread-test")

    async for _ in runner.run(
        message="hi",
        session_key="agent:main:iter-thread-test",
        tool_context=tool_ctx,
        iteration_timeout=444.0,
    ):
        pass

    assert any(kw.get("iteration_timeout") == 444.0 for kw in seen_kwargs), (
        f"AgentConfig never received iteration_timeout=444.0; saw {seen_kwargs!r}"
    )


@pytest.mark.asyncio
async def test_stream_iteration_timeout_closes_provider_stream_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = Agent.__new__(Agent)
    agent.config = MagicMock(timeout=1.0, iteration_timeout=0.01)
    close_calls = 0

    async def provider_stream() -> AsyncIterator[dict[str, str]]:
        try:
            await asyncio.sleep(1.0)
            yield {"type": "chunk", "data": "late"}
        finally:
            await asyncio.sleep(0)

    async def record_close(
        _stream_iter: AsyncIterator[Any],
        *,
        require_aclose: bool = False,
    ) -> bool:
        nonlocal close_calls
        del require_aclose
        close_calls += 1
        return True

    monkeypatch.setattr(agent, "_close_provider_stream", record_close)

    loop = asyncio.get_running_loop()

    with pytest.raises(_IterationStreamTimeoutError):
        async for _event in agent._stream_provider_events_with_deadline(
            provider_stream(),
            loop=loop,
            total_deadline=None,
        ):
            pass

    assert close_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("deadline_kind", ["iteration", "total"])
async def test_cancellation_resistant_provider_cannot_block_deadline_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    deadline_kind: str,
) -> None:
    agent = Agent.__new__(Agent)
    agent.config = SimpleNamespace(timeout=0.02, iteration_timeout=0.02)
    release = asyncio.Event()
    closed = asyncio.Event()

    async def provider_stream() -> AsyncIterator[dict[str, str]]:
        try:
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
            yield {"type": "chunk", "data": "late"}
        finally:
            closed.set()

    monkeypatch.setattr(
        "opensquilla.engine.agent._PROVIDER_STREAM_CLEANUP_TIMEOUT_SECONDS",
        0.01,
    )
    loop = asyncio.get_running_loop()
    total_deadline = loop.time() + 0.02 if deadline_kind == "total" else None
    iteration_deadline = loop.time() + 0.02 if deadline_kind == "iteration" else loop.time() + 1.0

    async def consume() -> None:
        async for _event in agent._stream_provider_events_with_deadline(
            provider_stream(),
            loop=loop,
            total_deadline=total_deadline,
            iteration_deadline=iteration_deadline,
        ):
            pass

    expected_error = TimeoutError if deadline_kind == "total" else _IterationStreamTimeoutError
    try:
        with pytest.raises(expected_error):
            await asyncio.wait_for(consume(), timeout=0.2)
    finally:
        release.set()
    await asyncio.wait_for(closed.wait(), timeout=0.2)


@pytest.mark.asyncio
async def test_outer_turn_close_blocks_next_turn_until_provider_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = asyncio.Event()
    closed = asyncio.Event()

    class _CancellationResistantProvider:
        provider_name = "fake"

        def __init__(self) -> None:
            self.calls = 0

        def chat(
            self,
            *args: Any,
            **kwargs: Any,
        ) -> AsyncIterator[Any]:
            del args, kwargs
            self.calls += 1

            async def _stream() -> AsyncIterator[Any]:
                try:
                    yield ProviderTextDeltaEvent(text="partial")
                finally:
                    while not release.is_set():
                        try:
                            await release.wait()
                        except asyncio.CancelledError:
                            continue
                    closed.set()

            return _stream()

        async def list_models(self) -> list[Any]:
            return []

    monkeypatch.setattr(
        "opensquilla.engine.agent._PROVIDER_STREAM_CLEANUP_TIMEOUT_SECONDS",
        0.01,
    )
    provider = _CancellationResistantProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(timeout=1.0, iteration_timeout=1.0),
    )
    first_turn = agent.run_turn("first")

    try:
        while True:
            event = await asyncio.wait_for(first_turn.__anext__(), timeout=0.2)
            if isinstance(event, AgentTextDeltaEvent):
                break
        await asyncio.wait_for(first_turn.aclose(), timeout=0.2)

        second_events = [event async for event in agent.run_turn("second")]
        [cleanup_error] = [
            event for event in second_events if isinstance(event, AgentErrorEvent)
        ]
        assert cleanup_error.code == "agent_cleanup_in_progress"
        assert provider.calls == 1
    finally:
        release.set()
    await asyncio.wait_for(closed.wait(), timeout=0.5)


@pytest.mark.asyncio
async def test_agent_rejects_concurrent_turn_on_same_instance() -> None:
    class _Provider:
        provider_name = "fake"

        def __init__(self) -> None:
            self.calls = 0

        def chat(
            self,
            *args: Any,
            **kwargs: Any,
        ) -> AsyncIterator[Any]:
            del args, kwargs
            self.calls += 1

            async def _stream() -> AsyncIterator[Any]:
                yield ProviderTextDeltaEvent(text="unused")

            return _stream()

        async def list_models(self) -> list[Any]:
            return []

    provider = _Provider()
    agent = Agent(provider=provider, config=AgentConfig())
    first_turn = agent.run_turn("first")
    await first_turn.__anext__()
    try:
        second_events = [event async for event in agent.run_turn("second")]
    finally:
        await first_turn.aclose()

    [active_error] = [
        event for event in second_events if isinstance(event, AgentErrorEvent)
    ]
    assert active_error.code == "agent_turn_in_progress"
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_top_level_meta_resume_obeys_total_deadline_and_resets_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Provider:
        provider_name = "fake"

        def __init__(self) -> None:
            self.calls = 0

        def chat(self, *_args: Any, **_kwargs: Any) -> AsyncIterator[Any]:
            self.calls += 1

            async def _stream() -> AsyncIterator[Any]:
                yield ProviderTextDeltaEvent(text="unexpected")

            return _stream()

        async def list_models(self) -> list[Any]:
            return []

    provider = _Provider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            timeout=0.02,
            metadata={"meta_resume": ("claim", {})},
        ),
    )

    async def _slow_meta_resume(_value: Any) -> AsyncIterator[Any]:
        await asyncio.sleep(30.0)
        yield DoneEvent(text="late")

    monkeypatch.setattr(agent, "_run_meta_resume", _slow_meta_resume)

    events = await asyncio.wait_for(
        _collect_agent_events(agent.run_turn("resume")),
        timeout=0.2,
    )

    [timeout_error] = [
        event for event in events if isinstance(event, AgentErrorEvent)
    ]
    assert timeout_error.code == "agent_meta_resume_timeout"
    assert agent.state.value == "idle"
    assert agent._active_turn is False
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_top_level_meta_terminal_snapshot_survives_streamed_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Provider:
        provider_name = "fake"

        async def list_models(self) -> list[Any]:
            return []

    agent = Agent(
        provider=_Provider(),
        config=AgentConfig(
            timeout=1.0,
            metadata={"meta_resume": ("claim", {})},
        ),
    )

    async def _meta_resume(_value: Any) -> AsyncIterator[Any]:
        yield AgentTextDeltaEvent(text="streamed prefix")
        yield DoneEvent(
            text="authoritative final",
            text_snapshot="authoritative final",
            iterations=1,
        )

    monkeypatch.setattr(agent, "_run_meta_resume", _meta_resume)
    events = await _collect_agent_events(agent.run_turn("resume"))

    assert any(
        isinstance(event, AgentTextDeltaEvent)
        and event.text == "streamed prefix"
        for event in events
    )
    [done] = [event for event in events if isinstance(event, DoneEvent)]
    assert done.text_snapshot == "authoritative final"
    assert agent.state.value == "idle"


@pytest.mark.asyncio
async def test_total_deadline_limited_wait_stays_total_timeout_on_early_wake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = Agent.__new__(Agent)
    agent.config = SimpleNamespace(timeout=0.05, iteration_timeout=30.0)
    observed_timeouts: list[float | None] = []

    async def provider_stream() -> AsyncIterator[dict[str, str]]:
        await asyncio.sleep(30.0)
        yield {"type": "chunk", "data": "late"}

    async def return_before_deadline(
        futures: set[asyncio.Future[Any]],
        *,
        timeout: float | None = None,
    ) -> tuple[set[asyncio.Future[Any]], set[asyncio.Future[Any]]]:
        observed_timeouts.append(timeout)
        return set(), futures

    monkeypatch.setattr(asyncio, "wait", return_before_deadline)
    fake_loop: Any = SimpleNamespace(time=lambda: 10.0)

    with pytest.raises(TimeoutError, match="total timeout") as exc_info:
        async for _event in agent._stream_provider_events_with_deadline(
            provider_stream(),
            loop=fake_loop,
            total_deadline=10.05,
        ):
            pass

    assert type(exc_info.value) is TimeoutError
    assert observed_timeouts[0] == pytest.approx(0.05)
    assert observed_timeouts[1] == pytest.approx(_PROVIDER_STREAM_CLEANUP_TIMEOUT_SECONDS)
