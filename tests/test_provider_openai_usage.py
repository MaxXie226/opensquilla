"""Unit tests for OpenAI-compatible provider usage extraction.

Covers cache_write_tokens detection across the variants we encounter
in the wild:
- DeepSeek: ``prompt_cache_miss_tokens`` is fresh input already counted in
  ``prompt_tokens`` — not a cache write — so it must be excluded.
- Anthropic-via-OpenRouter passthrough: ``cache_creation_input_tokens``.
- Nested under ``prompt_tokens_details`` (some chat-completion providers).
- Absent: returns 0 cleanly.
"""

import hashlib
import json
from decimal import Decimal

import pytest

from opensquilla.provider import DoneEvent
from opensquilla.provider.openai import (
    _apply_benchmark_cache_namespace,
    _benchmark_cache_namespace,
    _billing_result,
    _exact_provider_billing_payload,
    _mark_stream_fallback_cost_unknown,
    _openrouter_is_byok,
    _provider_billed_cost,
    _provider_cost_with_byok_evidence,
    _provider_usage_evidence,
    _ProviderBillingAccumulator,
    _sanitize_openrouter_metadata,
    _usage_diagnostic_done,
    _usage_fields,
    _UsageSnapshotAccumulator,
)


def test_benchmark_cache_namespace_is_fail_closed_and_not_persisted_raw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENSQUILLA_BENCHMARK_CACHE_NAMESPACE", raising=False)
    monkeypatch.setenv("OPENSQUILLA_BENCHMARK_CACHE_NAMESPACE_REQUIRED", "1")
    with pytest.raises(ValueError, match="required but missing"):
        _benchmark_cache_namespace()

    monkeypatch.setenv("OPENSQUILLA_BENCHMARK_CACHE_NAMESPACE", "not-hex")
    with pytest.raises(ValueError, match="64 lowercase hex"):
        _benchmark_cache_namespace()

    raw = "a" * 64
    expected_digest = f"sha256:{hashlib.sha256(raw.encode()).hexdigest()}"
    monkeypatch.setenv("OPENSQUILLA_BENCHMARK_CACHE_NAMESPACE", raw)
    messages = [{"role": "system", "content": "system prompt"}]
    assert (
        _apply_benchmark_cache_namespace(messages, provider_kind="openrouter")
        == expected_digest
    )
    assert messages[0]["content"].startswith(
        f"[OpenSquilla benchmark cache namespace: {raw}]\n"
    )
    evidence = _provider_usage_evidence(
        provider_kind="openrouter",
        usage={"is_byok": False, "cost": 0.1},
        router_metadata={},
        response_ids=["gen-1"],
        cache_namespace_sha256=expected_digest,
    )
    assert evidence["cache_namespace_sha256"] == expected_digest
    assert raw not in json.dumps(evidence, sort_keys=True)


def test_usage_fields_returns_zero_when_usage_missing() -> None:
    assert _usage_fields(None) == (0, 0, 0, 0, 0, 0.0)
    assert _usage_fields({}) == (0, 0, 0, 0, 0, 0.0)


def test_usage_fields_ignores_malformed_token_details() -> None:
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "prompt_tokens_details": ["not", "a", "mapping"],
        "completion_tokens_details": ["not", "a", "mapping"],
    }

    assert _usage_fields(usage) == (100, 10, 0, 0, 0, 0.0)


def test_deepseek_prompt_cache_miss_tokens_are_not_cache_writes() -> None:
    """DeepSeek's prompt_cache_miss_tokens is fresh input already billed inside
    prompt_tokens at the standard rate — it carries no write premium and must
    not be counted as a cache write."""
    usage = {
        "prompt_tokens": 1500,
        "completion_tokens": 200,
        "prompt_cache_hit_tokens": 1100,
        "prompt_cache_miss_tokens": 400,
    }

    input_t, output_t, reasoning_t, cached_t, cache_write_t, billed_cost = _usage_fields(usage)

    assert input_t == 1500
    assert output_t == 200
    assert reasoning_t == 0
    assert cached_t == 1100
    assert cache_write_t == 0
    assert billed_cost == 0.0


def test_usage_fields_extracts_anthropic_cache_creation_via_openrouter() -> None:
    usage = {
        "prompt_tokens": 2000,
        "completion_tokens": 100,
        "prompt_tokens_details": {"cached_tokens": 1500},
        "cache_creation_input_tokens": 456,
        "cost": 0.012,
    }

    _, _, _, cached_t, cache_write_t, billed_cost = _usage_fields(usage)

    assert cached_t == 1500
    assert cache_write_t == 456
    assert billed_cost == 0.012


def test_usage_cost_is_trusted_as_provider_bill_only_for_openrouter() -> None:
    assert _provider_billed_cost("openrouter", 0.012) == (0.012, "provider_billed")
    assert _provider_billed_cost("deepseek", 0.012) == (0.0, "none")
    assert _provider_billed_cost("volcengine", 0.012) == (0.0, "none")


def test_openrouter_cost_requires_explicit_non_byok_evidence() -> None:
    assert _openrouter_is_byok({"is_byok": False}) is False
    assert _openrouter_is_byok({"is_byok": True}) is True
    assert _openrouter_is_byok({}) is None
    assert _openrouter_is_byok({"is_byok": "false"}) is None

    assert _provider_cost_with_byok_evidence(
        "openrouter", 0.012, {"is_byok": False, "cost": 0.012}
    ) == (0.012, "provider_billed")
    assert _provider_cost_with_byok_evidence(
        "openrouter", 0.004, {"is_byok": True}
    ) == (0.0, "openrouter_byok")
    assert _provider_cost_with_byok_evidence(
        "openrouter", 0.012, {}
    ) == (0.0, "provider_billed_unverified")
    assert _provider_cost_with_byok_evidence(
        "openrouter", 0.0, {"is_byok": False, "cost": 0}
    ) == (0.0, "provider_billed")
    assert _provider_cost_with_byok_evidence(
        "openrouter", 0.0, {"is_byok": False}
    ) == (0.0, "openrouter_billing_unverified")


@pytest.mark.parametrize("invalid_cost", [float("nan"), float("inf"), "bad"])
def test_openrouter_cost_falls_back_to_finite_total_cost(invalid_cost: object) -> None:
    usage = {
        "is_byok": False,
        "cost": invalid_cost,
        "total_cost": 0.25,
    }

    assert _usage_fields(usage)[-1] == pytest.approx(0.25)
    assert _provider_cost_with_byok_evidence(
        "openrouter", _usage_fields(usage)[-1], usage
    ) == (0.25, "provider_billed")
    evidence = _provider_usage_evidence(
        provider_kind="openrouter",
        usage=usage,
        router_metadata={"is_byok": False},
        response_ids=["gen-cost-fallback"],
    )
    assert evidence["provider_reported_cost"] == pytest.approx(0.25)


def test_openrouter_router_metadata_is_sanitized_and_persisted() -> None:
    raw = {
        "requested": "qwen/qwen3.7-max",
        "strategy": "direct",
        "attempt": 1,
        "is_byok": False,
        "summary": "must not be persisted",
        "endpoints": {
            "total": 1,
            "available": [
                {
                    "provider": "Alibaba",
                    "model": "qwen/qwen3.7-max-20260520",
                    "selected": True,
                    "private": "drop-me",
                }
            ],
        },
        "attempts": [
            {
                "provider": "Alibaba",
                "model": "qwen/qwen3.7-max-20260520",
                "status": 200,
                "latency": 123,
            }
        ],
        "pipeline": [
            {"type": "context_compression", "name": "middle-out", "data": {"x": 1}}
        ],
    }

    sanitized = _sanitize_openrouter_metadata(raw)
    assert "summary" not in sanitized
    assert "private" not in sanitized["endpoints"]["available"][0]
    assert "latency" not in sanitized["attempts"][0]
    assert "data" not in sanitized["pipeline"][0]
    evidence = _provider_usage_evidence(
        provider_kind="openrouter",
        usage={"is_byok": False, "cost": 0.1},
        router_metadata=raw,
        response_ids=["gen-2", "gen-1", "gen-1"],
    )
    assert evidence["router_metadata"] == sanitized
    assert evidence["response_ids"] == ["gen-1", "gen-2"]
    assert evidence["provider_reported_cost"] == 0.1
    assert "cost" not in evidence
    assert _provider_cost_with_byok_evidence(
        "openrouter", 0.0, {}
    ) == (0.0, "openrouter_billing_unverified")


def test_provider_usage_evidence_is_allowlisted_bounded_and_finite() -> None:
    evidence = _provider_usage_evidence(
        provider_kind="openrouter",
        usage={
            "prompt_tokens": 12,
            "completion_tokens": float("inf"),
            "total_tokens": -1,
            "cost": float("nan"),
            "total_cost": 0.25,
            "is_byok": False,
            "secret": {"api_key": "must-not-survive"},
            "prompt_tokens_details": {
                "cached_tokens": 3,
                "cache_write_tokens": 4,
                "cache_creation_input_tokens": 5,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 6,
                    "private": 999,
                },
                "unknown_detail": 999,
            },
            "server_tool_use": {
                "web_search": 2,
                "negative": -1,
                "infinite": float("inf"),
            },
        },
        router_metadata={
            "requested": "x" * 513,
            "strategy": "direct",
            "is_byok": False,
            "summary": "must-not-survive",
            "attempts": [
                {
                    "provider": "OpenAI",
                    "model": "openai/gpt",
                    "status": 200,
                    "private": {"api_key": "must-not-survive"},
                }
                for _ in range(40)
            ],
        },
        response_ids=[f"gen-{index}" for index in range(40)] + ["x" * 513],
    )

    assert evidence["prompt_tokens"] == 12
    assert evidence["provider_reported_cost"] == 0.25
    assert evidence["is_byok"] is False
    assert evidence["prompt_tokens_details"] == {
        "cached_tokens": 3,
        "cache_write_tokens": 4,
        "cache_creation_input_tokens": 5,
        "cache_creation": {"ephemeral_5m_input_tokens": 6},
    }
    assert evidence["server_tool_use"] == {"web_search": 2}
    assert "completion_tokens" not in evidence
    assert "total_tokens" not in evidence
    assert "secret" not in evidence
    assert "requested" not in evidence["router_metadata"]
    assert "summary" not in evidence["router_metadata"]
    assert len(evidence["router_metadata"]["attempts"]) == 32
    assert len(evidence["response_ids"]) == 32
    assert "must-not-survive" not in json.dumps(evidence, sort_keys=True)


def test_stream_fallback_retains_unknown_physical_request() -> None:
    event = DoneEvent(
        model="deepseek/deepseek-v4-pro",
        input_tokens=10,
        output_tokens=2,
        billed_cost=0.01,
        cost_source="provider_billed",
    )

    marked = _mark_stream_fallback_cost_unknown(
        event,
        provider_kind="openrouter",
        requested_model="deepseek/deepseek-v4-pro",
    )

    assert isinstance(marked, DoneEvent)
    assert marked.cost_source == "mixed"
    assert marked.model_usage_breakdown[0]["role"] == "abandoned_stream_request"
    assert marked.model_usage_breakdown[0]["cost_source"] == "none"
    assert marked.model_usage_breakdown[1]["cost_source"] == "provider_billed"


def test_usage_fields_falls_back_to_prompt_details_cache_creation() -> None:
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "prompt_tokens_details": {
            "cached_tokens": 50,
            "cache_creation_tokens": 30,
        },
    }

    *_, cache_write_t, _ = _usage_fields(usage)
    assert cache_write_t == 30


def test_usage_fields_cache_creation_takes_precedence_over_miss() -> None:
    """If both keys are present, cache_creation_input_tokens wins.

    This matches OpenRouter's documented behaviour when proxying Anthropic
    models — the cache_creation count is the canonical write number.
    """
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 50,
        "cache_creation_input_tokens": 200,
        "prompt_cache_miss_tokens": 9999,
    }

    *_, cache_write_t, _ = _usage_fields(usage)
    assert cache_write_t == 200


def test_usage_fields_handles_only_cached_tokens_no_writes() -> None:
    """OpenAI native: only cached_tokens, no write/miss count → cache_write_tokens=0."""
    usage = {
        "prompt_tokens": 500,
        "completion_tokens": 100,
        "prompt_tokens_details": {"cached_tokens": 200},
    }

    *_, cached_t, cache_write_t, _ = _usage_fields(usage)
    assert cached_t == 200
    assert cache_write_t == 0


def test_usage_fields_extracts_top_level_cached_tokens_alias() -> None:
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 20,
        "cached_tokens": 777,
    }

    *_, cached_t, cache_write_t, _ = _usage_fields(usage)

    assert cached_t == 777
    assert cache_write_t == 0


# ---------------------------------------------------------------------------
# Documented OpenRouter / DeepSeek shapes and zero-precedence safety.
# ---------------------------------------------------------------------------


def test_usage_fields_extracts_deepseek_prompt_cache_hit_tokens_for_reads() -> None:
    """DeepSeek native shape exposes reads at top-level prompt_cache_hit_tokens.

    Without this fallback, sessions using DeepSeek directly (not via OpenRouter,
    which already maps them into prompt_tokens_details.cached_tokens) would
    show 0 cache reads even when a hit happened.
    """
    usage = {
        "prompt_tokens": 1500,
        "completion_tokens": 200,
        "prompt_cache_hit_tokens": 1100,
        "prompt_cache_miss_tokens": 400,
    }

    *_, cached_t, cache_write_t, _ = _usage_fields(usage)
    assert cached_t == 1100
    assert cache_write_t == 0


def test_usage_fields_prompt_tokens_details_cached_takes_precedence_over_top_level_hit() -> None:
    """If both shapes are present, the prompt_tokens_details path is canonical."""
    usage = {
        "prompt_tokens": 1500,
        "completion_tokens": 200,
        "prompt_tokens_details": {"cached_tokens": 900},
        "prompt_cache_hit_tokens": 1100,
    }

    *_, cached_t, _, _ = _usage_fields(usage)
    assert cached_t == 900


def test_usage_fields_extracts_openrouter_cache_write_tokens() -> None:
    """OpenRouter usage docs expose prompt_tokens_details.cache_write_tokens."""
    usage = {
        "prompt_tokens": 2000,
        "completion_tokens": 100,
        "prompt_tokens_details": {
            "cached_tokens": 1500,
            "cache_write_tokens": 350,
        },
    }

    *_, cache_write_t, _ = _usage_fields(usage)
    assert cache_write_t == 350


def test_usage_fields_extracts_dashscope_nested_cache_creation_input_tokens() -> None:
    usage = {
        "prompt_tokens": 2000,
        "completion_tokens": 100,
        "prompt_tokens_details": {
            "cached_tokens": 0,
            "cache_creation_input_tokens": 1500,
            "cache_type": "ephemeral",
        },
    }

    *_, cached_t, cache_write_t, _ = _usage_fields(usage)

    assert cached_t == 0
    assert cache_write_t == 1500


def test_usage_fields_extracts_dashscope_cache_creation_object_tokens() -> None:
    usage = {
        "prompt_tokens": 2000,
        "completion_tokens": 100,
        "prompt_tokens_details": {
            "cached_tokens": 0,
            "cache_creation": {"ephemeral_5m_input_tokens": 1500},
        },
    }

    *_, cache_write_t, _ = _usage_fields(usage)

    assert cache_write_t == 1500


def test_usage_fields_existing_cache_write_field_beats_dashscope_nested_creation() -> None:
    usage = {
        "prompt_tokens": 2000,
        "completion_tokens": 100,
        "prompt_tokens_details": {
            "cached_tokens": 0,
            "cache_write_tokens": 350,
            "cache_creation_input_tokens": 1500,
        },
    }

    *_, cache_write_t, _ = _usage_fields(usage)

    assert cache_write_t == 350


def test_usage_fields_extracts_top_level_cache_write_tokens_alias() -> None:
    """Some proxies expose cache_write_tokens at the top level of usage."""
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "cache_write_tokens": 42,
    }

    *_, cache_write_t, _ = _usage_fields(usage)
    assert cache_write_t == 42


def test_usage_fields_canonical_zero_beats_fallback_nonzero() -> None:
    """Zero from a more-canonical key must NOT be replaced by a non-zero fallback.

    Regression for the truthiness-fallback bug: when the upstream explicitly
    says "cache writes = 0 this turn" and a less-canonical legacy field happens
    to carry a stale value, the canonical zero must win.
    """
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 50,
        "cache_creation_input_tokens": 0,  # canonical, explicit zero
        "prompt_tokens_details": {"cache_write_tokens": 9999},  # fallback — must NOT win here
    }

    *_, cache_write_t, _ = _usage_fields(usage)
    assert cache_write_t == 0


def test_usage_fields_write_priority_orders_prompt_details_over_top_level_alias() -> None:
    """When both OpenRouter's documented prompt_tokens_details.cache_write_tokens
    field and the top-level cache_write_tokens alias are present, the
    prompt_tokens_details field (documented as canonical) wins."""
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 50,
        "prompt_tokens_details": {
            "cached_tokens": 800,
            "cache_write_tokens": 100,
        },
        "cache_write_tokens": 9999,
    }

    *_, cache_write_t, _ = _usage_fields(usage)
    assert cache_write_t == 100


def test_usage_snapshot_accumulator_merges_fields_without_adding_snapshots() -> None:
    usage = _UsageSnapshotAccumulator()
    usage.update(
        {
            "prompt_tokens": 6,
            "completion_tokens": 9,
            "completion_tokens_details": {"reasoning_tokens": 6},
            "prompt_tokens_details": {"cached_tokens": 4},
        }
    )
    usage.update({"prompt_tokens": 7, "completion_tokens": 10})

    assert usage.fields() == (7, 10, 6, 4, 0, 0.0)


def test_usage_snapshot_accumulator_treats_later_zero_as_replacement() -> None:
    usage = _UsageSnapshotAccumulator()
    usage.update(
        {
            "prompt_tokens": 6,
            "completion_tokens": 9,
            "completion_tokens_details": {"reasoning_tokens": 6},
            "prompt_tokens_details": {"cached_tokens": 4},
            "cache_creation_input_tokens": 3,
        }
    )
    usage.update(
        {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "completion_tokens_details": {"reasoning_tokens": 0},
            "prompt_tokens_details": {"cached_tokens": 0},
            "cache_creation_input_tokens": 0,
        }
    )

    assert usage.fields() == (0, 0, 0, 0, 0, 0.0)


def _tokenrhythm_billing_result(
    *,
    pending: object = False,
    cost: object = 0.000021,
    base_url: str = "https://tokenrhythm.studio/v1",
    include_pending: bool = True,
    include_cost: bool = True,
):
    billing = _ProviderBillingAccumulator()
    chunk: dict[str, object] = {}
    if include_pending:
        chunk["billing_pending"] = pending
    if include_cost:
        chunk["cost_cny"] = cost
    billing.update("tokenrhythm", chunk)
    return _billing_result(
        provider_kind="tokenrhythm",
        base_url=base_url,
        usage=_UsageSnapshotAccumulator(),
        billing=billing,
        model="synthetic-model",
    )


def test_tokenrhythm_confirmed_receipt_preserves_cny_and_normalizes_usd() -> None:
    billed_cost, cost_source, receipt = _tokenrhythm_billing_result()

    assert billed_cost == 0.000003011
    assert cost_source == "provider_billed"
    assert receipt is not None
    assert receipt.currency == "CNY"
    assert receipt.status == "confirmed"
    assert receipt.amount_nanos == 21_000
    assert receipt.usd_equivalent_nanos == 3_011
    assert receipt.fx_native_per_usd_nanos == 6_975_000_000


def test_tokenrhythm_native_money_is_reparsed_without_float_rounding() -> None:
    raw = '{"cost_cny":0.123456789499999999,"billing_pending":false}'
    payload = _exact_provider_billing_payload(
        "tokenrhythm",
        {"cost_cny": 0.1234567895, "billing_pending": False},
        raw,
    )

    assert payload["cost_cny"] == Decimal("0.123456789499999999")
    billing = _ProviderBillingAccumulator()
    billing.update("tokenrhythm", payload)
    _, source, receipt = _billing_result(
        provider_kind="tokenrhythm",
        base_url="https://tokenrhythm.studio/v1",
        usage=_UsageSnapshotAccumulator(),
        billing=billing,
        model="synthetic-model",
    )
    assert source == "provider_billed"
    assert receipt is not None
    assert receipt.amount_nanos == 123_456_789


def test_tokenrhythm_confirmed_zero_is_still_provider_billed() -> None:
    billed_cost, cost_source, receipt = _tokenrhythm_billing_result(cost=0)

    assert billed_cost == 0.0
    assert cost_source == "provider_billed"
    assert receipt is not None
    assert receipt.amount_nanos == 0
    assert receipt.usd_equivalent_nanos == 0


@pytest.mark.parametrize("include_cost", [False, True])
def test_tokenrhythm_pending_receipt_is_not_added_to_billed_cost(
    include_cost: bool,
) -> None:
    billed_cost, cost_source, receipt = _tokenrhythm_billing_result(
        pending=True,
        cost=0.000021,
        include_cost=include_cost,
    )

    assert billed_cost == 0.0
    assert cost_source == "none"
    assert receipt is not None
    assert receipt.status == "pending"
    assert receipt.amount_nanos == (21_000 if include_cost else None)
    assert receipt.usd_equivalent_nanos is None


def test_tokenrhythm_pending_receipt_discards_malformed_provisional_amount() -> None:
    billed_cost, cost_source, receipt = _tokenrhythm_billing_result(
        pending=True,
        cost=-1,
    )

    assert billed_cost == 0.0
    assert cost_source == "none"
    assert receipt is not None
    assert receipt.status == "pending"
    assert receipt.amount_nanos is None
    assert receipt.usd_equivalent_nanos is None


def test_tokenrhythm_pending_receipt_discards_out_of_range_provisional_amount() -> None:
    billed_cost, cost_source, receipt = _tokenrhythm_billing_result(
        pending=True,
        cost=10_000_000_000,
    )

    assert billed_cost == 0.0
    assert cost_source == "none"
    assert receipt is not None
    assert receipt.status == "pending"
    assert receipt.amount_nanos is None
    assert receipt.usd_equivalent_nanos is None


@pytest.mark.parametrize(
    ("pending", "cost", "include_pending", "include_cost"),
    [
        (False, 0.1, False, True),
        ("false", 0.1, True, True),
        (False, -0.1, True, True),
        (False, float("nan"), True, True),
        (False, float("inf"), True, True),
        (False, True, True, True),
        (False, "0.1", True, True),
        (False, 10_000_000_000, True, True),
        (False, 1e20, True, True),
        (False, 0.1, True, False),
    ],
)
def test_tokenrhythm_invalid_or_incomplete_confirmed_billing_is_untrusted(
    pending: object,
    cost: object,
    include_pending: bool,
    include_cost: bool,
) -> None:
    billed_cost, cost_source, receipt = _tokenrhythm_billing_result(
        pending=pending,
        cost=cost,
        include_pending=include_pending,
        include_cost=include_cost,
    )

    assert (billed_cost, cost_source, receipt) == (0.0, "none", None)


def test_tokenrhythm_billing_metadata_is_ignored_on_unofficial_host() -> None:
    assert _tokenrhythm_billing_result(base_url="https://example.test/v1") == (
        0.0,
        "none",
        None,
    )


def test_openrouter_positive_usage_cost_produces_usd_receipt() -> None:
    usage = _UsageSnapshotAccumulator()
    usage.update({"cost": 0.012})

    billed_cost, cost_source, receipt = _billing_result(
        provider_kind="openrouter",
        base_url="https://openrouter.ai/api/v1",
        usage=usage,
        billing=_ProviderBillingAccumulator(),
        model="synthetic-model",
    )

    assert billed_cost == 0.012
    assert cost_source == "provider_billed"
    assert receipt is not None
    assert receipt.currency == "USD"
    assert receipt.amount_nanos == 12_000_000
    assert receipt.usd_equivalent_nanos == 12_000_000
    assert receipt.fx_native_per_usd_nanos == 1_000_000_000


def test_openrouter_explicit_zero_usage_cost_produces_usd_receipt() -> None:
    usage = _UsageSnapshotAccumulator()
    usage.update({"cost": 0})

    billed_cost, cost_source, receipt = _billing_result(
        provider_kind="openrouter",
        base_url="https://openrouter.ai/api/v1",
        usage=usage,
        billing=_ProviderBillingAccumulator(),
        model="synthetic-model",
    )

    assert billed_cost == 0.0
    assert cost_source == "provider_billed"
    assert receipt is not None
    assert receipt.status == "confirmed"
    assert receipt.currency == "USD"
    assert receipt.amount_nanos == 0
    assert receipt.usd_equivalent_nanos == 0


def test_openrouter_missing_usage_cost_does_not_produce_receipt() -> None:
    usage = _UsageSnapshotAccumulator()

    assert _billing_result(
        provider_kind="openrouter",
        base_url="https://openrouter.ai/api/v1",
        usage=usage,
        billing=_ProviderBillingAccumulator(),
        model="synthetic-model",
    ) == (0.0, "none", None)


def test_openrouter_error_usage_retains_confirmed_zero_receipt() -> None:
    usage = _UsageSnapshotAccumulator()
    usage.update({"prompt_tokens": 5, "completion_tokens": 2, "cost": 0})

    done = _usage_diagnostic_done(
        provider_kind="openrouter",
        provider_id="openrouter",
        requested_model="google/gemini-test",
        base_url="https://openrouter.ai/api/v1",
        actual_model="google/gemini-test-versioned",
        usage=usage,
        billing=_ProviderBillingAccumulator(),
        usage_evidence_seen=True,
        provider_usage={"is_byok": False, "cost": 0},
        router_metadata={},
        response_ids=["gen-zero-error"],
        cache_namespace_sha256="",
    )

    assert done is not None
    assert done.stop_reason == "error"
    assert done.billed_cost == 0.0
    assert done.cost_source == "provider_billed"
    assert done.billing_receipt is not None
    assert done.billing_receipt.status == "confirmed"
    assert done.billing_receipt.usd_equivalent_nanos == 0
