"""Opt-in levers: placeholder-offense escalation + pre-deadline wrap-up.

Covers OPENSQUILLA_PLACEHOLDER_ESCALATION_THRESHOLD and
OPENSQUILLA_DEADLINE_WRAPUP_MARGIN_SECONDS (both off by default). Motivation:
in long unattended runs, models can keep re-issuing tool calls that reference
compacted placeholders despite delivered per-call feedback, and deadline-capped
runs can be cut off mid-exploration with no wrap-up attempt.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig, AgentState, ThinkingLevel, ToolResult
from opensquilla.engine.agent import (
    _DEADLINE_WRAPUP_DIRECTIVE_TEMPLATE,
    _INVALID_PROVIDER_CONTEXT_ARGUMENTS_KEY,
    _PLACEHOLDER_ESCALATION_DIRECTIVE,
)
from opensquilla.provider import (
    ChatConfig,
    Message,
    ModelCapabilities,
    ProviderHeartbeatEvent,
    ToolDefinition,
    ToolInputSchema,
)
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import ErrorEvent as ProviderError
from opensquilla.provider import ReasoningDeltaEvent as ProviderReasoning
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.provider import ToolUseEndEvent as ProviderToolUseEnd
from opensquilla.provider import ToolUseStartEvent as ProviderToolUseStart
from opensquilla.provider.types import (
    EnsembleProgressEvent as ProviderEnsembleProgress,
)

_WRAPUP_PREFIX = _DEADLINE_WRAPUP_DIRECTIVE_TEMPLATE.split("{minutes}", maxsplit=1)[0]


class _SequenceProvider:
    provider_name = "fake"

    def __init__(self, streams: list[list[Any]]) -> None:
        self.streams = streams
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        index = len(self.calls)
        self.calls.append({"messages": messages, "tools": tools, "config": config})
        events = self.streams[index] if index < len(self.streams) else self.streams[-1]
        return self._stream(events)

    async def _stream(self, events: list[Any]) -> AsyncIterator[Any]:
        for event in events:
            if isinstance(event, float):
                # Wall-clock gap between stream events, for tests that need a
                # stream to span a deadline margin mid-flight.
                await asyncio.sleep(event)
                continue
            yield event

    async def list_models(self) -> list[Any]:
        return []


class _RecordingTurnCallLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []

    def write(self, kind: str, payload: dict[str, Any]) -> None:
        self.records.append((kind, payload))


class _CloseTrackingStream:
    def __init__(self, events: list[Any]) -> None:
        self.events = events
        self.index = 0
        self.closed = False
        self.close_calls = 0

    def __aiter__(self) -> _CloseTrackingStream:
        return self

    async def __anext__(self) -> Any:
        while not self.closed and self.index < len(self.events):
            event = self.events[self.index]
            self.index += 1
            if isinstance(event, float):
                await asyncio.sleep(event)
                continue
            return event
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.close_calls += 1
        self.closed = True


class _CloseTrackingProvider(_SequenceProvider):
    def __init__(self, streams: list[list[Any]]) -> None:
        super().__init__(streams)
        self.first_stream: _CloseTrackingStream | None = None
        self.first_closed_before_second_call = False

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        index = len(self.calls)
        self.calls.append({"messages": messages, "tools": tools, "config": config})
        events = self.streams[index] if index < len(self.streams) else self.streams[-1]
        if index == 0:
            self.first_stream = _CloseTrackingStream(events)
            return self.first_stream
        self.first_closed_before_second_call = bool(
            self.first_stream is not None and self.first_stream.closed
        )
        return self._stream(events)


def _placeholder_tool_call(tool_use_id: str) -> list[Any]:
    return [
        ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="echo"),
        ProviderToolUseEnd(
            tool_use_id=tool_use_id,
            tool_name="echo",
            arguments={_INVALID_PROVIDER_CONTEXT_ARGUMENTS_KEY: True},
        ),
        ProviderDone(stop_reason="tool_use", input_tokens=3, output_tokens=1),
    ]


def _echo_tool_call(tool_use_id: str) -> list[Any]:
    return [
        ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="echo"),
        ProviderToolUseEnd(
            tool_use_id=tool_use_id,
            tool_name="echo",
            arguments={"value": "hi"},
        ),
        ProviderDone(stop_reason="tool_use", input_tokens=3, output_tokens=1),
    ]


def _two_echo_tool_calls() -> list[Any]:
    return [
        ProviderToolUseStart(tool_use_id="slow-a", tool_name="echo"),
        ProviderToolUseEnd(
            tool_use_id="slow-a",
            tool_name="echo",
            arguments={"value": "a"},
        ),
        ProviderToolUseStart(tool_use_id="queued-b", tool_name="echo"),
        ProviderToolUseEnd(
            tool_use_id="queued-b",
            tool_name="echo",
            arguments={"value": "b"},
        ),
        ProviderDone(stop_reason="tool_use", input_tokens=3, output_tokens=1),
    ]


def _named_tool_call(tool_use_id: str, tool_name: str) -> list[Any]:
    return [
        ProviderToolUseStart(tool_use_id=tool_use_id, tool_name=tool_name),
        ProviderToolUseEnd(
            tool_use_id=tool_use_id,
            tool_name=tool_name,
            arguments={"query": "current evidence"},
        ),
        ProviderDone(stop_reason="tool_use", input_tokens=3, output_tokens=1),
    ]


def _empty_response() -> list[Any]:
    return [ProviderDone(stop_reason="stop", input_tokens=5, output_tokens=0)]


def _final_text() -> list[Any]:
    return [
        ProviderText(text="done"),
        ProviderDone(stop_reason="stop", input_tokens=5, output_tokens=1),
    ]


def _echo_agent(provider: _SequenceProvider, config: AgentConfig) -> Agent:
    async def tool_handler(call: object) -> ToolResult:
        return ToolResult(
            tool_use_id=getattr(call, "tool_use_id"),
            tool_name=getattr(call, "tool_name"),
            content="tool ok",
        )

    return Agent(
        provider=provider,
        config=config,
        tool_definitions=[
            ToolDefinition(
                name="echo",
                description="Echo.",
                input_schema=ToolInputSchema(
                    properties={"value": {"type": "string"}},
                    required=["value"],
                ),
            )
        ],
        tool_handler=tool_handler,
    )


def _retrieval_agent(provider: _SequenceProvider, config: AgentConfig) -> Agent:
    async def tool_handler(call: object) -> ToolResult:
        return ToolResult(
            tool_use_id=getattr(call, "tool_use_id"),
            tool_name=getattr(call, "tool_name"),
            content="retrieval ok",
        )

    return Agent(
        provider=provider,
        config=config,
        tool_definitions=[
            ToolDefinition(
                name=name,
                description="Retrieve web evidence.",
                input_schema=ToolInputSchema(
                    properties={"query": {"type": "string"}},
                    required=["query"],
                ),
            )
            for name in ("web_search", "web_fetch")
        ],
        tool_handler=tool_handler,
    )


def _user_texts(messages: list[Message]) -> list[str]:
    return [
        message.content
        for message in messages
        if message.role == "user" and isinstance(message.content, str)
    ]


async def _collect_agent_events(agent: Agent, message: str) -> list[Any]:
    return [event async for event in agent.run_turn(message)]


@pytest.mark.asyncio
async def test_agent_does_not_forge_actual_identity_from_requested_config() -> None:
    provider = _SequenceProvider([_final_text()])
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            model_id="requested-model",
            provider_id="requested-provider",
        ),
    )

    events = await _collect_agent_events(agent, "answer")
    done = next(event for event in reversed(events) if event.kind == "done")

    assert done.model == ""
    assert done.provider == ""
    assert done.requested_model == "requested-model"
    assert done.requested_provider == "requested-provider"
    assert done.model_usage_breakdown == []


@pytest.mark.asyncio
async def test_agent_terminal_identity_does_not_leak_from_prior_tool_call() -> None:
    first = _echo_tool_call("use-1")
    first[-1] = ProviderDone(
        stop_reason="tool_use",
        input_tokens=3,
        output_tokens=1,
        model="prior-actual-model",
        provider="prior-actual-provider",
    )
    provider = _SequenceProvider([first, _final_text()])
    agent = _echo_agent(
        provider,
        AgentConfig(
            model_id="requested-model",
            provider_id="requested-provider",
        ),
    )

    events = await _collect_agent_events(agent, "use the tool")
    done = next(event for event in reversed(events) if event.kind == "done")

    assert len(provider.calls) == 2
    assert done.model == ""
    assert done.provider == ""
    assert done.requested_model == "requested-model"
    assert done.requested_provider == "requested-provider"


@pytest.mark.asyncio
async def test_agent_preserves_terminal_provider_requested_identity() -> None:
    provider = _SequenceProvider(
        [
            [
                ProviderText(text="done"),
                ProviderDone(
                    stop_reason="stop",
                    input_tokens=5,
                    output_tokens=1,
                    model="aggregator-actual",
                    provider="openrouter",
                    requested_model="aggregator-requested",
                    requested_provider="openrouter",
                ),
            ]
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            model_id="ensemble-profile",
            provider_id="ensemble",
        ),
    )

    events = await _collect_agent_events(agent, "answer")
    done = next(event for event in reversed(events) if event.kind == "done")

    assert done.model == "aggregator-actual"
    assert done.provider == "openrouter"
    assert done.requested_model == "aggregator-requested"
    assert done.requested_provider == "openrouter"


@pytest.mark.asyncio
async def test_placeholder_escalation_fires_at_threshold() -> None:
    provider = _SequenceProvider(
        [
            _placeholder_tool_call("blocked-1"),
            _placeholder_tool_call("blocked-2"),
            _final_text(),
        ]
    )
    agent = _echo_agent(
        provider,
        AgentConfig(
            max_iterations=5,
            placeholder_escalation_threshold=2,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert len(provider.calls) == 3
    # Below threshold after offense 1: no escalation in call 2.
    assert _PLACEHOLDER_ESCALATION_DIRECTIVE not in _user_texts(provider.calls[1]["messages"])
    # At threshold after offense 2: escalation delivered in call 3.
    assert _PLACEHOLDER_ESCALATION_DIRECTIVE in _user_texts(provider.calls[2]["messages"])


@pytest.mark.asyncio
async def test_placeholder_escalation_default_off() -> None:
    provider = _SequenceProvider(
        [
            _placeholder_tool_call("blocked-1"),
            _placeholder_tool_call("blocked-2"),
            _final_text(),
        ]
    )
    agent = _echo_agent(
        provider,
        AgentConfig(
            max_iterations=5,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    for call in provider.calls:
        assert _PLACEHOLDER_ESCALATION_DIRECTIVE not in _user_texts(call["messages"])


@pytest.mark.asyncio
async def test_cancellation_resistant_tool_stops_batch_and_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _SequenceProvider([_two_echo_tool_calls(), _final_text()])
    release = asyncio.Event()
    slow_stopped = asyncio.Event()
    queued_started = asyncio.Event()

    async def tool_handler(call: object) -> ToolResult:
        value = getattr(call, "arguments").get("value")
        if value == "b":
            queued_started.set()
            return ToolResult(
                tool_use_id=getattr(call, "tool_use_id"),
                tool_name=getattr(call, "tool_name"),
                content="unexpected queued execution",
            )
        try:
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    # Simulate a buggy/foreign tool that does not cooperate
                    # with task cancellation.
                    continue
        finally:
            slow_stopped.set()
        return ToolResult(
            tool_use_id=getattr(call, "tool_use_id"),
            tool_name=getattr(call, "tool_name"),
            content="late",
        )

    monkeypatch.setattr(
        "opensquilla.engine.agent._TOOL_TASK_CLEANUP_TIMEOUT_SECONDS",
        0.01,
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=3,
            timeout=1.0,
            iteration_timeout=1.0,
            tool_timeout=0.01,
            max_safe_tool_concurrency=1,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
        tool_definitions=[
            ToolDefinition(
                name="echo",
                description="Echo.",
                input_schema=ToolInputSchema(
                    properties={"value": {"type": "string"}},
                    required=["value"],
                ),
            )
        ],
        tool_handler=tool_handler,
    )

    try:
        events = await asyncio.wait_for(
            _collect_agent_events(agent, "run tools"),
            timeout=0.3,
        )
    finally:
        release.set()
    await asyncio.wait_for(slow_stopped.wait(), timeout=0.3)

    assert queued_started.is_set() is False
    assert len(provider.calls) == 1
    assert any(
        getattr(event, "code", "") == "tool_stream_close_timeout"
        for event in events
    )
    # A terminal tool-cleanup error must not advance the agent to its normal
    # successful DONE state or trigger another provider call.  The engine may
    # still emit an empty accounting-only DoneEvent after the error so the
    # already-consumed provider receipt is not discarded.
    assert not any(
        getattr(event, "kind", "") == "state_change"
        and getattr(event, "to_state", None) == AgentState.DONE
        for event in events
    )
    accounting_done = [
        event for event in events if getattr(event, "kind", "") == "done"
    ]
    assert all(not str(getattr(event, "text", "") or "").strip() for event in accounting_done)


@pytest.mark.asyncio
async def test_repeated_turn_cancel_keeps_tool_cleanup_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _SequenceProvider([_echo_tool_call("slow"), _final_text()])
    release = asyncio.Event()
    tool_started = asyncio.Event()
    cancellation_seen = asyncio.Event()
    tool_stopped = asyncio.Event()

    async def tool_handler(call: object) -> ToolResult:
        tool_started.set()
        try:
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancellation_seen.set()
                    continue
        finally:
            tool_stopped.set()
        return ToolResult(
            tool_use_id=getattr(call, "tool_use_id"),
            tool_name=getattr(call, "tool_name"),
            content="late",
        )

    monkeypatch.setattr(
        "opensquilla.engine.agent._TOOL_TASK_CLEANUP_TIMEOUT_SECONDS",
        1.0,
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=3,
            timeout=5.0,
            iteration_timeout=5.0,
            tool_timeout=5.0,
        ),
        tool_definitions=[
            ToolDefinition(
                name="echo",
                description="Echo.",
                input_schema=ToolInputSchema(
                    properties={"value": {"type": "string"}},
                    required=["value"],
                ),
            )
        ],
        tool_handler=tool_handler,
    )
    turn_task = asyncio.create_task(_collect_agent_events(agent, "run tool"))

    try:
        await asyncio.wait_for(tool_started.wait(), timeout=0.2)
        turn_task.cancel()
        await asyncio.wait_for(cancellation_seen.wait(), timeout=0.2)
        # Interrupt the outer batch-cleanup wait while both the wrapper and
        # its physical tool task are still cancellation-resistant.
        turn_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(turn_task, timeout=0.2)

        assert len(agent._pending_cleanup_tasks) >= 2
        retry_events = await asyncio.wait_for(
            _collect_agent_events(agent, "second"),
            timeout=0.2,
        )
        assert any(
            getattr(event, "code", "") == "agent_cleanup_in_progress"
            for event in retry_events
        )
        assert len(provider.calls) == 1
    finally:
        release.set()
    await asyncio.wait_for(tool_stopped.wait(), timeout=0.3)


@pytest.mark.asyncio
async def test_repeated_turn_cancel_keeps_inline_meta_producer_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _SequenceProvider(
        [
            [
                ProviderToolUseStart(
                    tool_use_id="meta-slow",
                    tool_name="meta_invoke",
                ),
                ProviderToolUseEnd(
                    tool_use_id="meta-slow",
                    tool_name="meta_invoke",
                    arguments={"name": "slow-meta"},
                ),
                ProviderDone(stop_reason="tool_use", input_tokens=3, output_tokens=1),
            ],
            _final_text(),
        ]
    )
    release = asyncio.Event()
    producer_started = asyncio.Event()
    cancellation_seen = asyncio.Event()
    producer_stopped = asyncio.Event()

    async def cancellation_resistant_meta_stream(
        self: Agent,
        tc: object,
        tool_context: object,
    ) -> AsyncIterator[Any]:
        del self, tool_context
        producer_started.set()
        try:
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancellation_seen.set()
                    continue
        finally:
            producer_stopped.set()
        yield ToolResult(
            tool_use_id=getattr(tc, "tool_use_id"),
            tool_name="meta_invoke",
            content="late meta result",
        )

    monkeypatch.setattr(
        Agent,
        "_run_one_streaming",
        cancellation_resistant_meta_stream,
    )
    monkeypatch.setattr(
        "opensquilla.engine.agent._TOOL_TASK_CLEANUP_TIMEOUT_SECONDS",
        1.0,
    )

    async def unused_handler(call: object) -> ToolResult:
        return ToolResult(
            tool_use_id=getattr(call, "tool_use_id"),
            tool_name=getattr(call, "tool_name"),
            content="unused",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=3,
            timeout=5.0,
            iteration_timeout=5.0,
            tool_timeout=5.0,
        ),
        tool_definitions=[
            ToolDefinition(
                name="meta_invoke",
                description="Run a meta skill.",
                input_schema=ToolInputSchema(
                    properties={"name": {"type": "string"}},
                    required=["name"],
                ),
            )
        ],
        tool_handler=unused_handler,
    )
    turn_task = asyncio.create_task(_collect_agent_events(agent, "run meta"))

    try:
        await asyncio.wait_for(producer_started.wait(), timeout=0.2)
        turn_task.cancel()
        await asyncio.wait_for(cancellation_seen.wait(), timeout=0.2)
        # A second cancellation interrupts asyncio.wait(meta_producer). The
        # producer must already be visible to the next-turn cleanup gate.
        turn_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(turn_task, timeout=0.2)

        assert agent._pending_cleanup_tasks
        retry_events = await asyncio.wait_for(
            _collect_agent_events(agent, "second"),
            timeout=0.2,
        )
        assert any(
            getattr(event, "code", "") == "agent_cleanup_in_progress"
            for event in retry_events
        )
        assert len(provider.calls) == 1
    finally:
        release.set()
    await asyncio.wait_for(producer_stopped.wait(), timeout=0.3)


@pytest.mark.asyncio
async def test_deadline_wrapup_splices_directive_when_margin_reached() -> None:
    provider = _SequenceProvider([_final_text()])
    # margin > timeout: the wrap-up arms at the first loop-top check.
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            timeout=30.0,
            deadline_wrapup_margin_seconds=60,
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    wrapup_texts = [
        text
        for text in _user_texts(provider.calls[0]["messages"])
        if text.startswith(_WRAPUP_PREFIX)
    ]
    assert len(wrapup_texts) == 1


@pytest.mark.asyncio
async def test_deadline_wrapup_can_force_no_tool_aggregator_finalization() -> None:
    provider = _SequenceProvider([_final_text()])
    agent = _echo_agent(
        provider,
        AgentConfig(
            timeout=30.0,
            deadline_wrapup_margin_seconds=60,
            deadline_wrapup_disable_tools=True,
            finalization_aggregator_only=True,
            finalization_disable_thinking=True,
            thinking=ThinkingLevel.HIGH,
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert provider.calls[0]["tools"] is None
    call_config = provider.calls[0]["config"]
    assert call_config.ensemble_execution_mode == "aggregator_only"
    assert call_config.thinking is False
    assert any("Do not call tools" in text for text in _user_texts(provider.calls[0]["messages"]))


@pytest.mark.asyncio
async def test_graceful_ensemble_forced_finalization_keeps_full_fusion() -> None:
    provider = _SequenceProvider([_final_text()])
    provider.supports_graceful_ensemble_finalization = True
    agent = _echo_agent(
        provider,
        AgentConfig(
            timeout=30.0,
            deadline_wrapup_margin_seconds=60,
            deadline_wrapup_disable_tools=True,
            finalization_aggregator_only=True,
            finalization_disable_thinking=True,
            thinking=ThinkingLevel.HIGH,
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert provider.calls[0]["tools"] is None
    call_config = provider.calls[0]["config"]
    # Cooperative providers preserve proposer diversity and own their soft
    # cutoff; aggregator-only remains a legacy fallback for providers that
    # cannot finalize an in-flight ensemble safely.
    assert call_config.ensemble_execution_mode == "full"
    assert call_config.ensemble_soft_deadline_seconds > 0
    assert call_config.ensemble_soft_deadline_disable_tools is True
    assert call_config.ensemble_soft_deadline_disable_thinking is True
    assert call_config.thinking is False


@pytest.mark.asyncio
async def test_deadline_wrapup_preempts_ensemble_progress_into_finalization() -> None:
    call_outcomes: list[dict[str, Any]] = []
    provider = _SequenceProvider(
        [
            [
                ProviderHeartbeatEvent(
                    phase="ensemble_proposers_wait",
                    message="still running proposers",
                ),
                1.05,
                ProviderHeartbeatEvent(
                    phase="ensemble_proposers_wait",
                    message="still running proposers",
                ),
            ],
            _final_text(),
        ]
    )
    agent = _echo_agent(
        provider,
        AgentConfig(
            timeout=2.0,
            deadline_wrapup_margin_seconds=1,
            deadline_wrapup_disable_tools=True,
            finalization_aggregator_only=True,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
            provider_call_observer=lambda **payload: call_outcomes.append(payload),
        ),
    )
    turn_call_logger = _RecordingTurnCallLogger()
    agent._turn_call_logger = turn_call_logger

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" and event.text == "done" for event in events)
    assert len(provider.calls) == 2
    assert provider.calls[0]["tools"]
    assert provider.calls[1]["tools"] is None
    assert provider.calls[1]["config"].ensemble_execution_mode == "aggregator_only"
    assert call_outcomes[0]["ok"] is False
    assert call_outcomes[0]["failure_kind"] == "policy_preempt"
    assert call_outcomes[1]["ok"] is True
    abandoned = [payload for kind, payload in turn_call_logger.records if kind == "llm_abandoned"]
    assert len(abandoned) == 1
    assert abandoned[0]["got_done_event"] is False
    assert abandoned[0]["failure_kind"] == "policy_preempt"
    assert abandoned[0]["usage_missing_count"] >= 1
    assert not any(
        kind == "llm_response" and payload.get("call_id") == abandoned[0]["call_id"]
        for kind, payload in turn_call_logger.records
    )


@pytest.mark.asyncio
async def test_deadline_wrapup_preempt_does_not_replay_unsafe_composite() -> None:
    provider = _SequenceProvider(
        [
            [
                ProviderHeartbeatEvent(
                    phase="ensemble_proposers_wait",
                    message="still running proposers",
                ),
                1.05,
                ProviderHeartbeatEvent(
                    phase="ensemble_proposers_wait",
                    message="soft deadline crossed",
                ),
            ],
            _final_text(),
        ]
    )
    provider.provider_name = "ensemble"
    provider.retry_failed_call_safe = False
    agent = _echo_agent(
        provider,
        AgentConfig(
            timeout=2.0,
            deadline_wrapup_margin_seconds=1,
            deadline_wrapup_disable_tools=True,
            finalization_aggregator_only=True,
            max_provider_retries=3,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert len(provider.calls) == 1
    assert any(
        event.kind == "error" and event.code == "provider_retry_unsafe"
        for event in events
    )
    assert not any(event.kind == "done" for event in events)


@pytest.mark.asyncio
async def test_deadline_wrapup_does_not_preempt_aggregator_finish() -> None:
    provider = _SequenceProvider(
        [
            [
                1.05,
                ProviderEnsembleProgress(event_type="aggregator_finish"),
                ProviderText(text="done"),
                ProviderDone(stop_reason="stop", input_tokens=5, output_tokens=1),
            ],
        ]
    )
    agent = _echo_agent(
        provider,
        AgentConfig(
            timeout=2.0,
            deadline_wrapup_margin_seconds=1,
            deadline_wrapup_disable_tools=True,
            finalization_aggregator_only=True,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" and event.text == "done" for event in events)
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_graceful_ensemble_receives_soft_deadline_without_agent_preempt() -> None:
    provider = _SequenceProvider(
        [
            [
                ProviderHeartbeatEvent(
                    phase="ensemble_proposers_wait",
                    message="still running proposers",
                ),
                1.05,
                ProviderReasoning(text="aggregator is wrapping up"),
                ProviderHeartbeatEvent(
                    phase="ensemble_proposers_wait",
                    message="soft deadline crossed",
                ),
                ProviderText(text="done"),
                ProviderDone(stop_reason="stop", input_tokens=5, output_tokens=1),
            ],
        ]
    )
    provider.supports_graceful_ensemble_finalization = True
    agent = _echo_agent(
        provider,
        AgentConfig(
            timeout=2.0,
            deadline_wrapup_margin_seconds=1,
            deadline_wrapup_disable_tools=True,
            finalization_disable_thinking=True,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" and event.text == "done" for event in events)
    assert len(provider.calls) == 1
    call_config = provider.calls[0]["config"]
    assert 0 < call_config.ensemble_soft_deadline_seconds <= 1.0
    assert call_config.ensemble_soft_deadline_disable_tools is True
    assert call_config.ensemble_soft_deadline_disable_thinking is True


@pytest.mark.asyncio
async def test_legacy_deadline_preempt_closes_stream_before_retry() -> None:
    provider = _CloseTrackingProvider(
        [
            [
                ProviderHeartbeatEvent(
                    phase="ensemble_proposers_wait",
                    message="still running proposers",
                ),
                1.05,
                ProviderHeartbeatEvent(
                    phase="ensemble_proposers_wait",
                    message="soft deadline crossed",
                ),
            ],
            _final_text(),
        ]
    )
    agent = _echo_agent(
        provider,
        AgentConfig(
            timeout=2.0,
            deadline_wrapup_margin_seconds=1,
            deadline_wrapup_disable_tools=True,
            finalization_aggregator_only=True,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" and event.text == "done" for event in events)
    assert len(provider.calls) == 2
    assert provider.first_closed_before_second_call is True
    assert provider.first_stream is not None
    assert provider.first_stream.close_calls == 1


@pytest.mark.asyncio
async def test_deadline_preempt_does_not_retry_over_unclosed_provider_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = asyncio.Event()
    closed = asyncio.Event()

    class _CancellationResistantProvider(_SequenceProvider):
        def __init__(self) -> None:
            super().__init__([[]])

        def chat(
            self,
            messages: list[Message],
            tools: list[Any] | None = None,
            config: ChatConfig | None = None,
        ) -> AsyncIterator[Any]:
            self.calls.append({"messages": messages, "tools": tools, "config": config})

            async def _stream() -> AsyncIterator[Any]:
                try:
                    yield ProviderHeartbeatEvent(
                        phase="ensemble_proposers_wait",
                        message="still running proposers",
                    )
                    while not release.is_set():
                        try:
                            await release.wait()
                        except asyncio.CancelledError:
                            continue
                finally:
                    closed.set()

            return _stream()

    monkeypatch.setattr(
        "opensquilla.engine.agent._PROVIDER_STREAM_CLEANUP_TIMEOUT_SECONDS",
        0.01,
    )
    provider = _CancellationResistantProvider()
    agent = _echo_agent(
        provider,
        AgentConfig(
            timeout=2.0,
            deadline_wrapup_margin_seconds=1,
            deadline_wrapup_disable_tools=True,
            finalization_aggregator_only=True,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    try:
        events = [event async for event in agent.run_turn("fix the bug")]
    finally:
        release.set()
    await asyncio.wait_for(closed.wait(), timeout=0.2)

    assert len(provider.calls) == 1
    assert any(
        event.kind == "error" and getattr(event, "code", "") == "provider_stream_close_timeout"
        for event in events
    )


@pytest.mark.asyncio
async def test_retrieval_loop_threshold_forces_no_tool_finalization() -> None:
    provider = _SequenceProvider(
        [
            _named_tool_call("search-1", "web_search"),
            _named_tool_call("fetch-1", "web_fetch"),
            _named_tool_call("search-2", "web_search"),
            _final_text(),
        ]
    )
    agent = _retrieval_agent(
        provider,
        AgentConfig(
            max_iterations=8,
            retrieval_loop_finalization_threshold=3,
            finalization_disable_thinking=True,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("research the answer")]

    assert any(event.kind == "done" and event.text == "done" for event in events)
    assert len(provider.calls) == 4
    assert all(call["tools"] for call in provider.calls[:3])
    assert provider.calls[3]["tools"] is None
    assert any(
        "retrieval-only iteration limit" in text
        for text in _user_texts(provider.calls[3]["messages"])
    )


@pytest.mark.asyncio
async def test_retrieval_finalization_retries_transient_provider_error() -> None:
    provider = _SequenceProvider(
        [
            _named_tool_call("search-1", "web_search"),
            [
                ProviderError(
                    message="service unavailable",
                    code="503",
                    ensemble_trace={
                        "llm_request_count": 4,
                        "failure_stage": "aggregator",
                    },
                )
            ],
            [
                ProviderText(text="done"),
                ProviderDone(
                    stop_reason="stop",
                    input_tokens=5,
                    output_tokens=1,
                    ensemble_trace={
                        "llm_request_count": 2,
                        "execution_mode": "aggregator_only",
                    },
                ),
            ],
        ]
    )
    agent = _retrieval_agent(
        provider,
        AgentConfig(
            max_iterations=5,
            max_provider_retries=1,
            retrieval_loop_finalization_threshold=1,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )
    turn_call_logger = _RecordingTurnCallLogger()
    agent._turn_call_logger = turn_call_logger

    events = [event async for event in agent.run_turn("research the answer")]

    done = next(event for event in events if event.kind == "done" and event.text == "done")
    assert done.ensemble_trace == {
        "llm_request_count": 6,
        "physical_request_count": 6,
        "usage_missing_count": 0,
        "execution_mode": "aggregator_only",
    }
    assert len(provider.calls) == 3
    assert provider.calls[1]["tools"] is None
    assert provider.calls[2]["tools"] is None
    llm_error = next(payload for kind, payload in turn_call_logger.records if kind == "llm_error")
    assert llm_error["ensemble_trace"] == {
        "llm_request_count": 4,
        "failure_stage": "aggregator",
    }
    assert any(
        event.kind == "warning" and event.code == "provider_finalization_retry" for event in events
    )


@pytest.mark.asyncio
async def test_deadline_wrapup_default_off() -> None:
    provider = _SequenceProvider([_final_text()])
    agent = Agent(
        provider=provider,
        config=AgentConfig(timeout=30.0),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert not [
        text
        for text in _user_texts(provider.calls[0]["messages"])
        if text.startswith(_WRAPUP_PREFIX)
    ]


@pytest.mark.asyncio
async def test_deadline_wrapup_not_armed_when_margin_not_reached() -> None:
    provider = _SequenceProvider([_final_text()])
    # Large timeout, small margin: the trigger stays far in the future.
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            timeout=3600.0,
            deadline_wrapup_margin_seconds=60,
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert not [
        text
        for text in _user_texts(provider.calls[0]["messages"])
        if text.startswith(_WRAPUP_PREFIX)
    ]


@pytest.mark.asyncio
async def test_deadline_wrapup_persists_across_calls_without_history_growth() -> None:
    provider = _SequenceProvider([_echo_tool_call("use-1"), _final_text()])
    agent = _echo_agent(
        provider,
        AgentConfig(
            timeout=30.0,
            deadline_wrapup_margin_seconds=60,
            max_iterations=5,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert len(provider.calls) == 2
    for call in provider.calls:
        wrapup_texts = [
            text for text in _user_texts(call["messages"]) if text.startswith(_WRAPUP_PREFIX)
        ]
        # Spliced into every request exactly once, never accumulated.
        assert len(wrapup_texts) == 1
    assert not [
        message
        for message in agent._history
        if message.role == "user"
        and isinstance(message.content, str)
        and message.content.startswith(_WRAPUP_PREFIX)
    ]


@pytest.mark.asyncio
async def test_deadline_wrapup_defers_to_max_iterations_finalization() -> None:
    provider = _SequenceProvider([_echo_tool_call("use-1"), _final_text()])
    agent = _echo_agent(
        provider,
        AgentConfig(
            timeout=30.0,
            deadline_wrapup_margin_seconds=60,
            max_iterations=1,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert len(provider.calls) == 2
    assert [
        text
        for text in _user_texts(provider.calls[0]["messages"])
        if text.startswith(_WRAPUP_PREFIX)
    ]
    finalization_texts = [
        text
        for text in _user_texts(provider.calls[1]["messages"])
        if "iteration limit has been reached" in text
    ]
    assert finalization_texts
    assert not [
        text
        for text in _user_texts(provider.calls[1]["messages"])
        if text.startswith(_WRAPUP_PREFIX)
    ]


@pytest.mark.asyncio
async def test_deadline_wrapup_preserves_post_tool_empty_response_recovery() -> None:
    # post_tool_empty_decision only fires on post_tool_turn=True; the spliced
    # wrap-up directive (a plain user message, always last in the request)
    # must not mask the post-tool shape of the underlying turn.
    provider = _SequenceProvider(
        [
            _echo_tool_call("use-1"),
            _empty_response(),
            _final_text(),
        ]
    )
    agent = _echo_agent(
        provider,
        AgentConfig(
            timeout=30.0,
            deadline_wrapup_margin_seconds=60,
            max_iterations=5,
            post_tool_empty_recovery_mode="warn_model",
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert any(
        event.kind == "warning" and event.code == "post_tool_empty_recovery" for event in events
    )
    assert len(provider.calls) == 3
    assert any(
        text.startswith("[Runtime recovery]") for text in _user_texts(provider.calls[2]["messages"])
    )


@pytest.mark.asyncio
async def test_deadline_wrapup_skips_splice_on_reasoning_prefill_tail() -> None:
    provider = _SequenceProvider(
        [
            [
                ProviderDone(
                    stop_reason="stop",
                    input_tokens=10,
                    output_tokens=5,
                    reasoning_tokens=5,
                    reasoning_content="internal reasoning",
                )
            ],
            _final_text(),
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            timeout=30.0,
            deadline_wrapup_margin_seconds=60,
            thinking=ThinkingLevel.MEDIUM,
            model_capabilities=ModelCapabilities(
                supports_reasoning=True,
                supports_tools=True,
                reasoning_format="openrouter",
            ),
            reasoning_prefill_recovery_mode="recover",
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert len(provider.calls) == 2
    assert [
        text
        for text in _user_texts(provider.calls[0]["messages"])
        if text.startswith(_WRAPUP_PREFIX)
    ]
    # The prefill continuation keeps the assistant tail last; the wrap-up is
    # withheld for that request rather than displacing the prefill.
    prefill_tail = provider.calls[1]["messages"][-1]
    assert prefill_tail.role == "assistant"
    assert prefill_tail.reasoning_content == "internal reasoning"
    assert not [
        text
        for text in _user_texts(provider.calls[1]["messages"])
        if text.startswith(_WRAPUP_PREFIX)
    ]


@pytest.mark.asyncio
async def test_deadline_wrapup_preempts_reasoning_only_stream() -> None:
    # A reasoning-only stream that spans the margin boundary is preempted while
    # margin remains; the retried call carries the directive. Without the
    # preempt, arming waits for the next iteration boundary, which for this
    # stream would land past the hard deadline with no directive delivered.
    provider = _SequenceProvider(
        [
            [
                ProviderReasoning(text="deep thought"),
                2.5,
                ProviderReasoning(text="more thought"),
                ProviderText(text="never reached in the preempted attempt"),
                ProviderDone(stop_reason="stop", input_tokens=5, output_tokens=2),
            ],
            _final_text(),
        ]
    )
    # timeout 8 / margin 6: the preempt threshold sits 2s in; the first
    # reasoning delta arrives before it, the second lands past it.
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            timeout=8.0,
            deadline_wrapup_margin_seconds=6,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert len(provider.calls) == 2
    assert not [
        text
        for text in _user_texts(provider.calls[0]["messages"])
        if text.startswith(_WRAPUP_PREFIX)
    ]
    wrapup_texts = [
        text
        for text in _user_texts(provider.calls[1]["messages"])
        if text.startswith(_WRAPUP_PREFIX)
    ]
    assert len(wrapup_texts) == 1


@pytest.mark.asyncio
async def test_deadline_wrapup_preempt_skips_streams_past_reasoning_phase() -> None:
    # Once the attempt has emitted user-visible output, the stream may be
    # writing the final answer; it must run to completion even when a late
    # reasoning delta arrives inside the margin.
    provider = _SequenceProvider(
        [
            [
                ProviderText(text="working on it"),
                2.5,
                ProviderReasoning(text="late reasoning"),
                ProviderText(text=" done"),
                ProviderDone(stop_reason="stop", input_tokens=5, output_tokens=2),
            ],
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            timeout=8.0,
            deadline_wrapup_margin_seconds=6,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert len(provider.calls) == 1
    assert not [
        text
        for text in _user_texts(provider.calls[0]["messages"])
        if text.startswith(_WRAPUP_PREFIX)
    ]


@pytest.mark.asyncio
async def test_deadline_wrapup_preempt_default_off() -> None:
    # Margin unset: the same margin-spanning reasoning stream runs to
    # completion in a single call with no directive anywhere.
    provider = _SequenceProvider(
        [
            [
                ProviderReasoning(text="deep thought"),
                2.5,
                ProviderReasoning(text="more thought"),
                ProviderText(text="answer"),
                ProviderDone(stop_reason="stop", input_tokens=5, output_tokens=2),
            ],
        ]
    )
    agent = Agent(provider=provider, config=AgentConfig(timeout=8.0))

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert len(provider.calls) == 1
    for call in provider.calls:
        assert not [
            text for text in _user_texts(call["messages"]) if text.startswith(_WRAPUP_PREFIX)
        ]


@pytest.mark.asyncio
async def test_deadline_wrapup_preempt_one_shot_once_armed() -> None:
    # Already-armed turns keep the upstream behavior: the directive is in the
    # request, and the reasoning stream runs to completion without a second
    # splice or a preempt retry.
    provider = _SequenceProvider(
        [
            [
                ProviderReasoning(text="thinking"),
                0.2,
                ProviderReasoning(text="still thinking"),
                ProviderText(text="answer"),
                ProviderDone(stop_reason="stop", input_tokens=5, output_tokens=2),
            ],
        ]
    )
    # margin > timeout: armed at the first loop-top check, before streaming.
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            timeout=30.0,
            deadline_wrapup_margin_seconds=60,
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert len(provider.calls) == 1
    wrapup_texts = [
        text
        for text in _user_texts(provider.calls[0]["messages"])
        if text.startswith(_WRAPUP_PREFIX)
    ]
    assert len(wrapup_texts) == 1


@pytest.mark.asyncio
async def test_deadline_wrapup_preempt_skipped_during_finalization() -> None:
    # The preempt exists to get the directive spliced into a retry; during the
    # max-iterations finalization the finalization message takes precedence
    # and the directive would never be spliced, so preempting only discards
    # the finalization stream for a byte-identical retry.
    provider = _SequenceProvider(
        [
            _echo_tool_call("use-1"),
            [
                ProviderReasoning(text="summarizing"),
                2.5,
                ProviderReasoning(text="still summarizing"),
                ProviderText(text="final summary"),
                ProviderDone(stop_reason="stop", input_tokens=5, output_tokens=2),
            ],
        ]
    )
    # timeout 8 / margin 6: the preempt threshold sits 2s in; the tool
    # iteration finishes before it, the finalization stream crosses it.
    agent = _echo_agent(
        provider,
        AgentConfig(
            timeout=8.0,
            deadline_wrapup_margin_seconds=6,
            max_iterations=1,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    # No preempt retry of the finalization call.
    assert len(provider.calls) == 2
    assert [
        text
        for text in _user_texts(provider.calls[1]["messages"])
        if "iteration limit has been reached" in text
    ]
    for call in provider.calls:
        assert not [
            text for text in _user_texts(call["messages"]) if text.startswith(_WRAPUP_PREFIX)
        ]


@pytest.mark.asyncio
async def test_deadline_progress_does_not_repreempt_forced_finalization() -> None:
    provider = _SequenceProvider(
        [
            _echo_tool_call("use-1"),
            [
                ProviderHeartbeatEvent(
                    phase="ensemble_proposers_wait",
                    message="finalizer waiting",
                ),
                1.05,
                ProviderHeartbeatEvent(
                    phase="ensemble_proposers_wait",
                    message="finalizer still waiting",
                ),
                ProviderText(text="final summary"),
                ProviderDone(stop_reason="stop", input_tokens=5, output_tokens=2),
            ],
            _final_text(),
        ]
    )
    agent = _echo_agent(
        provider,
        AgentConfig(
            timeout=2.0,
            deadline_wrapup_margin_seconds=1,
            deadline_wrapup_disable_tools=True,
            finalization_aggregator_only=True,
            max_iterations=1,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" and event.text == "final summary" for event in events)
    assert len(provider.calls) == 2
    assert provider.calls[1]["tools"] is None


@pytest.mark.asyncio
async def test_deadline_wrapup_preempt_skipped_on_reasoning_prefill_tail() -> None:
    # The splice is withheld while the turn ends on the prefill assistant
    # tail; a preempt there would discard the continuation stream for a
    # directive-free, byte-identical retry.
    provider = _SequenceProvider(
        [
            [
                ProviderDone(
                    stop_reason="stop",
                    input_tokens=10,
                    output_tokens=5,
                    reasoning_tokens=5,
                    reasoning_content="internal reasoning",
                )
            ],
            [
                ProviderReasoning(text="continuing"),
                2.5,
                ProviderReasoning(text="still continuing"),
                ProviderText(text="answer"),
                ProviderDone(stop_reason="stop", input_tokens=5, output_tokens=2),
            ],
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            timeout=8.0,
            deadline_wrapup_margin_seconds=6,
            thinking=ThinkingLevel.MEDIUM,
            model_capabilities=ModelCapabilities(
                supports_reasoning=True,
                supports_tools=True,
                reasoning_format="openrouter",
            ),
            reasoning_prefill_recovery_mode="recover",
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    # The continuation call crosses the preempt threshold mid-stream but runs
    # to completion: no preempt retry.
    assert len(provider.calls) == 2
    prefill_tail = provider.calls[1]["messages"][-1]
    assert prefill_tail.role == "assistant"
    for call in provider.calls:
        assert not [
            text for text in _user_texts(call["messages"]) if text.startswith(_WRAPUP_PREFIX)
        ]


@pytest.mark.asyncio
async def test_deadline_wrapup_preempt_records_no_tool_loop_event(tmp_path) -> None:
    # The preempted attempt is incomplete by engine choice; it must not be
    # reported to the tool-loop observer as a provider stream failure.
    runtime_events_path = tmp_path / "runtime_events.jsonl"
    provider = _SequenceProvider(
        [
            [
                ProviderReasoning(text="deep thought"),
                2.5,
                ProviderReasoning(text="more thought"),
                ProviderDone(stop_reason="stop", input_tokens=5, output_tokens=2),
            ],
            _final_text(),
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            timeout=8.0,
            deadline_wrapup_margin_seconds=6,
            tool_loop_observer_mode="log",
            runtime_events_path=str(runtime_events_path),
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    # The preempt itself still happened: the retry carries the directive.
    assert len(provider.calls) == 2
    assert [
        text
        for text in _user_texts(provider.calls[1]["messages"])
        if text.startswith(_WRAPUP_PREFIX)
    ]
    observer_events = []
    if runtime_events_path.exists():
        observer_events = [
            json.loads(line)
            for line in runtime_events_path.read_text().splitlines()
            if line.strip() and json.loads(line).get("mechanism") == "tool_loop_observer"
        ]
    assert observer_events == []


def test_env_plumbing_for_both_levers(monkeypatch: pytest.MonkeyPatch) -> None:
    # Helper-level check only; the full env -> bootstrap-stage -> AgentConfig
    # threading is covered in turn_runner/test_agent_bootstrap_stage_unit.py.
    from opensquilla.engine.turn_runner.agent_bootstrap_stage import (
        _nonnegative_int_from_env,
    )

    monkeypatch.delenv("OPENSQUILLA_PLACEHOLDER_ESCALATION_THRESHOLD", raising=False)
    monkeypatch.delenv("OPENSQUILLA_DEADLINE_WRAPUP_MARGIN_SECONDS", raising=False)
    assert _nonnegative_int_from_env("OPENSQUILLA_PLACEHOLDER_ESCALATION_THRESHOLD", 0) == 0
    assert _nonnegative_int_from_env("OPENSQUILLA_DEADLINE_WRAPUP_MARGIN_SECONDS", 0) == 0
    monkeypatch.setenv("OPENSQUILLA_PLACEHOLDER_ESCALATION_THRESHOLD", "3")
    monkeypatch.setenv("OPENSQUILLA_DEADLINE_WRAPUP_MARGIN_SECONDS", "360")
    assert _nonnegative_int_from_env("OPENSQUILLA_PLACEHOLDER_ESCALATION_THRESHOLD", 0) == 3
    assert _nonnegative_int_from_env("OPENSQUILLA_DEADLINE_WRAPUP_MARGIN_SECONDS", 0) == 360


def test_agent_config_defaults_keep_both_levers_off() -> None:
    config = AgentConfig()

    assert config.placeholder_escalation_threshold == 0
    assert config.deadline_wrapup_margin_seconds == 0
    assert config.deadline_wrapup_disable_tools is False
    assert config.retrieval_loop_finalization_threshold == 0
    assert config.finalization_aggregator_only is False
    assert config.finalization_disable_thinking is False
