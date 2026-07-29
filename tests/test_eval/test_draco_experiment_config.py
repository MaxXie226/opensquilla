from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from opensquilla.eval.draco_experiment_config import (
    DracoRunnerConfig,
    load_draco_experiment_config,
    validate_reference_input,
)
from opensquilla.provider.ranking_router import load_model_registry_snapshot

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "configs/benchmarks/draco_b2_g12.json"


def test_default_b2_config_is_g12_derived_quality_first_profile() -> None:
    config = load_draco_experiment_config(DEFAULT_CONFIG).config
    assert config.profile_id == "opensquilla_b2_quality_first_v2"

    assert config.reference.source_commit == ("153e5ff267950b0e285efcdb180cea8724c0471d")
    assert config.reference.group == "G12"
    assert config.reference.profile == "g12_k2_replace_gemini"
    assert config.benchmark_input.sha256 == (
        "1eb4e618c8df8e7f68bded3d2b6f77a541744aa1072eb338835b776183188a8d"
    )
    assert config.benchmark_input.task_count == 10
    assert config.routing.selection_mode == "static_openrouter_b5"
    assert config.routing.skip_single_model_router is True
    assert config.g1_routing is not None
    assert config.g1_routing.profile_id == "draco_g1_formal_registry_all_20260729"
    assert config.g1_routing.selection_mode == "router_dynamic"
    assert config.g1_routing.user_profile_enabled is False
    assert config.g1_routing.candidate_scope == "registry_all"
    assert config.g1_routing.expected_source_registry_snapshot_sha256 == (
        "a7cad0eb0b68c97ab82bf21c336107041bc77a14b3e8931b0e672a345610fe9b"
    )
    assert config.g1_routing.expected_ranking_config_schema_version == "step2-ranking-config-v3"
    assert config.g1_routing.expected_ranking_config_version == "step2-ranking-2026-07-22.1"
    assert config.g1_routing.expected_ranking_config_sha256 == (
        "a8addcdefa04349209c20e97ca5851ed0f5ca55646c9d0c5badc5d32dd7ef10c"
    )
    assert config.g1_routing.expected_proposer_count_max == 5
    assert config.g1_routing.expected_candidate_count is None
    assert config.g1_routing.expected_routes is None
    assert config.g1_routing.expected_routes_sha256 is None
    assert [member.model for member in config.ensemble.proposers] == [
        "deepseek/deepseek-v4-pro",
        "z-ai/glm-5.2",
        "moonshotai/kimi-k2.7-code",
        "qwen/qwen3.7-max",
    ]
    assert config.ensemble.aggregator.model == "z-ai/glm-5.2"
    assert [member.thinking for member in config.ensemble.proposers] == [
        "xhigh",
        "xhigh",
        "high",
        "high",
    ]
    assert config.ensemble.aggregator.thinking == "xhigh"
    assert config.generation.require_highest_thinking is True
    assert config.generation.thinking_budget_tokens == 50_000
    assert config.generation.model_thinking_levels == {
        "anthropic/claude-fable-5": "max",
        "anthropic/claude-opus-4.8": "max",
        "anthropic/claude-sonnet-5": "max",
        "deepseek/deepseek-v4-flash": "xhigh",
        "deepseek/deepseek-v4-pro": "xhigh",
        "google/gemini-3.1-pro-preview": "high",
        "google/gemini-3-flash-preview": "high",
        "google/gemini-3.5-flash": "high",
        "kwaipilot/kat-coder-air-v2.5": "off",
        "kwaipilot/kat-coder-pro-v2.5": "off",
        "meta-llama/llama-4-scout": "off",
        "minimax/minimax-m3": "high",
        "mistralai/mistral-medium-3-5": "high",
        "moonshotai/kimi-k2.7-code": "high",
        "openai/gpt-5.5": "xhigh",
        "openai/gpt-5.5-pro": "xhigh",
        "openai/gpt-5.6-sol": "max",
        "poolside/laguna-xs-2.1": "high",
        "qwen/qwen3.7-max": "high",
        "qwen/qwen3.7-plus": "high",
        "sakana/fugu-ultra": "max",
        "tencent/hy3": "high",
        "x-ai/grok-4.5": "high",
        "z-ai/glm-5.2": "xhigh",
    }
    assert all(member.max_tokens == 16_384 for member in config.ensemble.proposers)
    assert all(member.temperature == 0.0 for member in config.ensemble.proposers)
    assert config.ensemble.aggregator.max_tokens == 16_384
    assert config.ensemble.aggregator.temperature == 0.0
    assert config.ensemble.min_successful_proposers == 3
    assert config.ensemble.all_failed_policy == "fallback_single"
    assert config.ensemble.candidate_max_chars == 24_000
    assert config.ensemble.shuffle_candidates is False
    assert config.ensemble.record_candidates is True
    assert config.ensemble.proposer_tools is False
    assert config.ensemble.aggregator_tools is True
    assert config.ensemble.wait_for_all_proposers is True
    assert config.ensemble.quorum_grace_seconds == 0.0
    assert config.tools.web_search.provider == "brave"
    assert config.tools.sandbox_enabled is False
    assert config.tools.web_search.max_results == 5
    assert config.tools.web_fetch.max_content_tokens == 50_000
    assert config.timeouts.proposer_seconds == pytest.approx(907.5)
    assert config.timeouts.aggregator_seconds == pytest.approx(2662.5)
    assert config.timeouts.task_seconds == 10800.0
    assert config.timeouts.task_margin_seconds == 30.0
    assert config.runner.mode == "agent_loop"
    assert config.runner.agent_max_iterations == 20
    assert config.runner.concurrency == 2
    assert config.runner.deadline_wrapup_margin_seconds == 300
    assert config.runner.deadline_wrapup_disable_tools is True
    assert config.runner.deadline_thinking_off_margin_seconds == 0
    assert config.runner.max_iterations_includes_finalization is False
    assert config.runner.retrieval_loop_finalization_threshold == 0
    assert config.runner.finalization_aggregator_only is False
    assert config.runner.finalization_disable_thinking is False
    assert config.generation.max_attempts == 3
    assert config.generation.retry_backoff_seconds == 2.0
    assert config.judge.model == "google/gemini-3.1-pro-preview"
    assert config.judge.repeats == 3
    assert config.judge.concurrency == 6
    assert config.judge.max_attempts == 3


def test_draco_ensemble_rejects_invalid_aggregator_output_budget() -> None:
    with pytest.raises(ValidationError, match="greater than or equal to 2"):
        load_draco_experiment_config(
            DEFAULT_CONFIG,
            inline_sets=["ensemble.aggregator_max_tokens_cap=1"],
        )

    with pytest.raises(
        ValidationError,
        match="aggregator_visible_answer_reserve_tokens must be smaller",
    ):
        load_draco_experiment_config(
            DEFAULT_CONFIG,
            inline_sets=["ensemble.aggregator_max_tokens_cap=8192"],
        )


def test_runner_finalization_fields_default_off_for_legacy_configs() -> None:
    config = DracoRunnerConfig(
        mode="agent_loop",
        agent_max_iterations=12,
        concurrency=2,
    )

    assert config.deadline_wrapup_margin_seconds == 0
    assert config.deadline_wrapup_disable_tools is False
    assert config.deadline_thinking_off_margin_seconds == 0
    assert config.max_iterations_includes_finalization is False
    assert config.retrieval_loop_finalization_threshold == 0
    assert config.finalization_aggregator_only is False
    assert config.finalization_disable_thinking is False


def test_override_files_and_inline_paths_apply_in_documented_order(tmp_path: Path) -> None:
    override_path = tmp_path / "override.json"
    override_path.write_text(
        json.dumps(
            {
                "runner": {"concurrency": 3},
                "ensemble": {"candidate_max_chars": 32000},
            }
        ),
        encoding="utf-8",
    )

    bundle = load_draco_experiment_config(
        DEFAULT_CONFIG,
        override_paths=[override_path],
        inline_sets=[
            "runner.concurrency=4",
            "ensemble.proposers.0.max_tokens=8192",
        ],
    )

    assert bundle.config.runner.concurrency == 4
    assert bundle.config.ensemble.candidate_max_chars == 32_000
    assert bundle.config.ensemble.proposers[0].max_tokens == 8192
    assert bundle.provenance()["overrides"][0]["path"] == str(override_path.resolve())


def test_unknown_override_path_fails_instead_of_silently_missing() -> None:
    with pytest.raises(ValueError, match="unknown experiment config override path"):
        load_draco_experiment_config(
            DEFAULT_CONFIG,
            inline_sets=["ensemble.typo_timeout=12"],
        )


def test_highest_thinking_invariant_rejects_accidental_downgrade() -> None:
    with pytest.raises(ValidationError, match="highest configured setting"):
        load_draco_experiment_config(
            DEFAULT_CONFIG,
            inline_sets=["ensemble.proposers.2.thinking=medium"],
        )


def test_formal_g1_thinking_map_uses_each_registry_models_highest_level() -> None:
    config = load_draco_experiment_config(DEFAULT_CONFIG).config
    assert config.g1_routing is not None
    registry = load_model_registry_snapshot()
    highest_by_model = {
        str(row["registry_facts"]["model_id"]): str(
            row["registry_facts"]["supported_thinking_levels"][0]
        )
        for row in registry["models"]
    }

    configured_registry_models = set(config.generation.model_thinking_levels) & set(
        highest_by_model
    )
    assert {
        model: config.generation.model_thinking_levels[model]
        for model in configured_registry_models
    } == {model: highest_by_model[model] for model in configured_registry_models}


def test_g1_registry_all_requires_no_explicit_route_fields(tmp_path: Path) -> None:
    config = load_draco_experiment_config(DEFAULT_CONFIG).config
    assert config.g1_routing is not None
    assert config.g1_routing.candidate_scope == "registry_all"

    for index, partial_contract in enumerate(
        (
            {"expected_candidate_count": 20},
            {"expected_routes": {"deepseek/deepseek-v4-pro": "deepseek"}},
            {"expected_routes_sha256": "0" * 64},
        )
    ):
        payload = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
        payload["g1_routing"].update(partial_contract)
        partial_config = tmp_path / f"partial-{index}.json"
        partial_config.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ValidationError, match="must be specified together"):
            load_draco_experiment_config(partial_config)


def test_g1_explicit_routes_remain_hash_and_count_fail_closed(tmp_path: Path) -> None:
    payload = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    routes = {
        "deepseek/deepseek-v4-pro": "deepseek",
        "z-ai/glm-5.2": "z-ai",
    }
    routes_hash = hashlib.sha256(
        json.dumps(
            routes,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    payload["g1_routing"].update(
        {
            "expected_proposer_count_max": len(routes),
            "expected_candidate_count": len(routes),
            "expected_routes": routes,
            "expected_routes_sha256": routes_hash,
        }
    )
    explicit_config = tmp_path / "explicit.json"
    explicit_config.write_text(json.dumps(payload), encoding="utf-8")

    config = load_draco_experiment_config(explicit_config).config
    assert config.g1_routing is not None
    assert config.g1_routing.candidate_scope == "exact_routes"

    with pytest.raises(ValidationError, match="expected_routes_sha256"):
        load_draco_experiment_config(
            explicit_config,
            inline_sets=[f"g1_routing.expected_routes_sha256={'0' * 64}"],
        )
    with pytest.raises(ValidationError, match="expected_candidate_count"):
        load_draco_experiment_config(
            explicit_config,
            inline_sets=["g1_routing.expected_candidate_count=3"],
        )


@pytest.mark.parametrize(
    "override",
    [
        "g1_routing.user_profile_enabled=true",
        "tools.sandbox_enabled=true",
    ],
)
def test_formal_runtime_false_flags_cannot_be_overridden(override: str) -> None:
    with pytest.raises(ValidationError):
        load_draco_experiment_config(
            DEFAULT_CONFIG,
            inline_sets=[override],
        )


def test_aggregator_rejects_unsupported_multiple_samples() -> None:
    with pytest.raises(ValidationError, match="ensemble.aggregator.k must be 1"):
        load_draco_experiment_config(
            DEFAULT_CONFIG,
            inline_sets=["ensemble.aggregator.k=2"],
        )


def test_reference_input_validation_checks_bytes_count_ids_and_order(tmp_path: Path) -> None:
    path = tmp_path / "mini.jsonl"
    path.write_text('{"id":"task-a","prompt":"hello"}\n', encoding="utf-8")
    default = load_draco_experiment_config(DEFAULT_CONFIG).config.benchmark_input
    input_config = default.model_copy(
        update={
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "task_count": 1,
            "task_ids": ["task-a"],
        }
    )

    trace = validate_reference_input(
        path,
        task_ids=["task-a"],
        config=input_config,
    )

    assert trace["status"] == "matched"
    with pytest.raises(ValueError, match="task_ids_or_order"):
        validate_reference_input(path, task_ids=["task-b"], config=input_config)
