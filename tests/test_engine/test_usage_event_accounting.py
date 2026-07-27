from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig, SubagentSpec
from opensquilla.engine.outcome import outcome_from_error
from opensquilla.engine.pricing import PriceEntry, ResolvedModelPrice
from opensquilla.engine.runtime import TurnRunner, _SelectorFallbackProvider
from opensquilla.engine.types import DoneEvent as AgentDone
from opensquilla.engine.types import ErrorEvent
from opensquilla.engine.usage_accounting import (
    UsageAccountingScope,
    UsageAccountingUnavailableError,
    UsageCallResult,
    UsageCallStart,
    UsageExecutionContext,
    account_provider_stream,
    bind_usage_accounting_scope,
    has_known_provider_usage_receipt,
    normalize_provider_usage,
    provider_usage_receipt_rows,
    usd_to_nanos,
)
from opensquilla.provider import ChatConfig, Message, ModelCapabilities
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import ErrorEvent as ProviderError
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.provider.ensemble import EnsembleMemberConfig, EnsembleProvider
from opensquilla.provider.preset_registry import get_preset
from opensquilla.provider.selector import ProviderConfig
from opensquilla.provider.types import ContentBlockImage, ProviderBillingReceipt
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from opensquilla.skills.meta.orchestrator import make_llm_chat_from_provider
from opensquilla.tools.types import CallerKind, ToolContext
from opensquilla.usage_reasons import (
    normalize_usage_unknown_reason,
    provider_error_usage_reason,
)


class _RecordingSink:
    def __init__(self) -> None:
        self.started: list[UsageCallStart] = []
        self.finalized: list[tuple[UsageCallStart, UsageCallResult]] = []
        self.unknown: list[tuple[UsageCallStart, str]] = []

    async def start(self, call: UsageCallStart) -> None:
        self.started.append(call)

    async def finalize(self, call: UsageCallStart, result: UsageCallResult) -> None:
        self.finalized.append((call, result))

    async def mark_unknown(self, call: UsageCallStart, reason: str) -> None:
        self.unknown.append((call, reason))


class _UnavailableSink(_RecordingSink):
    async def start(self, call: UsageCallStart) -> None:
        self.started.append(call)
        raise UsageAccountingUnavailableError("ledger busy")


class _DoneProvider:
    provider_name = "fake"

    def __init__(self, sink: _RecordingSink) -> None:
        self.sink = sink
        self.calls = 0

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        del messages, tools, config
        # start() is a fail-closed barrier before provider.chat is invoked.
        assert len(self.sink.started) == self.calls + 1
        self.calls += 1
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        yield ProviderText(text="ok")
        yield ProviderDone(
            input_tokens=11,
            output_tokens=3,
            cached_tokens=2,
            billed_cost=0.000000123,
            cost_source="provider_billed",
            model="model-a",
        )


class _ErrorProvider:
    provider_name = "fake"
    retry_failed_call_safe = False

    def __init__(self, code: str = "401") -> None:
        self.code = code

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        del messages, tools, config
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        yield ProviderError(message="denied", code=self.code)


class _BlockingProvider:
    provider_name = "fake"

    def __init__(self) -> None:
        self.entered = asyncio.Event()

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        del messages, tools, config
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        self.entered.set()
        await asyncio.Event().wait()
        if False:  # pragma: no cover - make this an async generator
            yield None


class _CloseTrackingIterator:
    def __init__(self, events: list[Any], *, block_close: bool = False) -> None:
        self._events = iter(events)
        self.close_calls = 0
        self.closed = False
        self.close_started = asyncio.Event()
        self.allow_close = asyncio.Event()
        if not block_close:
            self.allow_close.set()

    def __aiter__(self) -> _CloseTrackingIterator:
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._events)
        except StopIteration as exc:
            raise StopAsyncIteration from exc

    async def aclose(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        await self.allow_close.wait()
        self.closed = True


class _CallScopedSnapshotIterator(_CloseTrackingIterator):
    def __init__(self, events: list[Any], snapshot: ProviderError) -> None:
        super().__init__(events)
        self._snapshot = snapshot
        self.snapshot_calls = 0

    def usage_accounting_snapshot(self) -> ProviderError:
        self.snapshot_calls += 1
        return self._snapshot


class _NonClosableIterator:
    def __init__(self, events: list[Any]) -> None:
        self._events = iter(events)

    def __aiter__(self) -> _NonClosableIterator:
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._events)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _SequenceProvider:
    provider_name = "fake"

    def __init__(self, streams: list[list[Any]]) -> None:
        self.streams = streams
        self.calls = 0

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        del messages, tools, config
        events = self.streams[self.calls]
        self.calls += 1
        return self._stream(events)

    async def _stream(self, events: list[Any]) -> AsyncIterator[Any]:
        for event in events:
            yield event


class _PhysicalLegProvider(_SequenceProvider):
    def __init__(self, name: str, events: list[Any]) -> None:
        super().__init__([events])
        self.provider_name = name


class _FallbackSelector:
    def __init__(self, fallback: Any) -> None:
        self._fallback = fallback
        self.active_provider_id = "openai"
        self.current_config = SimpleNamespace(model="primary-model")

    def next_fallback_after_failure(self, exc: Exception) -> Any:
        del exc
        self.active_provider_id = "anthropic"
        self.current_config = SimpleNamespace(model="fallback-model")
        return self._fallback


class _SingleProviderSelector:
    def __init__(self, provider: Any) -> None:
        self.provider = provider
        self.active_provider_id = "fake"
        self.current_config = SimpleNamespace(model="model-a")

    def clone(self) -> _SingleProviderSelector:
        return _SingleProviderSelector(self.provider)

    def resolve(self) -> Any:
        return self.provider

    def override_model(self, model: str) -> None:
        self.current_config = SimpleNamespace(model=model)


class _RecordingTracker:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict[str, Any]]] = []

    def add(self, session_key: str, **values: Any) -> None:
        self.rows.append((session_key, values))

    def session_checkpoint(self, session_key: str) -> None:
        del session_key
        return None

    def session_delta_snapshot(self, session_key: str, checkpoint: Any) -> None:
        del session_key, checkpoint
        return None

    def session_snapshot(self, session_key: str) -> None:
        del session_key
        return None

    def get(self, session_key: str) -> None:
        del session_key
        return None


def _context() -> UsageExecutionContext:
    return UsageExecutionContext(
        execution_id="turn-1",
        agent_run_id="run-1",
        turn_id="turn-1",
        session_id="session-1",
        session_epoch=7,
        agent_id="main",
        run_kind="webchat",
    )


def _image_rejecting_ensemble(*, fallback_provider: Any | None = None) -> EnsembleProvider:
    return EnsembleProvider(
        profile_name="image-validation",
        proposers=[],
        aggregator=EnsembleMemberConfig(
            provider_config=ProviderConfig(provider="fake", model="never-called")
        ),
        fallback_provider=fallback_provider,
        fallback_provider_name="fake" if fallback_provider is not None else "",
        fallback_model="fallback-model" if fallback_provider is not None else "",
        all_failed_policy="fallback_single" if fallback_provider is not None else "error",
    )


def _image_message() -> Message:
    return Message(
        role="user",
        content=[ContentBlockImage(media_type="image/png", data="aW1hZ2U=")],
    )


class _CapturingTurnLog:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def write(self, kind: str, payload: dict[str, Any]) -> None:
        self.records.append({"kind": kind, "payload": payload})


@pytest.mark.asyncio
async def test_selector_preflight_rejects_ensemble_image_before_usage_or_fallback() -> None:
    sink = _RecordingSink()
    fallback = _PhysicalLegProvider(
        "anthropic",
        [ProviderText(text="must not run"), ProviderDone(model="fallback-model")],
    )
    wrapper = _SelectorFallbackProvider(
        _image_rejecting_ensemble(fallback_provider=fallback),
        _FallbackSelector(fallback),
    )
    scope = UsageAccountingScope(sink=sink, context=_context())

    with bind_usage_accounting_scope(scope):
        events = [event async for event in wrapper.chat([_image_message()])]

    assert [getattr(event, "code", "") for event in events] == [
        "ensemble_multimodal_unsupported"
    ]
    assert fallback.calls == 0
    assert sink.started == []
    assert sink.finalized == []
    assert sink.unknown == []


@pytest.mark.asyncio
@pytest.mark.parametrize("image_location", ["current", "history"])
@pytest.mark.parametrize("wrapped_by_selector", [False, True])
async def test_agent_preflight_rejects_ensemble_image_before_call_accounting(
    image_location: str,
    wrapped_by_selector: bool,
) -> None:
    sink = _RecordingSink()
    tracker = _RecordingTracker()
    fallback = _PhysicalLegProvider(
        "anthropic",
        [ProviderText(text="must not run"), ProviderDone(model="fallback-model")],
    )
    observer_calls: list[dict[str, Any]] = []
    turn_log = _CapturingTurnLog()
    turn_metadata: dict[str, Any] = {}
    ensemble = _image_rejecting_ensemble(fallback_provider=fallback)
    provider: Any = (
        _SelectorFallbackProvider(
            ensemble,
            _FallbackSelector(fallback),
            turn_metadata,
        )
        if wrapped_by_selector
        else ensemble
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            max_provider_retries=3,
            model_id="ensemble/image-validation",
            model_capabilities=ModelCapabilities(supports_vision=True),
            preserve_historical_images=True,
            provider_call_observer=lambda **kwargs: observer_calls.append(kwargs),
        ),
        usage_tracker=tracker,
        session_key="agent:main:image-validation",
        turn_call_logger=turn_log,  # type: ignore[arg-type]
        usage_event_sink=sink,
        usage_execution_context=_context(),
    )
    if image_location == "history":
        agent.set_history([_image_message()])
        message = "continue"
        extra_messages = None
    else:
        message = ""
        extra_messages = [_image_message()]

    events = [
        event
        async for event in agent.run_turn(
            message,
            extra_messages=extra_messages,
        )
    ]

    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert [error.code for error in errors] == ["ensemble_multimodal_unsupported"]
    assert fallback.calls == 0
    assert sink.started == []
    assert sink.finalized == []
    assert sink.unknown == []
    assert tracker.rows == []
    assert observer_calls == []
    assert "router_fallback_hops" not in turn_metadata
    assert not any(record["kind"] == "llm_request" for record in turn_log.records)
    [decision] = [
        record for record in turn_log.records if record["kind"] == "turn_policy_decision"
    ]
    assert decision["payload"]["code"] == "ensemble_multimodal_unsupported"
    assert "messages" not in decision["payload"]


@pytest.mark.asyncio
async def test_provider_call_is_started_before_chat_and_finalized_once() -> None:
    sink = _RecordingSink()
    provider = _DoneProvider(sink)
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=1, provider_id="fake", model_id="model-a"),
        usage_event_sink=sink,
        usage_execution_context=_context(),
    )

    async for _ in agent.run_turn("hello"):
        pass

    assert provider.calls == 1
    assert [(call.execution_id, call.call_index) for call in sink.started] == [
        ("turn-1", 1)
    ]
    assert len(sink.finalized) == 1
    assert sink.unknown == []
    call, result = sink.finalized[0]
    assert call.event_id == sink.started[0].event_id
    assert call.session_epoch == 7
    assert result.billed_cost_nanos == 123
    assert result.estimated_cost_nanos == 0
    assert result.cost_source == "provider_billed"


@pytest.mark.asyncio
async def test_ledger_start_failure_is_retryable_and_withholds_provider_request() -> None:
    sink = _UnavailableSink()
    provider = _SequenceProvider([[ProviderDone()]])
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=1),
        usage_event_sink=sink,
        usage_execution_context=_context(),
    )

    with pytest.raises(UsageAccountingUnavailableError, match="ledger busy"):
        async for _ in agent.run_turn("hello"):
            pass

    assert provider.calls == 0
    outcome = outcome_from_error(code=UsageAccountingUnavailableError.code)
    assert outcome.kind == "blocked"
    assert outcome.retryable is True


@pytest.mark.asyncio
async def test_provider_error_closes_started_call_as_unknown() -> None:
    sink = _RecordingSink()
    agent = Agent(
        provider=_ErrorProvider(),
        config=AgentConfig(max_iterations=1, max_provider_retries=0),
        usage_event_sink=sink,
        usage_execution_context=_context(),
    )

    async for _ in agent.run_turn("hello"):
        pass

    assert len(sink.started) == 1
    assert sink.finalized == []
    assert [(call.event_id, reason) for call, reason in sink.unknown] == [
        (sink.started[0].event_id, "provider_error:401")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ProviderError(
            message="call rejected before start",
            code="call_in_progress",
            request_started=False,
        ),
        ProviderError(
            message="call rejected before start",
            code="call_in_progress",
            physical_request_count=0,
        ),
    ],
)
async def test_explicit_not_started_error_finalizes_zero_without_foreign_snapshot(
    error: ProviderError,
) -> None:
    sink = _RecordingSink()
    scope = UsageAccountingScope(sink=sink, context=_context())
    snapshot_calls = 0

    async def stream() -> AsyncIterator[ProviderError]:
        yield error

    def foreign_snapshot() -> ProviderError:
        nonlocal snapshot_calls
        snapshot_calls += 1
        return ProviderError(
            message="snapshot from another call",
            code="foreign_snapshot",
            request_started=True,
            physical_request_count=2,
            usage_missing_count=2,
        )

    with bind_usage_accounting_scope(scope):
        events = [
            event
            async for event in account_provider_stream(
                stream,
                provider="ensemble",
                model="aggregator",
                usage_snapshot=foreign_snapshot,
            )
        ]

    assert events == [error]
    assert snapshot_calls == 0
    assert len(sink.started) == 1
    assert len(sink.finalized) == 1
    assert sink.finalized[0][1].items == ()
    assert sink.finalized[0][1].missing_usage_entries == 0
    assert sink.unknown == []


@pytest.mark.asyncio
async def test_agent_does_not_persist_untrusted_provider_error_code() -> None:
    sink = _RecordingSink()
    agent = Agent(
        provider=_ErrorProvider("https://provider.invalid/error?key=sk-secret\nnext"),
        config=AgentConfig(max_iterations=1, max_provider_retries=0),
        usage_event_sink=sink,
        usage_execution_context=_context(),
    )

    async for _ in agent.run_turn("hello"):
        pass

    assert [reason for _, reason in sink.unknown] == ["provider_error"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("401", "provider_error:401"),
        ("rate_limit_error", "provider_error:rate_limit"),
        ("timeout", "provider_error:timeout"),
        ("vendor_private_code", "provider_error"),
        ("https://provider.invalid/error", "provider_error"),
        ("sk-proj-secret-value", "provider_error"),
        ("401\nrequest-id", "provider_error"),
    ],
)
def test_provider_error_reason_uses_closed_taxonomy(value: str, expected: str) -> None:
    assert provider_error_usage_reason(value) == expected


def test_unknown_reason_normalizer_drops_exception_and_arbitrary_details() -> None:
    assert normalize_usage_unknown_reason("raised:SecretBearingException") == (
        "provider_exception"
    )
    assert normalize_usage_unknown_reason("arbitrary-third-party-value") == "usage_unknown"


@pytest.mark.asyncio
async def test_cancelled_provider_call_is_marked_unknown() -> None:
    sink = _RecordingSink()
    provider = _BlockingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=1),
        usage_event_sink=sink,
        usage_execution_context=_context(),
    )

    async def consume() -> None:
        async for _ in agent.run_turn("hello"):
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(provider.entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(sink.started) == 1
    assert sink.finalized == []
    assert len(sink.unknown) == 1
    assert sink.unknown[0][1] == "cancelled"


@pytest.mark.asyncio
async def test_cancelled_composite_call_finalizes_snapshot_multiplicity() -> None:
    sink = _RecordingSink()
    scope = UsageAccountingScope(sink=sink, context=_context())
    entered = asyncio.Event()
    never = asyncio.Event()

    async def stream() -> AsyncIterator[ProviderText]:
        entered.set()
        await never.wait()
        yield ProviderText(text="unreachable")

    def usage_snapshot() -> ProviderError:
        return ProviderError(
            message="composite cancelled",
            code="composite_usage_snapshot",
            request_started=True,
            physical_request_count=2,
            usage_missing_count=2,
        )

    async def consume() -> None:
        with bind_usage_accounting_scope(scope):
            async for _ in account_provider_stream(
                stream,
                provider="ensemble",
                model="aggregator",
                usage_snapshot=usage_snapshot,
            ):
                pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(sink.started) == 1
    assert len(sink.finalized) == 1
    assert sink.finalized[0][1].items == ()
    assert sink.finalized[0][1].missing_usage_entries == 2
    assert sink.unknown == []


@pytest.mark.asyncio
async def test_runtime_wrapper_finalizes_cancelled_ensemble_fallback_multiplicity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink = _RecordingSink()
    scope = UsageAccountingScope(sink=sink, context=_context())
    fallback_entered = asyncio.Event()
    never = asyncio.Event()

    class _FailedProposer:
        provider_name = "fake"

        async def chat(
            self,
            messages: list[Message],
            tools: list[Any] | None = None,
            config: ChatConfig | None = None,
        ) -> AsyncIterator[Any]:
            del messages, tools, config
            yield ProviderError(
                message="proposer failed",
                code="503",
                request_started=True,
                physical_request_count=1,
            )

    class _BlockingFallback:
        provider_name = "fallback"

        async def chat(
            self,
            messages: list[Message],
            tools: list[Any] | None = None,
            config: ChatConfig | None = None,
        ) -> AsyncIterator[Any]:
            del messages, tools, config
            fallback_entered.set()
            await never.wait()
            if False:  # pragma: no cover - keep this an async generator
                yield ProviderDone(model="fallback")

    monkeypatch.setattr(
        "opensquilla.provider.ensemble._build_provider",
        lambda _cfg: _FailedProposer(),
    )
    member = EnsembleMemberConfig(
        provider_config=ProviderConfig(provider="fake", model="proposer"),
    )
    fallback = _BlockingFallback()
    ensemble = EnsembleProvider(
        profile_name="accounting-test",
        proposers=[member],
        aggregator=member,
        fallback_provider=fallback,
        fallback_provider_name="fallback",
        fallback_model="fallback",
        min_successful_proposers=1,
        all_failed_policy="fallback_single",
        shuffle_candidates=False,
    )
    wrapper = _SelectorFallbackProvider(
        ensemble,
        _FallbackSelector(fallback),
    )

    async def consume() -> None:
        async for _ in wrapper.chat(
            [Message(role="user", content="x")],
            config=ChatConfig(),
        ):
            pass

    with bind_usage_accounting_scope(scope):
        task = asyncio.create_task(consume())
        await asyncio.wait_for(fallback_entered.wait(), timeout=1)
        concurrent_events = [
            event
            async for event in wrapper.chat(
                [Message(role="user", content="concurrent")],
                config=ChatConfig(),
            )
        ]
        concurrent_error = next(
            event
            for event in concurrent_events
            if isinstance(event, ProviderError)
        )
        assert concurrent_error.code == "ensemble_call_in_progress"
        assert concurrent_error.request_started is False
        assert concurrent_error.physical_request_count == 0
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    snapshot = ensemble.usage_accounting_snapshot()
    assert snapshot is not None
    assert snapshot.physical_request_count == 2
    assert snapshot.usage_missing_count == 2
    assert len(sink.started) == 2
    assert len(sink.finalized) == 2
    assert all(result.items == () for _, result in sink.finalized)
    assert sorted(result.missing_usage_entries for _, result in sink.finalized) == [
        0,
        2,
    ]
    assert sink.unknown == []


@pytest.mark.asyncio
async def test_accounted_stream_closes_owned_iterator_without_scope() -> None:
    physical = _CloseTrackingIterator([ProviderText(text="partial")])
    stream = account_provider_stream(
        lambda: physical,
        provider="fake",
        model="model-a",
    )

    assert (await anext(stream)).text == "partial"
    await stream.aclose()

    assert physical.close_calls == 1
    assert physical.closed is True


@pytest.mark.asyncio
async def test_accounted_stream_prefers_call_scoped_snapshot_over_provider_snapshot() -> None:
    sink = _RecordingSink()
    scope = UsageAccountingScope(sink=sink, context=_context())
    physical = _CallScopedSnapshotIterator(
        [ProviderText(text="partial")],
        ProviderError(
            message="this call's snapshot",
            code="call_snapshot",
            request_started=True,
            physical_request_count=1,
            usage_missing_count=1,
        ),
    )
    foreign_snapshot_calls = 0

    def foreign_snapshot() -> ProviderError:
        nonlocal foreign_snapshot_calls
        foreign_snapshot_calls += 1
        return ProviderError(
            message="another call's snapshot",
            code="foreign_snapshot",
            request_started=True,
            physical_request_count=3,
            usage_missing_count=3,
        )

    with bind_usage_accounting_scope(scope):
        stream = account_provider_stream(
            lambda: physical,
            provider="ensemble",
            model="aggregator",
            usage_snapshot=foreign_snapshot,
        )
        assert (await anext(stream)).text == "partial"
        await stream.aclose()

    assert physical.snapshot_calls == 1
    assert foreign_snapshot_calls == 0
    assert len(sink.finalized) == 1
    assert sink.finalized[0][1].missing_usage_entries == 1
    assert sink.unknown == []


@pytest.mark.asyncio
async def test_accounted_stream_close_survives_repeated_cancellation() -> None:
    sink = _RecordingSink()
    scope = UsageAccountingScope(sink=sink, context=_context())
    physical = _CloseTrackingIterator(
        [ProviderText(text="partial")],
        block_close=True,
    )
    with bind_usage_accounting_scope(scope):
        stream = account_provider_stream(
            lambda: physical,
            provider="fake",
            model="model-a",
        )
        assert (await anext(stream)).text == "partial"

    close_task = asyncio.create_task(stream.aclose())
    await asyncio.wait_for(physical.close_started.wait(), timeout=1)
    close_task.cancel()
    await asyncio.sleep(0)
    close_task.cancel()
    await asyncio.sleep(0)
    assert close_task.done() is False

    physical.allow_close.set()
    with pytest.raises(asyncio.CancelledError):
        await close_task

    assert physical.close_calls == 1
    assert physical.closed is True
    assert len(sink.started) == 1
    assert sink.finalized == []
    assert [(call.event_id, reason) for call, reason in sink.unknown] == [
        (sink.started[0].event_id, "provider_stream_ended_without_usage")
    ]


@pytest.mark.asyncio
async def test_accounted_stream_rejects_unclosable_nonterminal_iterator() -> None:
    physical = _NonClosableIterator([ProviderText(text="partial")])
    stream = account_provider_stream(
        lambda: physical,
        provider="fake",
        model="model-a",
    )

    assert (await anext(stream)).text == "partial"
    with pytest.raises(RuntimeError, match="does not support aclose"):
        await stream.aclose()


@pytest.mark.asyncio
async def test_accounted_stream_allows_terminal_unclosable_iterator() -> None:
    physical = _NonClosableIterator([ProviderDone(model="model-a")])
    stream = account_provider_stream(
        lambda: physical,
        provider="fake",
        model="model-a",
    )

    assert isinstance(await anext(stream), ProviderDone)
    await stream.aclose()


@pytest.mark.asyncio
async def test_retried_done_calls_get_monotonic_distinct_identities() -> None:
    sink = _RecordingSink()
    provider = _SequenceProvider(
        [
            [ProviderDone(input_tokens=2, output_tokens=0)],
            [ProviderText(text="ok"), ProviderDone(input_tokens=3, output_tokens=1)],
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            max_provider_retries=1,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
        usage_event_sink=sink,
        usage_execution_context=_context(),
    )

    async for _ in agent.run_turn("hello"):
        pass

    assert provider.calls == 2
    assert [call.call_index for call in sink.started] == [1, 2]
    assert len({call.event_id for call in sink.started}) == 2
    assert [call.event_id for call, _ in sink.finalized] == [
        call.event_id for call in sink.started
    ]
    assert sink.unknown == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_stream", "expected_code"),
    [
        (
            [ProviderDone(input_tokens=2, output_tokens=0)],
            "empty_response",
        ),
        ([], "provider_stream_incomplete"),
    ],
)
async def test_agent_does_not_replay_unsafe_composite_invalid_response(
    first_stream: list[Any],
    expected_code: str,
) -> None:
    provider = _SequenceProvider(
        [
            first_stream,
            [
                ProviderText(text="must-not-run"),
                ProviderDone(input_tokens=3, output_tokens=1),
            ],
        ]
    )
    provider.retry_failed_call_safe = False
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            max_provider_retries=1,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert provider.calls == 1
    assert not any(
        getattr(event, "text", "") == "must-not-run"
        for event in events
    )
    assert any(
        isinstance(event, ErrorEvent) and event.code == expected_code
        for event in events
    )


@pytest.mark.asyncio
async def test_agent_does_not_promote_unverified_provider_cost_to_billed() -> None:
    provider = _SequenceProvider(
        [
            [
                ProviderText(text="ok"),
                ProviderDone(
                    input_tokens=10,
                    output_tokens=2,
                    billed_cost=99.0,
                    cost_source="provider_billed_unverified",
                    model="model-a",
                ),
            ]
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=1, provider_id="openrouter", model_id="model-a"),
    )

    events = [event async for event in agent.run_turn("hello")]
    done = next(event for event in events if isinstance(event, AgentDone))

    assert done.billed_cost == 0.0
    assert done.cost_source != "provider_billed"


@pytest.mark.asyncio
async def test_agent_mixed_breakdown_keeps_exact_rows_without_washing_unverified_cost() -> None:
    provider = _SequenceProvider(
        [
            [
                ProviderText(text="ok"),
                ProviderDone(
                    input_tokens=30,
                    output_tokens=5,
                    billed_cost=0.75,
                    cost_source="mixed",
                    model="ensemble",
                    model_usage_breakdown=[
                        {
                            "provider": "openrouter",
                            "model": "model-exact",
                            "input_tokens": 10,
                            "output_tokens": 2,
                            "billed_cost": 0.25,
                            "cost_source": "provider_billed",
                        },
                        {
                            "provider": "openrouter",
                            "model": "model-unverified",
                            "input_tokens": 20,
                            "output_tokens": 3,
                            "billed_cost": 0.50,
                            "cost_source": "provider_billed_unverified",
                        },
                    ],
                ),
            ]
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=1, provider_id="openrouter", model_id="ensemble"),
    )

    events = [event async for event in agent.run_turn("hello")]
    done = next(event for event in events if isinstance(event, AgentDone))
    by_model = {row["model"]: row for row in done.model_usage_breakdown}

    assert done.billed_cost == pytest.approx(0.25)
    assert by_model["model-exact"]["billed_cost"] == pytest.approx(0.25)
    assert by_model["model-unverified"]["billed_cost"] == 0.0
    assert by_model["model-unverified"]["billed_cost_usd"] == 0.0
    assert by_model["model-unverified"]["cost_source"] != "provider_billed"


@pytest.mark.asyncio
async def test_agent_known_error_receipt_is_retained_in_totals_and_llm_error_log() -> None:
    provider = _SequenceProvider(
        [
            [
                ProviderError(
                    message="rate limited after completion",
                    code="429",
                    model_usage_breakdown=[
                        {
                            "provider": "openrouter",
                            "model": "model-a",
                            "input_tokens": 12,
                            "output_tokens": 3,
                            "billed_cost": 0.25,
                            "cost_source": "provider_billed",
                            "provider_usage": {
                                "is_byok": False,
                                "provider_reported_cost": 0.25,
                                "response_ids": ["failed-call-1"],
                                "router_metadata": {"is_byok": False},
                            },
                        }
                    ],
                    usage_missing_count=1,
                )
            ]
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            max_provider_retries=0,
            provider_id="openrouter",
            model_id="model-a",
        ),
    )
    call_logs: list[tuple[str, dict[str, Any]]] = []
    agent._write_turn_call_log = (  # type: ignore[method-assign]
        lambda kind, **payload: call_logs.append((kind, payload))
    )

    events = [event async for event in agent.run_turn("hello")]
    done = next(event for event in events if isinstance(event, AgentDone))
    error_log = next(payload for kind, payload in call_logs if kind == "llm_error")

    assert done.input_tokens == 12
    assert done.output_tokens == 3
    assert done.billed_cost == pytest.approx(0.25)
    assert done.model_usage_breakdown[0]["model"] == "model-a"
    assert error_log["usage"]["input_tokens"] == 12
    assert error_log["usage"]["billed_cost"] == pytest.approx(0.25)
    assert error_log["usage"]["model_usage_breakdown"][0]["provider_usage"][
        "response_ids"
    ] == ["failed-call-1"]
    assert error_log["usage_missing_count"] == 1


@pytest.mark.asyncio
async def test_agent_diagnostic_only_error_receipt_is_retained_exactly() -> None:
    diagnostic_row = {
        "provider": "",
        "model": "",
        "requested_provider": "openrouter",
        "requested_model": "model-a",
        "input_tokens": 12,
        "output_tokens": 3,
        "billed_cost": 0.25,
        "cost_source": "provider_billed",
    }
    error = ProviderError(
        message="response rejected after billing",
        code="response_invalid",
        diagnostic_done=ProviderDone(
            provider="",
            model="",
            requested_provider="openrouter",
            requested_model="model-a",
            input_tokens=12,
            output_tokens=3,
            billed_cost=0.25,
            cost_source="provider_billed",
            model_usage_breakdown=[diagnostic_row],
        ),
        request_started=True,
        physical_request_count=1,
    )
    provider = _SequenceProvider([[error]])
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            max_provider_retries=0,
            provider_id="openrouter",
            model_id="model-a",
        ),
    )

    assert has_known_provider_usage_receipt(error) is True
    events = [event async for event in agent.run_turn("hello")]
    done = next(event for event in events if isinstance(event, AgentDone))

    assert done.input_tokens == 12
    assert done.output_tokens == 3
    assert done.billed_cost == pytest.approx(0.25)
    assert len(done.model_usage_breakdown) == 1
    assert done.model_usage_breakdown[0]["model"] == ""
    assert done.model_usage_breakdown[0]["provider"] == ""
    assert done.model_usage_breakdown[0]["requested_model"] == "model-a"
    assert done.model_usage_breakdown[0]["requested_provider"] == "openrouter"
    assert done.model_usage_breakdown[0]["billed_cost"] == pytest.approx(0.25)


@pytest.mark.asyncio
async def test_agent_uses_confirmed_receipt_nanos_as_authoritative_cost() -> None:
    receipt = ProviderBillingReceipt(
        currency="USD",
        status="confirmed",
        amount_nanos=200_000_000,
        usd_equivalent_nanos=200_000_000,
        fx_native_per_usd_nanos=1_000_000_000,
    )
    provider = _SequenceProvider(
        [
            [
                ProviderText(text="ok"),
                ProviderDone(
                    provider="openrouter",
                    model="actual-model",
                    requested_provider="openrouter",
                    requested_model="requested-model",
                    input_tokens=5,
                    output_tokens=1,
                    billed_cost=0.1,
                    cost_source="provider_billed",
                    billing_receipt=receipt,
                )
            ]
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            provider_id="openrouter",
            model_id="requested-model",
        ),
    )
    call_logs: list[tuple[str, dict[str, Any]]] = []
    agent._write_turn_call_log = (  # type: ignore[method-assign]
        lambda kind, **payload: call_logs.append((kind, payload))
    )

    events = [event async for event in agent.run_turn("hello")]
    done = next(event for event in events if isinstance(event, AgentDone))
    response_log = next(
        payload for kind, payload in call_logs if kind == "llm_response"
    )

    assert done.billed_cost == pytest.approx(0.2)
    assert done.cost_source == "provider_billed"
    assert response_log["usage"]["billed_cost"] == pytest.approx(0.2)
    assert response_log["usage"]["billing_receipt"] == receipt


@pytest.mark.asyncio
async def test_agent_pending_receipt_never_reenters_exact_billed_totals() -> None:
    receipt = ProviderBillingReceipt(
        currency="USD",
        status="pending",
        amount_nanos=None,
        usd_equivalent_nanos=None,
        fx_native_per_usd_nanos=1_000_000_000,
    )
    provider = _SequenceProvider(
        [
            [
                ProviderText(text="ok"),
                ProviderDone(
                    provider="openrouter",
                    model="unknown-model",
                    input_tokens=5,
                    output_tokens=1,
                    billed_cost=0.1,
                    cost_source="provider_billed",
                    billing_receipt=receipt,
                )
            ]
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            provider_id="openrouter",
            model_id="unknown-model",
        ),
    )

    events = [event async for event in agent.run_turn("hello")]
    done = next(event for event in events if isinstance(event, AgentDone))

    assert done.billed_cost == 0.0
    assert done.cost_source != "provider_billed"


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_source", ["provider_billed", "openrouter_usage"])
async def test_agent_malformed_receipt_blocks_legacy_exact_cost_fallback(
    monkeypatch: pytest.MonkeyPatch,
    legacy_source: str,
) -> None:
    monkeypatch.setattr(
        "opensquilla.engine.pricing.resolve_model_price",
        lambda model, provider: ResolvedModelPrice(
            entry=PriceEntry(input_per_m=1.0, output_per_m=2.0),
            source="test",
        ),
    )
    provider = _SequenceProvider(
        [
            [
                ProviderText(text="ok"),
                ProviderDone(
                    provider="openrouter",
                    model="priced-model",
                    input_tokens=1_000_000,
                    output_tokens=0,
                    billed_cost=99.0,
                    cost_source=legacy_source,
                    billing_receipt={
                        "currency": "USD",
                        "status": "confirmed",
                        "amount_nanos": 1_000_000_000,
                        "usd_equivalent_nanos": 1_000_000_000,
                        # A USD receipt cannot reconcile at this FX rate.
                        "fx_native_per_usd_nanos": 2_000_000_000,
                        "schema_version": 1,
                    },
                )
            ]
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            provider_id="openrouter",
            model_id="priced-model",
        ),
    )

    events = [event async for event in agent.run_turn("hello")]
    done = next(event for event in events if isinstance(event, AgentDone))

    assert done.billed_cost == 0.0
    assert done.cost_usd == pytest.approx(1.0)
    assert done.cost_source == "opensquilla_static_estimate"
    assert done.estimate_basis == "cache_aware"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "receipt",
    [
        ProviderBillingReceipt(
            currency="USD",
            status="pending",
            amount_nanos=None,
            usd_equivalent_nanos=None,
            fx_native_per_usd_nanos=1_000_000_000,
        ),
        {
            "currency": "usd",
            "status": "confirmed",
            "amount_nanos": 1_000_000_000,
            "usd_equivalent_nanos": 1_000_000_000,
            "fx_native_per_usd_nanos": 1_000_000_000,
            "schema_version": 1,
        },
    ],
    ids=["pending", "malformed"],
)
async def test_agent_unresolved_mixed_receipt_keeps_non_exact_static_estimate(
    monkeypatch: pytest.MonkeyPatch,
    receipt: object,
) -> None:
    monkeypatch.setattr(
        "opensquilla.engine.pricing.resolve_model_price",
        lambda model, provider: ResolvedModelPrice(
            entry=PriceEntry(input_per_m=1.0, output_per_m=2.0),
            source="test",
        ),
    )
    provider = _SequenceProvider(
        [
            [
                ProviderText(text="ok"),
                ProviderDone(
                    provider="openrouter",
                    model="priced-model",
                    input_tokens=1_000_000,
                    output_tokens=0,
                    billed_cost=0.25,
                    cost_source="mixed",
                    billing_receipt=receipt,  # type: ignore[arg-type]
                )
            ]
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            provider_id="openrouter",
            model_id="priced-model",
        ),
    )

    events = [event async for event in agent.run_turn("hello")]
    done = next(event for event in events if isinstance(event, AgentDone))

    assert done.billed_cost == 0.0
    assert done.cost_usd == pytest.approx(1.0)
    assert done.cost_source == "opensquilla_static_estimate"
    assert done.cost_source not in {"provider_billed", "mixed"}
    assert done.estimate_basis == "cache_aware"


@pytest.mark.asyncio
async def test_accounting_scope_does_not_promote_requested_identity_to_actual() -> None:
    sink = _RecordingSink()
    provider = _SequenceProvider(
        [
            [
                ProviderText(text="ok"),
                ProviderDone(
                    provider="",
                    model="",
                    requested_provider="openrouter",
                    requested_model="requested-model",
                    input_tokens=1,
                    output_tokens=1,
                    billed_cost=0.0,
                    cost_source="provider_billed",
                )
            ]
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            provider_id="openrouter",
            model_id="requested-model",
        ),
        usage_event_sink=sink,
        usage_execution_context=_context(),
    )

    events = [event async for event in agent.run_turn("hello")]
    done = next(event for event in events if isinstance(event, AgentDone))

    assert done.provider == ""
    assert done.model == ""
    assert done.requested_provider == "openrouter"
    assert done.requested_model == "requested-model"
    assert sink.finalized[0][1].items[0].provider == ""
    assert sink.finalized[0][1].items[0].model == ""


@pytest.mark.asyncio
async def test_agent_merges_disjoint_outer_and_diagnostic_error_receipts() -> None:
    outer_row = {
        "provider": "openrouter",
        "model": "model-a",
        "input_tokens": 8,
        "output_tokens": 2,
        "billed_cost": 0.1,
        "cost_source": "provider_billed",
    }
    diagnostic_row = {
        "provider": "openrouter",
        "model": "model-b",
        "input_tokens": 10,
        "output_tokens": 3,
        "billed_cost": 0.2,
        "cost_source": "provider_billed",
    }
    error = ProviderError(
        message="composite response rejected",
        code="response_invalid",
        model_usage_breakdown=[outer_row],
        diagnostic_done=ProviderDone(
            provider="openrouter",
            model="model-b",
            input_tokens=10,
            output_tokens=3,
            billed_cost=0.2,
            cost_source="provider_billed",
            model_usage_breakdown=[diagnostic_row],
        ),
        request_started=True,
        physical_request_count=2,
    )
    provider = _SequenceProvider([[error]])
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            max_provider_retries=0,
            provider_id="openrouter",
            model_id="router",
        ),
    )

    events = [event async for event in agent.run_turn("hello")]
    done = next(event for event in events if isinstance(event, AgentDone))

    assert done.input_tokens == 18
    assert done.output_tokens == 5
    assert done.billed_cost == pytest.approx(0.3)
    assert [row["model"] for row in done.model_usage_breakdown] == [
        "model-a",
        "model-b",
    ]
    assert sum(
        float(row.get("billed_cost") or 0.0)
        for row in done.model_usage_breakdown
    ) == pytest.approx(0.3)


def test_ensemble_breakdown_is_one_envelope_with_items(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "opensquilla.engine.usage_accounting.resolve_model_price",
        lambda model, provider: ResolvedModelPrice(
            entry=PriceEntry(input_per_m=1.0, output_per_m=2.0),
            source="test",
        ),
    )
    event = ProviderDone(
        input_tokens=30,
        output_tokens=5,
        billed_cost=0.5,
        model_usage_breakdown=[
            {
                "provider": "p1",
                "model": "m1",
                "input_tokens": 10,
                "output_tokens": 2,
                "billed_cost": 0.5,
                "cost_source": "provider_billed",
            },
            {
                "provider": "p2",
                "model": "m2",
                "input_tokens": 20,
                "output_tokens": 3,
                "billed_cost": 0.0,
            },
        ],
    )

    result = normalize_provider_usage(
        event,
        default_provider="ensemble",
        default_model="aggregator",
        completed_at_ms=1234,
    )

    assert len(result.items) == 2
    assert result.billed_cost_nanos == usd_to_nanos("0.5")
    assert result.estimated_cost_nanos == usd_to_nanos("0.000026")
    assert result.cost_source == "mixed"
    assert result.input_tokens == sum(item.input_tokens for item in result.items)
    assert result.output_tokens == sum(item.output_tokens for item in result.items)
    assert result.reasoning_tokens == sum(item.reasoning_tokens for item in result.items)
    assert result.cache_read_tokens == sum(
        item.cache_read_tokens for item in result.items
    )
    assert result.cache_write_tokens == sum(
        item.cache_write_tokens for item in result.items
    )
    assert sum(item.billed_cost_nanos for item in result.items) == result.billed_cost_nanos
    assert (
        sum(item.estimated_cost_nanos for item in result.items)
        == result.estimated_cost_nanos
    )


def _tokenrhythm_receipt(*, usd_nanos: int) -> ProviderBillingReceipt:
    """Build an exact receipt at TokenRhythm's fixed 6.975 CNY/USD rate."""

    amount_numerator = usd_nanos * 279
    assert amount_numerator % 40 == 0
    return ProviderBillingReceipt(
        currency="CNY",
        status="confirmed",
        amount_nanos=amount_numerator // 40,
        usd_equivalent_nanos=usd_nanos,
        fx_native_per_usd_nanos=6_975_000_000,
    )


def test_tokenrhythm_single_done_reconciles_native_receipt_and_all_token_buckets() -> None:
    receipt = _tokenrhythm_receipt(usd_nanos=2_000)
    event = ProviderDone(
        input_tokens=101,
        output_tokens=17,
        reasoning_tokens=9,
        cached_tokens=37,
        cache_write_tokens=3,
        billed_cost=0.000002,
        cost_source="provider_billed",
        provider="tokenrhythm",
        model="deepseek-v4-pro",
        billing_receipt=receipt,
    )

    result = normalize_provider_usage(
        event,
        default_provider="tokenrhythm",
        default_model="deepseek-v4-pro",
        completed_at_ms=1234,
    )

    assert len(result.items) == 1
    [item] = result.items
    assert (
        item.input_tokens,
        item.output_tokens,
        item.reasoning_tokens,
        item.cache_read_tokens,
        item.cache_write_tokens,
    ) == (101, 17, 9, 37, 3)
    assert item.billing_receipt == receipt
    assert item.billed_cost_nanos == receipt.usd_equivalent_nanos == 2_000
    assert item.estimated_cost_nanos == 0
    assert item.cost_source == result.cost_source == "provider_billed"
    assert result.billed_cost_nanos == sum(row.billed_cost_nanos for row in result.items)


def test_tokenrhythm_inline_router_c0_c3_reconciles_each_physical_request() -> None:
    preset = get_preset("tokenrhythm")
    assert preset is not None
    tiers = preset.tier_defaults()
    expected_models = {
        "c0": "deepseek-v4-flash",
        "c1": "deepseek-v4-pro",
        "c2": "kimi-k2.7-code",
        "c3": "glm-5.2",
    }

    results: list[UsageCallResult] = []
    for index, (tier, expected_model) in enumerate(expected_models.items(), start=1):
        assert tiers[tier]["provider"] == "tokenrhythm"
        assert tiers[tier]["model"] == expected_model
        usd_nanos = index * 4_000
        receipt = _tokenrhythm_receipt(usd_nanos=usd_nanos)
        event = ProviderDone(
            input_tokens=index * 100,
            output_tokens=index * 10,
            reasoning_tokens=index * 3,
            cached_tokens=index * 20,
            cache_write_tokens=index,
            billed_cost=usd_nanos / 1_000_000_000,
            cost_source="provider_billed",
            provider="tokenrhythm",
            model=expected_model,
            billing_receipt=receipt,
        )
        results.append(
            normalize_provider_usage(
                event,
                default_provider=str(tiers[tier]["provider"]),
                default_model=str(tiers[tier]["model"]),
                completed_at_ms=1234 + index,
            )
        )

    physical_items = [item for result in results for item in result.items]
    assert [item.model for item in physical_items] == list(expected_models.values())
    assert all(item.provider == "tokenrhythm" for item in physical_items)
    assert all(item.cost_source == "provider_billed" for item in physical_items)
    assert all(item.estimated_cost_nanos == 0 for item in physical_items)
    assert sum(item.input_tokens for item in physical_items) == 1_000
    assert sum(item.output_tokens for item in physical_items) == 100
    assert sum(item.reasoning_tokens for item in physical_items) == 30
    assert sum(item.cache_read_tokens for item in physical_items) == 200
    assert sum(item.cache_write_tokens for item in physical_items) == 10
    assert sum(item.billed_cost_nanos for item in physical_items) == 40_000
    assert sum(result.billed_cost_nanos for result in results) == 40_000


@pytest.mark.parametrize(
    "untrusted_source",
    ["provider_billed_unverified", "openrouter_byok"],
)
def test_untrusted_provider_cost_never_enters_exact_billed_bucket(
    monkeypatch: pytest.MonkeyPatch,
    untrusted_source: str,
) -> None:
    monkeypatch.setattr(
        "opensquilla.engine.usage_accounting.resolve_model_price",
        lambda model, provider: ResolvedModelPrice(
            entry=PriceEntry(input_per_m=1.0, output_per_m=2.0),
            source="test",
        ),
    )
    result = normalize_provider_usage(
        ProviderDone(
            input_tokens=1_000_000,
            output_tokens=0,
            billed_cost=99.0,
            cost_source=untrusted_source,
            model="model-a",
        ),
        default_provider="openrouter",
        default_model="model-a",
        completed_at_ms=1,
    )

    assert result.billed_cost_nanos == 0
    assert result.estimated_cost_nanos == usd_to_nanos("1.0")
    assert result.cost_source == "opensquilla_estimate"
    assert result.items[0].cost_source == "opensquilla_estimate"


def test_explicit_zero_provider_bill_is_exact_and_not_estimated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "opensquilla.engine.usage_accounting.resolve_model_price",
        lambda model, provider: ResolvedModelPrice(
            entry=PriceEntry(input_per_m=1.0, output_per_m=2.0),
            source="test",
        ),
    )
    result = normalize_provider_usage(
        ProviderDone(
            input_tokens=1_000_000,
            output_tokens=0,
            billed_cost=0.0,
            cost_source="provider_billed",
            model="model-a",
        ),
        default_provider="openrouter",
        default_model="model-a",
        completed_at_ms=1,
    )

    assert result.billed_cost_nanos == 0
    assert result.estimated_cost_nanos == 0
    assert result.cost_source == "provider_billed"
    assert result.items[0].cost_source == "provider_billed"


def test_distinct_unknown_sources_aggregate_to_unavailable_not_mixed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "opensquilla.engine.usage_accounting.resolve_model_price",
        lambda model, provider: ResolvedModelPrice(
            entry=PriceEntry(input_per_m=0.0, output_per_m=0.0),
            source="missing",
        ),
    )
    monkeypatch.setattr(
        "opensquilla.engine.usage_accounting.estimate_cost",
        lambda **kwargs: SimpleNamespace(cost_usd=0.0, basis=None),
    )
    result = normalize_provider_usage(
        ProviderDone(
            input_tokens=3,
            output_tokens=2,
            billed_cost=0.0,
            model_usage_breakdown=[
                {
                    "provider": "openrouter",
                    "model": "model-a",
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "billed_cost": 0.0,
                    "cost_source": "openrouter_byok",
                },
                {
                    "provider": "openrouter",
                    "model": "model-b",
                    "input_tokens": 2,
                    "output_tokens": 1,
                    "billed_cost": 0.0,
                    "cost_source": "provider_billed_unverified",
                },
            ],
        ),
        default_provider="openrouter",
        default_model="ensemble",
        completed_at_ms=1,
    )

    assert result.cost_source == "unavailable"


def test_exact_zero_plus_unknown_aggregates_to_mixed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "opensquilla.engine.usage_accounting.resolve_model_price",
        lambda model, provider: ResolvedModelPrice(
            entry=PriceEntry(input_per_m=0.0, output_per_m=0.0),
            source="missing",
        ),
    )
    monkeypatch.setattr(
        "opensquilla.engine.usage_accounting.estimate_cost",
        lambda **kwargs: SimpleNamespace(cost_usd=0.0, basis=None),
    )
    result = normalize_provider_usage(
        ProviderDone(
            input_tokens=3,
            output_tokens=2,
            billed_cost=0.0,
            model_usage_breakdown=[
                {
                    "provider": "openrouter",
                    "model": "model-a",
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "billed_cost": 0.0,
                    "cost_source": "provider_billed",
                },
                {
                    "provider": "openrouter",
                    "model": "model-b",
                    "input_tokens": 2,
                    "output_tokens": 1,
                    "billed_cost": 0.0,
                    "cost_source": "provider_billed_unverified",
                },
            ],
        ),
        default_provider="openrouter",
        default_model="ensemble",
        completed_at_ms=1,
    )

    assert result.cost_source == "mixed"


def test_incomplete_breakdown_falls_back_to_done_envelope() -> None:
    event = ProviderDone(
        input_tokens=30,
        output_tokens=5,
        cached_tokens=4,
        billed_cost=0.75,
        cost_source="provider_billed",
        model="aggregate-model",
        model_usage_breakdown=[
            {
                "provider": "p1",
                "model": "m1",
                "input_tokens": 10,
                "output_tokens": 2,
                "billed_cost": 0.25,
                "cost_source": "provider_billed",
            }
        ],
    )

    result = normalize_provider_usage(
        event,
        default_provider="ensemble",
        default_model="aggregate-model",
        completed_at_ms=1234,
    )

    assert len(result.items) == 1
    assert result.input_tokens == 30
    assert result.output_tokens == 5
    assert result.cache_read_tokens == 4
    assert result.billed_cost_nanos == usd_to_nanos("0.75")
    assert result.items[0].model == "aggregate-model"


def test_partial_ensemble_error_preserves_known_items_and_missing_coverage() -> None:
    event = ProviderError(
        message="aggregator failed",
        code="ensemble_aggregator_error",
        model_usage_breakdown=[
            {
                "provider": "p1",
                "model": "m1",
                "input_tokens": 10,
                "output_tokens": 2,
                "billed_cost": 0.25,
                "cost_source": "provider_billed",
            }
        ],
        usage_missing_count=1,
    )

    result = normalize_provider_usage(
        event,
        default_provider="ensemble",
        default_model="aggregator",
        completed_at_ms=1234,
    )

    assert len(result.items) == 1
    assert result.items[0].model == "m1"
    assert result.billed_cost_nanos == usd_to_nanos("0.25")
    assert result.missing_usage_entries == 1


def test_explicit_physical_count_fills_unreported_missing_usage() -> None:
    event = ProviderError(
        message="second physical request has no receipt",
        code="response_invalid",
        model_usage_breakdown=[
            {
                "provider": "openrouter",
                "model": "model-a",
                "input_tokens": 5,
                "output_tokens": 1,
                "billed_cost": 0.01,
                "cost_source": "provider_billed",
            }
        ],
        usage_missing_count=0,
        request_started=True,
        physical_request_count=2,
    )

    result = normalize_provider_usage(
        event,
        default_provider="openrouter",
        default_model="model-a",
        completed_at_ms=1234,
    )

    assert len(result.items) == 1
    assert result.missing_usage_entries == 1


def test_diagnostic_receipts_with_distinct_response_ids_are_not_collapsed() -> None:
    def receipt_row(response_id: str) -> dict[str, Any]:
        return {
            "provider": "openrouter",
            "model": "model-a",
            "input_tokens": 5,
            "output_tokens": 1,
            "billed_cost": 0.01,
            "cost_source": "provider_billed",
            "provider_usage": {"response_ids": [response_id]},
        }

    event = ProviderError(
        message="two identical-metric physical responses",
        code="response_invalid",
        model_usage_breakdown=[receipt_row("response-a")],
        diagnostic_done=ProviderDone(
            provider="openrouter",
            model="model-a",
            input_tokens=5,
            output_tokens=1,
            billed_cost=0.01,
            cost_source="provider_billed",
            model_usage_breakdown=[receipt_row("response-b")],
        ),
        request_started=True,
        physical_request_count=2,
    )

    result = normalize_provider_usage(
        event,
        default_provider="openrouter",
        default_model="model-a",
        completed_at_ms=1234,
    )

    assert len(result.items) == 2
    assert result.billed_cost_nanos == usd_to_nanos("0.02")
    assert result.missing_usage_entries == 0


def test_diagnostic_overlap_enriches_poorer_outer_receipt_provenance() -> None:
    outer = {
        "provider": "openrouter",
        "model": "model-a",
        "input_tokens": 5,
        "output_tokens": 1,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
    }
    richer = {
        **outer,
        "provider_usage": {"response_ids": ["response-a"], "is_byok": False},
    }
    event = ProviderError(
        message="same receipt repeated with richer metadata",
        code="response_invalid",
        model_usage_breakdown=[outer],
        diagnostic_done=ProviderDone(
            provider="openrouter",
            model="model-a",
            input_tokens=5,
            output_tokens=1,
            billed_cost=0.01,
            cost_source="provider_billed",
            model_usage_breakdown=[richer],
        ),
        request_started=True,
        physical_request_count=1,
    )

    rows = provider_usage_receipt_rows(event)
    result = normalize_provider_usage(
        event,
        default_provider="openrouter",
        default_model="model-a",
        completed_at_ms=1234,
    )

    assert len(rows) == 1
    assert rows[0]["provider_usage"]["response_ids"] == ["response-a"]
    assert rows[0]["provider_usage"]["is_byok"] is False
    assert len(result.items) == 1
    assert result.missing_usage_entries == 0


def test_same_response_id_uses_authoritative_diagnostic_receipt_fields() -> None:
    outer = {
        "provider": "",
        "model": "",
        "requested_provider": "openrouter",
        "requested_model": "requested-model",
        "input_tokens": 0,
        "output_tokens": 0,
        "billed_cost": 0.0,
        "cost_source": "none",
        "provider_usage": {"response_ids": ["response-a"]},
    }
    diagnostic = {
        **outer,
        "provider": "actual-provider",
        "model": "actual-model",
        "input_tokens": 7,
        "output_tokens": 3,
        "billed_cost": 0.25,
        "cost_source": "provider_billed",
        "provider_usage": {
            "response_ids": ["response-a"],
            "is_byok": False,
        },
    }
    event = ProviderError(
        message="outer wrapper retained only a partial receipt",
        code="response_invalid",
        model_usage_breakdown=[outer],
        diagnostic_done=ProviderDone(
            model_usage_breakdown=[diagnostic],
            input_tokens=7,
            output_tokens=3,
            billed_cost=0.25,
            cost_source="provider_billed",
        ),
        physical_request_count=1,
        request_started=True,
    )

    rows = provider_usage_receipt_rows(event)
    result = normalize_provider_usage(
        event,
        default_provider="openrouter",
        default_model="requested-model",
        completed_at_ms=1234,
    )

    assert len(rows) == 1
    assert rows[0]["provider"] == "actual-provider"
    assert rows[0]["model"] == "actual-model"
    assert rows[0]["input_tokens"] == 7
    assert rows[0]["output_tokens"] == 3
    assert rows[0]["billed_cost"] == pytest.approx(0.25)
    assert rows[0]["cost_source"] == "provider_billed"
    assert rows[0]["requested_model"] == "requested-model"
    assert rows[0]["provider_usage"]["is_byok"] is False
    assert len(result.items) == 1
    assert result.items[0].provider == "actual-provider"
    assert result.items[0].model == "actual-model"
    assert result.billed_cost_nanos == usd_to_nanos("0.25")


@pytest.mark.parametrize("diagnostic_id_first", [False, True])
def test_receipt_matching_prioritizes_stable_ids_before_idless_fallback(
    diagnostic_id_first: bool,
) -> None:
    def row(response_id: str | None) -> dict[str, Any]:
        return {
            "provider": "openrouter",
            "model": "same-model",
            "input_tokens": 5,
            "output_tokens": 1,
            "billed_cost": 0.01,
            "cost_source": "provider_billed",
            "provider_usage": (
                {"response_ids": [response_id]} if response_id is not None else {}
            ),
        }

    id_row = row("response-a")
    idless_row = row(None)
    diagnostic_rows = [id_row, idless_row]
    if not diagnostic_id_first:
        diagnostic_rows.reverse()
    event = ProviderError(
        message="diagnostic copies arrive in an adversarial order",
        code="response_invalid",
        model_usage_breakdown=[row("response-a"), row("response-b")],
        diagnostic_done=ProviderDone(
            input_tokens=10,
            output_tokens=2,
            billed_cost=0.02,
            cost_source="provider_billed",
            model_usage_breakdown=diagnostic_rows,
        ),
        physical_request_count=2,
        request_started=True,
    )

    rows = provider_usage_receipt_rows(event)
    result = normalize_provider_usage(
        event,
        default_provider="openrouter",
        default_model="same-model",
        completed_at_ms=1234,
    )

    assert len(rows) == 2
    assert len(result.items) == 2
    assert result.billed_cost_nanos == usd_to_nanos("0.02")
    assert result.missing_usage_entries == 0


def test_requested_defaults_price_but_do_not_forge_durable_actual_identity() -> None:
    event = ProviderDone(
        provider="",
        model="",
        requested_provider="openrouter",
        requested_model="requested-model",
        input_tokens=5,
        output_tokens=1,
        cost_source="none",
        model_usage_breakdown=[
            {
                "provider": "",
                "model": "",
                "requested_provider": "openrouter",
                "requested_model": "requested-model",
                "input_tokens": 5,
                "output_tokens": 1,
                "cost_source": "none",
            }
        ],
    )

    result = normalize_provider_usage(
        event,
        default_provider="openrouter",
        default_model="requested-model",
        completed_at_ms=1234,
    )

    assert len(result.items) == 1
    assert result.items[0].provider == ""
    assert result.items[0].model == ""


def test_missing_placeholder_does_not_persist_configured_actual_identity() -> None:
    event = ProviderDone(
        input_tokens=0,
        output_tokens=0,
        model_usage_breakdown=[
            {
                "role": "abandoned_stream_request",
                "provider": "openrouter",
                "model": "requested-model",
                "requested_provider": "openrouter",
                "requested_model": "requested-model",
                "cost_source": "none",
            }
        ],
        usage_missing_count=1,
    )

    result = normalize_provider_usage(
        event,
        default_provider="openrouter",
        default_model="requested-model",
        completed_at_ms=1234,
    )

    assert len(result.items) == 1
    assert result.items[0].provider == ""
    assert result.items[0].model == ""
    assert result.missing_usage_entries == 1


def test_pending_receipt_never_remains_exact_when_price_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "opensquilla.engine.usage_accounting.resolve_model_price",
        lambda *_args, **_kwargs: ResolvedModelPrice(
            entry=None,
            source="test_missing",
        ),
    )
    receipt = ProviderBillingReceipt(
        currency="USD",
        status="pending",
        amount_nanos=None,
        usd_equivalent_nanos=None,
        fx_native_per_usd_nanos=1_000_000_000,
    )
    event = ProviderDone(
        provider="openrouter",
        model="unknown-model",
        input_tokens=5,
        output_tokens=1,
        billed_cost=0.25,
        cost_source="provider_billed",
        billing_receipt=receipt,
    )

    result = normalize_provider_usage(
        event,
        default_provider="openrouter",
        default_model="unknown-model",
        completed_at_ms=1234,
    )

    assert result.billed_cost_nanos == 0
    assert result.estimated_cost_nanos == 0
    assert result.cost_source == "unavailable"
    assert result.items[0].cost_source == "unavailable"


@pytest.mark.parametrize("legacy_source", ["provider_billed", "openrouter_usage"])
@pytest.mark.parametrize(
    "receipt_override",
    [
        {"currency": "usd"},
        {"status": "settled"},
        {"amount_nanos": True},
        {"usd_equivalent_nanos": -1},
        {"fx_native_per_usd_nanos": 0},
        {"amount_nanos": 1 << 63},
        {"usd_equivalent_nanos": 1 << 63},
        {"fx_native_per_usd_nanos": 1 << 63},
        {"schema_version": 2},
        {
            "amount_nanos": 1_000_000_000,
            "usd_equivalent_nanos": 1_000_000_000,
            "fx_native_per_usd_nanos": 2_000_000_000,
        },
    ],
    ids=[
        "currency",
        "status",
        "amount",
        "usd-nanos",
        "fx",
        "amount-overflow",
        "usd-nanos-overflow",
        "fx-overflow",
        "schema-version",
        "inconsistent-nanos",
    ],
)
def test_malformed_receipt_never_falls_back_to_legacy_exact_cost(
    monkeypatch: pytest.MonkeyPatch,
    legacy_source: str,
    receipt_override: dict[str, object],
) -> None:
    monkeypatch.setattr(
        "opensquilla.engine.usage_accounting.resolve_model_price",
        lambda model, provider: ResolvedModelPrice(
            entry=PriceEntry(input_per_m=1.0, output_per_m=2.0),
            source="test",
        ),
    )
    receipt = {
        "currency": "USD",
        "status": "confirmed",
        "amount_nanos": 1_000_000_000,
        "usd_equivalent_nanos": 1_000_000_000,
        "fx_native_per_usd_nanos": 1_000_000_000,
        "schema_version": 1,
        **receipt_override,
    }
    result = normalize_provider_usage(
        ProviderDone(
            provider="openrouter",
            model="priced-model",
            input_tokens=1_000_000,
            output_tokens=0,
            billed_cost=99.0,
            cost_source=legacy_source,
            billing_receipt=receipt,  # type: ignore[arg-type]
        ),
        default_provider="openrouter",
        default_model="priced-model",
        completed_at_ms=1234,
    )

    assert result.billed_cost_nanos == 0
    assert result.estimated_cost_nanos == usd_to_nanos("1.0")
    assert result.cost_source == "opensquilla_estimate"
    assert result.items[0].billing_receipt is None
    assert result.items[0].cost_source == "opensquilla_estimate"


def test_missing_placeholder_is_not_counted_twice_in_usage_ledger() -> None:
    event = ProviderDone(
        input_tokens=10,
        output_tokens=2,
        billed_cost=0.01,
        cost_source="mixed",
        model="fallback-model",
        model_usage_breakdown=[
            {
                "role": "abandoned_stream_request",
                "provider": "openrouter",
                "model": "fallback-model",
                "input_tokens": 0,
                "output_tokens": 0,
                "billed_cost": 0.0,
                "cost_source": "none",
            },
            {
                "role": "fallback_non_stream",
                "provider": "openrouter",
                "model": "fallback-model",
                "input_tokens": 10,
                "output_tokens": 2,
                "billed_cost": 0.01,
                "cost_source": "provider_billed",
            },
        ],
        usage_missing_count=1,
    )

    result = normalize_provider_usage(
        event,
        default_provider="openrouter",
        default_model="fallback-model",
        completed_at_ms=1234,
    )

    assert len(result.items) == 2
    assert result.items[0].cost_source == "unavailable"
    assert result.items[1].cost_source == "provider_billed"
    assert result.missing_usage_entries == 1
    assert result.represented_missing_usage_entries == 1


def test_complete_ensemble_error_preserves_known_items_without_missing_coverage() -> None:
    event = ProviderError(
        message="aggregator was unavailable",
        code="ensemble_aggregator_error",
        model_usage_breakdown=[
            {
                "provider": "p1",
                "model": "m1",
                "input_tokens": 10,
                "output_tokens": 2,
                "billed_cost": 0.25,
                "cost_source": "provider_billed",
            }
        ],
        usage_missing_count=0,
    )

    result = normalize_provider_usage(
        event,
        default_provider="ensemble",
        default_model="aggregator",
        completed_at_ms=1234,
    )

    assert len(result.items) == 1
    assert result.items[0].model == "m1"
    assert result.billed_cost_nanos == usd_to_nanos("0.25")
    assert result.missing_usage_entries == 0


@pytest.mark.asyncio
async def test_partial_ensemble_error_finalizes_outer_call_instead_of_unknown() -> None:
    sink = _RecordingSink()
    scope = UsageAccountingScope(sink=sink, context=_context())

    async def stream() -> AsyncIterator[ProviderError]:
        yield ProviderError(
            message="fallback failed",
            code="ensemble_fallback_incomplete",
            model_usage_breakdown=[
                {
                    "provider": "p1",
                    "model": "m1",
                    "input_tokens": 4,
                    "output_tokens": 2,
                    "billed_cost": 0.1,
                    "cost_source": "provider_billed",
                }
            ],
            usage_missing_count=2,
        )

    with bind_usage_accounting_scope(scope):
        events = [
            event
            async for event in account_provider_stream(
                stream,
                provider="ensemble",
                model="aggregator",
            )
        ]

    assert len(events) == 1
    assert len(sink.finalized) == 1
    assert sink.finalized[0][1].missing_usage_entries == 2
    assert sink.unknown == []


@pytest.mark.asyncio
async def test_complete_ensemble_error_finalizes_outer_call_instead_of_unknown() -> None:
    sink = _RecordingSink()
    scope = UsageAccountingScope(sink=sink, context=_context())

    async def stream() -> AsyncIterator[ProviderError]:
        yield ProviderError(
            message="aggregator was unavailable",
            code="ensemble_aggregator_error",
            model_usage_breakdown=[
                {
                    "provider": "p1",
                    "model": "m1",
                    "input_tokens": 4,
                    "output_tokens": 2,
                    "billed_cost": 0.1,
                    "cost_source": "provider_billed",
                }
            ],
            usage_missing_count=0,
        )

    with bind_usage_accounting_scope(scope):
        events = [
            event
            async for event in account_provider_stream(
                stream,
                provider="ensemble",
                model="aggregator",
            )
        ]

    assert len(events) == 1
    assert len(sink.finalized) == 1
    assert sink.finalized[0][1].missing_usage_entries == 0
    assert sink.unknown == []


@pytest.mark.asyncio
async def test_agent_finalizes_partial_error_with_a_known_receipt() -> None:
    sink = _RecordingSink()
    provider = _SequenceProvider(
        [
            [
                ProviderError(
                    message="aggregator failed",
                    code="ensemble_aggregator_error",
                    model_usage_breakdown=[
                        {
                            "provider": "p1",
                            "model": "m1",
                            "input_tokens": 4,
                            "output_tokens": 2,
                            "billed_cost": 0.1,
                            "cost_source": "provider_billed",
                        }
                    ],
                    usage_missing_count=1,
                )
            ]
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=1, max_provider_retries=0),
        usage_event_sink=sink,
        usage_execution_context=_context(),
    )

    async for _ in agent.run_turn("hello"):
        pass

    assert len(sink.finalized) == 1
    assert sink.finalized[0][1].missing_usage_entries == 1
    assert sink.unknown == []


@pytest.mark.asyncio
async def test_partial_error_missing_count_without_known_rows_preserves_multiplicity() -> None:
    sink = _RecordingSink()
    scope = UsageAccountingScope(sink=sink, context=_context())

    async def stream() -> AsyncIterator[ProviderError]:
        yield ProviderError(
            message="fallback failed before a receipt",
            code="ensemble_fallback_incomplete",
            model_usage_breakdown=[],
            usage_missing_count=2,
        )

    with bind_usage_accounting_scope(scope):
        events = [
            event
            async for event in account_provider_stream(
                stream,
                provider="ensemble",
                model="aggregator",
            )
        ]

    assert len(events) == 1
    assert len(sink.finalized) == 1
    result = sink.finalized[0][1]
    assert result.items == ()
    assert result.missing_usage_entries == 2
    assert sink.unknown == []


def test_runtime_subagent_inherits_sink_with_distinct_execution() -> None:
    sink = _RecordingSink()
    parent = Agent(
        provider=_ErrorProvider(),
        config=AgentConfig(),
        usage_event_sink=sink,
        usage_execution_context=_context(),
    )

    child = parent._make_child_agent(SubagentSpec(task="child"), depth=1)

    assert child._usage_event_sink is sink
    child_context = child._usage_execution_context
    assert child_context is not None
    assert child_context.execution_id != "turn-1"
    assert child_context.parent_turn_id == "turn-1"
    assert child_context.session_id == "session-1"
    assert child_context.session_epoch == 7
    assert child_context.run_kind == "subagent"


@pytest.mark.asyncio
async def test_direct_meta_llm_helper_records_usage_with_parent_attribution() -> None:
    sink = _RecordingSink()
    provider = _DoneProvider(sink)
    chat = make_llm_chat_from_provider(
        provider=provider,
        base_config=AgentConfig(provider_id="fake", model_id="model-a"),
        usage_event_sink=sink,
        usage_execution_context=_context(),
    )

    result = await chat("system", "user")

    assert result == "ok"
    assert len(sink.started) == 1
    call = sink.started[0]
    assert call.execution_id != "turn-1"
    assert call.parent_turn_id == "turn-1"
    assert call.session_id == "session-1"
    assert call.session_epoch == 7
    assert call.run_kind == "meta_llm"
    assert len(sink.finalized) == 1
    assert sink.unknown == []


@pytest.mark.asyncio
async def test_selector_fallback_accounts_each_physical_leg_without_outer_duplicate() -> None:
    sink = _RecordingSink()
    fallback = _PhysicalLegProvider(
        "anthropic",
        [
            ProviderText(text="fallback"),
            ProviderDone(
                input_tokens=7,
                output_tokens=2,
                billed_cost=0.25,
                cost_source="provider_billed",
                model="fallback-model",
            ),
        ],
    )
    primary = _PhysicalLegProvider(
        "openai",
        [ProviderError(message="rate limited", code="429")],
    )
    wrapper = _SelectorFallbackProvider(primary, _FallbackSelector(fallback))
    tracker = _RecordingTracker()
    agent = Agent(
        provider=wrapper,
        config=AgentConfig(
            max_iterations=1,
            max_provider_retries=0,
            provider_id="openai",
            model_id="primary-model",
        ),
        usage_tracker=tracker,
        session_key="agent:main:test",
        usage_event_sink=sink,
        usage_execution_context=_context(),
    )

    async for _ in agent.run_turn("hello"):
        pass

    assert primary.calls == 1
    assert fallback.calls == 1
    assert [(call.call_index, call.provider, call.model) for call in sink.started] == [
        (1, "openai", "primary-model"),
        (2, "anthropic", "fallback-model"),
    ]
    assert [(call.call_index, reason) for call, reason in sink.unknown] == [
        (1, "provider_error:429")
    ]
    assert [call.call_index for call, _ in sink.finalized] == [2]
    assert len(sink.started) == 2  # no Agent-level wrapper envelope
    assert tracker.rows[0][1]["provider"] == "anthropic"
    assert tracker.rows[0][1]["model_id"] == "fallback-model"


@pytest.mark.asyncio
async def test_selector_wrapper_without_ledger_scope_is_streaming_compatible() -> None:
    fallback = _PhysicalLegProvider(
        "anthropic",
        [ProviderText(text="ok"), ProviderDone(model="fallback-model")],
    )
    primary = _PhysicalLegProvider(
        "openai",
        [ProviderError(message="rate limited", code="429")],
    )
    wrapper = _SelectorFallbackProvider(primary, _FallbackSelector(fallback))

    events = [event async for event in wrapper.chat([])]

    assert [getattr(event, "kind", "") for event in events] == ["text_delta", "done"]
    assert primary.calls == fallback.calls == 1


@pytest.mark.asyncio
async def test_meta_helper_with_selector_records_only_physical_legs() -> None:
    sink = _RecordingSink()
    fallback = _PhysicalLegProvider(
        "anthropic",
        [ProviderText(text="ok"), ProviderDone(model="fallback-model")],
    )
    primary = _PhysicalLegProvider(
        "openai",
        [ProviderError(message="rate limited", code="429")],
    )
    wrapper = _SelectorFallbackProvider(primary, _FallbackSelector(fallback))
    chat = make_llm_chat_from_provider(
        provider=wrapper,
        base_config=AgentConfig(provider_id="openai", model_id="primary-model"),
        usage_event_sink=sink,
        usage_execution_context=_context(),
    )

    assert await chat("system", "user") == "ok"

    assert [(call.call_index, call.provider, call.model) for call in sink.started] == [
        (1, "openai", "primary-model"),
        (2, "anthropic", "fallback-model"),
    ]
    assert {call.execution_id for call in sink.started} != {"turn-1"}
    assert all(call.run_kind == "meta_llm" for call in sink.started)
    assert len(sink.finalized) == 1
    assert len(sink.unknown) == 1


@pytest.mark.asyncio
async def test_shared_turn_scope_keeps_helper_and_agent_call_indices_unique() -> None:
    sink = _RecordingSink()
    context = _context()
    scope = UsageAccountingScope(sink=sink, context=context)
    helper_provider = _SequenceProvider([[ProviderDone(model="gate-model")]])
    agent_provider = _SequenceProvider(
        [[ProviderText(text="ok"), ProviderDone(model="agent-model")]]
    )
    agent = Agent(
        provider=agent_provider,
        config=AgentConfig(max_iterations=1, model_id="agent-model"),
        usage_event_sink=sink,
        usage_execution_context=context,
    )

    with bind_usage_accounting_scope(scope):
        _ = [
            event
            async for event in account_provider_stream(
                lambda: helper_provider.chat([]),
                provider="gate-provider",
                model="gate-model",
            )
        ]
        async for _ in agent.run_turn("hello"):
            pass

    assert [call.call_index for call in sink.started] == [1, 2]
    assert len({call.event_id for call in sink.started}) == 2


@pytest.mark.asyncio
async def test_turn_runner_preserves_retryable_ledger_start_error_code() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    session_key = "agent:main:usage-start-failure"
    await manager.create(session_key)
    sink = _UnavailableSink()
    provider = _SequenceProvider([[ProviderDone()]])
    runner = TurnRunner(
        provider_selector=_SingleProviderSelector(provider),
        session_manager=manager,
        usage_event_sink=sink,
    )
    try:
        events = [
            event
            async for event in runner.run(
                "hello",
                session_key,
                tool_context=ToolContext(
                    session_key=session_key,
                    caller_kind=CallerKind.CLI,
                ),
                history_has_persisted_user=False,
                no_memory_capture=True,
            )
        ]
    finally:
        await storage.close()

    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert len(errors) == 1
    assert errors[0].code == UsageAccountingUnavailableError.code
    assert provider.calls == 0
