"""Pin actual-vs-requested provider identity separation in usage tracking.

Local runtimes (vLLM, LM Studio, Ollama, …) are free, but the openai_compat
adapter class names itself ``"openai"`` for every deployment it serves. The
configured provider and adapter name are request context, not physical
response evidence.  When a Done event omits its provider, the legacy tracker
must preserve a blank actual provider rather than promote either value.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine.runtime import _SelectorFallbackProvider
from opensquilla.engine.usage import UsageTracker
from opensquilla.provider import ChatConfig, Message
from opensquilla.provider import DoneEvent as ProviderDoneEvent
from opensquilla.provider import TextDeltaEvent as ProviderTextDeltaEvent


class _LocalCompatProvider:
    """Fake openai_compat adapter: its class name is the generic ``openai``.

    This mirrors ``provider/openai.py`` where ``provider_name = "openai"`` is
    shared by every openai_compat deployment (vLLM, LM Studio, …). A single
    text-only turn with no per-model breakdown drives the fallback tracker-add
    branch.
    """

    provider_name = "openai"

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        yield ProviderTextDeltaEvent(text="hello from a local model")
        yield ProviderDoneEvent(
            stop_reason="end_turn",
            input_tokens=1000,
            output_tokens=50,
            billed_cost=0.0,
            model="qwen3-coder:30b",
        )

    async def list_models(self) -> list[Any]:
        return []


def _run_single_turn(
    config: AgentConfig,
    session_key: str,
    *,
    provider: Any | None = None,
) -> UsageTracker:
    tracker = UsageTracker()

    async def run() -> None:
        agent = Agent(
            provider=provider or _LocalCompatProvider(),
            config=config,
            tool_definitions=[],
            tool_handler=None,
            usage_tracker=tracker,
            session_key=session_key,
        )
        async for _ in agent.run_turn("hi"):
            pass

    asyncio.run(run())
    return tracker


def test_fallback_branch_does_not_promote_configured_provider_to_actual() -> None:
    """Configured vLLM identity is not physical response evidence."""
    session_key = "agent:test:webchat:vllm"
    tracker = _run_single_turn(
        AgentConfig(max_iterations=2, provider_id="vllm"),
        session_key,
    )

    usage = tracker.get(session_key)
    assert usage is not None
    assert usage._per_model is not None
    mu = usage._per_model["qwen3-coder:30b"]
    assert mu.provider == ""


def test_fallback_branch_does_not_promote_adapter_name_to_actual() -> None:
    """A generic adapter name is not physical response evidence either."""
    session_key = "agent:test:webchat:default"
    tracker = _run_single_turn(
        AgentConfig(max_iterations=2),
        session_key,
    )

    usage = tracker.get(session_key)
    assert usage is not None
    assert usage._per_model is not None
    mu = usage._per_model["qwen3-coder:30b"]
    assert mu.provider == ""
    assert mu.cost > 0.0


def test_routed_turn_does_not_promote_selector_config_to_actual_provider() -> None:
    """Selector configuration remains non-physical without a response receipt."""

    class _Selector:
        active_provider_id = "deepseek"

    session_key = "agent:test:webchat:routed"
    tracker = _run_single_turn(
        AgentConfig(max_iterations=2, provider_id="openrouter"),
        session_key,
        provider=_SelectorFallbackProvider(_LocalCompatProvider(), _Selector()),
    )

    usage = tracker.get(session_key)
    assert usage is not None
    [deployment] = usage.deployment_breakdown
    assert deployment["provider"] == ""
    assert deployment["model"] == "qwen3-coder:30b"


def test_routed_turn_cost_budget_uses_requested_provider_as_pricing_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requested provider may price a call without becoming actual identity."""
    from opensquilla.engine import pricing

    class _Selector:
        active_provider_id = "deepseek"

    calls: list[tuple[str, str]] = []
    original = pricing.resolve_model_price

    def recording_resolver(model_id: str, provider: str = "") -> Any:
        calls.append((model_id, provider))
        return original(model_id, provider)

    monkeypatch.setattr(pricing, "resolve_model_price", recording_resolver)
    _run_single_turn(
        AgentConfig(
            max_iterations=2,
            provider_id="openrouter",
            max_turn_cost_usd=1000.0,
        ),
        "agent:test:webchat:routed-budget",
        provider=_SelectorFallbackProvider(_LocalCompatProvider(), _Selector()),
    )

    assert ("qwen3-coder:30b", "openrouter") in calls
    assert ("qwen3-coder:30b", "deepseek") not in calls
