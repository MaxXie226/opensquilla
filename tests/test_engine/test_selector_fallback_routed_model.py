"""Failover must realign routed_model telemetry to the model that runs.

Same invariant the explicit-model override realignment enforces
(prompt_assembler_stage, commit 966df982): ``metadata["routed_model"]`` is
read by RouterDecisionEvent and comprehensive-savings pricing, so after a
selector failover it must name the fallback model, and route-savings figures
computed for the abandoned model no longer apply.
"""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig, ThinkingLevel
from opensquilla.engine.pipeline import TurnContext
from opensquilla.engine.runtime import TurnRunner, _SelectorFallbackProvider
from opensquilla.engine.types import DoneEvent as EngineDoneEvent
from opensquilla.engine.types import RouterDecisionEvent
from opensquilla.provider import (
    DoneEvent,
    ErrorEvent,
    ProviderRetryTransition,
    TextDeltaEvent,
    provider_retry_roster_fingerprint,
)
from opensquilla.tools.types import CallerKind, ToolContext


class _StubSelector:
    def __init__(self, fallback_model: str) -> None:
        self._fallback_model = fallback_model

    def next_fallback_after_failure(self, exc: Exception) -> object:
        return object()

    @property
    def current_config(self) -> SimpleNamespace:
        return SimpleNamespace(provider="fallback-provider", model=self._fallback_model)


def _router_dynamic_retry_plan(
    decision_id: str,
    proposer: str,
) -> dict[str, Any]:
    return {
        "strategy": "router_dynamic",
        "selection_mode": "router_dynamic",
        "decision_id": decision_id,
        "ranking_version": "test-ranking-v1",
        "registry_snapshot_version": "test-registry-v1",
        "registry_snapshot_hash": "a" * 64,
        "selected_P": [f"openrouter:{proposer}"],
        "backup_P": [],
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
        "proposer_models": [proposer],
        "selected_A": "openrouter:aggregator-model",
        "aggregator_candidates": ["openrouter:aggregator-model"],
        "effective_min_successful_proposers": 1,
        "proposer_sample_count": 1,
        "effective_tier": "c1",
        "session": {"escalation_level": 1},
    }


def _retry_transition(
    source_plan: dict[str, Any],
    target_plan: dict[str, Any],
    replacement_provider: object,
) -> ProviderRetryTransition:
    return ProviderRetryTransition(
        replacement_provider=replacement_provider,
        reason="reasoning_only_length",
        source_roster_fingerprint=provider_retry_roster_fingerprint(source_plan),
        target_roster_fingerprint=provider_retry_roster_fingerprint(target_plan),
        excluded_identities=("openrouter:source-proposer",),
        source_plan=source_plan,
        target_plan=target_plan,
    )


class _RetryPrimary:
    provider_name = "ensemble"
    retry_failed_call_safe = False

    def __init__(
        self,
        transition: ProviderRetryTransition | None,
    ) -> None:
        self.transition = transition
        self.events: list[ErrorEvent] = []

    def prepare_retry_after_failure(
        self,
        event: ErrorEvent,
    ) -> ProviderRetryTransition | None:
        self.events.append(event)
        return self.transition


class _RetryReplacement:
    provider_name = "ensemble"
    retry_failed_call_safe = False

    def chat(
        self,
        messages: list[Any],
        tools: Any = None,
        config: Any = None,
    ) -> AsyncIterator[Any]:
        del messages, tools, config
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        yield DoneEvent(stop_reason="stop")


def _valid_retry_plans() -> tuple[dict[str, Any], dict[str, Any]]:
    source = _router_dynamic_retry_plan(
        "source-decision",
        "source-proposer",
    )
    target = _router_dynamic_retry_plan(
        "target-decision",
        "target-proposer",
    )
    target.update(
        {
            "retry_parent_decision_id": source["decision_id"],
            "retry_excluded_proposer_identities": ["openrouter:source-proposer"],
            "task_analysis_reused": True,
            "task_analysis_reuse": {
                "source_decision_id": source["decision_id"],
            },
            "retry_routing": {
                "parent_decision_id": source["decision_id"],
                "task_analysis_reused": True,
                "excluded_proposer_identities": ["openrouter:source-proposer"],
            },
        }
    )
    return source, target


def test_fallback_realigns_routed_model_and_drops_savings() -> None:
    metadata: dict[str, object] = {
        "routed_model": "expensive/model",
        "savings_pct": 12.5,
        "savings_max_price_per_m": 3.0,
        "savings_routed_price_per_m": 0.5,
    }
    wrapper = _SelectorFallbackProvider(
        object(),
        _StubSelector("cheap/fallback"),
        turn_metadata=metadata,
    )

    assert wrapper.fallback_after_invalid_response("upstream 503") is True

    assert metadata["routed_model"] == "cheap/fallback"
    assert metadata["executed_provider"] == "fallback-provider"
    assert metadata["executed_model"] == "cheap/fallback"
    assert metadata["router_fallback_reason"] == "selector_fallback"
    assert metadata["savings_pct"] == 0.0
    assert metadata["savings_max_price_per_m"] == 0.0
    assert metadata["savings_routed_price_per_m"] == 0.0


def test_fallback_to_same_model_keeps_savings() -> None:
    metadata: dict[str, object] = {"routed_model": "same/model", "savings_pct": 7.0}
    wrapper = _SelectorFallbackProvider(
        object(),
        _StubSelector("same/model"),
        turn_metadata=metadata,
    )

    assert wrapper.fallback_after_invalid_response("upstream 503") is True

    assert metadata["routed_model"] == "same/model"
    assert metadata["savings_pct"] == 7.0


def test_fallback_without_metadata_is_noop() -> None:
    wrapper = _SelectorFallbackProvider(object(), _StubSelector("any/model"))
    assert wrapper.fallback_after_invalid_response("upstream 503") is True


@pytest.mark.asyncio
async def test_retry_transition_rewraps_provider_and_retargets_route_audit() -> None:
    source, target = _valid_retry_plans()
    raw_replacement = _RetryReplacement()
    primary = _RetryPrimary(_retry_transition(source, target, raw_replacement))
    selector = _StubSelector("same-selector")
    health_ledger = object()
    metadata: dict[str, Any] = {
        "router_dynamic_pending_route_plan": copy.deepcopy(source),
        "router_dynamic_decision": {"decision_id": source["decision_id"]},
        "ensemble_decision_id": source["decision_id"],
        "router_fallback_hops": 2,
    }
    source_metadata = copy.deepcopy(metadata)
    wrapper = _SelectorFallbackProvider(
        primary,
        selector,
        turn_metadata=metadata,
        health_ledger=health_ledger,  # type: ignore[arg-type]
    )
    event = ErrorEvent(
        message="not enough proposers",
        code="ensemble_insufficient_proposers",
    )

    transition = wrapper.prepare_retry_after_failure(event)

    assert transition is not None
    assert transition is not primary.transition
    assert primary.events == [event]
    replacement = transition.replacement_provider
    assert isinstance(replacement, _SelectorFallbackProvider)
    assert replacement.primary is raw_replacement
    assert replacement._selector is selector
    assert replacement._turn_metadata is metadata
    assert replacement._health_ledger is health_ledger
    assert replacement.accounts_physical_usage is True
    assert replacement.retry_failed_call_safe is False
    assert metadata == source_metadata

    retry_stream = replacement.chat([])
    assert metadata == source_metadata

    first_event = await anext(retry_stream)
    assert isinstance(first_event, DoneEvent)

    assert metadata["router_fallback_hops"] == 2
    assert metadata["router_dynamic_pending_route_plan"] == target
    assert metadata["router_dynamic_pending_route_plan"] is not target
    assert metadata["ensemble_decision_id"] == target["decision_id"]
    audit = metadata["router_dynamic_decision"]
    assert audit["decision_id"] == target["decision_id"]
    assert audit["selected_P"] == target["selected_P"]
    assert audit["selected_A"] == target["selected_A"]
    assert audit["retry_parent_decision_id"] == source["decision_id"]
    assert audit["retry_routing"] == target["retry_routing"]
    assert audit["task_analysis_reused"] is True
    assert audit["task_analysis_reuse"] == target["task_analysis_reuse"]
    await retry_stream.aclose()


def test_retry_transition_none_leaves_wrapper_and_metadata_unchanged() -> None:
    source, _ = _valid_retry_plans()
    primary = _RetryPrimary(None)
    metadata: dict[str, Any] = {
        "router_dynamic_pending_route_plan": copy.deepcopy(source),
        "router_dynamic_decision": {"decision_id": source["decision_id"]},
        "ensemble_decision_id": source["decision_id"],
        "router_fallback_hops": 0,
    }
    before = copy.deepcopy(metadata)
    wrapper = _SelectorFallbackProvider(
        primary,
        _StubSelector("same-selector"),
        turn_metadata=metadata,
    )

    assert (
        wrapper.prepare_retry_after_failure(ErrorEvent(message="ordinary failure", code="500"))
        is None
    )
    assert wrapper.primary is primary
    assert metadata == before


def test_invalid_retry_audit_projection_fails_closed_without_metadata_update() -> None:
    source, target = _valid_retry_plans()
    target["decision_id"] = ""
    primary = _RetryPrimary(_retry_transition(source, target, _RetryReplacement()))
    metadata: dict[str, Any] = {
        "router_dynamic_pending_route_plan": copy.deepcopy(source),
        "router_dynamic_decision": {"decision_id": source["decision_id"]},
        "ensemble_decision_id": source["decision_id"],
        "router_fallback_hops": 1,
    }
    before = copy.deepcopy(metadata)
    wrapper = _SelectorFallbackProvider(
        primary,
        _StubSelector("same-selector"),
        turn_metadata=metadata,
    )

    assert (
        wrapper.prepare_retry_after_failure(
            ErrorEvent(
                message="not enough proposers",
                code="ensemble_insufficient_proposers",
            )
        )
        is None
    )
    assert wrapper.primary is primary
    assert metadata == before


def test_retry_transition_exclusion_mismatch_fails_closed() -> None:
    source, target = _valid_retry_plans()
    target["retry_routing"]["excluded_proposer_identities"] = ["openrouter:unrelated-proposer"]
    primary = _RetryPrimary(_retry_transition(source, target, _RetryReplacement()))
    metadata: dict[str, Any] = {
        "router_dynamic_pending_route_plan": copy.deepcopy(source),
        "router_dynamic_decision": {"decision_id": source["decision_id"]},
        "ensemble_decision_id": source["decision_id"],
        "router_fallback_hops": 1,
    }
    before = copy.deepcopy(metadata)
    wrapper = _SelectorFallbackProvider(
        primary,
        _StubSelector("same-selector"),
        turn_metadata=metadata,
    )

    assert (
        wrapper.prepare_retry_after_failure(
            ErrorEvent(
                message="not enough proposers",
                code="ensemble_insufficient_proposers",
            )
        )
        is None
    )
    assert wrapper.primary is primary
    assert metadata == before


PRIMARY_MODEL = "routed-primary"
FALLBACK_MODEL = "fallback-secondary"


class _ChainProvider:
    """Scripted provider link: either fails pre-content or streams a reply."""

    provider_name = "openrouter"

    def __init__(self, model: str, *, fail: bool) -> None:
        self._model = model
        self._fail = fail

    async def chat(
        self,
        messages: list[Any],
        tools: Any = None,
        config: Any = None,
    ) -> AsyncIterator[Any]:
        if self._fail:
            yield ErrorEvent(message="HTTP 404: model not found", code="404")
            return
        yield TextDeltaEvent(text=f"answer-from:{self._model}")
        yield DoneEvent(model=self._model, input_tokens=3, output_tokens=2)

    async def list_models(self) -> list[Any]:
        return []


class _ChainSelector:
    """Two-link chain selector: primary fails, one fallback hop remains."""

    def __init__(self, *, primary_fails: bool) -> None:
        self._primary_fails = primary_fails
        self.current_config = SimpleNamespace(model=PRIMARY_MODEL)

    def clone(self) -> _ChainSelector:
        return self

    def override_model(self, model: str) -> None:
        self.current_config = SimpleNamespace(model=model)

    def resolve(self) -> _ChainProvider:
        return _ChainProvider(PRIMARY_MODEL, fail=self._primary_fails)

    def next_fallback_after_failure(self, exc: Exception) -> _ChainProvider:
        self.current_config = SimpleNamespace(model=FALLBACK_MODEL)
        return _ChainProvider(FALLBACK_MODEL, fail=False)


class _FailingFallbackSelector(_ChainSelector):
    def next_fallback_after_failure(self, exc: Exception) -> _ChainProvider:
        del exc
        raise RuntimeError("selector resolution failed")


class _CloseFailingStream:
    def __init__(self) -> None:
        self._emitted = False

    def __aiter__(self) -> _CloseFailingStream:
        return self

    async def __anext__(self) -> Any:
        if self._emitted:
            raise StopAsyncIteration
        self._emitted = True
        return ErrorEvent(
            message="HTTP 404: model not found",
            code="404",
        )

    async def aclose(self) -> None:
        raise RuntimeError("primary close failed")


class _CloseFailingProvider:
    provider_name = "openrouter"

    def chat(
        self,
        messages: list[Any],
        tools: Any = None,
        config: Any = None,
    ) -> AsyncIterator[Any]:
        del messages, tools, config
        return _CloseFailingStream()

    async def list_models(self) -> list[Any]:
        return []


def _exact_reasoning_length_done() -> DoneEvent:
    done = DoneEvent(
        stop_reason="length",
        output_tokens=16_384,
        reasoning_tokens=16_384,
        reasoning_content="internal",
        provider_usage={"test_usage_receipt": True},
    )
    done.request_started = True
    done.physical_request_count = 1
    return done


class _BudgetScriptProvider:
    provider_name = "openrouter"

    def __init__(self, model: str, streams: list[list[Any]]) -> None:
        self.model = model
        self.streams = streams
        self.calls: list[Any] = []

    def chat(
        self,
        messages: list[Any],
        tools: Any = None,
        config: Any = None,
    ) -> AsyncIterator[Any]:
        del messages, tools
        index = len(self.calls)
        self.calls.append(config)
        events = self.streams[index] if index < len(self.streams) else self.streams[-1]
        return self._stream(events)

    async def _stream(self, events: list[Any]) -> AsyncIterator[Any]:
        for event in events:
            yield event

    async def list_models(self) -> list[Any]:
        return []


class _BudgetChainSelector:
    active_provider_id = "openrouter"

    def __init__(self, providers: list[_BudgetScriptProvider]) -> None:
        self.providers = providers
        self.index = 0
        self.current_config = SimpleNamespace(
            provider="openrouter",
            model=providers[0].model,
        )

    def next_fallback_after_failure(
        self,
        exc: Exception,
    ) -> _BudgetScriptProvider:
        del exc
        self.index += 1
        provider = self.providers[self.index]
        self.current_config = SimpleNamespace(
            provider="openrouter",
            model=provider.model,
        )
        return provider


def _routed_pipeline_fake(routed_model: str) -> Any:
    async def routed_pipeline(
        self: TurnRunner,
        message: str,
        session_key: str,
        provider: Any,
        cloned_selector: Any,
        tool_defs: list[Any],
        base_prompt: str | tuple[str, str],
        attachments: list[dict[str, Any]],
        **_: Any,
    ) -> tuple[TurnContext, Any]:
        return (
            TurnContext(
                message=message,
                session_key=session_key,
                config=self._config,
                provider=provider,
                model=routed_model,
                tool_defs=tool_defs,
                system_prompt=base_prompt,
                attachments=attachments,
                metadata={
                    "routed_tier": "c1",
                    "routed_model": routed_model,
                    "baseline_model": "baseline-expensive",
                    "routing_source": "router",
                    "routing_confidence": 0.9,
                    "savings_pct": 41.0,
                    "savings_max_price_per_m": 3.0,
                    "savings_routed_price_per_m": 0.5,
                },
            ),
            provider,
        )

    return routed_pipeline


async def _run_turn_events(
    monkeypatch: Any,
    *,
    primary_fails: bool,
) -> list[Any]:
    monkeypatch.setattr(TurnRunner, "_run_pipeline", _routed_pipeline_fake(PRIMARY_MODEL))
    runner = TurnRunner(provider_selector=_ChainSelector(primary_fails=primary_fails))
    return [
        event
        async for event in runner.run(
            "hi",
            "agent:main:selector-fallback-e2e",
            tool_context=ToolContext(is_owner=True, caller_kind=CallerKind.CLI),
            history_has_persisted_user=False,
            no_memory_capture=True,
        )
    ]


async def test_precontent_fallback_emits_corrective_router_decision_before_done(
    monkeypatch: Any,
) -> None:
    events = await _run_turn_events(monkeypatch, primary_fails=True)

    router_events = [event for event in events if isinstance(event, RouterDecisionEvent)]
    assert len(router_events) == 2

    initial, corrective = router_events
    assert initial.model == PRIMARY_MODEL
    assert initial.source == "router"
    assert initial.fallback is False

    assert corrective.model == FALLBACK_MODEL
    assert corrective.source == "fallback"
    assert corrective.fallback is True
    assert corrective.savings_pct == 0.0

    done_events = [event for event in events if isinstance(event, EngineDoneEvent)]
    assert len(done_events) == 1
    assert done_events[0].model == FALLBACK_MODEL
    assert done_events[0].routed_model == FALLBACK_MODEL
    assert events.index(corrective) < events.index(done_events[0])


async def test_turn_without_fallback_hop_emits_exactly_one_router_decision(
    monkeypatch: Any,
) -> None:
    events = await _run_turn_events(monkeypatch, primary_fails=False)

    router_events = [event for event in events if isinstance(event, RouterDecisionEvent)]
    assert len(router_events) == 1
    assert router_events[0].model == PRIMARY_MODEL
    assert router_events[0].source == "router"
    assert router_events[0].fallback is False

    done_events = [event for event in events if isinstance(event, EngineDoneEvent)]
    assert len(done_events) == 1
    assert done_events[0].model == PRIMARY_MODEL


async def test_blocked_cross_provider_route_passes_primary_model_to_agent_request(
    monkeypatch: Any,
) -> None:
    foreign_model = "doubao-seed-1-6-251015"

    async def blocked_pipeline(
        self: TurnRunner,
        message: str,
        session_key: str,
        provider: Any,
        cloned_selector: Any,
        tool_defs: list[Any],
        base_prompt: str | tuple[str, str],
        attachments: list[dict[str, Any]],
        **_: Any,
    ) -> tuple[TurnContext, Any]:
        return (
            TurnContext(
                message=message,
                session_key=session_key,
                config=self._config,
                provider=provider,
                model=foreign_model,
                tool_defs=tool_defs,
                system_prompt=base_prompt,
                attachments=attachments,
                metadata={
                    "routed_tier": "c0",
                    "routed_provider": "volcengine",
                    "routed_model": foreign_model,
                    "routing_source": "router",
                    "routing_applied": True,
                    "routed_provider_blocked": "missing_credential",
                    "routed_provider_fallback_reason": "missing_credential",
                    "routed_provider_fallback_provider": "openrouter",
                    "routed_provider_fallback_model": PRIMARY_MODEL,
                    "executed_provider": "openrouter",
                    "executed_model": PRIMARY_MODEL,
                },
            ),
            provider,
        )

    observed_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(TurnRunner, "_run_pipeline", blocked_pipeline)
    runner = TurnRunner(
        provider_selector=_ChainSelector(primary_fails=False),
        provider_call_observer=lambda **payload: observed_calls.append(payload),
    )

    events = [
        event
        async for event in runner.run(
            "hi",
            "agent:main:blocked-cross-provider",
            tool_context=ToolContext(is_owner=True, caller_kind=CallerKind.CLI),
            history_has_persisted_user=False,
            no_memory_capture=True,
        )
    ]

    [router_event] = [
        event for event in events if isinstance(event, RouterDecisionEvent)
    ]
    assert router_event.model == foreign_model
    assert observed_calls
    assert observed_calls[0]["provider_id"] == "openrouter"
    assert observed_calls[0]["model"] == PRIMARY_MODEL

    [done_event] = [event for event in events if isinstance(event, EngineDoneEvent)]
    assert done_event.model == PRIMARY_MODEL
    assert done_event.routed_model == foreign_model


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("budget", "fallback_expected"),
    [
        (0, False),
        (1, True),
    ],
)
async def test_selector_fallback_uses_no_hook_provider_scope_budget(
    budget: int,
    fallback_expected: bool,
) -> None:
    selector = _ChainSelector(primary_fails=True)
    primary = selector.resolve()
    wrapper = _SelectorFallbackProvider(primary, selector)

    assert wrapper.begin_provider_retry_scope(
        "selector-budget",
        max_additional_physical_requests=budget,
    )
    events = [event async for event in wrapper.chat([])]

    if fallback_expected:
        assert any(
            isinstance(event, TextDeltaEvent) and event.text == f"answer-from:{FALLBACK_MODEL}"
            for event in events
        )
        assert wrapper.primary is not primary
    else:
        assert any(isinstance(event, ErrorEvent) for event in events)
        assert not any(isinstance(event, TextDeltaEvent) for event in events)
        assert wrapper.primary is primary
    assert wrapper._retry_scope_local_remaining["selector-budget"] == 0
    assert wrapper.end_provider_retry_scope("selector-budget")


@pytest.mark.asyncio
async def test_selector_resolution_failure_does_not_debit_retry_scope() -> None:
    selector = _FailingFallbackSelector(primary_fails=True)
    primary = selector.resolve()
    wrapper = _SelectorFallbackProvider(primary, selector)
    assert wrapper.begin_provider_retry_scope(
        "selector-resolution-failure",
        max_additional_physical_requests=1,
    )

    events = [event async for event in wrapper.chat([])]

    assert any(isinstance(event, ErrorEvent) for event in events)
    assert wrapper.primary is primary
    assert wrapper._retry_scope_local_remaining["selector-resolution-failure"] == 1
    assert wrapper.end_provider_retry_scope("selector-resolution-failure")


@pytest.mark.asyncio
async def test_primary_close_failure_does_not_debit_retry_scope() -> None:
    selector = _ChainSelector(primary_fails=True)
    primary = _CloseFailingProvider()
    wrapper = _SelectorFallbackProvider(primary, selector)
    assert wrapper.begin_provider_retry_scope(
        "primary-close-failure",
        max_additional_physical_requests=1,
    )

    with pytest.raises(RuntimeError, match="primary close failed"):
        async for _ in wrapper.chat([]):
            pass

    assert wrapper.primary is primary
    assert wrapper._retry_scope_local_remaining["primary-close-failure"] == 1
    assert wrapper.end_provider_retry_scope("primary-close-failure")


@pytest.mark.asyncio
async def test_selector_and_agent_retries_share_three_request_budget() -> None:
    primary = _BudgetScriptProvider(
        "model-a",
        [
            [
                ErrorEvent(
                    message="HTTP 404: model not found",
                    code="404",
                    request_started=True,
                    physical_request_count=1,
                    usage_missing_count=1,
                )
            ]
        ],
    )
    first_fallback = _BudgetScriptProvider(
        "model-b",
        [
            [_exact_reasoning_length_done()],
            [_exact_reasoning_length_done()],
        ],
    )
    second_fallback = _BudgetScriptProvider(
        "model-c",
        [
            [_exact_reasoning_length_done()],
            [
                TextDeltaEvent(text="must-not-run"),
                DoneEvent(
                    stop_reason="stop",
                    input_tokens=1,
                    output_tokens=1,
                ),
            ],
        ],
    )
    selector = _BudgetChainSelector([primary, first_fallback, second_fallback])
    wrapper = _SelectorFallbackProvider(primary, selector)
    agent = Agent(
        provider=wrapper,
        config=AgentConfig(
            thinking=ThinkingLevel.MEDIUM,
            max_provider_retries=3,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    # One selector fallback plus two later Agent dispatches exhaust the
    # run-turn cap. The attempted fourth recovery request never reaches C.
    assert len(primary.calls) == 1
    assert len(first_fallback.calls) == 2
    assert len(second_fallback.calls) == 1
    assert first_fallback.calls[0].allow_provider_stream_fallback is False
    assert first_fallback.calls[1].allow_provider_stream_fallback is False
    assert second_fallback.calls[0].allow_provider_stream_fallback is False
    assert not any(event.kind == "text_delta" and event.text == "must-not-run" for event in events)
    assert any(
        event.kind == "error" and event.code == "provider_retry_budget_exhausted"
        for event in events
    )
