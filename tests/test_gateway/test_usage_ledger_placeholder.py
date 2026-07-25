from __future__ import annotations

import pytest

from opensquilla.engine.usage_accounting import (
    UsageCallStart,
    normalize_provider_usage,
    usd_to_nanos,
)
from opensquilla.gateway.usage_ledger_runtime import _completion
from opensquilla.provider.types import DoneEvent


def _call() -> UsageCallStart:
    return UsageCallStart(
        event_id="event-1",
        execution_id="turn-1",
        call_index=1,
        agent_run_id="run-1",
        turn_id="turn-1",
        parent_turn_id=None,
        session_id="session-1",
        session_epoch=0,
        agent_id="main",
        run_kind="agent",
        provider="openrouter",
        model="fallback-model",
        started_at_ms=1_000,
    )


@pytest.mark.parametrize("reported_missing", [1, 2])
def test_fallback_placeholder_is_counted_once_and_keeps_usage_missing_provenance(
    reported_missing: int,
) -> None:
    event = DoneEvent(
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
        usage_missing_count=reported_missing,
    )

    normalized = normalize_provider_usage(
        event,
        default_provider="openrouter",
        default_model="fallback-model",
        completed_at_ms=2_000,
    )
    completion = _completion(_call(), normalized)

    assert len(normalized.items) == 2
    assert normalized.items[0].cost_source == "unavailable"
    assert normalized.items[1].cost_source == "provider_billed"
    assert normalized.missing_usage_entries == reported_missing
    assert normalized.represented_missing_usage_entries == 1
    assert completion.coverage_status == "usage_missing"
    assert completion.missing_cost_entries == reported_missing
    assert completion.billed_cost_nanos == usd_to_nanos("0.01")
