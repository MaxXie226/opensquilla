from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
import structlog.testing

import opensquilla.provider.ranking_router as ranking_router
from opensquilla.engine.usage_accounting import (
    UsageAccountingScope,
    UsageExecutionContext,
    bind_usage_accounting_scope,
)
from opensquilla.provider.ranking_router import (
    CAPABILITIES,
    DOMAINS,
    TASK_ANALYZER_MODEL_ID,
    TASK_ANALYZER_PROVIDER_ID,
    THINKING_LEVELS,
    DynamicRankingError,
    TaskAnalysisResult,
    TaskAnalyzerStreamCleanupError,
    analyze_task_with_provider,
    build_model_registry_snapshot,
    build_request_context,
    dynamic_output_token_budgets,
    fallback_task_profile,
    load_model_registry_snapshot,
    load_ranking_config,
    mock_user_profile,
    normalize_task_profile,
    rank_models,
    ranking_trace_replay_reasons,
)
from opensquilla.provider.types import ChatConfig, DoneEvent, Message, TextDeltaEvent


def _task_profile(
    *,
    tier: int = 3,
    risk: str = "medium",
    cost: str = "medium",
    latency: str = "normal",
    context: str = "short",
    modalities: list[str] | None = None,
    intent: str = "new_task",
    intent_confidence: float = 1.0,
) -> dict[str, Any]:
    return {
        "capability_dist": {"reasoning": 0.6, "code_generation": 0.4},
        "domain_dist": {"software_engineering": 1.0},
        "tier_dist": {str(tier): 1.0},
        "constraints": {
            "cost": cost,
            "latency": latency,
            "context": context,
            "modality": modalities or ["text"],
            "risk": risk,
        },
        "optional_constraints": {"format": "patch"},
        "session_intent": {"type": intent, "confidence": intent_confidence},
    }


def _analysis(**kwargs: Any) -> TaskAnalysisResult:
    return TaskAnalysisResult(
        profile=_task_profile(**kwargs),
        source="test",
        schema_valid=True,
        confidence=1.0,
    )


def _context(
    *,
    input_tokens: int = 1_000,
    candidate_tokens: int = 1_000,
    aggregator_tokens: int = 1_000,
    last_route: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "routing_budget": {
            "estimated_input_tokens": input_tokens,
            "tool_log_tokens": 0,
            "candidate_output_tokens": candidate_tokens,
            "aggregator_output_tokens": aggregator_tokens,
        },
        "input_modalities": ["text"],
        "last_route": last_route or {},
        "snapshot_hash": "request-context-test",
    }


def _model(
    model_id: str,
    *,
    provider: str = "test-provider",
    vendor: str | None = None,
    family: str | None = None,
    roles: list[str] | None = None,
    status: str = "enabled",
    health: str = "healthy",
    credential_available: bool = True,
    context_window: int = 128_000,
    modalities: list[str] | None = None,
    is_open_source: bool = False,
    is_chinese_model: bool = False,
    capability: float = 0.8,
    aggregator_fit: float = 0.8,
    price: float = 1.0,
    latency_ms: int = 2_000,
) -> dict[str, Any]:
    return {
        "source": "test_registry",
        "runtime": {"thinking": "off"},
        "registry_facts": {
            "model_id": model_id,
            "version": "test-v1",
            "provider": provider,
            "vendor": vendor or provider,
            "family": family or model_id,
            "is_open_source": is_open_source,
            "is_chinese_model": is_chinese_model,
            "status": status,
            "roles": roles or ["proposer", "aggregator"],
            "context_window": context_window,
            "effective_context_bucket": "extra_long",
            "modalities": modalities or ["text"],
            "tools": [],
            "price": {
                "input_per_million": price,
                "output_per_million": price,
            },
            "latency_p50_ms": latency_ms // 2,
            "latency_p95_ms": latency_ms,
            "quota": "available",
            "rate_limit": "available",
            "health": health,
            "credential_available": credential_available,
        },
        "static_profile": {
            "capability_dist_prior": {
                "reasoning": capability,
                "code_generation": capability,
                "format_following": capability,
            },
            "domain_dist_prior": {"software_engineering": capability},
            "tier_dist_prior": {
                "1": capability,
                "2": capability,
                "3": capability,
                "4": capability,
            },
            "role_fit_prior": {
                "proposer": capability,
                "aggregator": aggregator_fit,
            },
        },
        "online_profile": {
            "error_rates": {
                "hallucination": max(0.0, 1.0 - capability),
                "omission": max(0.0, 0.9 - capability),
            }
        },
    }


def _thinking_model(
    model_id: str,
    *,
    thinking_levels: list[str] | None = None,
    thinking_level_mapping: dict[str, str] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    model = _model(model_id, **kwargs)
    levels = (
        thinking_levels if thinking_levels is not None else ["low", "medium", "high", "highest"]
    )
    mapping = (
        thinking_level_mapping
        if thinking_level_mapping is not None
        else {
            "low": "low",
            "medium": "medium",
            "high": "high",
            "highest": "xhigh",
        }
    )
    facts = model["registry_facts"]
    facts.update(
        {
            "supports_reasoning": True,
            "supported_thinking_levels": sorted(set(mapping.values())),
            "thinking_levels": list(levels),
            "thinking_level_mapping": dict(mapping),
        }
    )
    return model


def _snapshot(*models: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "test",
        "snapshot_version": "test-snapshot-v1",
        "models": list(models),
    }


def _decision(
    *models: dict[str, Any],
    analysis: TaskAnalysisResult | None = None,
    context: dict[str, Any] | None = None,
    user_profile: dict[str, Any] | None = None,
    ranking_config: dict[str, Any] | None = None,
    thinking_assignment_enabled: bool = False,
):
    return rank_models(
        task_analysis=analysis or _analysis(),
        user_profile=user_profile or mock_user_profile(),
        request_context=context or _context(),
        registry_snapshot=_snapshot(*models),
        routed_tier="c2",
        routing_confidence=0.9,
        ranking_config=ranking_config,
        ranking_thinking_assignment_enabled=thinking_assignment_enabled,
    )


def _replayable_decision():
    return rank_models(
        task_analysis=_analysis(tier=3),
        user_profile=None,
        request_context=_context(),
        registry_snapshot=_snapshot(
            _model("alpha", capability=0.95, aggregator_fit=0.82),
            _model("beta", capability=0.90, aggregator_fit=0.97),
            _model("gamma", capability=0.85, aggregator_fit=0.88),
        ),
        routed_tier="c2",
        routing_confidence=0.91,
        decision_id="replay-decision",
    )


def test_ranking_trace_embeds_public_frozen_replay_evidence() -> None:
    trace = _replayable_decision().trace

    assert trace["registry_snapshot"]["models"]
    assert trace["request_context"]["snapshot_hash"] == trace["request_context_hash"]
    assert trace["task_profile_pre_escalation"]
    assert ranking_trace_replay_reasons(trace) == []


def test_v3_replay_binds_frozen_aggregator_candidate_chain() -> None:
    decision = rank_models(
        task_analysis=_analysis(tier=3),
        user_profile=None,
        request_context=_context(),
        registry_snapshot=_snapshot(
            _thinking_model("alpha", provider="provider-a", capability=0.95),
            _thinking_model("beta", provider="provider-b", capability=0.90),
            _thinking_model("gamma", provider="provider-c", capability=0.85),
        ),
        routed_tier="c2",
        routing_confidence=0.91,
        decision_id="aggregator-chain-replay",
        ranking_thinking_assignment_enabled=True,
    )
    trace = decision.trace
    assert trace["ranking_version"] == "step2-ranking-v3"

    tampered = json.loads(json.dumps(trace))
    tampered["aggregator_candidates"] = list(reversed(tampered["aggregator_candidates"]))

    assert "g1_frozen_ranker_replay_mismatch_aggregator_candidates" in ranking_trace_replay_reasons(
        tampered
    )


def test_legacy_v2_replay_allows_missing_aggregator_candidate_chain() -> None:
    trace = json.loads(json.dumps(_replayable_decision().trace))
    assert trace["ranking_version"] == "step2-ranking-v2"
    trace.pop("aggregator_candidates")

    assert ranking_trace_replay_reasons(trace) == []


@pytest.mark.parametrize("selection_field", ["selected_P", "selected_A"])
def test_frozen_replay_rejects_valid_pool_selection_swap(
    selection_field: str,
) -> None:
    trace = _replayable_decision().trace
    tampered = json.loads(json.dumps(trace))
    pool = [row["identity"] for row in trace["candidate_pool"]]
    if selection_field == "selected_P":
        tampered[selection_field] = list(reversed(trace[selection_field]))
    else:
        tampered[selection_field] = next(
            identity for identity in pool if identity != trace[selection_field]
        )

    assert f"g1_frozen_ranker_replay_mismatch_{selection_field}" in ranking_trace_replay_reasons(
        tampered
    )


@pytest.mark.parametrize(
    ("evidence", "needle"),
    [
        ({"api_key": "redacted"}, "secret-like field"),
        ({"nested": {"Authorization": "redacted"}}, "secret-like field"),
        ({"public_note": "sk-test-secret"}, "secret-like value"),
    ],
)
def test_ranking_trace_rejects_secret_like_replay_evidence(
    evidence: dict[str, Any],
    needle: str,
) -> None:
    context = _context()
    context["replay_evidence"] = evidence

    with pytest.raises(DynamicRankingError, match=needle):
        rank_models(
            task_analysis=_analysis(tier=3),
            user_profile=None,
            request_context=context,
            registry_snapshot=_snapshot(
                _model("alpha"),
                _model("beta"),
                _model("gamma"),
            ),
            routed_tier="c2",
            routing_confidence=0.9,
        )


def test_packaged_ranking_config_is_versioned_validated_and_isolated() -> None:
    first = load_ranking_config()
    second = load_ranking_config()

    assert first["schema_version"] == "step2-ranking-config-v4"
    assert first["config_version"].startswith("step2-ranking-")
    assert first["task_analyzer"]["max_output_tokens"] == 1_200
    assert first["routing_tiers"]["mapping"] == {"c0": 1, "c1": 2, "c2": 3, "c3": 4}
    assert first["context"]["bucket_min_tokens"]["extra_long"] == 128_000
    assert first["context"]["token_estimation"]["dense_chars_per_token"] == 1
    assert first["validation"]["task_profile_sum_tolerance"] == pytest.approx(0.02)
    assert first["fallback_task_profile"]["capability_dist"]["reasoning"] == 0.50
    assert first["synthetic_model"]["context_window"] == 128_000
    assert first["hard_filter"]["eligible_statuses"] == ["enabled", "canary"]
    assert first["exploration"] == {"enabled": False, "decision_propensity": 1.0}
    assert first["rerank"]["similarity_penalty_weight"] == pytest.approx(0.25)
    first["rerank"]["similarity_penalty_weight"] = 99.0
    assert second["rerank"]["similarity_penalty_weight"] == pytest.approx(0.25)


def test_invalid_ranking_config_fails_before_selection() -> None:
    config = load_ranking_config()
    config["quality"]["task_match_weight"] = 0.90

    with pytest.raises(DynamicRankingError, match="sum to 1"):
        _decision(_model("only"), analysis=_analysis(tier=1), ranking_config=config)


def test_ranking_config_rejects_ambiguous_or_inactive_settings() -> None:
    duplicate_errors = load_ranking_config()
    duplicate_errors["rerank"]["error_dimensions"].append("timeout")

    ambiguous_tiers = load_ranking_config()
    ambiguous_tiers["routing_tiers"]["mapping"]["c3"] = 3

    bool_penalty = load_ranking_config()
    bool_penalty["penalties"]["task_cost_weights"]["low"] = True

    inactive_exploration = load_ranking_config()
    inactive_exploration["exploration"]["enabled"] = True

    for config, message in (
        (duplicate_errors, "cannot contain duplicates"),
        (ambiguous_tiers, "one-to-one"),
        (bool_penalty, "must be numeric"),
        (inactive_exploration, "exploration is reserved"),
    ):
        with pytest.raises(DynamicRankingError, match=message):
            _decision(
                _model("only"),
                analysis=_analysis(tier=1),
                ranking_config=config,
            )


def test_ranking_config_rejects_unknown_or_missing_nested_parameters() -> None:
    unknown = load_ranking_config()
    unknown["rerank"]["similarity"]["capabilty_weight"] = 0.5

    missing = load_ranking_config()
    missing["task_analyzer"].pop("temperature")

    unsupported_protocol_value = load_ranking_config()
    unsupported_protocol_value["penalties"]["task_cost_weights"]["economy"] = 0.1

    for config, message in (
        (unknown, "unknown or missing keys"),
        (missing, "unknown or missing keys"),
        (unsupported_protocol_value, "supported protocol values"),
    ):
        with pytest.raises(DynamicRankingError, match=message):
            _decision(
                _model("only"),
                analysis=_analysis(tier=1),
                ranking_config=config,
            )


def test_packaged_curated_registry_has_versioned_step2_profiles() -> None:
    snapshot = load_model_registry_snapshot()
    model_ids = [model["registry_facts"]["model_id"] for model in snapshot["models"]]

    assert snapshot["snapshot_version"].startswith("curated-openrouter-step2-")
    assert len(snapshot["models"]) == 80
    assert len(set(model_ids)) == len(model_ids)
    assert {
        "poolside/laguna-xs-2.1",
        "tencent/hy3",
        "kwaipilot/kat-coder-air-v2.5",
        "meta-llama/llama-4-scout",
        "kwaipilot/kat-coder-pro-v2.5",
        "minimax/minimax-m3",
        "mistralai/mistral-medium-3-5",
        "openai/gpt-5.6-luna",
        "anthropic/claude-sonnet-5",
        "x-ai/grok-4.5",
        "google/gemini-3.1-pro-preview",
        "anthropic/claude-fable-5",
        "moonshotai/kimi-k3",
        "thinkingmachines/inkling",
        "nvidia/nemotron-3-ultra-550b-a55b",
        "openai/gpt-oss-120b",
        "qwen/qwen3.6-27b",
        "inclusionai/ling-2.6-1t",
        "mistralai/devstral-2512",
        "openai/gpt-oss-20b",
    }.issubset(model_ids)
    for model in snapshot["models"]:
        facts = model["registry_facts"]
        assert facts["model_id"]
        assert facts["provider"] == "openrouter"
        assert facts["roles"]
        assert facts["context_window"] > 0
        assert type(facts["is_open_source"]) is bool
        assert type(facts["is_chinese_model"]) is bool
        assert type(facts["supports_reasoning"]) is bool
        assert type(facts["supports_tools"]) is bool
        thinking_levels = facts["supported_thinking_levels"]
        assert thinking_levels
        assert len(thinking_levels) == len(set(thinking_levels))
        assert set(thinking_levels) <= set(THINKING_LEVELS)
        assert model["runtime"]["thinking"] == thinking_levels[0]
        assert facts["supports_reasoning"] is any(level != "off" for level in thinking_levels)
        assert facts["catalog_verified_at"] == "2026-07-24"
        assert facts["latency_source"] == "curated_estimate"
        assert set(model["static_profile"]["capability_dist_prior"]) == set(CAPABILITIES)
        assert set(model["static_profile"]["domain_dist_prior"]) == set(DOMAINS)
        assert model["static_profile"]["tier_dist_prior"]
        assert model["static_profile"]["role_fit_prior"]["aggregator"] >= 0
        assert model["online_profile"]["source"] == "curated_estimate"

    curated_models = [
        model for model in snapshot["models"] if model["source"] == "curated_openrouter_profile"
    ]
    assert len(curated_models) == 80
    by_model_id = {model["registry_facts"]["model_id"]: model for model in curated_models}
    assert by_model_id["deepseek/deepseek-v4-flash"]["registry_facts"][
        "supported_thinking_levels"
    ] == ["xhigh", "high", "off"]
    assert by_model_id["anthropic/claude-opus-4.8"]["runtime"]["thinking"] == "max"
    assert by_model_id["kwaipilot/kat-coder-pro-v2.5"]["registry_facts"][
        "supported_thinking_levels"
    ] == ["off"]
    assert (
        min(model["registry_facts"]["price"]["input_per_million"] for model in curated_models)
        <= 0.05
    )
    assert (
        max(model["static_profile"]["role_fit_prior"]["proposer"] for model in curated_models)
        >= 0.94
    )


def test_normalize_task_profile_falls_back_on_missing_required_distributions() -> None:
    profile, valid, errors = normalize_task_profile(
        {"constraints": {"risk": "low"}},
        routed_tier="c3",
        request_context=_context(),
    )

    assert valid is False
    assert "invalid_capability_dist" in errors
    assert profile["tier_dist"] == {"4": 1.0}
    assert profile["session_intent"] == {"type": "new_task", "confidence": 0.0}


@pytest.mark.parametrize(
    ("mutate", "expected_error"),
    [
        (
            lambda profile: profile.update(
                capability_dist={"reasoning": 0.5, "code_generation": 0.3}
            ),
            "invalid_capability_dist",
        ),
        (
            lambda profile: profile.update(
                capability_dist={"reasoning": "0.6", "code_generation": 0.4}
            ),
            "invalid_capability_dist",
        ),
        (
            lambda profile: profile["session_intent"].update(confidence=True),
            "invalid_session_intent_confidence",
        ),
    ],
)
def test_normalize_task_profile_rejects_invalid_required_numeric_fields(
    mutate: Any,
    expected_error: str,
) -> None:
    raw_profile = _task_profile(tier=2)
    mutate(raw_profile)

    _, valid, issues = normalize_task_profile(
        raw_profile,
        routed_tier="c1",
        request_context=_context(),
    )

    assert valid is False
    assert expected_error in issues


def test_normalize_task_profile_accepts_configured_distribution_rounding() -> None:
    raw_profile = _task_profile(tier=2)
    raw_profile["capability_dist"] = {"reasoning": 0.60, "code_generation": 0.39}

    profile, valid, issues = normalize_task_profile(
        raw_profile,
        routed_tier="c1",
        request_context=_context(),
    )

    assert valid is True
    assert issues == []
    assert sum(profile["capability_dist"].values()) == pytest.approx(1.0)


def test_normalize_task_profile_repairs_domain_distribution_format() -> None:
    raw_profile = _task_profile(tier=2)
    raw_profile["domain_dist"] = {
        "Software Engineering": "2",
        "unsupported-domain": 1.0,
    }

    profile, valid, issues = normalize_task_profile(
        raw_profile,
        routed_tier="c1",
        request_context=_context(),
    )

    assert valid is True
    assert issues == ["repaired_domain_dist"]
    assert profile["domain_dist"] == {"software_engineering": 1.0}
    assert sum(profile["domain_dist"].values()) == pytest.approx(1.0)
    assert set(profile["domain_dist"]).issubset(DOMAINS)


def test_request_context_uses_bounded_history_and_attachment_facts() -> None:
    context = build_request_context(
        message="current request",
        turn_metadata={
            "router_history_user_texts": ["old-1", "old-2"],
            "router_prev_assistant_text": "previous answer",
        },
        attachments=[{"name": "diagram.png", "media_type": "image/png"}],
        candidate_output_tokens=2_000,
        aggregator_output_tokens=3_000,
    )

    assert context["conversation"]["recent_turns"] == [
        "user: old-1",
        "user: old-2",
        "assistant: previous answer",
    ]
    assert context["input_modalities"] == ["text", "image"]
    assert context["workspace_state"]["referenced_files"] == ["diagram.png"]
    assert len(context["snapshot_hash"]) == 64


@pytest.mark.parametrize(
    "media_type",
    [
        "image/gif",
        "image/jpg",
        "IMAGE/PNG; charset=binary",
        "image/webp",
    ],
)
def test_request_context_normalizes_native_image_mime(media_type: str) -> None:
    context = build_request_context(
        message="review the image",
        turn_metadata={},
        attachments=[{"name": "diagram", "media_type": media_type}],
        candidate_output_tokens=2_000,
        aggregator_output_tokens=3_000,
    )

    assert context["input_modalities"] == ["text", "image"]


def test_request_context_bounds_history_and_projects_attachments_like_runtime() -> None:
    context = build_request_context(
        message="current request",
        turn_metadata={
            "material_estimated_tokens": 12_345,
            "router_dynamic_request_context": {
                "conversation": {
                    "summary": "s" * 5_000,
                    "recent_turns": [f"turn-{index}-" + ("x" * 3_000) for index in range(9)],
                }
            },
        },
        attachments=[
            {"filename": "voice.wav", "mime": "audio/wav"},
            {"name": "clip.mp4", "type": "video/mp4"},
            {"name": "brief.pdf", "media_type": "application/pdf"},
        ],
        candidate_output_tokens=2_000,
        aggregator_output_tokens=3_000,
    )

    assert len(context["conversation"]["summary"]) == 4_000
    assert len(context["conversation"]["recent_turns"]) == 6
    assert context["conversation"]["recent_turns"][0].startswith("turn-3-")
    assert all(len(turn) <= 2_000 for turn in context["conversation"]["recent_turns"])
    assert context["input_modalities"] == ["text"]
    assert context["attachment_refs"] == ["voice.wav", "clip.mp4", "brief.pdf"]
    assert context["routing_budget"]["estimated_input_tokens"] >= 12_345


def test_request_context_limits_and_token_estimation_are_config_driven() -> None:
    config = load_ranking_config()
    config["context"]["request_limits"]["max_recent_turns"] = 2
    config["context"]["request_limits"]["turn_max_chars"] = 12
    config["context"]["token_estimation"]["utf8_bytes_per_token"] = 1

    context = build_request_context(
        message="abcdefghij",
        turn_metadata={
            "router_dynamic_request_context": {
                "conversation": {
                    "recent_turns": ["first-long-turn", "second-long-turn", "third-long-turn"]
                }
            }
        },
        attachments=[],
        candidate_output_tokens=10,
        aggregator_output_tokens=10,
        ranking_config=config,
    )

    assert context["conversation"]["recent_turns"] == ["second-long-", "third-long-t"]
    assert context["routing_budget"]["estimated_input_tokens"] >= 10


def test_request_context_uses_a_conservative_dense_script_token_estimate() -> None:
    ascii_context = build_request_context(
        message="a" * 400,
        turn_metadata={},
        attachments=[],
        candidate_output_tokens=10,
        aggregator_output_tokens=10,
    )
    dense_context = build_request_context(
        message="中" * 400,
        turn_metadata={},
        attachments=[],
        candidate_output_tokens=10,
        aggregator_output_tokens=10,
    )

    ascii_tokens = ascii_context["routing_budget"]["estimated_input_tokens"]
    dense_tokens = dense_context["routing_budget"]["estimated_input_tokens"]
    assert dense_tokens >= ascii_tokens + 250


def test_dynamic_output_token_budgets_do_not_assume_ascii_density() -> None:
    assert dynamic_output_token_budgets(
        configured_output_tokens=0,
        candidate_max_chars=24_000,
    ) == (24_000, 8_192)
    assert dynamic_output_token_budgets(
        configured_output_tokens=20_000,
        candidate_max_chars=6_000,
    ) == (6_000, 20_000)
    assert dynamic_output_token_budgets(
        configured_output_tokens=4_096,
        candidate_max_chars=24_000,
    ) == (24_000, 4_096)
    assert dynamic_output_token_budgets(
        configured_output_tokens=4_096,
        candidate_max_chars=0,
    ) == (4_096, 4_096)

    config = load_ranking_config()
    config["context"]["output_budget"]["default_tokens"] = 77
    config["context"]["token_estimation"]["candidate_chars_per_token"] = 2
    assert dynamic_output_token_budgets(
        configured_output_tokens=0,
        candidate_max_chars=100,
        ranking_config=config,
    ) == (50, 77)


def test_request_context_sanitizes_supplied_state_and_estimates_tool_tokens() -> None:
    context = build_request_context(
        message="review the workspace",
        turn_metadata={
            "router_dynamic_request_context": {
                "secret_unbounded_field": "do-not-forward",
                "tool_state": {
                    "called_tools": [
                        {"name": f"tool-{index}", "arguments": [index]} for index in range(40)
                    ],
                    "tool_results_summary": "result" * 2_000,
                    "failed_tools": [["nested", index] for index in range(40)],
                },
                "workspace_state": {
                    "referenced_files": [{"path": f"src/file-{index}.py"} for index in range(40)],
                    "changed_files": ["changed.py", "changed.py"],
                    "test_results": "failed" * 1_000,
                },
                "intermediate_outputs": {
                    "previous_candidates": [
                        f"candidate-{index}:" + ("candidate" * 500) for index in range(12)
                    ],
                    "current_errors": [f"error-{index}:" + ("error" * 500) for index in range(12)],
                },
                "last_route": {
                    "selected_P": [f"provider:model-{index}" for index in range(20)],
                    "selected_A": "provider:aggregator",
                    "quality_feedback": 2.0,
                    "escalation_level": 99,
                    "raw_prompt": "must-not-survive",
                },
            }
        },
        attachments=[{"name": {"path": "diagram.png"}, "media_type": "image/png"}],
        candidate_output_tokens=8_192,
        aggregator_output_tokens=8_192,
    )

    assert "secret_unbounded_field" not in context
    assert len(context["tool_state"]["called_tools"]) == 32
    assert len(context["tool_state"]["tool_results_summary"]) == 4_000
    assert len(context["workspace_state"]["referenced_files"]) == 32
    assert context["workspace_state"]["changed_files"] == ["changed.py"]
    assert len(context["intermediate_outputs"]["previous_candidates"]) == 8
    assert len(context["last_route"]["selected_P"]) == 8
    assert context["last_route"]["quality_feedback"] == 1.0
    assert context["last_route"]["escalation_level"] == 2
    assert "raw_prompt" not in context["last_route"]
    assert context["routing_budget"]["tool_log_tokens"] > 0
    assert all(isinstance(value, str) for value in context["workspace_state"]["referenced_files"])


def test_fallback_context_bucket_uses_boundary_token_as_the_larger_bucket() -> None:
    profile = fallback_task_profile(
        routed_tier="c1",
        request_context=_context(input_tokens=8_000),
    )

    assert profile["constraints"]["context"] == "medium"


def test_fallback_profile_and_mock_user_are_loaded_from_ranking_config() -> None:
    config = load_ranking_config()
    config["context"]["bucket_min_tokens"]["medium"] = 100
    config["fallback_task_profile"]["capability_dist"] = {"writing": 1.0}
    config["fallback_task_profile"]["risk_by_tier"]["2"] = "high"
    config["mock_user_profile"]["preference"]["cost_sensitivity"] = "high"

    profile = fallback_task_profile(
        routed_tier="c1",
        request_context=_context(input_tokens=100),
        ranking_config=config,
    )
    user = mock_user_profile(config)

    assert profile["capability_dist"] == {"writing": 1.0}
    assert profile["constraints"]["context"] == "medium"
    assert profile["constraints"]["risk"] == "high"
    assert user["preference"]["cost_sensitivity"] == "high"


def test_runtime_anchor_does_not_inherit_unverified_task_modalities() -> None:
    snapshot = build_model_registry_snapshot(
        inherited_provider="test-provider",
        inherited_model="test-vendor/unknown-model",
        routed_tier="c2",
        anchor_modalities=["text"],
        packaged_snapshot={
            "schema_version": "test",
            "snapshot_version": "test-v1",
            "models": [],
        },
    )

    assert snapshot["models"][0]["registry_facts"]["modalities"] == ["text"]


def test_unknown_model_synthesis_is_config_driven() -> None:
    config = load_ranking_config()
    config["synthetic_model"]["context_window"] = 77_777
    config["synthetic_model"]["price_input_per_million"] = 1.25
    config["synthetic_model"]["base_strength_by_tier"]["3"] = 0.42

    snapshot = build_model_registry_snapshot(
        inherited_provider="test-provider",
        inherited_model="vendor/unknown-model-v1",
        routed_tier="c2",
        packaged_snapshot={
            "schema_version": "test",
            "snapshot_version": "test-v1",
            "models": [],
        },
        ranking_config=config,
    )

    anchor = snapshot["models"][0]
    assert anchor["registry_facts"]["context_window"] == 77_777
    assert anchor["registry_facts"]["price"]["input_per_million"] == 1.25
    assert anchor["static_profile"]["capability_dist_prior"]["reasoning"] == 0.42


def test_vendor_qualified_model_does_not_reuse_another_vendor_template() -> None:
    google_template = _model(
        "google/shared-model",
        provider="openrouter",
        vendor="google",
        family="google-shared",
        capability=0.99,
    )
    snapshot = build_model_registry_snapshot(
        inherited_provider="openrouter",
        inherited_model="acme/shared-model",
        routed_tier="c2",
        packaged_snapshot={
            "schema_version": "test",
            "snapshot_version": "test-v1",
            "models": [google_template],
        },
    )

    anchor = snapshot["models"][0]
    assert anchor["registry_facts"]["model_id"] == "acme/shared-model"
    assert anchor["registry_facts"]["vendor"] == "acme"
    assert anchor["registry_facts"]["family"] == "shared-model"


def test_ambiguous_bare_model_name_uses_synthesized_profile() -> None:
    snapshot = build_model_registry_snapshot(
        inherited_provider="openrouter",
        inherited_model="shared-model",
        routed_tier="c2",
        packaged_snapshot={
            "schema_version": "test",
            "snapshot_version": "test-v1",
            "models": [
                _model("google/shared-model", capability=0.99),
                _model("acme/shared-model", capability=0.10),
            ],
        },
    )

    anchor = snapshot["models"][0]
    assert anchor["registry_facts"]["model_id"] == "shared-model"
    assert anchor["static_profile"]["capability_dist_prior"]["reasoning"] == 0.74


def test_operator_candidates_only_use_explicit_aggregator_role_for_aggregation() -> None:
    snapshot = build_model_registry_snapshot(
        inherited_provider="anchor-provider",
        inherited_model="anchor-model",
        routed_tier="c2",
        operator_candidates=[
            {"provider": "provider-a", "model": "model-a", "role": ""},
            {"provider": "provider-b", "model": "model-b", "role": "critic"},
            {
                "provider": "provider-c",
                "model": "model-c",
                "role": "aggregator",
            },
        ],
        packaged_snapshot={
            "schema_version": "test",
            "snapshot_version": "test-v1",
            "models": [],
        },
    )
    by_model = {
        row["registry_facts"]["model_id"]: row["registry_facts"]["roles"]
        for row in snapshot["models"]
    }

    assert by_model["model-a"] == ["proposer"]
    assert by_model["model-b"] == ["proposer"]
    assert by_model["model-c"] == ["aggregator"]


def test_operator_role_overrides_duplicate_routed_anchor_role() -> None:
    snapshot = build_model_registry_snapshot(
        inherited_provider="anchor-provider",
        inherited_model="anchor-model",
        routed_tier="c2",
        operator_candidates=[
            {
                "provider": "ANCHOR-PROVIDER",
                "model": "ANCHOR-MODEL",
                "role": "aggregator",
            }
        ],
        packaged_snapshot={
            "schema_version": "test",
            "snapshot_version": "test-v1",
            "models": [],
        },
    )

    assert len(snapshot["models"]) == 1
    assert snapshot["models"][0]["source"] == "router_anchor"
    assert snapshot["models"][0]["registry_facts"]["roles"] == ["aggregator"]


def test_registry_builder_rejects_malformed_or_duplicate_profile_rows() -> None:
    malformed = {
        "schema_version": "test",
        "snapshot_version": "test-v1",
        "models": ["not-a-model"],
    }
    duplicate = {
        "schema_version": "test",
        "snapshot_version": "test-v1",
        "models": [_model("Vendor/Model"), _model("vendor/model")],
    }

    for snapshot, message in (
        (malformed, "row 0 must be an object"),
        (duplicate, "duplicate model identities"),
    ):
        with pytest.raises(DynamicRankingError, match=message):
            build_model_registry_snapshot(
                inherited_provider="test-provider",
                inherited_model="anchor",
                routed_tier="c1",
                packaged_snapshot=snapshot,
            )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("price", -1.0, "negative price"),
        ("latency", -1, "invalid latency bounds"),
        ("strength", 1.1, "out-of-range capability_dist_prior.reasoning"),
    ],
)
def test_ranking_rejects_malformed_numeric_model_profiles(
    field: str,
    value: float,
    message: str,
) -> None:
    model = _model("malformed")
    if field == "price":
        model["registry_facts"]["price"]["output_per_million"] = value
    elif field == "latency":
        model["registry_facts"]["latency_p95_ms"] = value
    else:
        model["static_profile"]["capability_dist_prior"]["reasoning"] = value

    with pytest.raises(DynamicRankingError, match=message):
        _decision(model, analysis=_analysis(tier=1))


@pytest.mark.parametrize(
    "field",
    [
        "is_open_source",
        "is_chinese_model",
        "supports_reasoning",
        "supports_tools",
    ],
)
def test_ranking_rejects_non_boolean_model_boolean_fact(field: str) -> None:
    model = _model("malformed")
    model["registry_facts"][field] = "false"

    with pytest.raises(DynamicRankingError, match=f"invalid {field}"):
        _decision(model, analysis=_analysis(tier=1))


@pytest.mark.parametrize(
    ("supports_reasoning", "levels", "message"),
    [
        (True, ["high", "turbo"], "invalid supported_thinking_levels"),
        (True, ["high", "high"], "duplicate supported_thinking_levels"),
        (True, ["off"], "no enabled supported_thinking_levels"),
        (False, ["high", "off"], "without reasoning support"),
    ],
)
def test_ranking_rejects_invalid_supported_thinking_levels(
    supports_reasoning: bool,
    levels: list[str],
    message: str,
) -> None:
    model = _model("malformed")
    model["registry_facts"]["supports_reasoning"] = supports_reasoning
    model["registry_facts"]["supported_thinking_levels"] = levels

    with pytest.raises(DynamicRankingError, match=message):
        _decision(model, analysis=_analysis(tier=1))


class _AnalyzerProvider:
    provider_name = "analyzer-test"

    def __init__(
        self,
        response: str | list[str],
        *,
        include_done: bool | list[bool] = True,
    ) -> None:
        self.responses = response if isinstance(response, list) else [response]
        self.include_done = include_done if isinstance(include_done, list) else [include_done]
        self.calls: list[tuple[list[Message], ChatConfig | None]] = []

    async def _stream(
        self,
        response: str,
        *,
        response_id: str,
        include_done: bool,
    ) -> AsyncIterator[Any]:
        yield TextDeltaEvent(text=response)
        if include_done:
            yield DoneEvent(
                model="analyzer-test",
                input_tokens=11,
                output_tokens=7,
                billed_cost=0.012,
                cost_source="provider_billed",
                provider_usage={
                    "is_byok": False,
                    "provider_reported_cost": 0.012,
                    "response_ids": [response_id],
                    "router_metadata": {"is_byok": False},
                },
            )

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append((messages, config))
        attempt = len(self.calls)
        response = self.responses[min(attempt - 1, len(self.responses) - 1)]
        include_done = self.include_done[min(attempt - 1, len(self.include_done) - 1)]
        return self._stream(
            response,
            response_id=f"analyzer-{attempt}",
            include_done=include_done,
        )

    async def list_models(self) -> list[Any]:
        return []


@pytest.mark.asyncio
async def test_task_analyzer_hanging_stream_close_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _HangingStream:
        def __init__(self) -> None:
            self.close_started = False

        def __aiter__(self) -> _HangingStream:
            return self

        async def __anext__(self) -> Any:
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            self.close_started = True
            await asyncio.Event().wait()

    class _HangingProvider:
        provider_name = "hanging-analyzer"
        model = "hanging-model"
        accounts_physical_usage = True

        def __init__(self) -> None:
            self.stream = _HangingStream()

        def chat(
            self,
            messages: list[Message],
            tools: list[Any] | None = None,
            config: ChatConfig | None = None,
        ) -> AsyncIterator[Any]:
            return self.stream

    config = load_ranking_config()
    config["task_analyzer"]["max_retries"] = 0
    monkeypatch.setattr(
        ranking_router,
        "_TASK_ANALYZER_STREAM_CLOSE_TIMEOUT_SECONDS",
        0.01,
    )
    provider = _HangingProvider()

    with pytest.raises(TaskAnalyzerStreamCleanupError):
        await asyncio.wait_for(
            analyze_task_with_provider(
                provider=provider,
                message="classify this",
                user_profile_enabled=False,
                request_context=_context(),
                routed_tier="c1",
                routing_confidence=0.8,
                timeout_seconds=0.01,
                ranking_config=config,
            ),
            timeout=0.2,
        )

    assert provider.stream.close_started is True


@pytest.mark.asyncio
async def test_task_analyzer_missing_aclose_requires_a_terminal_stream() -> None:
    stream = object()

    assert (
        await ranking_router._bounded_close_task_analyzer_stream(
            stream,
            timeout_seconds=0.01,
            require_aclose=False,
        )
        is True
    )
    assert (
        await ranking_router._bounded_close_task_analyzer_stream(
            stream,
            timeout_seconds=0.01,
            require_aclose=True,
        )
        is False
    )


@pytest.mark.asyncio
async def test_task_analyzer_uses_provider_interface_and_validates_json() -> None:
    expected = _task_profile(tier=2)
    provider = _AnalyzerProvider(f"```json\n{json.dumps(expected)}\n```")

    class _UsageTracker:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        def add(self, session_key: str, **kwargs: Any) -> None:
            self.calls.append((session_key, kwargs))

    usage_tracker = _UsageTracker()

    result = await analyze_task_with_provider(
        provider=provider,
        message="implement a parser",
        user_profile_enabled=True,
        request_context=_context(),
        routed_tier="c1",
        routing_confidence=0.8,
        usage_tracker=usage_tracker,
        session_key="agent:main:test",
        analyzer_provider_id=TASK_ANALYZER_PROVIDER_ID,
        analyzer_model_id=TASK_ANALYZER_MODEL_ID,
    )

    assert result.source == "llm_provider"
    assert result.schema_valid is True
    assert result.profile["tier_dist"] == {"2": 1.0}
    assert len(provider.calls) == 1
    assert provider.calls[0][1] is not None
    assert provider.calls[0][1].temperature == 0.0
    assert '"modality":["<allowed modality>"]' in provider.calls[0][1].system
    assert '"session_intent":{"type":"<allowed intent>"' in provider.calls[0][1].system
    assert "research is a domain, not a capability" in provider.calls[0][1].system
    assert provider.calls[0][1].output_json_schema_strict is True
    output_schema = provider.calls[0][1].output_json_schema
    assert output_schema is not None
    assert output_schema["properties"]["domain_dist"]["additionalProperties"] is False
    assert output_schema["properties"]["domain_dist"]["required"] == list(DOMAINS)
    assert result.usage["input_tokens"] == 11
    assert result.usage["billed_cost"] == pytest.approx(0.012)
    assert result.usage["attempt_count"] == 1
    assert result.provider_id == TASK_ANALYZER_PROVIDER_ID
    assert result.model_id == TASK_ANALYZER_MODEL_ID
    assert result.trace()["provider"] == TASK_ANALYZER_PROVIDER_ID
    assert result.trace()["model"] == TASK_ANALYZER_MODEL_ID
    assert usage_tracker.calls[0][0] == "agent:main:test"
    assert usage_tracker.calls[0][1]["output_tokens"] == 7
    analyzer_payload = json.loads(str(provider.calls[0][0][0].content))
    assert analyzer_payload["allowed_constraints"]["risk"] == ["low", "medium", "high"]
    assert analyzer_payload["allowed_session_intents"] == ["new_task", "continue", "redo"]
    # The profile never reaches the analyzer provider, even when one is supplied.
    assert "user_profile" not in analyzer_payload


@pytest.mark.asyncio
async def test_task_analyzer_uses_durable_accounting_and_retains_provider_evidence() -> None:
    class _Sink:
        def __init__(self) -> None:
            self.started: list[Any] = []
            self.finalized: list[tuple[Any, Any]] = []
            self.unknown: list[tuple[Any, str]] = []

        async def start(self, call: Any) -> None:
            self.started.append(call)

        async def finalize(self, call: Any, result: Any) -> None:
            self.finalized.append((call, result))

        async def mark_unknown(self, call: Any, reason: str) -> None:
            self.unknown.append((call, reason))

    sink = _Sink()
    scope = UsageAccountingScope(
        sink=sink,
        context=UsageExecutionContext(
            execution_id="routing-decision-1",
            agent_run_id="routing-decision-1",
            turn_id="turn-1",
            session_id="session-1",
            agent_id="main",
            run_kind="routing",
        ),
    )
    provider = _AnalyzerProvider(json.dumps(_task_profile(tier=2)))

    with bind_usage_accounting_scope(scope):
        result = await analyze_task_with_provider(
            provider=provider,
            message="implement a parser",
            user_profile_enabled=False,
            request_context=_context(),
            routed_tier="c1",
            routing_confidence=0.8,
            analyzer_provider_id=TASK_ANALYZER_PROVIDER_ID,
            analyzer_model_id=TASK_ANALYZER_MODEL_ID,
        )

    assert len(sink.started) == 1
    assert len(sink.finalized) == 1
    assert sink.unknown == []
    assert sink.started[0].provider == TASK_ANALYZER_PROVIDER_ID
    assert sink.finalized[0][1].cost_source == "provider_billed"
    assert result.usage["provider_usage"]["is_byok"] is False
    assert result.usage["provider_usage"]["response_ids"] == ["analyzer-1"]


@pytest.mark.asyncio
async def test_task_analyzer_omits_user_profile_and_correlates_logs() -> None:
    provider = _AnalyzerProvider(json.dumps(_task_profile(tier=2)))

    with structlog.testing.capture_logs() as captured:
        result = await analyze_task_with_provider(
            provider=provider,
            message="implement a parser",
            user_profile_enabled=False,
            request_context=_context(),
            routed_tier="c1",
            routing_confidence=0.8,
            decision_id="decision-without-profile",
        )

    assert result.source == "llm_provider"
    analyzer_payload = json.loads(str(provider.calls[0][0][0].content))
    assert "user_profile" not in analyzer_payload
    analyzer_events = [
        row
        for row in captured
        if str(row["event"]).startswith("llm_ensemble.router_dynamic.task_analyzer_")
    ]
    assert [row["event"] for row in analyzer_events] == [
        "llm_ensemble.router_dynamic.task_analyzer_started",
        "llm_ensemble.router_dynamic.task_analyzer_completed",
    ]
    assert all(row["decision_id"] == "decision-without-profile" for row in analyzer_events)
    assert all(row["user_profile_enabled"] is False for row in analyzer_events)


@pytest.mark.asyncio
async def test_task_analyzer_logs_profile_enabled_without_receiving_profile() -> None:
    provider = _AnalyzerProvider(json.dumps(_task_profile(tier=2)))

    with structlog.testing.capture_logs() as captured:
        await analyze_task_with_provider(
            provider=provider,
            message="implement a parser",
            user_profile_enabled=True,
            request_context=_context(),
            routed_tier="c1",
            routing_confidence=0.8,
            decision_id="decision-with-profile",
        )

    assert "user_profile" not in json.loads(str(provider.calls[0][0][0].content))
    analyzer_events = [
        row
        for row in captured
        if str(row["event"]).startswith("llm_ensemble.router_dynamic.task_analyzer_")
    ]
    assert analyzer_events
    assert all(row["user_profile_enabled"] is True for row in analyzer_events)


@pytest.mark.asyncio
async def test_task_analyzer_chat_parameters_are_loaded_from_ranking_config() -> None:
    config = load_ranking_config()
    config["task_analyzer"]["max_output_tokens"] = 321
    config["task_analyzer"]["temperature"] = 0.2
    config["task_analyzer"]["thinking"] = True
    config["task_analyzer"]["timeout_seconds"] = 7.5
    config["task_analyzer"]["input_max_chars"] = 80
    provider = _AnalyzerProvider(json.dumps(_task_profile(tier=2)))

    result = await analyze_task_with_provider(
        provider=provider,
        message="implement a parser " * 100,
        user_profile_enabled=True,
        request_context=_context(),
        routed_tier="c1",
        routing_confidence=0.8,
        ranking_config=config,
    )

    chat_config = provider.calls[0][1]
    assert result.source == "llm_provider"
    assert chat_config is not None
    assert chat_config.max_tokens == 321
    assert chat_config.temperature == 0.2
    assert chat_config.thinking is True
    assert chat_config.timeout == 7.5
    analyzer_payload = json.loads(str(provider.calls[0][0][0].content))
    assert len(analyzer_payload["task"]) == 80
    assert "truncated for classification" in analyzer_payload["task"]


@pytest.mark.asyncio
async def test_task_analyzer_incomplete_stream_falls_back_even_with_valid_json() -> None:
    result = await analyze_task_with_provider(
        provider=_AnalyzerProvider(json.dumps(_task_profile(tier=2)), include_done=False),
        message="hello",
        user_profile_enabled=True,
        request_context=_context(),
        routed_tier="c2",
        routing_confidence=0.77,
    )

    assert result.source == "router_fallback"
    assert result.schema_valid is False
    assert result.fallback_reason == "RuntimeError"


@pytest.mark.asyncio
async def test_task_analyzer_retries_three_times_before_succeeding() -> None:
    malformed = _task_profile(tier=2)
    malformed["domain_dist"] = {"unsupported-domain": 1.0}
    provider = _AnalyzerProvider(
        [
            json.dumps(malformed),
            "not-json",
            json.dumps(malformed),
            json.dumps(_task_profile(tier=2)),
        ]
    )

    with structlog.testing.capture_logs() as captured:
        result = await analyze_task_with_provider(
            provider=provider,
            message="hello",
            user_profile_enabled=True,
            request_context=_context(),
            routed_tier="c2",
            routing_confidence=0.77,
        )

    assert result.source == "llm_provider"
    assert result.schema_valid is True
    assert len(provider.calls) == 4
    assert result.usage["attempt_count"] == 4
    assert result.usage["input_tokens"] == 44
    assert result.usage["billed_cost"] == pytest.approx(0.048)
    physical_attempts = result.usage["physical_attempts"]
    assert [row["attempt"] for row in physical_attempts] == [1, 2, 3, 4]
    assert [row["provider_usage"]["response_ids"][0] for row in physical_attempts] == [
        "analyzer-1",
        "analyzer-2",
        "analyzer-3",
        "analyzer-4",
    ]
    assert len({row["physical_attempt_id"] for row in physical_attempts}) == 4
    retry_events = [row for row in captured if row["event"].endswith("task_analyzer_retry")]
    assert [row["attempt"] for row in retry_events] == [1, 2, 3]
    assert not any(row["event"].endswith("task_analyzer_fallback") for row in captured)


@pytest.mark.asyncio
async def test_task_analyzer_preserves_unknown_then_exact_retry_attempts() -> None:
    config = load_ranking_config()
    config["task_analyzer"]["max_retries"] = 1
    provider = _AnalyzerProvider(
        [
            json.dumps(_task_profile(tier=2)),
            json.dumps(_task_profile(tier=2)),
        ],
        include_done=[False, True],
    )

    result = await analyze_task_with_provider(
        provider=provider,
        message="hello",
        user_profile_enabled=False,
        request_context=_context(),
        routed_tier="c2",
        routing_confidence=0.77,
        analyzer_provider_id=TASK_ANALYZER_PROVIDER_ID,
        analyzer_model_id=TASK_ANALYZER_MODEL_ID,
        ranking_config=config,
        decision_id="a" * 32,
    )

    assert result.source == "llm_provider"
    assert result.usage["attempt_count"] == 2
    attempts = result.usage["physical_attempts"]
    assert [row["attempt"] for row in attempts] == [1, 2]
    assert attempts[0]["usage_unknown"] is True
    assert attempts[0]["provider"] == ""
    assert attempts[0]["requested_provider"] == TASK_ANALYZER_PROVIDER_ID
    assert attempts[0]["requested_model"] == TASK_ANALYZER_MODEL_ID
    assert attempts[0]["cost_source"] == "none"
    assert attempts[0]["billed_cost"] == 0.0
    assert attempts[1]["provider_usage"]["response_ids"] == ["analyzer-2"]
    assert attempts[1].get("usage_unknown") is not True
    assert len({row["physical_attempt_id"] for row in attempts}) == 2


@pytest.mark.asyncio
async def test_task_analyzer_malformed_output_falls_back_to_tree_router_profile() -> None:
    provider = _AnalyzerProvider("not-json")
    result = await analyze_task_with_provider(
        provider=provider,
        message="hello",
        user_profile_enabled=True,
        request_context=_context(),
        routed_tier="c2",
        routing_confidence=0.77,
    )

    assert result.source == "router_fallback"
    assert result.schema_valid is False
    assert result.profile["tier_dist"] == {"3": 1.0}
    assert result.confidence == pytest.approx(0.77)
    assert len(provider.calls) == 4
    assert result.usage["attempt_count"] == 4
    assert result.usage["billed_cost"] == pytest.approx(0.048)
    assert len(result.usage["physical_attempts"]) == 4


@pytest.mark.asyncio
async def test_task_analyzer_invalid_required_constraint_uses_fallback() -> None:
    malformed = _task_profile(tier=2)
    malformed["constraints"]["risk"] = "catastrophic"

    result = await analyze_task_with_provider(
        provider=_AnalyzerProvider(json.dumps(malformed)),
        message="hello",
        user_profile_enabled=True,
        request_context=_context(),
        routed_tier="c2",
        routing_confidence=0.77,
    )

    assert result.source == "router_fallback"
    assert result.schema_valid is False
    assert result.profile["tier_dist"] == {"3": 1.0}


@pytest.mark.asyncio
async def test_task_analyzer_drops_invalid_optional_fields_without_full_fallback() -> None:
    profile = _task_profile(tier=2)
    profile["optional_constraints"] = {"format": "unsupported-format"}
    profile["analysis_confidence"] = "high"

    result = await analyze_task_with_provider(
        provider=_AnalyzerProvider(json.dumps(profile)),
        message="hello",
        user_profile_enabled=True,
        request_context=_context(),
        routed_tier="c1",
        routing_confidence=0.77,
    )

    assert result.source == "llm_provider"
    assert result.schema_valid is True
    assert result.profile["optional_constraints"] == {}
    assert result.confidence == pytest.approx(0.80)
    assert result.normalization_warnings == (
        "invalid_optional_format",
        "invalid_analysis_confidence",
    )
    assert result.trace()["normalization_warnings"] == [
        "invalid_optional_format",
        "invalid_analysis_confidence",
    ]


@pytest.mark.asyncio
async def test_task_analyzer_cannot_drop_an_actual_input_modality() -> None:
    incomplete = _task_profile(tier=2, modalities=["text"])
    request_context = _context()
    request_context["input_modalities"] = ["text", "image"]

    result = await analyze_task_with_provider(
        provider=_AnalyzerProvider(json.dumps(incomplete)),
        message="review the attached diagram",
        user_profile_enabled=True,
        request_context=request_context,
        routed_tier="c2",
        routing_confidence=0.77,
    )

    assert result.source == "router_fallback"
    assert result.profile["constraints"]["modality"] == ["text", "image"]


def test_hard_filter_records_availability_permission_modality_and_context_reasons() -> None:
    eligible = _model("eligible", roles=["proposer", "aggregator"], modalities=["text", "image"])
    unavailable = _model("unavailable", credential_available=False, modalities=["text", "image"])
    denied = _model("denied", modalities=["text", "image"])
    text_only = _model("text-only", modalities=["text"])
    short_context = _model("short-context", context_window=1_500, modalities=["image"])
    user = mock_user_profile()
    user["permission"]["deny_models"] = ["denied"]

    decision = _decision(
        eligible,
        unavailable,
        denied,
        text_only,
        short_context,
        analysis=_analysis(tier=1, modalities=["image"]),
        context=_context(input_tokens=1_000, candidate_tokens=1_000),
        user_profile=user,
    )
    by_model = {row["model"]: row for row in decision.trace["hard_filter"]["proposer_results"]}

    assert "credential_unavailable" in by_model["unavailable"]["reasons"]
    assert "no_permission" in by_model["denied"]["reasons"]
    assert "modality_mismatch" in by_model["text-only"]["reasons"]
    assert "context_exceeded" in by_model["short-context"]["reasons"]
    assert decision.proposers[0].model_id == "eligible"


def test_runtime_generation_policy_reason_excludes_before_ranking() -> None:
    primary = _model("primary", capability=0.95)
    backup = _model("backup", capability=0.90)
    contrast = _model("contrast", capability=0.85)
    blocked = _model("blocked", capability=1.0)
    blocked["registry_facts"]["runtime_hard_filter_reasons"] = [
        "generation_policy_reasoning_unsupported"
    ]

    decision = _decision(
        primary,
        backup,
        contrast,
        blocked,
        analysis=_analysis(tier=3, latency="interactive"),
    )
    selected = {
        *(model.model_id for model in decision.proposers),
        decision.aggregator.model_id,
    }
    proposer_row = next(
        row
        for row in decision.trace["hard_filter"]["proposer_results"]
        if row["model"] == "blocked"
    )
    aggregator_row = next(
        row
        for row in decision.trace["hard_filter"]["aggregator_results"]
        if row["model"] == "blocked"
    )

    assert "blocked" not in selected
    assert proposer_row["eligible"] is False
    assert aggregator_row["eligible"] is False
    assert proposer_row["reasons"] == ["generation_policy_reasoning_unsupported"]
    assert aggregator_row["reasons"] == ["generation_policy_reasoning_unsupported"]


def test_generation_policy_filter_fails_clearly_when_below_n_min() -> None:
    eligible = _model("eligible", capability=0.95)
    blocked_one = _model("blocked-one", capability=0.90)
    blocked_two = _model("blocked-two", capability=0.85)
    for blocked in (blocked_one, blocked_two):
        blocked["registry_facts"]["runtime_hard_filter_reasons"] = [
            "generation_policy_reasoning_unsupported"
        ]

    with pytest.raises(
        DynamicRankingError,
        match=r"generation-policy filtering left 1 eligible proposer\(s\), fewer than N_min=2",
    ):
        _decision(
            eligible,
            blocked_one,
            blocked_two,
            analysis=_analysis(tier=3, latency="interactive"),
        )


def _profile_with_history(*, positive: list[str], negative: list[str], count: int) -> dict:
    profile = mock_user_profile()
    profile["history"]["positive_model_ids"] = positive
    profile["history"]["negative_model_ids"] = negative
    profile["history"]["feedback_count"] = count
    return profile


def test_history_reorders_candidates_that_task_match_alone_would_not() -> None:
    """The regression that matters: history must be able to change the order.

    With an empty history every model gets the same neutral S_user, so the
    0.15 * S_user term is a uniform offset and cannot reorder anything — the
    profile is inert rather than approximate. A saturated history splits
    S_user across models and the weaker-but-liked model wins.
    """
    liked = _model("liked", capability=0.80, aggregator_fit=0.80)
    disliked = _model("disliked", capability=0.85, aggregator_fit=0.85)

    neutral = _decision(liked, disliked, analysis=_analysis(tier=2))
    assert [m.model_id for m in neutral.proposers][0] == "disliked"

    opinionated = _decision(
        liked,
        disliked,
        analysis=_analysis(tier=2),
        user_profile=_profile_with_history(positive=["liked"], negative=["disliked"], count=20),
    )
    assert [m.model_id for m in opinionated.proposers][0] == "liked"


def test_history_confidence_ramps_in_with_feedback_count() -> None:
    """One click must not swing the ranking to an extreme.

    confidence = min(1, feedback_count / 20), so a single rating moves S_user
    by 1/20th of the full signal — not enough to overturn a task-match gap
    that a saturated history does overturn.
    """
    liked = _model("liked", capability=0.80, aggregator_fit=0.80)
    disliked = _model("disliked", capability=0.85, aggregator_fit=0.85)

    barely = _decision(
        liked,
        disliked,
        analysis=_analysis(tier=2),
        user_profile=_profile_with_history(positive=["liked"], negative=["disliked"], count=1),
    )
    assert [m.model_id for m in barely.proposers][0] == "disliked"


def test_ranking_without_user_profile_bypasses_all_profile_effects() -> None:
    preferred = _model("preferred", capability=0.95, aggregator_fit=0.95)
    backup = _model("backup", capability=0.80, aggregator_fit=0.80)
    contrast = _model("contrast", capability=0.70, aggregator_fit=0.70)
    profile = mock_user_profile()
    profile["permission"]["deny_models"] = ["preferred"]
    profile["permission"]["risk_allowlist"] = ["medium"]
    profile["preference"]["cost_sensitivity"] = "hard_limit"
    profile["preference"]["quality_latency_tradeoff"] = "latency_first"

    enabled = _decision(
        preferred,
        backup,
        contrast,
        analysis=_analysis(tier=3, risk="medium"),
        user_profile=profile,
    )
    risk_blocked_profile = mock_user_profile()
    risk_blocked_profile["permission"]["risk_allowlist"] = ["low"]
    with pytest.raises(DynamicRankingError, match="no proposer"):
        _decision(
            preferred,
            backup,
            contrast,
            analysis=_analysis(tier=3, risk="medium"),
            user_profile=risk_blocked_profile,
        )
    disabled = rank_models(
        task_analysis=_analysis(tier=3, risk="medium"),
        user_profile=None,
        request_context=_context(),
        registry_snapshot=_snapshot(preferred, backup, contrast),
        routed_tier="c2",
        routing_confidence=0.9,
        decision_id="ranking-without-profile",
    )

    assert enabled.trace["user_profile_enabled"] is True
    assert enabled.trace["N_max"] == 2
    assert disabled.trace["decision_id"] == "ranking-without-profile"
    assert disabled.trace["user_profile_enabled"] is False
    assert disabled.trace["user_profile_version"] == ""
    assert disabled.trace["user_profile_source"] == ""
    assert disabled.trace["N_max"] == 3
    disabled_filters = [
        *disabled.trace["hard_filter"]["proposer_results"],
        *disabled.trace["hard_filter"]["aggregator_results"],
    ]
    assert all("no_permission" not in row["reasons"] for row in disabled_filters)
    assert all("risk_not_allowed" not in row["reasons"] for row in disabled_filters)
    assert {row["model"] for row in disabled.trace["model_scores"]} == {
        "preferred",
        "backup",
        "contrast",
    }
    for row in disabled.trace["model_scores"]:
        assert row["S_user"] == 0.0
        assert row["S_qual_clean"] == row["S_match"]
        assert row["cost_weight"] == pytest.approx(0.10)
        assert row["latency_weight"] == pytest.approx(0.08)


def test_availability_filter_covers_registry_health_quota_rate_and_role() -> None:
    healthy = _model("healthy")
    disabled = _model("disabled", status="disabled")
    unhealthy = _model("unhealthy", health="unavailable")
    no_quota = _model("no-quota")
    no_quota["registry_facts"]["quota"] = 0
    limited = _model("limited")
    limited["registry_facts"]["rate_limit"] = "limited"
    aggregator_only = _model("aggregator-only", roles=["aggregator"])

    decision = _decision(
        healthy,
        disabled,
        unhealthy,
        no_quota,
        limited,
        aggregator_only,
        analysis=_analysis(tier=1),
    )
    by_model = {row["model"]: row for row in decision.trace["hard_filter"]["proposer_results"]}

    assert "status_unavailable" in by_model["disabled"]["reasons"]
    assert "health_unavailable" in by_model["unhealthy"]["reasons"]
    assert "quota_exhausted" in by_model["no-quota"]["reasons"]
    assert "rate_limited" in by_model["limited"]["reasons"]
    assert "role_proposer_unsupported" in by_model["aggregator-only"]["reasons"]


def test_hard_filter_availability_states_are_config_driven() -> None:
    config = load_ranking_config()
    config["hard_filter"]["eligible_statuses"].append("maintenance")

    decision = _decision(
        _model("maintenance-model", status="maintenance"),
        analysis=_analysis(tier=1),
        ranking_config=config,
    )

    assert decision.proposers[0].model_id == "maintenance-model"


def test_user_risk_permission_is_a_hard_filter() -> None:
    user = mock_user_profile()
    user["permission"]["risk_allowlist"] = ["low", "medium"]

    with pytest.raises(DynamicRankingError, match="no proposer"):
        _decision(
            _model("eligible-by-model"),
            analysis=_analysis(tier=4, risk="high"),
            user_profile=user,
        )


def test_greedy_selection_prefers_cross_family_complement_over_duplicate_family() -> None:
    primary = _model(
        "primary",
        provider="openrouter",
        vendor="vendor-a",
        family="family-a",
        capability=0.88,
    )
    duplicate = _model(
        "duplicate",
        provider="openrouter",
        vendor="vendor-a",
        family="family-a",
        capability=0.87,
    )
    complement = _model(
        "complement",
        provider="openrouter",
        vendor="vendor-b",
        family="family-b",
        capability=0.86,
    )

    decision = _decision(
        primary,
        duplicate,
        complement,
        analysis=_analysis(tier=3, latency="interactive"),
    )

    assert decision.trace["N_min"] == 2
    assert decision.trace["N_max"] == 2
    assert [model.model_id for model in decision.proposers] == ["primary", "complement"]
    assert decision.trace["selection_steps"][1]["max_similarity"] < 0.75
    assert [row["proposer_count"] for row in decision.trace["aggregator_feasibility"]] == [1, 2]
    assert all(row["eligible_aggregator_ids"] for row in decision.trace["aggregator_feasibility"])
    assert decision.trace["selection_steps"][0]["eligible_aggregator_count"] == 3


def test_rerank_trace_records_quality_floor_exclusions_and_stop_detail() -> None:
    strong = _model("strong", capability=0.90)
    weak = _model("weak", provider="provider-b", capability=0.10)

    decision = _decision(
        strong,
        weak,
        analysis=_analysis(tier=2, risk="low"),
    )

    assert [model.model_id for model in decision.proposers] == ["strong"]
    assert decision.trace["quality_floor_excluded_ids"] == ["provider-b:weak"]
    assert decision.trace["stop_reason"] == "quality_floor_or_pool_exhausted"
    assert decision.trace["stop_detail"] == {
        "quality_floor_excluded_count": 1,
        "remaining_candidate_count": 0,
    }


def test_aggregator_feasibility_filters_once_per_prospective_set_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = tuple(
        _model(
            f"model-{index}",
            provider=f"provider-{index}",
            family=f"family-{index}",
        )
        for index in range(4)
    )
    original = ranking_router._hard_filter_reasons
    aggregator_filter_calls = 0

    def counted_hard_filter(*args: Any, **kwargs: Any):
        nonlocal aggregator_filter_calls
        if kwargs.get("role") == "aggregator":
            aggregator_filter_calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(ranking_router, "_hard_filter_reasons", counted_hard_filter)

    decision = _decision(*models, analysis=_analysis(tier=3))

    prospective_counts = len(decision.trace["aggregator_feasibility"])
    assert prospective_counts == len(decision.proposers)
    assert aggregator_filter_calls == len(models) * (prospective_counts + 1)


def test_rerank_weights_from_json_change_the_selected_proposer_set() -> None:
    primary = _model(
        "primary",
        provider="openrouter",
        vendor="vendor-a",
        family="family-a",
        capability=0.88,
    )
    duplicate = _model(
        "duplicate",
        provider="openrouter",
        vendor="vendor-a",
        family="family-a",
        capability=0.87,
    )
    complement = _model(
        "complement",
        provider="openrouter",
        vendor="vendor-b",
        family="family-b",
        capability=0.86,
    )
    config = load_ranking_config()
    config["config_version"] = "test-no-similarity-penalty-v1"
    config["rerank"]["similarity_penalty_weight"] = 0.0

    decision = _decision(
        primary,
        duplicate,
        complement,
        analysis=_analysis(tier=3, latency="interactive"),
        ranking_config=config,
    )

    assert [model.model_id for model in decision.proposers] == ["primary", "duplicate"]
    assert decision.trace["ranking_config_version"] == config["config_version"]
    assert decision.trace["ranking_parameters"] == (
        ranking_router._legacy_ranking_config_projection(config)
    )
    assert len(decision.trace["ranking_config_hash"]) == 64


def test_cost_and_latency_break_quality_ties_in_proposer_selection() -> None:
    expensive = _model(
        "a-expensive",
        capability=0.82,
        price=40.0,
        latency_ms=30_000,
    )
    efficient = _model(
        "z-efficient",
        provider="provider-b",
        capability=0.82,
        price=0.1,
        latency_ms=1_000,
    )

    decision = _decision(
        expensive,
        efficient,
        analysis=_analysis(tier=1, cost="low", latency="interactive"),
    )

    assert decision.proposers[0].model_id == "z-efficient"
    efficient_score = next(
        row for row in decision.trace["model_scores"] if row["model"] == "z-efficient"
    )
    expensive_score = next(
        row for row in decision.trace["model_scores"] if row["model"] == "a-expensive"
    )
    assert efficient_score["S_base_clean"] > expensive_score["S_base_clean"]


def test_aggregator_is_ranked_after_proposers_with_full_context_need() -> None:
    proposer_a = _model("proposer-a", roles=["proposer"], capability=0.9)
    proposer_b = _model(
        "proposer-b",
        provider="provider-b",
        family="family-b",
        roles=["proposer"],
        capability=0.88,
    )
    short_aggregator = _model(
        "short-aggregator",
        roles=["aggregator"],
        context_window=4_500,
        capability=0.98,
        aggregator_fit=0.99,
    )
    long_aggregator = _model(
        "long-aggregator",
        roles=["aggregator"],
        context_window=20_000,
        capability=0.80,
        aggregator_fit=0.85,
    )

    decision = _decision(
        proposer_a,
        proposer_b,
        short_aggregator,
        long_aggregator,
        analysis=_analysis(tier=3, latency="interactive"),
        context=_context(input_tokens=1_000, candidate_tokens=2_000, aggregator_tokens=1_000),
    )

    assert len(decision.proposers) == 2
    assert decision.aggregator.model_id == "long-aggregator"
    short_filter = next(
        row
        for row in decision.trace["hard_filter"]["aggregator_results"]
        if row["model"] == "short-aggregator"
    )
    assert short_filter["context_need_tokens"] == 6_000
    assert "context_exceeded" in short_filter["reasons"]


def test_continue_applies_weak_stickiness_between_near_equal_models() -> None:
    first = _model("first", capability=0.80)
    previous = _model("previous", provider="provider-b", capability=0.79)
    context = _context(
        last_route={
            "selected_P": ["previous"],
            "selected_A": "previous",
            "quality_feedback": 1.0,
            "escalation_level": 0,
        }
    )

    decision = _decision(
        first,
        previous,
        analysis=_analysis(tier=1, intent="continue"),
        context=context,
    )

    assert decision.proposers[0].model_id == "previous"
    assert decision.trace["session"]["sticky_applied"] is True
    previous_score = next(
        row for row in decision.trace["model_scores"] if row["model"] == "previous"
    )
    assert previous_score["S_session"] == pytest.approx(0.1)


def test_low_confidence_continue_does_not_apply_stickiness() -> None:
    previous = _model("previous", capability=0.79)
    stronger = _model("stronger", provider="provider-b", capability=0.80)
    context = _context(
        last_route={
            "selected_P": ["previous"],
            "selected_A": "previous",
            "quality_feedback": 1.0,
            "escalation_level": 0,
        }
    )

    decision = _decision(
        previous,
        stronger,
        analysis=_analysis(tier=1, intent="continue", intent_confidence=0.4),
        context=context,
    )

    assert decision.proposers[0].model_id == "stronger"
    assert decision.trace["session"]["intent"] == "new_task"
    assert decision.trace["session"]["sticky_applied"] is False


def test_continue_without_a_previous_route_is_treated_as_a_new_task() -> None:
    decision = _decision(
        _model("first"),
        _model("second", provider="provider-b"),
        analysis=_analysis(tier=1, intent="continue"),
        context=_context(),
    )

    assert decision.trace["session"]["intent"] == "new_task"
    assert decision.trace["session"]["sticky_applied"] is False


def test_continue_does_not_claim_stickiness_when_previous_models_are_ineligible() -> None:
    context = _context(
        last_route={
            "selected_P": ["unavailable"],
            "selected_A": "unavailable",
            "quality_feedback": 1.0,
            "escalation_level": 0,
        }
    )

    decision = _decision(
        _model("available"),
        _model("unavailable", provider="provider-b", credential_available=False),
        analysis=_analysis(tier=1, intent="continue"),
        context=context,
    )

    assert decision.trace["session"]["intent"] == "continue"
    assert decision.trace["session"]["sticky_applied"] is False
    assert decision.trace["session"]["adjusted_model_ids"] == []


def test_session_adjustment_applies_to_aggregator_selection() -> None:
    proposer = _model("proposer", roles=["proposer"], capability=0.90)
    previous = _model(
        "previous-aggregator",
        provider="provider-b",
        roles=["aggregator"],
        capability=0.80,
        aggregator_fit=0.80,
    )
    alternative = _model(
        "alternative-aggregator",
        provider="provider-c",
        roles=["aggregator"],
        capability=0.82,
        aggregator_fit=0.82,
    )
    context = _context(
        last_route={
            "selected_P": ["proposer"],
            "selected_A": "previous-aggregator",
            "quality_feedback": 1.0,
            "escalation_level": 0,
        }
    )

    continued = _decision(
        proposer,
        previous,
        alternative,
        analysis=_analysis(tier=1, intent="continue"),
        context=context,
    )
    redone = _decision(
        proposer,
        previous,
        alternative,
        analysis=_analysis(tier=1, intent="redo"),
        context=context,
    )

    assert continued.aggregator.model_id == "previous-aggregator"
    continued_score = next(
        row
        for row in continued.trace["aggregator"]["scores"]
        if row["model"] == "previous-aggregator"
    )
    assert continued_score["S_session"] == pytest.approx(0.1)
    assert redone.aggregator.model_id == "alternative-aggregator"


def test_redo_demotes_previous_models_and_shifts_tier_once() -> None:
    previous = _model("previous", capability=0.86)
    alternative = _model("alternative", provider="provider-b", capability=0.84)
    context = _context(
        last_route={
            "selected_P": ["previous"],
            "selected_A": "previous",
            "quality_feedback": 0.2,
            "escalation_level": 0,
        }
    )

    decision = _decision(
        previous,
        alternative,
        analysis=_analysis(tier=2, intent="redo"),
        context=context,
    )

    assert decision.effective_tier == 3
    assert decision.trace["session"]["tier_shifted"] is True
    assert decision.trace["session"]["escalation_level"] == 1
    assert decision.trace["task_profile_pre_escalation"]["tier_dist"] == {"2": 1.0}
    assert decision.trace["task_profile_post_escalation"]["tier_dist"] == {"3": 1.0}
    previous_score = next(
        row for row in decision.trace["model_scores"] if row["model"] == "previous"
    )
    assert previous_score["S_session"] == pytest.approx(-0.1)
    assert decision.proposers[0].model_id == "alternative"


def test_redo_stops_escalating_at_the_configured_ceiling() -> None:
    context = _context(
        last_route={
            "selected_P": ["previous"],
            "selected_A": "previous",
            "quality_feedback": 0.1,
            "escalation_level": 2,
        }
    )

    decision = _decision(
        _model("previous"),
        _model("alternative", provider="provider-b"),
        analysis=_analysis(tier=3, intent="redo"),
        context=context,
    )

    assert decision.effective_tier == 3
    assert decision.trace["session"]["tier_shifted"] is False
    assert decision.trace["session"]["escalation_level"] == 2


def test_high_risk_shortfall_is_recorded_without_violating_filters() -> None:
    decision = _decision(
        _model("one", provider="provider-a"),
        _model("two", provider="provider-b"),
        analysis=_analysis(tier=4, risk="high"),
    )

    assert decision.trace["N_min"] == 4
    assert len(decision.proposers) == 2
    assert decision.trace["coverage_shortfall"] is True
    assert decision.trace["stop_reason"] == "candidate_pool_exhausted"


def test_no_feasible_aggregator_fails_with_explicit_error() -> None:
    with pytest.raises(DynamicRankingError, match="feasible aggregator"):
        _decision(
            _model("proposer", roles=["proposer"]),
            analysis=_analysis(tier=1),
        )


def test_duplicate_registry_identity_is_rejected_before_scoring() -> None:
    duplicate = _model("Vendor/Duplicate")
    duplicate_case_variant = _model("vendor/duplicate")

    with pytest.raises(DynamicRankingError, match="duplicate model identities"):
        _decision(duplicate, duplicate_case_variant, analysis=_analysis(tier=1))


def test_malformed_registry_row_is_not_silently_dropped() -> None:
    with pytest.raises(DynamicRankingError, match="malformed model row"):
        rank_models(
            task_analysis=_analysis(tier=1),
            user_profile=mock_user_profile(),
            request_context=_context(),
            registry_snapshot={
                "snapshot_version": "test",
                "models": [_model("valid"), "not-a-model"],
            },
            routed_tier="c0",
            routing_confidence=1.0,
        )


def test_ranking_is_deterministic_for_the_same_snapshot() -> None:
    models = (
        _model("a", provider="provider-a"),
        _model("b", provider="provider-b"),
        _model("c", provider="provider-c"),
    )

    first = _decision(*models, analysis=_analysis(tier=3))
    second = _decision(*models, analysis=_analysis(tier=3))

    assert [model.identity for model in first.proposers] == [
        model.identity for model in second.proposers
    ]
    assert first.aggregator.identity == second.aggregator.identity
    assert first.trace["selection_steps"] == second.trace["selection_steps"]
    assert first.trace["registry_snapshot_hash"] == second.trace["registry_snapshot_hash"]
    assert len(first.trace["registry_snapshot_hash"]) == 64
    assert all(len(row["profile_hash"]) == 64 for row in first.trace["candidate_pool"])
    assert all(
        type(row["is_open_source"]) is bool and type(row["is_chinese_model"]) is bool
        for row in first.trace["candidate_pool"]
    )


def test_ranking_emits_the_required_debug_lifecycle_events() -> None:
    with structlog.testing.capture_logs() as captured:
        rank_models(
            task_analysis=_analysis(tier=2),
            user_profile=mock_user_profile(),
            request_context=_context(),
            registry_snapshot=_snapshot(
                _model("a", provider="provider-a"),
                _model("b", provider="provider-b"),
            ),
            routed_tier="c1",
            routing_confidence=0.9,
            decision_id="ranking-log-decision",
        )

    event_names = {row["event"] for row in captured}
    assert {
        "llm_ensemble.router_dynamic.candidate_pool_recorded",
        "llm_ensemble.router_dynamic.model_scores_recorded",
        "llm_ensemble.router_dynamic.proposer_selection_recorded",
        "llm_ensemble.router_dynamic.aggregator_selection_recorded",
        "llm_ensemble.router_dynamic.router_decision_recorded",
    }.issubset(event_names)
    lifecycle = [
        row for row in captured if str(row["event"]).startswith("llm_ensemble.router_dynamic.")
    ]
    assert all(row["decision_id"] == "ranking-log-decision" for row in lifecycle)


def test_enabled_thinking_assignment_emits_dedicated_router_event() -> None:
    with structlog.testing.capture_logs() as captured:
        rank_models(
            task_analysis=_analysis(tier=3),
            user_profile=mock_user_profile(),
            request_context=_context(),
            registry_snapshot=_snapshot(
                _thinking_model("a", provider="provider-a"),
                _thinking_model("b", provider="provider-b"),
            ),
            routed_tier="c2",
            routing_confidence=0.9,
            decision_id="thinking-log-decision",
            ranking_thinking_assignment_enabled=True,
        )

    assignment_event = next(
        row
        for row in captured
        if row["event"] == "llm_ensemble.router_dynamic.thinking_assignment_recorded"
    )
    assert assignment_event["decision_id"] == "thinking-log-decision"
    assert assignment_event["thinking_assignment"]["proposers"]
    assert assignment_event["thinking_assignment"]["aggregator"]
    assert assignment_event["policy_versions"]["thinking"] == ("thinking-policy-v1")


def test_thinking_assignment_is_default_off_and_selection_is_unchanged() -> None:
    models = (
        _thinking_model("alpha", provider="provider-a", capability=0.95),
        _thinking_model("beta", provider="provider-b", capability=0.90),
        _thinking_model("gamma", provider="provider-c", capability=0.85),
    )

    disabled = _decision(*models, analysis=_analysis(tier=3))
    enabled = _decision(
        *models,
        analysis=_analysis(tier=3),
        thinking_assignment_enabled=True,
    )

    assert "ranking_thinking_assignment_enabled" not in disabled.trace
    assert "thinking_assignment" not in disabled.trace
    assert "assignment_reasons" not in disabled.trace
    assert all(model.requested_thinking_level is None for model in disabled.proposers)
    assert disabled.aggregator.requested_thinking_level is None
    assert [model.identity for model in disabled.proposers] == [
        model.identity for model in enabled.proposers
    ]
    assert disabled.aggregator.identity == enabled.aggregator.identity
    assert disabled.trace["model_scores"] == enabled.trace["model_scores"]
    assert disabled.trace["selection_steps"] == enabled.trace["selection_steps"]


def test_disabled_thinking_assignment_preserves_exact_legacy_trace_shape() -> None:
    current_config = load_ranking_config()
    current_snapshot = {
        "schema_version": "step2-model-registry-v2",
        "snapshot_version": "curated-openrouter-step2-2026-07-27.1",
        "models": [
            _thinking_model("alpha", provider="provider-a", capability=0.95),
            _thinking_model("beta", provider="provider-b", capability=0.90),
            _thinking_model("gamma", provider="provider-c", capability=0.85),
        ],
    }
    legacy_config = ranking_router._legacy_ranking_config_projection(current_config)
    legacy_snapshot = ranking_router._legacy_registry_snapshot_projection(current_snapshot)
    common = {
        "task_analysis": _analysis(tier=3),
        "user_profile": mock_user_profile(),
        "request_context": _context(),
        "routed_tier": "c2",
        "routing_confidence": 0.9,
        "decision_id": "legacy-shape",
    }

    disabled = rank_models(
        **common,
        registry_snapshot=current_snapshot,
        ranking_config=current_config,
    )
    legacy = rank_models(
        **common,
        registry_snapshot=legacy_snapshot,
        ranking_config=legacy_config,
    )

    assert disabled.trace == legacy.trace
    assert disabled.trace["ranking_version"] == "step2-ranking-v2"
    assert (
        disabled.trace["ranking_config_hash"]
        == "a8addcdefa04349209c20e97ca5851ed0f5ca55646c9d0c5badc5d32dd7ef10c"
    )
    for field in (
        "ranking_thinking_assignment_enabled",
        "thinking_policy_version",
        "thinking_assignment",
        "thinking_assignment_details",
        "assignment_reasons",
        "unsupported_level_fallbacks",
        "policy_versions",
    ):
        assert field not in disabled.trace
    assert all(
        "thinking_levels" not in row and "thinking_level_mapping" not in row
        for row in disabled.trace["candidate_pool"]
    )


def test_legacy_v3_ranking_config_remains_usable_only_when_assignment_is_off() -> None:
    legacy = load_ranking_config()
    legacy["schema_version"] = "step2-ranking-config-v3"
    legacy["config_version"] = "step2-ranking-legacy-test"
    legacy.pop("thinking_assignment")
    models = (
        _thinking_model("alpha", provider="provider-a"),
        _thinking_model("beta", provider="provider-b"),
    )

    decision = _decision(
        *models,
        analysis=_analysis(tier=1),
        ranking_config=legacy,
    )

    assert decision.trace["ranking_config_schema_version"] == "step2-ranking-config-v3"
    with pytest.raises(DynamicRankingError, match="requires step2-ranking-config-v4"):
        _decision(
            *models,
            analysis=_analysis(tier=1),
            ranking_config=legacy,
            thinking_assignment_enabled=True,
        )


@pytest.mark.parametrize(
    ("tier", "proposer_level", "aggregator_level"),
    [
        (1, "low", "medium"),
        (2, "medium", "high"),
        (3, "high", "highest"),
        (4, "highest", "highest"),
    ],
)
def test_thinking_policy_maps_tiers_and_aggregator_step(
    tier: int,
    proposer_level: str,
    aggregator_level: str,
) -> None:
    policy = ranking_router._thinking_assignment_policy(load_ranking_config())
    profile = _task_profile(tier=tier)

    proposer, _, _ = ranking_router._thinking_target_for_role(
        role="proposer",
        effective_tier=tier,
        task_profile=profile,
        session_trace={"intent": "new_task"},
        policy=policy,
    )
    aggregator, _, _ = ranking_router._thinking_target_for_role(
        role="aggregator",
        effective_tier=tier,
        task_profile=profile,
        session_trace={"intent": "new_task"},
        policy=policy,
    )

    assert proposer == proposer_level
    assert aggregator == aggregator_level


@pytest.mark.parametrize(
    ("tier_dist", "expected"),
    [
        ({"1": 0.51, "2": 0.49}, 1),
        ({"1": 0.50, "2": 0.50}, 2),
    ],
)
def test_effective_tier_uses_half_up_rounding(
    tier_dist: dict[str, float],
    expected: int,
) -> None:
    profile = _task_profile(tier=1)
    profile["tier_dist"] = tier_dist

    assert ranking_router._effective_tier(profile, load_ranking_config()) == expected


def test_enabled_thinking_policy_rejects_non_half_up_tier_rounding() -> None:
    config = load_ranking_config()
    config["proposer_count"]["effective_tier_rounding_offset"] = 0.25
    models = (
        _thinking_model("alpha", provider="provider-a"),
        _thinking_model("beta", provider="provider-b"),
    )

    disabled = _decision(*models, ranking_config=config)
    assert disabled.trace["ranking_version"] == "step2-ranking-v2"
    with pytest.raises(
        DynamicRankingError,
        match="effective_tier_rounding_offset to be 0.5",
    ):
        _decision(
            *models,
            ranking_config=config,
            thinking_assignment_enabled=True,
        )


def test_thinking_policy_applies_risk_floor_before_single_resource_downshift() -> None:
    policy = ranking_router._thinking_assignment_policy(load_ranking_config())
    both_constrained = _task_profile(
        tier=4,
        cost="hard_limit",
        latency="interactive",
    )
    high_risk = _task_profile(
        tier=2,
        risk="high",
        cost="low",
        latency="hard_timeout",
    )

    constrained_level, constrained_reasons, _ = ranking_router._thinking_target_for_role(
        role="proposer",
        effective_tier=4,
        task_profile=both_constrained,
        session_trace={"intent": "new_task"},
        policy=policy,
    )
    risk_level, risk_reasons, risk_floor = ranking_router._thinking_target_for_role(
        role="proposer",
        effective_tier=2,
        task_profile=high_risk,
        session_trace={"intent": "new_task"},
        policy=policy,
    )

    assert constrained_level == "high"
    assert sum("resource_" in reason for reason in constrained_reasons) == 1
    assert risk_floor == "high"
    assert risk_level == "high"
    assert any("risk_high_floor_high" in reason for reason in risk_reasons)
    assert any("downshift_blocked" in reason for reason in risk_reasons)


def test_redo_does_not_apply_a_second_thinking_level_shift() -> None:
    policy = ranking_router._thinking_assignment_policy(load_ranking_config())
    profile = _task_profile(tier=3, intent="redo")

    new_level, _, _ = ranking_router._thinking_target_for_role(
        role="proposer",
        effective_tier=3,
        task_profile=profile,
        session_trace={"intent": "new_task"},
        policy=policy,
    )
    redo_level, redo_reasons, _ = ranking_router._thinking_target_for_role(
        role="proposer",
        effective_tier=3,
        task_profile=profile,
        session_trace={"intent": "redo"},
        policy=policy,
    )

    assert redo_level == new_level == "high"
    assert "redo_uses_session_adjusted_tier_only" in redo_reasons


def test_unsupported_thinking_level_uses_deterministic_nearest_tie_breaks() -> None:
    policy = ranking_router._thinking_assignment_policy(load_ranking_config())
    model = ranking_router._normalize_model(
        _thinking_model(
            "partial",
            thinking_levels=["low", "high"],
            thinking_level_mapping={"low": "low", "high": "high"},
        ),
        load_ranking_config(),
        thinking_policy=policy,
    )

    normal, normal_detail, _ = ranking_router._resolve_model_thinking_level(
        model,
        role="proposer",
        requested_level="medium",
        reasons=[],
        risk_floor=None,
        policy=policy,
    )
    high_risk, high_risk_detail, _ = ranking_router._resolve_model_thinking_level(
        model,
        role="proposer",
        requested_level="medium",
        reasons=[],
        risk_floor="low",
        policy=policy,
    )

    assert normal.effective_thinking_level == "low"
    assert normal_detail["fallback_reason"].endswith("_lower")
    assert high_risk.effective_thinking_level == "high"
    assert high_risk_detail["fallback_reason"].endswith("_higher")


def test_provider_rejection_fallbacks_recompute_nearest_remaining_level() -> None:
    policy = ranking_router._thinking_assignment_policy(load_ranking_config())
    model = ranking_router._normalize_model(
        _thinking_model("all-levels"),
        load_ranking_config(),
        thinking_policy=policy,
    )

    assigned, detail, _ = ranking_router._resolve_model_thinking_level(
        model,
        role="proposer",
        requested_level="high",
        reasons=[],
        risk_floor=None,
        policy=policy,
    )

    assert assigned.effective_thinking_level == "high"
    assert [row["unified_level"] for row in assigned.thinking_fallbacks] == [
        "medium",
        "low",
        "highest",
    ]
    assert [row["unified_level"] for row in detail["provider_rejection_fallbacks"]] == [
        "medium",
        "low",
        "highest",
    ]


def test_normal_risk_partial_thinking_support_falls_back_without_hard_filter() -> None:
    decision = _decision(
        _thinking_model(
            "partial",
            thinking_levels=["low"],
            thinking_level_mapping={"low": "low"},
        ),
        analysis=_analysis(tier=1, risk="medium"),
        thinking_assignment_enabled=True,
    )

    assert decision.proposers[0].effective_thinking_level == "low"
    assert decision.aggregator.effective_thinking_level == "low"
    assert decision.trace["unsupported_level_fallbacks"]
    assert (
        decision.trace["hard_filter"]["filter_reason_counts"].get("thinking_level_unavailable")
        is None
    )


def test_high_risk_requires_at_least_high_thinking_support() -> None:
    with pytest.raises(
        DynamicRankingError,
        match="thinking_level_unavailable",
    ) as caught:
        _decision(
            _thinking_model(
                "partial",
                thinking_levels=["low", "medium"],
                thinking_level_mapping={"low": "low", "medium": "medium"},
            ),
            analysis=_analysis(tier=3, risk="high"),
            thinking_assignment_enabled=True,
        )
    assert caught.value.reason == "thinking_level_unavailable"


@pytest.mark.parametrize(
    "mapping",
    [
        {"low": "off"},
        {"low": "high"},
        {"highest": "high"},
    ],
)
def test_registry_v2_rejects_semantically_invalid_thinking_mapping(
    mapping: dict[str, str],
) -> None:
    model = _thinking_model(
        "invalid",
        thinking_levels=list(mapping),
        thinking_level_mapping=mapping,
    )
    model["registry_facts"]["supported_thinking_levels"] = sorted(set(mapping.values()))

    with pytest.raises(
        DynamicRankingError,
        match="no enabled supported_thinking_levels|semantically invalid",
    ):
        ranking_router._validate_registry_snapshot(
            {
                "schema_version": "step2-model-registry-v2",
                "snapshot_version": "invalid-test",
                "models": [model],
            }
        )


def test_registry_v2_requires_explicit_thinking_contract_fields() -> None:
    with pytest.raises(DynamicRankingError, match="requires thinking_levels"):
        ranking_router._validate_registry_snapshot(
            {
                "schema_version": "step2-model-registry-v2",
                "snapshot_version": "missing-test",
                "models": [_model("missing")],
            }
        )


def test_enabled_assignment_is_complete_auditable_and_role_specific() -> None:
    decision = _decision(
        _thinking_model("alpha", provider="provider-a", capability=0.95),
        _thinking_model("beta", provider="provider-b", capability=0.90),
        _thinking_model("gamma", provider="provider-c", capability=0.85),
        analysis=_analysis(tier=3),
        thinking_assignment_enabled=True,
    )
    assignment = decision.trace["thinking_assignment"]

    assert assignment == {
        "proposers": {model.identity: "high" for model in decision.proposers},
        "aggregator": "highest",
        "thinking_policy_version": "thinking-policy-v1",
    }
    assert decision.aggregator.effective_thinking_level == "highest"
    assert all(model.effective_thinking_level == "high" for model in decision.proposers)
    assert decision.trace["assignment_reasons"]["proposers"]
    assert decision.trace["assignment_reasons"]["aggregator"]
    assert decision.trace["policy_versions"] == {
        "ranking": "step2-ranking-v3",
        "thinking": "thinking-policy-v1",
    }


def test_enabled_thinking_assignment_is_replayable_and_tamper_evident() -> None:
    decision = rank_models(
        task_analysis=_analysis(tier=3),
        user_profile=None,
        request_context=_context(),
        registry_snapshot=_snapshot(
            _thinking_model("alpha", provider="provider-a", capability=0.95),
            _thinking_model("beta", provider="provider-b", capability=0.90),
            _thinking_model("gamma", provider="provider-c", capability=0.85),
        ),
        routed_tier="c2",
        routing_confidence=0.91,
        decision_id="thinking-replay-decision",
        ranking_thinking_assignment_enabled=True,
    )
    trace = decision.trace

    assert ranking_trace_replay_reasons(trace) == []
    tampered = json.loads(json.dumps(trace))
    tampered["thinking_assignment"]["aggregator"] = "low"
    assert "g1_frozen_ranker_replay_mismatch_thinking_assignment" in ranking_trace_replay_reasons(
        tampered
    )


def test_enabled_thinking_assignment_replay_rejects_switch_downgrade() -> None:
    decision = rank_models(
        task_analysis=_analysis(tier=3),
        user_profile=None,
        request_context=_context(),
        registry_snapshot=_snapshot(
            _thinking_model("alpha", provider="provider-a", capability=0.95),
            _thinking_model("beta", provider="provider-b", capability=0.90),
            _thinking_model("gamma", provider="provider-c", capability=0.85),
        ),
        routed_tier="c2",
        routing_confidence=0.91,
        decision_id="thinking-replay-downgrade",
        ranking_thinking_assignment_enabled=True,
    )
    tampered = json.loads(json.dumps(decision.trace))
    tampered.pop("ranking_thinking_assignment_enabled")
    for field_name in (
        "thinking_policy_version",
        "thinking_assignment",
        "thinking_assignment_details",
        "assignment_reasons",
        "unsupported_level_fallbacks",
        "policy_versions",
    ):
        tampered.pop(field_name)

    assert "missing_g1_replay_thinking_assignment_switch" in ranking_trace_replay_reasons(tampered)


def test_registry_builder_never_reuses_native_mapping_across_providers() -> None:
    openrouter_template = _thinking_model(
        "vendor/shared-model",
        provider="openrouter",
    )

    snapshot = build_model_registry_snapshot(
        inherited_provider="direct-provider",
        inherited_model="vendor/shared-model",
        routed_tier="c2",
        packaged_snapshot={
            "schema_version": "step2-model-registry-v2",
            "snapshot_version": "provider-isolation-test",
            "models": [openrouter_template],
        },
    )
    anchor = snapshot["models"][0]

    assert anchor["registry_facts"]["provider"] == "direct-provider"
    assert anchor["registry_facts"]["thinking_levels"] == []
    assert anchor["registry_facts"]["thinking_level_mapping"] == {}
    assert "supported_thinking_levels" not in anchor["registry_facts"]
    assert anchor["static_profile"] == openrouter_template["static_profile"]
    ranking_router._validate_registry_snapshot(snapshot)


def test_packaged_registry_v2_has_valid_unified_thinking_contracts() -> None:
    snapshot = load_model_registry_snapshot()

    assert snapshot["schema_version"] == "step2-model-registry-v2"
    for row in snapshot["models"]:
        facts = row["registry_facts"]
        levels = facts["thinking_levels"]
        mapping = facts["thinking_level_mapping"]
        assert set(mapping) == set(levels)
        assert all(level in {"low", "medium", "high", "highest"} for level in levels)
        assert mapping.get("highest") != "high"


def test_aggregator_recovery_candidates_preserve_frozen_top_three_order() -> None:
    decision = _decision(
        _model(
            "proposer-only",
            roles=["proposer"],
            capability=0.95,
            aggregator_fit=0.1,
        ),
        _model(
            "aggregator-alpha",
            roles=["aggregator"],
            capability=0.92,
            aggregator_fit=0.99,
            price=1.0,
        ),
        _model(
            "aggregator-beta",
            roles=["aggregator"],
            capability=0.90,
            aggregator_fit=0.96,
            price=1.1,
        ),
        _model(
            "aggregator-gamma",
            roles=["aggregator"],
            capability=0.88,
            aggregator_fit=0.93,
            price=1.2,
        ),
        _model(
            "aggregator-delta",
            roles=["aggregator"],
            capability=0.86,
            aggregator_fit=0.90,
            price=1.3,
        ),
        user_profile=None,
    )

    ranked_identities = [row["identity"] for row in decision.trace["aggregator"]["scores"]]
    candidate_identities = [model.identity for model in decision.aggregator_candidates]

    assert len(candidate_identities) == 3
    assert candidate_identities == ranked_identities[:3]
    assert candidate_identities[0] == decision.aggregator.identity
    assert len(set(candidate_identities)) == 3


def test_aggregator_recovery_candidates_do_not_pad_a_small_eligible_pool() -> None:
    decision = _decision(
        _model(
            "proposer-only",
            roles=["proposer"],
            capability=0.95,
            aggregator_fit=0.1,
        ),
        _model(
            "aggregator-alpha",
            roles=["aggregator"],
            capability=0.92,
            aggregator_fit=0.99,
        ),
        _model(
            "aggregator-beta",
            roles=["aggregator"],
            capability=0.90,
            aggregator_fit=0.96,
        ),
        user_profile=None,
    )

    ranked_identities = [row["identity"] for row in decision.trace["aggregator"]["scores"]]
    candidate_identities = [model.identity for model in decision.aggregator_candidates]

    assert len(candidate_identities) == 2
    assert candidate_identities == ranked_identities
    assert len(set(candidate_identities)) == 2
