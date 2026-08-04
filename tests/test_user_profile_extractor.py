"""Rendering, batching, and the streaming provider call.

Rendering preserves the transcript entries and truncates head+tail so a long
session still fits a bounded prompt; batching drops a session that alone blows
the budget rather than showing the model a half-session it will still cite. The
streaming call mirrors the task analyzer's loop and must fail open — a batch
that errors, times out, or ends early becomes a failed batch, never a raise.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from opensquilla.engine.usage_accounting import (
    UsageAccountingScope,
    UsageCallResult,
    UsageCallStart,
    UsageExecutionContext,
    account_provider_stream,
    bind_usage_accounting_scope,
)
from opensquilla.provider.types import DoneEvent, ErrorEvent, TextDeltaEvent
from opensquilla.squilla_router.user_profile import extractor
from opensquilla.squilla_router.user_profile.prompts import SYSTEM_PROMPT, build_batch_prompt
from opensquilla.squilla_router.user_profile.schema import SessionTranscript


@dataclass
class _Row:
    role: str
    content: str | None


def test_render_preserves_all_nonempty_transcript_roles() -> None:
    rows = [
        _Row("system", "you are helpful"),
        _Row("user", "write a function"),
        _Row("tool", "{...}"),
        _Row("assistant", "here you go"),
    ]
    text = extractor.render_transcript("s1", rows, per_session_max_chars=10_000).text
    assert "user: write a function" in text
    assert "assistant: here you go" in text
    assert "system: you are helpful" in text
    assert "tool: {...}" in text


def test_render_truncates_head_and_tail_with_a_marker() -> None:
    rows = [_Row("user", "A" * 500 + "B" * 500)]
    rendered = extractor.render_transcript("s1", rows, per_session_max_chars=120)
    assert len(rendered.text) <= 120
    assert "truncated" in rendered.text


def test_batching_groups_by_size() -> None:
    sessions = [SessionTranscript(f"s{i}", "x" * 10) for i in range(25)]
    batches = extractor.batch_sessions(sessions, batch_size=10, batch_input_max_chars=100_000)
    assert [len(b) for b in batches] == [10, 10, 5]


def test_a_session_over_the_batch_budget_is_dropped_whole() -> None:
    sessions = [
        SessionTranscript("small", "x" * 10),
        SessionTranscript("huge", "x" * 1000),
    ]
    small_budget = len(SYSTEM_PROMPT) + len(build_batch_prompt([sessions[0]]))
    batches = extractor.batch_sessions(
        sessions, batch_size=10, batch_input_max_chars=small_budget
    )
    kept = [s.session_id for b in batches for s in b]
    assert kept == ["small"]  # huge dropped, not split


def test_char_budget_forces_a_new_batch() -> None:
    sessions = [SessionTranscript(f"s{i}", "x" * 60) for i in range(3)]
    one_fits = len(SYSTEM_PROMPT) + len(build_batch_prompt([sessions[0]]))
    batches = extractor.batch_sessions(sessions, batch_size=10, batch_input_max_chars=one_fits)
    # The final serialized request budget includes prompt vocabulary and JSON overhead.
    assert [len(b) for b in batches] == [1, 1, 1]


def test_batch_budget_counts_ensure_ascii_json_overhead() -> None:
    sessions = [
        SessionTranscript("ascii", "x" * 10),
        SessionTranscript("cjk", "你好" * 10),
    ]
    ascii_only = len(SYSTEM_PROMPT) + len(build_batch_prompt([sessions[0]]))
    both = len(SYSTEM_PROMPT) + len(build_batch_prompt(sessions))
    assert both > ascii_only + len(sessions[1].text)
    batches = extractor.batch_sessions(
        sessions,
        batch_size=10,
        batch_input_max_chars=ascii_only,
    )
    assert [[session.session_id for session in batch] for batch in batches] == [["ascii"]]


class _FakeProvider:
    """A provider whose ``chat`` yields a scripted event stream."""

    def __init__(self, events, *, delay: float = 0.0) -> None:
        self._events = events
        self._delay = delay
        self.closed = False
        self.calls = 0

    def chat(self, messages, tools=None, config=None):  # noqa: ANN001
        self.calls += 1
        provider = self

        async def _stream() -> AsyncIterator[object]:
            try:
                for event in provider._events:
                    if provider._delay:
                        await asyncio.sleep(provider._delay)
                    yield event
            finally:
                provider.closed = True

        return _stream()


@dataclass
class _RecordingSink:
    started: list[UsageCallStart] = field(default_factory=list)
    finalized: list[tuple[UsageCallStart, UsageCallResult]] = field(default_factory=list)
    unknown: list[tuple[UsageCallStart, str]] = field(default_factory=list)
    fail_start: bool = False

    async def start(self, call: UsageCallStart) -> None:
        if self.fail_start:
            raise RuntimeError("ledger unavailable")
        self.started.append(call)

    async def finalize(self, call: UsageCallStart, result: UsageCallResult) -> None:
        self.finalized.append((call, result))

    async def mark_unknown(self, call: UsageCallStart, reason: str) -> None:
        self.unknown.append((call, reason))


def _scope(sink: _RecordingSink) -> UsageAccountingScope:
    return UsageAccountingScope(
        sink=sink,
        context=UsageExecutionContext(
            execution_id="profile-execution",
            agent_run_id="profile-run",
            turn_id="profile-turn",
            session_id="profile-session",
            session_epoch=0,
            agent_id="main",
            run_kind="user_profile_generation",
        ),
    )


def _batch() -> list[SessionTranscript]:
    return [SessionTranscript("s1", "user: hi")]


def _stream_factory(
    *,
    provider,
    user_prompt: str,
    system_prompt: str,
    max_output_tokens: int,
    temperature: float,
    timeout: float,
):
    del user_prompt, system_prompt, max_output_tokens, temperature, timeout
    return provider.chat([], tools=None, config=None)


def _accounted_stream_factory(
    *,
    provider,
    user_prompt: str,
    system_prompt: str,
    max_output_tokens: int,
    temperature: float,
    timeout: float,
):
    del user_prompt, system_prompt, max_output_tokens, temperature, timeout
    return account_provider_stream(
        lambda: provider.chat([], tools=None, config=None),
        provider="profile-provider",
        model="profile-model",
    )


_GOOD_JSON = (
    '{"session_labels":[{"session_id":"s1","capability":"reasoning",'
    '"confidence":0.8}],"quality_latency_tradeoff":{"value":"quality_first",'
    '"confidence":0.7,"session_ids":["s1"]},"cost_sensitivity":{"value":"unknown",'
    '"confidence":0.0},"model_mentions":[]}'
)


async def test_a_clean_stream_parses_into_an_ok_batch() -> None:
    provider = _FakeProvider([TextDeltaEvent(text=_GOOD_JSON), DoneEvent()])
    analysis = await extractor.extract_batch(
        provider=provider,
        stream_factory=_stream_factory,
        batch=_batch(),
        max_output_tokens=500,
        temperature=0.0,
        timeout=5.0,
        response_max_chars=48_000,
    )
    assert analysis.ok
    assert analysis.session_labels[0].capability == "reasoning"
    assert provider.closed  # stream always closed


async def test_an_error_event_fails_the_batch_open() -> None:
    provider = _FakeProvider([TextDeltaEvent(text="{"), ErrorEvent(message="boom", code="500")])
    analysis = await extractor.extract_batch(
        provider=provider,
        stream_factory=_stream_factory,
        batch=_batch(),
        max_output_tokens=500,
        temperature=0.0,
        timeout=5.0,
        response_max_chars=48_000,
    )
    assert analysis.ok is False
    assert analysis.session_ids == ("s1",)


async def test_a_stream_ending_before_done_fails_open() -> None:
    provider = _FakeProvider([TextDeltaEvent(text=_GOOD_JSON)])  # no DoneEvent
    analysis = await extractor.extract_batch(
        provider=provider,
        stream_factory=_stream_factory,
        batch=_batch(),
        max_output_tokens=500,
        temperature=0.0,
        timeout=5.0,
        response_max_chars=48_000,
    )
    assert analysis.ok is False


async def test_a_timeout_fails_open() -> None:
    provider = _FakeProvider([TextDeltaEvent(text=_GOOD_JSON), DoneEvent()], delay=0.2)
    analysis = await extractor.extract_batch(
        provider=provider,
        stream_factory=_stream_factory,
        batch=_batch(),
        max_output_tokens=500,
        temperature=0.0,
        timeout=0.01,
        response_max_chars=48_000,
    )
    assert analysis.ok is False


async def test_an_oversized_response_fails_open() -> None:
    provider = _FakeProvider([TextDeltaEvent(text="x" * 100), DoneEvent()])
    analysis = await extractor.extract_batch(
        provider=provider,
        stream_factory=_stream_factory,
        batch=_batch(),
        max_output_tokens=500,
        temperature=0.0,
        timeout=5.0,
        response_max_chars=10,
    )
    assert analysis.ok is False


async def test_accounted_stream_start_happens_before_provider_request() -> None:
    sink = _RecordingSink()

    class _StartOrderProvider(_FakeProvider):
        def chat(self, messages, tools=None, config=None):  # noqa: ANN001
            assert len(sink.started) == 1
            return super().chat(messages, tools=tools, config=config)

    provider = _StartOrderProvider(
        [TextDeltaEvent(text=_GOOD_JSON), DoneEvent(input_tokens=3, output_tokens=2)]
    )

    with bind_usage_accounting_scope(_scope(sink)):
        analysis = await extractor.extract_batch(
            provider=provider,
            stream_factory=_accounted_stream_factory,
            batch=_batch(),
            max_output_tokens=500,
            temperature=0.0,
            timeout=5.0,
            response_max_chars=48_000,
        )

    assert analysis.ok
    assert len(sink.started) == 1
    assert len(sink.finalized) == 1
    assert sink.unknown == []


async def test_accounted_stream_start_failure_sends_no_provider_request() -> None:
    sink = _RecordingSink(fail_start=True)
    provider = _FakeProvider([TextDeltaEvent(text=_GOOD_JSON), DoneEvent()])

    with bind_usage_accounting_scope(_scope(sink)):
        analysis = await extractor.extract_batch(
            provider=provider,
            stream_factory=_accounted_stream_factory,
            batch=_batch(),
            max_output_tokens=500,
            temperature=0.0,
            timeout=5.0,
            response_max_chars=48_000,
        )

    assert analysis.ok is False
    assert sink.started == []
    assert sink.finalized == []
    assert sink.unknown == []
    assert provider.calls == 0
    assert provider.closed is False


async def test_accounted_stream_error_event_marks_unknown() -> None:
    sink = _RecordingSink()
    provider = _FakeProvider([ErrorEvent(message="boom", code="500")])

    with bind_usage_accounting_scope(_scope(sink)):
        analysis = await extractor.extract_batch(
            provider=provider,
            stream_factory=_accounted_stream_factory,
            batch=_batch(),
            max_output_tokens=500,
            temperature=0.0,
            timeout=5.0,
            response_max_chars=48_000,
        )

    assert analysis.ok is False
    assert sink.finalized == []
    assert [(call.event_id, reason) for call, reason in sink.unknown] == [
        (sink.started[0].event_id, "provider_error:500")
    ]


async def test_accounted_stream_early_end_marks_unknown() -> None:
    sink = _RecordingSink()
    provider = _FakeProvider([TextDeltaEvent(text=_GOOD_JSON)])

    with bind_usage_accounting_scope(_scope(sink)):
        analysis = await extractor.extract_batch(
            provider=provider,
            stream_factory=_accounted_stream_factory,
            batch=_batch(),
            max_output_tokens=500,
            temperature=0.0,
            timeout=5.0,
            response_max_chars=48_000,
        )

    assert analysis.ok is False
    assert sink.finalized == []
    assert [(call.event_id, reason) for call, reason in sink.unknown] == [
        (sink.started[0].event_id, "provider_stream_ended_without_usage")
    ]


async def test_accounted_stream_timeout_marks_unknown() -> None:
    sink = _RecordingSink()
    provider = _FakeProvider([TextDeltaEvent(text=_GOOD_JSON), DoneEvent()], delay=0.2)

    with bind_usage_accounting_scope(_scope(sink)):
        analysis = await extractor.extract_batch(
            provider=provider,
            stream_factory=_accounted_stream_factory,
            batch=_batch(),
            max_output_tokens=500,
            temperature=0.0,
            timeout=0.01,
            response_max_chars=48_000,
        )

    assert analysis.ok is False
    assert sink.finalized == []
    assert [(call.event_id, reason) for call, reason in sink.unknown] == [
        (sink.started[0].event_id, "cancelled")
    ]


class _BlockingProvider:
    def __init__(self) -> None:
        self.entered = asyncio.Event()

    def chat(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        del args, kwargs
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        self.entered.set()
        await asyncio.Event().wait()
        if False:  # pragma: no cover - preserve async generator shape
            yield None


async def test_accounted_stream_cancellation_propagates_and_marks_unknown() -> None:
    sink = _RecordingSink()
    provider = _BlockingProvider()

    async def run() -> None:
        with bind_usage_accounting_scope(_scope(sink)):
            await extractor.extract_batch(
                provider=provider,
                stream_factory=_accounted_stream_factory,
                batch=_batch(),
                max_output_tokens=500,
                temperature=0.0,
                timeout=5.0,
                response_max_chars=48_000,
            )

    task = asyncio.create_task(run())
    await asyncio.wait_for(provider.entered.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert sink.finalized == []
    assert [(call.event_id, reason) for call, reason in sink.unknown] == [
        (sink.started[0].event_id, "cancelled")
    ]


class _HangingCloseStream:
    def __init__(self) -> None:
        self._events = iter([TextDeltaEvent(text=_GOOD_JSON), DoneEvent()])
        self.close_entered = False

    def __aiter__(self) -> _HangingCloseStream:
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._events)
        except StopIteration as exc:
            raise StopAsyncIteration from exc

    async def aclose(self) -> None:
        self.close_entered = True
        await asyncio.Event().wait()


async def test_hanging_aclose_is_bounded_by_batch_timeout() -> None:
    stream = _HangingCloseStream()

    def stream_factory(**kwargs) -> _HangingCloseStream:  # noqa: ANN003
        del kwargs
        return stream

    analysis = await extractor.extract_batch(
        provider=object(),
        stream_factory=stream_factory,
        batch=_batch(),
        max_output_tokens=500,
        temperature=0.0,
        timeout=0.01,
        response_max_chars=48_000,
    )

    assert analysis.ok is False
    assert stream.close_entered is True


class _FailingCloseStream(_HangingCloseStream):
    async def aclose(self) -> None:
        raise RuntimeError("close failed")


async def test_aclose_failure_fails_the_batch() -> None:
    stream = _FailingCloseStream()

    def stream_factory(**kwargs) -> _FailingCloseStream:  # noqa: ANN003
        del kwargs
        return stream

    analysis = await extractor.extract_batch(
        provider=object(),
        stream_factory=stream_factory,
        batch=_batch(),
        max_output_tokens=500,
        temperature=0.0,
        timeout=5.0,
        response_max_chars=48_000,
    )

    assert analysis.ok is False
