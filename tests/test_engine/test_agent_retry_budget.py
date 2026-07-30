from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig, ThinkingLevel, ToolResult
from opensquilla.provider import (
    ChatConfig,
    Message,
    ProviderMessageLimitProof,
    ProviderRetryTransition,
    ToolDefinition,
    ToolInputSchema,
    provider_retry_roster_fingerprint,
)
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import ErrorEvent as ProviderError
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.provider import ToolUseEndEvent as ProviderToolUseEnd
from opensquilla.provider import ToolUseStartEvent as ProviderToolUseStart


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
        self.calls.append({"messages": messages, "tools": tools})
        events = self.streams[index] if index < len(self.streams) else self.streams[-1]
        return self._stream(events)

    async def _stream(self, events: list[Any]) -> AsyncIterator[Any]:
        for event in events:
            yield event

    async def list_models(self) -> list[Any]:
        return []


class _RetryScopeSequenceProvider(_SequenceProvider):
    def __init__(
        self,
        streams: list[list[Any]],
        *,
        begin_error: Exception | None = None,
    ) -> None:
        super().__init__(streams)
        self.begin_error = begin_error
        self.scope_begins: list[tuple[str, int]] = []
        self.scope_ends: list[str] = []

    def begin_provider_retry_scope(
        self,
        scope_id: str,
        *,
        max_additional_physical_requests: int = 3,
    ) -> bool:
        self.scope_begins.append((scope_id, max_additional_physical_requests))
        if self.begin_error is not None:
            raise self.begin_error
        return True

    def end_provider_retry_scope(self, scope_id: str) -> bool:
        self.scope_ends.append(scope_id)
        return True


class _CompositeSequenceProvider(_SequenceProvider):
    provider_name = "ensemble"
    retry_failed_call_safe = False


class _TransitionSequenceProvider(_SequenceProvider):
    provider_name = "ensemble"

    def __init__(
        self,
        streams: list[list[Any]],
        *,
        provider_retry_owner: str = "agent",
        retry_failed_call_safe: bool = False,
    ) -> None:
        super().__init__(streams)
        self.provider_retry_owner = provider_retry_owner
        self.retry_failed_call_safe = retry_failed_call_safe
        self.transition: ProviderRetryTransition | None = None
        self.transition_error: Exception | None = None
        self.transition_calls: list[ProviderError] = []
        self.scope_begins: list[tuple[str, int]] = []
        self.scope_ends: list[str] = []

    def prepare_retry_after_failure(
        self,
        event: ProviderError,
    ) -> ProviderRetryTransition | None:
        self.transition_calls.append(event)
        if self.transition_error is not None:
            raise self.transition_error
        return self.transition

    def begin_provider_retry_scope(
        self,
        scope_id: str,
        *,
        max_additional_physical_requests: int = 3,
    ) -> bool:
        self.scope_begins.append((scope_id, max_additional_physical_requests))
        return True

    def end_provider_retry_scope(self, scope_id: str) -> bool:
        self.scope_ends.append(scope_id)
        return True


class _NativeScopeTransitionSequenceProvider(_TransitionSequenceProvider):
    def __init__(self, streams: list[list[Any]]) -> None:
        super().__init__(streams)
        self.scope_reservations: list[tuple[str, int]] = []

    def reserve_provider_retry_physical_request(
        self,
        scope_id: str,
        *,
        physical_request_count: int = 1,
    ) -> bool:
        self.scope_reservations.append((scope_id, physical_request_count))
        return True


def _retry_plan(
    proposer_model: str,
    *,
    aggregator: str = "openrouter:aggregator",
) -> dict[str, Any]:
    return {
        "strategy": "router_dynamic",
        "selection_mode": "router_dynamic",
        "selected_P": [f"openrouter:{proposer_model}"],
        "backup_P": [],
        "proposer_models": [proposer_model],
        "selected_A": aggregator,
        "aggregator_candidates": [aggregator],
        "effective_min_successful_proposers": 1,
        "proposer_sample_count": 1,
        "proposer_recovery_policy": {
            "schema": "opensquilla.router-dynamic-proposer-recovery/v1",
            "configured_backup_count": 0,
            "effective_backup_count": 0,
            "max_additional_physical_requests": 3,
            "quorum_required": 1,
            "max_tokens_cap": 16_384,
            "visible_answer_reserve_tokens": 4_096,
            "thinking_downgrade_order": ["one_strictly_lower"],
            "transient_same_model_retries": 1,
            "backup_reasoning_downgrades": 1,
        },
    }


def _provider_retry_transition(
    *,
    replacement_provider: _SequenceProvider,
    source_plan: dict[str, Any],
    target_plan: dict[str, Any],
    setup_physical_request_count: int = 0,
) -> ProviderRetryTransition:
    return ProviderRetryTransition(
        replacement_provider=replacement_provider,
        reason="exclude_failed_roster_member",
        source_roster_fingerprint=provider_retry_roster_fingerprint(source_plan),
        target_roster_fingerprint=provider_retry_roster_fingerprint(target_plan),
        excluded_identities=("openrouter:source-model",),
        source_plan=source_plan,
        target_plan=target_plan,
        setup_physical_request_count=setup_physical_request_count,
    )


def _retryable_error() -> ProviderError:
    return ProviderError(
        message="upstream service unavailable",
        code="503",
        usage_missing_count=1,
        request_started=True,
        physical_request_count=1,
    )


def _reasoning_only_done() -> ProviderDone:
    return ProviderDone(
        stop_reason="stop",
        input_tokens=4,
        output_tokens=2,
        reasoning_tokens=2,
        reasoning_content="internal",
    )


def _empty_done() -> ProviderDone:
    return ProviderDone(stop_reason="stop", input_tokens=3, output_tokens=0)


def _ok_done() -> ProviderDone:
    return ProviderDone(stop_reason="stop", input_tokens=5, output_tokens=1)


@pytest.mark.asyncio
async def test_reasoning_only_retries_once_then_errors() -> None:
    provider = _SequenceProvider(
        [
            [_reasoning_only_done()],
            [_reasoning_only_done()],
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            thinking=ThinkingLevel.MEDIUM,
            max_provider_retries=1,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 2
    assert any(
        event.kind == "warning" and event.code == "provider_reasoning_only_retry"
        for event in events
    )
    assert any(event.kind == "error" and event.code == "empty_response" for event in events)


@pytest.mark.asyncio
async def test_reasoning_only_resolves_on_retry() -> None:
    provider = _SequenceProvider(
        [
            [_reasoning_only_done()],
            [ProviderText(text="ok"), _ok_done()],
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            thinking=ThinkingLevel.MEDIUM,
            max_provider_retries=1,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 2
    assert any(event.kind == "done" and event.text == "ok" for event in events)


@pytest.mark.asyncio
async def test_malformed_empty_retries_once_then_errors() -> None:
    provider = _SequenceProvider(
        [
            [_empty_done()],
            [_empty_done()],
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_provider_retries=1,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 2
    assert any(event.kind == "warning" and event.code == "provider_empty_retry" for event in events)
    assert any(event.kind == "error" and event.code == "empty_response" for event in events)


@pytest.mark.asyncio
async def test_malformed_empty_resolves_on_retry() -> None:
    provider = _SequenceProvider(
        [
            [_empty_done()],
            [ProviderText(text="ok"), _ok_done()],
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_provider_retries=1,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 2
    assert any(event.kind == "done" and event.text == "ok" for event in events)


@pytest.mark.asyncio
async def test_stream_incomplete_retries_once_then_errors() -> None:
    provider = _SequenceProvider([[], []])
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_provider_retries=1,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 2
    assert any(event.kind == "warning" and event.code == "provider_empty_retry" for event in events)
    assert any(
        event.kind == "error" and event.code == "provider_stream_incomplete"
        for event in events
    )


@pytest.mark.asyncio
async def test_stream_incomplete_resolves_on_retry() -> None:
    provider = _SequenceProvider(
        [
            [],
            [ProviderText(text="ok"), _ok_done()],
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_provider_retries=1,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 2
    assert any(event.kind == "done" and event.text == "ok" for event in events)


@pytest.mark.asyncio
async def test_timeout_error_code_retries_when_message_lacks_timeout_token() -> None:
    provider = _SequenceProvider(
        [
            [ProviderError(message="Request timed out: ", code="timeout")],
            [ProviderText(text="ok"), _ok_done()],
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_provider_retries=1,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 2
    assert any(event.kind == "done" and event.text == "ok" for event in events)
    assert not any(event.kind == "error" and event.code == "timeout" for event in events)


@pytest.mark.asyncio
async def test_composite_timeout_surfaces_without_replaying_full_call() -> None:
    provider = _CompositeSequenceProvider(
        [
            [ProviderError(message="Request timed out: ", code="timeout")],
            [ProviderText(text="should-not-run"), _ok_done()],
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_provider_retries=3,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 1
    assert any(event.kind == "error" and event.code == "timeout" for event in events)
    assert not any(
        event.kind == "text_delta" and event.text == "should-not-run"
        for event in events
    )


@pytest.mark.asyncio
async def test_composite_message_limit_recovery_does_not_replay_full_call() -> None:
    provider = _CompositeSequenceProvider(
        [
            [
                ProviderError(
                    message="request exceeds provider message limit",
                    code="400",
                    message_limit_proof=ProviderMessageLimitProof(
                        actual_wire_messages=101,
                        limit=100,
                        logical_messages=101,
                        system_messages=0,
                        tool_result_messages=0,
                        provider_kind="openrouter",
                        model="test/model",
                        base_host="openrouter.ai",
                    ),
                    request_started=True,
                    physical_request_count=1,
                )
            ],
            [ProviderText(text="should-not-run"), _ok_done()],
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_provider_retries=3,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 1
    assert any(event.kind == "error" and event.code == "400" for event in events)
    assert not any(
        event.kind == "warning"
        and event.code == "provider_request_message_limit_recovery_success"
        for event in events
    )


@pytest.mark.asyncio
async def test_composite_context_overflow_recovery_does_not_replay_full_call() -> None:
    provider = _CompositeSequenceProvider(
        [
            [
                ProviderError(
                    message='{"fallback_reason":"provider_request_budget_exhausted"}',
                    code="provider_request_budget_exhausted",
                    request_started=True,
                    physical_request_count=1,
                )
            ],
            [ProviderText(text="should-not-run"), _ok_done()],
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_provider_retries=3,
            max_overflow_retries=2,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 1
    assert any(
        event.kind == "error"
        and event.code == "provider_request_budget_exhausted"
        for event in events
    )
    assert not any(
        event.kind == "warning" and event.code == "context_auto_compaction_retry"
        for event in events
    )


@pytest.mark.asyncio
async def test_composite_partial_timeout_does_not_duplicate_visible_text() -> None:
    provider = _CompositeSequenceProvider(
        [
            [
                ProviderText(text="partial"),
                ProviderError(message="Request timed out: ", code="timeout"),
            ],
            [ProviderText(text="duplicate"), _ok_done()],
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_provider_retries=3,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 1
    visible = [event.text for event in events if event.kind == "text_delta"]
    assert visible == ["partial"]
    assert any(event.kind == "error" and event.code == "timeout" for event in events)


@pytest.mark.asyncio
async def test_composite_length_capped_response_does_not_replay_full_call() -> None:
    provider = _CompositeSequenceProvider(
        [
            [
                ProviderText(text="partial answer"),
                ProviderDone(
                    stop_reason="length",
                    input_tokens=5,
                    output_tokens=2,
                ),
            ],
            [ProviderText(text="should-not-run"), _ok_done()],
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_provider_retries=3,
            length_capped_continuations=3,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 1
    assert any(
        event.kind == "text_delta" and event.text == "partial answer"
        for event in events
    )
    assert not any(
        event.kind == "warning" and event.code == "provider_output_continue"
        for event in events
    )
    assert any(
        event.kind == "error" and event.code == "provider_output_truncated"
        for event in events
    )
    assert not any(getattr(event, "text", "") == "should-not-run" for event in events)


@pytest.mark.asyncio
async def test_provider_retry_transition_replaces_roster_without_backoff() -> None:
    target = _RetryScopeSequenceProvider([[ProviderText(text="ok"), _ok_done()]])
    source = _TransitionSequenceProvider([[_retryable_error()]])
    source_plan = _retry_plan("source-model")
    target_plan = _retry_plan("target-model")
    source.transition = _provider_retry_transition(
        replacement_provider=target,
        source_plan=source_plan,
        target_plan=target_plan,
    )
    agent = Agent(
        provider=source,
        config=AgentConfig(
            max_provider_retries=1,
            retry_base_backoff_ms=60_000,
            retry_max_backoff_ms=60_000,
        ),
    )
    audit_records: list[tuple[str, dict[str, Any]]] = []
    agent._write_turn_call_log = (  # type: ignore[method-assign]
        lambda kind, **payload: audit_records.append((kind, payload))
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(source.calls) == 1
    assert len(source.transition_calls) == 1
    assert len(target.calls) == 1
    assert agent.provider is target
    assert any(
        event.kind == "warning" and event.code == "provider_retry_transition" for event in events
    )
    assert any(event.kind == "done" and event.text == "ok" for event in events)
    transition_audit = next(
        payload
        for kind, payload in audit_records
        if kind == "provider_retry_transition" and payload["action"] == "apply"
    )
    audit_kinds = [kind for kind, _payload in audit_records]
    assert audit_kinds.index("llm_error") < audit_kinds.index("provider_retry_transition")
    assert transition_audit["source_roster_fingerprint"] == (
        provider_retry_roster_fingerprint(source_plan)
    )
    assert transition_audit["target_roster_fingerprint"] == (
        provider_retry_roster_fingerprint(target_plan)
    )
    assert transition_audit["setup_physical_request_count"] == 0
    assert len(source.scope_begins) == 1
    assert len(target.scope_begins) == 1
    source_scope_id, source_scope_budget = source.scope_begins[0]
    target_scope_id, target_scope_budget = target.scope_begins[0]
    assert source_scope_id == target_scope_id
    assert source_scope_budget == target_scope_budget == 1
    assert source.scope_ends == [source_scope_id]
    assert target.scope_ends == [target_scope_id]


@pytest.mark.asyncio
async def test_retry_transition_binds_only_remaining_agent_budget() -> None:
    target = _RetryScopeSequenceProvider([[ProviderText(text="ok"), _ok_done()]])
    source = _TransitionSequenceProvider(
        [
            [_empty_done()],
            [_retryable_error()],
        ],
        retry_failed_call_safe=True,
    )
    source_plan = _retry_plan("source-model")
    target_plan = _retry_plan("target-model")
    source.transition = _provider_retry_transition(
        replacement_provider=target,
        source_plan=source_plan,
        target_plan=target_plan,
    )
    agent = Agent(
        provider=source,
        config=AgentConfig(
            max_provider_retries=3,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(source.calls) == 2
    assert len(target.calls) == 1
    assert any(event.kind == "done" and event.text == "ok" for event in events)
    [source_begin] = source.scope_begins
    [target_begin] = target.scope_begins
    assert source_begin[0] == target_begin[0]
    assert source_begin[1] == 3
    assert target_begin[1] == 2
    assert source.scope_ends == [source_begin[0]]
    assert target.scope_ends == [target_begin[0]]


@pytest.mark.asyncio
async def test_native_scope_transition_without_handoff_proof_fails_closed() -> None:
    target = _RetryScopeSequenceProvider([[ProviderText(text="must-not-run"), _ok_done()]])
    source = _NativeScopeTransitionSequenceProvider([[_retryable_error()]])
    source.transition = _provider_retry_transition(
        replacement_provider=target,
        source_plan=_retry_plan("source-model"),
        target_plan=_retry_plan("target-model"),
    )
    agent = Agent(
        provider=source,
        config=AgentConfig(
            max_provider_retries=3,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )
    audit_records: list[tuple[str, dict[str, Any]]] = []
    agent._write_turn_call_log = (  # type: ignore[method-assign]
        lambda kind, **payload: audit_records.append((kind, payload))
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(source.calls) == 1
    assert len(source.transition_calls) == 1
    assert source.scope_reservations == []
    assert target.calls == []
    assert target.scope_begins == []
    assert agent.provider is source
    assert any(event.kind == "error" and event.code == "503" for event in events)
    rejection = next(
        payload
        for kind, payload in audit_records
        if kind == "provider_retry_transition" and payload["action"] == "reject"
    )
    assert rejection["reason"] == "native_retry_scope_handoff_unproven"


@pytest.mark.asyncio
async def test_provider_retry_transition_rejects_composite_replacement() -> None:
    target = _CompositeSequenceProvider(
        [[ProviderText(text="should-not-run"), _ok_done()]]
    )
    source = _TransitionSequenceProvider([[_retryable_error()]])
    source.transition = _provider_retry_transition(
        replacement_provider=target,
        source_plan=_retry_plan("source-model"),
        target_plan=_retry_plan("target-model"),
    )
    agent = Agent(
        provider=source,
        config=AgentConfig(
            max_provider_retries=3,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )
    audit_records: list[tuple[str, dict[str, Any]]] = []
    agent._write_turn_call_log = (  # type: ignore[method-assign]
        lambda kind, **payload: audit_records.append((kind, payload))
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(source.calls) == 1
    assert len(source.transition_calls) == 1
    assert target.calls == []
    assert agent.provider is source
    assert any(event.kind == "error" and event.code == "503" for event in events)
    assert not any(
        event.kind == "warning" and event.code == "provider_retry_transition"
        for event in events
    )
    rejection = next(
        payload
        for kind, payload in audit_records
        if kind == "provider_retry_transition" and payload["action"] == "reject"
    )
    assert rejection["reason"] == "composite_replacement_provider"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_kind", ["same_roster", "setup_request", "raises"])
async def test_invalid_provider_retry_transition_fails_closed_without_replay(
    invalid_kind: str,
) -> None:
    target = _SequenceProvider([[ProviderText(text="should-not-run"), _ok_done()]])
    source = _TransitionSequenceProvider(
        [
            [_retryable_error()],
            [ProviderText(text="replayed"), _ok_done()],
        ],
        # Prove a rejected typed transition suppresses even a provider that
        # otherwise declares exact-call replay safe.
        retry_failed_call_safe=True,
    )
    source_plan = _retry_plan("source-model")
    target_plan = source_plan if invalid_kind == "same_roster" else _retry_plan("target-model")
    source.transition = _provider_retry_transition(
        replacement_provider=target,
        source_plan=source_plan,
        target_plan=target_plan,
        setup_physical_request_count=(1 if invalid_kind == "setup_request" else 0),
    )
    if invalid_kind == "raises":
        source.transition_error = RuntimeError("synthetic transition failure")
    agent = Agent(
        provider=source,
        config=AgentConfig(
            max_provider_retries=2,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(source.calls) == 1
    assert len(source.transition_calls) == 1
    assert len(target.calls) == 0
    assert agent.provider is source
    assert any(event.kind == "error" and event.code == "503" for event in events)
    assert not any(
        event.kind == "text_delta" and event.text in {"replayed", "should-not-run"}
        for event in events
    )


@pytest.mark.asyncio
async def test_provider_retry_transition_respects_zero_retry_budget() -> None:
    target = _SequenceProvider([[ProviderText(text="should-not-run"), _ok_done()]])
    source = _TransitionSequenceProvider([[_retryable_error()]])
    source.transition = _provider_retry_transition(
        replacement_provider=target,
        source_plan=_retry_plan("source-model"),
        target_plan=_retry_plan("target-model"),
    )
    agent = Agent(
        provider=source,
        config=AgentConfig(
            max_provider_retries=0,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(source.calls) == 1
    assert len(source.transition_calls) == 0
    assert len(target.calls) == 0
    assert agent.provider is source
    assert any(event.kind == "error" and event.code == "503" for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_owner", "metadata"),
    [
        ("caller", {}),
        ("agent", {"provider_retry_owner": "caller"}),
        ("agent", {"provider_retry_owner": "unexpected-owner"}),
    ],
)
async def test_provider_retry_transition_owner_caller_skips_agent_transition(
    provider_owner: str,
    metadata: dict[str, Any],
) -> None:
    target = _SequenceProvider([[ProviderText(text="should-not-run"), _ok_done()]])
    source = _TransitionSequenceProvider(
        [[_retryable_error()]],
        provider_retry_owner=provider_owner,
    )
    source.transition = _provider_retry_transition(
        replacement_provider=target,
        source_plan=_retry_plan("source-model"),
        target_plan=_retry_plan("target-model"),
    )
    agent = Agent(
        provider=source,
        config=AgentConfig(
            max_provider_retries=1,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
            metadata=metadata,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(source.calls) == 1
    assert len(source.transition_calls) == 0
    assert len(target.calls) == 0
    assert agent.provider is source
    assert any(event.kind == "error" and event.code == "503" for event in events)


@pytest.mark.asyncio
async def test_provider_retry_transition_rejects_a_visited_target_roster() -> None:
    source_plan = _retry_plan("source-model")
    target_plan = _retry_plan("target-model")
    source = _TransitionSequenceProvider(
        [
            [_retryable_error()],
            [ProviderText(text="cycle-replay"), _ok_done()],
        ],
    )
    target = _TransitionSequenceProvider(
        [[_retryable_error()]],
        retry_failed_call_safe=True,
    )
    source.transition = _provider_retry_transition(
        replacement_provider=target,
        source_plan=source_plan,
        target_plan=target_plan,
    )
    target.transition = _provider_retry_transition(
        replacement_provider=source,
        source_plan=target_plan,
        target_plan=source_plan,
    )
    agent = Agent(
        provider=source,
        config=AgentConfig(
            max_provider_retries=2,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(source.calls) == 1
    assert len(source.transition_calls) == 1
    assert len(target.calls) == 1
    assert len(target.transition_calls) == 1
    assert agent.provider is target
    assert any(event.kind == "error" and event.code == "503" for event in events)
    assert not any(event.kind == "text_delta" and event.text == "cycle-replay" for event in events)


@pytest.mark.asyncio
async def test_provider_retry_transition_skips_after_visible_output() -> None:
    target = _SequenceProvider([[ProviderText(text="should-not-run"), _ok_done()]])
    source = _TransitionSequenceProvider(
        [[ProviderText(text="partial"), _retryable_error()]],
    )
    source.transition = _provider_retry_transition(
        replacement_provider=target,
        source_plan=_retry_plan("source-model"),
        target_plan=_retry_plan("target-model"),
    )
    agent = Agent(
        provider=source,
        config=AgentConfig(
            max_provider_retries=1,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(source.calls) == 1
    assert len(source.transition_calls) == 0
    assert len(target.calls) == 0
    assert [event.text for event in events if event.kind == "text_delta"] == ["partial"]
    assert any(event.kind == "error" and event.code == "503" for event in events)


@pytest.mark.asyncio
async def test_first_turn_provider_empty_response_error_surfaces_without_retry() -> None:
    provider = _SequenceProvider(
        [
            [ProviderError(message="Provider returned an empty response", code="empty_response")],
            [ProviderText(text="should-not-run"), _ok_done()],
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_provider_retries=1,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 1
    assert any(event.kind == "error" and event.code == "empty_response" for event in events)


@pytest.mark.asyncio
async def test_post_tool_provider_empty_response_error_retries_once_and_recovers() -> None:
    provider = _SequenceProvider(
        [
            [
                ProviderToolUseStart(tool_use_id="tool-1", tool_name="echo"),
                ProviderToolUseEnd(
                    tool_use_id="tool-1",
                    tool_name="echo",
                    arguments={"value": "ok"},
                ),
                ProviderDone(stop_reason="tool_use", input_tokens=3, output_tokens=1),
            ],
            [ProviderError(message="Provider returned an empty response", code="empty_response")],
            [ProviderText(text="done"), _ok_done()],
        ]
    )

    async def tool_handler(call: object) -> ToolResult:
        return ToolResult(
            tool_use_id=getattr(call, "tool_use_id"),
            tool_name=getattr(call, "tool_name"),
            content="tool ok",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=2,
            max_provider_retries=1,
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

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 3
    assert any(event.kind == "warning" and event.code == "provider_empty_retry" for event in events)
    assert any(event.kind == "done" and event.text == "done" for event in events)


@pytest.mark.asyncio
async def test_post_tool_composite_empty_error_does_not_replay_full_call() -> None:
    provider = _CompositeSequenceProvider(
        [
            [
                ProviderToolUseStart(tool_use_id="tool-1", tool_name="echo"),
                ProviderToolUseEnd(
                    tool_use_id="tool-1",
                    tool_name="echo",
                    arguments={"value": "ok"},
                ),
                ProviderDone(stop_reason="tool_use", input_tokens=3, output_tokens=1),
            ],
            [
                ProviderError(
                    message="Provider returned an empty response",
                    code="empty_response",
                )
            ],
            [ProviderText(text="should-not-run"), _ok_done()],
        ]
    )

    async def tool_handler(call: object) -> ToolResult:
        return ToolResult(
            tool_use_id=getattr(call, "tool_use_id"),
            tool_name=getattr(call, "tool_name"),
            content="tool ok",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=2,
            max_provider_retries=3,
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

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 2
    assert any(event.kind == "error" and event.code == "empty_response" for event in events)
    assert not any(
        event.kind == "text_delta" and event.text == "should-not-run" for event in events
    )


@pytest.mark.asyncio
async def test_post_tool_provider_empty_response_error_retries_once_with_default_budget() -> None:
    provider = _SequenceProvider(
        [
            [
                ProviderToolUseStart(tool_use_id="tool-1", tool_name="echo"),
                ProviderToolUseEnd(
                    tool_use_id="tool-1",
                    tool_name="echo",
                    arguments={"value": "ok"},
                ),
                ProviderDone(stop_reason="tool_use", input_tokens=3, output_tokens=1),
            ],
            [ProviderError(message="Provider returned an empty response", code="empty_response")],
            [ProviderError(message="Provider returned an empty response", code="empty_response")],
            [ProviderText(text="should-not-run"), _ok_done()],
        ]
    )

    async def tool_handler(call: object) -> ToolResult:
        return ToolResult(
            tool_use_id=getattr(call, "tool_use_id"),
            tool_name=getattr(call, "tool_name"),
            content="tool ok",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=2,
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

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 3
    assert (
        len(
            [
                event
                for event in events
                if event.kind == "warning" and event.code == "provider_empty_retry"
            ]
        )
        == 1
    )
    assert any(event.kind == "error" and event.code == "empty_response" for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configured_budget", "expected_scope_budget"),
    [(0, 0), (1, 1), (9, 3)],
)
async def test_provider_retry_scope_uses_effective_turn_budget(
    configured_budget: int,
    expected_scope_budget: int,
) -> None:
    provider = _RetryScopeSequenceProvider([[ProviderText(text="ok"), _ok_done()]])
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_provider_retries=configured_budget),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert any(event.kind == "done" and event.text == "ok" for event in events)
    assert len(provider.scope_begins) == 1
    scope_id, scope_budget = provider.scope_begins[0]
    assert scope_id.startswith("agent-run-turn:")
    assert scope_budget == expected_scope_budget
    assert provider.scope_ends == [scope_id]


@pytest.mark.asyncio
async def test_provider_retry_scope_spans_post_tool_calls() -> None:
    provider = _RetryScopeSequenceProvider(
        [
            [
                ProviderToolUseStart(tool_use_id="tool-1", tool_name="echo"),
                ProviderToolUseEnd(
                    tool_use_id="tool-1",
                    tool_name="echo",
                    arguments={"value": "ok"},
                ),
                ProviderDone(
                    stop_reason="tool_use",
                    input_tokens=3,
                    output_tokens=1,
                ),
            ],
            [ProviderText(text="done"), _ok_done()],
        ]
    )

    async def tool_handler(call: object) -> ToolResult:
        return ToolResult(
            tool_use_id=getattr(call, "tool_use_id"),
            tool_name=getattr(call, "tool_name"),
            content="tool ok",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=2,
            max_provider_retries=2,
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

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 2
    assert any(event.kind == "done" and event.text == "done" for event in events)
    assert len(provider.scope_begins) == 1
    scope_id, scope_budget = provider.scope_begins[0]
    assert scope_budget == 2
    assert provider.scope_ends == [scope_id]


@pytest.mark.asyncio
async def test_agent_retry_budget_is_shared_across_tool_iterations() -> None:
    def tool_stream(index: int) -> list[Any]:
        tool_use_id = f"tool-{index}"
        return [
            ProviderToolUseStart(
                tool_use_id=tool_use_id,
                tool_name="echo",
            ),
            ProviderToolUseEnd(
                tool_use_id=tool_use_id,
                tool_name="echo",
                arguments={"value": str(index)},
            ),
            ProviderDone(
                stop_reason="tool_use",
                input_tokens=3,
                output_tokens=1,
            ),
        ]

    empty_error = [
        ProviderError(
            message="Provider returned an empty response",
            code="empty_response",
        )
    ]
    provider = _RetryScopeSequenceProvider(
        [
            tool_stream(1),
            empty_error,
            tool_stream(2),
            empty_error,
            tool_stream(3),
            empty_error,
            tool_stream(4),
            empty_error,
            [ProviderText(text="should-not-run"), _ok_done()],
        ]
    )

    async def tool_handler(call: object) -> ToolResult:
        return ToolResult(
            tool_use_id=getattr(call, "tool_use_id"),
            tool_name=getattr(call, "tool_name"),
            content="tool ok",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=0,
            max_provider_retries=9,
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

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 8
    assert not any(
        event.kind == "text_delta" and event.text == "should-not-run" for event in events
    )
    assert any(
        event.kind == "error" and event.code == "provider_retry_budget_exhausted"
        for event in events
    )
    assert provider.scope_begins[0][1] == 3


@pytest.mark.asyncio
async def test_provider_retry_scope_begin_failure_is_local_and_fail_closed() -> None:
    provider = _RetryScopeSequenceProvider(
        [[ProviderText(text="should-not-run"), _ok_done()]],
        begin_error=RuntimeError("synthetic scope failure"),
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_provider_retries=3),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert provider.calls == []
    assert len(provider.scope_begins) == 1
    assert provider.scope_ends == []
    scope_errors = [
        event
        for event in events
        if event.kind == "error" and event.code == "provider_retry_scope_begin_failed"
    ]
    assert len(scope_errors) == 1
    assert scope_errors[0].request_started is False
    assert scope_errors[0].physical_request_count == 0


@pytest.mark.asyncio
async def test_provider_retry_scope_ends_once_when_consumer_closes_early() -> None:
    provider = _RetryScopeSequenceProvider([[ProviderText(text="must-not-run"), _ok_done()]])
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_provider_retries=3),
    )

    turn = agent.run_turn("hello")
    first_event = await anext(turn)
    await turn.aclose()

    assert first_event.kind == "state_change"
    assert provider.calls == []
    assert len(provider.scope_begins) == 1
    scope_id, scope_budget = provider.scope_begins[0]
    assert scope_budget == 3
    assert provider.scope_ends == [scope_id]
