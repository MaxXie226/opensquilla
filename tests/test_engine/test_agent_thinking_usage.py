from __future__ import annotations

from opensquilla.engine.agent import _summarize_model_usage_breakdown


def test_model_usage_summary_keeps_thinking_levels_in_separate_buckets() -> None:
    common = {
        "role": "proposer",
        "label": "proposer_1",
        "provider": "openrouter",
        "requested_provider": "openrouter",
        "model": "model-a",
        "requested_model": "model-a",
        "requested_thinking_level": "high",
        "thinking_policy_version": "thinking-policy-v1",
        "input_tokens": 10,
        "billed_cost": 0.0,
        "cost_source": "none",
    }

    summarized = _summarize_model_usage_breakdown(
        [
            {
                **common,
                "effective_thinking_level": "medium",
                "provider_thinking_level": "medium",
                "thinking_fallback_reason": "provider_rejected_thinking_level",
                "output_tokens": 2,
            },
            {
                **common,
                "effective_thinking_level": "high",
                "provider_thinking_level": "high",
                "thinking_fallback_reason": "",
                "output_tokens": 3,
            },
        ]
    )

    assert len(summarized) == 2
    assert {
        (row["effective_thinking_level"], row["output_tokens"])
        for row in summarized
    } == {("medium", 2), ("high", 3)}
    medium = next(
        row
        for row in summarized
        if row["effective_thinking_level"] == "medium"
    )
    assert medium["thinking_fallback_reason"] == (
        "provider_rejected_thinking_level"
    )
    assert medium["thinking_policy_version"] == "thinking-policy-v1"


def test_model_usage_summary_preserves_legacy_schema_when_policy_is_off() -> None:
    summarized = _summarize_model_usage_breakdown(
        [
            {
                "role": "proposer",
                "label": "proposer_1",
                "provider": "openrouter",
                "requested_provider": "openrouter",
                "model": "model-a",
                "requested_model": "model-a",
                "input_tokens": 10,
                "output_tokens": 2,
                "billed_cost": 0.0,
                "cost_source": "none",
            }
        ]
    )

    assert len(summarized) == 1
    assert not {
        "requested_thinking_level",
        "effective_thinking_level",
        "provider_thinking_level",
        "thinking_fallback_reason",
        "thinking_policy_version",
    }.intersection(summarized[0])


def test_model_usage_summary_does_not_merge_distinct_thinking_audit_rows() -> None:
    common = {
        "role": "proposer",
        "label": "proposer_1",
        "provider": "openrouter",
        "requested_provider": "openrouter",
        "model": "model-a",
        "requested_model": "model-a",
        "effective_thinking_level": "high",
        "provider_thinking_level": "high",
        "input_tokens": 1,
        "billed_cost": 0.0,
        "cost_source": "none",
    }

    summarized = _summarize_model_usage_breakdown(
        [
            {
                **common,
                "requested_thinking_level": "highest",
                "thinking_fallback_reason": "nearest_lower",
                "thinking_policy_version": "thinking-policy-v1",
            },
            {
                **common,
                "requested_thinking_level": "high",
                "thinking_fallback_reason": "",
                "thinking_policy_version": "thinking-policy-v2",
            },
        ]
    )

    assert len(summarized) == 2
