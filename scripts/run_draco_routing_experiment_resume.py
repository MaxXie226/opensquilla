#!/usr/bin/env python3
"""Run the DRACO single-model and ensemble-routing experiment matrix."""

# ruff: noqa: E402 - the standalone runner adds the repository root to sys.path.

from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import hashlib
import json
import math
import os
import shlex
import statistics
import subprocess
import sys
import time
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from opensquilla.engine.agent import Agent
from opensquilla.engine.pipeline import TurnContext, run_pipeline
from opensquilla.engine.pricing import estimate_cost, resolve_model_price
from opensquilla.engine.selector_override import apply_model_override
from opensquilla.engine.steps.squilla_router import apply_squilla_router
from opensquilla.engine.types import (
    THINKING_BUDGETS,
    AgentConfig,
    ThinkingLevel,
    done_text_snapshot,
)
from opensquilla.engine.types import (
    DoneEvent as AgentDoneEvent,
)
from opensquilla.engine.types import (
    ErrorEvent as AgentErrorEvent,
)
from opensquilla.engine.types import (
    RunHeartbeatEvent as AgentRunHeartbeatEvent,
)
from opensquilla.engine.types import (
    StateChangeEvent as AgentStateChangeEvent,
)
from opensquilla.engine.types import (
    TextDeltaEvent as AgentTextDeltaEvent,
)
from opensquilla.engine.types import (
    ThinkingEvent as AgentThinkingEvent,
)
from opensquilla.engine.types import (
    ToolResultEvent as AgentToolResultEvent,
)
from opensquilla.engine.types import (
    ToolUseDeltaEvent as AgentToolUseDeltaEvent,
)
from opensquilla.engine.types import (
    ToolUseStartEvent as AgentToolUseStartEvent,
)
from opensquilla.engine.types import (
    WarningEvent as AgentWarningEvent,
)
from opensquilla.eval.draco_artifact_integrity import (
    compact_tool_result_diagnostic,
    seal_result_row,
    trace_row_from_result,
    verify_result_row_evidence,
)
from opensquilla.eval.draco_experiment_config import (
    DracoEnsembleMemberConfig,
    DracoExperimentConfig,
    DracoExperimentConfigBundle,
    load_draco_experiment_config,
    validate_reference_input,
)
from opensquilla.execution_status import compact_provider_status
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.llm_runtime import (
    OPENROUTER_DEFAULT_PROVIDER_ROUTING,
    resolve_llm_runtime_config,
)
from opensquilla.provider.ensemble import (
    EnsembleMemberConfig,
    EnsembleProvider,
    build_ensemble_provider_from_config,
    openrouter_static_capabilities,
    resolve_effective_generation_request_parameters,
)
from opensquilla.provider.registry import get_provider_spec
from opensquilla.provider.selector import (
    ModelSelector,
    ProviderConfig,
    SelectorConfig,
)
from opensquilla.provider.types import (
    REASONING_ONLY_LENGTH_STOP_REASONS,
    ChatConfig,
    DoneEvent,
    ErrorEvent,
    Message,
    ProviderBillingReceipt,
    ProviderHeartbeatEvent,
    ReasoningDeltaEvent,
    TextDeltaEvent,
    ToolDefinition,
    ToolInputSchema,
    ToolUseDeltaEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
)
from opensquilla.result_budget import ToolRunBudgetPolicy
from opensquilla.tool_boundary import ToolCall
from opensquilla.tools.dispatch import build_tool_handler
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import CallerKind, InteractionMode, ToolContext, ToolSpec
from opensquilla.usage_evidence import (
    MISSING_USAGE_PLACEHOLDER_ROLES,
    USAGE_EVIDENCE_SCHEMA,
    canonical_run_usage_units,
    canonicalize_run_usage,
    derive_physical_request_count,
    is_missing_usage_placeholder,
)

DEFAULT_B2_EXPERIMENT_CONFIG_PATH = ROOT / "configs/benchmarks/draco_b2_g12.json"


GROUP_SPECS: dict[str, dict[str, Any]] = {
    # B0/B1 remain the summary baselines, so the reference report's delta
    # columns compare every multi-model strategy against fixed Opus 4.8 and
    # SquillaRouter-selected single-model execution respectively.
    "B0": {
        "kind": "single",
        "model": "anthropic/claude-opus-4.8",
        "label": "fixed_opus48",
    },
    "B1": {"kind": "router_single", "label": "single_model_routing"},
    "B2": {
        "kind": "selection_mode",
        "selection_mode": "static_openrouter_b5",
        "label": "b2_quality_first_static_openrouter_b5",
        "experiment_config": "draco_b2_quality_first_v1",
    },
    "B3": {
        "kind": "selection_mode",
        "selection_mode": "router_tree_baseline",
        "label": "router_tree_baseline",
    },
    "B4": {
        "kind": "single",
        "model": "openai/gpt-5.5",
        "label": "fixed_gpt55",
    },
    "G1": {
        "kind": "selection_mode",
        "selection_mode": "router_dynamic",
        "label": "ranking_router_dynamic",
    },
}

TOOL_MODE_PROVIDER_ONLY = "provider_only"
TOOL_MODE_OPENROUTER_SERVER_TOOLS = "openrouter_server_tools"
TOOL_MODE_LOCAL_WEB_TOOLS = "local_web_tools"
RUNNER_MODE = TOOL_MODE_PROVIDER_ONLY
RUNNER_MODE_PROVIDER = "provider"
RUNNER_MODE_AGENT_LOOP = "agent_loop"
DEFAULT_DRACO_RUNNER_MODE = RUNNER_MODE_AGENT_LOOP
DEFAULT_AGENT_MAX_ITERATIONS = 12
DEFAULT_DEADLINE_WRAPUP_MARGIN_SECONDS = 0
DEFAULT_DEADLINE_WRAPUP_DISABLE_TOOLS = False
DEFAULT_DEADLINE_THINKING_OFF_MARGIN_SECONDS = 0
DEFAULT_MAX_ITERATIONS_INCLUDES_FINALIZATION = False
DEFAULT_RETRIEVAL_LOOP_FINALIZATION_THRESHOLD = 0
DEFAULT_FINALIZATION_AGGREGATOR_ONLY = False
DEFAULT_FINALIZATION_DISABLE_THINKING = False
AGENT_FINALIZATION_POLICY_FIELDS = (
    "deadline_wrapup_margin_seconds",
    "deadline_wrapup_disable_tools",
    "deadline_thinking_off_margin_seconds",
    "max_iterations_includes_finalization",
    "retrieval_loop_finalization_threshold",
    "finalization_aggregator_only",
    "finalization_disable_thinking",
)
AGENT_FINALIZATION_BOOLEAN_FIELDS = frozenset(
    {
        "deadline_wrapup_disable_tools",
        "max_iterations_includes_finalization",
        "finalization_aggregator_only",
        "finalization_disable_thinking",
    }
)
DEFAULT_AGENT_FINALIZATION_POLICY: dict[str, int | bool] = {
    "deadline_wrapup_margin_seconds": DEFAULT_DEADLINE_WRAPUP_MARGIN_SECONDS,
    "deadline_wrapup_disable_tools": DEFAULT_DEADLINE_WRAPUP_DISABLE_TOOLS,
    "deadline_thinking_off_margin_seconds": DEFAULT_DEADLINE_THINKING_OFF_MARGIN_SECONDS,
    "max_iterations_includes_finalization": DEFAULT_MAX_ITERATIONS_INCLUDES_FINALIZATION,
    "retrieval_loop_finalization_threshold": (DEFAULT_RETRIEVAL_LOOP_FINALIZATION_THRESHOLD),
    "finalization_aggregator_only": DEFAULT_FINALIZATION_AGGREGATOR_ONLY,
    "finalization_disable_thinking": DEFAULT_FINALIZATION_DISABLE_THINKING,
}
# B2 and G1 are compared in the same DRACO campaign.  Their task envelope
# (Agent policy, tools, Judge, generation, and timeouts) must therefore come
# from the same profile even when either group is launched by itself.
GLOBAL_EXPERIMENT_PROFILE_GROUPS = frozenset({"B2", "G1"})
SUPPORTED_RUNNER_MODES = (RUNNER_MODE_PROVIDER, RUNNER_MODE_AGENT_LOOP)
SUPPORTED_TOOL_MODES = (
    TOOL_MODE_PROVIDER_ONLY,
    TOOL_MODE_OPENROUTER_SERVER_TOOLS,
    TOOL_MODE_LOCAL_WEB_TOOLS,
)
BENCHMARK_WEB_PREFLIGHT_QUERY = "OpenAI official website"
BENCHMARK_WEB_FETCH_PREFLIGHT_URL = "https://example.com/"
DEFAULT_OPENROUTER_WEB_SEARCH_ENGINE = "exa"
DEFAULT_OPENROUTER_WEB_SEARCH_MAX_RESULTS = 5
DEFAULT_OPENROUTER_WEB_SEARCH_MAX_TOTAL_RESULTS = 10
DEFAULT_OPENROUTER_WEB_SEARCH_CONTEXT_SIZE = "medium"
DEFAULT_LOCAL_WEB_SEARCH_PROVIDER = "duckduckgo"
SUPPORTED_LOCAL_WEB_SEARCH_PROVIDERS = ("duckduckgo", "brave")
DEFAULT_OPENROUTER_WEB_FETCH_ENGINE = "openrouter"
DEFAULT_OPENROUTER_WEB_FETCH_MAX_USES = 5
DEFAULT_OPENROUTER_WEB_FETCH_MAX_CONTENT_TOKENS = 50_000
DEFAULT_OPENROUTER_FUSION_ANALYSIS_MODELS = (
    "deepseek/deepseek-v4-pro",
    "z-ai/glm-5.2",
    "google/gemini-3.1-pro-preview",
    "qwen/qwen3.7-max",
)
DEFAULT_OPENROUTER_FUSION_MODEL = "z-ai/glm-5.2"
DEFAULT_OPENROUTER_FUSION_MAX_TOOL_CALLS = 12
DEFAULT_OPENROUTER_FUSION_MAX_COMPLETION_TOKENS = 16_384
DEFAULT_OPENROUTER_FUSION_REASONING_EFFORT = "xhigh"
DEFAULT_OPENROUTER_FUSION_TEMPERATURE = 0.0
GENERATION_THINKING_MODEL_MAX = "model_max"
DEFAULT_GENERATION_THINKING = GENERATION_THINKING_MODEL_MAX
DEFAULT_GENERATION_THINKING_FALLBACK = "xhigh"
DEFAULT_GENERATION_MAX_TOKENS_OVERRIDE = 0
DEFAULT_MODEL_MAX_GENERATION_THINKING: dict[str, str] = {
    "anthropic/claude-fable-5": "max",
    "anthropic/claude-opus-4.8": "max",
    "deepseek/deepseek-v4-pro": "xhigh",
    "google/gemini-3.1-pro-preview": "high",
    "moonshotai/kimi-k2.7-code": "high",
    "openai/gpt-5.5": "xhigh",
    "openai/gpt-5.5-pro": "xhigh",
    "openai/gpt-5.6-sol": "max",
    "qwen/qwen3.7-max": "high",
    "sakana/fugu-ultra": "max",
    "z-ai/glm-5.2": "xhigh",
}
DEFAULT_GENERATION_TEMPERATURE = 0.0
DEFAULT_CONTAMINATION_BLOCKED_DOMAINS = (
    "hf.co",
    "huggingface.co",
    "datasets-server.huggingface.co",
    "github.com",
    "raw.githubusercontent.com",
    "openrouter.ai",
    "perplexity.ai",
    "research.perplexity.ai",
)
PROFILE_TIMEOUT_MARGIN_SECONDS = 30.0
DEFAULT_PROFILE_PROPOSER_TIMEOUT_SECONDS = 120.0
DEFAULT_PROFILE_AGGREGATOR_TIMEOUT_SECONDS = 300.0
JUDGE_MAX_ATTEMPTS = 3
GENERATION_MAX_ATTEMPTS = 3
GENERATION_ATTEMPT_EVIDENCE_SCHEMA = "opensquilla.draco-generation-attempt/v1"
JUDGE_ATTEMPT_EVIDENCE_SCHEMA = "opensquilla.draco-judge-attempt/v1"
JUDGE_ATTEMPT_BUDGET_SCOPE = "criterion_repeat_campaign"
JUDGE_ATTEMPT_BUDGET_EXHAUSTED_ERROR = "judge_attempt_budget_exhausted"
DEFAULT_GENERATION_RETRY_BACKOFF_SECONDS = 2.0
GENERATION_EMPTY_OUTPUT_ERROR = "empty_generation_output"
GENERATION_MISSING_DONE_ERROR = "generation_missing_done"
RUNNER_STREAM_CLEANUP_TIMEOUT_SECONDS = 1.0
RUN_COMPATIBILITY_SCHEMA = "opensquilla.draco.run-compatibility/v1"
REPAIR_ONLY_SOURCE_DRIFT_SCHEMA = "opensquilla.draco.repair-only-source-drift/v1"


def normalized_agent_finalization_policy(
    value: Mapping[str, Any] | None = None,
) -> dict[str, int | bool]:
    raw = dict(value or {})
    policy: dict[str, int | bool] = {}
    for field_name in AGENT_FINALIZATION_POLICY_FIELDS:
        default = DEFAULT_AGENT_FINALIZATION_POLICY[field_name]
        candidate = raw.get(field_name, default)
        if field_name in AGENT_FINALIZATION_BOOLEAN_FIELDS:
            if not isinstance(candidate, bool):
                raise ValueError(f"{field_name} must be a boolean")
            policy[field_name] = candidate
            continue
        if isinstance(candidate, bool) or not isinstance(candidate, int):
            raise ValueError(f"{field_name} must be an integer >= 0")
        if candidate < 0:
            raise ValueError(f"{field_name} must be an integer >= 0")
        policy[field_name] = candidate
    return policy


def legal_proposer_quorum(proposer_count: int) -> int:
    """Return the DRACO two-thirds quorum (4 -> 3, 3 -> 2)."""

    if isinstance(proposer_count, bool) or not isinstance(proposer_count, int):
        raise ValueError("proposer_count must be a non-negative integer")
    if proposer_count < 0:
        raise ValueError("proposer_count must be a non-negative integer")
    return (2 * proposer_count + 2) // 3 if proposer_count else 0


def validate_g1_registry_contract(
    experiment: DracoExperimentConfig,
    config: GatewayConfig,
) -> dict[str, Any]:
    """Validate the formal G1 pool and upstream pins before any model call."""

    contract = experiment.g1_routing
    if contract is None:
        raise ValueError("G1 requires a versioned g1_routing experiment contract")
    from opensquilla.provider.ranking_router import (
        _legacy_registry_snapshot_projection,
        load_model_registry_snapshot,
        ranking_config_snapshot,
    )

    snapshot = load_model_registry_snapshot()
    thinking_assignment_enabled = (
        config.llm_ensemble.ranking_thinking_assignment_enabled is True
    )
    if not thinking_assignment_enabled:
        snapshot = _legacy_registry_snapshot_projection(snapshot)
    actual_version = str(snapshot.get("snapshot_version") or "").strip()
    if actual_version != contract.source_registry_snapshot_version:
        raise ValueError("G1 registry snapshot version differs from the experiment contract")
    actual_registry_hash = canonical_json_sha256(snapshot).removeprefix("sha256:")
    if actual_registry_hash != contract.expected_source_registry_snapshot_sha256:
        raise ValueError("G1 registry snapshot content differs from the experiment contract")
    ranking_config = ranking_config_snapshot(
        thinking_assignment_enabled=thinking_assignment_enabled,
    )
    actual_ranking_schema = str(ranking_config.get("schema_version") or "").strip()
    actual_ranking_version = str(ranking_config.get("config_version") or "").strip()
    actual_ranking_hash = canonical_json_sha256(ranking_config).removeprefix("sha256:")
    if (
        actual_ranking_schema != contract.expected_ranking_config_schema_version
        or actual_ranking_version != contract.expected_ranking_config_version
        or actual_ranking_hash != contract.expected_ranking_config_sha256
    ):
        raise ValueError("G1 ranking configuration differs from the experiment contract")
    proposer_count_config = ranking_config.get("proposer_count")
    by_tier = (
        proposer_count_config.get("by_tier") if isinstance(proposer_count_config, Mapping) else None
    )
    high_risk = (
        proposer_count_config.get("high_risk")
        if isinstance(proposer_count_config, Mapping)
        else None
    )
    try:
        actual_proposer_max = max(
            *(int(row["max"]) for row in by_tier.values() if isinstance(row, Mapping)),
            int(high_risk["max"]),
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("G1 ranking proposer bounds are malformed") from exc
    if actual_proposer_max != contract.expected_proposer_count_max:
        raise ValueError("G1 ranking proposer maximum differs from the experiment contract")
    rows = snapshot.get("models")
    if not isinstance(rows, list):
        raise ValueError("G1 registry snapshot has no model rows")
    available: set[str] = set()
    for row in rows:
        facts = row.get("registry_facts") if isinstance(row, Mapping) else None
        if not isinstance(facts, Mapping):
            raise ValueError("G1 registry snapshot contains a malformed model row")
        if str(facts.get("provider") or "").strip().lower() != "openrouter":
            raise ValueError("G1 formal registry contains a non-OpenRouter model")
        model = str(facts.get("model_id") or "").strip().lower()
        if not model or model in available:
            raise ValueError("G1 registry snapshot contains a missing or duplicate model id")
        available.add(model)
    if contract.candidate_scope == "exact_routes":
        assert contract.expected_routes is not None
        assert contract.expected_candidate_count is not None
        assert contract.expected_routes_sha256 is not None
        expected_routes = dict(contract.expected_routes)
        expected_count = contract.expected_candidate_count
        expected_hash = contract.expected_routes_sha256
        missing_models = sorted(set(expected_routes) - available)
        if missing_models:
            raise ValueError(
                "G1 expected route model(s) missing from registry: " + ", ".join(missing_models)
            )
        runtime = resolve_llm_runtime_config(config)
        pin_mismatches = {
            model: {
                "expected": expected_provider,
                "actual": runtime.provider_routing.get(model),
            }
            for model, expected_provider in expected_routes.items()
            if runtime.provider_routing.get(model) != expected_provider
        }
        if pin_mismatches:
            raise ValueError(
                "G1 expected route provider pin(s) differ: " + ", ".join(sorted(pin_mismatches))
            )
        policy = "exact_openrouter_routes"
        runtime_pin_policy = "required_exact"
        runtime_pins_match: bool | None = True
    else:
        expected_routes = {model: "auto" for model in sorted(available)}
        expected_count = len(expected_routes)
        expected_hash = canonical_json_sha256(expected_routes).removeprefix("sha256:")
        policy = "all_registry_models"
        runtime_pin_policy = "optional_auto"
        runtime_pins_match = None
    if expected_count < contract.expected_proposer_count_max:
        raise ValueError("G1 registry has fewer candidates than the proposer maximum")
    return {
        **contract.model_dump(mode="json", exclude_none=True),
        "candidate_scope": contract.candidate_scope,
        "policy": policy,
        "expected_candidate_count": expected_count,
        "expected_routes": expected_routes,
        "expected_routes_sha256": expected_hash,
        "expected_identities": sorted(f"openrouter:{model}" for model in expected_routes),
        "validated": True,
        "available_registry_candidate_count": len(available),
        "runtime_pin_policy": runtime_pin_policy,
        "runtime_pins_match": runtime_pins_match,
    }


def aggregator_recovery_policy(
    experiment: DracoExperimentConfig,
) -> dict[str, Any]:
    """Return the frozen aggregator recovery settings from the experiment profile."""

    ensemble = experiment.ensemble
    return {
        "aggregator_recovery_mode": ensemble.aggregator_recovery_mode,
        "aggregator_recovery_top_k": ensemble.aggregator_recovery_top_k,
        "aggregator_max_tokens_cap": ensemble.aggregator_max_tokens_cap,
        "aggregator_visible_answer_reserve_tokens": (
            ensemble.aggregator_visible_answer_reserve_tokens
        ),
    }


def apply_aggregator_recovery_policy(target: Any, policy: Mapping[str, Any]) -> None:
    """Apply one normalized recovery policy to a runtime or dry provider config."""

    for field_name, value in policy.items():
        setattr(target, field_name, value)


def enforce_formal_draco_runtime_config(
    config: GatewayConfig,
    experiment: DracoExperimentConfig | None,
    groups: list[str],
) -> dict[str, Any]:
    """Apply formal runtime switches independently of the operator base config."""

    if experiment is None:
        return {}
    if experiment.tools.sandbox_enabled is not False:
        raise ValueError("formal DRACO requires tools.sandbox_enabled=false")
    config.sandbox.sandbox = False
    config.sandbox.security_grading = False
    recovery_policy = aggregator_recovery_policy(experiment)
    apply_aggregator_recovery_policy(config.llm_ensemble, recovery_policy)
    freeze: dict[str, Any] = {
        "source": "experiment_config",
        "sandbox_enabled": False,
        "sandbox_security_grading_enabled": False,
        **recovery_policy,
    }
    if "G1" in groups:
        g1_routing = experiment.g1_routing
        if g1_routing is None or g1_routing.user_profile_enabled is not False:
            raise ValueError("formal G1 requires g1_routing.user_profile_enabled=false")
        config.llm_ensemble.ranking_user_profile_generation_enabled = False
        config.llm_ensemble.ranking_user_profile_enabled = False
        freeze.update(
            {
                "g1_user_profile_generation_enabled": False,
                "g1_user_profile_enabled": False,
            }
        )
    return freeze


def agent_finalization_policy_from_args(
    args: argparse.Namespace,
) -> dict[str, int | bool]:
    return normalized_agent_finalization_policy(
        {
            field_name: getattr(
                args,
                field_name,
                DEFAULT_AGENT_FINALIZATION_POLICY[field_name],
            )
            for field_name in AGENT_FINALIZATION_POLICY_FIELDS
        }
    )


def normalize_agent_runner_args(
    args: argparse.Namespace,
) -> dict[str, int | bool]:
    raw_max_iterations = getattr(
        args,
        "agent_max_iterations",
        DEFAULT_AGENT_MAX_ITERATIONS,
    )
    if raw_max_iterations is None:
        raw_max_iterations = DEFAULT_AGENT_MAX_ITERATIONS
    if isinstance(raw_max_iterations, bool) or not isinstance(raw_max_iterations, int):
        raise ValueError("agent_max_iterations must be an integer >= 0")
    if raw_max_iterations < 0:
        raise ValueError("agent_max_iterations must be an integer >= 0")
    args.agent_max_iterations = raw_max_iterations
    policy = agent_finalization_policy_from_args(args)
    for field_name, value in policy.items():
        setattr(args, field_name, value)
    return policy


def apply_b2_g12_argument_alignment(
    args: argparse.Namespace,
    groups: list[str],
) -> dict[str, Any] | None:
    """Load the composable JSON profile and apply its run-wide settings."""

    config_requested = bool(
        getattr(args, "experiment_config", None)
        or getattr(args, "experiment_config_override", None)
        or getattr(args, "experiment_config_set", None)
    )
    uses_global_profile = bool(GLOBAL_EXPERIMENT_PROFILE_GROUPS.intersection(groups))
    if not uses_global_profile and not config_requested:
        return None

    base_path = Path(getattr(args, "experiment_config", None) or DEFAULT_B2_EXPERIMENT_CONFIG_PATH)
    bundle = load_draco_experiment_config(
        base_path,
        override_paths=list(getattr(args, "experiment_config_override", []) or []),
        inline_sets=list(getattr(args, "experiment_config_set", []) or []),
    )
    config = bundle.config
    overrides = {
        "concurrency": config.runner.concurrency,
        "timeout": config.timeouts.task_seconds,
        "ensemble_proposer_timeout": config.timeouts.proposer_seconds,
        "ensemble_aggregator_timeout": config.timeouts.aggregator_seconds,
        "ensemble_proposer_early_stop_success_count": 0,
        "ensemble_proposer_early_stop_after": 0.0,
        "expand_ensemble_timeouts_to_task_timeout": False,
        "runner_mode": config.runner.mode,
        "agent_max_iterations": config.runner.agent_max_iterations,
        **{
            field_name: getattr(
                config.runner,
                field_name,
                DEFAULT_AGENT_FINALIZATION_POLICY[field_name],
            )
            for field_name in AGENT_FINALIZATION_POLICY_FIELDS
        },
        "judge_model": config.judge.model,
        "judge_repeats": config.judge.repeats,
        "judge_concurrency": config.judge.concurrency,
        "judge_max_attempts": config.judge.max_attempts,
        "judge_candidates": config.judge.judge_candidates,
        "generation_max_attempts": config.generation.max_attempts,
        "generation_max_tokens": config.generation.max_tokens,
        "generation_retry_backoff": config.generation.retry_backoff_seconds,
        "tool_mode": config.tools.mode,
        "contamination_blocked_domains": ",".join(config.tools.contamination_blocked_domains),
        "local_web_search_provider": config.tools.web_search.provider,
        "local_web_search_api_key_env": config.tools.web_search.api_key_env,
        "openrouter_web_search_max_results": config.tools.web_search.max_results,
        "openrouter_web_fetch_max_content_tokens": (config.tools.web_fetch.max_content_tokens),
    }
    requested = {key: getattr(args, key, None) for key in overrides}
    overridden = {
        key: {"requested": requested[key], "effective": value}
        for key, value in overrides.items()
        if requested[key] != value
    }
    for key, value in overrides.items():
        setattr(args, key, value)
    effective_sources = dict(getattr(args, "_effective_argument_sources", {}) or {})
    effective_sources.update({key: "experiment_config" for key in overrides})
    args._effective_argument_sources = effective_sources
    args.experiment_config = bundle.base_path
    args._draco_experiment_config_bundle = bundle
    record = {
        "id": config.profile_id,
        "reference": config.reference.model_dump(mode="json"),
        "scope": "Shared DRACO execution profile; B2 ensemble mapping remains B2-only",
        "config_provenance": bundle.provenance(),
        "effective_config": config.model_dump(mode="json"),
        "requested_args": requested,
        "effective_args": dict(overrides),
        "effective_arg_sources": {key: "experiment_config" for key in overrides},
        "overridden_args": overridden,
    }
    alignments = dict(getattr(args, "_benchmark_alignments", {}) or {})
    alignments["global_experiment_profile"] = record
    if "B2" in groups:
        alignments["B2"] = record
    args._benchmark_alignments = alignments
    return record


def build_webresearch_tool_run_budget_policy(
    *,
    max_single_fetch_chars: int | None,
    max_web_search_results: int | None,
) -> ToolRunBudgetPolicy:
    """Preserve the reference DRACO policy on the current extended budget type."""

    return ToolRunBudgetPolicy(
        max_single_fetch_chars=max_single_fetch_chars,
        max_web_search_results=max_web_search_results,
        max_web_search_fetch_top_k=None,
        max_web_search_chars_per_source=None,
        max_repeated_retrievals_per_turn=None,
    )


class _BenchmarkApprovalQueue:
    """Non-interactive approval queue for unattended benchmark runs."""

    def request(self, namespace: str = "exec", params: dict | None = None) -> str:
        return "draco-benchmark:auto-deny"

    async def wait(self, approval_id: str, timeout: float | None = None) -> bool:
        return False

    def resolve(self, approval_id: str, approved: bool) -> None:
        return None


@dataclass
class RunResult:
    final_text: str
    done: DoneEvent | None
    error: str = ""
    latency_ms: int = 0
    ttft_ms: int | None = None
    tool_call_count: int = 0
    trace_events: list[dict[str, Any]] = field(default_factory=list)
    setup_latency_ms: int = 0
    setup_usage: list[dict[str, Any]] = field(default_factory=list)
    routing_trace: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderBuildResult:
    provider: Any
    prompt: str
    setup_latency_ms: int = 0
    setup_usage: list[dict[str, Any]] = field(default_factory=list)
    routing_trace: dict[str, Any] = field(default_factory=dict)


class ProviderBuildError(RuntimeError):
    """Preserve already-billed setup receipts when provider construction fails."""

    def __init__(
        self,
        cause: Exception,
        *,
        setup_latency_ms: int,
        setup_usage: list[dict[str, Any]],
        routing_trace: dict[str, Any],
    ) -> None:
        super().__init__(
            "provider_build_failed_after_setup:"
            + type(cause).__name__
        )
        self.setup_latency_ms = setup_latency_ms
        self.setup_usage = list(setup_usage)
        self.routing_trace = dict(routing_trace)


def safe_provider_build_routing_trace(value: Any) -> dict[str, Any]:
    """Return only JSON-safe routing evidence for a failed provider build."""

    try:
        normalized = json_safe(value)
    except Exception:
        return {}
    return dict(normalized) if isinstance(normalized, Mapping) else {}


def _provider_selection_plan_execution_snapshot(
    provider: Any,
) -> Mapping[str, Any] | None:
    """Read the provider's immutable route plus its persisted receipt prefix."""

    snapshot = getattr(provider, "selection_plan_execution_snapshot", None)
    plan = (
        snapshot()
        if callable(snapshot)
        else getattr(provider, "selection_plan", None)
    )
    return plan if isinstance(plan, Mapping) else None


def attach_provider_setup(provider: Any, build: ProviderBuildResult) -> Any:
    """Attach one-shot setup accounting plus a reusable frozen routing receipt."""

    try:
        selection_plan = getattr(provider, "selection_plan", None)
        if not (
            isinstance(selection_plan, Mapping)
            and selection_plan.get(
                "ranking_thinking_assignment_enabled"
            )
            is True
        ):
            setattr(provider, "_draco_frozen_routing_trace", None)
            setattr(
                provider,
                "_draco_setup_metrics",
                {
                    "latency_ms": build.setup_latency_ms,
                    "usage": list(build.setup_usage),
                    "routing": dict(build.routing_trace),
                },
            )
            return provider
        execution_plan = _provider_selection_plan_execution_snapshot(
            provider
        )
        if not (
            isinstance(execution_plan, Mapping)
            and execution_plan.get(
                "ranking_thinking_assignment_enabled"
            )
            is True
        ):
            raise ValueError(
                "managed provider selection plan snapshot is invalid"
            )
        frozen_routing = json_safe(build.routing_trace)
        if not isinstance(frozen_routing, Mapping):
            raise TypeError(
                "provider routing trace must serialize to an object"
            )
        if execution_plan is not None:
            serialized_plan = json_safe(
                copy.deepcopy(dict(execution_plan))
            )
            if not isinstance(serialized_plan, Mapping):
                raise TypeError(
                    "provider selection plan must serialize to an object"
                )
            frozen_routing = {
                **dict(frozen_routing),
                "selection_plan": copy.deepcopy(
                    dict(serialized_plan)
                ),
            }
        setattr(
            provider,
            "_draco_setup_metrics",
            {
                "latency_ms": build.setup_latency_ms,
                "usage": copy.deepcopy(list(build.setup_usage)),
            },
        )
        setattr(
            provider,
            "_draco_frozen_routing_trace",
            copy.deepcopy(dict(frozen_routing)),
        )
        return provider
    except ProviderBuildError:
        raise
    except Exception as exc:
        if build.setup_usage:
            raise ProviderBuildError(
                exc,
                setup_latency_ms=build.setup_latency_ms,
                setup_usage=build.setup_usage,
                routing_trace=safe_provider_build_routing_trace(
                    build.routing_trace
                ),
            ) from exc
        raise


def consume_provider_setup(provider: Any) -> dict[str, Any]:
    """Consume setup once and bind the frozen route to the executed plan state."""

    setup = getattr(provider, "_draco_setup_metrics", None)
    routing = getattr(provider, "_draco_frozen_routing_trace", None)
    if not isinstance(routing, Mapping):
        if not isinstance(setup, dict):
            return {}
        setattr(provider, "_draco_setup_metrics", None)
        return setup
    if not isinstance(setup, Mapping) and not isinstance(routing, Mapping):
        return {}
    if isinstance(setup, Mapping):
        setattr(provider, "_draco_setup_metrics", None)
    routing_receipt = copy.deepcopy(dict(routing)) if isinstance(routing, Mapping) else {}
    executed_plan = _provider_selection_plan_execution_snapshot(provider)
    if routing_receipt and isinstance(executed_plan, Mapping):
        serialized_plan = json_safe(copy.deepcopy(dict(executed_plan)))
        if not isinstance(serialized_plan, Mapping):
            raise TypeError("provider selection plan must serialize to an object")
        routing_receipt["selection_plan"] = copy.deepcopy(dict(serialized_plan))
    return {
        "latency_ms": coerce_metric_int(
            setup.get("latency_ms") if isinstance(setup, Mapping) else 0
        ),
        "usage": copy.deepcopy(
            setup.get("usage")
            if isinstance(setup, Mapping) and isinstance(setup.get("usage"), list)
            else []
        ),
        "routing": routing_receipt,
    }


class DryProvider:
    provider_name = "dry"

    def __init__(self, model: str, group: str) -> None:
        self.model = model
        self.group = group

    async def chat(self, messages: list[Message], tools=None, config=None):  # noqa: ANN001
        prompt = str(messages[-1].content if messages else "")
        text = f"[dry:{self.group}:{self.model}] {prompt[:160]}"
        yield TextDeltaEvent(text=text)
        yield DoneEvent(
            input_tokens=max(1, len(prompt) // 4),
            output_tokens=max(1, len(text) // 4),
            model=self.model,
            provider="dry",
            cost_source="none",
        )

    async def list_models(self) -> list[Any]:
        return []


class DryEnsembleProvider:
    provider_name = "dry_ensemble"

    def __init__(
        self,
        *,
        group: str,
        profile: str,
        proposer_models: list[str] | None = None,
        model: str = "dry-aggregator",
        selection_mode: str = "",
    ) -> None:
        self.group = group
        self.profile = profile
        self.model = model
        self.proposer_models = proposer_models or ["dry-proposer-a", "dry-proposer-b"]
        self.selection_mode = selection_mode or profile
        self.selection_plan: dict[str, Any] = {}

    async def chat(self, messages: list[Message], tools=None, config=None):  # noqa: ANN001
        prompt = str(messages[-1].content if messages else "")
        sample_indexes: dict[str, int] = {}
        candidates: list[dict[str, Any]] = []
        selected_p = self.selection_plan.get("selected_P")
        selected_identities = selected_p if isinstance(selected_p, list) else []
        for index, model in enumerate(self.proposer_models):
            sample_index = sample_indexes.get(model, 0)
            sample_indexes[model] = sample_index + 1
            identity = (
                selected_identities[index]
                if index < len(selected_identities) and isinstance(selected_identities[index], str)
                else f"dry:{model}"
            )
            provider, separator, selected_model = identity.partition(":")
            provider = provider.strip() if separator else "dry"
            selected_model = selected_model.strip() if separator else model
            candidate_text = f"Candidate {index + 1} for {prompt[:80]}"
            candidates.append(
                {
                    "index": index,
                    "sample_index": sample_index,
                    "label": f"proposer_{index + 1}",
                    "provider": provider,
                    "requested_provider": provider,
                    "model": selected_model,
                    "requested_model": selected_model,
                    "ok": True,
                    "request_started": True,
                    "physical_request_count": 1,
                    "usage_reported": True,
                    "stop_reason": "stop",
                    "content": {
                        "text": candidate_text,
                        "chars": len(candidate_text),
                        "truncated": False,
                    },
                    "text": candidate_text,
                    "input_tokens": 10 + index,
                    "output_tokens": 8,
                    "billed_cost": 0.0,
                    "cost_source": "none",
                }
            )
        text = f"[dry:{self.group}:{self.profile}] fused answer for {prompt[:120]}"
        proposer_usage = [
            {
                "role": "proposer",
                "provider": candidate["provider"],
                "model": candidate["model"],
                "input_tokens": candidate["input_tokens"],
                "output_tokens": candidate["output_tokens"],
                "cached_tokens": 0,
                "cache_write_tokens": 0,
                "billed_cost": 0.0,
                "cost_source": "none",
            }
            for candidate in candidates
        ]
        yield TextDeltaEvent(text=text)
        aggregator_provider = (
            str(self.selection_plan.get("selected_A") or "dry:").partition(":")[0].strip() or "dry"
        )
        aggregator_identity = f"{aggregator_provider}:{self.model}"
        raw_aggregator_candidates = self.selection_plan.get("aggregator_candidates")
        aggregator_candidates = (
            [
                str(identity).strip()
                for identity in raw_aggregator_candidates
                if str(identity).strip()
            ]
            if isinstance(raw_aggregator_candidates, list)
            else []
        )
        if not aggregator_candidates or aggregator_candidates[0] != aggregator_identity:
            aggregator_candidates = [aggregator_identity]
        self.selection_plan["aggregator_candidates"] = list(aggregator_candidates)
        yield DoneEvent(
            input_tokens=sum(int(candidate["input_tokens"]) for candidate in candidates) + 21,
            output_tokens=max(1, len(text) // 4),
            model=self.model,
            provider=aggregator_provider,
            model_usage_breakdown=[
                *proposer_usage,
                {
                    "role": "aggregator",
                    "provider": aggregator_provider,
                    "model": self.model,
                    "input_tokens": 21,
                    "output_tokens": max(1, len(text) // 4),
                    "cached_tokens": 0,
                    "cache_write_tokens": 0,
                    "billed_cost": 0.0,
                    "cost_source": "none",
                },
            ],
            ensemble_trace={
                "mode": "b5_fusion",
                "profile": self.profile,
                "selection_strategy": self.selection_mode,
                "selection_plan": dict(self.selection_plan),
                "successful_proposers": len(candidates),
                "total_candidates": len(candidates),
                "fallback_used": False,
                "candidates": candidates,
                "shuffle_candidates": False,
                "proposer_tools": getattr(self, "proposer_tools", False),
                "aggregator_tools": getattr(self, "aggregator_tools", True),
                "aggregator_recovery": {
                    "schema": "opensquilla.ensemble-aggregator-recovery/v1",
                    "mode": getattr(
                        self,
                        "aggregator_recovery_mode",
                        "serving",
                    ),
                    "candidate_count": len(aggregator_candidates),
                    "candidate_ids": list(aggregator_candidates),
                    "max_tokens_cap": getattr(
                        self,
                        "aggregator_max_tokens_cap",
                        65_536,
                    ),
                    "visible_answer_reserve_tokens": getattr(
                        self,
                        "aggregator_visible_answer_reserve_tokens",
                        8_192,
                    ),
                    "attempts": [
                        {
                            "attempt": 1,
                            "physical_attempt_index": 1,
                            "physical_request_count": 1,
                            "kind": "primary",
                            "fallback_index": 0,
                            "trigger": "",
                            "request_started": True,
                            "visible_output_emitted": True,
                            "stream_closed": True,
                            "outcome": "succeeded",
                            "stop_reason": "stop",
                            "requested_provider": aggregator_provider,
                            "requested_model": self.model,
                            "actual_provider": aggregator_provider,
                            "actual_model": self.model,
                        }
                    ],
                    "proposer_reused": True,
                    "success": True,
                    "degraded": False,
                    "selected_attempt": 1,
                    "selected_kind": "primary",
                    "fallback_index": 0,
                    "fallback_reason": "",
                    "executed_A": aggregator_identity,
                    "continuation_count": 0,
                    "same_model_recovery_count": 0,
                },
                "executed_A": aggregator_identity,
                "fallback_reason": "",
                "run_outcome": "success",
                "delivery_outcome": "complete",
                "final_request_role": "aggregator",
                "final_request": {
                    "role": "aggregator",
                    "request_started": True,
                    "execution": {
                        "provider": aggregator_provider,
                        "actual_provider": aggregator_provider,
                        "model": self.model,
                        "actual_model": self.model,
                        "requested_provider": aggregator_provider,
                        "requested_model": self.model,
                    },
                    "usage": {
                        "provider": aggregator_provider,
                        "model": self.model,
                        "requested_provider": aggregator_provider,
                        "requested_model": self.model,
                        "stop_reason": "stop",
                    },
                    "output": {
                        "text": text,
                        "chars": len(text),
                        "truncated": False,
                    },
                },
                "llm_request_count": len(candidates) + 1,
                "physical_request_count": len(candidates) + 1,
            },
        )

    async def list_models(self) -> list[Any]:
        return []


def load_tasks(path: Path, *, max_tasks: int = 0) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    task_id_lines: dict[str, int] = {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        payload = json.loads(line)
        task_id = str(payload.get("id") or payload.get("task_id") or "").strip()
        prompt = str(payload.get("prompt") or payload.get("problem") or "").strip()
        if not task_id or not prompt:
            raise ValueError(f"{path}:{lineno} requires non-empty id/task_id and prompt/problem")
        prior_lineno = task_id_lines.get(task_id)
        if prior_lineno is not None:
            raise ValueError(
                f"{path}:{lineno} duplicate task id {task_id!r}; "
                f"first declared on line {prior_lineno}"
            )
        task_id_lines[task_id] = lineno
        payload["id"] = task_id
        payload["prompt"] = prompt
        if "rubric" in payload:
            payload["rubric"] = parse_maybe_json(payload["rubric"])
        elif "answer" in payload:
            payload["rubric"] = parse_maybe_json(payload["answer"])
        tasks.append(payload)
        if max_tasks and len(tasks) >= max_tasks:
            break
    return tasks


def parse_groups(raw: str) -> list[str]:
    groups = [item.strip().upper() for item in raw.split(",") if item.strip()]
    if not groups:
        raise ValueError("--groups must contain at least one experiment group")
    unknown = [group for group in groups if group not in GROUP_SPECS]
    if unknown:
        raise ValueError(f"unknown group(s): {', '.join(unknown)}")
    duplicates = sorted(group for group in set(groups) if groups.count(group) > 1)
    if duplicates:
        raise ValueError(f"duplicate group(s): {', '.join(duplicates)}")
    return groups


def result_key_coverage(
    rows: list[dict[str, Any]],
    *,
    expected_keys: set[tuple[str, str]],
) -> dict[str, Any]:
    """Audit exact one-row coverage for every normalized group/task key."""

    counts: dict[tuple[str, str], int] = {}
    for row in rows:
        key = (
            str(row.get("group") or "").strip().upper(),
            str(row.get("task_id") or "").strip(),
        )
        counts[key] = counts.get(key, 0) + 1
    actual_keys = set(counts)
    missing = sorted(expected_keys - actual_keys)
    unexpected = sorted(actual_keys - expected_keys)
    duplicates = sorted(key for key, count in counts.items() if count > 1)
    row = {
        "pass": not missing and not unexpected and not duplicates,
        "expected_row_count": len(expected_keys),
        "actual_row_count": len(rows),
        "actual_unique_key_count": len(actual_keys),
        "missing_keys": [list(key) for key in missing],
        "unexpected_keys": [list(key) for key in unexpected],
        "duplicate_keys": [{"key": list(key), "count": counts[key]} for key in duplicates],
    }
    return row


def normalize_domain(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    if "://" in raw:
        parsed = urlparse(raw)
        host = parsed.hostname or parsed.netloc
    else:
        host = raw.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
        host = urlparse(f"//{host}").hostname or host
    return host.strip().lstrip("*.").strip(".")


def parse_domain_list(raw: Any) -> list[str]:
    if raw is None:
        values: list[Any] = list(DEFAULT_CONTAMINATION_BLOCKED_DOMAINS)
    elif isinstance(raw, str):
        values = raw.split(",")
    else:
        values = list(raw)
    domains: list[str] = []
    for value in values:
        domain = normalize_domain(value)
        if domain and domain not in domains:
            domains.append(domain)
    return domains


def positive_int_value(raw: Any, *, default: int, field: str) -> int:
    value = default if raw is None else raw
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return parsed


def bounded_int_value(
    raw: Any,
    *,
    default: int,
    field: str,
    minimum: int,
    maximum: int,
) -> int:
    value = positive_int_value(raw, default=default, field=field)
    if value < minimum or value > maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return value


def parse_csv_values(raw: Any, *, default: tuple[str, ...] = ()) -> list[str]:
    value = raw if raw is not None else ",".join(default)
    if isinstance(value, str):
        candidates = value.split(",")
    elif isinstance(value, tuple | list):
        candidates = list(value)
    else:
        candidates = [str(value)]
    items: list[str] = []
    for candidate in candidates:
        item = str(candidate).strip()
        if item and item not in items:
            items.append(item)
    return items


def float_range_value(
    raw: Any,
    *,
    default: float,
    field: str,
    minimum: float,
    maximum: float,
) -> float:
    value = default if raw is None else raw
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a number") from exc
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return parsed


def approx_chars_for_content_tokens(tokens: int) -> int:
    return max(100, int(tokens) * 4)


def openrouter_server_tool_settings(
    args: argparse.Namespace | None,
    *,
    blocked_domains: list[str],
) -> dict[str, Any]:
    search_context_size = (
        str(
            getattr(
                args,
                "openrouter_web_search_context_size",
                DEFAULT_OPENROUTER_WEB_SEARCH_CONTEXT_SIZE,
            )
            or DEFAULT_OPENROUTER_WEB_SEARCH_CONTEXT_SIZE
        )
        .strip()
        .lower()
    )
    if search_context_size not in {"low", "medium", "high"}:
        raise ValueError("openrouter_web_search_context_size must be one of: low, medium, high")
    web_search = {
        "type": "openrouter:web_search",
        "parameters": {
            "engine": str(
                getattr(
                    args,
                    "openrouter_web_search_engine",
                    DEFAULT_OPENROUTER_WEB_SEARCH_ENGINE,
                )
                or DEFAULT_OPENROUTER_WEB_SEARCH_ENGINE
            ).strip(),
            "max_results": positive_int_value(
                getattr(args, "openrouter_web_search_max_results", None),
                default=DEFAULT_OPENROUTER_WEB_SEARCH_MAX_RESULTS,
                field="openrouter_web_search_max_results",
            ),
            "max_total_results": positive_int_value(
                getattr(args, "openrouter_web_search_max_total_results", None),
                default=DEFAULT_OPENROUTER_WEB_SEARCH_MAX_TOTAL_RESULTS,
                field="openrouter_web_search_max_total_results",
            ),
            "search_context_size": search_context_size,
            "excluded_domains": blocked_domains,
        },
    }
    web_fetch = {
        "type": "openrouter:web_fetch",
        "parameters": {
            "engine": str(
                getattr(
                    args,
                    "openrouter_web_fetch_engine",
                    DEFAULT_OPENROUTER_WEB_FETCH_ENGINE,
                )
                or DEFAULT_OPENROUTER_WEB_FETCH_ENGINE
            ).strip(),
            "max_uses": positive_int_value(
                getattr(args, "openrouter_web_fetch_max_uses", None),
                default=DEFAULT_OPENROUTER_WEB_FETCH_MAX_USES,
                field="openrouter_web_fetch_max_uses",
            ),
            "max_content_tokens": positive_int_value(
                getattr(args, "openrouter_web_fetch_max_content_tokens", None),
                default=DEFAULT_OPENROUTER_WEB_FETCH_MAX_CONTENT_TOKENS,
                field="openrouter_web_fetch_max_content_tokens",
            ),
            "blocked_domains": blocked_domains,
        },
    }
    return {
        "web_search": web_search,
        "web_fetch": web_fetch,
    }


def openrouter_fusion_tool_settings(args: argparse.Namespace | None) -> dict[str, Any]:
    analysis_models = parse_csv_values(
        getattr(args, "openrouter_fusion_analysis_models", None),
        default=DEFAULT_OPENROUTER_FUSION_ANALYSIS_MODELS,
    )
    if not 1 <= len(analysis_models) <= 8:
        raise ValueError("openrouter_fusion_analysis_models must contain 1 to 8 models")
    judge_model = str(
        getattr(args, "openrouter_fusion_model", DEFAULT_OPENROUTER_FUSION_MODEL)
        or DEFAULT_OPENROUTER_FUSION_MODEL
    ).strip()
    if not judge_model:
        raise ValueError("openrouter_fusion_model must not be empty")
    reasoning_effort = (
        str(
            getattr(
                args,
                "openrouter_fusion_reasoning_effort",
                DEFAULT_OPENROUTER_FUSION_REASONING_EFFORT,
            )
            or DEFAULT_OPENROUTER_FUSION_REASONING_EFFORT
        )
        .strip()
        .lower()
    )
    if reasoning_effort not in {"minimal", "low", "medium", "high", "xhigh", "max"}:
        raise ValueError(
            "openrouter_fusion_reasoning_effort must be one of: "
            "minimal, low, medium, high, xhigh, max"
        )
    return {
        "type": "openrouter:fusion",
        "parameters": {
            "analysis_models": analysis_models,
            "model": judge_model,
            "max_tool_calls": bounded_int_value(
                getattr(args, "openrouter_fusion_max_tool_calls", None),
                default=DEFAULT_OPENROUTER_FUSION_MAX_TOOL_CALLS,
                field="openrouter_fusion_max_tool_calls",
                minimum=1,
                maximum=16,
            ),
            "max_completion_tokens": positive_int_value(
                getattr(args, "openrouter_fusion_max_completion_tokens", None),
                default=DEFAULT_OPENROUTER_FUSION_MAX_COMPLETION_TOKENS,
                field="openrouter_fusion_max_completion_tokens",
            ),
            "reasoning": {"effort": reasoning_effort},
            "temperature": float_range_value(
                getattr(args, "openrouter_fusion_temperature", None),
                default=DEFAULT_OPENROUTER_FUSION_TEMPERATURE,
                field="openrouter_fusion_temperature",
                minimum=0.0,
                maximum=2.0,
            ),
        },
    }


def benchmark_tool_policy(args: argparse.Namespace | None = None) -> dict[str, Any]:
    mode = str(getattr(args, "tool_mode", RUNNER_MODE) or RUNNER_MODE).strip()
    blocked_domains = parse_domain_list(getattr(args, "contamination_blocked_domains", None))
    if mode not in SUPPORTED_TOOL_MODES:
        raise ValueError(f"unknown tool mode: {mode}")
    if mode == TOOL_MODE_LOCAL_WEB_TOOLS:
        if not blocked_domains:
            raise ValueError("DRACO research-tool runs require contamination-blocked domains")
        local_search_max_results = positive_int_value(
            getattr(args, "openrouter_web_search_max_results", None),
            default=DEFAULT_OPENROUTER_WEB_SEARCH_MAX_RESULTS,
            field="openrouter_web_search_max_results",
        )
        local_search_provider = (
            str(
                getattr(args, "local_web_search_provider", DEFAULT_LOCAL_WEB_SEARCH_PROVIDER)
                or DEFAULT_LOCAL_WEB_SEARCH_PROVIDER
            ).strip()
            or DEFAULT_LOCAL_WEB_SEARCH_PROVIDER
        )
        if local_search_provider not in SUPPORTED_LOCAL_WEB_SEARCH_PROVIDERS:
            raise ValueError(
                "local_web_search_provider must be one of: "
                f"{', '.join(SUPPORTED_LOCAL_WEB_SEARCH_PROVIDERS)}"
            )
        local_search_api_key_env = str(
            getattr(args, "local_web_search_api_key_env", "") or ""
        ).strip()
        local_fetch_max_content_tokens = positive_int_value(
            getattr(args, "openrouter_web_fetch_max_content_tokens", None),
            default=DEFAULT_OPENROUTER_WEB_FETCH_MAX_CONTENT_TOKENS,
            field="openrouter_web_fetch_max_content_tokens",
        )
        return {
            "tool_mode": mode,
            "tools_enabled": True,
            "tool_names": ["web_search", "web_fetch"],
            "local_web_tools": {
                "web_search": {
                    "excluded_domains": blocked_domains,
                    "max_results": local_search_max_results,
                    "provider": local_search_provider,
                    "api_key_env": local_search_api_key_env,
                },
                "web_fetch": {
                    "blocked_domains": blocked_domains,
                    "max_content_tokens": local_fetch_max_content_tokens,
                    "max_content_chars": approx_chars_for_content_tokens(
                        local_fetch_max_content_tokens
                    ),
                    "allow_firecrawl": bool(getattr(args, "allow_firecrawl_web_fetch", False)),
                },
            },
            "contamination_blocked_domains": blocked_domains,
            "contamination_controls": {
                "status": "enforced_by_local_web_tools",
                "web_search_field": "excluded_domains_query_and_result_filter",
                "web_fetch_field": "blocked_domains",
            },
        }
    if mode == TOOL_MODE_OPENROUTER_SERVER_TOOLS:
        if not blocked_domains:
            raise ValueError("DRACO research-tool runs require contamination-blocked domains")
        server_tools = openrouter_server_tool_settings(
            args,
            blocked_domains=blocked_domains,
        )
        return {
            "tool_mode": mode,
            "tools_enabled": True,
            "tool_names": [
                server_tools["web_search"]["type"],
                server_tools["web_fetch"]["type"],
            ],
            "openrouter_server_tools": server_tools,
            "contamination_blocked_domains": blocked_domains,
            "contamination_controls": {
                "status": "enforced_by_openrouter_server_tools",
                "web_search_field": "excluded_domains",
                "web_fetch_field": "blocked_domains",
            },
        }
    return {
        "tool_mode": mode,
        "tools_enabled": False,
        "tool_names": [],
        "contamination_blocked_domains": blocked_domains,
        "contamination_controls": {
            "status": "not_applicable_no_external_tools",
            "web_search_field": "excluded_domains",
            "web_fetch_field": "blocked_domains",
        },
    }


def group_uses_openrouter_fusion(group: str) -> bool:
    spec = GROUP_SPECS[group]
    return spec.get("server_tool_profile") == "openrouter_fusion"


def validate_runner_mode_for_groups(runner_mode: str, groups: list[str]) -> None:
    if runner_mode != RUNNER_MODE_AGENT_LOOP:
        return
    fusion_groups = [group for group in groups if group_uses_openrouter_fusion(group)]
    if fusion_groups:
        raise ValueError(
            "OpenRouter Fusion experiment groups are provider-level server-side "
            "agent baselines; run "
            f"{','.join(fusion_groups)} with --runner-mode=provider"
        )


def validate_tool_mode_for_runner(
    runner_mode: str,
    tool_mode: str,
    *,
    smoke_only: bool = False,
) -> None:
    if smoke_only:
        return
    if tool_mode == TOOL_MODE_LOCAL_WEB_TOOLS and runner_mode != RUNNER_MODE_AGENT_LOOP:
        raise ValueError(
            "--tool-mode=local_web_tools requires --runner-mode=agent_loop so local "
            "tool calls are executed rather than only forwarded to the provider."
        )
    if tool_mode == TOOL_MODE_OPENROUTER_SERVER_TOOLS and runner_mode != RUNNER_MODE_PROVIDER:
        raise ValueError("--tool-mode=openrouter_server_tools requires --runner-mode=provider.")


def benchmark_tool_policy_for_group(
    tool_policy: dict[str, Any],
    group: str,
    *,
    args: argparse.Namespace | None = None,
) -> dict[str, Any]:
    if not group_uses_openrouter_fusion(group):
        return tool_policy
    fusion_tool = openrouter_fusion_tool_settings(args)
    fusion_tool_name = str(fusion_tool.get("type") or "openrouter:fusion")
    return {
        **tool_policy,
        "tools_enabled": True,
        "tool_names": [fusion_tool_name],
        "openrouter_fusion_enabled": True,
        "openrouter_fusion_only": True,
        "openrouter_fusion_tool": fusion_tool,
        "openrouter_fusion_tool_choice": "required",
        "contamination_controls": {
            **dict(tool_policy.get("contamination_controls") or {}),
            "fusion_status": "internal_web_domain_controls_not_exposed",
            "fusion_internal_web_tools": (
                "openrouter_fusion_enables_internal_web_search_and_fetch; "
                "domain exclusion is not exposed in the documented fusion parameters"
            ),
        },
    }


def benchmark_tool_policies_for_groups(
    tool_policy: dict[str, Any],
    groups: list[str],
    *,
    args: argparse.Namespace | None = None,
) -> dict[str, dict[str, Any]]:
    return {
        group: benchmark_tool_policy_for_group(tool_policy, group, args=args) for group in groups
    }


def _local_web_tool_definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="web_search",
            description=(
                "Search the web and return result titles, URLs, and snippets. "
                "Benchmark leakage domains are excluded and filtered."
            ),
            input_schema=ToolInputSchema(
                properties={
                    "query": {"type": "string", "description": "Search query."},
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results to return.",
                    },
                },
                required=["query"],
            ),
        ),
        ToolDefinition(
            name="web_fetch",
            description=(
                "Fetch a URL and extract readable content. Benchmark leakage domains are blocked."
            ),
            input_schema=ToolInputSchema(
                properties={
                    "url": {"type": "string", "description": "URL to fetch."},
                    "extract_mode": {
                        "type": "string",
                        "description": "Extraction mode, usually markdown.",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Maximum characters to return.",
                    },
                },
                required=["url"],
            ),
        ),
    ]


def _provider_tool_definition(
    provider_tool: dict[str, Any],
    *,
    description: str,
) -> ToolDefinition:
    tool_type = str(provider_tool.get("type") or "")
    return ToolDefinition(
        name=tool_type,
        description=description,
        input_schema=ToolInputSchema(),
        provider_tool=provider_tool,
    )


def benchmark_tools_for_policy(tool_policy: dict[str, Any]) -> list[ToolDefinition] | None:
    fusion_enabled = bool(tool_policy.get("openrouter_fusion_enabled"))
    if not tool_policy.get("tools_enabled") and not fusion_enabled:
        return None
    tools: list[ToolDefinition] = []
    if tool_policy.get("tool_mode") == TOOL_MODE_LOCAL_WEB_TOOLS and not tool_policy.get(
        "openrouter_fusion_only"
    ):
        tools.extend(_local_web_tool_definitions())
    server_tools = tool_policy.get("openrouter_server_tools") or {}
    if not tool_policy.get("openrouter_fusion_only"):
        for key, description in (
            ("web_search", "OpenRouter server-side web search."),
            ("web_fetch", "OpenRouter server-side web fetch."),
        ):
            provider_tool = server_tools.get(key)
            if not isinstance(provider_tool, dict):
                continue
            tools.append(_provider_tool_definition(provider_tool, description=description))
    fusion_tool = tool_policy.get("openrouter_fusion_tool")
    if fusion_enabled and isinstance(fusion_tool, dict):
        tools.append(
            _provider_tool_definition(
                fusion_tool,
                description=("OpenRouter Fusion server-side multi-model deliberation."),
            )
        )
    return tools or None


def local_web_search_max_results(tool_policy: dict[str, Any]) -> int:
    local_policy = tool_policy.get("local_web_tools") or {}
    search_defaults = local_policy.get("web_search") or {}
    return positive_int_value(
        search_defaults.get("max_results"),
        default=DEFAULT_OPENROUTER_WEB_SEARCH_MAX_RESULTS,
        field="local_web_search_max_results",
    )


def local_web_search_provider(tool_policy: dict[str, Any]) -> str:
    local_policy = tool_policy.get("local_web_tools") or {}
    search_defaults = local_policy.get("web_search") or {}
    provider = (
        str(search_defaults.get("provider") or DEFAULT_LOCAL_WEB_SEARCH_PROVIDER).strip()
        or DEFAULT_LOCAL_WEB_SEARCH_PROVIDER
    )
    if provider not in SUPPORTED_LOCAL_WEB_SEARCH_PROVIDERS:
        raise ValueError(
            "local_web_search_provider must be one of: "
            f"{', '.join(SUPPORTED_LOCAL_WEB_SEARCH_PROVIDERS)}"
        )
    return provider


def local_web_search_api_key_env(tool_policy: dict[str, Any]) -> str:
    local_policy = tool_policy.get("local_web_tools") or {}
    search_defaults = local_policy.get("web_search") or {}
    return str(search_defaults.get("api_key_env") or "").strip()


def local_web_fetch_max_chars(tool_policy: dict[str, Any]) -> int:
    local_policy = tool_policy.get("local_web_tools") or {}
    fetch_defaults = local_policy.get("web_fetch") or {}
    chars = fetch_defaults.get("max_content_chars")
    if isinstance(chars, int | float) and not isinstance(chars, bool):
        return max(100, int(chars))
    tokens = positive_int_value(
        fetch_defaults.get("max_content_tokens"),
        default=DEFAULT_OPENROUTER_WEB_FETCH_MAX_CONTENT_TOKENS,
        field="local_web_fetch_max_content_tokens",
    )
    return approx_chars_for_content_tokens(tokens)


def bounded_tool_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int | None = None,
) -> int:
    try:
        if isinstance(value, bool):
            parsed = default
        elif isinstance(value, int | float):
            parsed = int(value)
        else:
            parsed = int(str(value).strip())
    except (TypeError, ValueError, OverflowError):
        parsed = default
    parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(parsed, maximum)
    return parsed


def configure_local_web_search_runtime(
    config: GatewayConfig,
    tool_policy: dict[str, Any],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    if tool_policy.get("tool_mode") != TOOL_MODE_LOCAL_WEB_TOOLS:
        return {}

    import opensquilla.search.providers.brave  # noqa: F401
    import opensquilla.search.providers.duckduckgo  # noqa: F401
    from opensquilla.search.registry import get_provider_spec
    from opensquilla.tools.builtin.web import configure_search

    configured_provider = local_web_search_provider(tool_policy)
    provider = configured_provider
    env_key = local_web_search_api_key_env(tool_policy)
    if not env_key:
        env_key = str(getattr(config, "search_api_key_env", "") or "").strip()
    try:
        spec = get_provider_spec(provider)
    except Exception as exc:
        raise ValueError(f"unknown local web search provider: {provider}") from exc
    if not spec.runtime_supported:
        raise ValueError(f"local web search provider is not runtime-supported: {provider}")
    api_key = ""
    api_key_source = ""
    credential_status = "not_required"
    if spec.requires_api_key:
        credential_status = "configured"
        if not env_key:
            env_key = str(getattr(spec, "env_key", "") or "").strip()
        if env_key and os.environ.get(env_key):
            api_key = str(os.environ.get(env_key) or "")
            api_key_source = f"env:{env_key}"
        if not api_key:
            api_key = str(getattr(config, "search_api_key", "") or "").strip()
            api_key_source = "config" if api_key else ""
        if not api_key:
            env_hint = env_key or getattr(spec, "env_key", "") or "the provider API key env var"
            if not dry_run:
                raise ValueError(
                    f"local web search provider '{provider}' requires an API key; "
                    f"set {env_hint} or choose --local-web-search-provider duckduckgo"
                )
            credential_status = "missing_allowed_dry_run"
    else:
        env_key = ""

    runtime_max_results = local_web_search_max_results(tool_policy)
    proxy = str(getattr(config, "search_proxy", "") or "").strip()
    fallback_policy = str(getattr(config, "search_fallback_policy", "off") or "off")
    diagnostics = bool(getattr(config, "search_diagnostics", False))
    use_env_proxy = bool(getattr(config, "search_use_env_proxy", False))
    runtime_configured = bool(api_key) or not spec.requires_api_key
    if runtime_configured:
        configure_search(
            provider_name=provider,
            max_results=runtime_max_results,
            api_key=api_key,
            proxy=proxy,
            use_env_proxy=use_env_proxy,
            fallback_policy=fallback_policy,
            diagnostics=diagnostics,
        )
    return {
        "configured_provider": configured_provider,
        "provider": provider,
        "max_results": runtime_max_results,
        "api_key_configured": bool(api_key),
        "api_key_source": api_key_source,
        "api_key_env": env_key,
        "credential_status": credential_status,
        "runtime_configured": runtime_configured,
        "proxy_configured": bool(proxy),
        "use_env_proxy": use_env_proxy,
        "fallback_policy": fallback_policy,
        "diagnostics": diagnostics,
    }


def configure_benchmark_sandbox_runtime(
    config: GatewayConfig,
    tool_policy: dict[str, Any],
) -> dict[str, Any]:
    if tool_policy.get("tool_mode") != TOOL_MODE_LOCAL_WEB_TOOLS:
        return {}

    from opensquilla.sandbox.integration import configure_runtime

    workspace = Path(config.workspace_dir) if config.workspace_dir else ROOT
    runtime = configure_runtime(
        config.sandbox,
        approval_queue=_BenchmarkApprovalQueue(),
        workspace=workspace,
    )
    return {
        "configured": True,
        "backend": runtime.backend.name,
        "workspace": str(runtime.workspace),
        "approval_queue": "auto_deny_unattended",
        "effective": runtime.effective.as_dict(),
    }


def configure_local_web_fetch_runtime(tool_policy: dict[str, Any]) -> dict[str, Any]:
    if tool_policy.get("tool_mode") != TOOL_MODE_LOCAL_WEB_TOOLS:
        return {}
    local_policy = tool_policy.get("local_web_tools") or {}
    fetch_policy = local_policy.get("web_fetch") or {}
    allow_firecrawl = bool(fetch_policy.get("allow_firecrawl"))
    firecrawl_was_configured = bool(os.environ.get("FIRECRAWL_API_KEY", "").strip())
    if firecrawl_was_configured and not allow_firecrawl:
        os.environ.pop("FIRECRAWL_API_KEY", None)
    return {
        "extractor_mode": "auto_local_first",
        "firecrawl_allowed": allow_firecrawl,
        "firecrawl_api_key_active": firecrawl_was_configured and allow_firecrawl,
        "firecrawl_disabled_for_reproducibility": (
            firecrawl_was_configured and not allow_firecrawl
        ),
        "external_fetch_cost_tracking": (
            "required_when_firecrawl_used" if allow_firecrawl else "not_applicable"
        ),
    }


def blocked_domain_match(url: str, blocked_domains: list[str]) -> str:
    domain = normalize_domain(url)
    if not domain:
        return ""
    for blocked in blocked_domains:
        if domain == blocked or domain.endswith(f".{blocked}"):
            return blocked
    return ""


def append_search_exclusions(query: str, blocked_domains: list[str]) -> str:
    clean_query = str(query or "").strip()
    exclusions = " ".join(f"-site:{domain}" for domain in blocked_domains if domain)
    return f"{clean_query} {exclusions}".strip() if exclusions else clean_query


def filter_blocked_search_results(
    payload: dict[str, Any],
    *,
    blocked_domains: list[str],
    original_query: str,
    executed_query: str,
) -> dict[str, Any]:
    filtered = dict(payload)
    removed_by_field: dict[str, list[dict[str, str]]] = {}
    for field_name in ("results", "sources"):
        items = filtered.get(field_name)
        removed: list[dict[str, str]] = []
        kept: list[Any] = []
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                kept.append(item)
                continue
            match = blocked_domain_match(str(item.get("url") or ""), blocked_domains)
            if match:
                removed.append(
                    {
                        "url": str(item.get("url") or ""),
                        "blocked_domain": match,
                    }
                )
            else:
                kept.append(item)
        filtered[field_name] = kept
        removed_by_field[field_name] = removed
    filtered["query"] = original_query
    filtered["executed_query"] = executed_query
    filtered["blocked_domains"] = blocked_domains
    blocked_results = removed_by_field.get("results", [])
    blocked_sources = removed_by_field.get("sources", [])
    filtered["blocked_result_count"] = len(blocked_results)
    filtered["blocked_source_count"] = len(blocked_sources)
    if blocked_results:
        filtered["blocked_results"] = blocked_results
    if blocked_sources:
        filtered["blocked_sources"] = blocked_sources
    return filtered


def build_local_web_tool_registry(tool_policy: dict[str, Any]) -> ToolRegistry:
    registry = ToolRegistry()
    blocked_domains = parse_domain_list(tool_policy.get("contamination_blocked_domains") or [])
    default_max_results = local_web_search_max_results(tool_policy)
    configured_search_provider = local_web_search_provider(tool_policy)
    default_fetch_max_chars = local_web_fetch_max_chars(tool_policy)

    async def _web_search(query: str, max_results: int | None = None) -> str:
        from opensquilla.tools.builtin.web import run_web_search_payload

        original_query = str(query or "")
        executed_query = (
            append_search_exclusions(original_query, blocked_domains)
            if configured_search_provider == "duckduckgo"
            else original_query
        )
        limit = bounded_tool_int(
            max_results,
            default=default_max_results,
            minimum=1,
            maximum=default_max_results,
        )
        payload = await run_web_search_payload(
            executed_query,
            limit,
            exclude_domains=blocked_domains,
            provider=configured_search_provider,
        )
        filtered = filter_blocked_search_results(
            payload,
            blocked_domains=blocked_domains,
            original_query=original_query,
            executed_query=executed_query,
        )
        return json.dumps(filtered, ensure_ascii=False, indent=2)

    async def _web_fetch(
        url: str,
        extract_mode: str = "markdown",
        max_chars: int | None = None,
    ) -> str:
        from opensquilla.tools.builtin.web_fetch import web_fetch

        match = blocked_domain_match(str(url or ""), blocked_domains)
        if match:
            return json.dumps(
                {
                    "url": url,
                    "error_class": "BlockedDomain",
                    "error": (
                        "This URL belongs to a DRACO contamination-blocked domain "
                        f"({match}) and was not fetched."
                    ),
                    "blocked_domain": match,
                    "blocked_domains": blocked_domains,
                },
                ensure_ascii=False,
                indent=2,
            )
        effective_max_chars = bounded_tool_int(
            max_chars,
            default=default_fetch_max_chars,
            minimum=100,
            maximum=default_fetch_max_chars,
        )
        content = await web_fetch(
            url,
            extract_mode=extract_mode,
            max_chars=effective_max_chars,
        )
        try:
            payload = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            return content
        final_url = str(payload.get("final_url") or payload.get("url") or "")
        final_match = blocked_domain_match(final_url, blocked_domains)
        if not final_match:
            return content
        return json.dumps(
            {
                "url": url,
                "final_url": final_url,
                "error_class": "BlockedDomain",
                "error": (
                    "This request redirected to a DRACO contamination-blocked domain "
                    f"({final_match}); fetched content was discarded."
                ),
                "blocked_domain": final_match,
                "blocked_domains": blocked_domains,
            },
            ensure_ascii=False,
            indent=2,
        )

    registry.register(
        ToolSpec(
            name="web_search",
            description=(
                "Search the web and return result titles, URLs, and snippets. "
                "Benchmark leakage domains are excluded and filtered."
            ),
            parameters={
                "query": {"type": "string", "description": "Search query."},
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return.",
                },
            },
            required=["query"],
            result_budget_class="external",
        ),
        _web_search,
    )
    registry.register(
        ToolSpec(
            name="web_fetch",
            description=(
                "Fetch a URL and extract readable content. Benchmark leakage domains are blocked."
            ),
            parameters={
                "url": {"type": "string", "description": "URL to fetch."},
                "extract_mode": {
                    "type": "string",
                    "description": "Extraction mode, usually markdown.",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum characters to return.",
                },
            },
            required=["url"],
            result_budget_class="external",
        ),
        _web_fetch,
    )
    return registry


def build_benchmark_tool_context(
    *,
    task_id: str,
    group: str,
    tool_policy: dict[str, Any],
    output_dir: Path | None = None,
) -> ToolContext:
    scratch_dir = None
    if output_dir is not None:
        scratch_dir = str(output_dir / "scratch" / group / task_id)
    return ToolContext(
        is_owner=True,
        caller_kind=CallerKind.CLI,
        interaction_mode=InteractionMode.UNATTENDED,
        agent_id=f"draco-{group}",
        session_key=f"draco:{group}:{task_id}",
        task_id=task_id,
        allowed_tools={"web_search", "web_fetch"},
        workspace_dir=str(ROOT),
        workspace_strict=True,
        scratch_dir=scratch_dir,
        tool_run_budget_policy=build_webresearch_tool_run_budget_policy(
            max_single_fetch_chars=local_web_fetch_max_chars(tool_policy),
            max_web_search_results=local_web_search_max_results(tool_policy),
        ),
    )


async def run_local_web_tools_preflight(
    tool_policy: dict[str, Any],
    *,
    dry_run: bool = False,
    max_attempts: int = 3,
    retry_backoff_seconds: float = 1.0,
    call_timeout_seconds: float = 90.0,
) -> dict[str, Any]:
    """Fail fast unless the benchmark's real web tool path can search and fetch."""

    if tool_policy.get("tool_mode") != TOOL_MODE_LOCAL_WEB_TOOLS:
        return {}
    if dry_run:
        return {
            "status": "skipped_dry_run",
            "web_fetch_url": BENCHMARK_WEB_FETCH_PREFLIGHT_URL,
            "preflight_calls": {"web_search": 0, "web_fetch": 0},
            "cost_attribution": "no_preflight_calls_dry_run",
        }

    attempt_limit = max(1, int(max_attempts))
    timeout = max(1.0, float(call_timeout_seconds))
    call_counts = {"web_search": 0, "web_fetch": 0}

    async def _attempt(attempt: int) -> dict[str, Any]:
        registry = build_local_web_tool_registry(tool_policy)
        context = build_benchmark_tool_context(
            task_id=f"__local_web_tools_preflight_{attempt}__",
            group="preflight",
            tool_policy=tool_policy,
        )
        handler = build_tool_handler(registry, context)

        async def _call(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            call_counts[tool_name] += 1
            try:
                result = await asyncio.wait_for(
                    handler(
                        ToolCall(
                            tool_use_id=f"draco-preflight-{attempt}-{tool_name}",
                            tool_name=tool_name,
                            arguments=arguments,
                        )
                    ),
                    timeout=timeout,
                )
            except TimeoutError as exc:
                raise RuntimeError(
                    f"DRACO local web tool {tool_name} preflight timed out after {timeout:.0f}s"
                ) from exc
            detail = str(result.content or "")[:500]
            if result.is_error:
                raise RuntimeError(f"DRACO local web tool {tool_name} preflight failed: {detail}")
            try:
                payload = json.loads(result.content)
            except (TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"DRACO local web tool {tool_name} preflight returned non-JSON: {detail}"
                ) from exc
            if not isinstance(payload, dict):
                raise RuntimeError(
                    f"DRACO local web tool {tool_name} preflight returned a non-object payload"
                )
            status = str(payload.get("status") or "").strip().casefold()
            reason = str(payload.get("reason") or "").strip().casefold()
            if (
                payload.get("error")
                or payload.get("error_class")
                or status in {"error", "failed", "denied", "approval_denied"}
                or "denied" in reason
                or "policy" in reason
            ):
                raise RuntimeError(f"DRACO local web tool {tool_name} preflight failed: {detail}")
            return payload

        search_payload = await _call(
            "web_search",
            {"query": BENCHMARK_WEB_PREFLIGHT_QUERY, "max_results": 1},
        )
        search_results = search_payload.get("results")
        if (
            search_payload.get("ok") is False
            or not isinstance(search_results, list)
            or not search_results
        ):
            raise RuntimeError("DRACO local web tool web_search preflight returned invalid results")
        if not any(
            isinstance(item, dict)
            and urlparse(str(item.get("url") or "")).scheme in {"http", "https"}
            and bool(urlparse(str(item.get("url") or "")).netloc)
            for item in search_results
        ):
            raise RuntimeError(
                "DRACO local web tool web_search preflight returned no valid HTTP(S) result"
            )

        fetch_payload = await _call(
            "web_fetch",
            {
                "url": BENCHMARK_WEB_FETCH_PREFLIGHT_URL,
                "extract_mode": "text",
                "max_chars": 1_000,
            },
        )
        fetched_text = fetch_payload.get("text")
        if not isinstance(fetched_text, str) or not fetched_text.strip():
            raise RuntimeError("DRACO local web tool web_fetch preflight returned no text")
        try:
            fetch_status = int(fetch_payload.get("status"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "DRACO local web tool web_fetch preflight returned no HTTP status"
            ) from exc
        if not 200 <= fetch_status < 400:
            raise RuntimeError(
                f"DRACO local web tool web_fetch preflight returned HTTP {fetch_status}"
            )

        return {
            "status": "passed",
            "attempts_used": attempt,
            "web_search_query": BENCHMARK_WEB_PREFLIGHT_QUERY,
            "web_search_result_count": len(search_results),
            "web_fetch_url": BENCHMARK_WEB_FETCH_PREFLIGHT_URL,
            "web_fetch_http_status": fetch_status,
            "web_fetch_text_chars": len(fetched_text),
            "preflight_calls": dict(call_counts),
            "cost_attribution": "setup_preflight_excluded_from_benchmark_row_metrics",
        }

    last_error: RuntimeError | None = None
    for attempt in range(1, attempt_limit + 1):
        try:
            return await _attempt(attempt)
        except RuntimeError as exc:
            last_error = exc
            if attempt < attempt_limit and retry_backoff_seconds > 0:
                await asyncio.sleep(float(retry_backoff_seconds) * (2 ** (attempt - 1)))
    assert last_error is not None
    raise RuntimeError(
        f"DRACO local web tools preflight failed after {attempt_limit} attempt(s): {last_error}"
    ) from last_error


def generation_thinking_policy(args: argparse.Namespace | None = None) -> dict[str, Any]:
    mode = DEFAULT_GENERATION_THINKING
    bundle = getattr(args, "_draco_experiment_config_bundle", None)
    experiment = bundle.config if isinstance(bundle, DracoExperimentConfigBundle) else None
    generation = experiment.generation if experiment is not None else None
    fallback_level = str(
        generation.default_thinking_level
        if generation is not None
        else DEFAULT_GENERATION_THINKING_FALLBACK
    )
    max_tokens_override = max(
        0,
        int(
            getattr(
                args,
                "generation_max_tokens",
                DEFAULT_GENERATION_MAX_TOKENS_OVERRIDE,
            )
            or DEFAULT_GENERATION_MAX_TOKENS_OVERRIDE
        ),
    )
    return {
        "generation_thinking": mode,
        "temperature": (
            generation.temperature if generation is not None else DEFAULT_GENERATION_TEMPERATURE
        ),
        "thinking_enabled": (generation.thinking_enabled if generation is not None else True),
        "thinking_level": "model-specific",
        "default_thinking_level": fallback_level,
        "thinking_budget_tokens": (
            generation.thinking_budget_tokens if generation is not None else "model-specific"
        ),
        "max_thinking_budget_tokens": (
            generation.thinking_budget_tokens
            if generation is not None
            else THINKING_BUDGETS[ThinkingLevel.XHIGH]
        ),
        "max_tokens": max_tokens_override or ChatConfig().max_tokens,
        "max_tokens_overridden": max_tokens_override > 0,
        "model_thinking_levels": (
            dict(generation.model_thinking_levels)
            if generation is not None
            else dict(DEFAULT_MODEL_MAX_GENERATION_THINKING)
        ),
        "require_highest_thinking": bool(
            generation.require_highest_thinking if generation is not None else False
        ),
        "applies_to": "single baselines and ensemble members",
    }


def _normalized_model_id(model: str | None) -> str:
    return str(model or "").strip().lower()


@cache
def _packaged_openrouter_thinking_levels() -> dict[str, tuple[str, ...]]:
    """Return the validated model-specific OpenRouter thinking ladder."""

    from opensquilla.provider.ranking_router import load_model_registry_snapshot

    levels_by_model: dict[str, tuple[str, ...]] = {}
    for row in load_model_registry_snapshot().get("models", []):
        facts = row.get("registry_facts") if isinstance(row, Mapping) else None
        if not isinstance(facts, Mapping):
            continue
        if str(facts.get("provider") or "").strip().lower() != "openrouter":
            continue
        model = _normalized_model_id(str(facts.get("model_id") or ""))
        raw_levels = facts.get("supported_thinking_levels")
        if (
            not model
            or not isinstance(raw_levels, Sequence)
            or isinstance(raw_levels, (str, bytes))
        ):
            continue
        levels = tuple(str(level).strip().lower() for level in raw_levels if str(level).strip())
        if levels:
            levels_by_model[model] = levels
    return levels_by_model


def _openrouter_supported_thinking_levels(model: str | None) -> tuple[str, ...]:
    return _packaged_openrouter_thinking_levels().get(_normalized_model_id(model), ())


def generation_thinking_for_model(
    model: str | None,
    policy: dict[str, Any] | None = None,
) -> str:
    policy = policy or generation_thinking_policy()
    mode = str(policy.get("generation_thinking") or DEFAULT_GENERATION_THINKING)
    if mode != GENERATION_THINKING_MODEL_MAX:
        return mode
    raw_mapping = policy.get("model_thinking_levels")
    mapping = raw_mapping if isinstance(raw_mapping, dict) else {}
    normalized_mapping = {
        _normalized_model_id(str(key)): str(value).strip().lower() for key, value in mapping.items()
    }
    normalized_model = _normalized_model_id(model)
    configured = normalized_mapping.get(normalized_model)
    supported = _openrouter_supported_thinking_levels(normalized_model)
    if supported:
        highest = supported[0]
        if configured is None:
            return highest
        if configured != highest and bool(policy.get("require_highest_thinking")):
            raise ValueError(
                f"OpenRouter model {model!r} freezes thinking={configured!r}, "
                f"but the packaged registry highest supported level is {highest!r}"
            )
        return highest
    return configured or str(
        policy.get("default_thinking_level") or DEFAULT_GENERATION_THINKING_FALLBACK
    )


def with_openrouter_model_capabilities(
    config: ChatConfig,
    model: str | None,
) -> ChatConfig:
    if config.model_capabilities is not None:
        return config
    capabilities = openrouter_static_capabilities(model or "")
    if capabilities is None:
        return config
    return config.model_copy(update={"model_capabilities": capabilities})


def generation_chat_config(
    policy: dict[str, Any],
    *,
    model: str | None = None,
    tool_choice: Any | None = None,
) -> ChatConfig:
    mode = generation_thinking_for_model(model, policy)
    budget_level = ThinkingLevel.MAX if mode == "max" else ThinkingLevel(mode)
    thinking_level: ThinkingLevel | str = "max" if mode == "max" else budget_level
    raw_budget = policy.get("thinking_budget_tokens")
    thinking_budget = (
        int(raw_budget)
        if isinstance(raw_budget, int | float) and not isinstance(raw_budget, bool)
        else THINKING_BUDGETS.get(budget_level, THINKING_BUDGETS[ThinkingLevel.XHIGH])
    )
    return with_openrouter_model_capabilities(
        ChatConfig(
            max_tokens=int(policy.get("max_tokens") or ChatConfig().max_tokens),
            temperature=policy.get("temperature"),
            thinking=bool(policy.get("thinking_enabled", True)),
            thinking_level=thinking_level,
            thinking_budget_tokens=thinking_budget,
            tool_choice=tool_choice,
        ),
        model,
    )


def _experiment_member_provider_config(
    member: DracoEnsembleMemberConfig,
    *,
    templates: list[ProviderConfig],
) -> ProviderConfig:
    provider_id = member.provider.strip().lower()
    template = next(
        (item for item in templates if str(item.provider or "").strip().lower() == provider_id),
        None,
    )
    api_key = str(os.environ.get(member.api_key_env, "") or "").strip()
    if template is not None:
        return replace(
            template,
            provider=provider_id,
            model=member.model,
            api_key=api_key or template.api_key,
            base_url=member.base_url or template.base_url,
        )
    spec = get_provider_spec(provider_id)
    env_name = member.api_key_env or spec.env_key
    api_key = api_key or str(os.environ.get(env_name, "") or "").strip()
    return ProviderConfig(
        provider=provider_id,
        model=member.model,
        api_key=api_key,
        base_url=member.base_url or spec.default_base_url,
    )


def align_b2_provider_to_g12(
    provider: EnsembleProvider,
    experiment: DracoExperimentConfig,
) -> EnsembleProvider:
    """Apply the effective member settings observed in the reference G12 run."""

    ensemble = experiment.ensemble
    templates = [
        *(member.provider_config for member in provider.proposers),
        provider.aggregator.provider_config,
    ]

    def _member(member: DracoEnsembleMemberConfig) -> EnsembleMemberConfig:
        return EnsembleMemberConfig(
            provider_config=_experiment_member_provider_config(
                member,
                templates=templates,
            ),
            label=member.label,
            temperature=member.temperature,
            max_tokens=member.max_tokens,
            thinking=member.thinking,
            k=member.k,
        )

    pre_alignment = {
        "profile": provider.profile_name,
        "min_successful_proposers": provider.min_successful_proposers,
        "proposer_timeout_seconds": provider.proposer_timeout_seconds,
        "aggregator_timeout_seconds": provider.aggregator_timeout_seconds,
        "quorum_grace_seconds": provider.quorum_grace_seconds,
        "selection_plan": dict(provider.selection_plan),
    }
    provider.proposers = [_member(member) for member in ensemble.proposers]
    provider.aggregator = _member(ensemble.aggregator)
    provider.profile_name = ensemble.profile_name
    provider.min_successful_proposers = ensemble.min_successful_proposers
    provider.proposer_timeout_seconds = experiment.timeouts.proposer_seconds
    provider.aggregator_timeout_seconds = experiment.timeouts.aggregator_seconds
    provider.candidate_max_chars = ensemble.candidate_max_chars
    provider.shuffle_candidates = ensemble.shuffle_candidates
    provider.record_candidates = ensemble.record_candidates
    provider.proposer_tools = ensemble.proposer_tools
    provider.aggregator_tools = ensemble.aggregator_tools
    recovery_policy = aggregator_recovery_policy(experiment)
    apply_aggregator_recovery_policy(provider, recovery_policy)
    provider.quorum_grace_seconds = ensemble.quorum_grace_seconds
    provider.all_failed_policy = ensemble.all_failed_policy
    provider._member_request_budget_bindings = {}

    member_generation = [
        {
            "role": "proposer",
            "provider": member.provider_config.provider,
            "model": member.provider_config.model,
            "temperature": member.temperature,
            "max_tokens": member.max_tokens,
            "thinking": member.thinking,
            "thinking_budget_tokens": experiment.generation.thinking_budget_tokens,
            "k": member.k,
        }
        for member in provider.proposers
    ]
    member_generation.append(
        {
            "role": "aggregator",
            "provider": provider.aggregator.provider_config.provider,
            "model": provider.aggregator.provider_config.model,
            "temperature": provider.aggregator.temperature,
            "max_tokens": provider.aggregator.max_tokens,
            "thinking": provider.aggregator.thinking,
            "thinking_budget_tokens": experiment.generation.thinking_budget_tokens,
            "k": provider.aggregator.k,
        }
    )
    proposer_models = [
        member.provider_config.model
        for member in provider.proposers
        for _ in range(max(1, int(member.k or 1)))
    ]
    selected_proposers = [
        f"{member.provider_config.provider}:{member.provider_config.model}"
        for member in provider.proposers
        for _ in range(max(1, int(member.k or 1)))
    ]
    aggregator_model = provider.aggregator.provider_config.model
    selected_aggregator = f"{provider.aggregator.provider_config.provider}:{aggregator_model}"
    provider.selection_plan = {
        "strategy": experiment.routing.selection_mode,
        "selection_mode": experiment.routing.selection_mode,
        "profile": provider.profile_name,
        "proposer_models": proposer_models,
        "aggregator_model": aggregator_model,
        "proposer_count": len(provider.proposers),
        "proposer_sample_count": sum(member.k for member in provider.proposers),
        "selected_P": selected_proposers,
        "selected_A": selected_aggregator,
        "benchmark_alignment": {
            "id": experiment.profile_id,
            "reference_commit": experiment.reference.source_commit,
            "reference_group": experiment.reference.group,
            "reference_profile": experiment.reference.profile,
        },
        "pre_alignment": pre_alignment,
        "configured_min_successful_proposers": provider.min_successful_proposers,
        "effective_min_successful_proposers": provider.min_successful_proposers,
        "configured_proposer_timeout_seconds": provider.proposer_timeout_seconds,
        "effective_proposer_timeout_seconds": provider.proposer_timeout_seconds,
        "configured_aggregator_timeout_seconds": provider.aggregator_timeout_seconds,
        "effective_aggregator_timeout_seconds": provider.aggregator_timeout_seconds,
        "configured_shuffle_candidates": provider.shuffle_candidates,
        "effective_shuffle_candidates": provider.shuffle_candidates,
        "quorum_grace_seconds": provider.quorum_grace_seconds,
        "wait_for_all_proposers": ensemble.wait_for_all_proposers,
        "all_failed_policy": provider.all_failed_policy,
        "candidate_max_chars": provider.candidate_max_chars,
        "proposer_tools": provider.proposer_tools,
        "aggregator_tools": provider.aggregator_tools,
        **recovery_policy,
        "record_candidates": provider.record_candidates,
        "require_highest_thinking": experiment.generation.require_highest_thinking,
        "member_generation": member_generation,
    }
    return provider


def enforce_draco_legal_proposer_quorum(provider: Any) -> Any:
    """Apply the experiment's two-thirds proposer quorum to a live ensemble."""

    proposers = list(getattr(provider, "proposers", []) or [])
    proposer_count = sum(max(1, int(getattr(member, "k", 1) or 1)) for member in proposers)
    if proposer_count <= 0:
        return provider
    required = legal_proposer_quorum(proposer_count)
    configured = int(getattr(provider, "min_successful_proposers", 1) or 1)
    provider.min_successful_proposers = required
    selection_plan = dict(getattr(provider, "selection_plan", {}) or {})
    selection_plan.setdefault("configured_min_successful_proposers", configured)
    selection_plan["effective_min_successful_proposers"] = required
    selection_plan["legal_min_successful_proposers"] = required
    selection_plan["legal_quorum_policy"] = "ceil(2*n/3)"
    provider.selection_plan = selection_plan
    return provider


def apply_generation_policy_to_profile(profile: Any, policy: dict[str, Any]) -> Any:
    preserve_temperature = bool(getattr(profile, "preserve_member_temperature", False))

    def _apply_member_policy(member: Any) -> Any:
        thinking = (
            generation_thinking_for_model(
                str(getattr(member, "model", "") or ""),
                policy,
            )
            if bool(policy.get("thinking_enabled", True))
            else "off"
        )
        update: dict[str, Any] = {
            "temperature": policy.get("temperature"),
            "thinking": thinking,
        }
        if preserve_temperature and getattr(member, "temperature", None) is not None:
            update.pop("temperature", None)
        if policy.get("max_tokens_overridden"):
            update["max_tokens"] = int(policy["max_tokens"])
        return member.model_copy(update=update)

    proposers = [_apply_member_policy(proposer) for proposer in profile.proposers]
    aggregator = _apply_member_policy(profile.aggregator)
    update: dict[str, Any] = {"proposers": proposers, "aggregator": aggregator}
    if getattr(profile, "candidate_scorer", None) is not None:
        update["candidate_scorer"] = _apply_member_policy(profile.candidate_scorer)
    return profile.model_copy(update=update)


def apply_generation_policy_to_ensemble_provider(
    provider: EnsembleProvider,
    policy: dict[str, Any],
) -> EnsembleProvider:
    """Apply the run-wide frozen generation policy to realized ensemble members."""

    def _apply(member: EnsembleMemberConfig) -> EnsembleMemberConfig:
        model = member.provider_config.model
        thinking = (
            generation_thinking_for_model(model, policy)
            if bool(policy.get("thinking_enabled", True))
            else "off"
        )
        updates: dict[str, Any] = {
            "temperature": policy.get("temperature"),
            "thinking": thinking,
        }
        if policy.get("max_tokens_overridden"):
            updates["max_tokens"] = int(policy["max_tokens"])
        return replace(member, **updates)

    provider.proposers = [_apply(member) for member in provider.proposers]
    provider.aggregator = _apply(provider.aggregator)
    provider._member_request_budget_bindings = {}
    member_generation = [
        {
            "role": role,
            "label": member.label,
            "provider": member.provider_config.provider,
            "model": member.provider_config.model,
            "temperature": member.temperature,
            "max_tokens": member.max_tokens,
            "thinking": member.thinking,
            "thinking_budget_tokens": generation_chat_config(
                policy,
                model=member.provider_config.model,
            ).thinking_budget_tokens,
            "k": member.k,
        }
        for role, member in [
            *(("proposer", member) for member in provider.proposers),
            ("aggregator", provider.aggregator),
        ]
    ]
    provider.selection_plan = {
        **dict(provider.selection_plan),
        "generation_policy_applied": True,
        "member_generation": member_generation,
    }
    return provider


def validate_strict_openrouter_ensemble_members(
    provider: EnsembleProvider,
    policy: dict[str, Any],
    *,
    fallback_config: ProviderConfig | None = None,
    allow_unpinned_openrouter: bool = False,
) -> None:
    """Fail before billing when a realized member cannot honor the frozen request."""

    strict_routing = os.environ.get("OPENSQUILLA_PROVIDER_ROUTING_STRICT", "").strip().lower()
    truthy = {"1", "true", "yes", "on", "enabled"}
    strict_routing_enabled = strict_routing in truthy
    require_parameters = (
        os.environ.get("OPENSQUILLA_OPENROUTER_REQUIRE_PARAMETERS", "").strip().lower()
    )
    require_parameters_enabled = require_parameters in truthy
    requests: list[tuple[ProviderConfig, str]] = [
        (member.provider_config, str(member.thinking or ""))
        for member in [*provider.proposers, provider.aggregator]
    ]
    if fallback_config is not None:
        fallback_thinking = (
            generation_thinking_for_model(fallback_config.model, policy)
            if bool(policy.get("thinking_enabled", True))
            else "off"
        )
        requests.append((fallback_config, fallback_thinking))
    for cfg, raw_thinking in requests:
        if cfg.provider.strip().lower() != "openrouter":
            continue
        model = cfg.model.strip()
        thinking = raw_thinking.strip().lower()
        thinking_enabled = bool(policy.get("thinking_enabled", True))
        if thinking_enabled:
            supported = _openrouter_supported_thinking_levels(model)
            if strict_routing_enabled and supported and thinking not in supported:
                raise ValueError(
                    f"OpenRouter model {model!r} does not support "
                    f"frozen thinking={thinking!r}; supported levels are "
                    f"{list(supported)!r}"
                )
            if (
                strict_routing_enabled
                and supported
                and bool(policy.get("require_highest_thinking"))
                and thinking != supported[0]
            ):
                raise ValueError(
                    f"OpenRouter model {model!r} requires highest thinking="
                    f"{supported[0]!r}, not {thinking!r}"
                )
        if thinking_enabled and thinking not in {"", "off"}:
            capabilities = openrouter_static_capabilities(model)
            if capabilities is None or not capabilities.supports_reasoning:
                raise ValueError(
                    f"OpenRouter model {model!r} cannot prove support for "
                    f"frozen thinking={thinking!r}"
                )
        if (
            strict_routing_enabled
            and not allow_unpinned_openrouter
            and model not in cfg.provider_routing
        ):
            raise ValueError(f"OpenRouter model {model!r} has no strict upstream provider pin")
        if strict_routing_enabled and not require_parameters_enabled:
            raise ValueError(
                "strict OpenRouter routing requires parameter compatibility enforcement"
            )


def compact_chat_config(
    config: ChatConfig | None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _ = policy
    if config is None:
        config = generation_chat_config(generation_thinking_policy())
    level = config.thinking_level
    return {
        "thinking": config.thinking,
        "thinking_level": level.value if isinstance(level, ThinkingLevel) else level,
        "thinking_budget_tokens": config.thinking_budget_tokens,
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
    }


def profile_timeout_seconds(
    profile: Any,
    *,
    requested_timeout: float | None = None,
    proposer_timeout_override: float | None = None,
    aggregator_timeout_override: float | None = None,
    expand_to_requested_timeout: bool = False,
) -> tuple[float, float]:
    moa_layers = max(1, int(getattr(profile, "moa_layers", 1) or 1))
    proposer_timeout = max(
        DEFAULT_PROFILE_PROPOSER_TIMEOUT_SECONDS,
        float(getattr(profile, "proposer_timeout_seconds", 0) or 0),
    )
    aggregator_timeout = max(
        DEFAULT_PROFILE_AGGREGATOR_TIMEOUT_SECONDS,
        float(getattr(profile, "aggregator_timeout_seconds", 0) or 0),
    )
    if proposer_timeout_override is not None and proposer_timeout_override > 0:
        proposer_timeout = float(proposer_timeout_override)
    if aggregator_timeout_override is not None and aggregator_timeout_override > 0:
        aggregator_timeout = float(aggregator_timeout_override)
    if not expand_to_requested_timeout or requested_timeout is None or requested_timeout <= 0:
        return proposer_timeout, aggregator_timeout
    available = max(0.0, float(requested_timeout) - PROFILE_TIMEOUT_MARGIN_SECONDS)
    base_budget = proposer_timeout + aggregator_timeout * moa_layers
    if available <= base_budget:
        return proposer_timeout, aggregator_timeout
    extra = available - base_budget
    return (
        proposer_timeout + extra * 0.25,
        aggregator_timeout + (extra * 0.75 / moa_layers),
    )


def profile_aggregator_timeout_seconds(
    profile: Any,
    *,
    requested_timeout: float | None = None,
    aggregator_timeout_override: float | None = None,
    expand_to_requested_timeout: bool = False,
) -> float:
    return profile_timeout_seconds(
        profile,
        requested_timeout=requested_timeout,
        aggregator_timeout_override=aggregator_timeout_override,
        expand_to_requested_timeout=expand_to_requested_timeout,
    )[1]


def parse_maybe_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped:
        return value
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


def extract_json_object(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def coerce_weight(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 1


def rubric_criteria(task: dict[str, Any]) -> list[dict[str, Any]]:
    raw_items = task.get("rubric_items") or task.get("criteria")
    if isinstance(raw_items, list):
        return [
            item
            for index, raw in enumerate(raw_items, start=1)
            if (item := normalize_criterion(raw, index=index)) is not None
        ]
    rubric = parse_maybe_json(task.get("rubric"))
    if not isinstance(rubric, dict):
        return []
    items: list[dict[str, Any]] = []
    for section in rubric.get("sections") or []:
        if not isinstance(section, dict):
            continue
        section_id = str(section.get("id") or "").strip()
        section_title = str(section.get("title") or section_id).strip()
        for raw in section.get("criteria") or []:
            item = normalize_criterion(
                raw,
                index=len(items) + 1,
                section_id=section_id,
                section_title=section_title,
            )
            if item is not None:
                items.append(item)
    return items


def normalize_criterion(
    raw: Any,
    *,
    index: int,
    section_id: str = "",
    section_title: str = "",
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    requirement = str(
        raw.get("requirement")
        or raw.get("criterion")
        or raw.get("description")
        or raw.get("text")
        or ""
    ).strip()
    if not requirement:
        return None
    return {
        "id": str(raw.get("id") or f"criterion-{index}"),
        "section_id": str(raw.get("section_id") or section_id or "rubric"),
        "section_title": str(raw.get("section_title") or section_title or section_id or "Rubric"),
        "weight": coerce_weight(raw.get("weight")),
        "requirement": requirement,
    }


def parse_verdict(value: Any) -> bool | None:
    verdict = str(value or "").strip().upper()
    if verdict in {"MET", "TRUE", "YES", "PASS", "PASSED", "1"}:
        return True
    if verdict in {"UNMET", "FALSE", "NO", "FAIL", "FAILED", "0"}:
        return False
    return None


def clamp_percent(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


LEGACY_JUDGE_SCORE_KEYS = (
    "accuracy",
    "completeness",
    "objectivity",
    "citation",
)


def valid_legacy_judge_scores(result: Mapping[str, Any]) -> dict[str, float] | None:
    scores = result.get("scores")
    if not isinstance(scores, Mapping) or set(scores) != set(LEGACY_JUDGE_SCORE_KEYS):
        return None
    validated: dict[str, float] = {}
    for key in LEGACY_JUDGE_SCORE_KEYS:
        value = scores.get(key)
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        score = float(value)
        if not math.isfinite(score) or score < 1.0 or score > 5.0:
            return None
        validated[key] = score
    return validated


def normalize_legacy_judge_result(result: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(result)
    normalized.setdefault("mode", "legacy_dimension_score")
    normalized["score_scale"] = "0-100"

    scores = valid_legacy_judge_scores(normalized)
    raw_total = normalized.get("total")
    if isinstance(raw_total, int | float) and not isinstance(raw_total, bool):
        normalized["raw_total"] = float(raw_total)

    if scores is not None:
        normalized["scores"] = scores
        normalized_score = sum(scores.values()) / 20.0 * 100.0
        normalized["normalized_score"] = clamp_percent(normalized_score)
        normalized["total"] = normalized["normalized_score"]
        normalized["score_status"] = "complete"
        normalized["judge_error_count"] = 0
        normalized.pop("error", None)
    else:
        normalized["normalized_score"] = None
        normalized["total"] = None
        normalized["score_status"] = "incomplete"
        normalized["judge_error_count"] = max(
            1,
            coerce_metric_int(normalized.get("judge_error_count")),
        )
        normalized["error"] = "judge_json_schema_invalid"
    return normalized


def judge_completion_reasons(
    row: Mapping[str, Any],
    *,
    judge_required: bool,
) -> list[str]:
    """Return Judge-only incompleteness reasons for a stored result row."""

    if not judge_required:
        return []
    judge = row.get("judge")
    reasons: list[str] = []
    if not isinstance(judge, Mapping):
        return ["judge_incomplete", "judge_errors", "missing_quality_total"]

    if judge.get("score_status") != "complete" or quality_total(dict(judge)) is None:
        reasons.append("judge_incomplete")
    if judge.get("error") or coerce_metric_int(judge.get("judge_error_count")) != 0:
        reasons.append("judge_errors")
    expected_quality = quality_total(dict(judge))
    stored_quality = row.get("quality_total")
    if isinstance(stored_quality, bool) or not isinstance(stored_quality, int | float):
        reasons.append("missing_quality_total")
    elif expected_quality is None or not math.isclose(
        float(stored_quality),
        float(expected_quality),
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        reasons.append("quality_total_mismatch")
    return reasons


def inherited_provider_config(config: GatewayConfig) -> ProviderConfig:
    runtime = resolve_llm_runtime_config(config)
    base_url = runtime.base_url[:-3] if runtime.base_url.endswith("/v1") else runtime.base_url
    return ProviderConfig(
        provider=runtime.provider,
        model=runtime.model,
        api_key=runtime.api_key,
        base_url=base_url,
        proxy=runtime.proxy,
        provider_routing=runtime.provider_routing,
    )


def build_single_provider(
    *,
    inherited: ProviderConfig,
    group: str,
    model: str,
    dry_run: bool,
    provider: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    api_key_env: str | None = None,
):
    if dry_run:
        return DryProvider(model=model, group=group)
    resolved_api_key = api_key if api_key is not None else inherited.api_key
    if api_key_env:
        resolved_api_key = os.environ.get(api_key_env, resolved_api_key)
    cfg = ProviderConfig(
        provider=provider or inherited.provider,
        model=model,
        api_key=resolved_api_key,
        base_url=base_url or inherited.base_url,
        proxy=inherited.proxy,
        provider_routing=inherited.provider_routing if provider is None else {},
    )
    return ModelSelector(SelectorConfig(primary=cfg)).resolve()


def build_task_analyzer_provider(
    routed_config: ProviderConfig,
    *,
    provider_id: str,
    model_id: str,
):
    """Resolve the analyzer without dropping routing or replay-safety fields."""

    analyzer_cfg = replace(
        routed_config,
        provider=provider_id,
        model=model_id,
        replay_provider_state=False,
    )
    return ModelSelector(SelectorConfig(primary=analyzer_cfg)).resolve()


def task_analyzer_usage_row(
    usage: Mapping[str, Any],
    *,
    provider_id: str,
    model_id: str,
    source: str,
    fallback_reason: str,
) -> dict[str, Any]:
    usage_unknown = bool(usage.get("usage_unknown")) or not bool(usage)
    provider_usage = (
        dict(usage.get("provider_usage"))
        if isinstance(usage.get("provider_usage"), Mapping)
        else {}
    )
    provider_usage.update(
        {
            "task_analysis_source": source,
            "fallback_reason": fallback_reason,
            "usage_unknown": usage_unknown,
        }
    )
    physical_attempt_id = str(usage.get("physical_attempt_id") or uuid.uuid4().hex)
    provider_usage["physical_attempt_id"] = physical_attempt_id
    row = {
        "role": "unknown_request" if usage_unknown else "task_analyzer",
        "label": "task_analyzer",
        "request_count": 1,
        "attempt": max(1, coerce_metric_int(usage.get("attempt"))),
        "physical_attempt_id": physical_attempt_id,
        "provider": str(usage.get("provider") or ""),
        "model": str(usage.get("model") or ""),
        "requested_provider": str(usage.get("requested_provider") or provider_id or ""),
        "requested_model": str(usage.get("requested_model") or model_id or ""),
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "reasoning_tokens": int(usage.get("reasoning_tokens") or 0),
        "cached_tokens": int(usage.get("cached_tokens") or 0),
        "cache_write_tokens": int(usage.get("cache_write_tokens") or 0),
        "billed_cost": float(usage.get("billed_cost") or 0.0),
        "cost_source": str(usage.get("cost_source") or "none"),
        "provider_usage": provider_usage,
    }
    billing_receipt = usage.get("billing_receipt", usage.get("billingReceipt"))
    if billing_receipt is not None:
        row["billing_receipt"] = billing_receipt
    row["billed_cost"] = trusted_provider_billed_cost(row)
    exact_cost = exact_provider_usage_cost(row)
    if exact_cost is not None:
        row["cost_source"] = "provider_billed"
    elif billing_receipt is not None:
        row["cost_source"] = "unavailable"
    return row


def task_analyzer_usage_rows(
    usage: Mapping[str, Any],
    *,
    provider_id: str,
    model_id: str,
    source: str,
    fallback_reason: str,
) -> list[dict[str, Any]]:
    """Expand analyzer retry accounting into one row per physical request."""

    raw_attempts = usage.get("physical_attempts")
    attempts = (
        [dict(item) for item in raw_attempts if isinstance(item, Mapping)]
        if isinstance(raw_attempts, list)
        else []
    )
    raw_declared_count = usage.get("attempt_count")
    if (
        not attempts
        and isinstance(raw_declared_count, int)
        and not isinstance(raw_declared_count, bool)
        and raw_declared_count == 0
    ):
        return []
    declared_count = max(
        1,
        coerce_metric_int(usage.get("attempt_count")),
        len(attempts),
    )
    if not attempts and declared_count == 1:
        single = dict(usage)
        single.pop("physical_attempts", None)
        single.pop("attempt_count", None)
        single.setdefault("attempt", 1)
        return [
            task_analyzer_usage_row(
                single,
                provider_id=provider_id,
                model_id=model_id,
                source=source,
                fallback_reason=fallback_reason,
            )
        ]

    attempts_by_ordinal: dict[int, dict[str, Any]] = {}
    for position, attempt_usage in enumerate(attempts, start=1):
        ordinal = max(
            1,
            coerce_metric_int(attempt_usage.get("attempt")) or position,
        )
        if ordinal in attempts_by_ordinal:
            continue
        attempts_by_ordinal[ordinal] = attempt_usage

    aggregate_provider_usage = (
        usage.get("provider_usage") if isinstance(usage.get("provider_usage"), Mapping) else {}
    )
    aggregate_evidence = {
        "attempt_count": declared_count,
        "provider": str(usage.get("provider") or ""),
        "model": str(usage.get("model") or ""),
        "requested_provider": str(usage.get("requested_provider") or provider_id),
        "requested_model": str(usage.get("requested_model") or model_id),
        "input_tokens": coerce_metric_int(usage.get("input_tokens")),
        "output_tokens": coerce_metric_int(usage.get("output_tokens")),
        "reasoning_tokens": coerce_metric_int(usage.get("reasoning_tokens")),
        "cached_tokens": coerce_metric_int(usage.get("cached_tokens")),
        "cache_write_tokens": coerce_metric_int(usage.get("cache_write_tokens")),
        "billed_cost": float(usage.get("billed_cost") or 0.0),
        "cost_source": str(usage.get("cost_source") or "none"),
        "response_ids": [
            str(value)
            for value in aggregate_provider_usage.get("response_ids", [])
            if str(value).strip()
        ],
    }
    rows: list[dict[str, Any]] = []
    for ordinal in range(1, declared_count + 1):
        attempt_usage = dict(attempts_by_ordinal.get(ordinal) or {})
        if not attempt_usage:
            attempt_usage = {
                "attempt": ordinal,
                "physical_attempt_id": uuid.uuid4().hex,
                "requested_provider": provider_id,
                "requested_model": model_id,
                "usage_unknown": True,
                "unknown_reason": "per_attempt_receipt_unavailable",
                "provider_usage": {
                    "usage_unknown": True,
                    "unknown_reason": "per_attempt_receipt_unavailable",
                },
            }
            if ordinal == 1:
                attempt_usage["provider_usage"]["unallocated_aggregate_usage"] = aggregate_evidence
        rows.append(
            task_analyzer_usage_row(
                attempt_usage,
                provider_id=provider_id,
                model_id=model_id,
                source=source,
                fallback_reason=fallback_reason,
            )
        )
    return rows


def conservative_task_analyzer_usage_rows(
    usage: Any,
    *,
    provider_id: str,
    model_id: str,
    source: str,
    fallback_reason: str,
) -> list[dict[str, Any]]:
    """Recover analyzer retry cardinality and IDs without trusting parsing."""

    def safe_get(value: Any, key: str, default: Any = None) -> Any:
        try:
            return value.get(key, default) if isinstance(value, Mapping) else default
        except Exception:  # noqa: BLE001 - evidence objects may be malformed
            return default

    def safe_count(value: Any) -> int:
        if isinstance(value, bool):
            return 0
        if isinstance(value, int):
            return max(0, value)
        try:
            return max(0, int(str(value).strip()))
        except Exception:  # noqa: BLE001 - use observed attempts instead
            return 0

    raw_attempts = safe_get(usage, "physical_attempts", [])
    attempts = raw_attempts if isinstance(raw_attempts, list) else []
    declared_raw = safe_get(usage, "attempt_count")
    declared_count = safe_count(declared_raw)
    if (
        declared_raw == 0
        and not isinstance(declared_raw, bool)
        and not attempts
    ):
        return []
    request_count = max(1, declared_count, len(attempts))
    attempts_by_ordinal: dict[int, Any] = {}
    for position, attempt in enumerate(attempts, start=1):
        ordinal = safe_count(safe_get(attempt, "attempt")) or position
        attempts_by_ordinal.setdefault(ordinal, attempt)

    rows: list[dict[str, Any]] = []
    for ordinal in range(1, request_count + 1):
        raw_attempt = attempts_by_ordinal.get(ordinal)
        if raw_attempt is None and request_count == 1:
            raw_attempt = usage
        try:
            attempt_payload = (
                dict(raw_attempt)
                if isinstance(raw_attempt, Mapping)
                else {}
            )
            attempt_payload.pop("physical_attempts", None)
            attempt_payload.pop("attempt_count", None)
            attempt_payload.setdefault("attempt", ordinal)
            row = task_analyzer_usage_row(
                attempt_payload,
                provider_id=provider_id,
                model_id=model_id,
                source=source,
                fallback_reason=fallback_reason,
            )
        except Exception:  # noqa: BLE001 - build a primitive unknown row
            try:
                physical_attempt_id = str(
                    safe_get(raw_attempt, "physical_attempt_id") or ""
                ).strip()
            except Exception:  # noqa: BLE001 - generate a stable-shape ID
                physical_attempt_id = ""
            physical_attempt_id = (
                physical_attempt_id or uuid.uuid4().hex
            )
            row = {
                "role": "unknown_request",
                "label": "task_analyzer",
                "request_count": 1,
                "attempt": ordinal,
                "physical_attempt_id": physical_attempt_id,
                "provider": "",
                "model": "",
                "requested_provider": str(provider_id or ""),
                "requested_model": str(model_id or ""),
                "input_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "cached_tokens": 0,
                "cache_write_tokens": 0,
                "billed_cost": 0.0,
                "cost_source": "none",
                "usage_unknown": True,
                "provider_usage": {
                    "physical_attempt_id": physical_attempt_id,
                    "usage_unknown": True,
                    "task_analysis_source": source,
                    "fallback_reason": fallback_reason,
                    "recovery_source": (
                        "analyzer_postprocess_primitive_fallback"
                    ),
                },
            }
        rows.append(row)
    return rows


def _bind_frozen_g1_retry_provenance(
    plan: dict[str, Any],
    *,
    initial_plan: Mapping[str, Any],
    excluded_proposer_identities: Sequence[str],
) -> None:
    """Bind a materialized reroute to the original paid analyzer decision."""

    from opensquilla.provider.ranking_router import (
        ROUTER_DYNAMIC_RETRY_ROUTING_SCHEMA,
        build_router_dynamic_task_analysis_reuse_binding,
    )

    exclusions = sorted(
        {
            str(identity or "").strip().lower()
            for identity in excluded_proposer_identities
            if str(identity or "").strip()
        }
    )
    initial_decision_id = str(initial_plan.get("decision_id") or "")
    if not initial_decision_id or not exclusions:
        raise ValueError("frozen G1 reroute lacks parent decision or exclusions")
    binding = build_router_dynamic_task_analysis_reuse_binding(initial_plan)
    plan["retry_parent_decision_id"] = initial_decision_id
    plan["retry_excluded_proposer_identities"] = exclusions
    plan["task_analysis_reused"] = True
    plan["task_analysis_reuse"] = binding
    retry_routing = dict(plan.get("retry_routing") or {})
    retry_routing.update(
        {
            "schema": ROUTER_DYNAMIC_RETRY_ROUTING_SCHEMA,
            "reason": "prior_attempt_reasoning_only_length",
            "parent_decision_id": initial_decision_id,
            "excluded_proposer_identities": exclusions,
            "task_analysis_reused": True,
            "task_analysis_source_decision_id": initial_decision_id,
            "task_analysis_reuse_sha256": binding["projection_sha256"],
        }
    )
    plan["retry_routing"] = retry_routing


def restore_g1_thinking_execution_prefix(
    provider: Any,
    *,
    target_plan: Mapping[str, Any],
    physical_execution_prefix: Mapping[str, Any] | None,
) -> None:
    """Restore the full cross-decision projected thinking receipt ledger."""

    if not isinstance(physical_execution_prefix, Mapping):
        return
    from opensquilla.provider.thinking_execution import (
        restore_projected_thinking_execution,
    )

    restore_projected_thinking_execution(
        provider,
        target_plan=target_plan,
        projected_plan=physical_execution_prefix,
    )


async def build_experiment_provider(
    *,
    config: GatewayConfig,
    inherited: ProviderConfig,
    group: str,
    prompt: str,
    dry_run: bool,
    enable_proposer_tools: bool,
    ensemble_proposer_timeout: float | None,
    ensemble_aggregator_timeout: float | None,
    experiment_config: DracoExperimentConfig | None = None,
    g1_registry_contract: Mapping[str, Any] | None = None,
    generation_policy: dict[str, Any] | None = None,
    tools: Sequence[ToolDefinition] | None = None,
    frozen_g1_lifecycle: Mapping[str, Any] | None = None,
) -> ProviderBuildResult:
    """Build one DRACO provider through the same routing primitives as runtime.py."""

    spec = GROUP_SPECS[group]
    started = time.monotonic()
    kind = spec["kind"]
    b2_experiment = experiment_config if group == "B2" else None
    g1_routing = (
        experiment_config.g1_routing if group == "G1" and experiment_config is not None else None
    )
    if group == "G1" and g1_routing is None:
        raise ValueError("G1 requires a versioned g1_routing experiment contract")
    if frozen_g1_lifecycle is not None and (
        group != "G1"
        or dry_run
        or frozen_g1_lifecycle.get("schema")
        != G1_CROSS_WAVE_FROZEN_LIFECYCLE_SCHEMA
    ):
        raise ValueError("frozen G1 lifecycle is invalid for this provider build")
    resolved_g1_registry_contract = (
        dict(g1_registry_contract)
        if isinstance(g1_registry_contract, Mapping)
        else g1_routing.model_dump(mode="json", exclude_none=True)
        if g1_routing is not None
        else None
    )
    recovery_policy = (
        aggregator_recovery_policy(experiment_config)
        if experiment_config is not None
        else {
            "aggregator_recovery_mode": config.llm_ensemble.aggregator_recovery_mode,
            "aggregator_recovery_top_k": config.llm_ensemble.aggregator_recovery_top_k,
            "aggregator_max_tokens_cap": config.llm_ensemble.aggregator_max_tokens_cap,
            "aggregator_visible_answer_reserve_tokens": (
                config.llm_ensemble.aggregator_visible_answer_reserve_tokens
            ),
        }
    )
    if dry_run:
        if kind in {"single", "router_single"}:
            model = spec.get("model") or "dry-routed-single"
            dry_provider: Any = DryProvider(model=model, group=group)
        else:
            dry_provider = DryEnsembleProvider(
                group=group,
                profile=(
                    b2_experiment.ensemble.profile_name
                    if b2_experiment is not None
                    else spec["selection_mode"]
                ),
                proposer_models=(
                    [
                        member.model
                        for member in b2_experiment.ensemble.proposers
                        for _ in range(max(1, int(member.k or 1)))
                    ]
                    if b2_experiment is not None
                    else None
                ),
                model=(
                    b2_experiment.ensemble.aggregator.model
                    if b2_experiment is not None
                    else "dry-aggregator"
                ),
                selection_mode=str(spec.get("selection_mode") or ""),
            )
            apply_aggregator_recovery_policy(dry_provider, recovery_policy)
            if b2_experiment is not None:
                dry_provider.min_successful_proposers = legal_proposer_quorum(
                    len(dry_provider.proposer_models)
                )
                dry_provider.proposer_timeout_seconds = b2_experiment.timeouts.proposer_seconds
                dry_provider.aggregator_timeout_seconds = b2_experiment.timeouts.aggregator_seconds
                dry_provider.quorum_grace_seconds = b2_experiment.ensemble.quorum_grace_seconds
                dry_provider.candidate_max_chars = b2_experiment.ensemble.candidate_max_chars
                dry_provider.proposer_tools = b2_experiment.ensemble.proposer_tools
                dry_provider.aggregator_tools = b2_experiment.ensemble.aggregator_tools
        dry_routing_trace: dict[str, Any] = {
            "dry_run": True,
            "kind": kind,
            **dict(spec),
        }
        if b2_experiment is not None:
            proposer_models = [
                member.model
                for member in b2_experiment.ensemble.proposers
                for _ in range(max(1, int(member.k or 1)))
            ]
            selected_proposers = [
                f"{member.provider.strip().lower()}:{member.model}"
                for member in b2_experiment.ensemble.proposers
                for _ in range(max(1, int(member.k or 1)))
            ]
            aggregator = b2_experiment.ensemble.aggregator
            member_generation = [
                {"role": "proposer", **member.model_dump(mode="json")}
                for member in b2_experiment.ensemble.proposers
            ]
            member_generation.append({"role": "aggregator", **aggregator.model_dump(mode="json")})
            dry_routing_trace.update(
                {
                    "benchmark_alignment": b2_experiment.profile_id,
                    "profile": b2_experiment.ensemble.profile_name,
                    "selection_plan": {
                        "strategy": b2_experiment.routing.selection_mode,
                        "selection_mode": b2_experiment.routing.selection_mode,
                        "profile": b2_experiment.ensemble.profile_name,
                        "proposer_models": proposer_models,
                        "aggregator_model": aggregator.model,
                        "proposer_count": len(b2_experiment.ensemble.proposers),
                        "proposer_sample_count": sum(
                            member.k for member in b2_experiment.ensemble.proposers
                        ),
                        "selected_P": selected_proposers,
                        "selected_A": f"{aggregator.provider.strip().lower()}:{aggregator.model}",
                        "configured_min_successful_proposers": (
                            b2_experiment.ensemble.min_successful_proposers
                        ),
                        "effective_min_successful_proposers": (
                            legal_proposer_quorum(len(proposer_models))
                        ),
                        "effective_proposer_timeout_seconds": (
                            b2_experiment.timeouts.proposer_seconds
                        ),
                        "effective_aggregator_timeout_seconds": (
                            b2_experiment.timeouts.aggregator_seconds
                        ),
                        "effective_shuffle_candidates": (b2_experiment.ensemble.shuffle_candidates),
                        "proposer_tools": b2_experiment.ensemble.proposer_tools,
                        "aggregator_tools": b2_experiment.ensemble.aggregator_tools,
                        "wait_for_all_proposers": (b2_experiment.ensemble.wait_for_all_proposers),
                        "member_generation": member_generation,
                    },
                }
            )
            dry_provider.selection_plan = dict(dry_routing_trace["selection_plan"])
        elif g1_routing is not None:
            from opensquilla.provider.ranking_router import (
                TaskAnalysisResult,
                build_request_context,
                dynamic_output_token_budgets,
                fallback_task_profile,
                ranking_config_snapshot,
            )

            thinking_assignment_enabled = (
                config.llm_ensemble.ranking_thinking_assignment_enabled is True
            )
            ranking_config = ranking_config_snapshot(
                thinking_assignment_enabled=thinking_assignment_enabled,
            )
            dry_config = config.model_copy(deep=True)
            dry_ensemble = dry_config.llm_ensemble
            dry_ensemble.enabled = True
            dry_ensemble.selection_mode = "router_dynamic"
            dry_ensemble.ranking_user_profile_generation_enabled = False
            dry_ensemble.ranking_user_profile_enabled = False
            apply_aggregator_recovery_policy(dry_ensemble, recovery_policy)
            if thinking_assignment_enabled:
                configured_output_tokens, _ = (
                    resolve_effective_generation_request_parameters(
                        llm_config=dry_config.llm,
                        generation_policy=generation_policy,
                    )
                )
            else:
                configured_output_tokens = int(
                    getattr(dry_config.llm, "max_tokens", 0) or 0
                )
            candidate_output_tokens, aggregator_output_tokens = dynamic_output_token_budgets(
                configured_output_tokens=configured_output_tokens,
                candidate_max_chars=int(dry_ensemble.candidate_max_chars or 0),
                ranking_config=ranking_config,
            )
            request_context = build_request_context(
                message=prompt,
                turn_metadata={},
                attachments=[],
                candidate_output_tokens=candidate_output_tokens,
                aggregator_output_tokens=aggregator_output_tokens,
                ranking_config=ranking_config,
            )
            task_profile = fallback_task_profile(
                routed_tier="c1",
                request_context=request_context,
                ranking_config=ranking_config,
            )
            dry_ranking_inputs = {
                "decision_id": ("dry-" + hashlib.sha256(prompt.encode()).hexdigest()[:24]),
                "task_analysis": TaskAnalysisResult(
                    profile=task_profile,
                    source="dry_run_fallback",
                    schema_valid=False,
                    confidence=0.0,
                    fallback_reason="dry_run_no_analyzer_call",
                ),
                "user_profile": None,
                "request_context": request_context,
                "ranking_config": ranking_config,
                    "generation_policy": (
                        dict(generation_policy)
                        if generation_policy is not None
                        else None
                        if thinking_assignment_enabled
                        else {}
                    ),
                "registry_allowlist": resolved_g1_registry_contract,
            }
            if thinking_assignment_enabled:
                dry_ranking_inputs["request_tools_present"] = bool(tools)
            dry_dynamic_provider = build_ensemble_provider_from_config(
                config=dry_config,
                inherited_provider_config=inherited,
                fallback_provider=None,
                turn_metadata={
                    "routed_tier": "c1",
                    "routing_confidence": 0.0,
                    "router_dynamic_task_text": prompt,
                },
                ranking_inputs=dry_ranking_inputs,
            )
            plan = copy.deepcopy(dry_dynamic_provider.selection_plan)
            proposer_models = list(plan.get("proposer_models") or [])
            aggregator_model = str(plan.get("aggregator_model") or "")
            dry_provider.proposer_models = proposer_models
            dry_provider.model = aggregator_model
            dry_provider.min_successful_proposers = legal_proposer_quorum(len(proposer_models))
            dry_routing_trace.update(
                {
                    "benchmark_alignment": g1_routing.profile_id,
                    "profile": "router_dynamic",
                    "selection_plan": plan,
                }
            )
            dry_provider.selection_plan = copy.deepcopy(plan)
        if isinstance(dry_provider, DryEnsembleProvider):
            dry_provider.selection_plan.update(recovery_policy)
            selected_aggregator = str(dry_provider.selection_plan.get("selected_A") or "").strip()
            if selected_aggregator:
                dry_provider.selection_plan.setdefault(
                    "aggregator_candidates", [selected_aggregator]
                )
            dry_routing_trace["aggregator_recovery_policy"] = {
                "schema": "opensquilla.ensemble-aggregator-recovery-policy/v1",
                "evidence_kind": "dry_run_policy_only",
                **dict(recovery_policy),
            }
            dry_routing_trace["selection_plan"] = copy.deepcopy(dry_provider.selection_plan)
        result = ProviderBuildResult(
            provider=dry_provider,
            prompt=prompt,
            setup_latency_ms=int((time.monotonic() - started) * 1000),
            routing_trace=dry_routing_trace,
        )
        attach_provider_setup(dry_provider, result)
        return result

    if kind == "single":
        provider = build_single_provider(
            inherited=inherited,
            group=group,
            model=spec["model"],
            dry_run=False,
            provider=spec.get("provider"),
            base_url=spec.get("base_url"),
            api_key=spec.get("api_key"),
            api_key_env=spec.get("api_key_env"),
        )
        result = ProviderBuildResult(
            provider=provider,
            prompt=prompt,
            setup_latency_ms=int((time.monotonic() - started) * 1000),
            routing_trace={
                "kind": kind,
                "selection": "fixed",
                "model": spec["model"],
            },
        )
        attach_provider_setup(provider, result)
        return result

    group_config = config.model_copy(deep=True)
    selector = ModelSelector(SelectorConfig(primary=inherited))
    fallback_provider = selector.resolve()
    session_key = f"draco:{group}:{hashlib.sha256(prompt.encode()).hexdigest()[:16]}"
    turn = TurnContext(
        message=prompt,
        session_key=session_key,
        config=group_config,
        provider=fallback_provider,
        model=inherited.model,
        tool_defs=[],
        system_prompt="",
        attachments=[],
        metadata={},
        raw_message=prompt,
    )
    if b2_experiment is not None and b2_experiment.routing.skip_single_model_router:
        # This quality-first profile retains G12's fixed B2 model mapping and
        # skips the single-model router. Keep the inherited provider as fallback.
        routed_provider = fallback_provider
        routed_config = inherited
        routing_trace: dict[str, Any] = {
            "kind": kind,
            "routing_applied": False,
            "routing_source": "b2_quality_first_profile",
            "routed_model": None,
            "applied_model": None,
            "benchmark_alignment": b2_experiment.profile_id,
        }
    else:
        turn = await run_pipeline(turn, [apply_squilla_router])
        routed_provider = apply_model_override(
            selector,
            turn.model or inherited.model,
            turn_metadata=turn.metadata,
            realign_routed_model=False,
        )
        routed_config = selector.current_config
        routing_trace = {
            "kind": kind,
            "routed_tier": turn.metadata.get("routed_tier"),
            "routed_model": turn.metadata.get("routed_model") or turn.model,
            "applied_model": turn.model,
            "routing_source": turn.metadata.get("routing_source"),
            "routing_confidence": turn.metadata.get("routing_confidence"),
            "routing_applied": turn.metadata.get("routing_applied"),
            "rollout_phase": turn.metadata.get("rollout_phase"),
        }
    routing_trace["fallback_model"] = routed_config.model
    if kind == "router_single":
        result = ProviderBuildResult(
            provider=routed_provider,
            prompt=turn.message,
            setup_latency_ms=int((time.monotonic() - started) * 1000),
            routing_trace=routing_trace,
        )
        attach_provider_setup(routed_provider, result)
        return result

    selection_mode = (
        b2_experiment.routing.selection_mode
        if b2_experiment is not None
        else spec["selection_mode"]
    )
    ensemble_cfg = group_config.llm_ensemble
    ensemble_cfg.enabled = True
    ensemble_cfg.selection_mode = selection_mode
    apply_aggregator_recovery_policy(ensemble_cfg, recovery_policy)
    if g1_routing is not None:
        ensemble_cfg.ranking_user_profile_generation_enabled = False
        ensemble_cfg.ranking_user_profile_enabled = bool(g1_routing.user_profile_enabled)
    ensemble_cfg.proposer_tools = (
        b2_experiment.ensemble.proposer_tools
        if b2_experiment is not None
        else bool(enable_proposer_tools)
    )
    ensemble_cfg.aggregator_tools = (
        b2_experiment.ensemble.aggregator_tools if b2_experiment is not None else True
    )
    ensemble_cfg.record_candidates = (
        b2_experiment.ensemble.record_candidates if b2_experiment is not None else True
    )
    ensemble_cfg.shuffle_candidates = (
        b2_experiment.ensemble.shuffle_candidates if b2_experiment is not None else False
    )
    if ensemble_proposer_timeout is not None:
        ensemble_cfg.proposer_timeout_seconds = float(ensemble_proposer_timeout)
    if ensemble_aggregator_timeout is not None:
        ensemble_cfg.aggregator_timeout_seconds = float(ensemble_aggregator_timeout)

    decision_id = uuid.uuid4().hex
    turn.metadata["ensemble_decision_id"] = decision_id
    turn.metadata["ensemble_enabled"] = True
    turn.metadata["routed_model_before_ensemble"] = turn.model or routed_config.model
    ranking_inputs: dict[str, Any] | None = None
    setup_usage: list[dict[str, Any]] = []
    if selection_mode == "router_dynamic":
        from opensquilla.provider.ranking_router import (
            TASK_ANALYZER_MODEL_ID,
            TASK_ANALYZER_PROVIDER_ID,
            TaskAnalyzerStreamCleanupError,
            analyze_task_with_provider,
            build_request_context,
            dynamic_output_token_budgets,
            mock_user_profile,
            ranking_config_snapshot,
        )

        thinking_assignment_enabled = (
            group_config.llm_ensemble.ranking_thinking_assignment_enabled is True
        )
        ranking_config = ranking_config_snapshot(
            thinking_assignment_enabled=thinking_assignment_enabled,
        )
        routing_extra = turn.metadata.get("routing_extra")
        routing_extra_map = routing_extra if isinstance(routing_extra, Mapping) else {}
        routed_tier = str(
            turn.metadata.get("routed_tier")
            or routing_extra_map.get("final_tier")
            or routing_extra_map.get("base_tier")
            or "c1"
        )
        try:
            routing_confidence = float(turn.metadata.get("routing_confidence") or 0.0)
        except (TypeError, ValueError):
            routing_confidence = 0.0
        user_profile_enabled = (
            bool(g1_routing.user_profile_enabled)
            if g1_routing is not None
            else bool(ensemble_cfg.ranking_user_profile_enabled)
        )
        if frozen_g1_lifecycle is not None:
            from opensquilla.provider.ranking_router import (
                TaskAnalysisResult,
                ranking_trace_replay_reasons,
            )

            initial_plan = frozen_g1_lifecycle.get("initial_plan")
            target_plan = frozen_g1_lifecycle.get("target_plan")
            exclusions = frozen_g1_lifecycle.get(
                "cumulative_excluded_proposer_identities"
            )
            if (
                not isinstance(initial_plan, Mapping)
                or not isinstance(target_plan, Mapping)
                or not isinstance(exclusions, list)
                or not all(isinstance(identity, str) for identity in exclusions)
                or target_plan.get("ranking_thinking_assignment_enabled")
                is not True
                or getattr(
                    ensemble_cfg,
                    "ranking_thinking_assignment_enabled",
                    False,
                )
                is not True
                or user_profile_enabled
            ):
                raise ValueError(
                    "frozen G1 lifecycle is incompatible with current config"
                )
            frozen_ranking_config = initial_plan.get("ranking_parameters")
            raw_profile = initial_plan.get("task_profile_pre_escalation")
            request_context = initial_plan.get("request_context")
            analyzer = initial_plan.get("task_analyzer")
            if (
                not isinstance(frozen_ranking_config, Mapping)
                or dict(ranking_config) != dict(frozen_ranking_config)
                or not isinstance(raw_profile, Mapping)
                or not isinstance(request_context, Mapping)
                or not isinstance(analyzer, Mapping)
                or ranking_trace_replay_reasons(initial_plan)
                or ranking_trace_replay_reasons(target_plan)
            ):
                raise ValueError(
                    "frozen G1 ranker evidence differs from the current registry/config"
                )
            profile_decimal_places = coerce_metric_int(
                (
                    frozen_ranking_config.get("trace")
                    if isinstance(
                        frozen_ranking_config.get("trace"),
                        Mapping,
                    )
                    else {}
                ).get("profile_decimal_places")
            )
            if (
                routed_tier != str(initial_plan.get("routed_tier") or "")
                or round(routing_confidence, profile_decimal_places)
                != float(initial_plan.get("routing_confidence") or 0.0)
            ):
                raise ValueError(
                    "frozen G1 Squilla route differs from current routing"
                )
            task_analysis = TaskAnalysisResult(
                profile=copy.deepcopy(dict(raw_profile)),
                source=str(analyzer.get("source") or ""),
                schema_valid=analyzer.get("schema_valid") is True,
                confidence=float(analyzer.get("confidence") or 0.0),
                analyzer_version=str(analyzer.get("analyzer_version") or ""),
                fallback_reason=str(analyzer.get("fallback_reason") or ""),
                usage=(
                    copy.deepcopy(dict(analyzer.get("usage") or {}))
                    if isinstance(analyzer.get("usage"), Mapping)
                    else {}
                ),
                provider_id=str(analyzer.get("provider") or ""),
                model_id=str(analyzer.get("model") or ""),
                normalization_warnings=tuple(
                    str(value)
                    for value in analyzer.get("normalization_warnings") or []
                ),
            )
            decision_id = str(target_plan.get("decision_id") or "")
            if not decision_id:
                raise ValueError("frozen G1 target decision is missing")
            turn.metadata["ensemble_decision_id"] = decision_id
            ranking_inputs = {
                "decision_id": decision_id,
                "task_analysis": task_analysis,
                "user_profile": None,
                "request_context": copy.deepcopy(dict(request_context)),
                "ranking_config": ranking_config,
                "generation_policy": (
                    dict(generation_policy)
                    if generation_policy is not None
                    else None
                    if thinking_assignment_enabled
                    else {}
                ),
                "registry_allowlist": resolved_g1_registry_contract,
                "request_tools_present": bool(tools),
            }
            if exclusions:
                ranking_inputs.update(
                    {
                        "retry_excluded_proposer_identities": list(exclusions),
                        "retry_parent_decision_id": str(
                            initial_plan.get("decision_id") or ""
                        ),
                        "task_analysis_reused": True,
                    }
                )
            routing_trace["task_analyzer"] = copy.deepcopy(dict(analyzer))
            routing_trace["task_analyzer_reuse"] = {
                "schema": "opensquilla.draco.task-analysis-reuse/v1",
                "source_decision_id": str(
                    initial_plan.get("decision_id") or ""
                ),
                "physical_request_count": 0,
                "historical_physical_request_count": coerce_metric_int(
                    frozen_g1_lifecycle.get(
                        "task_analyzer_physical_request_count"
                    )
                ),
                "excluded_proposer_identities": list(exclusions),
            }
        else:
            if thinking_assignment_enabled:
                configured_output_tokens, _ = (
                    resolve_effective_generation_request_parameters(
                        llm_config=getattr(group_config, "llm", None),
                        generation_policy=generation_policy,
                    )
                )
            else:
                configured_output_tokens = int(
                    getattr(
                        getattr(group_config, "llm", None),
                        "max_tokens",
                        0,
                    )
                    or 0
                )
            candidate_output_tokens, aggregator_output_tokens = (
                dynamic_output_token_budgets(
                    configured_output_tokens=configured_output_tokens,
                    candidate_max_chars=int(
                        ensemble_cfg.candidate_max_chars or 0
                    ),
                    ranking_config=ranking_config,
                )
            )
            request_context = build_request_context(
                message=turn.semantic_message,
                turn_metadata=turn.metadata,
                attachments=turn.attachments,
                candidate_output_tokens=candidate_output_tokens,
                aggregator_output_tokens=aggregator_output_tokens,
                ranking_config=ranking_config,
            )
            user_profile = (
                mock_user_profile(ranking_config)
                if user_profile_enabled
                else None
            )
            analyzer_provider = build_task_analyzer_provider(
                routed_config,
                provider_id=TASK_ANALYZER_PROVIDER_ID,
                model_id=TASK_ANALYZER_MODEL_ID,
            )
            try:
                task_analysis = await analyze_task_with_provider(
                    provider=analyzer_provider,
                    message=turn.semantic_message,
                    user_profile_enabled=user_profile_enabled,
                    request_context=request_context,
                    routed_tier=routed_tier,
                    routing_confidence=routing_confidence,
                    analyzer_provider_id=TASK_ANALYZER_PROVIDER_ID,
                    analyzer_model_id=TASK_ANALYZER_MODEL_ID,
                    ranking_config=ranking_config,
                    decision_id=decision_id,
                )
            except TaskAnalyzerStreamCleanupError as exc:
                setup_usage.extend(
                    task_analyzer_usage_rows(
                        exc.usage or {"attempt_count": 1},
                        provider_id=TASK_ANALYZER_PROVIDER_ID,
                        model_id=TASK_ANALYZER_MODEL_ID,
                        source="analyzer_stream_cleanup_failed",
                        fallback_reason=type(exc).__name__,
                    )
                )
                routing_trace["task_analyzer"] = {
                    "provider": TASK_ANALYZER_PROVIDER_ID,
                    "model": TASK_ANALYZER_MODEL_ID,
                    "source": "analyzer_stream_cleanup_failed",
                    "schema_valid": False,
                    "fallback_reason": type(exc).__name__,
                    "request_context_hash": request_context.get(
                        "snapshot_hash"
                    ),
                    "user_profile_enabled": user_profile_enabled,
                }
                raise ProviderBuildError(
                    exc,
                    setup_latency_ms=int(
                        (time.monotonic() - started) * 1000
                    ),
                    setup_usage=setup_usage,
                    routing_trace=safe_provider_build_routing_trace(
                        routing_trace
                    ),
                ) from exc
            analyzer_postprocess_error: Exception | None = None
            analyzer_usage_materialization_failed = False
            raw_analyzer_usage: Any = None
            try:
                raw_analyzer_usage = task_analysis.usage
                analyzer_usage = dict(raw_analyzer_usage or {})
            except Exception as exc:  # noqa: BLE001 - preserve raw retry evidence
                analyzer_postprocess_error = exc
                analyzer_usage_materialization_failed = True
                analyzer_usage = {
                    "attempt_count": 1,
                    "usage_unknown": True,
                }
            try:
                analyzer_source = str(task_analysis.source or "")
                analyzer_fallback_reason = str(
                    task_analysis.fallback_reason or ""
                )
            except Exception as exc:  # noqa: BLE001 - usage was already captured
                analyzer_postprocess_error = (
                    analyzer_postprocess_error or exc
                )
                analyzer_source = "analyzer_postprocess_failed"
                analyzer_fallback_reason = type(exc).__name__
            try:
                if analyzer_usage_materialization_failed:
                    analyzer_usage_rows = (
                        conservative_task_analyzer_usage_rows(
                            (
                                raw_analyzer_usage
                                if isinstance(
                                    raw_analyzer_usage,
                                    Mapping,
                                )
                                else analyzer_usage
                            ),
                            provider_id=TASK_ANALYZER_PROVIDER_ID,
                            model_id=TASK_ANALYZER_MODEL_ID,
                            source="analyzer_postprocess_failed",
                            fallback_reason=type(
                                analyzer_postprocess_error
                            ).__name__,
                        )
                    )
                else:
                    analyzer_usage_rows = task_analyzer_usage_rows(
                        analyzer_usage,
                        provider_id=TASK_ANALYZER_PROVIDER_ID,
                        model_id=TASK_ANALYZER_MODEL_ID,
                        source=analyzer_source,
                        fallback_reason=analyzer_fallback_reason,
                    )
            except Exception as exc:  # noqa: BLE001 - retain one unknown receipt
                analyzer_postprocess_error = (
                    analyzer_postprocess_error or exc
                )
                analyzer_usage_rows = conservative_task_analyzer_usage_rows(
                    (
                        raw_analyzer_usage
                        if isinstance(raw_analyzer_usage, Mapping)
                        else analyzer_usage
                    ),
                    provider_id=TASK_ANALYZER_PROVIDER_ID,
                    model_id=TASK_ANALYZER_MODEL_ID,
                    source="analyzer_postprocess_failed",
                    fallback_reason=type(exc).__name__,
                )
            setup_usage.extend(analyzer_usage_rows)
            if analyzer_postprocess_error is not None:
                routing_trace["task_analyzer"] = {
                    "provider": TASK_ANALYZER_PROVIDER_ID,
                    "model": TASK_ANALYZER_MODEL_ID,
                    "source": "analyzer_postprocess_failed",
                    "schema_valid": False,
                    "fallback_reason": type(
                        analyzer_postprocess_error
                    ).__name__,
                    "request_context_hash": request_context.get(
                        "snapshot_hash"
                    ),
                    "user_profile_enabled": user_profile_enabled,
                }
                raise ProviderBuildError(
                    analyzer_postprocess_error,
                    setup_latency_ms=int(
                        (time.monotonic() - started) * 1000
                    ),
                    setup_usage=setup_usage,
                    routing_trace=safe_provider_build_routing_trace(
                        routing_trace
                    ),
                ) from analyzer_postprocess_error
            try:
                ranking_inputs = {
                    "decision_id": decision_id,
                    "task_analysis": task_analysis,
                    "user_profile": user_profile,
                    "request_context": request_context,
                    "ranking_config": ranking_config,
                    "generation_policy": (
                        dict(generation_policy)
                        if generation_policy is not None
                        else None
                    ),
                    "registry_allowlist": resolved_g1_registry_contract,
                }
                if thinking_assignment_enabled:
                    ranking_inputs["request_tools_present"] = bool(tools)
                routing_trace["task_analyzer"] = {
                    "provider": TASK_ANALYZER_PROVIDER_ID,
                    "model": TASK_ANALYZER_MODEL_ID,
                    "source": analyzer_source,
                    "schema_valid": task_analysis.schema_valid,
                    "confidence": task_analysis.confidence,
                    "fallback_reason": analyzer_fallback_reason,
                    "request_context_hash": request_context.get(
                        "snapshot_hash"
                    ),
                    "user_profile_enabled": user_profile_enabled,
                }
            except Exception as exc:  # noqa: BLE001 - preserve a paid analyzer call
                if not setup_usage:
                    raise
                routing_trace["task_analyzer"] = {
                    "provider": TASK_ANALYZER_PROVIDER_ID,
                    "model": TASK_ANALYZER_MODEL_ID,
                    "source": "analyzer_postprocess_failed",
                    "schema_valid": False,
                    "fallback_reason": type(exc).__name__,
                    "request_context_hash": request_context.get(
                        "snapshot_hash"
                    ),
                    "user_profile_enabled": user_profile_enabled,
                }
                raise ProviderBuildError(
                    exc,
                    setup_latency_ms=int(
                        (time.monotonic() - started) * 1000
                    ),
                    setup_usage=setup_usage,
                    routing_trace=safe_provider_build_routing_trace(
                        routing_trace
                    ),
                ) from exc
        try:
            turn.metadata["router_dynamic_task_profile"] = task_analysis.profile
            turn.metadata["router_dynamic_task_analyzer"] = task_analysis.trace(
                ranking_config
            )
            turn.metadata["router_dynamic_request_context_hash"] = (
                request_context.get("snapshot_hash")
            )
        except Exception as exc:  # noqa: BLE001 - preserve a paid analyzer call
            if not setup_usage:
                raise
            routing_trace["task_analyzer"] = {
                "provider": TASK_ANALYZER_PROVIDER_ID,
                "model": TASK_ANALYZER_MODEL_ID,
                "source": "analyzer_postprocess_failed",
                "schema_valid": False,
                "fallback_reason": type(exc).__name__,
                "request_context_hash": request_context.get("snapshot_hash"),
                "user_profile_enabled": user_profile_enabled,
            }
            raise ProviderBuildError(
                exc,
                setup_latency_ms=int((time.monotonic() - started) * 1000),
                setup_usage=setup_usage,
                routing_trace=safe_provider_build_routing_trace(
                    routing_trace
                ),
            ) from exc

    def materialize_ensemble_provider(
        active_ranking_inputs: Mapping[str, Any] | None,
    ) -> Any:
        materialized = build_ensemble_provider_from_config(
            config=group_config,
            inherited_provider_config=routed_config,
            fallback_provider=routed_provider,
            turn_metadata=turn.metadata,
            ranking_inputs=active_ranking_inputs,
        )
        if b2_experiment is not None:
            materialized = align_b2_provider_to_g12(materialized, b2_experiment)
        if g1_routing is not None:
            materialized.selection_plan["user_profile_enabled"] = user_profile_enabled
        materialized = enforce_draco_legal_proposer_quorum(materialized)
        if generation_policy is not None:
            materialized = apply_generation_policy_to_ensemble_provider(
                materialized,
                generation_policy,
            )
            validate_strict_openrouter_ensemble_members(
                materialized,
                generation_policy,
                fallback_config=routed_config,
                allow_unpinned_openrouter=bool(
                    isinstance(resolved_g1_registry_contract, Mapping)
                    and resolved_g1_registry_contract.get("candidate_scope") == "registry_all"
                ),
            )
        materialized.seal_managed_thinking_execution_guard()
        return materialized

    try:
        provider = materialize_ensemble_provider(ranking_inputs)
        if frozen_g1_lifecycle is not None:
            initial_plan = frozen_g1_lifecycle["initial_plan"]
            target_plan = frozen_g1_lifecycle["target_plan"]
            target_execution_prefix = frozen_g1_lifecycle.get(
                "target_execution_prefix"
            )
            thinking_execution_history = frozen_g1_lifecycle.get(
                "thinking_execution_history"
            )
            stored_projection_audit = frozen_g1_lifecycle.get(
                "thinking_execution_projection"
            )
            if (
                not isinstance(target_execution_prefix, Mapping)
                or not isinstance(thinking_execution_history, list)
                or any(
                    not isinstance(plan, Mapping)
                    for plan in thinking_execution_history
                )
                or not isinstance(stored_projection_audit, Mapping)
            ):
                raise ValueError(
                    "frozen G1 thinking execution closure is incomplete"
                )
            from opensquilla.provider.thinking_execution import (
                immutable_selection_plan_payload,
                validate_thinking_execution_history_closure,
            )

            (
                recomputed_execution_prefix,
                recomputed_projection_audit,
                execution_closure_reason,
            ) = validate_thinking_execution_history_closure(
                thinking_execution_history,
                target_execution_prefix,
            )
            if (
                execution_closure_reason
                or recomputed_execution_prefix
                != dict(target_execution_prefix)
                or recomputed_projection_audit
                != dict(stored_projection_audit)
                or immutable_selection_plan_payload(target_plan)
                != immutable_selection_plan_payload(
                    target_execution_prefix
                )
            ):
                raise ValueError(
                    "frozen G1 thinking execution closure failed validation"
                )
            exclusions = list(
                frozen_g1_lifecycle[
                    "cumulative_excluded_proposer_identities"
                ]
            )
            if exclusions:
                _bind_frozen_g1_retry_provenance(
                    provider.selection_plan,
                    initial_plan=initial_plan,
                    excluded_proposer_identities=exclusions,
                )
                provider.seal_managed_thinking_execution_guard()
            if (
                not _g1_full_plans_match(
                    provider.selection_plan,
                    target_plan,
                )
                or g1_registry_contract_reasons(
                    {"selection_plan": provider.selection_plan},
                    resolved_g1_registry_contract,
                )
            ):
                raise ValueError(
                    "frozen G1 provider materialization drifted from its target plan"
                )
            restore_g1_thinking_execution_prefix(
                provider,
                target_plan=target_plan,
                physical_execution_prefix=target_execution_prefix,
            )
            restored_snapshot = provider.selection_plan_execution_snapshot()
            if (
                not isinstance(restored_snapshot, Mapping)
                or dict(restored_snapshot)
                != dict(target_execution_prefix)
            ):
                raise ValueError(
                    "frozen G1 thinking execution prefix did not restore exactly"
                )
            setattr(
                provider,
                "_draco_prior_excluded_proposer_identities",
                list(exclusions),
            )
            setattr(
                provider,
                "_draco_g1_initial_selection_plan",
                copy.deepcopy(dict(initial_plan)),
            )
            setattr(
                provider,
                "_draco_g1_thinking_execution_history",
                copy.deepcopy(list(thinking_execution_history)),
            )
            if setup_usage:
                raise ValueError(
                    "frozen G1 provider build duplicated historical setup usage"
                )
        routing_trace.update(
            {
                "selection_mode": selection_mode,
                "profile": provider.profile_name,
                "selection_plan": provider.selection_plan,
                "user_profile_enabled": user_profile_enabled if g1_routing is not None else None,
            }
        )
    except Exception as exc:
        if setup_usage:
            raise ProviderBuildError(
                exc,
                setup_latency_ms=int((time.monotonic() - started) * 1000),
                setup_usage=setup_usage,
                routing_trace=safe_provider_build_routing_trace(
                    routing_trace
                ),
            ) from exc
        raise
    try:
        result = ProviderBuildResult(
            provider=provider,
            prompt=turn.message,
            setup_latency_ms=int((time.monotonic() - started) * 1000),
            setup_usage=setup_usage,
            routing_trace=routing_trace,
        )
        attach_provider_setup(provider, result)
        if g1_routing is not None and isinstance(ranking_inputs, Mapping):
            frozen_ranking_inputs = dict(ranking_inputs)
            initial_selection_plan = copy.deepcopy(
                dict(frozen_g1_lifecycle["initial_plan"])
                if frozen_g1_lifecycle is not None
                else provider.selection_plan
            )
            initial_decision_id = str(
                initial_selection_plan.get("decision_id") or ""
            )
            initial_task_analysis = frozen_ranking_inputs.get(
                "task_analysis"
            )
            initial_task_analysis_trace = dict(
                routing_trace.get("task_analyzer") or {}
            )

            def rebuild_after_reasoning_only_length(
                excluded_proposer_identities: Sequence[str],
            ) -> Any:
                exclusions = sorted(
                    {
                        str(identity or "").strip().lower()
                        for identity in excluded_proposer_identities
                        if str(identity or "").strip()
                    }
                )
                if not exclusions:
                    raise ValueError(
                        "G1 retry rebuild requires at least one proposer exclusion"
                    )
                retry_started = time.monotonic()
                exclusion_hash = hashlib.sha256(
                    "\n".join(exclusions).encode("utf-8")
                ).hexdigest()[:16]
                retry_inputs = dict(frozen_ranking_inputs)
                retry_inputs.update(
                    {
                        "decision_id": (
                            f"{initial_decision_id}-retry-{exclusion_hash}"
                        ),
                        "task_analysis": initial_task_analysis,
                        "retry_excluded_proposer_identities": exclusions,
                        "retry_parent_decision_id": initial_decision_id,
                        "task_analysis_reused": True,
                    }
                )
                retry_provider = materialize_ensemble_provider(retry_inputs)
                retry_plan = retry_provider.selection_plan
                _bind_frozen_g1_retry_provenance(
                    retry_plan,
                    initial_plan=initial_selection_plan,
                    excluded_proposer_identities=exclusions,
                )
                retry_provider.seal_managed_thinking_execution_guard()
                retry_routing_trace = {
                    **dict(routing_trace),
                    "selection_plan": retry_plan,
                    "task_analyzer": initial_task_analysis_trace,
                    "task_analyzer_reuse": {
                        "schema": (
                            "opensquilla.draco.task-analysis-reuse/v1"
                        ),
                        "source_decision_id": initial_decision_id,
                        "physical_request_count": 0,
                        "excluded_proposer_identities": exclusions,
                    },
                }
                retry_build = ProviderBuildResult(
                    provider=retry_provider,
                    prompt=turn.message,
                    setup_latency_ms=int(
                        (time.monotonic() - retry_started) * 1000
                    ),
                    setup_usage=[],
                    routing_trace=retry_routing_trace,
                )
                attach_provider_setup(retry_provider, retry_build)
                return retry_provider

            setattr(
                provider,
                "_draco_reasoning_only_retry_factory",
                rebuild_after_reasoning_only_length,
            )
        return result
    except ProviderBuildError:
        raise
    except Exception as exc:
        if setup_usage:
            raise ProviderBuildError(
                exc,
                setup_latency_ms=int(
                    (time.monotonic() - started) * 1000
                ),
                setup_usage=setup_usage,
                routing_trace=safe_provider_build_routing_trace(
                    routing_trace
                ),
            ) from exc
        raise


def build_profile_provider(
    *,
    config: GatewayConfig,
    inherited: ProviderConfig,
    group: str,
    profile: str,
    dry_run: bool,
    generation_policy: dict[str, Any] | None = None,
    requested_timeout: float | None = None,
    enable_proposer_tools: bool = False,
    ensemble_proposer_timeout: float | None = None,
    ensemble_aggregator_timeout: float | None = None,
    ensemble_proposer_early_stop_success_count: int | None = None,
    ensemble_proposer_early_stop_after: float | None = None,
    expand_ensemble_timeouts_to_task_timeout: bool = False,
):
    if dry_run:
        return DryEnsembleProvider(group=group, profile=profile)
    if profile not in config.llm_ensemble.profiles:
        raise ValueError(f"profile {profile!r} for group {group} is not configured")
    config.llm_ensemble.enabled = True
    config.llm_ensemble.active_profile = profile
    config.llm_ensemble.proposer_tools = bool(enable_proposer_tools)
    profile_config = config.llm_ensemble.profiles[profile]
    if generation_policy is not None:
        profile_config = apply_generation_policy_to_profile(
            profile_config,
            generation_policy,
        )
    proposer_timeout_s, aggregator_timeout_s = profile_timeout_seconds(
        profile_config,
        requested_timeout=requested_timeout,
        proposer_timeout_override=ensemble_proposer_timeout,
        aggregator_timeout_override=ensemble_aggregator_timeout,
        expand_to_requested_timeout=expand_ensemble_timeouts_to_task_timeout,
    )
    profile_updates: dict[str, Any] = {
        "record_candidates": True,
        "shuffle_candidates": False,
        "proposer_timeout_seconds": proposer_timeout_s,
        "aggregator_timeout_seconds": aggregator_timeout_s,
    }
    if ensemble_proposer_early_stop_success_count is not None:
        profile_updates["proposer_early_stop_success_count"] = max(
            0,
            int(ensemble_proposer_early_stop_success_count or 0),
        )
    if ensemble_proposer_early_stop_after is not None:
        profile_updates["proposer_early_stop_after_seconds"] = max(
            0.0,
            float(ensemble_proposer_early_stop_after or 0.0),
        )
    config.llm_ensemble.profiles[profile] = profile_config.model_copy(update=profile_updates)
    fallback = build_single_provider(
        inherited=inherited,
        group=f"{group}-fallback",
        model=inherited.model,
        dry_run=False,
    )
    return build_ensemble_provider_from_config(
        config=config,
        inherited_provider_config=inherited,
        fallback_provider=fallback,
    )


def _consume_background_task(task: asyncio.Future[Any]) -> None:
    with contextlib.suppress(BaseException):
        task.result()


_BENCHMARK_OWNER_CLEANUP_STATE: dict[int, dict[str, Any]] = {}


def _benchmark_owner_state(owner: Any) -> dict[str, Any]:
    key = id(owner)
    state = _BENCHMARK_OWNER_CLEANUP_STATE.get(key)
    if state is None or state.get("owner") is not owner:
        state = {"owner": owner, "pending": set(), "poisoned_reason": ""}
        _BENCHMARK_OWNER_CLEANUP_STATE[key] = state
    return state


def _benchmark_owner_cleanup_reason(owner: Any) -> str:
    state = _benchmark_owner_state(owner)
    poisoned = str(state.get("poisoned_reason") or "")
    if poisoned:
        return poisoned
    pending = state.get("pending")
    if pending:
        return "owned_cleanup_in_progress"
    _BENCHMARK_OWNER_CLEANUP_STATE.pop(id(owner), None)
    return ""


def _poison_benchmark_owner(owner: Any, reason: str) -> None:
    _benchmark_owner_state(owner)["poisoned_reason"] = reason


def _track_benchmark_owner_cleanup(
    owner: Any,
    task: asyncio.Future[Any],
    *,
    reason: str,
) -> None:
    pending = _benchmark_owner_state(owner)["pending"]
    pending.add(task)

    def _finished(done: asyncio.Future[Any]) -> None:
        pending.discard(done)
        _observe_benchmark_cleanup_result(owner, done, reason=reason)
        state = _BENCHMARK_OWNER_CLEANUP_STATE.get(id(owner))
        if state is not None and not state.get("pending") and not state.get("poisoned_reason"):
            _BENCHMARK_OWNER_CLEANUP_STATE.pop(id(owner), None)

    task.add_done_callback(_finished)


class _RunnerStreamCloseError(RuntimeError):
    pass


def _observe_benchmark_cleanup_result(
    owner: Any,
    task: asyncio.Future[Any],
    *,
    reason: str,
) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        # Expected after timeout: cancellation is safe only because the
        # consumer's owned close path has now completed.
        return
    except _RunnerStreamCloseError:
        _poison_benchmark_owner(owner, reason)
    except BaseException:
        # The owned stream wrapper already ran its close path before exposing
        # any unrelated consumer exception.
        return


async def _close_runner_stream(stream_iter: Any, stream: Any) -> None:
    lookup_errors: list[BaseException] = []
    candidates = [stream_iter]
    if stream is not stream_iter:
        candidates.append(stream)
    for candidate in candidates:
        try:
            aclose = getattr(candidate, "aclose", None)
        except BaseException as exc:
            lookup_errors.append(exc)
            continue
        if not callable(aclose):
            continue
        try:
            await aclose()
        except BaseException as exc:
            raise _RunnerStreamCloseError(
                "provider_stream_close_failed: iterator aclose failed"
            ) from exc
        return
    cause = lookup_errors[-1] if lookup_errors else None
    error = _RunnerStreamCloseError(
        "provider_stream_close_unavailable: iterator has no usable aclose"
    )
    if cause is not None:
        raise error from cause
    raise error


async def _aclosing_events(stream: Any) -> Any:
    try:
        stream_iter = stream.__aiter__()
    except BaseException as exc:
        try:
            await _close_runner_stream(stream, stream)
        except _RunnerStreamCloseError:
            raise
        raise exc
    exhausted = False
    try:
        while True:
            try:
                yield await stream_iter.__anext__()
            except StopAsyncIteration:
                exhausted = True
                return
    finally:
        if exhausted:
            return
        await _close_runner_stream(stream_iter, stream)


async def _await_benchmark_consumer(
    consumer: Any,
    *,
    timeout: float,
    owner: Any,
) -> str:
    """Own one stream consumer and bound cancellation cleanup.

    Returns ``"timeout"`` when cancellation closed the stream, or
    ``"cleanup_timeout"`` when the consumer remained alive after the bounded
    close window.  The latter must suppress generation retries.
    """

    task = asyncio.create_task(consumer)
    try:
        if not timeout or timeout <= 0:
            await task
            return ""
        done, _ = await asyncio.wait({task}, timeout=timeout)
    except BaseException:
        if not task.done():
            task.cancel()
            try:
                cleaned, _ = await asyncio.wait(
                    {task},
                    timeout=max(0.0, RUNNER_STREAM_CLEANUP_TIMEOUT_SECONDS),
                )
            except BaseException:
                _track_benchmark_owner_cleanup(
                    owner,
                    task,
                    reason="external_cancel_cleanup_failed",
                )
                task.add_done_callback(_consume_background_task)
                raise
            if not cleaned:
                _track_benchmark_owner_cleanup(
                    owner,
                    task,
                    reason="external_cancel_cleanup_timeout",
                )
                task.add_done_callback(_consume_background_task)
            else:
                _observe_benchmark_cleanup_result(
                    owner,
                    task,
                    reason="external_cancel_cleanup_failed",
                )
        else:
            _observe_benchmark_cleanup_result(
                owner,
                task,
                reason="external_cancel_cleanup_failed",
            )
        raise
    if done:
        try:
            await task
        except _RunnerStreamCloseError:
            _poison_benchmark_owner(owner, "stream_close_failed")
            raise
        return ""
    task.cancel()
    cleaned, _ = await asyncio.wait(
        {task},
        timeout=max(0.0, RUNNER_STREAM_CLEANUP_TIMEOUT_SECONDS),
    )
    if not cleaned:
        _track_benchmark_owner_cleanup(
            owner,
            task,
            reason="stream_close_timeout",
        )
        task.add_done_callback(_consume_background_task)
        return "cleanup_timeout"
    try:
        await task
    except asyncio.CancelledError:
        pass
    except _RunnerStreamCloseError:
        _poison_benchmark_owner(owner, "stream_close_failed")
        raise
    return "timeout"


async def collect_run(
    provider: Any,
    prompt: str,
    *,
    timeout: float,
    config: ChatConfig | None = None,
    tools: list[ToolDefinition] | None = None,
) -> RunResult:
    messages = [Message(role="user", content=prompt)]
    text_parts: list[str] = []
    done: DoneEvent | None = None
    error = ""
    ttft_ms: int | None = None
    tool_call_count = 0
    trace_events: list[dict[str, Any]] = []
    started = time.monotonic()

    def _trace(kind: str, **payload: Any) -> None:
        trace_events.append(
            {
                "seq": len(trace_events) + 1,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "kind": kind,
                **payload,
            }
        )

    owner_cleanup_reason = _benchmark_owner_cleanup_reason(provider)
    if owner_cleanup_reason:
        error = (
            "benchmark_owner_cleanup_in_progress: a prior stream owned by this "
            "provider has not proved closure"
        )
        _trace(
            "cleanup_in_progress",
            code="benchmark_owner_cleanup_in_progress",
            reason=owner_cleanup_reason,
        )
        return RunResult(
            final_text="",
            done=None,
            error=error,
            latency_ms=int((time.monotonic() - started) * 1000),
            trace_events=trace_events,
        )

    try:
        chat_config = (
            config.model_copy(update={"timeout": timeout})
            if config is not None
            else ChatConfig(timeout=timeout)
        )

        async def _consume() -> None:
            nonlocal done, error, ttft_ms, tool_call_count
            stream = provider.chat(
                messages,
                tools=tools,
                config=chat_config,
            )
            async with contextlib.aclosing(_aclosing_events(stream)) as owned_stream:
                async for event in owned_stream:
                    if isinstance(event, TextDeltaEvent):
                        if ttft_ms is None and event.text:
                            ttft_ms = int((time.monotonic() - started) * 1000)
                            _trace("first_text_delta", text_chars=len(event.text))
                        else:
                            _trace("text_delta", text_chars=len(event.text))
                        text_parts.append(event.text)
                    elif isinstance(event, ReasoningDeltaEvent):
                        _trace("reasoning_delta", text_chars=len(event.text))
                    elif isinstance(event, ToolUseStartEvent):
                        tool_call_count += 1
                        _trace(
                            "tool_use_start",
                            tool_use_id=event.tool_use_id,
                            tool_name=event.tool_name,
                            synthetic_from_text=event.synthetic_from_text,
                        )
                    elif isinstance(event, ToolUseDeltaEvent):
                        _trace(
                            "tool_use_delta",
                            tool_use_id=event.tool_use_id,
                            json_fragment_chars=len(event.json_fragment),
                        )
                    elif isinstance(event, ToolUseEndEvent):
                        _trace(
                            "tool_use_end",
                            tool_use_id=event.tool_use_id,
                            tool_name=event.tool_name,
                            argument_keys=sorted(event.arguments.keys()),
                            synthetic_from_text=event.synthetic_from_text,
                        )
                    elif isinstance(event, ProviderHeartbeatEvent):
                        _trace(
                            "provider_heartbeat",
                            phase=event.phase,
                            message=event.message,
                        )
                    elif isinstance(event, DoneEvent):
                        done = event
                        _trace(
                            "done",
                            stop_reason=event.stop_reason,
                            usage=done_payload(event),
                            has_ensemble_trace=bool(event.ensemble_trace),
                        )
                    elif isinstance(event, ErrorEvent):
                        diagnostic_done = diagnostic_done_from_error_event(event)
                        if done is None and diagnostic_done is not None:
                            done = diagnostic_done
                            _trace(
                                "diagnostic_done",
                                stop_reason=done.stop_reason,
                                usage=done_payload(done),
                                has_ensemble_trace=bool(done.ensemble_trace),
                            )
                        error = event.message
                        _trace(
                            "error",
                            message=event.message,
                            code=event.code,
                            request_started=event.request_started,
                            physical_request_count=event.physical_request_count,
                        )
                        break
                    else:
                        _trace("stream_event", event_type=type(event).__name__)

        consume_outcome = await _await_benchmark_consumer(
            _consume(),
            timeout=timeout,
            owner=provider,
        )
        if consume_outcome == "cleanup_timeout":
            error = (
                "provider_stream_close_timeout: provider stream did not close "
                "within the bounded cleanup window"
            )
            _trace(
                "cleanup_timeout",
                code="provider_stream_close_timeout",
                timeout_s=RUNNER_STREAM_CLEANUP_TIMEOUT_SECONDS,
            )
        elif consume_outcome == "timeout":
            error = f"TimeoutError: run timed out after {timeout:g}s"
            _trace("timeout", timeout_s=timeout)
    except Exception as exc:  # noqa: BLE001 - benchmark rows should keep going
        error = type(exc).__name__
        _trace("exception", error=error)
    setup = consume_provider_setup(provider)
    setup_latency_ms = coerce_metric_int(setup.get("latency_ms"))
    setup_usage = setup.get("usage") if isinstance(setup.get("usage"), list) else []
    routing_trace = setup.get("routing") if isinstance(setup.get("routing"), dict) else {}
    if setup:
        trace_events.insert(
            0,
            {
                "seq": 0,
                "elapsed_ms": setup_latency_ms,
                "kind": "routing_setup",
                "routing": routing_trace,
                "usage": setup_usage,
            },
        )
    return RunResult(
        final_text="".join(text_parts),
        done=done,
        error=error,
        latency_ms=int((time.monotonic() - started) * 1000) + setup_latency_ms,
        ttft_ms=(ttft_ms + setup_latency_ms if ttft_ms is not None else None),
        tool_call_count=tool_call_count,
        trace_events=copy.deepcopy(trace_events),
        setup_latency_ms=setup_latency_ms,
        setup_usage=setup_usage,
        routing_trace=routing_trace,
    )


class BenchmarkTurnCallRecorder:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def write(self, kind: str, payload: dict[str, Any]) -> None:
        self.records.append(
            {
                "seq": len(self.records) + 1,
                "kind": kind,
                "payload": json_safe(payload),
            }
        )


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [json_safe(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return json_safe(asdict(value))
    if isinstance(value, Path):
        return str(value)
    try:
        json.dumps(value)
        return value
    except TypeError:
        return repr(value)


def agent_thinking_from_chat_config(config: ChatConfig | None) -> bool | ThinkingLevel:
    if config is None or not config.thinking:
        return False
    if config.thinking_level is not None:
        try:
            return ThinkingLevel(str(config.thinking_level))
        except ValueError:
            return True
    return True


def agent_config_from_chat_config(
    config: ChatConfig | None,
    *,
    timeout: float,
    model_id: str,
    max_iterations: int,
    finalization_policy: Mapping[str, Any] | None = None,
) -> AgentConfig:
    policy = normalized_agent_finalization_policy(finalization_policy)
    return AgentConfig(
        max_iterations=max(0, int(max_iterations)),
        timeout=timeout,
        iteration_timeout=timeout,
        request_timeout=(
            float(config.timeout)
            if config is not None and getattr(config, "timeout", 0)
            else timeout
        ),
        max_tokens=(
            int(config.max_tokens)
            if config is not None and getattr(config, "max_tokens", 0)
            else AgentConfig().max_tokens
        ),
        temperature=config.temperature if config is not None else None,
        thinking=agent_thinking_from_chat_config(config),
        thinking_budget_tokens=(
            int(config.thinking_budget_tokens)
            if config is not None and getattr(config, "thinking_budget_tokens", 0)
            else AgentConfig().thinking_budget_tokens
        ),
        stop_sequences=list(config.stop_sequences) if config is not None else [],
        model_capabilities=config.model_capabilities if config is not None else None,
        model_id=model_id,
        workspace_dir=str(ROOT),
        tool_result_external_keep_recent=3,
        deadline_wrapup_margin_seconds=int(policy["deadline_wrapup_margin_seconds"]),
        deadline_wrapup_disable_tools=bool(policy["deadline_wrapup_disable_tools"]),
        deadline_thinking_off_margin_seconds=int(policy["deadline_thinking_off_margin_seconds"]),
        max_iterations_includes_finalization=bool(policy["max_iterations_includes_finalization"]),
        retrieval_loop_finalization_threshold=int(policy["retrieval_loop_finalization_threshold"]),
        finalization_aggregator_only=bool(policy["finalization_aggregator_only"]),
        finalization_disable_thinking=bool(policy["finalization_disable_thinking"]),
        metadata={
            "benchmark": "DRACO",
            "runner_mode": RUNNER_MODE_AGENT_LOOP,
            "tool_activity_heartbeat_interval": 30.0,
            "agent_finalization_policy": dict(policy),
        },
    )


def llm_response_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        record
        for record in records
        if record.get("kind") in {"llm_response", "llm_error", "llm_abandoned"}
        and isinstance(record.get("payload"), dict)
    ]


def payload_physical_request_count(payload: Mapping[str, Any]) -> int | None:
    value = payload.get("physical_request_count")
    if isinstance(value, int) and not isinstance(value, bool):
        return max(0, value)
    if payload.get("request_started") is False:
        return 0
    return None


def aggregate_agent_model_usage(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for call_index, record in enumerate(llm_response_records(records), start=1):
        payload = record["payload"]
        missing_usage_count = max(
            0,
            coerce_metric_int(payload.get("usage_missing_count")),
        )

        def append_unknown_usage(count: int) -> None:
            for missing_index in range(1, count + 1):
                rows.append(
                    {
                        "role": "agent_llm_request_unknown",
                        "agent_call_index": call_index,
                        "agent_iteration": coerce_metric_int(payload.get("iteration")),
                        "agent_call_attempt": coerce_metric_int(
                            payload.get("attempt") or payload.get("call_attempt")
                        ),
                        "agent_missing_usage_index": missing_index,
                        "model": "",
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "reasoning_tokens": 0,
                        "cached_tokens": 0,
                        "cache_write_tokens": 0,
                        "billed_cost": 0.0,
                        "cost_source": "none",
                        "request_outcome": str(record.get("kind") or "unknown"),
                    }
                )

        usage = payload.get("usage") if isinstance(payload, dict) else None
        if not isinstance(usage, dict):
            explicit_request_count = payload_physical_request_count(payload)
            append_unknown_usage(
                max(
                    (explicit_request_count if explicit_request_count is not None else 1),
                    missing_usage_count,
                )
            )
            continue
        breakdown = usage.get("model_usage_breakdown")
        if isinstance(breakdown, list) and breakdown:
            represented_missing_count = sum(
                1
                for row in breakdown
                if isinstance(row, dict)
                and str(row.get("role") or "").strip().casefold() in MISSING_USAGE_PLACEHOLDER_ROLES
            )
            for row in breakdown:
                if not isinstance(row, dict):
                    continue
                enriched = dict(row)
                enriched["agent_call_index"] = call_index
                enriched["agent_iteration"] = coerce_metric_int(payload.get("iteration"))
                enriched["agent_call_attempt"] = coerce_metric_int(
                    payload.get("attempt") or payload.get("call_attempt")
                )
                rows.append(enriched)
            append_unknown_usage(max(0, missing_usage_count - represented_missing_count))
            continue
        synthesized = {
            "role": "agent_llm_call",
            "agent_call_index": call_index,
            "agent_iteration": coerce_metric_int(payload.get("iteration")),
            "agent_call_attempt": coerce_metric_int(
                payload.get("attempt") or payload.get("call_attempt")
            ),
            "provider": str(usage.get("provider") or ""),
            "model": str(usage.get("model") or ""),
            "requested_provider": str(usage.get("requested_provider") or ""),
            "requested_model": str(usage.get("requested_model") or ""),
            "input_tokens": coerce_metric_int(usage.get("input_tokens")),
            "output_tokens": coerce_metric_int(usage.get("output_tokens")),
            "reasoning_tokens": coerce_metric_int(usage.get("reasoning_tokens")),
            "cached_tokens": coerce_metric_int(usage.get("cached_tokens")),
            "cache_write_tokens": coerce_metric_int(usage.get("cache_write_tokens")),
            "billed_cost": float(usage.get("billed_cost") or 0.0),
            "cost_source": str(usage.get("cost_source") or "none"),
            "provider_usage": (
                dict(usage.get("provider_usage"))
                if isinstance(usage.get("provider_usage"), Mapping)
                else {}
            ),
        }
        billing_receipt = usage.get("billing_receipt", usage.get("billingReceipt"))
        if billing_receipt is not None:
            synthesized["billing_receipt"] = billing_receipt
        rows.append(synthesized)
        append_unknown_usage(missing_usage_count)
    return rows


def aggregate_agent_ensemble_trace(records: list[dict[str, Any]]) -> dict[str, Any]:
    call_records = llm_response_records(records)
    traces: list[dict[str, Any]] = []
    first_real_trace: dict[str, Any] | None = None
    last_real_trace: dict[str, Any] | None = None
    total_llm_requests = 0
    untraced_llm_requests = 0
    for call_index, record in enumerate(call_records, start=1):
        payload = record["payload"]
        trace = payload.get("ensemble_trace") if isinstance(payload, dict) else None
        if record.get("kind") == "llm_abandoned":
            trace_requests = (
                coerce_metric_int(trace.get("llm_request_count")) if isinstance(trace, dict) else 0
            )
            request_count = max(
                1,
                trace_requests,
                coerce_metric_int(payload.get("usage_missing_count")),
            )
            untraced_llm_requests += request_count
            traces.append(
                {
                    "request_outcome": "llm_abandoned",
                    "agent_call_index": call_index,
                    "agent_call_id": str(payload.get("call_id") or ""),
                    "agent_iteration": coerce_metric_int(payload.get("iteration")),
                    "agent_call_attempt": coerce_metric_int(payload.get("attempt")),
                    "agent_call_duration_ms": coerce_metric_int(payload.get("duration_ms")),
                    "trace_missing": True,
                    "llm_request_count": request_count,
                    "physical_request_count": request_count,
                    "usage_missing_count": max(
                        request_count,
                        coerce_metric_int(payload.get("usage_missing_count")),
                    ),
                }
            )
            continue
        if not isinstance(trace, dict) or not trace:
            usage = payload.get("usage") if isinstance(payload, dict) else None
            breakdown = usage.get("model_usage_breakdown") if isinstance(usage, dict) else None
            represented_missing = (
                sum(
                    1
                    for item in breakdown
                    if isinstance(item, dict)
                    and str(item.get("role") or "").strip().casefold()
                    in MISSING_USAGE_PLACEHOLDER_ROLES
                )
                if isinstance(breakdown, list)
                else 0
            )
            observed = (
                sum(1 for item in breakdown if isinstance(item, dict))
                if isinstance(breakdown, list) and breakdown
                else (1 if isinstance(usage, dict) and usage else 0)
            )
            missing = coerce_metric_int(
                payload.get("usage_missing_count") if isinstance(payload, dict) else 0
            )
            inferred_count = observed + max(0, missing - represented_missing)
            explicit_request_count = payload_physical_request_count(payload)
            if explicit_request_count is not None:
                request_count = max(explicit_request_count, inferred_count)
            else:
                # Older logs have no explicit start evidence. A response/error
                # record conservatively represents at least one request.
                request_count = max(1, inferred_count)
            receipt_count = max(0, observed - represented_missing)
            effective_missing = max(missing, request_count - receipt_count)
            untraced_llm_requests += request_count
            traces.append(
                {
                    "request_outcome": str(record.get("kind") or "unknown"),
                    "agent_call_index": call_index,
                    "agent_call_id": str(payload.get("call_id") or ""),
                    "agent_iteration": coerce_metric_int(payload.get("iteration")),
                    "agent_call_attempt": coerce_metric_int(payload.get("attempt")),
                    "agent_call_duration_ms": coerce_metric_int(payload.get("duration_ms")),
                    "trace_missing": True,
                    "llm_request_count": request_count,
                    "physical_request_count": request_count,
                    "usage_missing_count": effective_missing,
                }
            )
            continue
        enriched = dict(trace)
        if first_real_trace is None:
            first_real_trace = enriched
        last_real_trace = enriched
        enriched["request_outcome"] = str(record.get("kind") or "unknown")
        enriched["agent_call_index"] = call_index
        if payload.get("call_id") is not None:
            enriched["agent_call_id"] = str(payload.get("call_id"))
        enriched["agent_iteration"] = coerce_metric_int(payload.get("iteration"))
        enriched["agent_call_attempt"] = coerce_metric_int(payload.get("attempt"))
        enriched["agent_call_duration_ms"] = coerce_metric_int(payload.get("duration_ms"))
        traces.append(enriched)
        total_llm_requests += max(
            0,
            coerce_metric_int(
                trace.get("physical_request_count")
                if trace.get("physical_request_count") is not None
                else trace.get("llm_request_count")
            ),
        )
    if not call_records:
        return {}
    first_trace = first_real_trace or {}
    terminal_trace = last_real_trace or {}
    payload: dict[str, Any] = {
        "mode": "agent_loop",
        "agent_llm_call_count": len(call_records),
        "untraced_agent_llm_call_count": sum(
            1
            for record in call_records
            if record.get("kind") == "llm_abandoned"
            or not isinstance(record.get("payload", {}).get("ensemble_trace"), dict)
            or not record.get("payload", {}).get("ensemble_trace")
        ),
        "llm_request_count": total_llm_requests + untraced_llm_requests,
        "physical_request_count": total_llm_requests + untraced_llm_requests,
        "usage_missing_count": sum(
            max(0, coerce_metric_int(call.get("usage_missing_count"))) for call in traces
        ),
        "calls": traces,
    }
    for key in (
        "profile",
        "shuffle_candidates",
        "moa_layers",
        "moa_refine_count",
        "moa_intermediate_layers",
        "output_strategy",
    ):
        if key in first_trace:
            payload[key] = first_trace[key]
    for key in (
        "successful_proposers",
        "total_candidates",
        "fallback_used",
        "final_request_role",
    ):
        if key in terminal_trace:
            payload[key] = terminal_trace[key]
    intermediate_fallback_indexes = [
        coerce_metric_int(call.get("agent_call_index"))
        for call in traces[:-1]
        if call.get("fallback_used") is True
    ]
    payload["terminal_call_index"] = coerce_metric_int(terminal_trace.get("agent_call_index"))
    payload["any_intermediate_fallback"] = bool(intermediate_fallback_indexes)
    payload["intermediate_fallback_call_indexes"] = intermediate_fallback_indexes
    if terminal_trace.get("output_strategy") == "select_best_candidate":
        for key in ("selected_candidate_count", "selected_candidate_indexes"):
            if key in terminal_trace:
                payload[key] = terminal_trace[key]
    return payload


def provider_done_from_agent_done(
    done: AgentDoneEvent | None,
    *,
    recorder: BenchmarkTurnCallRecorder,
    fallback_model: str,
) -> DoneEvent | None:
    call_records = llm_response_records(recorder.records)
    breakdown = aggregate_agent_model_usage(recorder.records)
    trace = aggregate_agent_ensemble_trace(recorder.records)
    ignored_agent_done_summary_rows = 0
    ignored_agent_done_policy_evidence: list[dict[str, Any]] = []
    if done is not None:
        done_rows = [
            dict(row)
            for row in getattr(done, "model_usage_breakdown", [])
            if isinstance(row, Mapping)
        ]
        if (
            not done_rows
            and not call_records
            and not breakdown
            and (
                getattr(done, "billing_receipt", None) is not None
                or getattr(done, "provider_usage", None)
                or done.input_tokens
                or done.output_tokens
                or done.reasoning_tokens
                or done.cached_tokens
                or done.cache_write_tokens
                or done.billed_cost
            )
        ):
            done_rows = [
                {
                    "role": "agent_done",
                    "provider": str(getattr(done, "provider", "") or ""),
                    "model": str(getattr(done, "model", "") or ""),
                    "requested_provider": str(getattr(done, "requested_provider", "") or ""),
                    "requested_model": str(getattr(done, "requested_model", "") or fallback_model),
                    "input_tokens": done.input_tokens,
                    "output_tokens": done.output_tokens,
                    "reasoning_tokens": done.reasoning_tokens,
                    "cached_tokens": done.cached_tokens,
                    "cache_write_tokens": done.cache_write_tokens,
                    "billed_cost": done.billed_cost,
                    "cost_source": done.cost_source,
                    "provider_usage": dict(
                        getattr(done, "provider_usage", {})
                        if isinstance(getattr(done, "provider_usage", {}), Mapping)
                        else {}
                    ),
                    **(
                        {"billing_receipt": getattr(done, "billing_receipt")}
                        if getattr(done, "billing_receipt", None) is not None
                        else {}
                    ),
                }
            ]
        for done_row in done_rows:
            if not call_records:
                breakdown.append(done_row)
                continue
            request_count = max(
                0,
                coerce_metric_int(done_row.get("request_count")),
            )
            response_ids = usage_row_response_ids(done_row)
            if request_count > 1 or len(response_ids) > 1:
                ignored_agent_done_summary_rows += 1
                policy_evidence = ignored_agent_done_summary_policy_evidence(
                    done_row,
                    physical_rows=breakdown,
                )
                if policy_evidence is not None:
                    ignored_agent_done_policy_evidence.append(policy_evidence)
                continue
            if response_ids:
                candidates = [
                    (0, index)
                    for index, row in enumerate(breakdown)
                    if response_ids & usage_row_response_ids(row)
                ]
            else:
                candidates = [
                    (priority, index)
                    for index, row in enumerate(breakdown)
                    if not usage_row_response_ids(row)
                    if (priority := usage_row_match_priority(row, done_row)) is not None
                ]
            if candidates:
                merge_usage_receipt_provenance(
                    breakdown[min(candidates)[1]],
                    done_row,
                )
            elif len(response_ids) == 1:
                breakdown.append(done_row)
            else:
                ignored_agent_done_summary_rows += 1
        done_trace = dict(done.ensemble_trace) if isinstance(done.ensemble_trace, Mapping) else {}
        if done_trace:
            if not trace:
                trace = done_trace
            else:
                for key, value in done_trace.items():
                    trace.setdefault(key, value)
                for key in (
                    "llm_request_count",
                    "physical_request_count",
                    "usage_missing_count",
                ):
                    trace[key] = max(
                        coerce_metric_int(trace.get(key)),
                        coerce_metric_int(done_trace.get(key)),
                    )
    observed_rows = [
        row
        for row in breakdown
        if str(row.get("role") or "").strip().casefold() not in MISSING_USAGE_PLACEHOLDER_ROLES
    ]
    observed_providers = {
        str(row.get("provider") or "").strip()
        for row in observed_rows
        if str(row.get("provider") or "").strip()
    }
    observed_models = {
        str(row.get("model") or "").strip()
        for row in observed_rows
        if str(row.get("model") or "").strip()
    }
    observed_requested_providers = {
        str(row.get("requested_provider") or "").strip()
        for row in observed_rows
        if str(row.get("requested_provider") or "").strip()
    }
    usage_missing_count = max(
        sum(
            1
            for row in breakdown
            if str(row.get("role") or "").strip().casefold() in MISSING_USAGE_PLACEHOLDER_ROLES
        ),
        coerce_metric_int(trace.get("usage_missing_count")) if trace else 0,
        coerce_metric_int(getattr(done, "usage_missing_count", 0) if done is not None else 0),
    )
    if done is not None and (breakdown or usage_missing_count):
        represented_missing = sum(1 for row in breakdown if usage_row_is_missing_placeholder(row))
        physical_request_count = len(breakdown) + max(
            0,
            usage_missing_count - represented_missing,
        )
        trace = dict(trace or {})
        trace["llm_request_count"] = max(
            coerce_metric_int(trace.get("llm_request_count")),
            physical_request_count,
        )
        trace["physical_request_count"] = max(
            coerce_metric_int(trace.get("physical_request_count")),
            physical_request_count,
        )
        trace["usage_missing_count"] = usage_missing_count
    if done is None:
        if not breakdown:
            return None
        sources = {str(row.get("cost_source") or "none").strip().casefold() for row in breakdown}
        if sources == {"provider_billed"}:
            envelope_source = "provider_billed"
        elif len(sources) == 1:
            envelope_source = next(iter(sources))
        else:
            envelope_source = "mixed"
        return DoneEvent(
            stop_reason="error",
            input_tokens=sum(coerce_metric_int(row.get("input_tokens")) for row in breakdown),
            output_tokens=sum(coerce_metric_int(row.get("output_tokens")) for row in breakdown),
            reasoning_tokens=sum(
                coerce_metric_int(row.get("reasoning_tokens")) for row in breakdown
            ),
            cached_tokens=sum(coerce_metric_int(row.get("cached_tokens")) for row in breakdown),
            cache_write_tokens=sum(
                coerce_metric_int(row.get("cache_write_tokens")) for row in breakdown
            ),
            billed_cost=sum(trusted_provider_billed_cost(row) for row in breakdown),
            model=(next(iter(observed_models)) if len(observed_models) == 1 else ""),
            provider=(next(iter(observed_providers)) if len(observed_providers) == 1 else ""),
            cost_source=envelope_source,
            requested_model=fallback_model,
            requested_provider=(
                next(iter(observed_requested_providers))
                if len(observed_requested_providers) == 1
                else ""
            ),
            model_usage_breakdown=breakdown,
            ensemble_trace=trace,
            usage_missing_count=usage_missing_count,
            provider_usage={
                "diagnostic_usage_only": True,
                "agent_llm_call_count": len(call_records),
                "requested_model": fallback_model,
                "requested_provider": (
                    next(iter(observed_requested_providers))
                    if len(observed_requested_providers) == 1
                    else ""
                ),
            },
        )
    if trace:
        trace["agent_iterations"] = done.iterations
    provider_usage: dict[str, Any] = dict(
        getattr(done, "provider_usage", {})
        if isinstance(getattr(done, "provider_usage", {}), Mapping)
        else {}
    )
    provider_usage.update(
        {
            "agent_iterations": done.iterations,
            "agent_llm_call_count": len(call_records),
            "agent_done_summary_rows_ignored": ignored_agent_done_summary_rows,
            "provider_identity_source": (
                "unique_model_usage_breakdown" if len(observed_providers) == 1 else "unresolved"
            ),
            "requested_model": str(getattr(done, "requested_model", "") or fallback_model),
            "requested_provider": str(getattr(done, "requested_provider", "") or ""),
        }
    )
    if ignored_agent_done_policy_evidence:
        provider_usage[IGNORED_AGENT_DONE_POLICY_EVIDENCE_KEY] = ignored_agent_done_policy_evidence
    done_provider = str(getattr(done, "provider", "") or "").strip()
    done_model = str(getattr(done, "model", "") or "").strip()
    provider = done_provider or (
        next(iter(observed_providers)) if len(observed_providers) == 1 else ""
    )
    model = done_model or (next(iter(observed_models)) if len(observed_models) == 1 else "")
    requested_provider = str(
        getattr(done, "requested_provider", "")
        or (
            next(iter(observed_requested_providers))
            if len(observed_requested_providers) == 1
            else ""
        )
        or ""
    )
    provider_usage["requested_provider"] = requested_provider
    provider_done = DoneEvent(
        stop_reason="stop",
        input_tokens=done.input_tokens,
        output_tokens=done.output_tokens,
        reasoning_content=done.reasoning_content,
        reasoning_tokens=done.reasoning_tokens,
        cached_tokens=done.cached_tokens,
        billed_cost=done.billed_cost,
        model=model,
        provider=provider,
        requested_model=str(getattr(done, "requested_model", "") or fallback_model),
        requested_provider=requested_provider,
        cache_write_tokens=done.cache_write_tokens,
        cost_source=done.cost_source,
        model_usage_breakdown=breakdown,
        ensemble_trace=trace,
        usage_missing_count=usage_missing_count,
        billing_receipt=getattr(done, "billing_receipt", None),
        provider_usage=provider_usage,
    )
    return provider_done


async def collect_agent_run(
    provider: Any,
    prompt: str,
    *,
    timeout: float,
    config: ChatConfig | None,
    tools: list[ToolDefinition] | None,
    tool_policy: dict[str, Any],
    task_id: str,
    group: str,
    output_dir: Path | None = None,
    max_iterations: int = DEFAULT_AGENT_MAX_ITERATIONS,
    finalization_policy: Mapping[str, Any] | None = None,
) -> RunResult:
    text_parts: list[str] = []
    done: AgentDoneEvent | None = None
    error = ""
    ttft_ms: int | None = None
    tool_call_count = 0
    trace_events: list[dict[str, Any]] = []
    started = time.monotonic()
    recorder = BenchmarkTurnCallRecorder()

    def _trace(kind: str, **payload: Any) -> None:
        trace_events.append(
            {
                "seq": len(trace_events) + 1,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "kind": kind,
                **payload,
            }
        )

    owner_cleanup_reason = _benchmark_owner_cleanup_reason(provider)
    if owner_cleanup_reason:
        error = (
            "benchmark_owner_cleanup_in_progress: a prior stream owned by this "
            "provider has not proved closure"
        )
        _trace(
            "cleanup_in_progress",
            code="benchmark_owner_cleanup_in_progress",
            reason=owner_cleanup_reason,
        )
        return RunResult(
            final_text="",
            done=None,
            error=error,
            latency_ms=int((time.monotonic() - started) * 1000),
            trace_events=trace_events,
        )

    tool_registry: ToolRegistry | None = None
    tool_context: ToolContext | None = None
    tool_handler = None
    if tool_policy.get("tool_mode") == TOOL_MODE_LOCAL_WEB_TOOLS:
        tool_registry = build_local_web_tool_registry(tool_policy)
        tool_context = build_benchmark_tool_context(
            task_id=task_id,
            group=group,
            tool_policy=tool_policy,
            output_dir=output_dir,
        )
        tool_handler = build_tool_handler(tool_registry, tool_context)

    model_id = getattr(provider, "model", "") or getattr(provider, "profile_name", "")
    agent = Agent(
        provider=provider,
        config=agent_config_from_chat_config(
            config,
            timeout=timeout,
            model_id=str(model_id or ""),
            max_iterations=max_iterations,
            finalization_policy=finalization_policy,
        ),
        tool_definitions=tools,
        tool_handler=tool_handler,
        tool_registry=tool_registry,
        tool_context=tool_context,
        session_key=f"draco:{group}:{task_id}",
        turn_call_logger=recorder,
    )
    try:

        async def _consume() -> None:
            nonlocal done, error, ttft_ms, tool_call_count
            async for event in _aclosing_events(agent.run_turn(prompt)):
                if isinstance(event, AgentTextDeltaEvent):
                    if event.presentation == "answer":
                        if ttft_ms is None and event.text:
                            ttft_ms = int((time.monotonic() - started) * 1000)
                            _trace(
                                "first_text_delta",
                                text_chars=len(event.text),
                                presentation=event.presentation,
                            )
                        else:
                            _trace(
                                "text_delta",
                                text_chars=len(event.text),
                                presentation=event.presentation,
                            )
                        text_parts.append(event.text)
                    else:
                        _trace(
                            "intermediate_text_delta",
                            text_chars=len(event.text),
                            presentation=event.presentation,
                        )
                elif isinstance(event, AgentThinkingEvent):
                    _trace("thinking_delta", text_chars=len(event.text))
                elif isinstance(event, AgentToolUseStartEvent):
                    tool_call_count += 1
                    _trace(
                        "tool_use_start",
                        tool_use_id=event.tool_use_id,
                        tool_name=event.tool_name,
                        synthetic_from_text=event.synthetic_from_text,
                    )
                elif isinstance(event, AgentToolUseDeltaEvent):
                    _trace(
                        "tool_use_delta",
                        tool_use_id=event.tool_use_id,
                        json_fragment_chars=len(event.json_fragment),
                    )
                elif isinstance(event, AgentToolResultEvent):
                    _trace(
                        "tool_result",
                        tool_use_id=event.tool_use_id,
                        tool_name=event.tool_name,
                        is_error=event.is_error,
                        result_chars=len(event.result or ""),
                        execution_status=compact_provider_status(event.execution_status),
                        diagnostic=compact_tool_result_diagnostic(event.result),
                    )
                elif isinstance(event, AgentRunHeartbeatEvent):
                    _trace(
                        "run_heartbeat",
                        phase=event.phase,
                        message=event.message,
                        idle_ms=event.idle_ms,
                    )
                elif isinstance(event, AgentStateChangeEvent):
                    _trace(
                        "state_change",
                        from_state=str(event.from_state),
                        to_state=str(event.to_state),
                    )
                elif isinstance(event, AgentWarningEvent):
                    _trace("warning", code=event.code, message=event.message)
                elif isinstance(event, AgentDoneEvent):
                    done = event
                    has_snapshot, authoritative_text = done_text_snapshot(event)
                    if has_snapshot:
                        text_parts[:] = [authoritative_text]
                    _trace(
                        "done",
                        usage={
                            "input_tokens": event.input_tokens,
                            "output_tokens": event.output_tokens,
                            "reasoning_tokens": event.reasoning_tokens,
                            "cached_tokens": event.cached_tokens,
                            "cache_write_tokens": event.cache_write_tokens,
                            "billed_cost": event.billed_cost,
                            "cost_usd": event.cost_usd,
                            "cost_source": event.cost_source,
                            "model": event.model,
                            "iterations": event.iterations,
                        },
                    )
                elif isinstance(event, AgentErrorEvent):
                    error = event.message
                    _trace(
                        "error",
                        message=event.message,
                        code=event.code,
                        request_started=event.request_started,
                        physical_request_count=event.physical_request_count,
                    )
                else:
                    _trace("agent_event", event_type=type(event).__name__)

        consume_outcome = await _await_benchmark_consumer(
            _consume(),
            timeout=timeout,
            owner=provider,
        )
        if consume_outcome == "cleanup_timeout":
            error = (
                "agent_stream_close_timeout: agent turn did not close "
                "within the bounded cleanup window"
            )
            _trace(
                "cleanup_timeout",
                code="agent_stream_close_timeout",
                timeout_s=RUNNER_STREAM_CLEANUP_TIMEOUT_SECONDS,
            )
        elif consume_outcome == "timeout":
            error = f"TimeoutError: agent run timed out after {timeout:g}s"
            _trace("timeout", timeout_s=timeout)
    except Exception as exc:  # noqa: BLE001 - benchmark rows should keep going
        error = type(exc).__name__
        _trace("exception", error=error)

    provider_done = provider_done_from_agent_done(
        done,
        recorder=recorder,
        fallback_model=str(model_id or ""),
    )
    trace_events.append(
        {
            "seq": len(trace_events) + 1,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "kind": "turn_call_log_summary",
            "llm_response_records": len(llm_response_records(recorder.records)),
            "records": copy.deepcopy(recorder.records),
        }
    )
    setup = consume_provider_setup(provider)
    setup_latency_ms = coerce_metric_int(setup.get("latency_ms"))
    setup_usage = setup.get("usage") if isinstance(setup.get("usage"), list) else []
    routing_trace = setup.get("routing") if isinstance(setup.get("routing"), dict) else {}
    if setup:
        trace_events.insert(
            0,
            {
                "seq": 0,
                "elapsed_ms": setup_latency_ms,
                "kind": "routing_setup",
                "routing": routing_trace,
                "usage": setup_usage,
            },
        )
    return RunResult(
        final_text="".join(text_parts),
        done=provider_done,
        error=error,
        latency_ms=int((time.monotonic() - started) * 1000) + setup_latency_ms,
        ttft_ms=(ttft_ms + setup_latency_ms if ttft_ms is not None else None),
        tool_call_count=tool_call_count,
        trace_events=copy.deepcopy(trace_events),
        setup_latency_ms=setup_latency_ms,
        setup_usage=setup_usage,
        routing_trace=routing_trace,
    )


def coerce_metric_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int | float):
        return max(0, int(value))
    try:
        return max(0, int(str(value).strip()))
    except (TypeError, ValueError):
        return 0


def server_tool_counts_from_provider_usage(provider_usage: Any) -> dict[str, int]:
    if not isinstance(provider_usage, dict):
        return {}
    raw_counts = provider_usage.get("server_tool_use")
    if not isinstance(raw_counts, dict):
        return {}
    counts: dict[str, int] = {}
    for key, value in raw_counts.items():
        count = coerce_metric_int(value)
        if count:
            counts[str(key)] = counts.get(str(key), 0) + count
    return counts


def add_metric_counts(target: dict[str, int], source: dict[str, int]) -> None:
    for key, value in source.items():
        target[key] = target.get(key, 0) + int(value)


def server_tool_counts_from_usage_payload(usage: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    breakdown = usage.get("model_usage_breakdown")
    if isinstance(breakdown, list):
        for row in breakdown:
            if isinstance(row, dict):
                add_metric_counts(
                    counts,
                    server_tool_counts_from_provider_usage(row.get("provider_usage")),
                )
        if counts:
            return counts
    return server_tool_counts_from_provider_usage(usage.get("provider_usage"))


def llm_request_count_for_run(
    *,
    spec: dict[str, str],
    done: DoneEvent | None,
    provider_attempted: bool,
) -> int:
    if not provider_attempted:
        return 0
    if done is not None:
        usage = done_payload(done)
        trace = done.ensemble_trace if isinstance(done.ensemble_trace, Mapping) else {}
        evidence: dict[str, Any] = {
            "usage": usage,
            "request_started": True,
        }
        if trace:
            evidence["ensemble_trace"] = trace
        return derive_physical_request_count(evidence, default_request_count=1)
    return 1


def done_payload(done: DoneEvent | None) -> dict[str, Any]:
    if done is None:
        return {}
    payload = {
        "provider": str(getattr(done, "provider", "") or ""),
        "model": done.model,
        "requested_provider": str(getattr(done, "requested_provider", "") or ""),
        "requested_model": str(getattr(done, "requested_model", "") or ""),
        "stop_reason": done.stop_reason,
        "input_tokens": done.input_tokens,
        "output_tokens": done.output_tokens,
        "reasoning_tokens": done.reasoning_tokens,
        "cached_tokens": done.cached_tokens,
        "cache_write_tokens": done.cache_write_tokens,
        "billed_cost": done.billed_cost,
        "cost_source": done.cost_source,
        "usage_missing_count": max(
            0,
            coerce_metric_int(getattr(done, "usage_missing_count", 0)),
        ),
        "provider_usage": getattr(done, "provider_usage", {}),
        "model_usage_breakdown": done.model_usage_breakdown,
        "reasoning_content_chars": len(done.reasoning_content or ""),
        "thinking_signature_present": bool(done.thinking_signature),
    }
    billing_receipt = getattr(done, "billing_receipt", None)
    physical_attempt_id = str(
        getattr(done, "physical_attempt_id", "") or ""
    )
    if physical_attempt_id:
        payload["physical_attempt_id"] = physical_attempt_id
    if billing_receipt is not None:
        payload["billing_receipt"] = billing_receipt
    payload["billed_cost"] = trusted_provider_billed_cost(payload)
    exact_cost = exact_provider_usage_cost(payload)
    if exact_cost is not None:
        payload["cost_source"] = "provider_billed"
    elif billing_receipt is not None:
        payload["cost_source"] = "unavailable"
    trace = done.ensemble_trace if isinstance(done.ensemble_trace, Mapping) else {}
    evidence_run: dict[str, Any] = {
        "usage": payload,
        "request_started": True,
    }
    if trace:
        evidence_run["ensemble_trace"] = trace
    identity_material = {
        "provider": payload["provider"],
        "model": payload["model"],
        "requested_provider": payload["requested_provider"],
        "requested_model": payload["requested_model"],
        "stop_reason": payload["stop_reason"],
        "provider_usage": payload["provider_usage"],
        "usage_missing_count": payload["usage_missing_count"],
    }
    identity_seed = (
        "done:"
        + hashlib.sha256(
            json.dumps(
                identity_material,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    )
    payload = canonicalize_run_usage(
        evidence_run,
        identity_seed=identity_seed,
        requested_provider=payload["requested_provider"],
        requested_model=payload["requested_model"],
        default_request_count=1,
    )
    server_tool_use = server_tool_counts_from_usage_payload(payload)
    payload["server_tool_use"] = server_tool_use
    payload["server_tool_call_count"] = sum(server_tool_use.values())
    return payload


def usage_row_is_missing_placeholder(row: Mapping[str, Any]) -> bool:
    return is_missing_usage_placeholder(row)


def usage_row_response_ids(row: Mapping[str, Any]) -> frozenset[str]:
    values: list[Any] = []
    direct = row.get("response_id")
    if direct is not None:
        values.append(direct)
    provider_usage = row.get("provider_usage")
    if isinstance(provider_usage, Mapping):
        response_ids = provider_usage.get("response_ids")
        if isinstance(response_ids, (list, tuple, set, frozenset)):
            values.extend(response_ids)
        elif response_ids is not None:
            values.append(response_ids)
        response_id = provider_usage.get("response_id")
        if response_id is not None:
            values.append(response_id)
    return frozenset(str(value).strip() for value in values if str(value).strip())


def usage_receipt_fingerprint(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        str(row.get("provider") or "").strip(),
        str(row.get("model") or "").strip(),
        coerce_metric_int(row.get("input_tokens")),
        coerce_metric_int(row.get("output_tokens")),
        coerce_metric_int(row.get("reasoning_tokens")),
        coerce_metric_int(row.get("cached_tokens")),
        coerce_metric_int(row.get("cache_write_tokens")),
        float(row.get("billed_cost") or 0.0),
        str(row.get("cost_source") or "none").strip().casefold(),
    )


def usage_row_match_priority(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
) -> int | None:
    if usage_row_is_missing_placeholder(left) != usage_row_is_missing_placeholder(right):
        return None
    left_ids = usage_row_response_ids(left)
    right_ids = usage_row_response_ids(right)
    if left_ids and right_ids:
        return 0 if left_ids & right_ids else None
    if usage_receipt_fingerprint(left) != usage_receipt_fingerprint(right):
        return None
    return 1 if not left_ids and not right_ids else 2


STABLE_RECEIPT_EVIDENCE_KEY = "stable_receipt_evidence"
IGNORED_AGENT_DONE_POLICY_EVIDENCE_KEY = "ignored_agent_done_summary_policy_evidence"


def build_stable_receipt_evidence(
    *rows: Mapping[str, Any],
) -> dict[str, Any]:
    providers: set[str] = set()
    models: set[str] = set()
    cost_usd_nanos: set[int] = set()
    usage_is_byok_values: set[bool] = set()
    router_is_byok_values: set[bool] = set()
    token_values: dict[str, set[int]] = {
        key: set()
        for key in (
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cache_read_tokens",
            "cached_tokens",
            "cache_write_tokens",
        )
    }
    inherited_conflicts: set[str] = set()

    def _add_bool(value: Any, target: set[bool]) -> None:
        if value is True or value is False:
            target.add(value)

    def _add_cost(value: Any) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(float(value))
            or float(value) < 0
        ):
            return
        cost_usd_nanos.add(int(round(float(value) * 1_000_000_000)))

    for row in rows:
        provider = str(row.get("provider") or "").strip().casefold()
        if provider:
            providers.add(provider)
        model = str(row.get("model") or "").strip()
        if model:
            models.add(model)
        _add_cost(row.get("billed_cost"))
        for key, values in token_values.items():
            raw_value = row.get(key)
            if isinstance(raw_value, int) and not isinstance(raw_value, bool) and raw_value >= 0:
                values.add(raw_value)
        billing_receipt = row.get("billing_receipt")
        if isinstance(billing_receipt, Mapping):
            receipt_nanos = billing_receipt.get("usd_equivalent_nanos")
            if (
                isinstance(receipt_nanos, int)
                and not isinstance(receipt_nanos, bool)
                and receipt_nanos >= 0
            ):
                cost_usd_nanos.add(receipt_nanos)

        provider_usage = row.get("provider_usage")
        if not isinstance(provider_usage, Mapping):
            continue
        _add_bool(provider_usage.get("is_byok"), usage_is_byok_values)
        _add_cost(provider_usage.get("provider_reported_cost"))
        router_metadata = provider_usage.get("router_metadata")
        if isinstance(router_metadata, Mapping):
            _add_bool(router_metadata.get("is_byok"), router_is_byok_values)
        inherited = provider_usage.get(STABLE_RECEIPT_EVIDENCE_KEY)
        if not isinstance(inherited, Mapping):
            continue
        providers.update(
            str(value).strip().casefold()
            for value in inherited.get("providers") or []
            if str(value).strip()
        )
        models.update(
            str(value).strip() for value in inherited.get("models") or [] if str(value).strip()
        )
        for value in inherited.get("cost_usd_nanos") or []:
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                cost_usd_nanos.add(value)
        for value in inherited.get("usage_is_byok_values") or []:
            _add_bool(value, usage_is_byok_values)
        for value in inherited.get("router_is_byok_values") or []:
            _add_bool(value, router_is_byok_values)
        inherited_token_values = inherited.get("token_values")
        if isinstance(inherited_token_values, Mapping):
            for key, values in token_values.items():
                for value in inherited_token_values.get(key) or []:
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                        values.add(value)
        inherited_conflicts.update(
            str(value).strip()
            for value in inherited.get("conflict_fields") or []
            if str(value).strip()
        )

    conflict_fields = set(inherited_conflicts)
    if len(providers) > 1:
        conflict_fields.add("provider")
    if len(models) > 1:
        conflict_fields.add("model")
    if len(cost_usd_nanos) > 1:
        conflict_fields.add("cost_usd_nanos")
    if len(usage_is_byok_values | router_is_byok_values) > 1:
        conflict_fields.add("is_byok")
    for key, values in token_values.items():
        if len(values) > 1:
            conflict_fields.add(key)

    return {
        "providers": sorted(providers),
        "models": sorted(models),
        "cost_usd_nanos": sorted(cost_usd_nanos),
        "usage_is_byok_values": sorted(usage_is_byok_values),
        "router_is_byok_values": sorted(router_is_byok_values),
        "token_values": {key: sorted(values) for key, values in token_values.items() if values},
        "conflict_fields": sorted(conflict_fields),
        "receipt_conflict": bool(conflict_fields),
    }


def ignored_agent_done_summary_policy_evidence(
    summary: Mapping[str, Any],
    *,
    physical_rows: list[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Retain policy contradictions from a non-physical AgentDone roll-up.

    A roll-up with ``request_count > 1`` is never a physical request and must
    not contribute tokens, cost, or request cardinality.  It can still carry
    security-relevant receipt evidence, however.  Preserve only a compact
    contradiction record so a later non-BYOK audit cannot lose an explicit
    BYOK assertion or a stable-receipt identity conflict.
    """

    stable = build_stable_receipt_evidence(summary)
    conflict_fields = {
        str(value).strip() for value in stable.get("conflict_fields") or [] if str(value).strip()
    }
    summary_provider = str(summary.get("provider") or "").strip().casefold()
    summary_model = str(summary.get("model") or "").strip()
    physical_providers = {
        str(row.get("provider") or "").strip().casefold()
        for row in physical_rows
        if str(row.get("provider") or "").strip()
    }
    physical_models = {
        str(row.get("model") or "").strip()
        for row in physical_rows
        if str(row.get("model") or "").strip()
    }
    if summary_provider and physical_providers and summary_provider not in physical_providers:
        conflict_fields.add("provider")
    if (
        summary_model
        and physical_models
        and not any(
            _formal_openrouter_models_equivalent(summary_model, physical_model)
            for physical_model in physical_models
        )
    ):
        conflict_fields.add("model")

    summary_ids = usage_row_response_ids(summary)
    matching_rows = [row for row in physical_rows if summary_ids & usage_row_response_ids(row)]
    if matching_rows:
        overlap_evidence = build_stable_receipt_evidence(summary, *matching_rows)
        overlap_conflicts = {
            str(value).strip()
            for value in overlap_evidence.get("conflict_fields") or []
            if str(value).strip()
        }
        # Cost/token totals on a multi-response summary are aggregates rather
        # than per-request receipt fields.  They must not poison accounting.
        if len(summary_ids) > 1:
            overlap_conflicts &= {"provider", "model", "is_byok"}
        conflict_fields.update(overlap_conflicts)

    byok_values = {
        value
        for key in ("usage_is_byok_values", "router_is_byok_values")
        for value in stable.get(key) or []
        if value is True or value is False
    }
    explicit_byok = True in byok_values
    if not explicit_byok and not conflict_fields:
        return None
    classification = "conflict" if conflict_fields else "explicit_byok"
    response_id_fingerprint = canonical_json_sha256(sorted(summary_ids)) if summary_ids else ""
    return {
        "source": "ignored_agent_done_summary",
        "classification": classification,
        "request_count": max(
            0,
            coerce_metric_int(summary.get("request_count")),
        ),
        "response_id_set_sha256": response_id_fingerprint,
        "explicit_byok": explicit_byok,
        "conflict_fields": sorted(conflict_fields),
    }


def merge_usage_receipt_provenance(
    target: dict[str, Any],
    source: Mapping[str, Any],
) -> None:
    target_ids = usage_row_response_ids(target)
    source_ids = usage_row_response_ids(source)
    stable_id_match = bool(target_ids and source_ids and target_ids & source_ids)
    stable_receipt_evidence = (
        build_stable_receipt_evidence(target, source) if stable_id_match else None
    )
    if stable_id_match:
        for key in (
            "provider",
            "model",
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cache_read_tokens",
            "cached_tokens",
            "cache_write_tokens",
            "billed_cost",
            "cost_source",
            "billing_receipt",
        ):
            if key in source:
                target[key] = source[key]
    for key in (
        "provider",
        "model",
        "requested_provider",
        "requested_model",
        "response_id",
        "billing_receipt",
    ):
        if not target.get(key) and source.get(key):
            target[key] = source[key]
    source_usage = source.get("provider_usage")
    source_usage = source_usage if isinstance(source_usage, Mapping) else {}
    target_usage = (
        dict(target.get("provider_usage"))
        if isinstance(target.get("provider_usage"), Mapping)
        else {}
    )
    for key, value in source_usage.items():
        if key == STABLE_RECEIPT_EVIDENCE_KEY:
            continue
        if key == "response_ids":
            existing = target_usage.get(key)
            existing_values = (
                list(existing)
                if isinstance(existing, (list, tuple, set, frozenset))
                else [existing]
                if existing is not None
                else []
            )
            source_values = (
                list(value) if isinstance(value, (list, tuple, set, frozenset)) else [value]
            )
            target_usage[key] = sorted(
                {
                    str(item).strip()
                    for item in [*existing_values, *source_values]
                    if str(item).strip()
                }
            )
        elif stable_id_match or not target_usage.get(key):
            target_usage[key] = value
    if stable_receipt_evidence is not None:
        target_usage[STABLE_RECEIPT_EVIDENCE_KEY] = stable_receipt_evidence
    if target_usage:
        target["provider_usage"] = target_usage


def deduplicate_stable_usage_receipts(
    units: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Collapse repeated physical receipts only when a stable response ID proves it."""

    deduplicated: list[dict[str, Any]] = []
    for unit in units:
        row = dict(unit)
        response_ids = usage_row_response_ids(row)
        if not response_ids:
            # Similar token/cost values are not proof of the same physical call.
            deduplicated.append(row)
            continue
        matching_indexes = [
            index
            for index, existing in enumerate(deduplicated)
            if response_ids & usage_row_response_ids(existing)
        ]
        if not matching_indexes:
            deduplicated.append(row)
            continue
        target = deduplicated[matching_indexes[0]]
        merge_usage_receipt_provenance(target, row)
        # A later receipt can bridge two previously separate response-id sets.
        for index in reversed(matching_indexes[1:]):
            merge_usage_receipt_provenance(target, deduplicated.pop(index))
    return deduplicated


def diagnostic_done_from_error_event(event: ErrorEvent) -> DoneEvent | None:
    """Preserve receipts from a failed composite request for spend accounting."""

    nested_done = event.diagnostic_done if isinstance(event.diagnostic_done, DoneEvent) else None
    rows = [dict(row) for row in event.model_usage_breakdown if isinstance(row, Mapping)]
    trace = dict(event.ensemble_trace) if isinstance(event.ensemble_trace, dict) else None
    missing_count = max(
        coerce_metric_int(event.usage_missing_count),
        coerce_metric_int(trace.get("usage_missing_count")) if trace else 0,
    )
    explicit_count = (
        max(0, event.physical_request_count)
        if isinstance(event.physical_request_count, int)
        and not isinstance(event.physical_request_count, bool)
        else None
    )
    explicit_zero = explicit_count == 0 or event.request_started is False
    if (
        nested_done is not None
        and not rows
        and trace is None
        and explicit_count in {None, 1}
        and missing_count <= 0
    ):
        return nested_done
    if nested_done is not None:
        nested_rows = [
            dict(row) for row in nested_done.model_usage_breakdown if isinstance(row, Mapping)
        ]
        if not nested_rows:
            nested_rows = [
                {
                    "role": "diagnostic_request",
                    "provider": str(nested_done.provider or ""),
                    "model": str(nested_done.model or ""),
                    "requested_provider": str(nested_done.requested_provider or ""),
                    "requested_model": str(nested_done.requested_model or ""),
                    "input_tokens": nested_done.input_tokens,
                    "output_tokens": nested_done.output_tokens,
                    "reasoning_tokens": nested_done.reasoning_tokens,
                    "cached_tokens": nested_done.cached_tokens,
                    "cache_write_tokens": nested_done.cache_write_tokens,
                    "billed_cost": nested_done.billed_cost,
                    "cost_source": nested_done.cost_source,
                    "provider_usage": dict(nested_done.provider_usage),
                    **(
                        {"billing_receipt": nested_done.billing_receipt}
                        if nested_done.billing_receipt is not None
                        else {}
                    ),
                }
            ]
        nested_trace = (
            nested_done.ensemble_trace if isinstance(nested_done.ensemble_trace, dict) else {}
        )
        nested_placeholder_count = sum(
            1 for row in nested_rows if usage_row_is_missing_placeholder(row)
        )
        nested_real_receipt_count = len(nested_rows) - nested_placeholder_count
        nested_physical_count = max(
            coerce_metric_int(nested_trace.get("physical_request_count")),
            coerce_metric_int(nested_trace.get("llm_request_count")),
            nested_real_receipt_count
            + max(
                nested_placeholder_count,
                coerce_metric_int(nested_done.usage_missing_count),
                coerce_metric_int(nested_trace.get("usage_missing_count")),
            ),
        )
        missing_count = max(
            missing_count,
            nested_placeholder_count,
            coerce_metric_int(nested_done.usage_missing_count),
            coerce_metric_int(nested_trace.get("usage_missing_count")),
            nested_physical_count - nested_real_receipt_count,
        )

        outer_row_count = len(rows)
        consumed_rows: set[int] = set()
        matched_outer_by_nested: dict[int, int] = {}
        nested_match_order = sorted(
            range(len(nested_rows)),
            key=lambda index: (
                0 if usage_row_response_ids(nested_rows[index]) else 1,
                index,
            ),
        )
        for nested_index in nested_match_order:
            nested_row = nested_rows[nested_index]
            candidates = [
                (priority, index)
                for index, row in enumerate(rows[:outer_row_count])
                if index not in consumed_rows
                if (priority := usage_row_match_priority(row, nested_row)) is not None
            ]
            matched_index = min(candidates)[1] if candidates else None
            if matched_index is not None:
                consumed_rows.add(matched_index)
                matched_outer_by_nested[nested_index] = matched_index
        for nested_index, nested_row in enumerate(nested_rows):
            matched_index = matched_outer_by_nested.get(nested_index)
            if matched_index is None:
                rows.append(nested_row)
            else:
                merge_usage_receipt_provenance(rows[matched_index], nested_row)

    placeholder_count = sum(
        1
        for row in rows
        if str(row.get("role") or "").strip().casefold() in MISSING_USAGE_PLACEHOLDER_ROLES
    )
    real_receipt_count = max(0, len(rows) - placeholder_count)
    trace_count = max(
        coerce_metric_int(trace.get("physical_request_count")) if trace else 0,
        coerce_metric_int(trace.get("llm_request_count")) if trace else 0,
    )
    if explicit_zero and real_receipt_count == 0:
        physical_count = 0
        missing_count = 0
        rows = []
    else:
        physical_count = max(
            trace_count,
            explicit_count or 0,
            real_receipt_count + max(missing_count, placeholder_count),
        )
        missing_count = max(missing_count, physical_count - real_receipt_count)
    if not rows and physical_count <= 0 and missing_count <= 0:
        return None
    if physical_count > 0:
        trace = dict(trace or {})
        trace.setdefault("mode", "diagnostic_error")
        trace["llm_request_count"] = physical_count
        trace["physical_request_count"] = physical_count
        trace["usage_missing_count"] = missing_count
    models = {
        str(row.get("model") or "").strip()
        for row in rows
        if not usage_row_is_missing_placeholder(row)
        if str(row.get("model") or "").strip()
    }
    providers = {
        str(row.get("provider") or "").strip()
        for row in rows
        if not usage_row_is_missing_placeholder(row)
        if str(row.get("provider") or "").strip()
    }
    requested_models = {
        str(row.get("requested_model") or "").strip()
        for row in rows
        if str(row.get("requested_model") or "").strip()
    }
    requested_providers = {
        str(row.get("requested_provider") or "").strip()
        for row in rows
        if str(row.get("requested_provider") or "").strip()
    }
    sources = {str(row.get("cost_source") or "none").strip().casefold() for row in rows}
    cost_source = next(iter(sources)) if len(sources) == 1 else "mixed" if sources else "none"
    return DoneEvent(
        stop_reason="error",
        input_tokens=sum(coerce_metric_int(row.get("input_tokens")) for row in rows),
        output_tokens=sum(coerce_metric_int(row.get("output_tokens")) for row in rows),
        reasoning_tokens=sum(coerce_metric_int(row.get("reasoning_tokens")) for row in rows),
        cached_tokens=sum(coerce_metric_int(row.get("cached_tokens")) for row in rows),
        cache_write_tokens=sum(coerce_metric_int(row.get("cache_write_tokens")) for row in rows),
        billed_cost=sum(trusted_provider_billed_cost(row) for row in rows),
        model=next(iter(models)) if len(models) == 1 else "",
        provider=next(iter(providers)) if len(providers) == 1 else "",
        requested_model=(next(iter(requested_models)) if len(requested_models) == 1 else ""),
        requested_provider=(
            next(iter(requested_providers)) if len(requested_providers) == 1 else ""
        ),
        cost_source=cost_source,
        model_usage_breakdown=rows,
        ensemble_trace=trace,
        usage_missing_count=missing_count,
        billing_receipt=(rows[0].get("billing_receipt") if len(rows) == 1 else None),
        provider_usage={
            "diagnostic_usage_only": True,
            "terminal_error_code": str(event.code or ""),
        },
    )


def run_result_usage_payload(result: RunResult) -> dict[str, Any]:
    payload = done_payload(result.done)
    if not result.setup_usage:
        return payload
    merged = dict(payload)
    merged.setdefault("model", getattr(result.done, "model", "") if result.done else "")
    merged.setdefault(
        "requested_provider",
        getattr(result.done, "requested_provider", "") if result.done else "",
    )
    merged.setdefault(
        "requested_model",
        getattr(result.done, "requested_model", "") if result.done else "",
    )
    merged.setdefault("stop_reason", getattr(result.done, "stop_reason", "") if result.done else "")
    merged.setdefault("provider_usage", {})
    merged.setdefault("reasoning_content_chars", 0)
    merged.setdefault("thinking_signature_present", False)
    for key in (
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cached_tokens",
        "cache_write_tokens",
    ):
        merged[key] = coerce_metric_int(merged.get(key)) + sum(
            coerce_metric_int(row.get(key)) for row in result.setup_usage
        )
    merged["billed_cost"] = float(merged.get("billed_cost") or 0.0) + sum(
        trusted_provider_billed_cost(row) for row in result.setup_usage
    )
    existing_breakdown = merged.get("model_usage_breakdown")
    merged["model_usage_breakdown"] = [
        *result.setup_usage,
        *(existing_breakdown if isinstance(existing_breakdown, list) else []),
    ]
    cost_sources = {str(row.get("cost_source") or "none") for row in result.setup_usage}
    if merged.get("cost_source"):
        cost_sources.add(str(merged["cost_source"]))
    cost_sources.discard("none")
    merged["cost_source"] = (
        next(iter(cost_sources)) if len(cost_sources) == 1 else "mixed" if cost_sources else "none"
    )
    server_tool_use = server_tool_counts_from_usage_payload(merged)
    merged["server_tool_use"] = server_tool_use
    merged["server_tool_call_count"] = sum(server_tool_use.values())
    return merged


def candidate_texts(
    done: DoneEvent | None,
    *,
    final_agent_call_only: bool = False,
) -> list[str]:
    if done is None:
        return []
    trace = done.ensemble_trace or {}
    candidates: list[Any] = []
    if isinstance(trace, dict):
        direct_candidates = trace.get("candidates")
        if isinstance(direct_candidates, list):
            candidates.extend(direct_candidates)
        calls = trace.get("calls")
        if isinstance(calls, list):
            selected_calls = calls[-1:] if final_agent_call_only else calls
            for call in selected_calls:
                if not isinstance(call, dict):
                    continue
                call_candidates = call.get("candidates")
                if isinstance(call_candidates, list):
                    candidates.extend(call_candidates)
    if not isinstance(candidates, list):
        return []
    return [
        str(candidate.get("text") or "")
        for candidate in candidates
        if isinstance(candidate, dict) and str(candidate.get("text") or "").strip()
    ]


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_NO_PHYSICAL_REQUEST_GATE_CODES = frozenset(
    {
        "agent_cleanup_in_progress",
        "agent_turn_in_progress",
        "benchmark_owner_cleanup_in_progress",
        "ensemble_cleanup_in_progress",
        "ensemble_call_in_progress",
    }
)


def run_result_was_blocked_before_request(result: RunResult) -> bool:
    """Return true only for ownership gates that start no LLM request."""

    return any(
        str(event.get("code") or "").strip() in _NO_PHYSICAL_REQUEST_GATE_CODES
        for event in result.trace_events
        if isinstance(event, dict)
    )


def run_result_error_physical_request_count(result: RunResult) -> int | None:
    """Return explicit adapter/Agent failure evidence, including zero."""

    for event in reversed(result.trace_events):
        if not isinstance(event, dict) or str(event.get("kind") or "") != "error":
            continue
        value = event.get("physical_request_count")
        if isinstance(value, int) and not isinstance(value, bool):
            return max(0, value)
        if event.get("request_started") is False:
            return 0
    return None


def run_result_summary(
    result: RunResult,
    *,
    requested_provider: str | None = None,
    requested_model: str | None = None,
    missing_usage_role: str | None = None,
) -> dict[str, Any]:
    usage = run_result_usage_payload(result)
    server_tool_call_count = coerce_metric_int(usage.get("server_tool_call_count"))
    total_tool_call_count = result.tool_call_count + server_tool_call_count
    llm_request_count = 0
    usage_unknown_count = 0
    if result.done is not None:
        done_usage = done_payload(result.done)
        trace = (
            result.done.ensemble_trace if isinstance(result.done.ensemble_trace, Mapping) else {}
        )
        done_evidence: dict[str, Any] = {
            "usage": done_usage,
            "request_started": True,
        }
        if trace:
            done_evidence["ensemble_trace"] = trace
        llm_request_count = derive_physical_request_count(
            done_evidence,
            default_request_count=1,
        )
        usage_unknown_count = ensemble_usage_unknown_count(trace)
        usage_unknown_count = max(
            usage_unknown_count,
            coerce_metric_int(result.done.usage_missing_count),
            usage_unknown_count_from_usage_payload(done_usage),
        )
    elif result.error or result.final_text:
        explicit_request_count = run_result_error_physical_request_count(result)
        if explicit_request_count is not None:
            llm_request_count = explicit_request_count
            usage_unknown_count = max(
                usage_unknown_count,
                explicit_request_count,
            )
        elif not run_result_was_blocked_before_request(result):
            llm_request_count = 1
            usage_unknown_count = max(usage_unknown_count, 1)
    llm_request_count += usage_rows_request_count(result.setup_usage)
    if result.done is None and result.setup_usage:
        usage_unknown_count += usage_unknown_count_from_usage_payload(
            {"model_usage_breakdown": result.setup_usage}
        )
    if llm_request_count:
        identity_seed = (
            "run-result:"
            + hashlib.sha256(
                json.dumps(
                    {
                        "trace_events": result.trace_events,
                        "latency_ms": result.latency_ms,
                        "final_text_sha256": text_sha256(result.final_text),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        )
        usage = canonicalize_run_usage(
            {
                "usage": usage,
                "physical_request_count": llm_request_count,
                "request_started": True,
            },
            identity_seed=identity_seed,
            requested_provider=(
                str(usage.get("requested_provider") or "")
                if requested_provider is None
                else requested_provider
            ),
            requested_model=(
                str(usage.get("requested_model") or "")
                if requested_model is None
                else requested_model
            ),
            role=missing_usage_role,
        )
        usage_unknown_count = max(
            usage_unknown_count,
            usage_unknown_count_from_usage_payload(usage),
        )
    summary = {
        "latency_ms": result.latency_ms,
        "ttft_ms": result.ttft_ms,
        "tool_call_count": result.tool_call_count,
        "stream_tool_call_count": result.tool_call_count,
        "server_tool_call_count": server_tool_call_count,
        "server_tool_use": usage.get("server_tool_use") or {},
        "total_tool_call_count": total_tool_call_count,
        "trajectory_steps": total_tool_call_count + llm_request_count,
        "llm_request_count": llm_request_count,
        "usage_unknown_count": usage_unknown_count,
        "error": result.error,
        "final_text_chars": len(result.final_text),
        "final_text_sha256": text_sha256(result.final_text),
        "usage": usage,
        "trace_events": result.trace_events,
        "setup_latency_ms": result.setup_latency_ms,
        "routing_trace": result.routing_trace,
    }
    selection_plan = (
        result.routing_trace.get("selection_plan")
        if isinstance(result.routing_trace, Mapping)
        else None
    )
    if (
        isinstance(selection_plan, Mapping)
        and selection_plan.get("ranking_thinking_assignment_enabled") is True
    ):
        summary["ensemble_trace"] = json_safe(
            result.done.ensemble_trace
            if result.done is not None
            and isinstance(result.done.ensemble_trace, Mapping)
            else {}
        )
    return summary


def judge_run_result_summary(
    result: RunResult,
    *,
    judge_provider: Any,
) -> dict[str, Any]:
    """Bind missing Judge usage to the frozen requested route at creation."""

    return run_result_summary(
        result,
        requested_provider=str(
            getattr(judge_provider, "provider_id", "") or ""
        ),
        requested_model=str(getattr(judge_provider, "model", "") or ""),
        missing_usage_role="unknown_request",
    )


def generation_postprocessing_failure_reason(
    stage: str,
    exc: Exception,
) -> str:
    """Return a non-sensitive terminal reason for paid-call postprocessing."""

    return (
        "generation_postprocessing_failed:"
        + str(stage or "unknown")
        + ":"
        + type(exc).__name__
    )


def primitive_unknown_usage_payload(
    *,
    physical_count: int,
    identity_seed: str,
    requested_provider: str,
    requested_model: str,
    role: str = "usage_missing",
) -> dict[str, Any]:
    """Build strict unknown units without depending on canonicalizers."""

    count = (
        physical_count
        if isinstance(physical_count, int)
        and not isinstance(physical_count, bool)
        and physical_count > 0
        else 0
    )
    units: list[dict[str, Any]] = []
    for ordinal in range(1, count + 1):
        canonical = json.dumps(
            {
                "identity_seed": str(identity_seed),
                "ordinal": ordinal,
                "role": role,
                "schema": USAGE_EVIDENCE_SCHEMA,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        evidence_id = (
            "sha256:"
            + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        )
        units.append(
            {
                "usage_evidence_schema": USAGE_EVIDENCE_SCHEMA,
                "usage_evidence_id": evidence_id,
                "usage_evidence_source": (
                    "emergency_physical_request_counter"
                ),
                "role": role,
                "physical_request_ordinal": ordinal,
                "provider": "",
                "model": "",
                "requested_provider": str(
                    requested_provider or ""
                ).strip(),
                "requested_model": str(
                    requested_model or ""
                ).strip(),
                "input_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "cached_tokens": 0,
                "cache_write_tokens": 0,
                "billed_cost": 0.0,
                "cost_source": "none",
                "usage_unknown": True,
                "provider_usage": {
                    "usage_unknown": True,
                    "usage_evidence_schema": USAGE_EVIDENCE_SCHEMA,
                    "usage_evidence_id": evidence_id,
                },
            }
        )
    return {
        "usage_evidence_schema": USAGE_EVIDENCE_SCHEMA,
        "model_usage_breakdown": units,
        "usage_missing_count": count,
    }


def emergency_generation_run_summary(
    result: RunResult,
    *,
    reason: str,
    stage: str,
    exception_type: str,
    identity_seed: str,
    expected_provider: str = "",
    expected_model: str = "",
) -> dict[str, Any]:
    """Persist conservative physical evidence when normal summarization fails."""

    source_error_present = bool(result.error)
    primitive_usage_fallback = False
    try:
        summary = run_result_summary(
            result,
            requested_provider=expected_provider or None,
            requested_model=expected_model or None,
        )
        summary_error_type = ""
        unvalidated_usage: Any = None
    except Exception as summary_exc:  # noqa: BLE001 - this is the last evidence boundary
        summary_error_type = type(summary_exc).__name__
        try:
            raw_usage = run_result_usage_payload(result)
        except Exception:  # noqa: BLE001 - retain a conservative unknown unit
            raw_usage = {}
        try:
            unvalidated_usage = json_safe(raw_usage)
        except Exception:  # noqa: BLE001 - never persist an unsafe object
            unvalidated_usage = {
                "capture_failed": True,
                "exception_type": summary_error_type,
            }
        if not isinstance(unvalidated_usage, Mapping):
            unvalidated_usage = {}
        breakdown = unvalidated_usage.get("model_usage_breakdown")
        represented_units = (
            len([unit for unit in breakdown if isinstance(unit, Mapping)])
            if isinstance(breakdown, list)
            else 0
        )
        explicit_count = run_result_error_physical_request_count(result)
        if explicit_count is not None:
            primary_count = max(0, explicit_count)
        elif result.done is not None:
            try:
                done_usage = done_payload(result.done)
                done_evidence: dict[str, Any] = {
                    "usage": done_usage,
                    "request_started": True,
                }
                if isinstance(result.done.ensemble_trace, Mapping):
                    done_evidence["ensemble_trace"] = (
                        result.done.ensemble_trace
                    )
                primary_count = derive_physical_request_count(
                    done_evidence,
                    default_request_count=1,
                )
            except Exception:  # noqa: BLE001 - one completed call is conservative
                primary_count = 1
        elif (
            bool(result.error or result.final_text)
            and not run_result_was_blocked_before_request(result)
        ):
            primary_count = 1
        else:
            primary_count = 0
        try:
            setup_count = usage_rows_request_count(result.setup_usage)
        except Exception:  # noqa: BLE001 - one row is at least one request
            setup_count = len(result.setup_usage)
        physical_count = max(
            represented_units,
            primary_count + setup_count,
        )
        try:
            canonical_usage = canonicalize_run_usage(
                {
                    "usage": dict(unvalidated_usage),
                    "physical_request_count": physical_count,
                    "request_started": physical_count > 0,
                },
                identity_seed=identity_seed,
                requested_provider=expected_provider,
                requested_model=expected_model,
            )
        except Exception:  # noqa: BLE001 - retry only with safe primitives
            try:
                canonical_usage = canonicalize_run_usage(
                    {
                        "usage": {},
                        "physical_request_count": physical_count,
                        "request_started": physical_count > 0,
                    },
                    identity_seed=identity_seed + ":unknown",
                    requested_provider=expected_provider,
                    requested_model=expected_model,
                )
            except Exception:  # noqa: BLE001 - canonicalizer itself is unavailable
                primitive_usage_fallback = True
                canonical_usage = primitive_unknown_usage_payload(
                    physical_count=physical_count,
                    identity_seed=identity_seed + ":primitive-unknown",
                    requested_provider=expected_provider,
                    requested_model=expected_model,
                )
        unknown_count = (
            physical_count
            if primitive_usage_fallback
            else usage_unknown_count_from_usage_payload(
                canonical_usage
            )
        )
        try:
            server_tool_use = server_tool_counts_from_usage_payload(
                canonical_usage
            )
        except Exception:  # noqa: BLE001 - tools are secondary evidence
            server_tool_use = {}
        server_tool_call_count = sum(server_tool_use.values())
        total_tool_call_count = (
            coerce_metric_int(result.tool_call_count)
            + server_tool_call_count
        )
        try:
            safe_trace_events = json_safe(result.trace_events)
        except Exception as trace_exc:  # noqa: BLE001 - primitive evidence only
            safe_trace_events = [
                {
                    "kind": "error",
                    "code": "trace_evidence_capture_failed",
                    "exception_type": type(trace_exc).__name__,
                }
            ]
        try:
            safe_routing_trace = safe_provider_build_routing_trace(
                result.routing_trace
            )
        except Exception as routing_exc:  # noqa: BLE001 - primitive evidence only
            safe_routing_trace = {
                "capture_failed": True,
                "exception_type": type(routing_exc).__name__,
            }
        summary = {
            "latency_ms": coerce_metric_int(result.latency_ms),
            "ttft_ms": result.ttft_ms,
            "tool_call_count": coerce_metric_int(result.tool_call_count),
            "stream_tool_call_count": coerce_metric_int(
                result.tool_call_count
            ),
            "server_tool_call_count": server_tool_call_count,
            "server_tool_use": server_tool_use,
            "total_tool_call_count": total_tool_call_count,
            "trajectory_steps": total_tool_call_count + physical_count,
            "llm_request_count": physical_count,
            "usage_unknown_count": unknown_count,
            "error": reason,
            "final_text_chars": len(result.final_text),
            "final_text_sha256": text_sha256(result.final_text),
            "usage": canonical_usage,
            "trace_events": safe_trace_events,
            "setup_latency_ms": coerce_metric_int(
                result.setup_latency_ms
            ),
            "routing_trace": safe_routing_trace,
        }
    summary["error"] = reason
    summary["generation_postprocessing_failure"] = {
        "stage": stage,
        "exception_type": exception_type,
        "summary_exception_type": summary_error_type,
        "source_error_present": source_error_present,
        "evidence_precision": (
            "unvalidated_raw_plus_primitive_unknown"
            if summary_error_type and primitive_usage_fallback
            else "unvalidated_raw_plus_conservative_unknown"
            if summary_error_type
            else "canonical"
        ),
    }
    if unvalidated_usage is not None:
        summary["unvalidated_usage_evidence"] = unvalidated_usage
    return summary


def recover_paid_generation_postprocessing_failure(
    pending: Mapping[str, Any],
    exc: Exception,
) -> tuple[RunResult, list[dict[str, Any]], int, str]:
    """Commit one terminal attempt after a paid-call postprocessing exception."""

    result = pending.get("result")
    if not isinstance(result, RunResult):
        raise exc
    stage = str(pending.get("stage") or "unknown")
    reason = generation_postprocessing_failure_reason(stage, exc)
    attempt_id = str(pending.get("attempt_id") or "")
    attempt_index = coerce_metric_int(pending.get("attempt_index"))
    attempts_value = pending.get("attempts")
    attempts = (
        attempts_value
        if isinstance(attempts_value, list)
        else []
    )
    result.trace_events = [
        *(
            list(result.trace_events)
            if isinstance(result.trace_events, list)
            else []
        ),
        {
            "kind": "error",
            "code": "generation_postprocessing_failed",
            "stage": stage,
            "exception_type": type(exc).__name__,
        },
    ]
    run_summary = emergency_generation_run_summary(
        result,
        reason=reason,
        stage=stage,
        exception_type=type(exc).__name__,
        identity_seed=f"generation-attempt:{attempt_id or 'unknown'}",
        expected_provider=str(pending.get("expected_provider") or ""),
        expected_model=str(pending.get("expected_model") or ""),
    )
    result.error = reason
    existing = next(
        (
            attempt
            for attempt in reversed(attempts)
            if isinstance(attempt, dict)
            and str(attempt.get("attempt_id") or "") == attempt_id
        ),
        None,
    )
    if existing is None:
        existing = {
            "attempt_id": attempt_id,
            "attempt_kind": "generation",
            "attempt": attempt_index,
            "started_at": pending.get("attempt_started_at"),
            "completed_at": time.time(),
        }
        if pending.get("adaptive_g1") is True:
            try:
                safe_plan = json_safe(
                    copy.deepcopy(
                        dict(pending.get("selection_plan") or {})
                    )
                )
            except Exception as plan_exc:  # noqa: BLE001 - evidence stays terminal
                safe_plan = {
                    "capture_failed": True,
                    "exception_type": type(plan_exc).__name__,
                }
            existing.update(
                {
                    "selection_plan": safe_plan,
                    "deterministic_proposer_failures": [],
                    "excluded_proposer_identities": sorted(
                        str(value)
                        for value in (
                            pending.get(
                                "excluded_proposer_identities"
                            )
                            or []
                        )
                    ),
                }
            )
        attempts.append(existing)
    existing.update(
        {
            "completed_at": time.time(),
            "retryable": False,
            "retry_reason": reason,
            "retry_suppressed_reason": reason,
            "will_retry": False,
            "retry_backoff_s": 0.0,
            "run": run_summary,
            "generation_postprocessing_failure": {
                "stage": stage,
                "exception_type": type(exc).__name__,
            },
        }
    )
    return result, attempts, 0, reason


def usage_rows_request_count(rows: list[dict[str, Any]]) -> int:
    """Count physical requests represented by aggregate setup-usage rows."""

    total = 0
    for row in rows:
        provider_usage = row.get("provider_usage")
        response_ids = (
            provider_usage.get("response_ids") if isinstance(provider_usage, Mapping) else None
        )
        total += max(
            1,
            coerce_metric_int(row.get("request_count")),
            len(response_ids) if isinstance(response_ids, list) else 0,
        )
    return total


def bounded_generation_attempts(value: int | None) -> int:
    try:
        attempts = GENERATION_MAX_ATTEMPTS if value is None else int(value)
    except (TypeError, ValueError):
        attempts = GENERATION_MAX_ATTEMPTS
    return max(1, min(GENERATION_MAX_ATTEMPTS, attempts))


def bounded_generation_retry_backoff(value: Any) -> float:
    try:
        backoff = float(value)
    except (TypeError, ValueError):
        backoff = DEFAULT_GENERATION_RETRY_BACKOFF_SECONDS
    return max(0.0, backoff)


def remaining_generation_attempts(
    prior_attempts_used: int,
    requested_attempts: int | None,
) -> int:
    """Apply one cumulative three-attempt budget across every resume cycle."""

    remaining = max(
        0,
        GENERATION_MAX_ATTEMPTS - max(0, int(prior_attempts_used)),
    )
    if remaining <= 0:
        return 0
    return min(remaining, bounded_generation_attempts(requested_attempts))


def g1_cross_wave_lifecycle_requires_reconstruction(
    *,
    group: str,
    prior_attempts_used: int,
    state: Mapping[str, Any] | None,
    current_run_compatibility_contract: Mapping[str, Any] | None = None,
) -> bool:
    """Block paid adaptive G1 resume until its frozen lifecycle is restored."""

    if (
        group != "G1"
        or prior_attempts_used <= 0
        or not isinstance(state, Mapping)
        or state.get("action") != "regenerate"
        or not g1_cross_wave_frozen_lifecycle_enabled(
            current_run_compatibility_contract
        )
    ):
        return False
    row = state.get("row")
    execution = row.get("execution") if isinstance(row, Mapping) else None
    attempts = (
        execution.get("generation_attempts")
        if isinstance(execution, Mapping)
        and isinstance(execution.get("generation_attempts"), list)
        else []
    )
    for attempt in attempts:
        if not isinstance(attempt, Mapping):
            continue
        if attempt.get("attempt_kind") == "provider_build_after_paid_setup":
            return True
        if any(
            field in attempt
            for field in (
                "selection_plan",
                "excluded_proposer_identities",
                "deterministic_proposer_failures",
                "retry_selection_plan",
                "retry_excluded_proposer_identities",
            )
        ):
            return True
        run = attempt.get("run")
        if not isinstance(run, Mapping):
            continue
        raw_rows: list[Any] = []
        setup_usage = run.get("setup_usage")
        if isinstance(setup_usage, list):
            raw_rows.extend(setup_usage)
        usage = run.get("usage")
        breakdown = (
            usage.get("model_usage_breakdown")
            if isinstance(usage, Mapping)
            else None
        )
        if isinstance(breakdown, list):
            raw_rows.extend(breakdown)
        if any(
            isinstance(row, Mapping)
            and (
                str(row.get("role") or "").strip().casefold()
                == "task_analyzer"
                or (
                    str(row.get("role") or "").strip().casefold()
                    == "unknown_request"
                    and str(row.get("label") or "").strip().casefold()
                    == "task_analyzer"
                )
            )
            for row in raw_rows
        ):
            return True
    return False


def g1_cross_wave_frozen_lifecycle_enabled(
    run_compatibility_contract: Mapping[str, Any] | None,
) -> bool:
    """Enable frozen G1 resume only for an explicitly enabled current switch.

    The thinking-assignment switch defaults to off.  Missing or false must
    therefore retain the pre-feature resume path even when an old G1 row
    happens to contain task-analyzer usage.
    """

    gateway = (
        run_compatibility_contract.get("gateway_execution")
        if isinstance(run_compatibility_contract, Mapping)
        else None
    )
    ensemble = gateway.get("llm_ensemble") if isinstance(gateway, Mapping) else None
    return bool(
        isinstance(ensemble, Mapping)
        and ensemble.get("ranking_thinking_assignment_enabled") is True
    )


_ENSEMBLE_METADATA_ONLY_REASONS = frozenset(
    {
        "missing_proposer_usage_metadata",
        "missing_proposer_stop_reason",
        "missing_aggregator_usage_metadata",
        "missing_aggregator_stop_reason",
        "missing_actual_proposer_identity",
        "missing_requested_proposer_identity",
        "missing_actual_aggregator_model",
        "missing_actual_aggregator_provider",
        "missing_requested_aggregator_identity",
    }
)
_ENSEMBLE_METADATA_REPAIR_STATUSES = frozenset({"backfilled", "unavailable"})


def ensemble_metadata_only_reason(reason: str) -> bool:
    """Return true only for explicitly enumerated, non-generation metadata gaps."""

    return reason in _ENSEMBLE_METADATA_ONLY_REASONS


def ensemble_metadata_field_resolved(
    record: Mapping[str, Any],
    field: str,
) -> bool:
    """Accept an absent trace field only after an explicit provenance audit."""

    repair = record.get("metadata_repair")
    item = repair.get(field) if isinstance(repair, Mapping) else None
    return bool(
        isinstance(item, Mapping)
        and str(item.get("status") or "") in _ENSEMBLE_METADATA_REPAIR_STATUSES
        and str(item.get("source") or "").strip()
    )


def g1_registry_contract_reasons(
    trace: Mapping[str, Any],
    contract: Mapping[str, Any] | None,
) -> list[str]:
    """Fail closed when a G1 call drifts from its frozen registry allowlist."""

    if not isinstance(contract, Mapping):
        return []
    reasons: list[str] = []
    profile_id = str(contract.get("profile_id") or "").strip()
    selection_mode = str(contract.get("selection_mode") or "").strip()
    candidate_scope = str(contract.get("candidate_scope") or "").strip()
    if not candidate_scope:
        candidate_scope = "exact_routes"
    expected_policy = (
        "all_registry_models" if candidate_scope == "registry_all" else "exact_openrouter_routes"
    )
    declared_policy = str(contract.get("policy") or "").strip()
    source_version = str(contract.get("source_registry_snapshot_version") or "").strip()
    expected_hash = str(contract.get("expected_routes_sha256") or "").strip()
    expected_source_registry_hash = str(
        contract.get("expected_source_registry_snapshot_sha256") or ""
    ).strip()
    expected_ranking_schema = str(
        contract.get("expected_ranking_config_schema_version") or ""
    ).strip()
    expected_ranking_version = str(contract.get("expected_ranking_config_version") or "").strip()
    expected_ranking_hash = str(contract.get("expected_ranking_config_sha256") or "").strip()
    expected_proposer_max = coerce_metric_int(contract.get("expected_proposer_count_max"))
    expected_count = coerce_metric_int(contract.get("expected_candidate_count"))
    expected_routes = contract.get("expected_routes")
    if (
        not profile_id
        or selection_mode != "router_dynamic"
        or candidate_scope not in {"registry_all", "exact_routes"}
        or (
            declared_policy != expected_policy
            if "candidate_scope" in contract
            else declared_policy not in {"", expected_policy}
        )
        or contract.get("user_profile_enabled") is not False
        or not source_version
        or len(expected_hash) != 64
        or len(expected_source_registry_hash) != 64
        or not expected_ranking_schema
        or not expected_ranking_version
        or len(expected_ranking_hash) != 64
        or expected_proposer_max <= 0
        or expected_proposer_max > expected_count
        or expected_count <= 0
        or not isinstance(expected_routes, Mapping)
        or len(expected_routes) != expected_count
    ):
        return ["invalid_g1_registry_contract"]
    expected_identities = {f"openrouter:{str(model).strip().lower()}" for model in expected_routes}
    expected_filtered_version = f"{source_version}+{profile_id}+{expected_hash[:12]}"
    executed_plan = trace.get("selection_plan")
    if not isinstance(executed_plan, Mapping):
        return ["missing_g1_selection_plan"]
    if executed_plan.get("user_profile_enabled") is not False:
        reasons.append("wrong_g1_user_profile_enabled")
    ranking_parameters = executed_plan.get("ranking_parameters")
    ranking_parameters_valid = isinstance(ranking_parameters, Mapping)
    if not ranking_parameters_valid:
        reasons.append("missing_g1_ranking_parameters")
    else:
        try:
            actual_ranking_hash = canonical_json_sha256(ranking_parameters).removeprefix("sha256:")
        except (TypeError, ValueError):
            actual_ranking_hash = ""
        if actual_ranking_hash != expected_ranking_hash:
            reasons.append("wrong_g1_ranking_config_hash")
        if (
            str(ranking_parameters.get("schema_version") or "") != expected_ranking_schema
            or str(ranking_parameters.get("config_version") or "") != expected_ranking_version
        ):
            reasons.append("wrong_g1_ranking_config_identity")
    if (
        str(executed_plan.get("ranking_config_schema_version") or "") != expected_ranking_schema
        or str(executed_plan.get("ranking_config_version") or "") != expected_ranking_version
        or str(executed_plan.get("ranking_config_hash") or "") != expected_ranking_hash
    ):
        reasons.append("wrong_g1_ranking_config_trace")
    allowlist = executed_plan.get("candidate_allowlist")
    if not isinstance(allowlist, Mapping):
        reasons.append("missing_g1_candidate_allowlist")
    else:
        expected_fields = {
            "policy": expected_policy,
            "profile_id": profile_id,
            "source_registry_snapshot_version": source_version,
            "filtered_registry_snapshot_version": expected_filtered_version,
            "expected_routes_sha256": expected_hash,
            "expected_source_registry_snapshot_sha256": (expected_source_registry_hash),
            "expected_candidate_count": expected_count,
            "candidate_count": expected_count,
        }
        if "candidate_scope" in contract:
            expected_fields["candidate_scope"] = candidate_scope
        for field, expected_value in expected_fields.items():
            if allowlist.get(field) != expected_value:
                reasons.append(f"wrong_g1_candidate_allowlist_{field}")
        traced_identities = allowlist.get("expected_identities")
        if (
            not isinstance(traced_identities, list)
            or set(traced_identities) != expected_identities
            or len(traced_identities) != expected_count
        ):
            reasons.append("wrong_g1_candidate_allowlist_identities")
    if coerce_metric_int(executed_plan.get("candidate_pool_size")) != expected_count:
        reasons.append("wrong_g1_candidate_pool_size")
    if executed_plan.get("registry_snapshot_version") != expected_filtered_version:
        reasons.append("wrong_g1_registry_snapshot_version")
    registry_hash = str(executed_plan.get("registry_snapshot_hash") or "")
    if len(registry_hash) != 64 or any(char not in "0123456789abcdef" for char in registry_hash):
        reasons.append("invalid_g1_registry_snapshot_hash")
    candidate_pool = executed_plan.get("candidate_pool")
    candidate_pool_identities = (
        [str(item.get("identity") or "") for item in candidate_pool if isinstance(item, Mapping)]
        if isinstance(candidate_pool, list)
        else []
    )
    if (
        not isinstance(candidate_pool, list)
        or len(candidate_pool) != expected_count
        or len(candidate_pool_identities) != expected_count
        or len(set(candidate_pool_identities)) != expected_count
        or set(candidate_pool_identities) != expected_identities
    ):
        reasons.append("wrong_g1_candidate_pool")
    selected_p = executed_plan.get("selected_P")
    if (
        not isinstance(selected_p, list)
        or not selected_p
        or any(not isinstance(identity, str) for identity in selected_p)
        or len(set(str(identity) for identity in selected_p)) != len(selected_p)
        or any(str(identity) not in expected_identities for identity in selected_p)
    ):
        reasons.append("wrong_g1_selected_proposers")
    selected_a = executed_plan.get("selected_A")
    if not isinstance(selected_a, str) or selected_a not in expected_identities:
        reasons.append("wrong_g1_selected_aggregator")
    task_profile = executed_plan.get("task_profile")
    derived_min = derived_max = 0
    derived_bound_reasons: list[str] = []
    if ranking_parameters_valid and isinstance(task_profile, Mapping):
        try:
            from opensquilla.provider.ranking_router import _proposer_bounds

            derived_min, derived_max, derived_bound_reasons = _proposer_bounds(
                task_profile,
                {},
                ranking_parameters,
            )
        except Exception:  # noqa: BLE001 - malformed trace must fail closed
            reasons.append("invalid_g1_proposer_bound_evidence")
    else:
        reasons.append("missing_g1_task_profile")
    declared_min = coerce_metric_int(executed_plan.get("N_min"))
    declared_max = coerce_metric_int(executed_plan.get("N_max"))
    traced_bound_reasons = executed_plan.get("bound_reasons")
    if (
        derived_min <= 0
        or derived_max < derived_min
        or derived_max > expected_proposer_max
        or declared_min != derived_min
        or declared_max != derived_max
        or not isinstance(traced_bound_reasons, list)
        or traced_bound_reasons != derived_bound_reasons
    ):
        reasons.append("wrong_g1_proposer_bounds")
    selected_count = len(selected_p) if isinstance(selected_p, list) else 0
    selected_models = (
        [str(identity).partition(":")[2] for identity in selected_p]
        if isinstance(selected_p, list)
        else []
    )
    selected_aggregator_model = selected_a.partition(":")[2] if isinstance(selected_a, str) else ""
    if (
        selected_count < derived_min
        or selected_count > derived_max
        or selected_count > expected_proposer_max
        or coerce_metric_int(executed_plan.get("proposer_count")) != selected_count
        or coerce_metric_int(executed_plan.get("proposer_sample_count")) != selected_count
        or executed_plan.get("proposer_models") != selected_models
        or str(executed_plan.get("aggregator_model") or "") != selected_aggregator_model
    ):
        reasons.append("wrong_g1_selected_proposer_count")
    try:
        from opensquilla.provider.ranking_router import (
            ranking_trace_replay_reasons,
        )

        reasons.extend(ranking_trace_replay_reasons(executed_plan))
    except Exception:  # noqa: BLE001 - completion evidence must fail closed
        reasons.append("g1_frozen_ranker_replay_failed")
    return list(dict.fromkeys(reasons))


def ensemble_call_core_reasons(
    trace: Mapping[str, Any],
    *,
    expected_selection_mode: str = "",
    expected_selection_plan: Mapping[str, Any] | None = None,
    expected_g1_registry_contract: Mapping[str, Any] | None = None,
    final_text: str = "",
    require_output_binding: bool = False,
) -> list[str]:
    """Validate one physical ensemble call without trusting declared counts."""

    reasons: list[str] = []
    if str(trace.get("request_outcome") or "llm_response") != "llm_response":
        reasons.append("aggregator_call_error")
    if trace.get("fallback_used") is not False:
        reasons.append("aggregator_fallback_used_or_unknown")
    if str(trace.get("final_request_role") or "") != "aggregator":
        reasons.append("final_request_not_aggregator")

    total = trace.get("total_candidates")
    successful = trace.get("successful_proposers")
    declared_counts_valid = bool(
        isinstance(total, int)
        and not isinstance(total, bool)
        and total > 0
        and isinstance(successful, int)
        and not isinstance(successful, bool)
        and 0 <= successful <= total
        and successful >= legal_proposer_quorum(total)
    )
    if not declared_counts_valid:
        reasons.append("insufficient_proposer_quorum")

    expected_plan = (
        dict(expected_selection_plan) if isinstance(expected_selection_plan, Mapping) else {}
    )
    expected_total = coerce_metric_int(expected_plan.get("proposer_sample_count"))
    if expected_total <= 0:
        expected_models = expected_plan.get("proposer_models")
        if isinstance(expected_models, list):
            expected_total = len(expected_models)
    if expected_total and total != expected_total:
        reasons.append("wrong_executed_proposer_count")
    if expected_total and (
        not isinstance(successful, int)
        or isinstance(successful, bool)
        or successful < legal_proposer_quorum(expected_total)
    ):
        reasons.append("insufficient_configured_proposer_quorum")

    if expected_selection_mode:
        executed_mode = str(
            trace.get("selection_strategy")
            or (
                trace.get("selection_plan", {}).get("strategy")
                if isinstance(trace.get("selection_plan"), dict)
                else ""
            )
            or ""
        )
        if executed_mode != expected_selection_mode:
            reasons.append("wrong_executed_selection_mode")
    reasons.extend(
        g1_registry_contract_reasons(
            trace,
            expected_g1_registry_contract,
        )
    )
    executed_plan = trace.get("selection_plan")
    if expected_plan:
        if not isinstance(executed_plan, Mapping):
            reasons.append("missing_executed_selection_plan")
        else:
            for field in (
                "strategy",
                "selection_mode",
                "profile",
                "proposer_models",
                "selected_P",
                "proposer_sample_count",
                "aggregator_model",
                "selected_A",
            ):
                expected_value = expected_plan.get(field)
                if (
                    expected_value not in (None, [], "")
                    and executed_plan.get(field) != expected_value
                ):
                    reasons.append(f"wrong_executed_{field}")

    candidate_rows = trace.get("candidates")
    if not isinstance(candidate_rows, list):
        reasons.append("missing_actual_proposer_candidates")
    else:
        if isinstance(total, int) and not isinstance(total, bool) and len(candidate_rows) != total:
            reasons.append("wrong_actual_proposer_count")
        proven_successes: list[bool] = []
        for candidate in candidate_rows:
            content = candidate.get("content") if isinstance(candidate, Mapping) else None
            proven = bool(
                isinstance(candidate, Mapping)
                and candidate.get("ok") is True
                and candidate.get("request_started") is True
                and isinstance(candidate.get("physical_request_count"), int)
                and not isinstance(candidate.get("physical_request_count"), bool)
                and candidate.get("physical_request_count") > 0
                and not candidate.get("error")
                and isinstance(content, Mapping)
                and coerce_metric_int(content.get("chars")) > 0
                and bool(str(content.get("text") or "").strip())
            )
            proven_successes.append(proven)
            if isinstance(candidate, Mapping) and candidate.get("ok") is True and not proven:
                reasons.append("invalid_successful_proposer_evidence")
            if isinstance(candidate, Mapping) and proven:
                if candidate.get(
                    "usage_reported"
                ) is not True and not ensemble_metadata_field_resolved(candidate, "usage"):
                    reasons.append("missing_proposer_usage_metadata")
                if not str(
                    candidate.get("stop_reason") or ""
                ).strip() and not ensemble_metadata_field_resolved(candidate, "stop_reason"):
                    reasons.append("missing_proposer_stop_reason")
        actual_successful = sum(proven_successes)
        if (
            not isinstance(successful, int)
            or isinstance(successful, bool)
            or actual_successful != successful
        ):
            reasons.append("successful_proposer_count_mismatch")
        if expected_total and actual_successful < legal_proposer_quorum(expected_total):
            reasons.append("insufficient_actual_proposer_quorum")
        expected_selected_p = expected_plan.get("selected_P")
        if isinstance(expected_selected_p, list):
            if len(candidate_rows) != len(expected_selected_p):
                reasons.append("wrong_actual_proposer_count")
            else:
                requested_identity_missing = False
                requested_identity_wrong = False
                actual_identity_missing = False
                actual_identity_wrong = False
                for candidate_index, (candidate, identity) in enumerate(
                    zip(
                        candidate_rows,
                        expected_selected_p,
                        strict=True,
                    )
                ):
                    expected_provider, separator, expected_model = (
                        identity.partition(":") if isinstance(identity, str) else ("", "", "")
                    )
                    execution = (
                        candidate.get("execution") if isinstance(candidate, Mapping) else None
                    )
                    requested_provider = (
                        candidate.get("requested_provider")
                        if isinstance(candidate, Mapping)
                        else None
                    ) or (
                        execution.get("requested_provider") or execution.get("provider")
                        if isinstance(execution, Mapping)
                        else None
                    )
                    requested_model = (
                        candidate.get("requested_model") if isinstance(candidate, Mapping) else None
                    ) or (execution.get("model") if isinstance(execution, Mapping) else None)
                    candidate_provider = (
                        candidate.get("provider") if isinstance(candidate, Mapping) else None
                    )
                    candidate_model = (
                        candidate.get("model") if isinstance(candidate, Mapping) else None
                    )
                    if (
                        separator != ":"
                        or not expected_provider.strip()
                        or not expected_model.strip()
                    ):
                        requested_identity_wrong = True
                    elif (
                        not isinstance(requested_provider, str)
                        or not requested_provider.strip()
                        or not isinstance(requested_model, str)
                        or not requested_model.strip()
                    ):
                        requested_identity_missing = True
                    elif (
                        requested_provider.strip() != expected_provider.strip()
                        or requested_model.strip() != expected_model.strip()
                    ):
                        requested_identity_wrong = True
                    provider_missing = (
                        not isinstance(candidate_provider, str) or not candidate_provider.strip()
                    )
                    model_missing = (
                        not isinstance(candidate_model, str) or not candidate_model.strip()
                    )
                    if proven_successes[candidate_index] and (
                        (
                            provider_missing
                            and not ensemble_metadata_field_resolved(
                                candidate,
                                "actual_provider",
                            )
                        )
                        or (
                            model_missing
                            and not ensemble_metadata_field_resolved(
                                candidate,
                                "actual_model",
                            )
                        )
                    ):
                        actual_identity_missing = True
                    elif (
                        isinstance(candidate_provider, str)
                        and candidate_provider.strip()
                        and candidate_provider.strip() != expected_provider.strip()
                    ) or (
                        isinstance(candidate_model, str)
                        and candidate_model.strip()
                        and candidate_model.strip() != expected_model.strip()
                    ):
                        actual_identity_wrong = True
                if requested_identity_missing:
                    reasons.append("missing_requested_proposer_identity")
                if requested_identity_wrong:
                    reasons.append("wrong_requested_proposer_identity")
                if actual_identity_missing:
                    reasons.append("missing_actual_proposer_identity")
                if actual_identity_wrong:
                    reasons.append("wrong_actual_proposer_identity")

    final_request = trace.get("final_request")
    if (
        not isinstance(final_request, Mapping)
        or final_request.get("request_started") is not True
        or str(final_request.get("role") or "") != "aggregator"
        or final_request.get("error")
        or trace.get("aggregator_error")
    ):
        reasons.append("aggregator_request_incomplete")
        return list(dict.fromkeys(reasons))

    usage = final_request.get("usage")
    if not isinstance(usage, Mapping) and not ensemble_metadata_field_resolved(
        final_request, "usage"
    ):
        reasons.append("missing_aggregator_usage_metadata")
    if (
        not isinstance(usage, Mapping) or not str(usage.get("stop_reason") or "").strip()
    ) and not ensemble_metadata_field_resolved(final_request, "stop_reason"):
        reasons.append("missing_aggregator_stop_reason")

    if expected_plan:
        expected_aggregator_model = expected_plan.get("aggregator_model")
        expected_selected_a = expected_plan.get("selected_A")
        expected_provider, separator, selected_model = (
            expected_selected_a.partition(":")
            if isinstance(expected_selected_a, str)
            else ("", "", "")
        )
        actual_model = usage.get("model") if isinstance(usage, Mapping) else None
        actual_provider = usage.get("provider") if isinstance(usage, Mapping) else None
        requested_model = usage.get("requested_model") if isinstance(usage, Mapping) else None
        requested_provider = usage.get("requested_provider") if isinstance(usage, Mapping) else None
        if not isinstance(expected_aggregator_model, str) or not (
            expected_aggregator_model.strip()
        ):
            reasons.append("wrong_actual_aggregator_model")
        elif (
            not isinstance(actual_model, str) or not actual_model.strip()
        ) and not ensemble_metadata_field_resolved(final_request, "actual_model"):
            reasons.append("missing_actual_aggregator_model")
        elif actual_model.strip() != expected_aggregator_model.strip():
            reasons.append("wrong_actual_aggregator_model")
        if (
            separator != ":"
            or not expected_provider.strip()
            or selected_model.strip()
            != (
                expected_aggregator_model.strip()
                if isinstance(expected_aggregator_model, str)
                else ""
            )
        ):
            reasons.append("wrong_actual_aggregator_provider")
        elif (
            not isinstance(actual_provider, str) or not actual_provider.strip()
        ) and not ensemble_metadata_field_resolved(final_request, "actual_provider"):
            reasons.append("missing_actual_aggregator_provider")
        elif actual_provider.strip() != expected_provider.strip():
            reasons.append("wrong_actual_aggregator_provider")
        if (
            not isinstance(requested_provider, str)
            or not requested_provider.strip()
            or not isinstance(requested_model, str)
            or not requested_model.strip()
        ):
            reasons.append("missing_requested_aggregator_identity")
        elif requested_provider.strip() != expected_provider.strip() or requested_model.strip() != (
            expected_aggregator_model.strip() if isinstance(expected_aggregator_model, str) else ""
        ):
            reasons.append("wrong_requested_aggregator_identity")

    if require_output_binding:
        output = (
            trace.get("assembled_output")
            if trace.get("output_binding_schema") == "opensquilla.ensemble-output-binding/v1"
            else final_request.get("output")
        )
        if not isinstance(output, Mapping):
            reasons.append("missing_aggregator_output_binding")
        else:
            output_text = output.get("text")
            output_chars = coerce_metric_int(output.get("chars"))
            output_truncated = output.get("truncated") is True
            if not isinstance(output_text, str) or not output_text.strip() or output_chars <= 0:
                reasons.append("missing_aggregator_output_binding")
            elif output_chars > len(final_text):
                reasons.append("wrong_aggregator_output_length")
            else:
                final_output_tail = final_text[-output_chars:]
                if (output_truncated and not final_output_tail.startswith(output_text)) or (
                    not output_truncated and output_text != final_output_tail
                ):
                    reasons.append("wrong_aggregator_output_binding")
    return list(dict.fromkeys(reasons))


def ensemble_call_trace_sequence(
    trace: Mapping[str, Any],
) -> tuple[list[Mapping[str, Any]], list[str]]:
    """Return a complete, ordered Agent-loop call sequence or strict reasons."""

    calls = trace.get("calls")
    if calls is None and str(trace.get("mode") or "") != "agent_loop":
        return [trace], []
    reasons: list[str] = []
    if not isinstance(calls, list) or not calls:
        return [], ["missing_ensemble_call_trace"]
    if any(not isinstance(item, Mapping) for item in calls):
        return [], ["invalid_ensemble_call_trace"]
    call_traces = [item for item in calls if isinstance(item, Mapping)]
    raw_count = trace.get("agent_llm_call_count")
    if (
        not isinstance(raw_count, int)
        or isinstance(raw_count, bool)
        or raw_count <= 0
        or raw_count != len(call_traces)
    ):
        reasons.append("wrong_agent_llm_call_count")
    if coerce_metric_int(trace.get("untraced_agent_llm_call_count")) != 0:
        reasons.append("untraced_agent_llm_calls")
    indices = [item.get("agent_call_index") for item in call_traces]
    if indices != list(range(1, len(call_traces) + 1)):
        reasons.append("invalid_agent_call_index_sequence")
    return call_traces, reasons


def g1_retry_physical_usage_binding_reasons(
    run: Mapping[str, Any],
    *,
    identity_seed: str,
) -> list[str]:
    """Strictly bind every paid managed-G1 ledger before more paid work."""

    from opensquilla.provider.thinking_execution import (
        THINKING_PHYSICAL_EVIDENCE_SCHEMA,
    )

    trace = run.get("ensemble_trace")
    calls, reasons = ensemble_call_trace_sequence(
        trace if isinstance(trace, Mapping) else {}
    )
    strict_calls = [
        call
        for call in calls
        if isinstance(call.get("selection_plan"), Mapping)
        and call["selection_plan"].get(
            "thinking_physical_evidence_schema"
        )
        == THINKING_PHYSICAL_EVIDENCE_SCHEMA
    ]
    if not strict_calls:
        return [
            *reasons,
            "missing_g1_thinking_physical_evidence_schema",
        ]
    if len(strict_calls) != len(calls):
        reasons.append("mixed_g1_thinking_physical_evidence_schema")

    ledger_ids: list[str] = []
    for call in strict_calls:
        candidates = call.get("candidates")
        for candidate in candidates if isinstance(candidates, list) else []:
            execution = (
                candidate.get("execution")
                if isinstance(candidate, Mapping)
                else None
            )
            physical_attempts = (
                execution.get("physical_attempts")
                if isinstance(execution, Mapping)
                else None
            )
            ledger_ids.extend(
                str(attempt.get("physical_attempt_id") or "")
                for attempt in (
                    physical_attempts
                    if isinstance(physical_attempts, list)
                    else []
                )
                if isinstance(attempt, Mapping)
                and attempt.get("request_started") is True
            )
        recovery = call.get("aggregator_recovery")
        recovery_attempts = (
            recovery.get("attempts")
            if isinstance(recovery, Mapping)
            else None
        )
        ledger_ids.extend(
            str(attempt.get("physical_attempt_id") or "")
            for attempt in (
                recovery_attempts
                if isinstance(recovery_attempts, list)
                else []
            )
            if isinstance(attempt, Mapping)
            and attempt.get("request_started") is True
        )

    def valid_id(value: str) -> bool:
        return len(value) == 32 and all(
            character in "0123456789abcdef"
            for character in value
        )

    if (
        any(not valid_id(attempt_id) for attempt_id in ledger_ids)
        or len(ledger_ids) != len(set(ledger_ids))
    ):
        reasons.append("invalid_g1_thinking_physical_attempt_set")
    try:
        units = canonical_run_usage_units(
            run,
            identity_seed=identity_seed,
        )
        total_request_count = derive_physical_request_count(run)
    except Exception as exc:  # noqa: BLE001 - suppress retry without raw evidence
        reasons.append(
            "g1_physical_usage_canonicalization_failed:"
            + type(exc).__name__
        )
        return list(dict.fromkeys(reasons))

    def is_task_analyzer_unit(unit: Mapping[str, Any]) -> bool:
        role = str(unit.get("role") or "").strip().casefold()
        return role in {"task_analyzer", "task_analyzer_attempt"} or (
            role == "unknown_request"
            and str(unit.get("label") or "").strip().casefold()
            == "task_analyzer"
        )

    analyzer_unit_count = sum(
        1 for unit in units if is_task_analyzer_unit(unit)
    )
    generation_units = [
        unit for unit in units if not is_task_analyzer_unit(unit)
    ]
    usage_ids: list[str] = []
    for unit in generation_units:
        attempt_id = str(unit.get("physical_attempt_id") or "")
        provider_usage = unit.get("provider_usage")
        nested_id = (
            str(provider_usage.get("physical_attempt_id") or "")
            if isinstance(provider_usage, Mapping)
            else ""
        )
        if not valid_id(attempt_id) or nested_id != attempt_id:
            reasons.append(
                "invalid_g1_thinking_usage_physical_attempt_id"
            )
            continue
        usage_ids.append(attempt_id)
    if len(usage_ids) != len(set(usage_ids)):
        reasons.append("invalid_g1_thinking_usage_physical_attempt_set")
    if Counter(usage_ids) != Counter(ledger_ids):
        reasons.append("g1_thinking_physical_usage_set_mismatch")
    expected_generation_requests = max(
        0,
        total_request_count - analyzer_unit_count,
    )
    if (
        len(ledger_ids) != expected_generation_requests
        or len(generation_units) != expected_generation_requests
    ):
        reasons.append(
            "g1_thinking_physical_usage_multiplicity_mismatch"
        )
    return list(dict.fromkeys(reasons))


_ADMISSIBLE_NONTERMINAL_FALLBACK_CORE_REASONS = frozenset(
    {
        "aggregator_fallback_used_or_unknown",
        "final_request_not_aggregator",
        "insufficient_proposer_quorum",
        "insufficient_configured_proposer_quorum",
        "insufficient_actual_proposer_quorum",
        "aggregator_request_incomplete",
    }
)


def admissible_empty_nonterminal_fallback_reasons(
    trace: Mapping[str, Any],
    *,
    expected_selection_plan: Mapping[str, Any] | None = None,
) -> list[str]:
    """Validate the one nonterminal fallback shape that cannot add answer text."""

    reasons: list[str] = []
    if str(trace.get("request_outcome") or "llm_response") != "llm_response":
        reasons.append("invalid_intermediate_fallback_outcome")
    if trace.get("fallback_used") is not True:
        reasons.append("invalid_intermediate_fallback_flag")
    if str(trace.get("final_request_role") or "") != "fallback_single":
        reasons.append("invalid_intermediate_fallback_role")
    total = trace.get("total_candidates")
    successful = trace.get("successful_proposers")
    if (
        not isinstance(total, int)
        or isinstance(total, bool)
        or total <= 0
        or not isinstance(successful, int)
        or isinstance(successful, bool)
        or not 0 <= successful < legal_proposer_quorum(total)
    ):
        reasons.append("invalid_intermediate_fallback_quorum")
    final_request = trace.get("final_request")
    if (
        not isinstance(final_request, Mapping)
        or final_request.get("request_started") is not True
        or str(final_request.get("role") or "") != "fallback_single"
        or final_request.get("error")
        or trace.get("aggregator_error")
    ):
        reasons.append("invalid_intermediate_fallback_request")
        return list(dict.fromkeys(reasons))
    output = final_request.get("output")
    output_chars = output.get("chars") if isinstance(output, Mapping) else None
    if (
        not isinstance(output, Mapping)
        or output.get("text") != ""
        or not isinstance(output_chars, int)
        or isinstance(output_chars, bool)
        or output_chars != 0
        or output.get("truncated") is not False
    ):
        reasons.append("intermediate_fallback_visible_output")
    usage = final_request.get("usage")
    if not isinstance(usage, Mapping):
        reasons.append("missing_intermediate_fallback_usage")
        return list(dict.fromkeys(reasons))
    execution = final_request.get("execution")
    if not isinstance(execution, Mapping):
        reasons.append("missing_intermediate_fallback_execution")
        return list(dict.fromkeys(reasons))
    actual_provider = str(usage.get("provider") or "").strip().casefold()
    requested_provider = str(usage.get("requested_provider") or "").strip().casefold()
    actual_model = str(usage.get("model") or "").strip()
    requested_model = str(usage.get("requested_model") or "").strip()
    execution_providers = [
        str(execution.get(field) or "").strip().casefold()
        for field in ("requested_provider", "provider", "actual_provider")
    ]
    execution_models = [
        str(execution.get(field) or "").strip()
        for field in ("requested_model", "model", "actual_model")
    ]
    if (
        actual_provider != "openrouter"
        or requested_provider != "openrouter"
        or any(provider != "openrouter" for provider in execution_providers)
    ):
        reasons.append("wrong_intermediate_fallback_provider")
    plan = dict(expected_selection_plan) if isinstance(expected_selection_plan, Mapping) else {}
    selected_p = plan.get("selected_P")
    allowed_models = (
        [
            str(identity).partition(":")[2].strip()
            for identity in selected_p
            if isinstance(identity, str)
            and identity.partition(":")[1] == ":"
            and identity.partition(":")[0].strip().casefold() == "openrouter"
            and identity.partition(":")[2].strip()
        ]
        if isinstance(selected_p, list)
        else []
    )
    if (
        not actual_model
        or not requested_model
        or not allowed_models
        or not any(
            _formal_openrouter_models_equivalent(actual_model, model)
            and _formal_openrouter_models_equivalent(requested_model, model)
            and all(
                _formal_openrouter_models_equivalent(execution_model, model)
                for execution_model in execution_models
            )
            for model in allowed_models
        )
    ):
        reasons.append("wrong_intermediate_fallback_model")
    if str(execution.get("role") or "") != "fallback_single":
        reasons.append("wrong_intermediate_fallback_execution_role")
    if not str(usage.get("stop_reason") or "").strip() and not ensemble_metadata_field_resolved(
        final_request,
        "stop_reason",
    ):
        reasons.append("missing_intermediate_fallback_stop_reason")
    return list(dict.fromkeys(reasons))


def agent_call_output_sequence_reasons(
    calls: Sequence[Mapping[str, Any]],
    *,
    final_text: str,
) -> list[str]:
    """Bind every visible Agent-loop response segment to the stored answer."""

    if len(calls) <= 1:
        return []
    reasons: list[str] = []
    offset = 0
    for call in calls:
        final_request = call.get("final_request")
        output = (
            call.get("assembled_output")
            if call.get("output_binding_schema") == "opensquilla.ensemble-output-binding/v1"
            else final_request.get("output")
            if isinstance(final_request, Mapping)
            else None
        )
        if not isinstance(output, Mapping):
            reasons.append("missing_agent_call_output_binding")
            continue
        output_text = output.get("text")
        output_chars = output.get("chars")
        if (
            not isinstance(output_text, str)
            or not isinstance(output_chars, int)
            or isinstance(output_chars, bool)
            or output_chars < 0
            or offset + output_chars > len(final_text)
        ):
            reasons.append("invalid_agent_call_output_binding")
            continue
        segment = final_text[offset : offset + output_chars]
        if output.get("truncated") is True:
            if not segment.startswith(output_text):
                reasons.append("wrong_agent_call_output_binding")
        elif len(output_text) != output_chars or output_text != segment:
            reasons.append("wrong_agent_call_output_binding")
        offset += output_chars
    if offset != len(final_text):
        reasons.append("incomplete_agent_call_output_binding")
    return list(dict.fromkeys(reasons))


def ensemble_generation_retry_reason(
    result: RunResult,
    *,
    expected_selection_mode: str = "",
    expected_selection_plan: Mapping[str, Any] | None = None,
    expected_g1_registry_contract: Mapping[str, Any] | None = None,
) -> str:
    """Reject a fallback or sub-quorum ensemble result before Judge is called."""

    done = result.done
    if done is None or not isinstance(done.ensemble_trace, dict):
        return "missing_ensemble_trace"
    trace = done.ensemble_trace
    call_traces, sequence_reasons = ensemble_call_trace_sequence(trace)
    if sequence_reasons:
        return sequence_reasons[0]
    if not call_traces:
        return "missing_ensemble_call_trace"
    output_sequence_reasons = agent_call_output_sequence_reasons(
        call_traces,
        final_text=result.final_text,
    )
    if output_sequence_reasons:
        return output_sequence_reasons[0]
    for index, call_trace in enumerate(call_traces):
        reasons = ensemble_call_core_reasons(
            call_trace,
            expected_selection_mode=expected_selection_mode,
            expected_selection_plan=expected_selection_plan,
            expected_g1_registry_contract=expected_g1_registry_contract,
            final_text=result.final_text,
            require_output_binding=index == len(call_traces) - 1,
        )
        generation_reasons = [
            reason for reason in reasons if not ensemble_metadata_only_reason(reason)
        ]
        if index < len(call_traces) - 1 and call_trace.get("fallback_used") is True:
            fallback_reasons = admissible_empty_nonterminal_fallback_reasons(
                call_trace,
                expected_selection_plan=expected_selection_plan,
            )
            if fallback_reasons:
                return fallback_reasons[0]
            generation_reasons = [
                reason
                for reason in generation_reasons
                if reason not in _ADMISSIBLE_NONTERMINAL_FALLBACK_CORE_REASONS
            ]
        if generation_reasons:
            return generation_reasons[0]
    return ""


def backfill_result_requested_identity(
    result: RunResult,
    *,
    expected_model: str = "",
    expected_provider: str = "",
    expected_selection_plan: Mapping[str, Any] | None = None,
) -> bool:
    """Backfill only absent requested identity from the frozen request plan."""

    done = result.done
    if done is None:
        return False
    changed_fields: list[str] = []
    plan = dict(expected_selection_plan) if isinstance(expected_selection_plan, Mapping) else {}
    selected_a = plan.get("selected_A")
    plan_provider, separator, plan_model = (
        selected_a.partition(":") if isinstance(selected_a, str) else ("", "", "")
    )
    if separator == ":" and plan_provider.strip() and plan_model.strip():
        expected_provider = plan_provider.strip()
        expected_model = plan_model.strip()
    if expected_model and not str(done.requested_model or "").strip():
        done.requested_model = expected_model
        changed_fields.append("requested_model")
    if expected_provider and not str(done.requested_provider or "").strip():
        done.requested_provider = expected_provider
        changed_fields.append("requested_provider")

    root_trace = done.ensemble_trace
    if isinstance(root_trace, dict):
        calls = root_trace.get("calls")
        call_traces = (
            [item for item in calls if isinstance(item, dict)]
            if isinstance(calls, list)
            else [root_trace]
        )
        for call_trace in call_traces:
            call_plan = (
                call_trace.get("selection_plan")
                if isinstance(call_trace.get("selection_plan"), Mapping)
                else plan
            )
            selected_p = call_plan.get("selected_P") if isinstance(call_plan, Mapping) else None
            candidates = call_trace.get("candidates")
            if (
                isinstance(selected_p, list)
                and isinstance(candidates, list)
                and len(selected_p) == len(candidates)
            ):
                for candidate, identity in zip(candidates, selected_p, strict=True):
                    if not isinstance(candidate, dict) or not isinstance(identity, str):
                        continue
                    provider, identity_separator, model = identity.partition(":")
                    if identity_separator != ":" or not provider.strip() or not model.strip():
                        continue
                    if not str(candidate.get("requested_provider") or "").strip():
                        candidate["requested_provider"] = provider.strip()
                        changed_fields.append("candidate.requested_provider")
                    if not str(candidate.get("requested_model") or "").strip():
                        candidate["requested_model"] = model.strip()
                        changed_fields.append("candidate.requested_model")
            call_selected_a = (
                call_plan.get("selected_A") if isinstance(call_plan, Mapping) else None
            )
            final_provider, final_separator, final_model = (
                call_selected_a.partition(":") if isinstance(call_selected_a, str) else ("", "", "")
            )
            if final_separator != ":" or not final_provider.strip() or not final_model.strip():
                continue
            final_request = call_trace.get("final_request")
            if not isinstance(final_request, dict):
                continue
            usage = final_request.get("usage")
            if isinstance(usage, dict):
                if not str(usage.get("requested_provider") or "").strip():
                    usage["requested_provider"] = final_provider.strip()
                    changed_fields.append("aggregator.requested_provider")
                if not str(usage.get("requested_model") or "").strip():
                    usage["requested_model"] = final_model.strip()
                    changed_fields.append("aggregator.requested_model")
            execution = final_request.get("execution")
            if isinstance(execution, dict):
                if not str(execution.get("requested_provider") or "").strip():
                    execution["requested_provider"] = final_provider.strip()
                if not str(execution.get("requested_model") or "").strip():
                    execution["requested_model"] = final_model.strip()

    if changed_fields:
        provider_usage = (
            dict(done.provider_usage) if isinstance(done.provider_usage, Mapping) else {}
        )
        provider_usage["requested_identity_backfill"] = {
            "source": "frozen_request_configuration",
            "fields": sorted(set(changed_fields)),
        }
        done.provider_usage = provider_usage
    return bool(changed_fields)


def single_generation_identity_reason(
    result: RunResult,
    *,
    expected_model: str = "",
    expected_provider: str = "",
) -> str:
    """Validate one fixed/router-single generation from physical receipts."""

    done = result.done
    if done is None:
        return GENERATION_MISSING_DONE_ERROR
    breakdown = [
        row
        for row in done.model_usage_breakdown
        if isinstance(row, Mapping)
        and str(row.get("role") or "").strip().casefold() not in MISSING_USAGE_PLACEHOLDER_ROLES
    ]
    models = {
        str(row.get("model") or "").strip()
        for row in breakdown
        if str(row.get("model") or "").strip()
    }
    providers = {
        str(row.get("provider") or "").strip()
        for row in breakdown
        if str(row.get("provider") or "").strip()
    }
    direct_model = str(done.model or "").strip()
    direct_provider = str(done.provider or "").strip()
    requested_model = str(done.requested_model or "").strip()
    requested_provider = str(done.requested_provider or "").strip()
    actual_model = direct_model or (next(iter(models)) if len(models) == 1 else "")
    actual_provider = direct_provider or (next(iter(providers)) if len(providers) == 1 else "")
    if direct_model and models and any(model != direct_model for model in models):
        return "actual_model_receipt_mismatch"
    if direct_provider and providers and any(provider != direct_provider for provider in providers):
        return "actual_provider_receipt_mismatch"
    if not actual_model:
        return "actual_model_evidence_missing"
    if expected_model and actual_model != expected_model:
        return "wrong_actual_model"
    if not actual_provider:
        return "actual_provider_evidence_missing"
    if expected_provider and actual_provider != expected_provider:
        return "wrong_actual_provider"
    if not requested_model:
        return "missing_requested_model_identity"
    if expected_model and requested_model != expected_model:
        return "wrong_requested_model"
    if not requested_provider:
        return "missing_requested_provider_identity"
    if expected_provider and requested_provider != expected_provider:
        return "wrong_requested_provider"
    return ""


def generation_retry_reason(
    result: RunResult,
    *,
    expected_selection_mode: str = "",
    expected_selection_plan: Mapping[str, Any] | None = None,
    expected_g1_registry_contract: Mapping[str, Any] | None = None,
    expected_model: str = "",
    expected_provider: str = "",
) -> str:
    if result.error:
        return result.error
    if result.done is None:
        return GENERATION_MISSING_DONE_ERROR
    if not result.final_text.strip():
        return GENERATION_EMPTY_OUTPUT_ERROR
    if expected_selection_mode:
        return ensemble_generation_retry_reason(
            result,
            expected_selection_mode=expected_selection_mode,
            expected_selection_plan=expected_selection_plan,
            expected_g1_registry_contract=expected_g1_registry_contract,
        )
    if expected_model or expected_provider:
        return single_generation_identity_reason(
            result,
            expected_model=expected_model,
            expected_provider=expected_provider,
        )
    return ""


def is_agent_hard_timeout(result: RunResult) -> bool:
    if any(str(event.get("kind") or "") == "timeout" for event in result.trace_events):
        return True
    normalized_error = str(result.error or "").strip().casefold()
    return (
        ("agent run timed out" in normalized_error)
        or ("agent turn timed out" in normalized_error)
        or ("agent_runtime_timeout" in normalized_error)
    )


def generation_cleanup_failure_reason(result: RunResult) -> str:
    """Return a non-retryable cleanup/ownership failure code, if present."""

    for event in result.trace_events:
        code = str(event.get("code") or "").strip()
        if (
            code.endswith("_close_timeout")
            or code.endswith("_cleanup_timeout")
            or code
            in {
                "agent_cleanup_in_progress",
                "agent_turn_in_progress",
                "benchmark_owner_cleanup_in_progress",
                "ensemble_cleanup_in_progress",
                "ensemble_call_in_progress",
                "provider_stream_close_failed",
                "provider_stream_close_unavailable",
            }
        ):
            return code
    normalized_error = str(result.error or "").strip().casefold()
    for marker in (
        "close_timeout",
        "close_failed",
        "close_unavailable",
        "cleanup_timeout",
        "cleanup in progress",
        "cleanup_in_progress",
        "still closing",
        "turn_in_progress",
        "call_in_progress",
    ):
        if marker in normalized_error:
            return marker
    return ""


def mark_empty_generation_output(result: RunResult) -> None:
    if result.error or result.final_text.strip():
        return
    result.error = GENERATION_EMPTY_OUTPUT_ERROR
    result.trace_events.append(
        {
            "seq": len(result.trace_events) + 1,
            "elapsed_ms": result.latency_ms,
            "kind": GENERATION_EMPTY_OUTPUT_ERROR,
        }
    )


def mark_retryable_generation_error(result: RunResult, reason: str) -> None:
    if result.error or not reason:
        return
    result.error = reason
    result.trace_events.append(
        {
            "seq": len(result.trace_events) + 1,
            "elapsed_ms": result.latency_ms,
            "kind": reason,
        }
    )


def selected_generation_attempt(
    attempts: list[dict[str, Any]],
    selected_result: RunResult,
) -> int:
    selected_sha = text_sha256(selected_result.final_text)
    selected_error = selected_result.error
    for attempt in attempts:
        run = attempt.get("run")
        if not isinstance(run, dict):
            continue
        if run.get("final_text_sha256") == selected_sha and run.get("error") == selected_error:
            return coerce_metric_int(attempt.get("attempt"))
    return coerce_metric_int(attempts[-1].get("attempt")) if attempts else 0


def _reasoning_only_length_failures_from_trace(
    root_trace: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Normalize deterministic proposer failures from every physical ensemble call."""

    if not isinstance(root_trace, Mapping):
        return []
    call_traces, _ = ensemble_call_trace_sequence(root_trace)
    failures: list[dict[str, Any]] = []
    for call_index, call_trace in enumerate(call_traces, start=1):
        candidates = call_trace.get("candidates")
        if not isinstance(candidates, list):
            continue
        for candidate_index, candidate in enumerate(candidates):
            if not isinstance(candidate, Mapping) or candidate.get("ok") is True:
                continue
            stop_reason = str(candidate.get("stop_reason") or "").strip().casefold()
            if stop_reason not in REASONING_ONLY_LENGTH_STOP_REASONS:
                continue
            content = candidate.get("content")
            content_chars = content.get("chars") if isinstance(content, Mapping) else None
            visible_chars = (
                coerce_metric_int(content_chars)
                if isinstance(content_chars, int | float) and not isinstance(content_chars, bool)
                else len(
                    str(
                        (
                            content.get("text")
                            if isinstance(content, Mapping)
                            else candidate.get("text")
                        )
                        or ""
                    )
                )
            )
            reasoning_tokens = coerce_metric_int(candidate.get("reasoning_tokens"))
            output_tokens = coerce_metric_int(candidate.get("output_tokens"))
            if reasoning_tokens <= 0:
                for field in (
                    "model_usage_breakdown",
                    "diagnostic_model_usage_breakdown",
                ):
                    breakdown = candidate.get(field)
                    if isinstance(breakdown, list):
                        reasoning_tokens += sum(
                            coerce_metric_int(row.get("reasoning_tokens"))
                            for row in breakdown
                            if isinstance(row, Mapping)
                        )
            if output_tokens <= 0:
                breakdown = candidate.get("model_usage_breakdown")
                if isinstance(breakdown, list):
                    output_tokens = sum(
                        coerce_metric_int(row.get("output_tokens"))
                        for row in breakdown
                        if isinstance(row, Mapping)
                    )
            if (
                visible_chars != 0
                or reasoning_tokens <= 0
                or candidate.get("request_started") is not True
                or coerce_metric_int(candidate.get("physical_request_count")) <= 0
            ):
                continue
            provider = (
                str(candidate.get("requested_provider") or candidate.get("provider") or "")
                .strip()
                .lower()
            )
            model = (
                str(candidate.get("requested_model") or candidate.get("model") or "")
                .strip()
                .lower()
            )
            if not provider or not model:
                continue
            actual_provider = str(candidate.get("provider") or provider).strip().lower()
            actual_model = str(candidate.get("model") or model).strip().lower()
            raw_candidate_index = candidate.get("index")
            normalized_candidate_index = (
                raw_candidate_index
                if isinstance(raw_candidate_index, int)
                and not isinstance(raw_candidate_index, bool)
                and raw_candidate_index >= 0
                else candidate_index
            )
            failures.append(
                {
                    "identity": f"{provider}:{model}",
                    "reason": "reasoning_only_length",
                    "call_index": call_index,
                    "candidate_index": normalized_candidate_index,
                    "provider": actual_provider,
                    "model": actual_model,
                    "requested_provider": provider,
                    "requested_model": model,
                    "ok": False,
                    "request_started": True,
                    "physical_request_count": coerce_metric_int(
                        candidate.get("physical_request_count")
                    ),
                    "usage_reported": candidate.get("usage_reported") is True,
                    "usage_missing_count": coerce_metric_int(candidate.get("usage_missing_count")),
                    "stop_reason": stop_reason,
                    "visible_output_chars": visible_chars,
                    "input_tokens": coerce_metric_int(candidate.get("input_tokens")),
                    "output_tokens": output_tokens,
                    "reasoning_tokens": reasoning_tokens,
                    "error": str(candidate.get("error") or ""),
                    "error_code": str(candidate.get("error_code") or ""),
                }
            )
    return failures


def deterministic_reasoning_only_length_failures(
    result: RunResult,
) -> list[dict[str, Any]]:
    done = result.done
    root_trace = done.ensemble_trace if done is not None else None
    return (
        _reasoning_only_length_failures_from_trace(root_trace)
        if isinstance(root_trace, Mapping)
        else []
    )


def g1_retry_plan_provenance_reason(
    initial_plan: Mapping[str, Any],
    retry_plan: Mapping[str, Any],
    *,
    excluded_proposer_identities: set[str],
) -> str:
    from opensquilla.provider.ranking_router import (
        ROUTER_DYNAMIC_RETRY_ROUTING_SCHEMA,
        router_dynamic_task_analysis_reuse_reasons,
    )

    parent_decision_id = str(initial_plan.get("decision_id") or "")
    expected_exclusions = sorted(excluded_proposer_identities)
    retry_routing = retry_plan.get("retry_routing")
    reuse_binding = retry_plan.get("task_analysis_reuse")
    reuse_hash = (
        str(reuse_binding.get("projection_sha256") or "")
        if isinstance(reuse_binding, Mapping)
        else ""
    )
    if (
        not parent_decision_id
        or retry_plan.get("retry_parent_decision_id") != parent_decision_id
        or retry_plan.get("retry_excluded_proposer_identities")
        != expected_exclusions
        or retry_plan.get("task_analysis_reused") is not True
        or not isinstance(retry_routing, Mapping)
        or retry_routing.get("schema")
        != ROUTER_DYNAMIC_RETRY_ROUTING_SCHEMA
        or retry_routing.get("reason")
        != "prior_attempt_reasoning_only_length"
        or retry_routing.get("parent_decision_id") != parent_decision_id
        or retry_routing.get("excluded_proposer_identities")
        != expected_exclusions
        or retry_routing.get("task_analysis_reused") is not True
        or retry_routing.get("task_analysis_source_decision_id")
        != parent_decision_id
        or retry_routing.get("task_analysis_reuse_sha256") != reuse_hash
        or router_dynamic_task_analysis_reuse_reasons(
            initial_plan,
            retry_plan,
        )
    ):
        return "reasoning_only_retry_provenance_invalid"
    return ""


def g1_immutable_selection_plan_payload(
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    from opensquilla.provider.thinking_execution import (
        immutable_selection_plan_payload,
    )

    return immutable_selection_plan_payload(plan)


def g1_execution_plan_mutation_reason(
    expected_plan: Mapping[str, Any],
    observed_plan: Mapping[str, Any],
) -> str:
    from opensquilla.provider.thinking_execution import (
        validate_thinking_execution_plan_mutation,
    )

    return (
        "g1_attempt_thinking_execution_provenance_invalid"
        if validate_thinking_execution_plan_mutation(
            expected_plan,
            observed_plan,
        )
        else ""
    )


def g1_attempt_plan_consistency_reason(
    expected_plan: Mapping[str, Any],
    result: RunResult,
) -> str:
    if not expected_plan:
        return "g1_attempt_plan_provenance_invalid"
    expected_hash = canonical_json_sha256(
        g1_immutable_selection_plan_payload(expected_plan)
    )
    routing = result.routing_trace
    if not isinstance(routing, Mapping) or not routing:
        return "g1_attempt_plan_provenance_invalid"
    routed_plan = routing.get("selection_plan")
    if (
        not isinstance(routed_plan, Mapping)
        or canonical_json_sha256(
            g1_immutable_selection_plan_payload(routed_plan)
        )
        != expected_hash
        or g1_execution_plan_mutation_reason(expected_plan, routed_plan)
    ):
        return "g1_attempt_plan_provenance_invalid"
    done = result.done
    trace = done.ensemble_trace if done is not None else None
    explicit_request_count = run_result_error_physical_request_count(result)
    has_physical_request_evidence = (
        done is not None
        or explicit_request_count is not None
        and explicit_request_count > 0
        or explicit_request_count is None
        and bool(result.error or result.final_text)
        and not run_result_was_blocked_before_request(result)
    )
    if has_physical_request_evidence and (
        not isinstance(trace, Mapping) or not trace
    ):
        return "g1_attempt_plan_provenance_invalid"
    if isinstance(trace, Mapping) and trace:
        calls, sequence_reasons = ensemble_call_trace_sequence(trace)
        if sequence_reasons or not calls:
            return "g1_attempt_plan_provenance_invalid"
        previous_physical_plan: Mapping[str, Any] = expected_plan
        for call in calls:
            physical_plan = call.get("selection_plan")
            if (
                not isinstance(physical_plan, Mapping)
                or canonical_json_sha256(
                    g1_immutable_selection_plan_payload(physical_plan)
                )
                != expected_hash
            ):
                return "g1_attempt_plan_provenance_invalid"
            from opensquilla.provider.thinking_execution import (
                validate_thinking_execution_call,
            )

            validated_plan, execution_reason = validate_thinking_execution_call(
                previous_physical_plan,
                call,
            )
            if execution_reason:
                return "g1_attempt_plan_provenance_invalid"
            previous_physical_plan = validated_plan
        if g1_execution_plan_mutation_reason(
            routed_plan,
            previous_physical_plan,
        ):
            return "g1_attempt_plan_provenance_invalid"
    return ""


def _normalized_g1_retry_identities(raw: Any) -> tuple[set[str], bool]:
    if raw is None:
        return set(), True
    if not isinstance(raw, list):
        return set(), False
    normalized = [str(identity or "").strip().lower() for identity in raw]
    valid = bool(
        all(
            identity
            and identity.partition(":")[1] == ":"
            and identity.partition(":")[0]
            and identity.partition(":")[2]
            for identity in normalized
        )
        and len(normalized) == len(set(normalized))
    )
    return set(normalized), valid


async def collect_generation_with_retries(
    provider: Any,
    prompt: str,
    *,
    timeout: float,
    config: ChatConfig | None = None,
    tools: list[ToolDefinition] | None = None,
    runner_mode: str = RUNNER_MODE_PROVIDER,
    tool_policy: dict[str, Any] | None = None,
    task_id: str = "",
    group: str = "",
    output_dir: Path | None = None,
    agent_max_iterations: int = DEFAULT_AGENT_MAX_ITERATIONS,
    finalization_policy: Mapping[str, Any] | None = None,
    max_attempts: int = GENERATION_MAX_ATTEMPTS,
    attempt_offset: int = 0,
    retry_backoff_seconds: float = 0.0,
    expected_model: str = "",
    expected_provider: str = "",
    expected_g1_registry_contract: Mapping[str, Any] | None = None,
    paid_attempt_sink: dict[str, Any] | None = None,
) -> tuple[RunResult, list[dict[str, Any]], int]:
    attempts: list[dict[str, Any]] = []
    best_non_empty: RunResult | None = None
    best_non_empty_provider: Any | None = None
    last_result: RunResult | None = None
    last_result_provider: Any | None = None
    initial_provider = provider
    if paid_attempt_sink is not None:
        paid_attempt_sink.clear()
    current_provider = provider
    initial_selection_plan = getattr(initial_provider, "selection_plan", None)
    managed_g1_lifecycle = bool(
        group == "G1"
        and isinstance(initial_selection_plan, Mapping)
        and initial_selection_plan.get(
            "ranking_thinking_assignment_enabled"
        )
        is True
    )
    frozen_unmanaged_selection_plan = (
        copy.deepcopy(dict(initial_selection_plan or {}))
        if str((GROUP_SPECS.get(group) or {}).get("kind") or "")
        == "selection_mode"
        and not managed_g1_lifecycle
        else {}
    )

    def remember_selected_provider(selected_provider: Any) -> None:
        if (
            selected_provider is not initial_provider
            or hasattr(initial_provider, "_draco_selected_retry_provider")
        ):
            setattr(
                initial_provider,
                "_draco_selected_retry_provider",
                selected_provider,
            )

    def fail_before_generation_call(
        guard_error: str,
        *,
        attempt_id: str,
        attempt_index: int,
        attempt_started_at: float,
        selection_plan: Mapping[str, Any],
    ) -> tuple[RunResult, list[dict[str, Any]], int]:
        """Fail closed without dropping already-paid provider setup evidence."""

        observed_plan: Mapping[str, Any] | None = None
        observed_plan_error = ""
        try:
            observed_plan = _provider_selection_plan_execution_snapshot(
                current_provider
            )
        except Exception as exc:  # noqa: BLE001 - preserve paid setup on guard failure
            observed_plan_error = type(exc).__name__
        frozen_routing = getattr(
            current_provider,
            "_draco_frozen_routing_trace",
            None,
        )
        raw_setup = getattr(
            current_provider,
            "_draco_setup_metrics",
            None,
        )
        setup_snapshot_error = ""
        raw_setup_usage = (
            raw_setup.get("usage")
            if isinstance(raw_setup, Mapping)
            and isinstance(raw_setup.get("usage"), list)
            else []
        )
        try:
            setup_usage = copy.deepcopy(raw_setup_usage)
        except Exception as exc:  # noqa: BLE001 - use primitive analyzer recovery
            setup_snapshot_error = type(exc).__name__
            setup_usage = conservative_task_analyzer_usage_rows(
                {
                    "attempt_count": len(raw_setup_usage),
                    "physical_attempts": raw_setup_usage,
                },
                provider_id="openrouter",
                model_id="anthropic/claude-opus-4.8",
                source="g1_pre_call_guard_setup_recovery",
                fallback_reason=type(exc).__name__,
            )
        try:
            safe_selection_plan = json_safe(
                copy.deepcopy(dict(selection_plan))
            )
            if not isinstance(safe_selection_plan, Mapping):
                raise TypeError(
                    "selection plan did not serialize to an object"
                )
            safe_selection_plan = dict(safe_selection_plan)
        except Exception as exc:  # noqa: BLE001 - preserve setup, not raw data
            safe_selection_plan = {
                "capture_failed": True,
                "exception_type": type(exc).__name__,
            }
        safe_observed_plan: dict[str, Any] | None = None
        if isinstance(observed_plan, Mapping):
            try:
                safe_observed_plan = safe_provider_build_routing_trace(
                    observed_plan
                )
            except Exception as exc:  # noqa: BLE001 - diagnostics are secondary
                observed_plan_error = (
                    observed_plan_error or type(exc).__name__
                )
        raw_setup_routing = (
            raw_setup.get("routing")
            if isinstance(raw_setup, Mapping)
            else None
        )
        try:
            routing_trace = (
                safe_provider_build_routing_trace(frozen_routing)
                if isinstance(frozen_routing, Mapping)
                else safe_provider_build_routing_trace(
                    raw_setup_routing
                )
                if isinstance(raw_setup_routing, Mapping)
                else {"selection_plan": safe_selection_plan}
            )
            if not isinstance(routing_trace, dict):
                routing_trace = dict(routing_trace)
        except Exception as exc:  # noqa: BLE001 - preserve paid setup evidence
            routing_trace = {
                "selection_plan": safe_selection_plan,
                "capture_failed": True,
                "exception_type": type(exc).__name__,
            }
        routing_trace["pre_call_guard"] = {
            "error": guard_error,
            "expected_selection_plan": safe_selection_plan,
            "observed_selection_plan": safe_observed_plan,
            "observed_selection_plan_error": observed_plan_error,
            "setup_snapshot_error": setup_snapshot_error,
            "request_started": False,
            "physical_request_count": 0,
        }
        guarded_result = RunResult(
            final_text="",
            done=None,
            error=guard_error,
            setup_latency_ms=coerce_metric_int(
                raw_setup.get("latency_ms")
                if isinstance(raw_setup, Mapping)
                else 0
            ),
            setup_usage=setup_usage,
            routing_trace=routing_trace,
            trace_events=[
                {
                    "seq": 1,
                    "elapsed_ms": coerce_metric_int(
                        raw_setup.get("latency_ms")
                        if isinstance(raw_setup, Mapping)
                        else 0
                    ),
                    "kind": "error",
                    "code": "g1_pre_call_guard_failed",
                    "request_started": False,
                    "physical_request_count": 0,
                }
            ],
        )
        try:
            guard_run_summary = run_result_summary(guarded_result)
        except Exception as exc:  # noqa: BLE001 - setup evidence must survive
            guard_run_summary = emergency_generation_run_summary(
                guarded_result,
                reason=guard_error,
                stage="g1_pre_call_guard_evidence",
                exception_type=type(exc).__name__,
                identity_seed=f"generation-attempt:{attempt_id}",
            )
        attempts.append(
            {
                "attempt_id": attempt_id,
                "attempt_kind": (
                    "provider_build_after_paid_setup"
                    if setup_usage
                    else "generation_pre_call_guard"
                ),
                "attempt": attempt_index,
                "started_at": attempt_started_at,
                "completed_at": time.time(),
                "retryable": False,
                "retry_reason": guard_error,
                "retry_suppressed_reason": guard_error,
                "will_retry": False,
                "retry_backoff_s": 0.0,
                "selection_plan": safe_selection_plan,
                "deterministic_proposer_failures": [],
                "excluded_proposer_identities": sorted(
                    excluded_proposer_identities
                ),
                "run": guard_run_summary,
            }
        )
        if isinstance(raw_setup, Mapping):
            setattr(current_provider, "_draco_setup_metrics", None)
        remember_selected_provider(current_provider)
        return guarded_result, attempts, 0

    retry_provider_factory = (
        getattr(
            provider,
            "_draco_reasoning_only_retry_factory",
            None,
        )
        if managed_g1_lifecycle
        else None
    )
    raw_prior_exclusions = (
        getattr(
            provider,
            "_draco_prior_excluded_proposer_identities",
            [],
        )
        if managed_g1_lifecycle
        else []
    )
    prior_exclusions, prior_exclusions_valid = _normalized_g1_retry_identities(
        raw_prior_exclusions
    )
    if (
        managed_g1_lifecycle
        and (
            not prior_exclusions_valid
            or raw_prior_exclusions != sorted(prior_exclusions)
        )
    ):
        raise ValueError("frozen G1 prior proposer exclusions are invalid")
    excluded_proposer_identities = (
        set(prior_exclusions) if managed_g1_lifecycle else set()
    )
    frozen_initial_plan = (
        getattr(
            provider,
            "_draco_g1_initial_selection_plan",
            None,
        )
        if managed_g1_lifecycle
        else None
    )
    g1_retry_provenance_parent = (
        copy.deepcopy(dict(frozen_initial_plan))
        if isinstance(frozen_initial_plan, Mapping)
        else copy.deepcopy(
            dict(getattr(initial_provider, "selection_plan", {}) or {})
        )
    )
    g1_thinking_plan_prefixes: dict[str, dict[str, Any]] = {}
    raw_thinking_history = (
        getattr(
            provider,
            "_draco_g1_thinking_execution_history",
            [],
        )
        if managed_g1_lifecycle
        else []
    )
    if (
        managed_g1_lifecycle
        and (
            not isinstance(raw_thinking_history, list)
            or any(not isinstance(plan, Mapping) for plan in raw_thinking_history)
        )
    ):
        raise ValueError("frozen G1 thinking execution history is invalid")
    g1_thinking_execution_history = [
        copy.deepcopy(dict(plan))
        for plan in raw_thinking_history
        if isinstance(plan, Mapping)
    ]
    if (
        not isinstance(attempt_offset, int)
        or isinstance(attempt_offset, bool)
        or not 0 <= attempt_offset < GENERATION_MAX_ATTEMPTS
    ):
        raise ValueError("generation attempt offset must be an integer within the formal budget")
    attempt_limit = min(
        bounded_generation_attempts(max_attempts),
        GENERATION_MAX_ATTEMPTS - attempt_offset,
    )
    for local_attempt_index in range(1, attempt_limit + 1):
        attempt_index = attempt_offset + local_attempt_index
        attempt_id = uuid.uuid4().hex
        attempt_started_at = time.time()
        if managed_g1_lifecycle:
            selection_plan_snapshot = getattr(
                current_provider,
                "selection_plan_execution_snapshot",
                None,
            )
            try:
                expected_selection_plan = copy.deepcopy(
                    dict(
                        selection_plan_snapshot()
                        if callable(selection_plan_snapshot)
                        else getattr(current_provider, "selection_plan", {})
                        or {}
                    )
                )
            except Exception as exc:  # noqa: BLE001 - preserve paid setup evidence
                guard_error = (
                    "g1_thinking_execution_pre_call_guard_exception:"
                    "selection_plan_snapshot:"
                    + type(exc).__name__
                )
                return fail_before_generation_call(
                    guard_error,
                    attempt_id=attempt_id,
                    attempt_index=attempt_index,
                    attempt_started_at=attempt_started_at,
                    selection_plan=initial_selection_plan,
                )
            if (
                expected_selection_plan.get(
                    "ranking_thinking_assignment_enabled"
                )
                is not True
            ):
                guard_error = "g1_thinking_execution_managed_mode_changed_before_call"
                return fail_before_generation_call(
                    guard_error,
                    attempt_id=attempt_id,
                    attempt_index=attempt_index,
                    attempt_started_at=attempt_started_at,
                    selection_plan=initial_selection_plan,
                )
            adaptive_g1 = True
        else:
            expected_selection_plan = copy.deepcopy(
                frozen_unmanaged_selection_plan
            )
            adaptive_g1 = False
        if adaptive_g1:
            from opensquilla.provider.thinking_execution import (
                THINKING_PHYSICAL_EVIDENCE_SCHEMA,
                validate_thinking_execution_history_closure,
            )

            try:
                (
                    closed_execution_plan,
                    _,
                    closure_reason,
                ) = validate_thinking_execution_history_closure(
                    g1_thinking_execution_history,
                    expected_selection_plan,
                )
                expected_selection_plan = copy.deepcopy(
                    closed_execution_plan
                )
            except Exception as exc:  # noqa: BLE001 - preserve paid setup evidence
                guard_error = (
                    "g1_thinking_execution_pre_call_guard_exception:"
                    "history_closure:"
                    + type(exc).__name__
                )
                return fail_before_generation_call(
                    guard_error,
                    attempt_id=attempt_id,
                    attempt_index=attempt_index,
                    attempt_started_at=attempt_started_at,
                    selection_plan=expected_selection_plan,
                )
            if closure_reason:
                guard_error = (
                    "g1_thinking_execution_pre_call_guard_failed:"
                    + closure_reason
                )
                return fail_before_generation_call(
                    guard_error,
                    attempt_id=attempt_id,
                    attempt_index=attempt_index,
                    attempt_started_at=attempt_started_at,
                    selection_plan=expected_selection_plan,
                )
        strict_g1_physical_binding_required = bool(
            adaptive_g1
            and expected_selection_plan.get(
                "thinking_physical_evidence_schema"
            )
            == THINKING_PHYSICAL_EVIDENCE_SCHEMA
        )
        decision_id = str(expected_selection_plan.get("decision_id") or "")
        cached_execution_plan = g1_thinking_plan_prefixes.get(decision_id)
        if adaptive_g1 and cached_execution_plan is not None:
            try:
                cached_immutable_hash = canonical_json_sha256(
                    g1_immutable_selection_plan_payload(cached_execution_plan)
                )
                observed_immutable_hash = canonical_json_sha256(
                    g1_immutable_selection_plan_payload(
                        expected_selection_plan
                    )
                )
            except Exception as exc:  # noqa: BLE001 - preserve paid setup evidence
                guard_error = (
                    "g1_thinking_execution_pre_call_guard_exception:"
                    "immutable_hash:"
                    + type(exc).__name__
                )
                return fail_before_generation_call(
                    guard_error,
                    attempt_id=attempt_id,
                    attempt_index=attempt_index,
                    attempt_started_at=attempt_started_at,
                    selection_plan=expected_selection_plan,
                )
            if cached_immutable_hash != observed_immutable_hash:
                guard_error = "g1_thinking_execution_immutable_plan_changed_before_retry"
                return fail_before_generation_call(
                    guard_error,
                    attempt_id=attempt_id,
                    attempt_index=attempt_index,
                    attempt_started_at=attempt_started_at,
                    selection_plan=expected_selection_plan,
                )
            if (
                expected_selection_plan.get("executed_thinking_assignment")
                != cached_execution_plan.get("executed_thinking_assignment")
                or expected_selection_plan.get(
                    "thinking_execution_fallbacks",
                    [],
                )
                != cached_execution_plan.get("thinking_execution_fallbacks", [])
            ):
                guard_error = "g1_thinking_execution_state_reset_before_retry"
                return fail_before_generation_call(
                    guard_error,
                    attempt_id=attempt_id,
                    attempt_index=attempt_index,
                    attempt_started_at=attempt_started_at,
                    selection_plan=expected_selection_plan,
                )
            expected_selection_plan = copy.deepcopy(cached_execution_plan)
        if runner_mode == RUNNER_MODE_AGENT_LOOP:
            result = await collect_agent_run(
                current_provider,
                prompt,
                timeout=timeout,
                config=config,
                tools=tools,
                tool_policy=tool_policy or {},
                task_id=task_id,
                group=group,
                output_dir=output_dir,
                max_iterations=agent_max_iterations,
                finalization_policy=finalization_policy,
            )
        else:
            result = await collect_run(
                current_provider,
                prompt,
                timeout=timeout,
                config=config,
                tools=tools,
            )
        if paid_attempt_sink is not None:
            paid_attempt_sink.update(
                {
                    "result": result,
                    "attempts": attempts,
                    "attempt_id": attempt_id,
                    "attempt_index": attempt_index,
                    "attempt_started_at": attempt_started_at,
                    "selection_plan": expected_selection_plan,
                    "adaptive_g1": adaptive_g1,
                    "excluded_proposer_identities": tuple(
                        sorted(excluded_proposer_identities)
                    ),
                    "expected_provider": expected_provider,
                    "expected_model": expected_model,
                    "current_provider": current_provider,
                    "stage": "paid_call_returned",
                }
            )
        last_result = result
        last_result_provider = current_provider
        group_spec = GROUP_SPECS.get(group) or {}
        expected_selection_mode = (
            str(group_spec.get("selection_mode") or "")
            if group_spec.get("kind") == "selection_mode"
            else ""
        )
        if paid_attempt_sink is not None:
            paid_attempt_sink["stage"] = "requested_identity_backfill"
        backfill_result_requested_identity(
            result,
            expected_model=expected_model,
            expected_provider=expected_provider,
            expected_selection_plan=expected_selection_plan,
        )
        if paid_attempt_sink is not None:
            paid_attempt_sink["stage"] = "generation_retry_reason"
        reason = generation_retry_reason(
            result,
            expected_selection_mode=expected_selection_mode,
            expected_selection_plan=expected_selection_plan,
            expected_g1_registry_contract=expected_g1_registry_contract,
            expected_model=expected_model,
            expected_provider=expected_provider,
        )
        mark_retryable_generation_error(result, reason)
        retry_suppressed_reason = generation_cleanup_failure_reason(result)
        if (
            not retry_suppressed_reason
            and runner_mode == RUNNER_MODE_AGENT_LOOP
            and is_agent_hard_timeout(result)
        ):
            retry_suppressed_reason = "agent_hard_timeout"
        if paid_attempt_sink is not None:
            paid_attempt_sink["stage"] = "g1_attempt_plan_consistency"
        consistency_reason = (
            g1_attempt_plan_consistency_reason(
                expected_selection_plan,
                result,
            )
            if adaptive_g1
            else ""
        )
        if consistency_reason:
            reason = consistency_reason
            retry_suppressed_reason = consistency_reason
            mark_retryable_generation_error(result, consistency_reason)
        if (
            not consistency_reason
            and result.final_text.strip()
            and best_non_empty is None
        ):
            best_non_empty = result
            best_non_empty_provider = current_provider
        if paid_attempt_sink is not None:
            paid_attempt_sink["stage"] = "deterministic_failure_extraction"
        deterministic_failures = (
            deterministic_reasoning_only_length_failures(result)
            if adaptive_g1
            else []
        )
        will_retry = (
            bool(reason) and not retry_suppressed_reason and local_attempt_index < attempt_limit
        )
        prepare_deferred_g1_retry = bool(
            adaptive_g1
            and reason
            and not retry_suppressed_reason
            and deterministic_failures
            and attempt_index < GENERATION_MAX_ATTEMPTS
            and not will_retry
        )
        if paid_attempt_sink is not None:
            paid_attempt_sink["stage"] = "run_result_summary"
        attempt_run_summary = run_result_summary(result)
        if strict_g1_physical_binding_required:
            if paid_attempt_sink is not None:
                paid_attempt_sink["stage"] = "g1_physical_usage_binding"
            physical_binding_reasons = (
                g1_retry_physical_usage_binding_reasons(
                    attempt_run_summary,
                    identity_seed=f"generation-attempt:{attempt_id}",
                )
            )
            if physical_binding_reasons:
                physical_binding_failure = (
                    "g1_physical_usage_binding_failed:"
                    + physical_binding_reasons[0]
                )
                if not reason:
                    reason = physical_binding_failure
                    mark_retryable_generation_error(
                        result,
                        physical_binding_failure,
                    )
                retry_suppressed_reason = physical_binding_failure
                will_retry = False
                prepare_deferred_g1_retry = False
        if adaptive_g1 and (will_retry or prepare_deferred_g1_retry):
            execute_g1_retry_this_wave = will_retry
            if retry_suppressed_reason:
                will_retry = False
                prepare_deferred_g1_retry = False
            else:
                will_retry = execute_g1_retry_this_wave
            if (
                (will_retry or prepare_deferred_g1_retry)
                and result.done is not None
            ):
                trace = result.done.ensemble_trace
                calls, call_reasons = ensemble_call_trace_sequence(
                    trace if isinstance(trace, Mapping) else {}
                )
                if not call_reasons and calls:
                    for call in calls:
                        physical_plan = call.get("selection_plan")
                        if isinstance(physical_plan, Mapping):
                            g1_thinking_execution_history.append(
                                copy.deepcopy(dict(physical_plan))
                            )
                    last_plan = calls[-1].get("selection_plan")
                    if isinstance(last_plan, Mapping) and decision_id:
                        g1_thinking_plan_prefixes[decision_id] = copy.deepcopy(
                            dict(last_plan)
                        )
        retry_backoff_s = (
            bounded_generation_retry_backoff(retry_backoff_seconds) * (2 ** (attempt_index - 1))
            if will_retry
            else 0.0
        )
        if paid_attempt_sink is not None:
            paid_attempt_sink["stage"] = "attempt_serialization"
        attempt_record = {
            "attempt_id": attempt_id,
            "attempt_kind": "generation",
            "attempt": attempt_index,
            "started_at": attempt_started_at,
            "completed_at": time.time(),
            "retryable": bool(reason),
            "retry_reason": reason,
            "retry_suppressed_reason": retry_suppressed_reason,
            "will_retry": will_retry,
            "retry_backoff_s": retry_backoff_s,
            "run": attempt_run_summary,
        }
        if adaptive_g1:
            attempt_record.update(
                {
                    "selection_plan": json_safe(expected_selection_plan),
                    "deterministic_proposer_failures": deterministic_failures,
                    "excluded_proposer_identities": sorted(
                        excluded_proposer_identities
                    ),
                }
            )
        attempts.append(attempt_record)
        if paid_attempt_sink is not None:
            paid_attempt_sink["stage"] = "attempt_committed"
            paid_attempt_sink["attempt_appended"] = True
        if not reason:
            remember_selected_provider(current_provider)
            return result, attempts, attempt_index
        if retry_suppressed_reason:
            remember_selected_provider(current_provider)
            return result, attempts, 0
        if (
            adaptive_g1
            and deterministic_failures
            and (will_retry or prepare_deferred_g1_retry)
        ):
            excluded_proposer_identities.update(
                str(failure["identity"]) for failure in deterministic_failures
            )
            if not callable(retry_provider_factory):
                attempts[-1]["will_retry"] = False
                attempts[-1]["retry_suppressed_reason"] = (
                    "reasoning_only_retry_factory_unavailable"
                )
                remember_selected_provider(current_provider)
                return result, attempts, 0
            if paid_attempt_sink is not None:
                paid_attempt_sink["stage"] = "retry_provider_build"
            try:
                retry_provider = retry_provider_factory(
                    sorted(excluded_proposer_identities)
                )
            except Exception as exc:  # noqa: BLE001 - fail closed before another paid call
                attempts[-1]["will_retry"] = False
                attempts[-1]["retry_suppressed_reason"] = (
                    f"reasoning_only_retry_provider_build_failed:{type(exc).__name__}"
                )
                remember_selected_provider(current_provider)
                return result, attempts, 0
            if paid_attempt_sink is not None:
                paid_attempt_sink["stage"] = "retry_plan_validation"
            retry_plan = dict(getattr(retry_provider, "selection_plan", {}) or {})
            provenance_reason = g1_retry_plan_provenance_reason(
                g1_retry_provenance_parent,
                retry_plan,
                excluded_proposer_identities=excluded_proposer_identities,
            )
            if provenance_reason:
                attempts[-1]["will_retry"] = False
                attempts[-1]["retry_suppressed_reason"] = provenance_reason
                remember_selected_provider(current_provider)
                return result, attempts, 0
            prior_selected = list(expected_selection_plan.get("selected_P") or [])
            retry_selected = list(retry_plan.get("selected_P") or [])
            if (
                not retry_selected
                or retry_selected == prior_selected
                or any(
                    str(identity).strip().lower() in excluded_proposer_identities
                    for identity in retry_selected
                )
            ):
                attempts[-1]["will_retry"] = False
                attempts[-1]["retry_suppressed_reason"] = (
                    "reasoning_only_retry_roster_unchanged"
                )
                remember_selected_provider(current_provider)
                return result, attempts, 0
            expected_quorum = legal_proposer_quorum(len(retry_selected))
            if (
                coerce_metric_int(
                    getattr(retry_provider, "min_successful_proposers", 0)
                )
                != expected_quorum
            ):
                attempts[-1]["will_retry"] = False
                attempts[-1]["retry_suppressed_reason"] = (
                    "reasoning_only_retry_quorum_changed"
                )
                remember_selected_provider(current_provider)
                return result, attempts, 0
            if paid_attempt_sink is not None:
                paid_attempt_sink["stage"] = "retry_execution_projection"
            from opensquilla.provider.thinking_execution import (
                project_thinking_execution_history,
                restore_projected_thinking_execution,
            )

            retry_snapshot = getattr(
                retry_provider,
                "selection_plan_execution_snapshot",
                None,
            )
            retry_target_plan = copy.deepcopy(
                dict(
                    retry_snapshot()
                    if callable(retry_snapshot)
                    else retry_plan
                )
            )
            projected_plan, projection_audit, projection_reason = (
                project_thinking_execution_history(
                    g1_thinking_execution_history,
                    retry_target_plan,
                )
            )
            if projection_reason:
                attempts[-1]["will_retry"] = False
                attempts[-1]["retry_suppressed_reason"] = (
                    "g1_thinking_execution_projection_failed:"
                    + projection_reason
                )
                remember_selected_provider(current_provider)
                return result, attempts, 0
            if (
                retry_target_plan.get("executed_thinking_assignment")
                != projected_plan.get("executed_thinking_assignment")
                or retry_target_plan.get("thinking_execution_fallbacks", [])
                != projected_plan.get("thinking_execution_fallbacks", [])
            ):
                try:
                    restore_projected_thinking_execution(
                        retry_provider,
                        target_plan=retry_target_plan,
                        projected_plan=projected_plan,
                    )
                except Exception as exc:  # noqa: BLE001 - fail closed before another paid call
                    attempts[-1]["will_retry"] = False
                    attempts[-1]["retry_suppressed_reason"] = (
                        "g1_thinking_execution_projection_restore_failed:"
                        + type(exc).__name__
                    )
                    remember_selected_provider(current_provider)
                    return result, attempts, 0
            retry_plan = copy.deepcopy(
                dict(retry_snapshot() if callable(retry_snapshot) else projected_plan)
            )
            attempts[-1]["thinking_execution_projection"] = json_safe(
                projection_audit
            )
            attempts[-1]["retry_selection_plan"] = json_safe(retry_plan)
            attempts[-1]["retry_excluded_proposer_identities"] = sorted(
                excluded_proposer_identities
            )
            if prepare_deferred_g1_retry:
                attempts[-1]["retry_deferred_to_next_wave"] = True
            else:
                current_provider = retry_provider
                setattr(
                    initial_provider,
                    "_draco_selected_retry_provider",
                    retry_provider,
                )
        if retry_backoff_s > 0:
            await asyncio.sleep(retry_backoff_s)
    selected = (
        best_non_empty
        or last_result
        or RunResult(
            final_text="",
            done=None,
            error=GENERATION_MISSING_DONE_ERROR,
        )
    )
    selected_provider = (
        best_non_empty_provider
        if best_non_empty is not None
        else last_result_provider
        if last_result is not None
        else initial_provider
    )
    remember_selected_provider(selected_provider)
    mark_empty_generation_output(selected)
    return selected, attempts, 0


def sum_generation_attempt_metric(attempts: list[dict[str, Any]], key: str) -> int:
    total = 0
    for attempt in attempts:
        run = attempt.get("run")
        if isinstance(run, dict):
            total += coerce_metric_int(run.get(key))
    return total


def sum_generation_attempt_server_tools(attempts: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for attempt in attempts:
        run = attempt.get("run")
        if not isinstance(run, dict):
            continue
        server_tool_use = run.get("server_tool_use")
        if isinstance(server_tool_use, dict):
            add_metric_counts(counts, server_tool_use)
    return counts


def sum_generation_attempt_billed_cost(attempts: list[dict[str, Any]]) -> float:
    total = 0.0
    for attempt in attempts:
        run = attempt.get("run")
        if not isinstance(run, dict):
            continue
        usage = run.get("usage")
        if not isinstance(usage, dict):
            continue
        billed_cost = usage.get("billed_cost")
        if isinstance(billed_cost, int | float):
            total += float(billed_cost)
    return total


def bounded_judge_attempts(value: int | None) -> int:
    try:
        attempts = JUDGE_MAX_ATTEMPTS if value is None else int(value)
    except (TypeError, ValueError):
        attempts = JUDGE_MAX_ATTEMPTS
    return max(1, min(JUDGE_MAX_ATTEMPTS, attempts))


def validated_prior_judge_attempts(
    judgment: Mapping[str, Any],
    *,
    max_attempts: int,
    unit_label: str,
) -> list[dict[str, Any]]:
    """Validate cumulative physical Judge evidence before spending more."""

    attempts = judgment.get("judge_attempts")
    if not isinstance(attempts, list):
        raise ValueError(f"{unit_label} lacks cumulative Judge attempt evidence")
    if len(attempts) > max_attempts:
        raise ValueError(f"{unit_label} exceeds the cumulative Judge attempt budget")
    declared_count = judgment.get("judge_attempt_count")
    if (
        not isinstance(declared_count, int)
        or isinstance(declared_count, bool)
        or declared_count != len(attempts)
    ):
        raise ValueError(f"{unit_label} has contradictory Judge attempt evidence")
    copied: list[dict[str, Any]] = []
    for expected_ordinal, attempt in enumerate(attempts, start=1):
        if not isinstance(attempt, Mapping):
            raise ValueError(f"{unit_label} has invalid Judge attempt evidence")
        ordinal = attempt.get("attempt")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal != expected_ordinal:
            raise ValueError(f"{unit_label} has non-cumulative Judge attempt ordinals")
        attempt_id = attempt.get("attempt_id")
        if (
            not isinstance(attempt_id, str)
            or len(attempt_id) != 32
            or any(char not in "0123456789abcdef" for char in attempt_id)
        ):
            raise ValueError(f"{unit_label} has an invalid Judge attempt identity")
        if not isinstance(attempt.get("run"), Mapping):
            raise ValueError(f"{unit_label} lacks a physical Judge run")
        copied.append(copy.deepcopy(dict(attempt)))
    return copied


def judge_attempt_budget_fields(
    *,
    attempts: list[dict[str, Any]],
    prior_attempts_used: int,
    max_attempts: int,
    new_attempt_count: int,
    exhausted: bool,
) -> dict[str, Any]:
    used = len(attempts)
    return {
        "judge_attempt_evidence_schema": JUDGE_ATTEMPT_EVIDENCE_SCHEMA,
        "judge_attempt_budget_scope": JUDGE_ATTEMPT_BUDGET_SCOPE,
        "judge_attempt_budget_limit": max_attempts,
        "prior_judge_attempts_used": prior_attempts_used,
        "judge_attempt_count": used,
        "judge_attempt_budget_used": used,
        "judge_attempt_budget_remaining": max(0, max_attempts - used),
        "judge_new_attempt_count": new_attempt_count,
        "judge_attempt_budget_exhausted": exhausted,
    }


def indexed_prior_criterion_judgments(
    prior_judge: Mapping[str, Any] | None,
    *,
    rubric: str,
    criteria: list[dict[str, Any]],
    repeats: int,
    max_attempts: int,
) -> dict[tuple[str, int], dict[str, Any]]:
    """Bind prior Judge attempts to their immutable criterion/repeat unit."""

    if prior_judge is None:
        return {}
    if (
        prior_judge.get("judge_attempt_evidence_schema") != JUDGE_ATTEMPT_EVIDENCE_SCHEMA
        or prior_judge.get("judge_attempt_budget_scope") != JUDGE_ATTEMPT_BUDGET_SCOPE
        or prior_judge.get("judge_attempt_budget_limit_per_unit") != max_attempts
    ):
        raise ValueError("prior Judge result lacks the formal cumulative budget contract")
    if prior_judge.get("prior_judge_attempts"):
        raise ValueError(
            "legacy flat prior Judge attempts cannot prove a cumulative per-unit budget"
        )
    if (
        prior_judge.get("mode") != "draco_criterion_judgments"
        or str(prior_judge.get("rubric_id") or "") != rubric
        or coerce_metric_int(prior_judge.get("judge_repeats")) != repeats
    ):
        raise ValueError("prior Judge result is not bound to the current rubric contract")
    raw_judgments = prior_judge.get("criterion_judgments")
    expected_keys = {
        (str(criterion.get("id") or ""), repeat_index)
        for repeat_index in range(repeats)
        for criterion in criteria
    }
    if not isinstance(raw_judgments, list) or len(raw_judgments) != len(expected_keys):
        raise ValueError("prior Judge result does not cover every criterion/repeat unit")
    indexed: dict[tuple[str, int], dict[str, Any]] = {}
    for raw in raw_judgments:
        if not isinstance(raw, Mapping):
            raise ValueError("prior Judge criterion evidence is invalid")
        key = (
            str(raw.get("id") or ""),
            coerce_metric_int(raw.get("repeat_index")),
        )
        if key not in expected_keys or key in indexed:
            raise ValueError("prior Judge criterion/repeat binding is invalid or duplicated")
        copied = copy.deepcopy(dict(raw))
        copied["judge_attempts"] = validated_prior_judge_attempts(
            raw,
            max_attempts=max_attempts,
            unit_label=f"Judge unit {key[0]}/{key[1]}",
        )
        indexed[key] = copied
    return indexed


def closed_judge_criterion(
    *,
    criterion: dict[str, Any],
    repeat_index: int,
    prior_judgment: Mapping[str, Any],
    attempts: list[dict[str, Any]],
    max_attempts: int,
) -> dict[str, Any]:
    """Return an explicit no-call terminal row for an exhausted Judge unit."""

    prior_used = len(attempts)
    return {
        **criterion,
        "repeat_index": repeat_index,
        "verdict": str(prior_judgment.get("verdict") or ""),
        "met": None,
        "rationale": str(prior_judgment.get("rationale") or "")[:1000],
        "raw": str(prior_judgment.get("raw") or "")[:1000],
        "error": JUDGE_ATTEMPT_BUDGET_EXHAUSTED_ERROR,
        "judge_attempts": attempts,
        **judge_attempt_budget_fields(
            attempts=attempts,
            prior_attempts_used=prior_used,
            max_attempts=max_attempts,
            new_attempt_count=0,
            exhausted=True,
        ),
    }


async def judge_text(
    *,
    judge_provider: Any | None,
    task: dict[str, Any],
    answer: str,
    dry_run: bool,
    judge_repeats: int = 1,
    judge_concurrency: int = 1,
    judge_max_attempts: int = JUDGE_MAX_ATTEMPTS,
    judge_semaphore: asyncio.Semaphore | None = None,
    prior_judge: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not answer.strip():
        return None
    criteria = rubric_criteria(task)
    if dry_run:
        if criteria:
            repeats = max(1, int(judge_repeats or 1))
            judgments = [
                {
                    **criterion,
                    "repeat_index": repeat_index,
                    "verdict": "UNMET" if criterion["weight"] < 0 else "MET",
                    "met": criterion["weight"] >= 0,
                    "rationale": "dry-run heuristic",
                }
                for repeat_index in range(repeats)
                for criterion in criteria
            ]
            return score_criterion_judgments(
                rubric_id=rubric_id(task),
                judgments=judgments,
                judge_model="dry-run",
                judge_repeats=repeats,
            )
        score = min(20, max(4, len(answer) // 40))
        dry_judge = normalize_legacy_judge_result(
            {
                "mode": "legacy_dimension_score",
                "scores": {
                    "accuracy": score // 4,
                    "completeness": score // 4,
                    "objectivity": score // 4,
                    "citation": score // 4,
                },
                "total": score,
                "rationale": "dry-run heuristic",
            }
        )
        dry_judge["judge_model"] = "dry-run"
        dry_judge["judge_cost_exempt"] = True
        return dry_judge
    if judge_provider is None:
        return None
    max_attempts = bounded_judge_attempts(judge_max_attempts)
    # One experiment-wide semaphore covers both structured rubric judging and
    # the legacy single-call Judge path.  Otherwise task concurrency can
    # silently multiply the configured Judge concurrency.
    semaphore = judge_semaphore or asyncio.Semaphore(max(1, int(judge_concurrency or 1)))
    if criteria:
        repeats = max(1, int(judge_repeats or 1))
        prior_index = indexed_prior_criterion_judgments(
            prior_judge,
            rubric=rubric_id(task),
            criteria=criteria,
            repeats=repeats,
            max_attempts=max_attempts,
        )

        async def _guarded_judge(
            criterion: dict[str, Any],
            repeat_index: int,
        ) -> dict[str, Any]:
            key = (str(criterion.get("id") or ""), repeat_index)
            prior_judgment = prior_index.get(key)
            prior_attempts = (
                validated_prior_judge_attempts(
                    prior_judgment,
                    max_attempts=max_attempts,
                    unit_label=f"Judge unit {key[0]}/{key[1]}",
                )
                if prior_judgment is not None
                else []
            )
            if (
                prior_judgment is not None
                and isinstance(prior_judgment.get("met"), bool)
                and not prior_judgment.get("error")
            ):
                reused = copy.deepcopy(prior_judgment)
                reused.update(
                    judge_attempt_budget_fields(
                        attempts=prior_attempts,
                        prior_attempts_used=len(prior_attempts),
                        max_attempts=max_attempts,
                        new_attempt_count=0,
                        exhausted=False,
                    )
                )
                return reused
            if len(prior_attempts) >= max_attempts:
                return closed_judge_criterion(
                    criterion=criterion,
                    repeat_index=repeat_index,
                    prior_judgment=prior_judgment or {},
                    attempts=prior_attempts,
                    max_attempts=max_attempts,
                )
            async with semaphore:
                return await judge_criterion(
                    judge_provider=judge_provider,
                    task=task,
                    answer=answer,
                    criterion=criterion,
                    repeat_index=repeat_index,
                    max_attempts=max_attempts,
                    prior_attempts=prior_attempts,
                )

        judgments = await asyncio.gather(
            *[
                _guarded_judge(criterion, repeat_index)
                for repeat_index in range(repeats)
                for criterion in criteria
            ]
        )
        result = score_criterion_judgments(
            rubric_id=rubric_id(task),
            judgments=judgments,
            judge_model=str(getattr(judge_provider, "model", "") or ""),
            judge_repeats=repeats,
        )
        result.update(
            {
                "judge_attempt_evidence_schema": JUDGE_ATTEMPT_EVIDENCE_SCHEMA,
                "judge_attempt_budget_scope": JUDGE_ATTEMPT_BUDGET_SCOPE,
                "judge_attempt_budget_limit_per_unit": max_attempts,
                "judge_attempt_count": sum(
                    coerce_metric_int(item.get("judge_attempt_count")) for item in judgments
                ),
                "judge_new_attempt_count": sum(
                    coerce_metric_int(item.get("judge_new_attempt_count")) for item in judgments
                ),
                "judge_attempt_budget_exhausted_count": sum(
                    item.get("judge_attempt_budget_exhausted") is True for item in judgments
                ),
            }
        )
        result["judge_attempt_budget_exhausted"] = bool(
            result["judge_attempt_budget_exhausted_count"]
        )
        return result
    rubric = task.get("rubric") or "accuracy, completeness, objectivity, citation quality"
    reference = task.get("reference") or ""
    prompt = (
        "Judge this DRACO answer. Return only JSON with keys scores, total, rationale. "
        "scores must contain accuracy, completeness, objectivity, citation, each 1-5.\n\n"
        f"Task:\n{task['prompt']}\n\n"
        f"Rubric:\n{rubric}\n\n"
        f"Reference:\n{reference}\n\n"
        f"Answer:\n{answer}"
    )
    if prior_judge is not None:
        if (
            prior_judge.get("judge_attempt_evidence_schema") != JUDGE_ATTEMPT_EVIDENCE_SCHEMA
            or prior_judge.get("judge_attempt_budget_scope") != JUDGE_ATTEMPT_BUDGET_SCOPE
            or prior_judge.get("judge_attempt_budget_limit") != max_attempts
        ):
            raise ValueError(
                "prior legacy Judge result lacks the formal cumulative budget contract"
            )
        if prior_judge.get("prior_judge_attempts"):
            raise ValueError(
                "legacy flat prior Judge attempts cannot prove a cumulative Judge budget"
            )
    attempts = (
        validated_prior_judge_attempts(
            prior_judge,
            max_attempts=max_attempts,
            unit_label="legacy Judge unit",
        )
        if prior_judge is not None
        else []
    )
    prior_attempts_used = len(attempts)
    if prior_attempts_used >= max_attempts:
        return {
            **copy.deepcopy(dict(prior_judge or {})),
            "mode": "legacy_dimension_score",
            "score_status": "incomplete",
            "judge_error_count": 1,
            "normalized_score": None,
            "total": None,
            "error": JUDGE_ATTEMPT_BUDGET_EXHAUSTED_ERROR,
            "judge_attempts": attempts,
            **judge_attempt_budget_fields(
                attempts=attempts,
                prior_attempts_used=prior_attempts_used,
                max_attempts=max_attempts,
                new_attempt_count=0,
                exhausted=True,
            ),
        }
    last_result: RunResult | None = None
    last_cleanup_failure = ""
    for attempt_index in range(prior_attempts_used + 1, max_attempts + 1):
        async with semaphore:
            result = await collect_run(
                judge_provider,
                prompt,
                timeout=120.0,
                config=ChatConfig(temperature=0.0, thinking=False),
            )
        last_result = result
        cleanup_failure = generation_cleanup_failure_reason(result)
        if cleanup_failure:
            last_cleanup_failure = cleanup_failure
        parsed = extract_json_object(result.final_text)
        attempts.append(
            {
                "attempt_id": uuid.uuid4().hex,
                "attempt": attempt_index,
                "parsed": parsed is not None,
                "schema_valid": False,
                "retry_suppressed_reason": cleanup_failure,
                "run": judge_run_result_summary(
                    result,
                    judge_provider=judge_provider,
                ),
            }
        )
        if cleanup_failure:
            break
        if parsed is not None:
            normalized = normalize_legacy_judge_result(parsed)
            attempts[-1]["schema_valid"] = normalized.get("score_status") == "complete"
            if (
                normalized.get("score_status") == "complete"
                and not result.error
            ):
                normalized["judge_run"] = judge_run_result_summary(
                    result,
                    judge_provider=judge_provider,
                )
                normalized["judge_attempts"] = attempts
                normalized.update(
                    judge_attempt_budget_fields(
                        attempts=attempts,
                        prior_attempts_used=prior_attempts_used,
                        max_attempts=max_attempts,
                        new_attempt_count=len(attempts) - prior_attempts_used,
                        exhausted=False,
                    )
                )
                return normalized
    last_text = last_result.final_text if last_result is not None else ""
    parsed_any = any(bool(attempt.get("parsed")) for attempt in attempts)
    return {
        "mode": "legacy_dimension_score",
        "score_status": "incomplete",
        "judge_error_count": 1,
        "normalized_score": None,
        "total": None,
        "error": (
            last_cleanup_failure
            or (last_result.error if last_result is not None else "")
            or ("judge_json_schema_invalid" if parsed_any else "judge_json_parse_failed")
        ),
        "raw": last_text[:2000],
        "judge_run": (
            judge_run_result_summary(
                last_result,
                judge_provider=judge_provider,
            )
            if last_result is not None
            else {}
        ),
        "judge_attempts": attempts,
        **judge_attempt_budget_fields(
            attempts=attempts,
            prior_attempts_used=prior_attempts_used,
            max_attempts=max_attempts,
            new_attempt_count=len(attempts) - prior_attempts_used,
            exhausted=len(attempts) >= max_attempts,
        ),
    }


def rubric_id(task: dict[str, Any]) -> str:
    rubric = parse_maybe_json(task.get("rubric"))
    if isinstance(rubric, dict):
        return str(rubric.get("id") or task.get("id") or "")
    return str(task.get("id") or "")


async def judge_criterion(
    *,
    judge_provider: Any,
    task: dict[str, Any],
    answer: str,
    criterion: dict[str, Any],
    repeat_index: int = 0,
    max_attempts: int = JUDGE_MAX_ATTEMPTS,
    prior_attempts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    weight = coerce_weight(criterion.get("weight"))
    criterion_type = "negative" if weight < 0 else "positive"
    prompt = (
        "You are grading a DRACO deep research answer against one rubric criterion.\n"
        "Return only JSON with keys verdict and rationale. verdict must be MET or UNMET.\n"
        "Positive criteria describe desired content. Negative criteria describe an error; "
        "for negative criteria, MET means the answer contains that error.\n\n"
        f"Original query:\n{task['prompt']}\n\n"
        f"Answer:\n{answer}\n\n"
        "Criterion:\n"
        f"- id: {criterion.get('id')}\n"
        f"- section: {criterion.get('section_title') or criterion.get('section_id')}\n"
        f"- type: {criterion_type}\n"
        f"- weight: {weight}\n"
        f"- requirement: {criterion.get('requirement')}\n"
    )
    attempt_limit = bounded_judge_attempts(max_attempts)
    attempts = copy.deepcopy(list(prior_attempts or []))
    prior_attempts_used = len(attempts)
    if prior_attempts_used >= attempt_limit:
        return closed_judge_criterion(
            criterion=criterion,
            repeat_index=repeat_index,
            prior_judgment={},
            attempts=attempts,
            max_attempts=attempt_limit,
        )
    last_row: dict[str, Any] | None = None
    for attempt_index in range(prior_attempts_used + 1, attempt_limit + 1):
        result = await collect_run(
            judge_provider,
            prompt,
            timeout=120.0,
            config=ChatConfig(temperature=0.0, thinking=False),
        )
        parsed = extract_json_object(result.final_text) or {}
        met = parse_verdict(parsed.get("verdict"))
        cleanup_failure = generation_cleanup_failure_reason(result)
        run_summary = judge_run_result_summary(
            result,
            judge_provider=judge_provider,
        )
        attempts.append(
            {
                "attempt_id": uuid.uuid4().hex,
                "attempt": attempt_index,
                "verdict": parsed.get("verdict") if parsed else "",
                "met": met,
                "retry_suppressed_reason": cleanup_failure,
                "run": run_summary,
            }
        )
        row = {
            **criterion,
            "weight": weight,
            "repeat_index": repeat_index,
            "verdict": parsed.get("verdict") if parsed else "",
            "met": met,
            "rationale": str(parsed.get("rationale") or parsed.get("reason") or "")[:1000],
            "judge_run": run_summary,
            "judge_attempts": list(attempts),
            **judge_attempt_budget_fields(
                attempts=attempts,
                prior_attempts_used=prior_attempts_used,
                max_attempts=attempt_limit,
                new_attempt_count=len(attempts) - prior_attempts_used,
                exhausted=False,
            ),
        }
        if met is not None and not cleanup_failure and not result.error:
            return row
        row["error"] = result.error or "judge_verdict_parse_failed"
        row["raw"] = result.final_text[:1000]
        last_row = row
        if cleanup_failure:
            row["retry_suppressed_reason"] = cleanup_failure
            row["error"] = cleanup_failure
            break
    if last_row is not None and len(attempts) >= attempt_limit:
        last_row["last_judge_error"] = last_row.get("error")
        last_row["error"] = JUDGE_ATTEMPT_BUDGET_EXHAUSTED_ERROR
        last_row["judge_attempt_budget_exhausted"] = True
    return (
        last_row
        if last_row is not None
        else {
            **criterion,
            "weight": weight,
            "repeat_index": repeat_index,
            "verdict": "",
            "met": None,
            "rationale": "",
            "error": "judge_verdict_parse_failed",
            "judge_attempts": [],
            **judge_attempt_budget_fields(
                attempts=[],
                prior_attempts_used=prior_attempts_used,
                max_attempts=attempt_limit,
                new_attempt_count=0,
                exhausted=False,
            ),
        }
    )


def score_criterion_judgments(
    *,
    rubric_id: str,
    judgments: list[dict[str, Any]],
    judge_model: str,
    judge_repeats: int = 1,
) -> dict[str, Any]:
    valid_judgments = [item for item in judgments if isinstance(item.get("met"), bool)]
    invalid_count = len(judgments) - len(valid_judgments)
    score_status = "partial" if invalid_count else "complete"
    positive_weight_total = sum(max(0, coerce_weight(item.get("weight"))) for item in judgments)
    raw_score = sum(
        coerce_weight(item.get("weight")) for item in valid_judgments if item.get("met") is True
    )
    valid_positive_weight_total = sum(
        max(0, coerce_weight(item.get("weight"))) for item in valid_judgments
    )
    valid_normalized = (
        clamp_percent((raw_score / valid_positive_weight_total) * 100.0)
        if valid_positive_weight_total > 0
        else None
    )
    normalized = (
        clamp_percent((raw_score / positive_weight_total) * 100.0)
        if positive_weight_total > 0
        else None
    )

    def _passed(item: dict[str, Any]) -> bool:
        weight = coerce_weight(item.get("weight"))
        met = item.get("met")
        return bool(met) if weight >= 0 else met is False

    valid_passed = [_passed(item) for item in valid_judgments]
    valid_pass_rate = (
        sum(1 for item in valid_passed if item) / len(valid_passed) * 100.0
        if valid_passed
        else None
    )
    section_scores: dict[str, dict[str, Any]] = {}
    for item in judgments:
        section_id = str(item.get("section_id") or "rubric")
        section = section_scores.setdefault(
            section_id,
            {
                "title": item.get("section_title") or section_id,
                "criteria_count": 0,
                "valid_criteria_count": 0,
                "invalid_criteria_count": 0,
                "raw_score": 0,
                "positive_weight_total": 0,
                "valid_positive_weight_total": 0,
                "passed_count": 0,
            },
        )
        weight = coerce_weight(item.get("weight"))
        met = item.get("met")
        section["criteria_count"] += 1
        section["positive_weight_total"] += max(0, weight)
        if isinstance(met, bool):
            section["valid_criteria_count"] += 1
            section["valid_positive_weight_total"] += max(0, weight)
        else:
            section["invalid_criteria_count"] += 1
            continue
        if met is True:
            section["raw_score"] += weight
        if (met is True and weight >= 0) or (met is False and weight < 0):
            section["passed_count"] += 1
    for section in section_scores.values():
        total = section["positive_weight_total"]
        valid_total = section["valid_positive_weight_total"]
        valid_section_normalized = (
            clamp_percent((section["raw_score"] / valid_total) * 100.0) if valid_total > 0 else None
        )
        valid_section_pass_rate = (
            section["passed_count"] / section["valid_criteria_count"] * 100.0
            if section["valid_criteria_count"]
            else None
        )
        section["score_status"] = "partial" if section["invalid_criteria_count"] else "complete"
        section["valid_normalized_score"] = valid_section_normalized
        section["valid_pass_rate"] = valid_section_pass_rate
        section["normalized_score"] = (
            clamp_percent((section["raw_score"] / total) * 100.0)
            if total > 0 and not section["invalid_criteria_count"]
            else None
        )
        section["pass_rate"] = (
            valid_section_pass_rate if not section["invalid_criteria_count"] else None
        )
    judge_error_count = sum(
        1 for item in judgments if item.get("error") or not isinstance(item.get("met"), bool)
    )
    return {
        "mode": "draco_criterion_judgments",
        "rubric_id": rubric_id,
        "judge_model": judge_model,
        "judge_repeats": judge_repeats,
        "rubric_criteria_count": (len(judgments) // max(1, judge_repeats) if judgments else 0),
        "criteria_count": len(judgments),
        "valid_criteria_count": len(valid_judgments),
        "invalid_criteria_count": invalid_count,
        "score_status": score_status,
        "raw_score": raw_score,
        "positive_weight_total": positive_weight_total,
        "valid_positive_weight_total": valid_positive_weight_total,
        "valid_normalized_score": valid_normalized,
        "valid_pass_rate": valid_pass_rate,
        "normalized_score": normalized if score_status == "complete" else None,
        "pass_rate": valid_pass_rate if score_status == "complete" else None,
        "section_scores": section_scores,
        "criterion_judgments": judgments,
        "judge_error_count": judge_error_count,
        "total": normalized if score_status == "complete" else None,
    }


def quality_total(judge: dict[str, Any] | None) -> float | None:
    if not isinstance(judge, dict):
        return None
    if judge.get("mode") == "legacy_dimension_score":
        if judge.get("score_status") != "complete":
            return None
        scores = valid_legacy_judge_scores(judge)
        if scores is None:
            return None
        return clamp_percent(sum(scores.values()) / 20.0 * 100.0)
    if judge.get("mode") == "draco_criterion_judgments" and judge.get("score_status") != "complete":
        return None
    normalized = judge.get("normalized_score")
    if isinstance(normalized, int | float):
        return clamp_percent(float(normalized))
    total = judge.get("total")
    if isinstance(total, int | float):
        if judge.get("mode") == "legacy_dimension_score":
            total_float = float(total)
            normalized_total = total_float if total_float > 20.0 else total_float / 20.0 * 100.0
            return clamp_percent(normalized_total)
        return float(total)
    return None


async def run_one(
    *,
    task: dict[str, Any],
    group: str,
    config: GatewayConfig,
    inherited: ProviderConfig,
    dry_run: bool,
    judge_provider: Any | None,
    judge_candidates: bool,
    judge_repeats: int,
    judge_concurrency: int,
    judge_max_attempts: int,
    judge_semaphore: asyncio.Semaphore | None,
    timeout: float,
    ensemble_proposer_timeout: float | None,
    ensemble_aggregator_timeout: float | None,
    ensemble_proposer_early_stop_success_count: int | None,
    ensemble_proposer_early_stop_after: float | None,
    expand_ensemble_timeouts_to_task_timeout: bool,
    tool_policy: dict[str, Any],
    generation_policy: dict[str, Any],
    experiment_config: DracoExperimentConfig | None = None,
    runner_mode: str = RUNNER_MODE_PROVIDER,
    output_dir: Path | None = None,
    agent_max_iterations: int = DEFAULT_AGENT_MAX_ITERATIONS,
    agent_finalization_policy: Mapping[str, Any] | None = None,
    generation_max_attempts: int = GENERATION_MAX_ATTEMPTS,
    generation_attempt_offset: int = 0,
    generation_retry_backoff: float = DEFAULT_GENERATION_RETRY_BACKOFF_SECONDS,
    tools: list[ToolDefinition] | None = None,
    run_compatibility_fingerprint: str = "",
    g1_registry_contract: Mapping[str, Any] | None = None,
    frozen_g1_lifecycle: Mapping[str, Any] | None = None,
    require_openrouter_non_byok: bool = False,
) -> dict[str, Any]:
    spec = GROUP_SPECS[group]
    audit_provider_routing = _openrouter_audit_provider_routing(
        inherited.provider_routing,
        g1_registry_contract if group == "G1" else None,
    )
    started = time.time()
    if (
        not isinstance(generation_attempt_offset, int)
        or isinstance(generation_attempt_offset, bool)
        or not 0 <= generation_attempt_offset < GENERATION_MAX_ATTEMPTS
    ):
        raise ValueError("generation attempt offset must be an integer within the formal budget")
    provider = None
    build: ProviderBuildResult | None = None
    effective_prompt = str(task["prompt"])
    provider_error = ""
    failed_build_setup_latency_ms = 0
    failed_build_setup_usage: list[dict[str, Any]] = []
    failed_build_routing_trace: dict[str, Any] = {}
    frozen_build_routing_trace: dict[str, Any] = {}
    generation_attempt_limit = min(
        bounded_generation_attempts(generation_max_attempts),
        GENERATION_MAX_ATTEMPTS - generation_attempt_offset,
    )
    generation_retry_backoff_s = bounded_generation_retry_backoff(generation_retry_backoff)
    finalization_policy = normalized_agent_finalization_policy(agent_finalization_policy)
    effective_timeout = group_timeout_seconds(
        requested_timeout=timeout,
        config=config,
        group=group,
        ensemble_proposer_timeout=ensemble_proposer_timeout,
        ensemble_aggregator_timeout=ensemble_aggregator_timeout,
    )
    generation_config = generation_chat_config(
        generation_policy,
        model=spec["model"] if spec["kind"] == "single" else None,
        tool_choice=tool_policy.get("openrouter_fusion_tool_choice"),
    )
    try:
        build = await build_experiment_provider(
            config=config,
            inherited=inherited,
            group=group,
            prompt=effective_prompt,
            dry_run=dry_run,
            enable_proposer_tools=bool(
                tool_policy.get("tools_enabled")
                and tool_policy.get("tool_mode") != TOOL_MODE_LOCAL_WEB_TOOLS
            ),
            ensemble_proposer_timeout=ensemble_proposer_timeout,
            ensemble_aggregator_timeout=ensemble_aggregator_timeout,
            experiment_config=experiment_config,
            g1_registry_contract=g1_registry_contract,
            generation_policy=generation_policy,
            tools=tools,
            frozen_g1_lifecycle=frozen_g1_lifecycle,
        )
        candidate_prompt = build.prompt
        safe_routing_trace = json_safe(build.routing_trace)
        if not isinstance(safe_routing_trace, Mapping):
            raise TypeError("provider routing trace must serialize to an object")
        candidate_routing_trace = json.loads(
            json.dumps(dict(safe_routing_trace), ensure_ascii=False)
        )
        candidate_generation_config = with_openrouter_model_capabilities(
            generation_config,
            str(
                build.routing_trace.get("fallback_model")
                or build.routing_trace.get("applied_model")
                or build.routing_trace.get("routed_model")
                or build.routing_trace.get("model")
                or ""
            ),
        )
        effective_prompt = candidate_prompt
        frozen_build_routing_trace = candidate_routing_trace
        generation_config = candidate_generation_config
        provider = build.provider
    except Exception as exc:  # noqa: BLE001 - report config errors per row
        provider = None
        if isinstance(exc, ProviderBuildError):
            provider_error = str(exc)
            failed_build_setup_latency_ms = exc.setup_latency_ms
            failed_build_setup_usage = list(exc.setup_usage)
            failed_build_routing_trace = dict(exc.routing_trace)
        elif build is not None and build.setup_usage:
            provider_error = (
                "provider_build_failed_after_setup:"
                + type(exc).__name__
            )
            failed_build_setup_latency_ms = build.setup_latency_ms
            failed_build_setup_usage = list(build.setup_usage)
            failed_build_routing_trace = (
                safe_provider_build_routing_trace(build.routing_trace)
            )
        else:
            provider_error = (
                "provider_build_failed:" + type(exc).__name__
            )
    expected_generation_model = ""
    if spec.get("kind") == "single":
        expected_generation_model = str(spec.get("model") or "").strip()
    elif spec.get("kind") == "router_single":
        expected_generation_model = str(
            frozen_build_routing_trace.get("applied_model")
            or frozen_build_routing_trace.get("fallback_model")
            or ""
        ).strip()
    expected_generation_provider = (
        "dry"
        if dry_run and spec.get("kind") in {"single", "router_single"}
        else str(inherited.provider or "").strip()
    )
    terminal_generation_reason_override = ""
    if provider is not None:
        paid_attempt_sink: dict[str, Any] = {}
        try:
            (
                run,
                generation_attempts,
                selected_generation_attempt_index,
            ) = await collect_generation_with_retries(
                provider,
                effective_prompt,
                timeout=effective_timeout,
                config=generation_config,
                tools=tools,
                runner_mode=runner_mode,
                tool_policy=tool_policy,
                task_id=str(task["id"]),
                group=group,
                output_dir=output_dir,
                agent_max_iterations=agent_max_iterations,
                finalization_policy=finalization_policy,
                max_attempts=generation_attempt_limit,
                attempt_offset=generation_attempt_offset,
                retry_backoff_seconds=generation_retry_backoff_s,
                expected_model=expected_generation_model,
                expected_provider=expected_generation_provider,
                expected_g1_registry_contract=g1_registry_contract,
                paid_attempt_sink=paid_attempt_sink,
            )
        except Exception as exc:  # noqa: BLE001 - commit a returned paid call
            if not paid_attempt_sink:
                raise
            (
                run,
                generation_attempts,
                selected_generation_attempt_index,
                terminal_generation_reason_override,
            ) = recover_paid_generation_postprocessing_failure(
                paid_attempt_sink,
                exc,
            )
            recovered_provider = paid_attempt_sink.get(
                "current_provider"
            )
            if recovered_provider is not None:
                provider = recovered_provider
        provider = getattr(
            provider,
            "_draco_selected_retry_provider",
            provider,
        )
    else:
        run = RunResult(
            final_text="",
            done=None,
            error=provider_error,
            setup_latency_ms=failed_build_setup_latency_ms,
            setup_usage=failed_build_setup_usage,
            routing_trace=failed_build_routing_trace,
            trace_events=[
                {
                    "seq": 1,
                    "elapsed_ms": failed_build_setup_latency_ms,
                    "kind": "error",
                    "code": "provider_build_failed_after_setup",
                    "request_started": False,
                    "physical_request_count": 0,
                }
            ]
            if failed_build_setup_usage
            else [],
        )
        generation_attempts = (
            [
                {
                    "attempt_id": uuid.uuid4().hex,
                    "attempt_kind": "provider_build_after_paid_setup",
                    "attempt": generation_attempt_offset + 1,
                    "started_at": started,
                    "completed_at": time.time(),
                    "retryable": True,
                    "retry_reason": provider_error,
                    "retry_suppressed_reason": "",
                    "will_retry": False,
                    "retry_backoff_s": 0.0,
                    "run": run_result_summary(run),
                }
            ]
            if failed_build_setup_usage
            else []
        )
        selected_generation_attempt_index = 0
    if terminal_generation_reason_override:
        terminal_generation_reason = (
            terminal_generation_reason_override
        )
    else:
        try:
            terminal_generation_reason = generation_retry_reason(
                run,
                expected_selection_mode=(
                    str(spec.get("selection_mode") or "")
                    if spec.get("kind") == "selection_mode"
                    else ""
                ),
                expected_selection_plan=(
                    dict(getattr(provider, "selection_plan", {}) or {})
                    if provider is not None
                    and spec.get("kind") == "selection_mode"
                    else {}
                ),
                expected_g1_registry_contract=g1_registry_contract,
                expected_model=expected_generation_model,
                expected_provider=expected_generation_provider,
            )
        except Exception as exc:  # noqa: BLE001 - the paid attempt is committed
            pending = {
                "result": run,
                "attempts": generation_attempts,
                "attempt_id": (
                    generation_attempts[-1].get("attempt_id")
                    if generation_attempts
                    and isinstance(generation_attempts[-1], Mapping)
                    else uuid.uuid4().hex
                ),
                "attempt_index": (
                    generation_attempts[-1].get("attempt")
                    if generation_attempts
                    and isinstance(generation_attempts[-1], Mapping)
                    else generation_attempt_offset + 1
                ),
                "attempt_started_at": started,
                "expected_provider": expected_generation_provider,
                "expected_model": expected_generation_model,
                "current_provider": provider,
                "stage": "terminal_generation_validation",
            }
            (
                run,
                generation_attempts,
                selected_generation_attempt_index,
                terminal_generation_reason,
            ) = recover_paid_generation_postprocessing_failure(
                pending,
                exc,
            )
    mark_retryable_generation_error(run, terminal_generation_reason)
    generation_accepted = not bool(terminal_generation_reason)
    generation_completed_at = time.time()
    usage_payload = run_result_usage_payload(run)
    generation_non_byok_audit: dict[str, Any] | None = None
    generation_non_byok_policy_violation = False
    generation_non_byok_metadata_incomplete = False
    if require_openrouter_non_byok and generation_accepted:
        generation_non_byok_audit = openrouter_non_byok_audit(
            {
                "llm_request_count": (
                    llm_request_count_for_run(
                        spec=spec,
                        done=run.done,
                        provider_attempted=provider is not None,
                    )
                    + usage_rows_request_count(run.setup_usage)
                ),
                "usage": usage_payload,
                "execution": {"generation_attempts": generation_attempts},
                "judge": None,
                "candidate_judges": [],
                "tool_policy": tool_policy,
            },
            provider_routing=audit_provider_routing,
        )
        if not generation_non_byok_audit["pass"]:
            generation_non_byok_policy_violation = not bool(
                generation_non_byok_audit.get("policy_safe_to_continue")
            )
            generation_non_byok_metadata_incomplete = not (generation_non_byok_policy_violation)
    profile_proposer_timeout_s = getattr(provider, "proposer_timeout_seconds", None)
    profile_aggregator_timeout_s = getattr(provider, "aggregator_timeout_seconds", None)
    profile_min_successful_proposers = getattr(
        provider,
        "min_successful_proposers",
        None,
    )
    profile_quorum_grace_s = getattr(provider, "quorum_grace_seconds", None)
    profile_candidate_max_chars = getattr(provider, "candidate_max_chars", None)
    profile_proposer_tools = getattr(provider, "proposer_tools", None)
    profile_aggregator_tools = getattr(provider, "aggregator_tools", None)
    profile_aggregator_recovery_mode = getattr(
        provider,
        "aggregator_recovery_mode",
        None,
    )
    profile_aggregator_recovery_top_k = getattr(
        provider,
        "aggregator_recovery_top_k",
        None,
    )
    profile_aggregator_max_tokens_cap = getattr(
        provider,
        "aggregator_max_tokens_cap",
        None,
    )
    profile_aggregator_visible_answer_reserve_tokens = getattr(
        provider,
        "aggregator_visible_answer_reserve_tokens",
        None,
    )
    profile_wait_for_all_proposers = (
        profile_quorum_grace_s is not None and float(profile_quorum_grace_s) <= 0
    )
    profile_proposer_early_stop_success_count = getattr(
        provider,
        "proposer_early_stop_success_count",
        None,
    )
    profile_proposer_early_stop_after_s = getattr(
        provider,
        "proposer_early_stop_after_seconds",
        None,
    )
    should_judge = (
        generation_accepted and not generation_non_byok_policy_violation and run.done is not None
    )
    judge = (
        await judge_text(
            judge_provider=judge_provider,
            task=task,
            answer=run.final_text,
            dry_run=dry_run and judge_provider is not None,
            judge_repeats=judge_repeats,
            judge_concurrency=judge_concurrency,
            judge_max_attempts=judge_max_attempts,
            judge_semaphore=judge_semaphore,
        )
        if should_judge
        else None
    )
    candidate_judges: list[dict[str, Any] | None] = []
    if judge_candidates and should_judge:
        for candidate in candidate_texts(
            run.done,
            final_agent_call_only=runner_mode == RUNNER_MODE_AGENT_LOOP,
        ):
            candidate_judges.append(
                await judge_text(
                    judge_provider=judge_provider,
                    task=task,
                    answer=candidate,
                    dry_run=dry_run and judge_provider is not None,
                    judge_repeats=judge_repeats,
                    judge_concurrency=judge_concurrency,
                    judge_max_attempts=judge_max_attempts,
                    judge_semaphore=judge_semaphore,
                )
            )
    fused_total = quality_total(judge)
    candidate_totals = [
        total for total in (quality_total(item) for item in candidate_judges) if total is not None
    ]
    completed_at = time.time()
    final_text_sha = text_sha256(run.final_text)
    prompt_sha = text_sha256(str(task["prompt"]))
    selected_server_tool_call_count = coerce_metric_int(usage_payload.get("server_tool_call_count"))
    server_tool_use = usage_payload.get("server_tool_use") or {}
    selected_total_tool_call_count = run.tool_call_count + selected_server_tool_call_count
    selected_llm_request_count = llm_request_count_for_run(
        spec=spec,
        done=run.done,
        provider_attempted=provider is not None,
    ) + usage_rows_request_count(run.setup_usage)
    ensemble_trace = run.done.ensemble_trace if run.done is not None else {}
    selected_usage_unknown_count = max(
        ensemble_usage_unknown_count(ensemble_trace),
        usage_unknown_count_from_usage_payload(usage_payload),
    )
    selected_trajectory_steps = selected_total_tool_call_count + selected_llm_request_count
    selected_billed_cost = float(usage_payload.get("billed_cost") or 0.0)
    attempt_stream_tool_call_count = sum_generation_attempt_metric(
        generation_attempts, "stream_tool_call_count"
    )
    attempt_server_tool_call_count = sum_generation_attempt_metric(
        generation_attempts, "server_tool_call_count"
    )
    attempt_total_tool_call_count = sum_generation_attempt_metric(
        generation_attempts, "total_tool_call_count"
    )
    attempt_llm_request_count = sum_generation_attempt_metric(
        generation_attempts, "llm_request_count"
    )
    attempt_usage_unknown_count = sum_generation_attempt_metric(
        generation_attempts, "usage_unknown_count"
    )
    attempt_trajectory_steps = sum_generation_attempt_metric(
        generation_attempts, "trajectory_steps"
    )
    attempt_latency_ms = sum_generation_attempt_metric(generation_attempts, "latency_ms")
    attempt_server_tool_use = sum_generation_attempt_server_tools(generation_attempts)
    generation_attempt_total_billed_cost = sum_generation_attempt_billed_cost(generation_attempts)
    if not generation_attempts:
        generation_attempt_total_billed_cost = float(usage_payload.get("billed_cost") or 0.0)
    generation_retry_reasons = [
        str(attempt.get("retry_reason") or "")
        for attempt in generation_attempts
        if attempt.get("retry_reason")
    ]
    actual_latency_ms = attempt_latency_ms if generation_attempts else run.latency_ms
    actual_stream_tool_call_count = (
        attempt_stream_tool_call_count if generation_attempts else run.tool_call_count
    )
    actual_server_tool_call_count = (
        attempt_server_tool_call_count if generation_attempts else selected_server_tool_call_count
    )
    actual_server_tool_use = attempt_server_tool_use if generation_attempts else server_tool_use
    actual_total_tool_call_count = (
        attempt_total_tool_call_count if generation_attempts else selected_total_tool_call_count
    )
    actual_llm_request_count = (
        attempt_llm_request_count if generation_attempts else selected_llm_request_count
    )
    actual_trajectory_steps = (
        attempt_trajectory_steps
        if generation_attempts
        else actual_total_tool_call_count + actual_llm_request_count
    )
    actual_usage_unknown_count = (
        attempt_usage_unknown_count if generation_attempts else selected_usage_unknown_count
    )
    selected_generation_succeeded = generation_accepted
    if not selected_generation_succeeded:
        selected_generation_attempt_index = 0
        selected_server_tool_call_count = 0
        server_tool_use = {}
        selected_total_tool_call_count = 0
        selected_llm_request_count = 0
        selected_usage_unknown_count = 0
        selected_trajectory_steps = 0
        selected_billed_cost = 0.0
    selected_attempt_metrics = {
        "latency_ms": run.latency_ms if selected_generation_succeeded else 0,
        "stream_tool_call_count": (run.tool_call_count if selected_generation_succeeded else 0),
        "server_tool_call_count": selected_server_tool_call_count,
        "server_tool_use": server_tool_use,
        "total_tool_call_count": selected_total_tool_call_count,
        "trajectory_steps": selected_trajectory_steps,
        "llm_request_count": selected_llm_request_count,
        "usage_unknown_count": selected_usage_unknown_count,
        "billed_cost_usd": selected_billed_cost,
        "generation_attempt": selected_generation_attempt_index,
    }
    actual_spend_metrics = {
        "latency_ms": actual_latency_ms,
        "stream_tool_call_count": actual_stream_tool_call_count,
        "server_tool_call_count": actual_server_tool_call_count,
        "server_tool_use": actual_server_tool_use,
        "total_tool_call_count": actual_total_tool_call_count,
        "trajectory_steps": actual_trajectory_steps,
        "llm_request_count": actual_llm_request_count,
        "usage_unknown_count": actual_usage_unknown_count,
        "billed_cost_usd": generation_attempt_total_billed_cost,
        "generation_attempt_count": len(generation_attempts),
    }
    row = {
        "task_id": task["id"],
        "group": group,
        "domain": task.get("domain", ""),
        "prompt": task["prompt"],
        "prompt_sha256": prompt_sha,
        "task_input_sha256": canonical_json_sha256(task),
        "run_compatibility_fingerprint": run_compatibility_fingerprint,
        "metadata": task.get("metadata", {}),
        "provider_spec": dict(spec),
        "routing_trace": run.routing_trace or frozen_build_routing_trace,
        "runner_mode": runner_mode,
        "agent_finalization_policy": dict(finalization_policy),
        "tools_enabled": bool(tool_policy.get("tools_enabled")),
        "tool_policy": tool_policy,
        "generation_policy": generation_policy,
        "generation_config": compact_chat_config(generation_config, generation_policy),
        "contamination_blocked_domains": (tool_policy.get("contamination_blocked_domains") or []),
        "started_at": started,
        "generation_completed_at": generation_completed_at,
        "completed_at": completed_at,
        "total_elapsed_ms": int((completed_at - started) * 1000),
        "latency_ms": run.latency_ms,
        "ttft_ms": run.ttft_ms,
        "tool_call_count": run.tool_call_count,
        "stream_tool_call_count": run.tool_call_count,
        "server_tool_call_count": selected_server_tool_call_count,
        "server_tool_use": server_tool_use,
        "total_tool_call_count": selected_total_tool_call_count,
        "trajectory_steps": selected_trajectory_steps,
        "llm_request_count": selected_llm_request_count,
        "usage_unknown_count": selected_usage_unknown_count,
        "selected_attempt_metrics": selected_attempt_metrics,
        "selected_generation_succeeded": selected_generation_succeeded,
        "actual_spend_metrics": actual_spend_metrics,
        "selected_attempt_billed_cost_usd": selected_billed_cost,
        "actual_spend_billed_cost_usd": generation_attempt_total_billed_cost,
        "generation_attempt_count": len(generation_attempts),
        "generation_attempt_evidence_schema": GENERATION_ATTEMPT_EVIDENCE_SCHEMA,
        "generation_attempt_budget_limit": GENERATION_MAX_ATTEMPTS,
        "generation_attempt_budget_used": (generation_attempt_offset + len(generation_attempts)),
        "generation_max_attempts": generation_attempt_limit,
        "generation_retry_backoff_s": generation_retry_backoff_s,
        "generation_attempt_total_billed_cost": generation_attempt_total_billed_cost,
        "generation_retry_reasons": generation_retry_reasons,
        "error": (
            run.error
            or (
                "openrouter_non_byok_policy_violation"
                if generation_non_byok_policy_violation
                else "openrouter_non_byok_metadata_incomplete"
                if generation_non_byok_metadata_incomplete
                else None
            )
        ),
        "final_text": run.final_text,
        "final_text_chars": len(run.final_text),
        "final_text_sha256": final_text_sha,
        "execution": {
            "provider_error": provider_error,
            "run_error": run.error,
            "judge_skipped_reason": (
                "openrouter_non_byok_policy_violation"
                if generation_non_byok_policy_violation
                else ("run_not_done" if not should_judge else "")
            ),
            "requested_timeout_s": timeout,
            "effective_timeout_s": effective_timeout,
            "profile_proposer_timeout_s": profile_proposer_timeout_s,
            "profile_aggregator_timeout_s": profile_aggregator_timeout_s,
            "profile_min_successful_proposers": profile_min_successful_proposers,
            "profile_quorum_grace_s": profile_quorum_grace_s,
            "profile_wait_for_all_proposers": profile_wait_for_all_proposers,
            "profile_candidate_max_chars": profile_candidate_max_chars,
            "profile_proposer_tools": profile_proposer_tools,
            "profile_aggregator_tools": profile_aggregator_tools,
            "profile_aggregator_recovery_mode": (profile_aggregator_recovery_mode),
            "profile_aggregator_recovery_top_k": (profile_aggregator_recovery_top_k),
            "profile_aggregator_max_tokens_cap": (profile_aggregator_max_tokens_cap),
            "profile_aggregator_visible_answer_reserve_tokens": (
                profile_aggregator_visible_answer_reserve_tokens
            ),
            "profile_proposer_early_stop_success_count": (
                profile_proposer_early_stop_success_count
            ),
            "profile_proposer_early_stop_after_s": profile_proposer_early_stop_after_s,
            "runner_mode": runner_mode,
            "agent_max_iterations": agent_max_iterations,
            "agent_finalization_policy": dict(finalization_policy),
            "latency_ms": run.latency_ms,
            "selected_generation_latency_ms": run.latency_ms,
            "selected_attempt_metrics": selected_attempt_metrics,
            "actual_spend_metrics": actual_spend_metrics,
            "routing_setup_latency_ms": run.setup_latency_ms,
            "routing_trace": run.routing_trace or frozen_build_routing_trace,
            "ttft_ms": run.ttft_ms,
            "total_elapsed_ms": int((completed_at - started) * 1000),
            "tool_call_count": run.tool_call_count,
            "stream_tool_call_count": run.tool_call_count,
            "server_tool_call_count": selected_server_tool_call_count,
            "server_tool_use": server_tool_use,
            "total_tool_call_count": selected_total_tool_call_count,
            "trajectory_steps": selected_trajectory_steps,
            "llm_request_count": selected_llm_request_count,
            "usage_unknown_count": selected_usage_unknown_count,
            "generation_attempt_count": len(generation_attempts),
            "generation_max_attempts": generation_attempt_limit,
            "generation_retry_backoff_s": generation_retry_backoff_s,
            "selected_generation_attempt": selected_generation_attempt_index,
            "generation_retry_reasons": generation_retry_reasons,
            "generation_attempt_total_billed_cost": generation_attempt_total_billed_cost,
            "generation_attempts": generation_attempts,
            "prior_generation_attempts_used": generation_attempt_offset,
            "metadata_repair_attempted": False,
            "generation_attempt_budget_remaining": max(
                0,
                GENERATION_MAX_ATTEMPTS - generation_attempt_offset - len(generation_attempts),
            ),
        },
        "run_trace": {
            "event_count": len(run.trace_events),
            "events": run.trace_events,
        },
        "usage": usage_payload,
        "ensemble_trace": ensemble_trace,
        "judge": judge,
        "candidate_judges": candidate_judges,
        "quality_total": fused_total,
        "fusion_delta": (
            fused_total - max(candidate_totals)
            if fused_total is not None and candidate_totals
            else None
        ),
    }
    if generation_non_byok_audit is not None:
        row["openrouter_non_byok_audit"] = generation_non_byok_audit
    repair_row_cost_metadata_with_estimates(row)
    row["cost_accounting"] = public_cost_accounting(row_cost_accounting(row))
    final_non_byok_audit: dict[str, Any] | None = None
    if require_openrouter_non_byok:
        final_non_byok_audit = openrouter_non_byok_audit(
            row,
            provider_routing=audit_provider_routing,
        )
        row["openrouter_non_byok_audit"] = final_non_byok_audit
    judge_reasons = judge_completion_reasons(
        row,
        judge_required=judge_provider is not None,
    )
    cost_metadata_complete = bool(row["cost_accounting"].get("actual_llm_cost_complete"))
    if require_openrouter_non_byok:
        cost_metadata_complete = bool(
            cost_metadata_complete
            and isinstance(final_non_byok_audit, Mapping)
            and final_non_byok_audit.get("pass") is True
        )
    final_non_byok_policy_violation = bool(
        require_openrouter_non_byok
        and isinstance(final_non_byok_audit, Mapping)
        and final_non_byok_audit.get("policy_safe_to_continue") is False
    )
    if final_non_byok_policy_violation:
        prior_error = str(row.get("error") or "")
        if prior_error and prior_error != "openrouter_non_byok_policy_violation":
            execution = dict(row.get("execution") or {})
            execution["prior_error_before_non_byok_policy_violation"] = prior_error
            row["execution"] = execution
        row["error"] = "openrouter_non_byok_policy_violation"
    if generation_accepted and not row.get("error"):
        if not cost_metadata_complete:
            if (
                require_openrouter_non_byok
                and isinstance(final_non_byok_audit, Mapping)
                and final_non_byok_audit.get("pass") is not True
            ):
                row["error"] = (
                    "openrouter_non_byok_metadata_incomplete"
                    if final_non_byok_audit.get("policy_safe_to_continue") is True
                    else "openrouter_non_byok_policy_violation"
                )
            else:
                row["error"] = "cost_metadata_incomplete"
        elif judge_reasons:
            row["error"] = (
                JUDGE_ATTEMPT_BUDGET_EXHAUSTED_ERROR
                if isinstance(judge, Mapping)
                and judge.get("judge_attempt_budget_exhausted") is True
                else "judge_incomplete"
            )
    row["completion_status"] = {
        "generation_accepted": generation_accepted,
        "cost_metadata_complete": cost_metadata_complete,
        "cost_metadata_scope": "actual_llm_spend",
        "all_provider_cost_complete": bool(
            row["cost_accounting"].get("actual_spend_cost_complete")
        ),
        "actual_external_tool_cost_complete": bool(
            row["cost_accounting"].get("actual_external_cost_complete")
        ),
        "judge_complete": not judge_reasons,
        "status": (
            "complete"
            if generation_accepted and cost_metadata_complete and not judge_reasons
            else "incomplete"
        ),
        "incomplete_reasons": [
            *([] if generation_accepted else [terminal_generation_reason]),
            *([] if cost_metadata_complete else ["cost_metadata_incomplete"]),
            *judge_reasons,
        ],
    }
    return row


def group_timeout_seconds(
    *,
    requested_timeout: float,
    config: GatewayConfig,
    group: str,
    ensemble_proposer_timeout: float | None = None,
    ensemble_aggregator_timeout: float | None = None,
) -> float:
    if requested_timeout <= 0:
        return requested_timeout
    spec = GROUP_SPECS[group]
    if spec["kind"] != "profile":
        return requested_timeout
    profile = config.llm_ensemble.profiles.get(spec["profile"])
    if profile is None:
        return requested_timeout
    proposer_timeout_s, aggregator_timeout_s = profile_timeout_seconds(
        profile,
        proposer_timeout_override=ensemble_proposer_timeout,
        aggregator_timeout_override=ensemble_aggregator_timeout,
    )
    moa_layers = max(1, int(getattr(profile, "moa_layers", 1) or 1))
    profile_budget = (
        proposer_timeout_s + aggregator_timeout_s * moa_layers + PROFILE_TIMEOUT_MARGIN_SECONDS
    )
    return max(float(requested_timeout), profile_budget)


def trace_row(row: dict[str, Any]) -> dict[str, Any]:
    return trace_row_from_result(row)


def percentile(values: list[int], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((pct / 100.0) * (len(ordered) - 1))))
    return float(ordered[index])


def numeric_pct_delta(value: Any, baseline: Any) -> float | None:
    if isinstance(value, int | float) and isinstance(baseline, int | float):
        baseline_float = float(baseline)
        if baseline_float == 0.0:
            return None
        return (float(value) - baseline_float) / baseline_float * 100.0
    return None


def row_metric_int(row: dict[str, Any], key: str, fallback_key: str | None = None) -> int:
    value = row.get(key)
    if value is None and fallback_key is not None:
        value = row.get(fallback_key)
    return coerce_metric_int(value)


def row_generation_attempt_usage_total(row: dict[str, Any], key: str) -> float | None:
    execution = row.get("execution") or {}
    attempts = execution.get("generation_attempts")
    if not isinstance(attempts, list) or not attempts:
        return None
    total = 0.0
    observed_usage = False
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        run = attempt.get("run")
        if not isinstance(run, dict):
            continue
        usage = run.get("usage")
        if not isinstance(usage, dict):
            continue
        value = usage.get(key)
        if isinstance(value, int | float):
            total += float(value)
            observed_usage = True
    return total if observed_usage else 0.0


def row_usage_number(row: dict[str, Any], key: str) -> float:
    """Return usage for the selected successful generation attempt only."""

    if row.get("selected_generation_succeeded") is False:
        return 0.0
    usage = row.get("usage") or {}
    if isinstance(usage, dict):
        value = usage.get(key)
        if isinstance(value, int | float):
            return float(value)
    return 0.0


def row_actual_spend_usage_number(row: dict[str, Any], key: str) -> float:
    """Return usage charged across every generation attempt."""

    attempt_total = row_generation_attempt_usage_total(row, key)
    if attempt_total is not None:
        return attempt_total
    usage = row.get("usage") or {}
    if isinstance(usage, dict):
        value = usage.get(key)
        if isinstance(value, int | float):
            return float(value)
    return 0.0


def row_billed_cost(row: dict[str, Any]) -> float:
    """Compatibility cost: selected successful generation attempt."""

    return row_usage_number(row, "billed_cost")


def row_actual_spend_billed_cost(row: dict[str, Any]) -> float:
    attempt_cost = row_generation_attempt_usage_total(row, "billed_cost")
    if attempt_cost is not None:
        return attempt_cost
    value = row.get("generation_attempt_total_billed_cost")
    if isinstance(value, int | float):
        return float(value)
    execution = row.get("execution") or {}
    value = execution.get("generation_attempt_total_billed_cost")
    if isinstance(value, int | float):
        return float(value)
    return row_billed_cost(row)


def _usage_token_count(usage: dict[str, Any]) -> int:
    # OpenRouter reports reasoning_tokens as a completion/output-token detail,
    # not an additional billed token bucket.  Keep it separately observable
    # without adding it to input + output a second time.
    return sum(coerce_metric_int(usage.get(key)) for key in ("input_tokens", "output_tokens"))


def _finite_nonnegative_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and math.isfinite(float(value))
        and float(value) >= 0.0
    )


def _openrouter_router_provider_metadata_is_complete(
    router_metadata: Mapping[str, Any],
) -> bool:
    """Require attested metadata for the upstream that served the response."""

    attempts = router_metadata.get("attempts")
    if isinstance(attempts, list):
        for attempt in attempts:
            if not isinstance(attempt, Mapping):
                continue
            status = attempt.get("status")
            if (
                isinstance(attempt.get("provider"), str)
                and bool(str(attempt.get("provider")).strip())
                and isinstance(attempt.get("model"), str)
                and bool(str(attempt.get("model")).strip())
                and isinstance(status, int)
                and not isinstance(status, bool)
                and 200 <= status < 300
            ):
                return True
    endpoints = router_metadata.get("endpoints")
    available = endpoints.get("available") if isinstance(endpoints, Mapping) else None
    if isinstance(available, list):
        return any(
            isinstance(endpoint, Mapping)
            and endpoint.get("selected") is True
            and isinstance(endpoint.get("provider"), str)
            and bool(str(endpoint.get("provider")).strip())
            and isinstance(endpoint.get("model"), str)
            and bool(str(endpoint.get("model")).strip())
            for endpoint in available
        )
    return False


def _normalize_openrouter_provider_identity(value: Any) -> str:
    """Normalize OpenRouter provider slugs and display names for comparison."""

    return "".join(character for character in str(value or "").casefold() if character.isalnum())


@cache
def _formal_openrouter_model_aliases() -> dict[str, frozenset[str]]:
    """Bind requested model ids to the frozen registry's serving-model aliases."""

    from opensquilla.provider.ranking_router import load_model_registry_snapshot

    aliases: dict[str, set[str]] = {
        str(model).strip().casefold(): {str(model).strip().casefold()}
        for model in OPENROUTER_DEFAULT_PROVIDER_ROUTING
        if str(model).strip()
    }
    snapshot = load_model_registry_snapshot()
    rows = snapshot.get("models")
    if not isinstance(rows, list):
        return {model: frozenset(values) for model, values in aliases.items()}
    for row in rows:
        facts = row.get("registry_facts") if isinstance(row, Mapping) else None
        if not isinstance(facts, Mapping):
            continue
        model = str(facts.get("model_id") or "").strip().casefold()
        version = str(facts.get("version") or "").strip().casefold()
        if not model:
            continue
        aliases.setdefault(model, {model})
        if version:
            aliases[model].add(version)
    return {model: frozenset(values) for model, values in aliases.items()}


def _formal_openrouter_models_equivalent(left: Any, right: Any) -> bool:
    """Treat a frozen requested model and its serving version as one identity."""

    left_model = str(left or "").strip().casefold()
    right_model = str(right or "").strip().casefold()
    if not left_model or not right_model:
        return False
    if left_model == right_model:
        return True
    for requested_model, aliases in _formal_openrouter_model_aliases().items():
        equivalence_class = {requested_model, *aliases}
        if left_model in equivalence_class and right_model in equivalence_class:
            return True
    return False


def _openrouter_router_provider_metadata_pin_state(
    unit: Mapping[str, Any],
    *,
    provider_routing: Mapping[str, str] | None = None,
) -> str:
    """Return ``exact``, ``missing``, or ``conflict`` for serving-route proof."""

    provider_usage = unit.get("provider_usage")
    if not isinstance(provider_usage, Mapping):
        return "missing"
    router_metadata = provider_usage.get("router_metadata")
    if not isinstance(router_metadata, Mapping):
        return "missing"
    routes = {
        str(model).strip().casefold(): str(provider).strip()
        for model, provider in (
            provider_routing
            if provider_routing is not None
            else OPENROUTER_DEFAULT_PROVIDER_ROUTING
        ).items()
        if str(model).strip() and str(provider).strip()
    }
    router_requested = str(router_metadata.get("requested") or "").strip().casefold()
    if not router_requested or router_requested not in routes:
        return "missing"
    request_identities = {
        str(value).strip().casefold()
        for value in (
            unit.get("requested_model"),
            provider_usage.get("requested_model"),
        )
        if str(value or "").strip().casefold() in routes
    }
    if request_identities and request_identities != {router_requested}:
        return "conflict"
    configured_provider = routes[router_requested].strip().casefold()
    auto_route = configured_provider == "auto"
    expected_provider = _normalize_openrouter_provider_identity(configured_provider)
    allowed_models = _formal_openrouter_model_aliases().get(
        router_requested,
        frozenset({router_requested}),
    )

    def endpoint_matches(endpoint: Mapping[str, Any]) -> bool:
        model_matches = str(endpoint.get("model") or "").strip().casefold() in allowed_models
        provider_identity = _normalize_openrouter_provider_identity(endpoint.get("provider"))
        return model_matches and (
            bool(provider_identity) if auto_route else provider_identity == expected_provider
        )

    successful_attempts = [
        attempt
        for attempt in (router_metadata.get("attempts") or [])
        if isinstance(attempt, Mapping)
        and isinstance(attempt.get("status"), int)
        and not isinstance(attempt.get("status"), bool)
        and 200 <= int(attempt["status"]) < 300
    ]
    if successful_attempts and any(
        not endpoint_matches(attempt) for attempt in successful_attempts
    ):
        return "conflict"
    endpoints = router_metadata.get("endpoints")
    available = endpoints.get("available") if isinstance(endpoints, Mapping) else None
    selected = (
        [
            endpoint
            for endpoint in available
            if isinstance(endpoint, Mapping) and endpoint.get("selected") is True
        ]
        if isinstance(available, list)
        else []
    )
    if selected and any(not endpoint_matches(endpoint) for endpoint in selected):
        return "conflict"
    if not successful_attempts and not selected:
        return "missing"
    return "exact"


def _openrouter_audit_provider_routing(
    provider_routing: Mapping[str, str],
    g1_registry_contract: Mapping[str, Any] | None,
) -> dict[str, str]:
    """Add auditable auto-route sentinels for unpinned registry-all candidates."""

    routes = {
        str(model).strip().casefold(): str(provider).strip()
        for model, provider in provider_routing.items()
        if str(model).strip() and str(provider).strip()
    }
    if (
        isinstance(g1_registry_contract, Mapping)
        and g1_registry_contract.get("candidate_scope") == "registry_all"
    ):
        expected_routes = g1_registry_contract.get("expected_routes")
        if isinstance(expected_routes, Mapping):
            for model, provider in expected_routes.items():
                normalized_model = str(model).strip().casefold()
                if normalized_model and str(provider).strip().casefold() == "auto":
                    routes.setdefault(normalized_model, "auto")
    return routes


def _openrouter_provider_billed_cost_is_exact(unit: Mapping[str, Any]) -> bool:
    """Preserve the non-audit OpenRouter provider-billed cost contract."""

    if str(unit.get("provider") or "").strip().casefold() != "openrouter":
        return False
    provider_usage = unit.get("provider_usage")
    if not isinstance(provider_usage, Mapping):
        return False
    router_metadata = provider_usage.get("router_metadata")
    response_ids = provider_usage.get("response_ids")
    billed_cost = unit.get("billed_cost")
    reported_cost = provider_usage.get("provider_reported_cost")
    return (
        provider_usage.get("is_byok") is False
        and isinstance(router_metadata, Mapping)
        and router_metadata.get("is_byok") is False
        and _finite_nonnegative_number(billed_cost)
        and _finite_nonnegative_number(reported_cost)
        and round(float(billed_cost) * 1_000_000_000) == round(float(reported_cost) * 1_000_000_000)
        and isinstance(response_ids, list)
        and bool(response_ids)
        and all(
            isinstance(response_id, str) and bool(response_id.strip())
            for response_id in response_ids
        )
    )


def _openrouter_non_byok_receipt_is_exact(
    unit: Mapping[str, Any],
    *,
    provider_routing: Mapping[str, str] | None = None,
) -> bool:
    """Require physical OpenRouter receipt evidence before calling cost exact."""

    if str(unit.get("provider") or "").strip().casefold() != "openrouter":
        return False
    provider_usage = unit.get("provider_usage")
    if not isinstance(provider_usage, Mapping):
        return False
    router_metadata = provider_usage.get("router_metadata")
    response_ids = provider_usage.get("response_ids")
    billed_cost = unit.get("billed_cost")
    reported_cost = provider_usage.get("provider_reported_cost")
    if (
        isinstance(billed_cost, bool)
        or not isinstance(billed_cost, int | float)
        or isinstance(reported_cost, bool)
        or not isinstance(reported_cost, int | float)
        or not _finite_nonnegative_number(billed_cost)
        or not _finite_nonnegative_number(reported_cost)
    ):
        return False
    receipt_present, receipt_status, receipt_cost = _billing_receipt_state(unit)
    if receipt_present and (receipt_status != "confirmed" or receipt_cost is None):
        return False
    exact_costs = [float(billed_cost), float(reported_cost)]
    if receipt_cost is not None:
        exact_costs.append(receipt_cost)
    return (
        provider_usage.get("is_byok") is False
        and isinstance(router_metadata, Mapping)
        and router_metadata.get("is_byok") is False
        and _openrouter_router_provider_metadata_is_complete(router_metadata)
        and _openrouter_router_provider_metadata_pin_state(
            unit,
            provider_routing=provider_routing,
        )
        == "exact"
        and len({round(cost * 1_000_000_000) for cost in exact_costs}) == 1
        and isinstance(response_ids, list)
        and bool(response_ids)
        and all(
            isinstance(response_id, str) and bool(response_id.strip())
            for response_id in response_ids
        )
    )


def _first_usage_cost(unit: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key in unit and _finite_nonnegative_number(unit.get(key)):
            return float(unit[key])
    return None


def _coerce_provider_billing_receipt(value: Any) -> ProviderBillingReceipt | None:
    """Validate the complete provider-native billing receipt schema."""

    if value is None:
        return None

    def receipt_int(raw: Any, *, nullable: bool = False) -> int | None:
        if raw is None and nullable:
            return None
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0 or raw > (1 << 63) - 1:
            raise ValueError("billing receipt nanos must be ledger-safe integers")
        return int(raw)

    if isinstance(value, ProviderBillingReceipt):
        candidate = value
    elif isinstance(value, Mapping):
        try:
            raw_fx = value.get("fx_native_per_usd_nanos")
            raw_schema = value.get("schema_version", 1)
            if (
                isinstance(raw_fx, bool)
                or not isinstance(raw_fx, int)
                or isinstance(raw_schema, bool)
                or not isinstance(raw_schema, int)
            ):
                return None
            candidate = ProviderBillingReceipt(
                currency=str(value.get("currency") or ""),
                status=str(value.get("status") or ""),  # type: ignore[arg-type]
                amount_nanos=receipt_int(value.get("amount_nanos"), nullable=True),
                usd_equivalent_nanos=receipt_int(
                    value.get("usd_equivalent_nanos"),
                    nullable=True,
                ),
                fx_native_per_usd_nanos=raw_fx,
                schema_version=raw_schema,
            )
        except (TypeError, ValueError, OverflowError):
            return None
    else:
        return None
    try:
        amount_nanos = receipt_int(candidate.amount_nanos, nullable=True)
        usd_nanos = receipt_int(candidate.usd_equivalent_nanos, nullable=True)
    except ValueError:
        return None
    if (
        not isinstance(candidate.currency, str)
        or len(candidate.currency) != 3
        or any(character < "A" or character > "Z" for character in candidate.currency)
        or candidate.status not in {"confirmed", "pending"}
        or isinstance(candidate.fx_native_per_usd_nanos, bool)
        or not isinstance(candidate.fx_native_per_usd_nanos, int)
        or candidate.fx_native_per_usd_nanos <= 0
        or candidate.fx_native_per_usd_nanos > (1 << 63) - 1
        or isinstance(candidate.schema_version, bool)
        or not isinstance(candidate.schema_version, int)
        or candidate.schema_version != 1
    ):
        return None
    if candidate.status == "confirmed" and (amount_nanos is None or usd_nanos is None):
        return None
    if candidate.status == "pending" and usd_nanos is not None:
        return None
    if candidate.status == "confirmed":
        expected_usd_nanos = (
            amount_nanos * 1_000_000_000 + candidate.fx_native_per_usd_nanos // 2
        ) // candidate.fx_native_per_usd_nanos
        if expected_usd_nanos != usd_nanos:
            return None
    return candidate


def _billing_receipt_state(
    unit: Mapping[str, Any],
) -> tuple[bool, str, float | None]:
    receipt = unit.get("billing_receipt", unit.get("billingReceipt"))
    if receipt is None:
        return False, "", None
    normalized = _coerce_provider_billing_receipt(receipt)
    if normalized is None:
        return True, "invalid", None
    if normalized.status == "confirmed":
        return (
            True,
            normalized.status,
            int(normalized.usd_equivalent_nanos or 0) / 1_000_000_000,
        )
    return True, normalized.status, None


def exact_provider_usage_cost(unit: Mapping[str, Any]) -> float | None:
    receipt_present, receipt_status, receipt_cost = _billing_receipt_state(unit)
    if receipt_present:
        return receipt_cost if receipt_status == "confirmed" else None
    source = str(unit.get("cost_source") or "none").strip().casefold()
    billed_cost = _first_usage_cost(unit, "billed_cost")
    if source == "openrouter_usage":
        return billed_cost
    if source == "provider_billed" and _openrouter_provider_billed_cost_is_exact(unit):
        return billed_cost
    return None


def trusted_provider_billed_cost(unit: Mapping[str, Any]) -> float:
    exact_cost = exact_provider_usage_cost(unit)
    if exact_cost is not None:
        return exact_cost
    receipt_present, _, _ = _billing_receipt_state(unit)
    if receipt_present:
        return 0.0
    source = str(unit.get("cost_source") or "none").strip().casefold()
    reported = _first_usage_cost(unit, "billed_cost")
    if source in {"provider_billed", "openrouter_usage"}:
        return float(reported or 0.0)
    if source in {"", "none", "unavailable"} and reported and reported > 0.0:
        return reported
    return 0.0


def _mixed_usage_cost(unit: Mapping[str, Any]) -> float | None:
    total = _first_usage_cost(unit, "cost_usd", "costUsd")
    if total is not None:
        return total
    billed = _first_usage_cost(
        unit,
        "billed_cost_usd",
        "billedCostUsd",
        "billed_cost",
    )
    estimated = _first_usage_cost(
        unit,
        "estimated_cost_usd",
        "estimatedCostUsd",
    )
    if billed is None and estimated is None:
        return None
    return (billed or 0.0) + (estimated or 0.0)


def estimate_missing_usage_costs(
    usage: Any,
    *,
    default_provider: str = "",
    default_model: str = "",
) -> bool:
    """Fill missing dollars from the frozen pricing resolver, never as exact."""

    if not isinstance(usage, dict):
        return False
    breakdown = usage.get("model_usage_breakdown")
    units = (
        [item for item in breakdown if isinstance(item, dict)]
        if isinstance(breakdown, list) and breakdown
        else [usage]
    )
    changed = False
    estimated_total = 0.0
    all_units_priced = True
    for unit in units:
        source = str(unit.get("cost_source") or "none").strip().casefold()
        exact_cost = exact_provider_usage_cost(unit)
        already_known = (
            exact_cost is not None
            or (
                source.startswith("opensquilla_")
                and _first_usage_cost(
                    unit,
                    "estimated_cost_usd",
                    "estimatedCostUsd",
                    "cost_usd",
                    "costUsd",
                )
                is not None
            )
            or (source == "mixed" and _mixed_usage_cost(unit) is not None)
        )
        if already_known:
            known_cost = (
                exact_cost
                if exact_cost is not None
                else _mixed_usage_cost(unit)
                if source == "mixed"
                else _first_usage_cost(
                    unit,
                    "estimated_cost_usd",
                    "estimatedCostUsd",
                    "cost_usd",
                    "costUsd",
                )
            )
            estimated_total += float(known_cost or 0.0)
            continue
        model = str(unit.get("model") or default_model or "")
        provider = str(unit.get("provider") or default_provider or "")
        input_tokens = coerce_metric_int(unit.get("input_tokens"))
        output_tokens = coerce_metric_int(unit.get("output_tokens"))
        cached_tokens = coerce_metric_int(
            unit.get("cache_read_tokens")
            if "cache_read_tokens" in unit
            else unit.get("cached_tokens")
        )
        cache_write_tokens = coerce_metric_int(unit.get("cache_write_tokens"))
        if not model or not any((input_tokens, output_tokens, cached_tokens, cache_write_tokens)):
            all_units_priced = False
            continue
        resolved = resolve_model_price(model, provider)
        estimate = estimate_cost(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cached_tokens,
            cache_write_tokens=cache_write_tokens,
            price=resolved.entry,
        )
        unit["estimated_cost_usd"] = float(estimate.cost_usd)
        unit["cost_usd"] = float(estimate.cost_usd)
        unit["cost_source"] = "opensquilla_static_estimate"
        provider_usage = (
            dict(unit.get("provider_usage"))
            if isinstance(unit.get("provider_usage"), Mapping)
            else {}
        )
        provider_usage.update(
            {
                "cost_repair": "token_price_estimate",
                "estimate_basis": estimate.basis,
                "price_source": resolved.source,
            }
        )
        unit["provider_usage"] = provider_usage
        estimated_total += float(estimate.cost_usd)
        changed = True
    if isinstance(breakdown, list) and breakdown and all_units_priced:
        usage["estimated_cost_usd"] = estimated_total
        usage["cost_usd"] = estimated_total
        if str(usage.get("cost_source") or "none").casefold() not in {
            "provider_billed",
            "mixed",
        }:
            usage["cost_source"] = "opensquilla_static_estimate"
    return changed


def repair_row_cost_metadata_with_estimates(row: dict[str, Any]) -> bool:
    """Estimate every retained generation/Judge receipt without rerunning models."""

    provider_spec = row.get("provider_spec")
    default_provider = (
        str(provider_spec.get("provider") or "") if isinstance(provider_spec, Mapping) else ""
    )
    default_model = (
        str(provider_spec.get("model") or "") if isinstance(provider_spec, Mapping) else ""
    )
    changed = estimate_missing_usage_costs(
        row.get("usage"),
        default_provider=default_provider,
        default_model=default_model,
    )
    execution = row.get("execution")
    attempts = execution.get("generation_attempts") if isinstance(execution, Mapping) else None
    if isinstance(attempts, list):
        for attempt in attempts:
            run = attempt.get("run") if isinstance(attempt, Mapping) else None
            if isinstance(run, dict):
                changed |= estimate_missing_usage_costs(
                    run.get("usage"),
                    default_provider=default_provider,
                    default_model=default_model,
                )
    for judge in [row.get("judge"), *(row.get("candidate_judges") or [])]:
        for run in iter_judge_attempt_runs(judge):
            changed |= estimate_missing_usage_costs(run.get("usage"))
    return changed


def usage_cost_accounting(
    usage: Any,
    *,
    expected_requests: int = 0,
    scope: str,
) -> dict[str, Any]:
    usage_dict = usage if isinstance(usage, dict) else {}
    breakdown = usage_dict.get("model_usage_breakdown")
    units = (
        [item for item in breakdown if isinstance(item, dict)]
        if isinstance(breakdown, list) and breakdown
        else ([usage_dict] if usage_dict else [])
    )
    raw_observed = len(units)
    units = deduplicate_stable_usage_receipts(units)
    observed = len(units)
    request_count = max(max(0, int(expected_requests)), observed)
    counts = {"exact": 0, "estimated": 0, "mixed": 0, "unknown": 0}
    tokens = {"exact": 0, "estimated": 0, "mixed": 0, "unknown": 0}
    recorded_cost = 0.0
    for unit in units:
        source = str(unit.get("cost_source") or "none").strip().casefold()
        cost: float | None = None
        exact_cost = exact_provider_usage_cost(unit)
        if exact_cost is not None:
            cost = exact_cost
            category = "exact" if cost is not None else "unknown"
        elif source.startswith("opensquilla_"):
            cost = _first_usage_cost(
                unit,
                "estimated_cost_usd",
                "estimatedCostUsd",
                "cost_usd",
                "costUsd",
            )
            category = "estimated" if cost is not None else "unknown"
        elif source == "mixed":
            cost = _mixed_usage_cost(unit)
            category = "mixed" if cost is not None else "unknown"
        else:
            category = "unknown"
        counts[category] += 1
        tokens[category] += _usage_token_count(unit)
        if cost is not None and category != "unknown":
            recorded_cost += cost
    missing = max(0, request_count - observed)
    counts["unknown"] += missing
    known_requests = counts["exact"] + counts["estimated"] + counts["mixed"]
    total_tokens = sum(tokens.values())
    return {
        "scope": scope,
        "request_count": request_count,
        "usage_observed_request_count": observed,
        "duplicate_stable_receipt_count": raw_observed - observed,
        "exact_request_count": counts["exact"],
        "estimated_request_count": counts["estimated"],
        "mixed_request_count": counts["mixed"],
        "unknown_request_count": counts["unknown"],
        "total_tokens": total_tokens,
        "exact_tokens": tokens["exact"],
        "estimated_tokens": tokens["estimated"],
        "mixed_tokens": tokens["mixed"],
        "unknown_tokens": tokens["unknown"],
        "recorded_cost_usd": recorded_cost,
        "known_request_coverage_pct": (
            known_requests / request_count * 100.0 if request_count else 100.0
        ),
        "exact_request_coverage_pct": (
            counts["exact"] / request_count * 100.0 if request_count else 100.0
        ),
        "known_token_coverage_pct": (
            (total_tokens - tokens["unknown"]) / total_tokens * 100.0
            if total_tokens
            else (100.0 if not counts["unknown"] else 0.0)
        ),
        "cost_complete": counts["unknown"] == 0,
        "cost_exact": (
            counts["unknown"] == 0 and counts["estimated"] == 0 and counts["mixed"] == 0
        ),
        # Internal-only provenance lets nested account merges deduplicate a
        # physical response across attempts and scopes. It is stripped before
        # result rows are serialized.
        "_stable_usage_receipts": units,
        "_receipt_provenance_complete": True,
    }


def merge_cost_accounting(scope: str, accounts: list[dict[str, Any]]) -> dict[str, Any]:
    if all(
        account.get("_receipt_provenance_complete") is True
        and isinstance(account.get("_stable_usage_receipts"), list)
        for account in accounts
    ):
        combined_units = [
            unit
            for account in accounts
            for unit in account["_stable_usage_receipts"]
            if isinstance(unit, Mapping)
        ]
        merged = usage_cost_accounting(
            {"model_usage_breakdown": combined_units} if combined_units else None,
            expected_requests=sum(
                max(0, coerce_metric_int(account.get("request_count"))) for account in accounts
            ),
            scope=scope,
        )
        merged["duplicate_stable_receipt_count"] += sum(
            max(0, coerce_metric_int(account.get("duplicate_stable_receipt_count")))
            for account in accounts
        )
        return merged

    merged_numeric: dict[str, int | float] = {
        "request_count": 0,
        "usage_observed_request_count": 0,
        "duplicate_stable_receipt_count": 0,
        "exact_request_count": 0,
        "estimated_request_count": 0,
        "mixed_request_count": 0,
        "unknown_request_count": 0,
        "total_tokens": 0,
        "exact_tokens": 0,
        "estimated_tokens": 0,
        "mixed_tokens": 0,
        "unknown_tokens": 0,
        "recorded_cost_usd": 0.0,
    }
    for account in accounts:
        for key in tuple(merged_numeric):
            value = account.get(key)
            if isinstance(value, int | float):
                merged_numeric[key] += value
    requests = int(merged_numeric["request_count"])
    known_requests = requests - int(merged_numeric["unknown_request_count"])
    total_tokens = int(merged_numeric["total_tokens"])
    merged: dict[str, Any] = {"scope": scope, **merged_numeric}
    merged.update(
        {
            "known_request_coverage_pct": (
                known_requests / requests * 100.0 if requests else 100.0
            ),
            "exact_request_coverage_pct": (
                int(merged_numeric["exact_request_count"]) / requests * 100.0 if requests else 100.0
            ),
            "known_token_coverage_pct": (
                (total_tokens - int(merged_numeric["unknown_tokens"])) / total_tokens * 100.0
                if total_tokens
                else (100.0 if not merged_numeric["unknown_request_count"] else 0.0)
            ),
            "cost_complete": int(merged_numeric["unknown_request_count"]) == 0,
            "cost_exact": (
                int(merged_numeric["unknown_request_count"]) == 0
                and int(merged_numeric["estimated_request_count"]) == 0
                and int(merged_numeric["mixed_request_count"]) == 0
            ),
        }
    )
    return merged


def public_cost_accounting(value: Any) -> Any:
    """Remove internal receipt provenance before serializing result rows."""

    if isinstance(value, Mapping):
        return {
            key: public_cost_accounting(item)
            for key, item in value.items()
            if not str(key).startswith("_")
        }
    if isinstance(value, list):
        return [public_cost_accounting(item) for item in value]
    return value


def summarized_run_expected_request_count(run: Mapping[str, Any]) -> int:
    """Preserve explicit zero-request evidence while supporting legacy rows."""

    declared = coerce_metric_int(run.get("llm_request_count"))
    trace_events = run.get("trace_events")
    if isinstance(trace_events, list):
        for event in reversed(trace_events):
            if not isinstance(event, Mapping):
                continue
            physical = event.get("physical_request_count")
            if isinstance(physical, int) and not isinstance(physical, bool):
                return max(declared, max(0, physical)) if physical else 0
            if event.get("request_started") is False:
                return 0
            if str(event.get("code") or "").strip() in _NO_PHYSICAL_REQUEST_GATE_CODES:
                return 0
    # Legacy summaries do not prove whether the request started.
    return max(1, declared)


def actual_generation_spend_accounting(row: dict[str, Any]) -> dict[str, Any]:
    accounts: list[dict[str, Any]] = []
    execution = row.get("execution") or {}
    attempts = execution.get("generation_attempts") if isinstance(execution, dict) else None
    observed_attempts = 0
    if isinstance(attempts, list):
        for attempt in attempts:
            run = attempt.get("run") if isinstance(attempt, dict) else None
            if not isinstance(run, dict):
                continue
            observed_attempts += 1
            accounts.append(
                usage_cost_accounting(
                    run.get("usage"),
                    expected_requests=summarized_run_expected_request_count(run),
                    scope="generation_attempt",
                )
            )
    actual_metrics = row.get("actual_spend_metrics")
    declared_attempts = max(
        coerce_metric_int(row.get("generation_attempt_count")),
        coerce_metric_int(
            actual_metrics.get("generation_attempt_count")
            if isinstance(actual_metrics, dict)
            else 0
        ),
    )
    if declared_attempts > observed_attempts:
        accounts.append(
            usage_cost_accounting(
                None,
                expected_requests=declared_attempts - observed_attempts,
                scope="missing_generation_attempt",
            )
        )
    if not accounts:
        expected_requests = row_llm_request_count(row)
        if str(row.get("final_text") or "").strip():
            expected_requests = max(1, expected_requests)
        accounts.append(
            usage_cost_accounting(
                row.get("usage"),
                expected_requests=expected_requests,
                scope="generation_attempt",
            )
        )
    return merge_cost_accounting("actual_generation_spend", accounts)


def generation_cost_accounting(row: dict[str, Any]) -> dict[str, Any]:
    """Account for only the selected generation attempt."""

    if row.get("selected_generation_succeeded") is False:
        return usage_cost_accounting(
            None,
            expected_requests=0,
            scope="selected_generation_attempt",
        )
    return usage_cost_accounting(
        row.get("usage"),
        expected_requests=row_llm_request_count(row),
        scope="selected_generation_attempt",
    )


def iter_judge_attempt_runs(judge: Any):
    if not isinstance(judge, dict):
        return
    prior_attempts = judge.get("prior_judge_attempts")
    if isinstance(prior_attempts, list):
        for attempt in prior_attempts:
            run = attempt.get("run") if isinstance(attempt, dict) else None
            if isinstance(run, dict):
                yield run
    criteria = judge.get("criterion_judgments")
    if isinstance(criteria, list):
        for criterion in criteria:
            attempts = criterion.get("judge_attempts") if isinstance(criterion, dict) else None
            if not isinstance(attempts, list):
                continue
            for attempt in attempts:
                run = attempt.get("run") if isinstance(attempt, dict) else None
                if isinstance(run, dict):
                    yield run
        return
    attempts = judge.get("judge_attempts")
    if isinstance(attempts, list):
        for attempt in attempts:
            run = attempt.get("run") if isinstance(attempt, dict) else None
            if isinstance(run, dict):
                yield run


def judge_cost_accounting(judge: Any, *, scope: str) -> dict[str, Any]:
    if not isinstance(judge, dict):
        return merge_cost_accounting(scope, [])
    if (
        judge.get("judge_cost_exempt") is True
        or str(judge.get("judge_model") or "").casefold() == "dry-run"
    ):
        return merge_cost_accounting(scope, [])
    runs = list(iter_judge_attempt_runs(judge))
    accounts = [
        usage_cost_accounting(
            run.get("usage"),
            expected_requests=summarized_run_expected_request_count(run),
            scope=f"{scope}_attempt",
        )
        for run in runs
    ]
    declared_attempts = coerce_metric_int(judge.get("judge_attempt_count"))
    criteria = judge.get("criterion_judgments")
    if isinstance(criteria, list):
        declared_attempts = max(
            declared_attempts,
            sum(
                max(
                    1,
                    coerce_metric_int(item.get("judge_attempt_count")),
                )
                for item in criteria
                if isinstance(item, dict)
            ),
            coerce_metric_int(judge.get("criteria_count")),
        )
    elif judge.get("score_status") or judge.get("error") or judge.get("judge_run"):
        declared_attempts = max(1, declared_attempts)
    if declared_attempts > len(runs):
        accounts.append(
            usage_cost_accounting(
                None,
                expected_requests=declared_attempts - len(runs),
                scope=f"{scope}_missing_attempt",
            )
        )
    return merge_cost_accounting(scope, accounts)


def external_tool_cost_accounting(
    row: dict[str, Any],
    *,
    actual_spend: bool = False,
) -> dict[str, Any]:
    tool_policy = row.get("tool_policy") or {}
    mode = str(tool_policy.get("tool_mode") or TOOL_MODE_PROVIDER_ONLY)
    actual_metrics = row.get("actual_spend_metrics") or {}
    tool_calls = (
        coerce_metric_int(actual_metrics.get("total_tool_call_count"))
        if actual_spend and isinstance(actual_metrics, dict)
        else row_total_tool_call_count(row)
    )
    local = tool_policy.get("local_web_tools") or {}
    search = local.get("web_search") or {}
    fetch = local.get("web_fetch") or {}
    provider = str(search.get("provider") or "")
    firecrawl_allowed = bool(fetch.get("allow_firecrawl"))
    untracked_paid_path_configured = mode == TOOL_MODE_LOCAL_WEB_TOOLS and (
        provider == "brave" or firecrawl_allowed
    )
    untracked_cost_possible = untracked_paid_path_configured and tool_calls > 0
    return {
        "scope": "actual_external_tools" if actual_spend else "external_tools",
        "tool_call_count": tool_calls,
        "recorded_cost_usd": 0.0,
        "estimated_cost_usd": None,
        "potentially_unpriced_tool_call_count_upper_bound": (
            tool_calls if untracked_cost_possible else 0
        ),
        "cost_complete": not untracked_cost_possible,
        "cost_exact": not untracked_cost_possible,
        "cost_status": "unknown" if untracked_cost_possible else "exact",
        "cost_precision": "unknown" if untracked_cost_possible else "exact",
        "recorded_cost_is_lower_bound": untracked_cost_possible,
        "recorded_cost_usd_is_lower_bound": untracked_cost_possible,
        "separate_from_task_completion": True,
        "note": (
            "Brave/Firecrawl spend is not returned by the local tool API"
            if untracked_cost_possible
            else "no untracked paid local tool path configured"
        ),
    }


def row_cost_accounting(row: dict[str, Any]) -> dict[str, Any]:
    generation = generation_cost_accounting(row)
    actual_generation = actual_generation_spend_accounting(row)
    judge = judge_cost_accounting(row.get("judge"), scope="judge")
    candidate_accounts = [
        judge_cost_accounting(item, scope="candidate_judge")
        for item in (row.get("candidate_judges") or [])
    ]
    candidate_judge = merge_cost_accounting("candidate_judge", candidate_accounts)
    llm_total = merge_cost_accounting("llm_total", [generation, judge, candidate_judge])
    actual_llm_total = merge_cost_accounting(
        "actual_llm_total",
        [actual_generation, judge, candidate_judge],
    )
    external = external_tool_cost_accounting(row)
    actual_external = external_tool_cost_accounting(row, actual_spend=True)
    return {
        "generation": generation,
        "selected_generation_attempt": generation,
        "actual_generation_spend": actual_generation,
        "judge": judge,
        "candidate_judge": candidate_judge,
        "llm_total": llm_total,
        "actual_llm_total": actual_llm_total,
        "external_tools": external,
        "actual_external_tools": actual_external,
        "recorded_total_cost_usd": float(llm_total["recorded_cost_usd"])
        + float(external["recorded_cost_usd"]),
        "actual_spend_recorded_total_cost_usd": (
            float(actual_llm_total["recorded_cost_usd"])
            + float(actual_external["recorded_cost_usd"])
        ),
        "result_llm_cost_complete": bool(llm_total["cost_complete"]),
        "result_llm_cost_exact": bool(llm_total["cost_exact"]),
        "actual_llm_cost_complete": bool(actual_llm_total["cost_complete"]),
        "actual_llm_cost_exact": bool(actual_llm_total["cost_exact"]),
        "actual_spend_llm_cost_complete": bool(actual_llm_total["cost_complete"]),
        "actual_spend_llm_cost_exact": bool(actual_llm_total["cost_exact"]),
        "external_cost_complete": bool(external["cost_complete"]),
        "external_cost_exact": bool(external["cost_exact"]),
        "actual_external_cost_complete": bool(actual_external["cost_complete"]),
        "actual_external_cost_exact": bool(actual_external["cost_exact"]),
        "result_cost_complete": bool(llm_total["cost_complete"])
        and bool(external["cost_complete"]),
        "result_cost_exact": bool(llm_total["cost_exact"]) and bool(external["cost_exact"]),
        "actual_spend_cost_complete": bool(actual_llm_total["cost_complete"])
        and bool(actual_external["cost_complete"]),
        "actual_spend_cost_exact": bool(actual_llm_total["cost_exact"])
        and bool(actual_external["cost_exact"]),
        "recorded_total_cost_is_lower_bound": not (
            bool(llm_total["cost_complete"]) and bool(external["cost_complete"])
        ),
        "actual_spend_recorded_total_cost_is_lower_bound": not (
            bool(actual_llm_total["cost_complete"]) and bool(actual_external["cost_complete"])
        ),
        "scope_note": (
            "Compatibility cost fields use the selected generation attempt plus recorded "
            "Judge attempts. actual_spend_* includes every generation attempt. "
            "Lifecycle completeness is gated on actual LLM spend; unpriced local Web "
            "tools remain a separate explicit lower-bound gap. Whole-experiment spend "
            "must also retain failed/replaced shards and preflight calls."
        ),
    }


def _usage_units_for_openrouter_non_byok_audit(
    usage: Any,
) -> list[Mapping[str, Any]]:
    if not isinstance(usage, Mapping):
        return []
    if not usage:
        return []
    breakdown = usage.get("model_usage_breakdown")
    if isinstance(breakdown, list) and breakdown:
        return [item for item in breakdown if isinstance(item, Mapping)]
    return [usage]


def _actual_llm_usage_units_for_openrouter_non_byok_audit(
    row: dict[str, Any],
) -> list[Mapping[str, Any]]:
    units: list[Mapping[str, Any]] = []
    execution = row.get("execution")
    attempts = execution.get("generation_attempts") if isinstance(execution, Mapping) else None
    observed_generation_run = False
    if isinstance(attempts, list):
        for attempt in attempts:
            run = attempt.get("run") if isinstance(attempt, Mapping) else None
            if not isinstance(run, Mapping):
                continue
            observed_generation_run = True
            units.extend(_usage_units_for_openrouter_non_byok_audit(run.get("usage")))
    actual_metrics = row.get("actual_spend_metrics")
    declared_generation_attempts = max(
        coerce_metric_int(row.get("generation_attempt_count")),
        coerce_metric_int(
            actual_metrics.get("generation_attempt_count")
            if isinstance(actual_metrics, Mapping)
            else 0
        ),
    )
    if not observed_generation_run and declared_generation_attempts <= 0:
        units.extend(_usage_units_for_openrouter_non_byok_audit(row.get("usage")))
    for judge in [row.get("judge"), *(row.get("candidate_judges") or [])]:
        if not isinstance(judge, dict):
            continue
        if (
            judge.get("judge_cost_exempt") is True
            or str(judge.get("judge_model") or "").casefold() == "dry-run"
        ):
            continue
        for run in iter_judge_attempt_runs(judge):
            units.extend(_usage_units_for_openrouter_non_byok_audit(run.get("usage")))
    return deduplicate_stable_usage_receipts(units)


def _independent_openrouter_policy_evidence(
    row: dict[str, Any],
) -> list[Mapping[str, Any]]:
    """Return non-physical policy evidence without changing request counts."""

    usage_payloads: list[Mapping[str, Any]] = []
    execution = row.get("execution")
    attempts = execution.get("generation_attempts") if isinstance(execution, Mapping) else None
    observed_generation_run = False
    if isinstance(attempts, list):
        for attempt in attempts:
            run = attempt.get("run") if isinstance(attempt, Mapping) else None
            usage = run.get("usage") if isinstance(run, Mapping) else None
            if isinstance(usage, Mapping):
                observed_generation_run = True
                usage_payloads.append(usage)
    actual_metrics = row.get("actual_spend_metrics")
    declared_generation_attempts = max(
        coerce_metric_int(row.get("generation_attempt_count")),
        coerce_metric_int(
            actual_metrics.get("generation_attempt_count")
            if isinstance(actual_metrics, Mapping)
            else 0
        ),
    )
    if not observed_generation_run and declared_generation_attempts <= 0:
        usage = row.get("usage")
        if isinstance(usage, Mapping):
            usage_payloads.append(usage)
    for judge in [row.get("judge"), *(row.get("candidate_judges") or [])]:
        if not isinstance(judge, dict):
            continue
        if (
            judge.get("judge_cost_exempt") is True
            or str(judge.get("judge_model") or "").casefold() == "dry-run"
        ):
            continue
        for run in iter_judge_attempt_runs(judge):
            usage = run.get("usage")
            if isinstance(usage, Mapping):
                usage_payloads.append(usage)

    evidence: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for usage in usage_payloads:
        units = [usage]
        breakdown = usage.get("model_usage_breakdown")
        if isinstance(breakdown, list):
            units.extend(item for item in breakdown if isinstance(item, Mapping))
        for unit in units:
            provider_usage = unit.get("provider_usage")
            raw_evidence = (
                provider_usage.get(IGNORED_AGENT_DONE_POLICY_EVIDENCE_KEY)
                if isinstance(provider_usage, Mapping)
                else None
            )
            if not isinstance(raw_evidence, list):
                continue
            for item in raw_evidence:
                if not isinstance(item, Mapping):
                    continue
                fingerprint = canonical_json_sha256(item)
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                evidence.append(item)
    return evidence


def classify_openrouter_non_byok_unit(
    unit: Mapping[str, Any],
    *,
    provider_routing: Mapping[str, str] | None = None,
) -> str:
    """Classify policy evidence without mistaking missing metadata for BYOK."""

    provider = str(unit.get("provider") or "").strip().casefold()
    if provider and provider != "openrouter":
        return "conflict"
    provider_usage = unit.get("provider_usage")
    if not isinstance(provider_usage, Mapping):
        return "unverified"
    stable_receipt_evidence = provider_usage.get(STABLE_RECEIPT_EVIDENCE_KEY)
    if isinstance(stable_receipt_evidence, Mapping):
        if stable_receipt_evidence.get("receipt_conflict") is True or stable_receipt_evidence.get(
            "conflict_fields"
        ):
            return "conflict"
        evidence_providers = {
            str(value).strip().casefold()
            for value in stable_receipt_evidence.get("providers") or []
            if str(value).strip()
        }
        if any(value != "openrouter" for value in evidence_providers):
            return "conflict"
        evidence_byok_values = {
            value
            for key in (
                "usage_is_byok_values",
                "router_is_byok_values",
            )
            for value in stable_receipt_evidence.get(key) or []
            if value is True or value is False
        }
        if len(evidence_byok_values) > 1:
            return "conflict"
        if True in evidence_byok_values:
            return "explicit_byok"
    router_metadata = provider_usage.get("router_metadata")
    router_is_byok = (
        router_metadata.get("is_byok") if isinstance(router_metadata, Mapping) else None
    )
    usage_is_byok = provider_usage.get("is_byok")
    if (usage_is_byok is True and router_is_byok is False) or (
        usage_is_byok is False and router_is_byok is True
    ):
        return "conflict"
    if usage_is_byok is True or router_is_byok is True:
        return "explicit_byok"
    pin_state = _openrouter_router_provider_metadata_pin_state(
        unit,
        provider_routing=provider_routing,
    )
    if pin_state == "conflict":
        return "conflict"
    if _openrouter_non_byok_receipt_is_exact(
        unit,
        provider_routing=provider_routing,
    ):
        return "exact"
    return "unverified"


def openrouter_non_byok_audit(
    row: dict[str, Any],
    *,
    provider_routing: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    # Keep this stricter audit separate from ordinary cost accounting. Native
    # receipts and legacy openrouter_usage remain valid cost evidence, but do
    # not prove the OpenRouter non-BYOK execution policy by themselves.
    accounting = row_cost_accounting(row)
    llm_total = accounting.get("actual_llm_total") or {}
    request_count = coerce_metric_int(llm_total.get("request_count"))
    units = _actual_llm_usage_units_for_openrouter_non_byok_audit(row)
    categories = {
        "exact": 0,
        "unverified": 0,
        "explicit_byok": 0,
        "conflict": 0,
    }
    for unit in units:
        category = classify_openrouter_non_byok_unit(
            unit,
            provider_routing=provider_routing,
        )
        categories[category] += 1
    independent_evidence = _independent_openrouter_policy_evidence(row)
    independent_explicit_byok_count = sum(
        1 for item in independent_evidence if item.get("classification") == "explicit_byok"
    )
    independent_conflict_count = sum(
        1 for item in independent_evidence if item.get("classification") == "conflict"
    )
    evidence_unit_count = sum(categories.values())
    missing_evidence_request_count = max(0, request_count - evidence_unit_count)
    evidence_overflow_count = max(0, evidence_unit_count - request_count)
    verified_count = min(request_count, categories["exact"])
    explicit_byok_count = categories["explicit_byok"]
    conflict_count = categories["conflict"]
    unverified_count = categories["unverified"] + missing_evidence_request_count
    policy_safe_to_continue = (
        explicit_byok_count == 0
        and conflict_count == 0
        and independent_explicit_byok_count == 0
        and independent_conflict_count == 0
        and evidence_overflow_count == 0
    )
    passed = (
        request_count > 0
        and evidence_unit_count == request_count
        and categories["exact"] == request_count
        and categories["unverified"] == 0
        and policy_safe_to_continue
    )
    return {
        "pass": passed,
        "policy": "every recorded or expected LLM request must prove is_byok=false",
        "status": (
            "exact"
            if passed
            else "metadata_incomplete"
            if policy_safe_to_continue
            else "policy_violation"
        ),
        "policy_safe_to_continue": policy_safe_to_continue,
        "request_count": request_count,
        "exact_request_count": verified_count,
        "unverified_request_count": unverified_count,
        "explicit_byok_request_count": explicit_byok_count,
        "conflict_request_count": conflict_count,
        "independent_policy_evidence_count": len(independent_evidence),
        "independent_explicit_byok_evidence_count": (independent_explicit_byok_count),
        "independent_conflict_evidence_count": independent_conflict_count,
        "unverified_or_byok_request_count": (
            max(0, request_count - verified_count) + evidence_overflow_count
        ),
        "evidence_unit_count": evidence_unit_count,
        "missing_evidence_request_count": missing_evidence_request_count,
        "evidence_overflow_count": evidence_overflow_count,
        "recorded_request_count": evidence_unit_count,
        "missing_expected_request_count": missing_evidence_request_count,
        "unexpected_recorded_request_count": evidence_overflow_count,
        "request_count_match": evidence_unit_count == request_count,
        "note": (
            "pass requires every request to carry OpenRouter provider identity, "
            "is_byok=false at usage and router levels, serving-provider metadata, "
            "a response id, and matching provider-reported cost; generic billing "
            "receipts and legacy openrouter_usage do not satisfy this policy"
        ),
    }


def row_server_tool_call_count(row: dict[str, Any]) -> int:
    if row.get("selected_generation_succeeded") is False:
        return 0
    if row.get("server_tool_call_count") is not None:
        return row_metric_int(row, "server_tool_call_count")
    usage = row.get("usage") or {}
    if isinstance(usage, dict):
        return sum(server_tool_counts_from_usage_payload(usage).values())
    return 0


def row_total_tool_call_count(row: dict[str, Any]) -> int:
    if row.get("selected_generation_succeeded") is False:
        return 0
    if row.get("total_tool_call_count") is not None:
        return row_metric_int(row, "total_tool_call_count")
    stream_count = row_metric_int(row, "stream_tool_call_count", "tool_call_count")
    return stream_count + row_server_tool_call_count(row)


def row_llm_request_count(row: dict[str, Any]) -> int:
    if row.get("selected_generation_succeeded") is False:
        return 0
    provider_spec = row.get("provider_spec") or {}
    execution = row.get("execution") or {}
    default_request_count = int(
        provider_spec.get("kind") == "single" and not execution.get("provider_error")
    )
    usage = row.get("usage")
    if isinstance(usage, Mapping) and isinstance(
        usage.get("model_usage_breakdown"),
        list,
    ):
        usage = dict(usage)
        usage["model_usage_breakdown"] = deduplicate_stable_usage_receipts(
            [
                dict(unit) if isinstance(unit, Mapping) else unit
                for unit in usage["model_usage_breakdown"]
            ]
        )
    # Row-level accounting is an audit boundary: never let a stale/undersized
    # scalar hide additional distinct receipts.  Newly persisted runs are
    # canonicalized strictly, while legacy or deliberately adversarial rows
    # are charged/audited at the larger of their declaration and evidence.
    evidence_count = derive_physical_request_count(
        {"usage": usage},
        default_request_count=default_request_count,
    )
    declared_count = (
        row_metric_int(row, "llm_request_count") if row.get("llm_request_count") is not None else 0
    )
    return max(evidence_count, declared_count)


def ensemble_usage_unknown_count(trace: Any) -> int:
    if not isinstance(trace, dict):
        return 0
    direct_missing = max(
        0,
        coerce_metric_int(trace.get("usage_missing_count")),
    )
    detected_missing = 0
    calls = trace.get("calls")
    if isinstance(calls, list):
        detected_missing = sum(ensemble_usage_unknown_count(call) for call in calls)
    early_stop = trace.get("proposer_early_stop")
    if isinstance(early_stop, dict):
        detected_missing = max(
            detected_missing,
            coerce_metric_int(early_stop.get("usage_unknown_count")),
        )
    candidates = trace.get("candidates")
    if isinstance(candidates, list):
        detected_missing = max(
            detected_missing,
            sum(
                1
                for candidate in candidates
                if isinstance(candidate, dict) and candidate.get("error_code") == "early_stopped"
            ),
        )
    return max(direct_missing, detected_missing)


def usage_unknown_count_from_usage_payload(usage: Any) -> int:
    if not isinstance(usage, dict):
        return 0
    breakdown = usage.get("model_usage_breakdown")
    units = (
        [item for item in breakdown if isinstance(item, dict)]
        if isinstance(breakdown, list) and breakdown
        else [usage]
    )
    represented_missing = sum(
        1
        for item in units
        if (
            str(item.get("role") or "").strip().casefold() in MISSING_USAGE_PLACEHOLDER_ROLES
            or item.get("error_code") == "early_stopped"
            or str(item.get("cost_source") or "none") == "unknown_canceled"
        )
    )
    unknown_receipt_cost = sum(
        1
        for item in units
        if (
            str(item.get("role") or "").strip().casefold() not in MISSING_USAGE_PLACEHOLDER_ROLES
            and item.get("error_code") != "early_stopped"
            and str(item.get("cost_source") or "none") != "unknown_canceled"
            and (
                _usage_token_count(item) > 0
                and exact_provider_usage_cost(item) is None
                and str(item.get("cost_source") or "none") != "mixed"
                and not str(item.get("cost_source") or "none").startswith("opensquilla_")
            )
        )
    )
    explicit_missing = max(
        0,
        coerce_metric_int(usage.get("usage_missing_count")),
    )
    return unknown_receipt_cost + max(explicit_missing, represented_missing)


def row_generation_attempt_usage_unknown_count(row: dict[str, Any]) -> int:
    execution = row.get("execution") or {}
    attempts = execution.get("generation_attempts") if isinstance(execution, dict) else None
    if not isinstance(attempts, list):
        return 0
    total = 0
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        run = attempt.get("run")
        if not isinstance(run, dict):
            continue
        total += max(
            coerce_metric_int(run.get("usage_unknown_count")),
            usage_unknown_count_from_usage_payload(run.get("usage")),
            ensemble_usage_unknown_count(run.get("ensemble_trace")),
        )
    return total


def row_usage_unknown_count(row: dict[str, Any]) -> int:
    direct = row_metric_int(row, "usage_unknown_count")
    execution = row.get("execution") or {}
    execution_value = 0
    if isinstance(execution, dict):
        execution_value = coerce_metric_int(execution.get("usage_unknown_count"))
    return max(
        direct,
        execution_value,
        row_generation_attempt_usage_unknown_count(row),
        usage_unknown_count_from_usage_payload(row.get("usage")),
        ensemble_usage_unknown_count(row.get("ensemble_trace")),
    )


def row_trajectory_steps(row: dict[str, Any]) -> int:
    llm_requests = row_llm_request_count(row)
    tool_calls = row_total_tool_call_count(row)
    if llm_requests or tool_calls:
        return llm_requests + tool_calls
    return row_metric_int(row, "trajectory_steps")


def completed_quality_value(row: dict[str, Any]) -> float:
    if row.get("error"):
        return 0.0
    value = row.get("quality_total")
    return float(value) if isinstance(value, int | float) else 0.0


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {"groups": {}}
    judging_enabled = any(isinstance(row.get("judge"), dict) for row in rows)
    for group in sorted({row["group"] for row in rows}):
        group_rows = [row for row in rows if row["group"] == group]
        completed_rows = [row for row in group_rows if not row.get("error")]
        latencies = [int(row["latency_ms"] or 0) for row in group_rows]
        scored_totals = [
            completed_quality_value(row)
            for row in completed_rows
            if row["quality_total"] is not None
        ]
        quality_values = (
            [completed_quality_value(row) for row in group_rows] if judging_enabled else []
        )
        pass_rates = [
            float((row.get("judge") or {}).get("pass_rate"))
            for row in completed_rows
            if isinstance((row.get("judge") or {}).get("pass_rate"), int | float)
        ]
        # Recompute with the current accounting contract. Persisted rows may
        # carry an older/stale cost_accounting object and must not bypass new
        # completeness gates.
        cost_accounts = [row_cost_accounting(row) for row in group_rows]
        completed_cost_accounts = [row_cost_accounting(row) for row in completed_rows]
        generation_costs = [
            float(account["generation"]["recorded_cost_usd"]) for account in cost_accounts
        ]
        actual_generation_costs = [
            float(account["actual_generation_spend"]["recorded_cost_usd"])
            for account in cost_accounts
        ]
        judge_costs = [float(account["judge"]["recorded_cost_usd"]) for account in cost_accounts]
        candidate_judge_costs = [
            float(account["candidate_judge"]["recorded_cost_usd"]) for account in cost_accounts
        ]
        costs = [float(account["recorded_total_cost_usd"]) for account in cost_accounts]
        actual_spend_costs = [
            float(account["actual_spend_recorded_total_cost_usd"]) for account in cost_accounts
        ]
        completed_costs = [
            float(account["recorded_total_cost_usd"]) for account in completed_cost_accounts
        ]
        group_llm_cost = merge_cost_accounting(
            "group_llm_total",
            [account["llm_total"] for account in cost_accounts],
        )
        actual_group_llm_cost = merge_cost_accounting(
            "actual_group_llm_total",
            [account["actual_llm_total"] for account in cost_accounts],
        )
        visible_tokens = [
            int(row_usage_number(row, "input_tokens")) + int(row_usage_number(row, "output_tokens"))
            for row in group_rows
        ]
        reasoning_tokens = [int(row_usage_number(row, "reasoning_tokens")) for row in group_rows]
        all_tokens = list(visible_tokens)
        stream_tool_calls = [
            row_metric_int(row, "stream_tool_call_count", "tool_call_count") for row in group_rows
        ]
        server_tool_calls = [row_server_tool_call_count(row) for row in group_rows]
        total_tool_calls = [row_total_tool_call_count(row) for row in group_rows]
        trajectory_steps = [row_trajectory_steps(row) for row in group_rows]
        llm_requests = [row_llm_request_count(row) for row in group_rows]
        usage_unknown = [
            int(account["llm_total"]["unknown_request_count"]) for account in cost_accounts
        ]
        summary["groups"][group] = {
            "rows": len(group_rows),
            "task_ids": sorted(str(row.get("task_id") or "") for row in group_rows),
            "completed": len(completed_rows),
            "scored_rows": len(scored_totals),
            "score_coverage_pct": (
                len(scored_totals) / len(group_rows) * 100.0 if group_rows else 0.0
            ),
            "avg_quality": statistics.mean(quality_values) if quality_values else None,
            "avg_quality_scored": (statistics.mean(scored_totals) if scored_totals else None),
            "avg_pass_rate": statistics.mean(pass_rates) if pass_rates else None,
            "judge_errors": sum(
                int((row.get("judge") or {}).get("judge_error_count") or 0)
                for row in completed_rows
            ),
            "avg_cost_usd": statistics.mean(costs) if costs else 0.0,
            "avg_cost_completed_usd": (
                statistics.mean(completed_costs) if completed_costs else None
            ),
            "recorded_total_cost_usd": sum(costs),
            "avg_actual_spend_cost_usd": (
                statistics.mean(actual_spend_costs) if actual_spend_costs else 0.0
            ),
            "actual_spend_recorded_total_cost_usd": sum(actual_spend_costs),
            "recorded_generation_cost_usd": sum(generation_costs),
            "actual_spend_generation_cost_usd": sum(actual_generation_costs),
            "recorded_judge_cost_usd": sum(judge_costs),
            "recorded_candidate_judge_cost_usd": sum(candidate_judge_costs),
            "avg_recorded_generation_cost_usd": (
                statistics.mean(generation_costs) if generation_costs else 0.0
            ),
            "avg_actual_spend_generation_cost_usd": (
                statistics.mean(actual_generation_costs) if actual_generation_costs else 0.0
            ),
            "avg_recorded_judge_cost_usd": (statistics.mean(judge_costs) if judge_costs else 0.0),
            "avg_recorded_candidate_judge_cost_usd": (
                statistics.mean(candidate_judge_costs) if candidate_judge_costs else 0.0
            ),
            "known_cost_request_coverage_pct": group_llm_cost["known_request_coverage_pct"],
            "exact_cost_request_coverage_pct": group_llm_cost["exact_request_coverage_pct"],
            "unknown_cost_request_count": group_llm_cost["unknown_request_count"],
            "unknown_cost_tokens": group_llm_cost["unknown_tokens"],
            "llm_cost_complete_rows": sum(
                1 for account in cost_accounts if account["llm_total"]["cost_complete"]
            ),
            "result_cost_complete_rows": sum(
                1 for account in cost_accounts if account["result_cost_complete"]
            ),
            "actual_spend_known_cost_request_coverage_pct": actual_group_llm_cost[
                "known_request_coverage_pct"
            ],
            "actual_spend_unknown_cost_request_count": actual_group_llm_cost[
                "unknown_request_count"
            ],
            "actual_spend_cost_complete_rows": sum(
                1 for account in cost_accounts if account["actual_spend_cost_complete"]
            ),
            "avg_visible_tokens": (statistics.mean(visible_tokens) if visible_tokens else 0.0),
            "avg_reasoning_tokens": (
                statistics.mean(reasoning_tokens) if reasoning_tokens else 0.0
            ),
            "avg_total_tokens": statistics.mean(all_tokens) if all_tokens else 0.0,
            "avg_stream_tool_calls": (
                statistics.mean(stream_tool_calls) if stream_tool_calls else 0.0
            ),
            "avg_server_tool_calls": (
                statistics.mean(server_tool_calls) if server_tool_calls else 0.0
            ),
            "avg_tool_calls": (statistics.mean(total_tool_calls) if total_tool_calls else 0.0),
            "total_tool_calls": sum(total_tool_calls),
            "tool_call_rate_pct": (
                sum(1 for count in total_tool_calls if count > 0) / len(total_tool_calls) * 100.0
                if total_tool_calls
                else 0.0
            ),
            "avg_trajectory_steps": (
                statistics.mean(trajectory_steps) if trajectory_steps else 0.0
            ),
            "avg_llm_requests": (statistics.mean(llm_requests) if llm_requests else 0.0),
            "total_llm_requests": sum(llm_requests),
            "avg_usage_unknown": (statistics.mean(usage_unknown) if usage_unknown else 0.0),
            "total_usage_unknown": sum(usage_unknown),
            "latency_p50_ms": percentile(latencies, 50),
            "latency_p95_ms": percentile(latencies, 95),
        }
    for item in summary["groups"].values():
        for baseline in ("B0", "B1"):
            baseline_item = summary["groups"].get(baseline) or {}
            suffix = baseline.lower()
            item[f"avg_quality_pct_delta_vs_{suffix}"] = numeric_pct_delta(
                item.get("avg_quality"),
                baseline_item.get("avg_quality"),
            )
            comparable_costs = (
                item.get("result_cost_complete_rows") == item.get("rows")
                and baseline_item.get("result_cost_complete_rows") == baseline_item.get("rows")
                and item.get("completed") == item.get("rows")
                and baseline_item.get("completed") == baseline_item.get("rows")
                and item.get("task_ids") == baseline_item.get("task_ids")
            )
            item[f"avg_cost_pct_delta_vs_{suffix}"] = (
                numeric_pct_delta(
                    item.get("avg_cost_usd"),
                    baseline_item.get("avg_cost_usd"),
                )
                if comparable_costs
                else None
            )
    return summary


def render_markdown(
    summary: dict[str, Any],
    jsonl_path: Path,
    tool_policy: dict[str, Any] | None = None,
    generation_policy: dict[str, Any] | None = None,
    runner_mode: str = DEFAULT_DRACO_RUNNER_MODE,
    agent_max_iterations: int = DEFAULT_AGENT_MAX_ITERATIONS,
    agent_finalization_policy: Mapping[str, Any] | None = None,
) -> str:
    stamp = jsonl_path.stem.removeprefix("draco_ensemble_")
    trace_path = jsonl_path.parent / f"draco_run_{stamp}.trace.jsonl"
    policy = tool_policy or benchmark_tool_policy()
    generation = generation_policy or generation_thinking_policy()
    finalization = normalized_agent_finalization_policy(agent_finalization_policy)
    blocked_domains = policy.get("contamination_blocked_domains") or []
    tool_line = (
        f"Runner mode: `{runner_mode}`; tool mode: `{policy.get('tool_mode') or RUNNER_MODE}`; "
        "tools enabled: "
        f"`{str(bool(policy.get('tools_enabled'))).lower()}`"
    )
    if policy.get("tools_enabled"):
        tool_names = ", ".join(str(name) for name in policy.get("tool_names") or [])
        if tool_names:
            tool_line = f"{tool_line}; tools: `{tool_names}`."
        else:
            tool_line = f"{tool_line}."
    if not policy.get("tools_enabled"):
        tool_line = (
            f"Runner mode: `{runner_mode}`; tool mode: "
            f"`{policy.get('tool_mode') or RUNNER_MODE}`; "
            "external research tools are not attached."
        )
    group_tool_policies = policy.get("group_tool_policies") or {}
    fusion_groups = [
        str(group)
        for group, group_policy in group_tool_policies.items()
        if isinstance(group_policy, dict) and group_policy.get("openrouter_fusion_enabled")
    ]
    fusion_line = ""
    if fusion_groups:
        if not policy.get("tools_enabled"):
            tool_line = (
                f"Runner mode: `{runner_mode}`; tool mode: "
                f"`{policy.get('tool_mode') or RUNNER_MODE}`; "
                "no global external research tools are attached."
            )
        fusion_line = (
            "OpenRouter Fusion groups: "
            f"`{', '.join(sorted(fusion_groups))}` use only `openrouter:fusion` "
            "with `tool_choice=required`; Fusion's internal web_search/web_fetch "
            "domain controls are not exposed in the documented tool parameters."
        )
    generation_budget_note = f"budget: `{generation.get('thinking_budget_tokens')}`"
    if generation.get("max_thinking_budget_tokens") is not None:
        generation_budget_note = (
            f"{generation_budget_note}, "
            f"max budget: `{generation.get('max_thinking_budget_tokens')}`"
        )

    def _signed_pct(value: Any) -> str:
        return f"{float(value):+.2f}%" if isinstance(value, int | float) else ""

    lines = [
        "# DRACO Ensemble Summary",
        "",
        f"Raw JSONL: `{jsonl_path}`",
        f"Trace JSONL: `{trace_path}`",
        "",
        "Generation thinking: "
        f"`{generation.get('generation_thinking')}` "
        f"(enabled: `{generation.get('thinking_enabled')}`, "
        f"level: `{generation.get('thinking_level')}`, "
        f"{generation_budget_note}, "
        f"temperature: `{generation.get('temperature')}`).",
        f"Agent max iterations: `{agent_max_iterations}`.",
        "Agent finalization policy: "
        f"`{json.dumps(finalization, ensure_ascii=False, sort_keys=True)}`.",
        tool_line,
        *([fusion_line] if fusion_line else []),
        "Contamination blocked domains: "
        f"`{', '.join(blocked_domains) if blocked_domains else '(none)'}`.",
        "Cost accounting: selected columns contain only the accepted generation attempt "
        "plus rubric/candidate Judge attempts; actual-spend columns contain every "
        "generation attempt plus those Judge attempts. Unpriced requests remain unknown "
        "rather than being treated as $0; selected cost deltas are blank unless both "
        "groups are complete. Preflight and replaced/failed shard spend must still be "
        "audited at the whole-experiment level.",
        "",
        "| Group | Rows | Done | Avg Quality | AvgQ Scored | Avg Pass | "
        "Judge Err | Avg Selected LLM $ | Avg Selected Gen $ | Avg Actual LLM $ | "
        "Avg Actual Gen $ | Avg Judge $ | Selected Known Cost % | Actual Known Cost % | "
        "Selected Complete | Actual Complete | Avg Visible | Avg Reason | Avg Tokens | "
        "Avg Tools | Tool % | "
        "Avg Steps | Avg LLM Req | Unknown Cost Req | p50 ms | p95 ms | "
        "AvgQ % vs B0 | Avg$ % vs B0 | "
        "AvgQ % vs B1 | Avg$ % vs B1 |",
        "| --- |" + " ---: |" * 29,
    ]
    for group, item in sorted(summary["groups"].items()):
        lines.append(
            "| {group} | {rows} | {done} | {quality} | {quality_scored} | "
            "{pass_rate} | {judge_errors} | {cost:.6f} | {generation_cost:.6f} | "
            "{actual_cost:.6f} | {actual_generation_cost:.6f} | {judge_cost:.6f} | "
            "{known_cost:.1f}% | {actual_known_cost:.1f}% | "
            "{cost_complete}/{rows} | {actual_cost_complete}/{rows} | "
            "{visible_tokens:.1f} | {reasoning_tokens:.1f} | "
            "{tokens:.1f} | {tool_calls:.1f} | {tool_rate:.1f}% | "
            "{steps:.1f} | {llm_requests:.1f} | {usage_unknown:.1f} | "
            "{p50:.0f} | {p95:.0f} | "
            "{q_b0} | {cost_b0} | {q_b1} | {cost_b1} |".format(
                group=group,
                rows=item["rows"],
                done=item["completed"],
                quality=(f"{item['avg_quality']:.2f}" if item["avg_quality"] is not None else ""),
                quality_scored=(
                    f"{item['avg_quality_scored']:.2f}"
                    if item["avg_quality_scored"] is not None
                    else ""
                ),
                pass_rate=(
                    f"{item['avg_pass_rate']:.2f}" if item["avg_pass_rate"] is not None else ""
                ),
                judge_errors=item["judge_errors"],
                cost=item["avg_cost_usd"],
                generation_cost=item["avg_recorded_generation_cost_usd"],
                actual_cost=item["avg_actual_spend_cost_usd"],
                actual_generation_cost=item["avg_actual_spend_generation_cost_usd"],
                judge_cost=(
                    item["avg_recorded_judge_cost_usd"]
                    + item["avg_recorded_candidate_judge_cost_usd"]
                ),
                known_cost=item["known_cost_request_coverage_pct"],
                actual_known_cost=item["actual_spend_known_cost_request_coverage_pct"],
                cost_complete=item["result_cost_complete_rows"],
                actual_cost_complete=item["actual_spend_cost_complete_rows"],
                visible_tokens=item["avg_visible_tokens"],
                reasoning_tokens=item["avg_reasoning_tokens"],
                tokens=item["avg_total_tokens"],
                tool_calls=item["avg_tool_calls"],
                tool_rate=item["tool_call_rate_pct"],
                steps=item["avg_trajectory_steps"],
                llm_requests=item["avg_llm_requests"],
                usage_unknown=item["avg_usage_unknown"],
                p50=item["latency_p50_ms"],
                p95=item["latency_p95_ms"],
                q_b0=_signed_pct(item.get("avg_quality_pct_delta_vs_b0")),
                cost_b0=_signed_pct(item.get("avg_cost_pct_delta_vs_b0")),
                q_b1=_signed_pct(item.get("avg_quality_pct_delta_vs_b1")),
                cost_b1=_signed_pct(item.get("avg_cost_pct_delta_vs_b1")),
            )
        )
    return "\n".join(lines) + "\n"


def manifest_args(args: argparse.Namespace) -> dict[str, Any]:
    keys = [
        "input",
        "resume_from_jsonl",
        "only_group_task_keys",
        "expected_compatibility_manifest",
        "config",
        "experiment_config",
        "experiment_config_override",
        "experiment_config_set",
        "output_dir",
        "groups",
        "max_tasks",
        "concurrency",
        "timeout",
        "ensemble_proposer_timeout",
        "ensemble_aggregator_timeout",
        "ensemble_proposer_early_stop_success_count",
        "ensemble_proposer_early_stop_after",
        "expand_ensemble_timeouts_to_task_timeout",
        "runner_mode",
        "agent_max_iterations",
        *AGENT_FINALIZATION_POLICY_FIELDS,
        "require_clean_source",
        "repair_only_source_drift",
        "dry_run",
        "judge_model",
        "judge_repeats",
        "judge_concurrency",
        "judge_max_attempts",
        "judge_candidates",
        "generation_max_attempts",
        "generation_max_tokens",
        "generation_retry_backoff",
        "tool_mode",
        "contamination_blocked_domains",
        "local_web_search_provider",
        "local_web_search_api_key_env",
        "allow_firecrawl_web_fetch",
        "require_openrouter_non_byok",
        "continue_after_cost_audit_failure",
        "openrouter_web_search_engine",
        "openrouter_web_search_max_results",
        "openrouter_web_search_max_total_results",
        "openrouter_web_search_context_size",
        "openrouter_web_fetch_engine",
        "openrouter_web_fetch_max_uses",
        "openrouter_web_fetch_max_content_tokens",
        "openrouter_fusion_analysis_models",
        "openrouter_fusion_model",
        "openrouter_fusion_max_tool_calls",
        "openrouter_fusion_max_completion_tokens",
        "openrouter_fusion_reasoning_effort",
        "openrouter_fusion_temperature",
    ]

    def _json_value(value: Any) -> Any:
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, list):
            return [_json_value(item) for item in value]
        return value

    payload: dict[str, Any] = {}
    for key in keys:
        payload[key] = _json_value(getattr(args, key, None))
    return payload


def reconstructed_cli_args(args: argparse.Namespace) -> list[str]:
    cli_args: list[str] = []
    for key, value in manifest_args(args).items():
        if value is None or value == "" or value is False:
            continue
        flag = f"--{key.replace('_', '-')}"
        if value is True:
            cli_args.append(flag)
        elif isinstance(value, list):
            for item in value:
                cli_args.extend([flag, str(item)])
        else:
            cli_args.extend([flag, str(value)])
    return cli_args


def command_argv(args: argparse.Namespace) -> list[str]:
    raw_argv = getattr(args, "command_argv", None)
    if raw_argv:
        return [str(item) for item in raw_argv]
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        *reconstructed_cli_args(args),
    ]


def command_payload(args: argparse.Namespace) -> dict[str, Any]:
    argv = command_argv(args)
    pythonpath = os.environ.get("PYTHONPATH", "")
    shell = shlex.join(argv)
    if pythonpath:
        shell = f"PYTHONPATH={shlex.quote(pythonpath)} {shell}"
    return {
        "cwd": str(Path.cwd()),
        "python": sys.executable,
        "argv": argv,
        "shell": shell,
        "pythonpath": pythonpath,
        "parsed_args": manifest_args(args),
    }


def canonical_json_sha256(value: Any) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return f"sha256:{hashlib.sha256(serialized.encode('utf-8')).hexdigest()}"


def _sanitize_url_for_fingerprint(value: str) -> str:
    try:
        parsed = urlparse(value)
    except ValueError:
        return "<configured>" if value else ""
    if not parsed.scheme or not parsed.hostname:
        return value
    host = parsed.hostname
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return parsed._replace(netloc=host, query="", fragment="").geturl()


def _sanitize_fingerprint_config(value: Any, *, key: str = "") -> Any:
    normalized_key = key.casefold().replace("-", "_")
    if normalized_key.endswith("_env") or normalized_key.endswith("_env_pool"):
        return value
    if normalized_key in {
        "api_key",
        "authorization",
        "credential",
        "credentials",
        "password",
        "secret",
    } or normalized_key.endswith(("_api_key", "_password", "_secret")):
        return "<redacted>" if value else ""
    if isinstance(value, Mapping):
        return {
            str(item_key): _sanitize_fingerprint_config(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_fingerprint_config(item, key=key) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_fingerprint_config(item, key=key) for item in value]
    if normalized_key in {"base_url", "proxy"} and isinstance(value, str):
        return _sanitize_url_for_fingerprint(value)
    return value


def gateway_execution_contract(config: GatewayConfig) -> dict[str, Any]:
    dumped = config.model_dump(mode="json")
    relevant = {
        key: dumped.get(key)
        for key in (
            "llm",
            "llm_profiles",
            "llm_ensemble",
            "model_catalog",
            "models",
            "squilla_router",
            "sandbox",
        )
    }
    return _sanitize_fingerprint_config(relevant)


def validate_strict_openrouter_non_byok_environment(
    config: GatewayConfig,
) -> dict[str, Any]:
    """Fail closed on every routing/cost isolation prerequisite."""

    truthy = {"1", "true", "yes", "on", "enabled"}
    falsey = {"0", "false", "no", "off", "disabled"}
    required_truthy = (
        "OPENSQUILLA_PROVIDER_ROUTING_STRICT",
        "OPENSQUILLA_PROVIDER_STREAM_ERROR_FRAMES",
        "OPENSQUILLA_OPENROUTER_METADATA_REQUIRED",
        "OPENSQUILLA_OPENROUTER_REQUIRE_PARAMETERS",
        "OPENSQUILLA_OPENROUTER_DISABLE_RESPONSE_CACHE",
        "DRACO_OPENROUTER_KEY_EXCLUSIVE",
    )
    failures = [
        f"{name}=1 required"
        for name in required_truthy
        if os.environ.get(name, "").strip().casefold() not in truthy
    ]
    if os.environ.get("OPENSQUILLA_TRUST_ENV", "").strip().casefold() not in falsey:
        failures.append("OPENSQUILLA_TRUST_ENV=0 required")
    forbidden_overrides = (
        "OPENROUTER_BASE_URL",
        "OPENSQUILLA_LLM_BASE_URL",
        "OPENSQUILLA_LLM_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
    )
    active_overrides = [name for name in forbidden_overrides if os.environ.get(name, "").strip()]
    if active_overrides:
        failures.append(
            "proxy/base-url override(s) forbidden: " + ", ".join(sorted(active_overrides))
        )

    runtime = resolve_llm_runtime_config(config)
    official_base_url = str(get_provider_spec("openrouter").default_base_url or "").rstrip("/")
    if runtime.provider != "openrouter":
        failures.append("resolved provider must be openrouter")
    if not runtime.api_key:
        failures.append("resolved OpenRouter API key is missing")
    if str(runtime.base_url or "").rstrip("/") != official_base_url:
        failures.append("resolved OpenRouter base URL is not the official endpoint")
    if runtime.base_url_from_env:
        failures.append("resolved OpenRouter base URL came from an environment override")
    if str(runtime.proxy or "").strip():
        failures.append("resolved OpenRouter proxy must be empty")
    if failures:
        raise ValueError(
            "strict OpenRouter non-BYOK environment validation failed: " + "; ".join(failures)
        )
    return {
        "validated": True,
        "provider": "openrouter",
        "official_base_url": official_base_url,
        "provider_routing_strict": True,
        "stream_error_frames": True,
        "router_metadata_required": True,
        "require_parameters": True,
        "response_cache_disabled": True,
        "key_exclusive": True,
        "trust_env": False,
        "proxy_or_base_url_override": False,
    }


def resolved_llm_runtime_contract(config: GatewayConfig) -> dict[str, Any]:
    runtime = resolve_llm_runtime_config(config)
    key_fingerprint = (
        f"sha256:{hashlib.sha256(runtime.api_key.encode('utf-8')).hexdigest()}"
        if runtime.api_key
        else ""
    )
    trust_environment = os.environ.get("OPENSQUILLA_TRUST_ENV", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    ambient_proxies = {}
    if trust_environment:
        ambient_proxies = {
            name: _sanitize_url_for_fingerprint(os.environ.get(name, ""))
            for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
            if os.environ.get(name)
        }
    cache_namespace = os.environ.get("OPENSQUILLA_BENCHMARK_CACHE_NAMESPACE", "").strip()
    return {
        "provider": runtime.provider,
        "model": runtime.model,
        "api_key_sha256": key_fingerprint,
        "api_key_from_env": runtime.api_key_from_env,
        "base_url": _sanitize_url_for_fingerprint(runtime.base_url),
        "base_url_from_env": runtime.base_url_from_env,
        "proxy": _sanitize_url_for_fingerprint(runtime.proxy),
        "provider_routing": dict(sorted(runtime.provider_routing.items())),
        "provider_routing_strict": (
            os.environ.get("OPENSQUILLA_PROVIDER_ROUTING_STRICT", "").strip().lower()
            in {"1", "true", "yes", "on", "enabled"}
        ),
        "stream_error_frames": (
            os.environ.get("OPENSQUILLA_PROVIDER_STREAM_ERROR_FRAMES", "").strip().lower()
            in {"1", "true", "yes", "on", "enabled"}
        ),
        "router_metadata_required": (
            os.environ.get("OPENSQUILLA_OPENROUTER_METADATA_REQUIRED", "").strip().lower()
            in {"1", "true", "yes", "on", "enabled"}
        ),
        "require_parameters": (
            os.environ.get("OPENSQUILLA_OPENROUTER_REQUIRE_PARAMETERS", "").strip().lower()
            in {"1", "true", "yes", "on", "enabled"}
        ),
        "response_cache_disabled": (
            os.environ.get("OPENSQUILLA_OPENROUTER_DISABLE_RESPONSE_CACHE", "").strip().lower()
            in {"1", "true", "yes", "on", "enabled"}
        ),
        "key_exclusive": (
            os.environ.get("DRACO_OPENROUTER_KEY_EXCLUSIVE", "").strip().lower()
            in {"1", "true", "yes", "on", "enabled"}
        ),
        "cache_namespace_enabled": bool(cache_namespace),
        "cache_namespace_required": (
            os.environ.get("OPENSQUILLA_BENCHMARK_CACHE_NAMESPACE_REQUIRED", "").strip().lower()
            in {"1", "true", "yes", "on", "enabled"}
        ),
        "cache_namespace_sha256": (
            f"sha256:{hashlib.sha256(cache_namespace.encode('utf-8')).hexdigest()}"
            if cache_namespace
            else ""
        ),
        "trust_env": trust_environment,
        "ambient_proxies": ambient_proxies,
    }


def source_provenance() -> dict[str, Any]:
    runner_path = Path(__file__).resolve()
    payload: dict[str, Any] = {
        "runner_path": str(runner_path),
        "runner_sha256": hashlib.sha256(runner_path.read_bytes()).hexdigest(),
    }
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        tracked_diff = subprocess.run(
            [
                "git",
                "diff",
                "--binary",
                "HEAD",
                "--",
                "scripts",
                "src",
                "configs",
                "pyproject.toml",
                "uv.lock",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
        untracked = subprocess.run(
            [
                "git",
                "ls-files",
                "--others",
                "--exclude-standard",
                "--",
                "scripts",
                "src",
                "configs",
                "pyproject.toml",
                "uv.lock",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.splitlines()
        source_digest = hashlib.sha256()
        source_digest.update(head.encode("utf-8"))
        source_digest.update(b"\0")
        source_digest.update(tracked_diff.encode("utf-8"))
        for relative in sorted(untracked):
            untracked_path = ROOT / relative
            if not untracked_path.is_file():
                continue
            source_digest.update(b"\0")
            source_digest.update(relative.encode("utf-8"))
            source_digest.update(b"\0")
            source_digest.update(hashlib.sha256(untracked_path.read_bytes()).digest())
        tracked_dirty = bool(tracked_diff)
        payload.update(
            {
                "git_head": head,
                "git_tracked_dirty": tracked_dirty,
                "git_untracked_file_count": len(untracked),
                "git_dirty": tracked_dirty or bool(untracked),
                "source_tree_sha256": source_digest.hexdigest(),
            }
        )
    except (OSError, subprocess.SubprocessError):
        payload.update(
            {
                "git_head": None,
                "git_tracked_dirty": None,
                "git_untracked_file_count": None,
                "git_dirty": None,
                "source_tree_sha256": None,
            }
        )
    return payload


def build_run_compatibility(
    *,
    args: argparse.Namespace,
    config: GatewayConfig,
    groups: list[str],
    group_tool_policies: dict[str, dict[str, Any]],
    generation_policy: dict[str, Any],
) -> dict[str, Any]:
    bundle = getattr(args, "_draco_experiment_config_bundle", None)
    effective_experiment_config = (
        bundle.config.model_dump(mode="json")
        if isinstance(bundle, DracoExperimentConfigBundle)
        else None
    )
    if isinstance(effective_experiment_config, dict):
        # Scheduling-only concurrency may change between a canary, a full run,
        # and retry waves without changing any model, prompt, or scoring semantics.
        runner_config = effective_experiment_config.get("runner")
        if isinstance(runner_config, dict):
            runner_config.pop("concurrency", None)
        judge_config = effective_experiment_config.get("judge")
        if isinstance(judge_config, dict):
            judge_config.pop("concurrency", None)
    global_experiment_profile = None
    if isinstance(effective_experiment_config, dict):
        global_experiment_profile = {
            key: effective_experiment_config[key]
            for key in (
                "schema_version",
                "profile_id",
                "benchmark_input",
                "g1_routing",
                "timeouts",
                "runner",
                "generation",
                "tools",
                "judge",
            )
            if key in effective_experiment_config
        }
    source = dict(getattr(args, "_source_provenance", {}) or {})
    source_identity = {
        "git_head": source.get("git_head"),
        "source_tree_sha256": source.get("source_tree_sha256"),
    }
    gateway_contract = gateway_execution_contract(config)
    runtime_contract = resolved_llm_runtime_contract(config)
    contracts: dict[str, dict[str, Any]] = {}
    fingerprints: dict[str, str] = {}
    for group in groups:
        tool_contract = json.loads(json.dumps(group_tool_policies[group], ensure_ascii=False))
        local_tool_contract = tool_contract.get("local_web_tools")
        if isinstance(local_tool_contract, dict):
            local_tool_contract.pop("preflight", None)
        contract = {
            "schema": RUN_COMPATIBILITY_SCHEMA,
            "benchmark": "DRACO",
            "group": group,
            "group_spec": GROUP_SPECS[group],
            "source_identity": source_identity,
            "runner": {
                "mode": args.runner_mode,
                "agent_max_iterations": args.agent_max_iterations,
                "finalization_policy": agent_finalization_policy_from_args(args),
            },
            "tools": tool_contract,
            "generation": {
                "policy": generation_policy,
                "max_attempts": args.generation_max_attempts,
                "retry_backoff_seconds": args.generation_retry_backoff,
            },
            "judge": {
                "model": args.judge_model,
                "repeats": args.judge_repeats,
                "max_attempts": args.judge_max_attempts,
                "judge_candidates": args.judge_candidates,
            },
            "timeouts": {
                "task_seconds": args.timeout,
                "proposer_seconds": args.ensemble_proposer_timeout,
                "aggregator_seconds": args.ensemble_aggregator_timeout,
                "proposer_early_stop_success_count": (
                    args.ensemble_proposer_early_stop_success_count
                ),
                "proposer_early_stop_after_seconds": args.ensemble_proposer_early_stop_after,
                "expand_to_task_timeout": args.expand_ensemble_timeouts_to_task_timeout,
            },
            "gateway_execution": gateway_contract,
            "resolved_llm_runtime": runtime_contract,
            "cost_policy": {
                "require_openrouter_non_byok": bool(
                    getattr(args, "require_openrouter_non_byok", False)
                )
            },
            "global_experiment_profile": global_experiment_profile,
            "experiment_config": (
                effective_experiment_config if GROUP_SPECS[group].get("experiment_config") else None
            ),
            "g1_registry_contract": (
                dict(getattr(args, "_g1_registry_contract", {}) or {}) if group == "G1" else None
            ),
            "formal_runtime_freeze": dict(getattr(args, "_formal_runtime_freeze", {}) or {}),
            "dry_run": bool(args.dry_run),
        }
        contracts[group] = contract
        fingerprints[group] = canonical_json_sha256(contract)
    return {
        "schema": RUN_COMPATIBILITY_SCHEMA,
        "fingerprints": fingerprints,
        "contracts": contracts,
    }


def validate_expected_run_compatibility(
    *,
    path: Path,
    actual: dict[str, Any],
    groups: list[str],
) -> None:
    if not path.is_file():
        raise ValueError(f"expected compatibility manifest does not exist: {path}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    expected = manifest.get("run_compatibility") if isinstance(manifest, dict) else None
    expected_fingerprints = expected.get("fingerprints") if isinstance(expected, dict) else None
    actual_fingerprints = actual.get("fingerprints")
    if not isinstance(expected_fingerprints, dict):
        raise ValueError(f"manifest lacks run_compatibility fingerprints: {path}")
    if not isinstance(actual_fingerprints, dict):
        raise ValueError("current run compatibility fingerprints are unavailable")
    mismatches = [
        group
        for group in groups
        if str(expected_fingerprints.get(group) or "") != str(actual_fingerprints.get(group) or "")
    ]
    if mismatches:
        raise ValueError(
            "current run configuration is incompatible with the expected manifest for "
            f"groups: {', '.join(mismatches)}"
        )


def validate_repair_only_source_drift_prerequisites(
    args: argparse.Namespace,
) -> dict[str, Any] | None:
    """Require an explicitly bounded, file-backed repair-only invocation."""

    if not bool(getattr(args, "repair_only_source_drift", False)):
        return None
    missing: list[str] = []
    if not bool(getattr(args, "require_clean_source", False)):
        missing.append("--require-clean-source")
    expected_manifest = getattr(args, "expected_compatibility_manifest", None)
    if expected_manifest is None:
        missing.append("--expected-compatibility-manifest")
    elif not Path(expected_manifest).is_file():
        missing.append("existing --expected-compatibility-manifest")
    resume_paths = list(getattr(args, "resume_from_jsonl", []) or [])
    if not resume_paths:
        missing.append("--resume-from-jsonl")
    elif any(not Path(path).is_file() for path in resume_paths):
        missing.append("existing --resume-from-jsonl files")
    only_keys = getattr(args, "only_group_task_keys", None)
    if only_keys is None:
        missing.append("--only-group-task-keys")
    elif not Path(only_keys).is_file():
        missing.append("existing --only-group-task-keys")
    if missing:
        raise ValueError(
            "--repair-only-source-drift requires all repair-only safeguards: " + ", ".join(missing)
        )
    return {
        "require_clean_source": True,
        "expected_compatibility_manifest": str(Path(expected_manifest)),
        "resume_source_count": len(resume_paths),
        "only_group_task_keys": str(Path(only_keys)),
    }


def validate_repair_only_source_drift_compatibility(
    *,
    path: Path,
    actual: Mapping[str, Any],
    groups: list[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Allow only source_identity drift and inherit the original contracts."""

    if not path.is_file():
        raise ValueError(f"expected compatibility manifest does not exist: {path}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    expected = manifest.get("run_compatibility") if isinstance(manifest, Mapping) else None
    if not isinstance(expected, Mapping):
        raise ValueError(f"manifest lacks run_compatibility: {path}")
    expected_contracts = expected.get("contracts")
    expected_fingerprints = expected.get("fingerprints")
    actual_contracts = actual.get("contracts")
    actual_fingerprints = actual.get("fingerprints")
    if (
        set(expected) != {"schema", "fingerprints", "contracts"}
        or set(actual) != {"schema", "fingerprints", "contracts"}
        or expected.get("schema") != RUN_COMPATIBILITY_SCHEMA
        or actual.get("schema") != RUN_COMPATIBILITY_SCHEMA
        or not isinstance(expected_contracts, Mapping)
        or not isinstance(expected_fingerprints, Mapping)
        or not isinstance(actual_contracts, Mapping)
        or not isinstance(actual_fingerprints, Mapping)
    ):
        raise ValueError(
            "repair-only source drift requires complete canonical run compatibility "
            f"contracts and fingerprints in both the current run and {path}"
        )

    group_audits: dict[str, Any] = {}
    mismatch_reasons: dict[str, list[str]] = {}
    for group in groups:
        reasons: list[str] = []
        expected_contract = expected_contracts.get(group)
        actual_contract = actual_contracts.get(group)
        expected_fingerprint = str(expected_fingerprints.get(group) or "")
        actual_fingerprint = str(actual_fingerprints.get(group) or "")
        if not isinstance(expected_contract, Mapping):
            reasons.append("missing_expected_contract")
        if not isinstance(actual_contract, Mapping):
            reasons.append("missing_current_contract")
        if reasons:
            mismatch_reasons[group] = reasons
            continue
        expected_contract_copy = json.loads(json.dumps(dict(expected_contract), ensure_ascii=False))
        actual_contract_copy = json.loads(json.dumps(dict(actual_contract), ensure_ascii=False))
        expected_canonical_fingerprint = canonical_json_sha256(expected_contract_copy)
        actual_canonical_fingerprint = canonical_json_sha256(actual_contract_copy)
        if expected_fingerprint != expected_canonical_fingerprint:
            reasons.append("expected_fingerprint_not_canonical")
        if actual_fingerprint != actual_canonical_fingerprint:
            reasons.append("current_fingerprint_not_canonical")
        expected_source = expected_contract_copy.pop("source_identity", None)
        actual_source = actual_contract_copy.pop("source_identity", None)
        if not isinstance(expected_source, Mapping):
            reasons.append("missing_expected_source_identity")
        if not isinstance(actual_source, Mapping):
            reasons.append("missing_current_source_identity")
        non_source_fingerprint = canonical_json_sha256(expected_contract_copy)
        current_non_source_fingerprint = canonical_json_sha256(actual_contract_copy)
        if (
            expected_contract_copy != actual_contract_copy
            or non_source_fingerprint != current_non_source_fingerprint
        ):
            reasons.append("non_source_contract_mismatch")
        if reasons:
            mismatch_reasons[group] = list(dict.fromkeys(reasons))
            continue
        group_audits[group] = {
            "expected_fingerprint": expected_fingerprint,
            "current_fingerprint": actual_fingerprint,
            "inherited_fingerprint": expected_fingerprint,
            "non_source_contract_fingerprint": non_source_fingerprint,
            "source_identity_changed": dict(expected_source) != dict(actual_source),
            "expected_source_identity": dict(expected_source),
            "current_source_identity": dict(actual_source),
            "non_source_contract_match": True,
            "canonical_fingerprints_verified": True,
        }
    if mismatch_reasons:
        details = "; ".join(
            f"{group}={','.join(reasons)}" for group, reasons in sorted(mismatch_reasons.items())
        )
        raise ValueError(
            "repair-only source drift rejected a non-source or non-canonical "
            f"compatibility difference: {details}"
        )

    inherited = json.loads(json.dumps(dict(expected), ensure_ascii=False))
    audit = {
        "schema": REPAIR_ONLY_SOURCE_DRIFT_SCHEMA,
        "mode": "repair_only_source_drift",
        "status": "compatibility_validated",
        "expected_manifest": str(path),
        "allowed_difference": "per-group contract.source_identity only",
        "runtime_run_compatibility": "inherited_from_expected_manifest",
        "groups": group_audits,
    }
    return inherited, audit


def repair_only_resume_classification_audit(
    *,
    selected_keys: set[tuple[str, str]],
    resume_states: Mapping[tuple[str, str], Mapping[str, Any]],
) -> dict[str, Any]:
    """Describe whether a repair-only wave can proceed without generation."""

    action_counts: Counter[str] = Counter()
    regenerate_pairs: list[dict[str, Any]] = []
    allowed_actions = {
        "complete",
        "judge_only",
        "metadata_only",
        "policy_violation",
    }
    for group, task_id in sorted(selected_keys):
        state = resume_states.get((group, task_id))
        action = str(state.get("action") or "") if isinstance(state, Mapping) else ""
        if not action:
            action = "regenerate"
        action_counts[action] += 1
        if action in allowed_actions:
            continue
        attempts_used = (
            coerce_metric_int(state.get("prior_generation_attempts_used"))
            if isinstance(state, Mapping)
            else 0
        )
        regenerate_pairs.append(
            {
                "group": group,
                "task_id": task_id,
                "reason": (
                    "missing_resume_state"
                    if not isinstance(state, Mapping)
                    else "unsupported_resume_action"
                    if action != "regenerate"
                    else "generation_budget_exhausted"
                    if attempts_used >= GENERATION_MAX_ATTEMPTS
                    else "generation_invalid"
                ),
                "prior_generation_attempts_used": attempts_used,
                "generation_reasons": (
                    list(state.get("generation_reasons") or [])
                    if isinstance(state, Mapping)
                    else []
                ),
            }
        )
    return {
        "status": "rejected_regeneration_required"
        if regenerate_pairs
        else "repair_actions_validated",
        "selected_pair_count": len(selected_keys),
        "action_counts": dict(sorted(action_counts.items())),
        "regenerate_pair_count": len(regenerate_pairs),
        "regenerate_pairs": regenerate_pairs,
        "generation_allowed": False,
        "judge_allowed_for_judge_only": True,
    }


def write_command_file(
    path: Path,
    *,
    args: argparse.Namespace,
    stamp: str,
) -> dict[str, Any]:
    payload = command_payload(args)
    lines = [
        "# DRACO benchmark command",
        f"stamp: {stamp}",
        f"cwd: {payload['cwd']}",
        f"python: {payload['python']}",
    ]
    if payload["pythonpath"]:
        lines.append(f"PYTHONPATH: {payload['pythonpath']}")
    lines.extend(
        [
            "",
            payload["shell"],
            "",
            "# Parsed args",
            json.dumps(payload["parsed_args"], ensure_ascii=False, indent=2, sort_keys=True),
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")
    return payload


def write_experiment_config_artifacts(
    output_dir: Path,
    *,
    args: argparse.Namespace,
    stamp: str,
) -> dict[str, str]:
    bundle = getattr(args, "_draco_experiment_config_bundle", None)
    if not isinstance(bundle, DracoExperimentConfigBundle):
        return {}

    def _write(name: str, payload: Any) -> Path:
        path = output_dir / f"draco_run_{stamp}.experiment-config.{name}.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return path

    artifacts = {
        "experiment_config_base_json": str(_write("base", bundle.base_document)),
        "experiment_config_effective_json": str(
            _write("effective", bundle.config.model_dump(mode="json"))
        ),
    }
    for index, (_, document) in enumerate(bundle.override_documents, start=1):
        key = f"experiment_config_override_{index:02d}_json"
        artifacts[key] = str(_write(f"override-{index:02d}", document))
    if bundle.inline_overrides:
        artifacts["experiment_config_inline_overrides_json"] = str(
            _write("inline-overrides", list(bundle.inline_overrides))
        )
    resolution = {
        "profile_id": bundle.config.profile_id,
        "provenance": bundle.provenance(),
        "input_validation": getattr(args, "_draco_input_validation", None),
        "artifact_keys": sorted([*artifacts, "experiment_config_resolution_json"]),
    }
    artifacts["experiment_config_resolution_json"] = str(_write("resolution", resolution))
    return artifacts


def write_manifest(
    path: Path,
    *,
    args: argparse.Namespace,
    stamp: str,
    status: str,
    started_at: float,
    tasks: list[dict[str, Any]],
    groups: list[str],
    artifacts: dict[str, str],
    rows_written: int = 0,
    finished_at: float | None = None,
    summary: dict[str, Any] | None = None,
    tool_policy: dict[str, Any] | None = None,
    command: dict[str, Any] | None = None,
    failure: dict[str, Any] | None = None,
) -> None:
    policy = tool_policy or benchmark_tool_policy(args)
    generation_policy = generation_thinking_policy(args)
    payload: dict[str, Any] = {
        "benchmark": "DRACO",
        "runner": f"scripts/{Path(__file__).name}",
        "runner_mode": getattr(args, "runner_mode", DEFAULT_DRACO_RUNNER_MODE),
        "agent_max_iterations": getattr(
            args,
            "agent_max_iterations",
            DEFAULT_AGENT_MAX_ITERATIONS,
        ),
        "agent_finalization_policy": agent_finalization_policy_from_args(args),
        "tool_policy": policy,
        "generation_policy": generation_policy,
        "stamp": stamp,
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "elapsed_ms": (int((finished_at - started_at) * 1000) if finished_at is not None else None),
        "args": manifest_args(args),
        "groups": groups,
        "group_specs": {group: GROUP_SPECS[group] for group in groups},
        "task_count": len(tasks),
        "task_ids": [str(task["id"]) for task in tasks],
        "rows_written": rows_written,
        "artifacts": artifacts,
        "effective_argument_sources": dict(getattr(args, "_effective_argument_sources", {}) or {}),
        "source_provenance": dict(getattr(args, "_source_provenance", None) or source_provenance()),
    }
    run_compatibility = getattr(args, "_run_compatibility", None)
    if isinstance(run_compatibility, dict):
        payload["run_compatibility"] = run_compatibility
    benchmark_alignments = dict(getattr(args, "_benchmark_alignments", {}) or {})
    if benchmark_alignments:
        payload["benchmark_alignments"] = benchmark_alignments
    g1_registry_contract = getattr(args, "_g1_registry_contract", None)
    if isinstance(g1_registry_contract, Mapping):
        payload["g1_registry_contract"] = dict(g1_registry_contract)
    formal_runtime_freeze = getattr(args, "_formal_runtime_freeze", None)
    if isinstance(formal_runtime_freeze, Mapping):
        payload["formal_runtime_freeze"] = dict(formal_runtime_freeze)
    input_validation = getattr(args, "_draco_input_validation", None)
    if input_validation is not None:
        payload["benchmark_input_validation"] = input_validation
    if command is not None:
        payload["command"] = command
    if summary is not None:
        payload["summary"] = summary
    if failure is not None:
        payload["failure"] = failure
    resume_selection = getattr(args, "_resume_selection", None)
    if isinstance(resume_selection, dict):
        payload["resume_selection"] = resume_selection
    repair_compatibility_audit = getattr(
        args,
        "_repair_compatibility_audit",
        None,
    )
    if isinstance(repair_compatibility_audit, Mapping):
        payload["repair_compatibility_audit"] = dict(repair_compatibility_audit)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_selected_group_task_keys(
    *,
    tasks: list[dict[str, Any]],
    groups: list[str],
    only_keys_path: Path | None,
) -> set[tuple[str, str]]:
    selected_keys = {(group, str(task["id"])) for task in tasks for group in groups}
    if only_keys_path is None:
        return selected_keys

    path = Path(only_keys_path)
    if not path.is_file():
        raise ValueError(f"group/task key JSONL does not exist: {path}")
    requested_keys: set[tuple[str, str]] = set()
    with path.open("r", encoding="utf-8") as only_fh:
        for line_number, line in enumerate(only_fh, start=1):
            if not line.strip():
                continue
            try:
                key_row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"invalid group/task key JSONL at {path}:{line_number}: {exc}"
                ) from exc
            if not isinstance(key_row, dict):
                raise ValueError(
                    f"group/task key JSONL row is not an object at {path}:{line_number}"
                )
            key = (
                str(key_row.get("group") or ""),
                str(key_row.get("task_id") or ""),
            )
            if not all(key):
                raise ValueError(
                    f"missing group or task_id in group/task key JSONL at {path}:{line_number}"
                )
            requested_keys.add(key)
    unknown_keys = requested_keys - selected_keys
    if unknown_keys:
        preview = ", ".join(f"{group}/{task_id}" for group, task_id in sorted(unknown_keys)[:5])
        raise ValueError(f"group/task key JSONL contains unknown keys: {preview}")
    return selected_keys & requested_keys


_NON_GENERATION_ERRORS = frozenset(
    {
        "openrouter_non_byok_verification_failed",
        "openrouter_non_byok_metadata_incomplete",
        "openrouter_non_byok_policy_violation",
        "cost_metadata_incomplete",
        "judge_incomplete",
        JUDGE_ATTEMPT_BUDGET_EXHAUSTED_ERROR,
    }
)
_REPAIRABLE_COMPLETION_ERRORS = frozenset(
    {
        "openrouter_non_byok_verification_failed",
        "openrouter_non_byok_metadata_incomplete",
        "cost_metadata_incomplete",
        "judge_incomplete",
    }
)
_FATAL_POLICY_ERRORS = frozenset(
    {
        "openrouter_non_byok_policy_violation",
    }
)
GENERATION_TERMINAL_RECLASSIFICATION_SCHEMA = (
    "opensquilla.draco.generation-terminal-reclassification/v1"
)
LEGACY_TERMINAL_POLICY_ERROR = "aggregator_fallback_used_or_unknown"
_G1_LIFECYCLE_PLAN_MATCH_FIELDS = (
    "decision_id",
    "registry_snapshot_hash",
    "ranking_config_hash",
    "selected_P",
    "selected_A",
    "proposer_models",
    "aggregator_model",
    "aggregator_candidates",
    "aggregator_recovery_mode",
    "aggregator_recovery_top_k",
    "aggregator_max_tokens_cap",
    "aggregator_visible_answer_reserve_tokens",
    "task_analyzer",
    "task_profile",
    "task_profile_hash",
    "request_context",
    "request_context_hash",
    "routed_tier",
    "routing_confidence",
    "user_profile_enabled",
    "user_profile_version",
    "user_profile_source",
    "N_min",
    "N_max",
    "bound_reasons",
    "retry_parent_decision_id",
    "retry_excluded_proposer_identities",
    "task_analysis_reused",
    "task_analysis_reuse",
    "retry_routing",
)


def generation_usage_identity_contract(value: Any) -> Any:
    """Project immutable generation usage while excluding repairable billing fields."""

    if not isinstance(value, Mapping):
        return None
    keys = (
        "provider",
        "model",
        "requested_provider",
        "requested_model",
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cached_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "physical_attempt_id",
    )
    contract = {key: value.get(key) for key in keys if key in value}
    breakdown = value.get("model_usage_breakdown")
    if isinstance(breakdown, list):
        contract["model_usage_breakdown"] = [
            generation_usage_identity_contract(item)
            for item in breakdown
            if isinstance(item, Mapping)
        ]
    return contract


def matching_saved_generation_attempts(
    row: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    """Find physical attempts whose immutable answer and usage equal the saved row."""

    final_sha = str(row.get("final_text_sha256") or "")
    usage_contract = generation_usage_identity_contract(row.get("usage"))
    execution = row.get("execution")
    attempts = (
        execution.get("generation_attempts")
        if isinstance(execution, Mapping) and isinstance(execution.get("generation_attempts"), list)
        else []
    )
    return [
        attempt
        for attempt in attempts
        if isinstance(attempt, Mapping)
        and isinstance(attempt.get("run"), Mapping)
        and str(attempt["run"].get("final_text_sha256") or "") == final_sha
        and generation_usage_identity_contract(attempt["run"].get("usage")) == usage_contract
    ]


def _normalized_g1_retry_identities(raw: Any) -> tuple[set[str], bool]:
    if raw is None:
        return set(), True
    if not isinstance(raw, list):
        return set(), False
    normalized = [str(identity or "").strip().lower() for identity in raw]
    valid = bool(
        all(
            identity
            and identity.partition(":")[1] == ":"
            and identity.partition(":")[0]
            and identity.partition(":")[2]
            for identity in normalized
        )
        and len(normalized) == len(set(normalized))
    )
    return set(normalized), valid


def _g1_reasoning_only_length_failures(
    run: Mapping[str, Any],
) -> list[dict[str, Any]]:
    trace = run.get("ensemble_trace")
    return _reasoning_only_length_failures_from_trace(trace) if isinstance(trace, Mapping) else []


def _g1_reasoning_only_length_failure_identities(run: Mapping[str, Any]) -> set[str]:
    return {
        str(failure.get("identity") or "") for failure in _g1_reasoning_only_length_failures(run)
    }


def _g1_is_unknown_task_analyzer_placeholder(
    unit: Mapping[str, Any],
) -> bool:
    provider_usage = unit.get("provider_usage")
    attempt_id = str(unit.get("physical_attempt_id") or "")
    billed_cost = unit.get("billed_cost")
    return (
        str(unit.get("role") or "").strip().casefold() == "unknown_request"
        and str(unit.get("label") or "").strip().casefold() == "task_analyzer"
        and str(unit.get("provider") or "").strip() == ""
        and str(unit.get("model") or "").strip() == ""
        and str(unit.get("requested_provider") or "").strip().casefold()
        == "openrouter"
        and str(unit.get("requested_model") or "").strip()
        == GROUP_SPECS["B0"]["model"]
        and isinstance(unit.get("attempt"), int)
        and not isinstance(unit.get("attempt"), bool)
        and int(unit["attempt"]) >= 1
        and len(attempt_id) == 32
        and all(character in "0123456789abcdef" for character in attempt_id)
        and coerce_metric_int(unit.get("input_tokens")) == 0
        and coerce_metric_int(unit.get("output_tokens")) == 0
        and coerce_metric_int(unit.get("reasoning_tokens")) == 0
        and coerce_metric_int(unit.get("cached_tokens")) == 0
        and coerce_metric_int(unit.get("cache_write_tokens")) == 0
        and isinstance(billed_cost, (int, float))
        and not isinstance(billed_cost, bool)
        and math.isfinite(float(billed_cost))
        and float(billed_cost) == 0.0
        and str(unit.get("cost_source") or "none").casefold()
        in {"none", "unavailable"}
        and isinstance(provider_usage, Mapping)
        and provider_usage.get("usage_unknown") is True
        and provider_usage.get("physical_attempt_id") == attempt_id
    )


def _g1_canonical_task_analyzer_setup_units(
    run: Mapping[str, Any],
    *,
    identity_seed: str,
) -> list[dict[str, Any]]:
    raw_setup = run.get("setup_usage")
    setup_rows = (
        [
            copy.deepcopy(dict(row))
            for row in raw_setup
            if isinstance(row, Mapping)
            and (
                str(row.get("role") or "").strip().casefold()
                == "task_analyzer"
                or (
                    str(row.get("role") or "").strip().casefold()
                    == "unknown_request"
                    and str(row.get("label") or "").strip().casefold()
                    == "task_analyzer"
                )
            )
        ]
        if isinstance(raw_setup, list)
        else []
    )
    evidence_run: Mapping[str, Any] = (
        {"usage": {"model_usage_breakdown": setup_rows}}
        if setup_rows
        else run
    )
    return [
        unit
        for unit in canonical_run_usage_units(
            evidence_run,
            identity_seed=identity_seed,
        )
        if (
            str(unit.get("role") or "").strip().casefold()
            == "task_analyzer"
            or _g1_is_unknown_task_analyzer_placeholder(unit)
        )
    ]


def g1_run_expected_ensemble_request_count(run: Mapping[str, Any]) -> int:
    """Exclude provable setup-only analyzer calls from ensemble trace demand."""

    total = derive_physical_request_count(run)
    analyzer_count = sum(
        1
        for unit in _g1_canonical_task_analyzer_setup_units(
            run,
            identity_seed="expected-ensemble-request-count",
        )
    )
    return max(0, total - analyzer_count)


def _g1_task_analyzer_decision_projection(value: Any) -> Any:
    """Normalize receipt representation while retaining analyzer usage evidence."""

    from opensquilla.provider.thinking_execution import (
        immutable_task_analyzer_payload,
    )

    return immutable_task_analyzer_payload(value)


def _g1_lifecycle_plan_field(plan: Mapping[str, Any], field: str) -> Any:
    value = plan.get(field)
    if field == "task_analyzer":
        return _g1_task_analyzer_decision_projection(value)
    return value


def _g1_plans_match(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return all(
        _g1_lifecycle_plan_field(left, field)
        == _g1_lifecycle_plan_field(right, field)
        for field in _G1_LIFECYCLE_PLAN_MATCH_FIELDS
    )


def _g1_full_plans_match(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
) -> bool:
    """Compare every immutable field while excluding execution fallback receipts."""

    try:
        return canonical_json_sha256(
            g1_immutable_selection_plan_payload(left)
        ) == canonical_json_sha256(g1_immutable_selection_plan_payload(right))
    except (TypeError, ValueError):
        return False


def _g1_execution_plan_mutation_reasons(
    expected_plan: Mapping[str, Any],
    observed_plan: Mapping[str, Any],
) -> list[str]:
    reason = g1_execution_plan_mutation_reason(
        expected_plan,
        observed_plan,
    )
    return [reason] if reason else []


def _g1_retry_plan_provenance_reasons(
    plan: Mapping[str, Any],
    *,
    initial_plan: Mapping[str, Any],
    initial_decision_id: str,
    exclusions: set[str],
) -> list[str]:
    """Require bound-v2 provenance for every adaptive retry plan."""

    reasons: list[str] = []
    provenance_fields = (
        "retry_parent_decision_id",
        "retry_excluded_proposer_identities",
        "task_analysis_reused",
        "task_analysis_reuse",
        "retry_routing",
    )
    if not exclusions:
        if any(field in plan for field in provenance_fields):
            reasons.append("unexpected_g1_initial_retry_provenance")
        return reasons
    expected_exclusions = sorted(exclusions)
    if not initial_decision_id or plan.get("retry_parent_decision_id") != initial_decision_id:
        reasons.append("wrong_g1_retry_parent_decision_id")
    if not str(plan.get("decision_id") or "") or plan.get("decision_id") == initial_decision_id:
        reasons.append("wrong_g1_retry_decision_id")
    if plan.get("retry_excluded_proposer_identities") != expected_exclusions:
        reasons.append("wrong_g1_retry_plan_exclusions")
    if plan.get("task_analysis_reused") is not True:
        reasons.append("missing_g1_retry_task_analysis_reuse")
    retry_routing = plan.get("retry_routing")
    if not isinstance(retry_routing, Mapping):
        reasons.append("missing_g1_retry_routing_provenance")
        return reasons
    retry_schema = retry_routing.get("schema")
    if retry_schema != "opensquilla.router-dynamic-retry-routing/v2":
        reasons.append("wrong_g1_retry_routing_schema")
    if retry_routing.get("reason") != "prior_attempt_reasoning_only_length":
        reasons.append("wrong_g1_retry_routing_reason")
    if retry_routing.get("parent_decision_id") != initial_decision_id:
        reasons.append("wrong_g1_retry_routing_parent_decision_id")
    if retry_routing.get("excluded_proposer_identities") != expected_exclusions:
        reasons.append("wrong_g1_retry_routing_exclusions")
    if retry_routing.get("task_analysis_reused") is not True:
        reasons.append("missing_g1_retry_routing_task_analysis_reuse")
    if retry_schema == "opensquilla.router-dynamic-retry-routing/v2":
        from opensquilla.provider.ranking_router import (
            router_dynamic_task_analysis_reuse_reasons,
        )

        reasons.extend(
            router_dynamic_task_analysis_reuse_reasons(
                initial_plan,
                plan,
            )
        )
        binding = plan.get("task_analysis_reuse")
        binding_hash = (
            str(binding.get("projection_sha256") or "") if isinstance(binding, Mapping) else ""
        )
        if retry_routing.get("task_analysis_source_decision_id") != initial_decision_id:
            reasons.append("wrong_g1_retry_routing_task_analysis_source_decision")
        if retry_routing.get("task_analysis_reuse_sha256") != binding_hash:
            reasons.append("wrong_g1_retry_routing_task_analysis_reuse_hash")
    return list(dict.fromkeys(reasons))


def _adaptive_g1_provider_lifecycle_routing_evidence(
    row: Mapping[str, Any],
    *,
    g1_contract: Mapping[str, Any],
    physical_plans: list[Mapping[str, Any]],
    initial_reasons: list[str],
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Validate each adaptive retry plan and bind the selected plan to receipts."""

    reasons = list(initial_reasons)
    matching_attempts = matching_saved_generation_attempts(row)
    if len(matching_attempts) != 1:
        reasons.append("ambiguous_g1_selected_generation_attempt")
        return {}, {}, list(dict.fromkeys(reasons))
    selected_attempt = matching_attempts[0]
    selected_attempt_id = str(selected_attempt.get("attempt_id") or "")
    selected_ordinal = coerce_metric_int(selected_attempt.get("attempt"))
    execution = row.get("execution")
    attempts = (
        execution.get("generation_attempts")
        if isinstance(execution, Mapping) and isinstance(execution.get("generation_attempts"), list)
        else []
    )
    expected_exclusions: set[str] = set()
    selected_plan: Mapping[str, Any] | None = None
    selected_routing: Mapping[str, Any] | None = None
    validated_plan_count = 0
    validated_physical_plan_count = 0
    previous_ordinal = 0
    previous_plan: Mapping[str, Any] | None = None
    pending_retry_plan: Mapping[str, Any] | None = None
    pending_retry_exclusions: set[str] | None = None
    initial_plan: Mapping[str, Any] | None = None
    initial_decision_id = ""
    for attempt in attempts:
        if not isinstance(attempt, Mapping):
            reasons.append("invalid_g1_attempt_evidence")
            continue
        ordinal = coerce_metric_int(attempt.get("attempt"))
        if ordinal <= 0:
            reasons.append("invalid_g1_attempt_ordinal_sequence")
            continue
        if ordinal <= previous_ordinal:
            reasons.append("invalid_g1_attempt_ordinal_sequence")
        previous_ordinal = ordinal
        run = attempt.get("run")
        run_map = run if isinstance(run, Mapping) else {}
        routing = run_map.get("routing_trace")
        routing_map = routing if isinstance(routing, Mapping) else {}
        routed_plan = routing_map.get("selection_plan")
        declared_plan = attempt.get("selection_plan")
        if not isinstance(declared_plan, Mapping):
            reasons.append("missing_g1_attempt_selection_plan")
            continue
        plan = declared_plan
        if not isinstance(routed_plan, Mapping):
            reasons.append("missing_g1_attempt_routing_plan")
        validated_plan_count += 1
        reasons.extend(
            g1_registry_contract_reasons(
                {"selection_plan": plan},
                g1_contract,
            )
        )
        if isinstance(routed_plan, Mapping):
            reasons.extend(
                g1_registry_contract_reasons(
                    {"selection_plan": routed_plan},
                    g1_contract,
                )
            )
            if not _g1_full_plans_match(declared_plan, routed_plan):
                reasons.append("g1_attempt_selection_plan_differs_from_routing")
            reasons.extend(
                _g1_execution_plan_mutation_reasons(
                    declared_plan,
                    routed_plan,
                )
            )

        if not initial_decision_id:
            initial_decision_id = str(plan.get("decision_id") or "")
            initial_plan = plan
        if pending_retry_plan is not None:
            if not _g1_full_plans_match(plan, pending_retry_plan):
                reasons.append("g1_retry_selection_plan_not_used_by_next_attempt")
            reasons.extend(
                _g1_execution_plan_mutation_reasons(
                    pending_retry_plan,
                    plan,
                )
            )
        elif previous_plan is not None:
            if not _g1_full_plans_match(plan, previous_plan):
                reasons.append("g1_attempt_plan_changed_without_retry_selection")
            reasons.extend(
                _g1_execution_plan_mutation_reasons(
                    previous_plan,
                    plan,
                )
            )

        attempt_trace = run_map.get("ensemble_trace")
        if isinstance(attempt_trace, Mapping) and attempt_trace:
            attempt_calls, attempt_sequence_reasons = ensemble_call_trace_sequence(attempt_trace)
            reasons.extend(attempt_sequence_reasons)
            if not attempt_calls:
                reasons.append("missing_g1_attempt_physical_selection_plan")
            for call in attempt_calls:
                physical_plan = call.get("selection_plan")
                reasons.extend(
                    g1_registry_contract_reasons(
                        {"selection_plan": physical_plan},
                        g1_contract,
                    )
                )
                if not isinstance(physical_plan, Mapping):
                    reasons.append("missing_g1_attempt_physical_selection_plan")
                    continue
                validated_physical_plan_count += 1
                if not _g1_full_plans_match(plan, physical_plan):
                    reasons.append("g1_attempt_selection_plan_differs_from_physical_plan")
                reasons.extend(
                    _g1_execution_plan_mutation_reasons(
                        plan,
                        physical_plan,
                    )
                )
                if isinstance(routed_plan, Mapping) and not _g1_full_plans_match(
                    routed_plan,
                    physical_plan,
                ):
                    reasons.append("g1_attempt_routing_plan_differs_from_physical_plan")
                if isinstance(routed_plan, Mapping):
                    reasons.extend(
                        _g1_execution_plan_mutation_reasons(
                            routed_plan,
                            physical_plan,
                        )
                    )
        elif g1_run_expected_ensemble_request_count(run_map) > 0:
            reasons.append("missing_g1_attempt_ensemble_trace")

        attempt_exclusions_raw = attempt.get("excluded_proposer_identities")
        exclusion_sources: list[tuple[set[str], bool]] = [
            _normalized_g1_retry_identities(attempt_exclusions_raw)
        ]
        if "retry_excluded_proposer_identities" in plan:
            exclusion_sources.append(
                _normalized_g1_retry_identities(plan.get("retry_excluded_proposer_identities"))
            )
        retry_routing = plan.get("retry_routing")
        if isinstance(retry_routing, Mapping):
            exclusion_sources.append(
                _normalized_g1_retry_identities(retry_routing.get("excluded_proposer_identities"))
            )
        if any(not valid for _, valid in exclusion_sources):
            reasons.append("invalid_g1_retry_excluded_proposer_identities")
        current_exclusions = exclusion_sources[0][0]
        if attempt_exclusions_raw != sorted(current_exclusions):
            reasons.append("noncanonical_g1_attempt_exclusions")
        if any(values != current_exclusions for values, _ in exclusion_sources[1:]):
            reasons.append("conflicting_g1_retry_excluded_proposer_identities")
        if not expected_exclusions.issubset(current_exclusions):
            reasons.append("nonmonotonic_g1_retry_exclusions")
        if current_exclusions != expected_exclusions:
            reasons.append("wrong_g1_retry_exclusion_evolution")
        if pending_retry_exclusions is not None and current_exclusions != pending_retry_exclusions:
            reasons.append("g1_retry_exclusions_not_used_by_next_attempt")
        pending_retry_plan = None
        pending_retry_exclusions = None
        reasons.extend(
            _g1_retry_plan_provenance_reasons(
                plan,
                initial_plan=initial_plan or plan,
                initial_decision_id=initial_decision_id,
                exclusions=current_exclusions,
            )
        )
        selected_p = {
            str(identity or "").strip().lower() for identity in plan.get("selected_P") or []
        }
        if selected_p & current_exclusions:
            reasons.append("g1_retry_selected_excluded_proposer")

        derived_failure_rows = _g1_reasoning_only_length_failures(run_map)
        derived_failures = {str(failure.get("identity") or "") for failure in derived_failure_rows}
        recorded_failures = attempt.get("deterministic_proposer_failures")
        normalized_recorded_failures = (
            [dict(failure) for failure in recorded_failures]
            if isinstance(recorded_failures, list)
            and all(isinstance(failure, Mapping) for failure in recorded_failures)
            else None
        )
        if normalized_recorded_failures != derived_failure_rows:
            reasons.append("wrong_g1_deterministic_proposer_failure_evidence")
        if not derived_failures.issubset(selected_p):
            reasons.append("g1_retry_failure_outside_attempt_roster")
        expected_exclusions = current_exclusions | derived_failures

        retry_selection_plan = attempt.get("retry_selection_plan")
        retry_exclusions_raw = attempt.get("retry_excluded_proposer_identities")
        if isinstance(retry_selection_plan, Mapping):
            reasons.extend(
                g1_registry_contract_reasons(
                    {"selection_plan": retry_selection_plan},
                    g1_contract,
                )
            )
            retry_exclusions, retry_exclusions_valid = _normalized_g1_retry_identities(
                retry_exclusions_raw
            )
            if not retry_exclusions_valid or retry_exclusions_raw != sorted(retry_exclusions):
                reasons.append("invalid_g1_retry_next_exclusions")
            if retry_exclusions != expected_exclusions:
                reasons.append("wrong_g1_retry_next_exclusion_evolution")
            retry_selected_p = {
                str(identity or "").strip().lower()
                for identity in retry_selection_plan.get("selected_P") or []
            }
            if retry_selected_p & retry_exclusions:
                reasons.append("g1_retry_selection_selected_excluded_proposer")
            reasons.extend(
                _g1_retry_plan_provenance_reasons(
                    retry_selection_plan,
                    initial_plan=initial_plan or plan,
                    initial_decision_id=initial_decision_id,
                    exclusions=retry_exclusions,
                )
            )
            if attempt.get("will_retry") is not True or not derived_failures:
                reasons.append("unexpected_g1_retry_selection_plan")
            pending_retry_plan = retry_selection_plan
            pending_retry_exclusions = retry_exclusions
        elif retry_selection_plan is not None:
            reasons.append("invalid_g1_retry_selection_plan")
        elif retry_exclusions_raw not in (None, []):
            reasons.append("g1_retry_exclusions_without_selection_plan")
        elif attempt.get("will_retry") is True and derived_failures:
            reasons.append("missing_g1_retry_selection_plan")

        if str(attempt.get("attempt_id") or "") == selected_attempt_id:
            selected_plan = plan
            selected_routing = routing_map
        previous_plan = plan

    if validated_plan_count <= 0:
        reasons.append("missing_g1_attempt_selection_plan")
    if pending_retry_plan is not None:
        reasons.append("dangling_g1_retry_selection_plan")
    if selected_plan is None:
        reasons.append("missing_g1_selected_attempt_selection_plan")
        return {}, {}, list(dict.fromkeys(reasons))
    for physical in physical_plans:
        if not _g1_full_plans_match(selected_plan, physical):
            reasons.append("g1_selected_attempt_plan_differs_from_physical_plan")
        reasons.extend(
            _g1_execution_plan_mutation_reasons(
                selected_plan,
                physical,
            )
        )

    routing = row.get("routing_trace")
    top_routing = dict(routing) if isinstance(routing, Mapping) else {}
    top_plan = top_routing.get("selection_plan")
    if isinstance(top_plan, Mapping):
        reasons.extend(
            g1_registry_contract_reasons(
                {"selection_plan": top_plan},
                g1_contract,
            )
        )
        if not _g1_full_plans_match(selected_plan, top_plan):
            reasons.append("g1_routing_plan_differs_from_selected_attempt")
        reasons.extend(
            _g1_execution_plan_mutation_reasons(
                selected_plan,
                top_plan,
            )
        )
        effective_routing = top_routing
    elif top_routing:
        reasons.append("invalid_g1_top_routing_trace")
        effective_routing = {}
    elif selected_routing:
        effective_routing = dict(selected_routing)
    else:
        effective_routing = {"selection_plan": dict(selected_plan)}

    evidence = {
        "schema": "opensquilla.draco.g1-provider-lifecycle-routing-recovery/v1",
        "source_attempt_id": selected_attempt_id,
        "source_attempt": selected_ordinal,
        "selected_attempt_id": selected_attempt_id,
        "selected_attempt": selected_ordinal,
        "decision_id": str(selected_plan.get("decision_id") or ""),
        "registry_snapshot_hash": str(selected_plan.get("registry_snapshot_hash") or ""),
        "ranking_config_hash": str(selected_plan.get("ranking_config_hash") or ""),
        "validated_attempt_plan_count": validated_plan_count,
        "validated_attempt_physical_plan_count": validated_physical_plan_count,
    }
    return effective_routing, evidence, list(dict.fromkeys(reasons))


def g1_provider_lifecycle_routing_evidence(
    row: Mapping[str, Any],
    *,
    contract: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Recover one frozen G1 plan from the same provider lifecycle, fail closed."""

    if str(row.get("group") or "") != "G1":
        routing = row.get("routing_trace")
        return (
            dict(routing) if isinstance(routing, Mapping) else {},
            {},
            [],
        )
    reasons: list[str] = []
    g1_contract = contract.get("g1_registry_contract") if isinstance(contract, Mapping) else None
    if not isinstance(g1_contract, Mapping):
        return {}, {}, ["missing_g1_registry_contract"]
    trace = row.get("ensemble_trace")
    calls, sequence_reasons = ensemble_call_trace_sequence(
        trace if isinstance(trace, Mapping) else {}
    )
    reasons.extend(sequence_reasons)
    physical_plans: list[Mapping[str, Any]] = []
    for call in calls:
        plan = call.get("selection_plan")
        if not isinstance(plan, Mapping):
            reasons.append("missing_g1_physical_selection_plan")
            continue
        reasons.extend(g1_registry_contract_reasons(call, g1_contract))
        physical_plans.append(plan)
    if not physical_plans:
        reasons.append("missing_g1_physical_selection_plan")
        return {}, {}, list(dict.fromkeys(reasons))
    execution = row.get("execution")
    generation_attempts = (
        execution.get("generation_attempts")
        if isinstance(execution, Mapping) and isinstance(execution.get("generation_attempts"), list)
        else []
    )
    if any(
        isinstance(attempt, Mapping)
        and (
            "selection_plan" in attempt
            or "excluded_proposer_identities" in attempt
            or "deterministic_proposer_failures" in attempt
            or "retry_selection_plan" in attempt
            or "retry_excluded_proposer_identities" in attempt
        )
        for attempt in generation_attempts
    ):
        return _adaptive_g1_provider_lifecycle_routing_evidence(
            row,
            g1_contract=g1_contract,
            physical_plans=physical_plans,
            initial_reasons=reasons,
        )
    first_physical = physical_plans[0]
    for plan in physical_plans[1:]:
        if not _g1_plans_match(plan, first_physical):
            reasons.append("conflicting_g1_physical_selection_plans")

    routing = row.get("routing_trace")
    top_routing = dict(routing) if isinstance(routing, Mapping) else {}
    top_plan = top_routing.get("selection_plan")
    if isinstance(top_plan, Mapping):
        reasons.extend(
            g1_registry_contract_reasons(
                {"selection_plan": top_plan},
                g1_contract,
            )
        )
        if not _g1_plans_match(top_plan, first_physical):
            reasons.append("g1_routing_plan_differs_from_physical_plan")
        return top_routing, {}, list(dict.fromkeys(reasons))
    if top_routing:
        reasons.append("invalid_g1_top_routing_trace")
        return {}, {}, list(dict.fromkeys(reasons))

    matching_attempts = matching_saved_generation_attempts(row)
    if len(matching_attempts) != 1:
        reasons.append("ambiguous_g1_selected_generation_attempt")
        return {}, {}, list(dict.fromkeys(reasons))
    selected_attempt = matching_attempts[0]
    selected_ordinal = coerce_metric_int(selected_attempt.get("attempt"))
    execution = row.get("execution")
    attempts = (
        execution.get("generation_attempts")
        if isinstance(execution, Mapping) and isinstance(execution.get("generation_attempts"), list)
        else []
    )
    candidates: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for attempt in attempts:
        if not isinstance(attempt, Mapping):
            continue
        run = attempt.get("run")
        attempt_routing = run.get("routing_trace") if isinstance(run, Mapping) else None
        plan = (
            attempt_routing.get("selection_plan") if isinstance(attempt_routing, Mapping) else None
        )
        if not isinstance(plan, Mapping):
            continue
        plan_reasons = g1_registry_contract_reasons(
            {"selection_plan": plan},
            g1_contract,
        )
        if plan_reasons:
            reasons.extend(plan_reasons)
            continue
        if not _g1_plans_match(plan, first_physical):
            reasons.append("g1_lifecycle_plan_differs_from_physical_plan")
            continue
        if coerce_metric_int(attempt.get("attempt")) > selected_ordinal:
            continue
        candidates.append((attempt, attempt_routing))
    unique_candidates = {
        canonical_json_sha256(dict(candidate_routing)): (attempt, candidate_routing)
        for attempt, candidate_routing in candidates
    }
    if len(unique_candidates) != 1:
        reasons.append("ambiguous_g1_lifecycle_routing_plan")
        return {}, {}, list(dict.fromkeys(reasons))
    attempt, recovered = next(iter(unique_candidates.values()))
    evidence = {
        "schema": "opensquilla.draco.g1-provider-lifecycle-routing-recovery/v1",
        "source_attempt_id": str(attempt.get("attempt_id") or ""),
        "source_attempt": coerce_metric_int(attempt.get("attempt")),
        "selected_attempt_id": str(selected_attempt.get("attempt_id") or ""),
        "selected_attempt": selected_ordinal,
        "decision_id": str(first_physical.get("decision_id") or ""),
        "registry_snapshot_hash": str(first_physical.get("registry_snapshot_hash") or ""),
        "ranking_config_hash": str(first_physical.get("ranking_config_hash") or ""),
    }
    return dict(recovered), evidence, list(dict.fromkeys(reasons))


def g1_provider_lifecycle_analyzer_reasons(row: Mapping[str, Any]) -> list[str]:
    """Require one valid analyzer in the setup-bearing attempt of each G1 lifecycle."""

    from opensquilla.provider.ranking_router import (
        TASK_ANALYZER_MODEL_ID,
        TASK_ANALYZER_PROVIDER_ID,
    )

    if str(row.get("group") or "") != "G1":
        return []
    execution = row.get("execution")
    attempts = (
        execution.get("generation_attempts")
        if isinstance(execution, Mapping) and isinstance(execution.get("generation_attempts"), list)
        else []
    )
    request_attempts = [
        attempt
        for attempt in attempts
        if isinstance(attempt, Mapping)
        and isinstance(attempt.get("run"), Mapping)
        and coerce_metric_int(attempt["run"].get("llm_request_count")) > 0
    ]
    if not request_attempts:
        return ["missing_g1_provider_lifecycle_attempt"]
    first_units = (
        request_attempts[0]["run"].get("usage", {}).get("model_usage_breakdown")
        if isinstance(request_attempts[0]["run"].get("usage"), Mapping)
        else None
    )
    analyzers = (
        [
            unit
            for unit in first_units
            if isinstance(unit, Mapping)
            and str(unit.get("role") or "").strip().casefold() == "task_analyzer"
        ]
        if isinstance(first_units, list)
        else []
    )
    if not analyzers:
        return ["missing_g1_task_analyzer_request"]
    for analyzer in analyzers:
        if (
            str(analyzer.get("provider") or "").strip().casefold()
            != TASK_ANALYZER_PROVIDER_ID.casefold()
            or str(analyzer.get("requested_provider") or "").strip().casefold()
            != TASK_ANALYZER_PROVIDER_ID.casefold()
            or not _formal_openrouter_models_equivalent(
                analyzer.get("model"),
                TASK_ANALYZER_MODEL_ID,
            )
            or not _formal_openrouter_models_equivalent(
                analyzer.get("requested_model"),
                TASK_ANALYZER_MODEL_ID,
            )
        ):
            return ["wrong_g1_task_analyzer_route"]
    for attempt in request_attempts[1:]:
        usage = attempt["run"].get("usage")
        breakdown = usage.get("model_usage_breakdown") if isinstance(usage, Mapping) else None
        if isinstance(breakdown, list) and any(
            isinstance(unit, Mapping)
            and str(unit.get("role") or "").strip().casefold() == "task_analyzer"
            for unit in breakdown
        ):
            return ["repeated_g1_task_analyzer_request"]
    return []


G1_CROSS_WAVE_FROZEN_LIFECYCLE_SCHEMA = (
    "opensquilla.draco.g1-cross-wave-frozen-lifecycle/v1"
)


def _g1_frozen_lifecycle_plan_reasons(
    plan: Any,
    *,
    g1_contract: Mapping[str, Any],
) -> list[str]:
    """Validate one serialized adaptive plan against replay and route freezes."""

    if not isinstance(plan, Mapping):
        return ["missing_g1_attempt_selection_plan"]
    from opensquilla.provider.ranking_router import ranking_trace_replay_reasons

    return list(
        dict.fromkeys(
            [
                *g1_registry_contract_reasons(
                    {"selection_plan": plan},
                    g1_contract,
                ),
                *ranking_trace_replay_reasons(plan),
            ]
        )
    )


def _g1_frozen_lifecycle_analyzer_reasons(
    attempts: Sequence[Mapping[str, Any]],
    *,
    initial_plan: Mapping[str, Any],
) -> list[str]:
    """Require analyzer receipts only on the first paid lifecycle attempt."""

    from opensquilla.provider.ranking_router import (
        TASK_ANALYZER_MODEL_ID,
        TASK_ANALYZER_PROVIDER_ID,
    )

    if not attempts:
        return ["missing_g1_provider_lifecycle_attempt"]
    def analyzer_units_from_usage(run: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [
            unit
            for unit in canonical_run_usage_units(
                run,
                identity_seed="frozen-g1-analyzer-usage",
            )
            if (
                str(unit.get("role") or "").strip().casefold()
                == "task_analyzer"
                or _g1_is_unknown_task_analyzer_placeholder(unit)
            )
        ]

    def analyzer_units_from_setup(run: Mapping[str, Any]) -> list[dict[str, Any]]:
        raw_setup = run.get("setup_usage")
        setup_rows = (
            [
                copy.deepcopy(dict(row))
                for row in raw_setup
                if isinstance(row, Mapping)
                and (
                    str(row.get("role") or "").strip().casefold()
                    == "task_analyzer"
                    or (
                        str(row.get("role") or "").strip().casefold()
                        == "unknown_request"
                        and str(row.get("label") or "").strip().casefold()
                        == "task_analyzer"
                    )
                )
            ]
            if isinstance(raw_setup, list)
            else []
        )
        if not setup_rows:
            return []
        return [
            unit
            for unit in canonical_run_usage_units(
                {"usage": {"model_usage_breakdown": setup_rows}},
                identity_seed="frozen-g1-analyzer-setup",
            )
            if (
                str(unit.get("role") or "").strip().casefold()
                == "task_analyzer"
                or _g1_is_unknown_task_analyzer_placeholder(unit)
            )
        ]

    analyzer_units_by_attempt: list[list[dict[str, Any]]] = []
    for attempt_index, attempt in enumerate(attempts):
        run = attempt.get("run")
        run_map = run if isinstance(run, Mapping) else {}
        usage_units = analyzer_units_from_usage(run_map)
        setup_units = analyzer_units_from_setup(run_map)
        if attempt_index == 0 and usage_units and not setup_units:
            return ["missing_g1_task_analyzer_setup_usage"]
        if attempt_index == 0 and setup_units and not usage_units:
            return ["missing_g1_task_analyzer_aggregate_usage"]
        if setup_units != usage_units:
            return ["g1_task_analyzer_setup_usage_mismatch"]
        analyzer_units_by_attempt.append(usage_units)
    first_units = analyzer_units_by_attempt[0]
    if not first_units:
        return ["missing_g1_task_analyzer_request"]
    attempt_ordinals = [unit.get("attempt") for unit in first_units]
    attempt_ids = [str(unit.get("physical_attempt_id") or "") for unit in first_units]
    ranking_parameters = initial_plan.get("ranking_parameters")
    analyzer_config = (
        ranking_parameters.get("task_analyzer")
        if isinstance(ranking_parameters, Mapping)
        else None
    )
    max_retries = (
        analyzer_config.get("max_retries")
        if isinstance(analyzer_config, Mapping)
        else None
    )
    if (
        not isinstance(max_retries, int)
        or isinstance(max_retries, bool)
        or max_retries < 0
        or len(first_units) > max_retries + 1
        or attempt_ordinals != list(range(1, len(first_units) + 1))
        or len(set(attempt_ids)) != len(attempt_ids)
        or any(
            len(attempt_id) != 32
            or any(character not in "0123456789abcdef" for character in attempt_id)
            for attempt_id in attempt_ids
        )
    ):
        return ["invalid_g1_task_analyzer_attempt_sequence"]
    for unit in first_units:
        if _g1_is_unknown_task_analyzer_placeholder(unit):
            continue
        if (
            str(unit.get("role") or "").strip().casefold() != "task_analyzer"
            or str(unit.get("provider") or "").strip().casefold()
            != TASK_ANALYZER_PROVIDER_ID.casefold()
            or str(unit.get("requested_provider") or "").strip().casefold()
            != TASK_ANALYZER_PROVIDER_ID.casefold()
            or not _formal_openrouter_models_equivalent(
                unit.get("model"),
                TASK_ANALYZER_MODEL_ID,
            )
            or not _formal_openrouter_models_equivalent(
                unit.get("requested_model"),
                TASK_ANALYZER_MODEL_ID,
            )
        ):
            return ["wrong_g1_task_analyzer_route"]
    if any(analyzer_units_by_attempt[1:]):
        return ["repeated_g1_task_analyzer_request"]
    return []


def _g1_thinking_physical_usage_binding_audit(
    run: Mapping[str, Any],
    *,
    identity_seed: str,
) -> tuple[list[str], list[str]]:
    """Bind strict managed-thinking request IDs before authorizing a resume."""

    from opensquilla.provider.thinking_execution import (
        THINKING_PHYSICAL_EVIDENCE_SCHEMA,
    )

    trace = run.get("ensemble_trace")
    calls, call_reasons = ensemble_call_trace_sequence(
        trace if isinstance(trace, Mapping) else {}
    )
    strict_calls = [
        call
        for call in calls
        if isinstance(call.get("selection_plan"), Mapping)
        and call["selection_plan"].get(
            "thinking_physical_evidence_schema"
        )
        == THINKING_PHYSICAL_EVIDENCE_SCHEMA
    ]
    if not strict_calls:
        return [], []

    reasons = list(call_reasons)
    if len(strict_calls) != len(calls):
        reasons.append("mixed_g1_thinking_physical_evidence_schema")

    ledger_ids: list[str] = []
    for call in strict_calls:
        candidates = call.get("candidates")
        if isinstance(candidates, list):
            for candidate in candidates:
                execution = (
                    candidate.get("execution")
                    if isinstance(candidate, Mapping)
                    else None
                )
                physical_attempts = (
                    execution.get("physical_attempts")
                    if isinstance(execution, Mapping)
                    else None
                )
                if isinstance(physical_attempts, list):
                    ledger_ids.extend(
                        str(
                            physical_attempt.get(
                                "physical_attempt_id"
                            )
                            or ""
                        )
                        for physical_attempt in physical_attempts
                        if isinstance(
                            physical_attempt,
                            Mapping,
                        )
                        and physical_attempt.get("request_started")
                        is True
                    )
        recovery = call.get("aggregator_recovery")
        recovery_attempts = (
            recovery.get("attempts")
            if isinstance(recovery, Mapping)
            else None
        )
        if isinstance(recovery_attempts, list):
            ledger_ids.extend(
                str(
                    physical_attempt.get("physical_attempt_id")
                    or ""
                )
                for physical_attempt in recovery_attempts
                if isinstance(physical_attempt, Mapping)
                and physical_attempt.get("request_started") is True
            )

    def is_physical_attempt_id(value: str) -> bool:
        return (
            len(value) == 32
            and all(
                character in "0123456789abcdef"
                for character in value
            )
        )

    if (
        any(
            not is_physical_attempt_id(attempt_id)
            for attempt_id in ledger_ids
        )
        or len(ledger_ids) != len(set(ledger_ids))
    ):
        reasons.append("invalid_g1_thinking_physical_attempt_set")

    units = canonical_run_usage_units(
        run,
        identity_seed=identity_seed,
    )
    analyzer_units = _g1_canonical_task_analyzer_setup_units(
        run,
        identity_seed=identity_seed,
    )
    analyzer_ids = {
        str(unit.get("physical_attempt_id") or "")
        for unit in analyzer_units
    }
    analyzer_ids.discard("")
    generation_units = [
        unit
        for unit in units
        if str(unit.get("role") or "").strip().casefold()
        not in {"task_analyzer", "task_analyzer_attempt"}
        and str(unit.get("physical_attempt_id") or "")
        not in analyzer_ids
    ]
    usage_ids: list[str] = []
    for unit in generation_units:
        attempt_id = str(unit.get("physical_attempt_id") or "")
        provider_usage = unit.get("provider_usage")
        nested_id = (
            str(provider_usage.get("physical_attempt_id") or "")
            if isinstance(provider_usage, Mapping)
            else ""
        )
        if (
            not is_physical_attempt_id(attempt_id)
            or nested_id != attempt_id
        ):
            reasons.append(
                "invalid_g1_thinking_usage_physical_attempt_id"
            )
            continue
        usage_ids.append(attempt_id)

    if Counter(usage_ids) != Counter(ledger_ids):
        reasons.append("g1_thinking_physical_usage_set_mismatch")
    expected = g1_run_expected_ensemble_request_count(run)
    if (
        len(ledger_ids) != expected
        or len(generation_units) != expected
    ):
        reasons.append(
            "g1_thinking_physical_usage_multiplicity_mismatch"
        )
    return (
        list(dict.fromkeys(reasons)),
        [
            *(
                attempt_id
                for attempt_id in sorted(analyzer_ids)
                if is_physical_attempt_id(attempt_id)
            ),
            *ledger_ids,
        ],
    )


def reconstruct_g1_cross_wave_frozen_lifecycle(
    *,
    attempts: Sequence[Mapping[str, Any]],
    current_run_compatibility_contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Reconstruct the exact paid G1 routing state shared by all prior Waves.

    Only serialized strict-v1 physical-attempt evidence reaches this helper.
    It replays every frozen ranker decision, validates A→B→C retry continuity,
    and derives the one plan/exclusion/execution-prefix state that a later
    Wave may safely consume without another analyzer request.
    """

    if not g1_cross_wave_frozen_lifecycle_enabled(
        current_run_compatibility_contract
    ):
        raise ValueError("G1 frozen lifecycle reconstruction is not enabled")
    g1_contract = current_run_compatibility_contract.get("g1_registry_contract")
    if not isinstance(g1_contract, Mapping):
        raise ValueError("G1 frozen lifecycle lacks the current registry contract")
    ordered = sorted(
        (copy.deepcopy(dict(attempt)) for attempt in attempts),
        key=lambda attempt: coerce_metric_int(attempt.get("attempt")),
    )
    if not ordered:
        raise ValueError("G1 frozen lifecycle has no physical generation attempts")
    reasons: list[str] = []
    initial_plan: Mapping[str, Any] | None = None
    initial_decision_id = ""
    previous_plan: Mapping[str, Any] | None = None
    pending_plan: Mapping[str, Any] | None = None
    pending_exclusions: set[str] | None = None
    expected_exclusions: set[str] = set()
    execution_prefixes: dict[str, dict[str, Any]] = {}
    thinking_execution_history: list[dict[str, Any]] = []
    last_plan: Mapping[str, Any] | None = None
    tail_deterministic_failures_without_pending = False
    seen_physical_attempt_ids: set[str] = set()

    for expected_ordinal, attempt in enumerate(ordered, start=1):
        ordinal = coerce_metric_int(attempt.get("attempt"))
        if ordinal != expected_ordinal:
            reasons.append("invalid_g1_attempt_ordinal_sequence")
        run = attempt.get("run")
        run_map = run if isinstance(run, Mapping) else {}
        trace_events = run_map.get("trace_events")
        routing_trace = run_map.get("routing_trace")
        if (
            any(
                isinstance(event, Mapping)
                and event.get("code") == "g1_pre_call_guard_failed"
                for event in (
                    trace_events if isinstance(trace_events, list) else []
                )
            )
            or (
                isinstance(routing_trace, Mapping)
                and isinstance(
                    routing_trace.get("pre_call_guard"),
                    Mapping,
                )
            )
        ):
            reasons.append("g1_pre_call_guard_failed")
            continue
        if attempt.get("attempt_kind") != "generation":
            reasons.append("g1_paid_setup_attempt_lacks_frozen_lifecycle")
            continue
        (
            physical_binding_reasons,
            physical_attempt_ids,
        ) = _g1_thinking_physical_usage_binding_audit(
            run_map,
            identity_seed=(
                "generation-attempt:"
                + str(attempt.get("attempt_id") or "")
            ),
        )
        reasons.extend(physical_binding_reasons)
        for physical_attempt_id in dict.fromkeys(
            physical_attempt_ids
        ):
            if physical_attempt_id in seen_physical_attempt_ids:
                reasons.append(
                    "duplicate_cross_attempt_g1_thinking_"
                    "physical_attempt_id"
                )
            seen_physical_attempt_ids.add(physical_attempt_id)
        plan = attempt.get("selection_plan")
        if not isinstance(plan, Mapping):
            reasons.append("missing_g1_attempt_selection_plan")
            continue
        if plan.get("ranking_thinking_assignment_enabled") is not True:
            reasons.append("g1_frozen_lifecycle_thinking_switch_not_enabled")
        reasons.extend(
            _g1_frozen_lifecycle_plan_reasons(
                plan,
                g1_contract=g1_contract,
            )
        )
        decision_id = str(plan.get("decision_id") or "")
        if not initial_decision_id:
            initial_plan = plan
            initial_decision_id = decision_id
        if not decision_id:
            reasons.append("missing_g1_retry_decision_id")

        if pending_plan is not None:
            if not _g1_full_plans_match(plan, pending_plan):
                reasons.append("g1_retry_selection_plan_not_used_by_next_attempt")
            reasons.extend(
                _g1_execution_plan_mutation_reasons(
                    pending_plan,
                    plan,
                )
            )
        elif previous_plan is not None and not _g1_full_plans_match(
            plan,
            previous_plan,
        ):
            reasons.append("g1_attempt_plan_changed_without_retry_selection")

        prior_execution_prefix = execution_prefixes.get(decision_id)
        if prior_execution_prefix is not None:
            if (
                plan.get("executed_thinking_assignment")
                != prior_execution_prefix.get("executed_thinking_assignment")
                or plan.get("thinking_execution_fallbacks", [])
                != prior_execution_prefix.get("thinking_execution_fallbacks", [])
            ):
                reasons.append("g1_cross_wave_thinking_execution_prefix_reset")

        declared_exclusions_raw = attempt.get("excluded_proposer_identities")
        declared_exclusions, exclusions_valid = _normalized_g1_retry_identities(
            declared_exclusions_raw
        )
        if (
            not exclusions_valid
            or declared_exclusions_raw != sorted(declared_exclusions)
        ):
            reasons.append("noncanonical_g1_attempt_exclusions")
        if declared_exclusions != expected_exclusions:
            reasons.append("wrong_g1_retry_exclusion_evolution")
        if (
            pending_exclusions is not None
            and declared_exclusions != pending_exclusions
        ):
            reasons.append("g1_retry_exclusions_not_used_by_next_attempt")
        reasons.extend(
            _g1_retry_plan_provenance_reasons(
                plan,
                initial_plan=initial_plan or plan,
                initial_decision_id=initial_decision_id,
                exclusions=declared_exclusions,
            )
        )
        selected_p = {
            str(identity or "").strip().lower()
            for identity in plan.get("selected_P") or []
        }
        if selected_p & declared_exclusions:
            reasons.append("g1_retry_selected_excluded_proposer")

        routing = run_map.get("routing_trace")
        routing_map = routing if isinstance(routing, Mapping) else {}
        routed_plan = routing_map.get("selection_plan")
        if not isinstance(routed_plan, Mapping):
            reasons.append("missing_g1_attempt_routing_plan")
        else:
            reasons.extend(
                _g1_frozen_lifecycle_plan_reasons(
                    routed_plan,
                    g1_contract=g1_contract,
                )
            )
            if not _g1_full_plans_match(plan, routed_plan):
                reasons.append("g1_attempt_selection_plan_differs_from_routing")
            reasons.extend(
                _g1_execution_plan_mutation_reasons(
                    plan,
                    routed_plan,
                )
            )

        attempt_trace = run_map.get("ensemble_trace")
        calls, call_reasons = ensemble_call_trace_sequence(
            attempt_trace if isinstance(attempt_trace, Mapping) else {}
        )
        reasons.extend(call_reasons)
        if not calls and g1_run_expected_ensemble_request_count(run_map) > 0:
            reasons.append("missing_g1_attempt_ensemble_trace")
        physical_prefix: Mapping[str, Any] = (
            prior_execution_prefix
            if prior_execution_prefix is not None
            else plan
        )
        for call in calls:
            physical_plan = call.get("selection_plan")
            if not isinstance(physical_plan, Mapping):
                reasons.append("missing_g1_attempt_physical_selection_plan")
                continue
            reasons.extend(
                _g1_frozen_lifecycle_plan_reasons(
                    physical_plan,
                    g1_contract=g1_contract,
                )
            )
            if not _g1_full_plans_match(plan, physical_plan):
                reasons.append(
                    "g1_attempt_selection_plan_differs_from_physical_plan"
                )
            reasons.extend(
                _g1_execution_plan_mutation_reasons(
                    physical_prefix,
                    physical_plan,
                )
            )
            from opensquilla.provider.thinking_execution import (
                validate_thinking_execution_call,
            )

            validated_physical_plan, physical_execution_reason = (
                validate_thinking_execution_call(
                    physical_prefix,
                    call,
                )
            )
            if physical_execution_reason:
                reasons.append(
                    "invalid_g1_physical_thinking_execution:"
                    + physical_execution_reason
                )
            else:
                physical_prefix = validated_physical_plan
            thinking_execution_history.append(
                copy.deepcopy(dict(physical_plan))
            )
        if calls and decision_id:
            execution_prefixes[decision_id] = copy.deepcopy(dict(physical_prefix))

        derived_failure_rows = _g1_reasoning_only_length_failures(run_map)
        recorded_failures = attempt.get("deterministic_proposer_failures")
        normalized_recorded_failures = (
            [dict(failure) for failure in recorded_failures]
            if isinstance(recorded_failures, list)
            and all(isinstance(failure, Mapping) for failure in recorded_failures)
            else None
        )
        if normalized_recorded_failures != derived_failure_rows:
            reasons.append("wrong_g1_deterministic_proposer_failure_evidence")
        derived_failures = {
            str(failure.get("identity") or "") for failure in derived_failure_rows
        }
        if not derived_failures.issubset(selected_p):
            reasons.append("g1_retry_failure_outside_attempt_roster")
        next_exclusions = declared_exclusions | derived_failures

        retry_plan = attempt.get("retry_selection_plan")
        retry_exclusions_raw = attempt.get("retry_excluded_proposer_identities")
        retry_deferred = attempt.get("retry_deferred_to_next_wave")
        if retry_deferred is not None and type(retry_deferred) is not bool:
            reasons.append("invalid_g1_retry_deferred_marker")
        pending_plan = None
        pending_exclusions = None
        if isinstance(retry_plan, Mapping):
            from opensquilla.provider.thinking_execution import (
                validate_thinking_execution_history_closure,
            )

            (
                expected_retry_projection,
                expected_projection_audit,
                retry_projection_reason,
            ) = validate_thinking_execution_history_closure(
                thinking_execution_history,
                retry_plan,
            )
            if retry_projection_reason:
                reasons.append(
                    "invalid_g1_thinking_execution_projection:"
                    + retry_projection_reason
                )
            elif (
                retry_plan.get("executed_thinking_assignment")
                != expected_retry_projection.get(
                    "executed_thinking_assignment"
                )
                or retry_plan.get("thinking_execution_fallbacks", [])
                != expected_retry_projection.get(
                    "thinking_execution_fallbacks",
                    [],
                )
            ):
                reasons.append(
                    "g1_retry_thinking_execution_projection_differs"
                )
            if (
                attempt.get("thinking_execution_projection")
                != expected_projection_audit
            ):
                reasons.append(
                    "wrong_g1_thinking_execution_projection_audit"
                )
            reasons.extend(
                _g1_frozen_lifecycle_plan_reasons(
                    retry_plan,
                    g1_contract=g1_contract,
                )
            )
            retry_exclusions, retry_exclusions_valid = (
                _normalized_g1_retry_identities(retry_exclusions_raw)
            )
            if (
                not retry_exclusions_valid
                or retry_exclusions_raw != sorted(retry_exclusions)
            ):
                reasons.append("invalid_g1_retry_next_exclusions")
            if retry_exclusions != next_exclusions:
                reasons.append("wrong_g1_retry_next_exclusion_evolution")
            reasons.extend(
                _g1_retry_plan_provenance_reasons(
                    retry_plan,
                    initial_plan=initial_plan or plan,
                    initial_decision_id=initial_decision_id,
                    exclusions=retry_exclusions,
                )
            )
            retry_selected = {
                str(identity or "").strip().lower()
                for identity in retry_plan.get("selected_P") or []
            }
            if retry_selected & retry_exclusions:
                reasons.append("g1_retry_selection_selected_excluded_proposer")
            if (
                (attempt.get("will_retry") is True)
                == (retry_deferred is True)
                or not derived_failures
            ):
                reasons.append("unexpected_g1_retry_selection_plan")
            pending_plan = retry_plan
            pending_exclusions = retry_exclusions
        elif retry_plan is not None:
            reasons.append("invalid_g1_retry_selection_plan")
        elif retry_exclusions_raw not in (None, []):
            reasons.append("g1_retry_exclusions_without_selection_plan")
        elif retry_deferred is True:
            reasons.append("g1_retry_deferred_without_selection_plan")
        elif attempt.get("will_retry") is True and derived_failures:
            reasons.append("missing_g1_retry_selection_plan")

        tail_deterministic_failures_without_pending = bool(
            derived_failures and pending_plan is None
        )
        expected_exclusions = next_exclusions
        previous_plan = plan
        last_plan = plan

    if tail_deterministic_failures_without_pending:
        reasons.append("g1_deterministic_failure_lacks_pending_retry_plan")
    if initial_plan is not None:
        reasons.extend(
            _g1_frozen_lifecycle_analyzer_reasons(
                ordered,
                initial_plan=initial_plan,
            )
        )
    if initial_plan is None or last_plan is None:
        reasons.append("missing_g1_attempt_selection_plan")
    last_decision_id = (
        str(last_plan.get("decision_id") or "")
        if isinstance(last_plan, Mapping)
        else ""
    )
    last_physical_prefix = execution_prefixes.get(last_decision_id)
    target_plan = (
        pending_plan
        or last_physical_prefix
        or last_plan
    )
    projected_target_prefix: dict[str, Any] | None = None
    projection_audit: dict[str, Any] | None = None
    if isinstance(target_plan, Mapping):
        from opensquilla.provider.thinking_execution import (
            validate_thinking_execution_history_closure,
        )

        (
            projected_target_prefix,
            projection_audit,
            projection_reason,
        ) = validate_thinking_execution_history_closure(
            thinking_execution_history,
            target_plan,
        )
        if projection_reason:
            reasons.append(
                "g1_thinking_execution_projection_failed:"
                + projection_reason
            )
        elif (
            target_plan.get("executed_thinking_assignment")
            != projected_target_prefix.get(
                "executed_thinking_assignment"
            )
            or target_plan.get("thinking_execution_fallbacks", [])
            != projected_target_prefix.get(
                "thinking_execution_fallbacks",
                [],
            )
        ):
            reasons.append(
                "g1_target_thinking_execution_projection_differs"
            )
    if reasons:
        unique_reasons = list(dict.fromkeys(reasons))
        raise ValueError(
            "invalid cross-Wave frozen G1 lifecycle: "
            + ",".join(unique_reasons)
        )

    target_exclusions = pending_exclusions or expected_exclusions
    return {
        "schema": G1_CROSS_WAVE_FROZEN_LIFECYCLE_SCHEMA,
        "attempt_count": len(ordered),
        "attempt_ids": [str(attempt.get("attempt_id") or "") for attempt in ordered],
        "initial_parent_decision_id": initial_decision_id,
        "initial_plan": copy.deepcopy(dict(initial_plan)),
        "target_plan": copy.deepcopy(dict(target_plan)),
        "target_plan_source": (
            "pending_retry_selection_plan"
            if pending_plan is not None
            else "last_physical_attempt"
        ),
        "cumulative_excluded_proposer_identities": sorted(target_exclusions),
        "target_execution_prefix": copy.deepcopy(projected_target_prefix),
        "thinking_execution_projection": copy.deepcopy(projection_audit),
        "thinking_execution_history": copy.deepcopy(
            thinking_execution_history
        ),
        "task_analyzer_physical_request_count": len(
            _g1_canonical_task_analyzer_setup_units(
                ordered[0].get("run")
                if isinstance(ordered[0].get("run"), Mapping)
                else {},
                identity_seed=(
                    "generation-attempt:"
                    + str(ordered[0].get("attempt_id") or "")
                ),
            )
        ),
    }


def ensemble_generation_completion_reasons(
    row: dict[str, Any],
    *,
    expected_run_compatibility_contract: Mapping[str, Any] | None = None,
) -> list[str]:
    """Require a quorum-backed, completed aggregator for ensemble generations."""

    group = str(row.get("group") or "")
    spec = GROUP_SPECS.get(group) or {}
    trace = row.get("ensemble_trace")
    requires_ensemble_proof = spec.get("kind") == "selection_mode"
    if not requires_ensemble_proof:
        return []
    if not isinstance(trace, dict) or not trace:
        return ["missing_ensemble_trace"]

    traces, sequence_reasons = ensemble_call_trace_sequence(trace)
    if not traces:
        return sequence_reasons or ["missing_ensemble_call_trace"]
    final_trace = traces[-1]
    reasons: list[str] = list(sequence_reasons)
    reasons.extend(
        agent_call_output_sequence_reasons(
            traces,
            final_text=str(row.get("final_text") or ""),
        )
    )
    expected_spec = GROUP_SPECS.get(group) or {}
    routing_trace = row.get("routing_trace")
    if (
        group == "G1"
        and isinstance(expected_run_compatibility_contract, Mapping)
        and isinstance(
            expected_run_compatibility_contract.get("g1_registry_contract"),
            Mapping,
        )
    ):
        lifecycle_routing, _, lifecycle_reasons = g1_provider_lifecycle_routing_evidence(
            row,
            contract=expected_run_compatibility_contract,
        )
        reasons.extend(lifecycle_reasons)
        routing_trace = lifecycle_routing
        reasons.extend(g1_provider_lifecycle_analyzer_reasons(row))
    expected_plan = (
        routing_trace.get("selection_plan") if isinstance(routing_trace, Mapping) else None
    )
    expected_selection_mode = str(expected_spec.get("selection_mode") or "")
    expected_g1_registry_contract = (
        expected_run_compatibility_contract.get("g1_registry_contract")
        if group == "G1" and isinstance(expected_run_compatibility_contract, Mapping)
        else None
    )

    # A nonterminal zero-text fallback may drive tool calls without becoming
    # part of the answer.  Its route and physical evidence remain strict; only
    # the expected fallback/quorum outcome markers are admissible.  The final
    # call always remains a quorum-backed aggregator bound to final_text.
    for index, call_trace in enumerate(traces):
        call_reasons = ensemble_call_core_reasons(
            call_trace,
            expected_selection_mode=expected_selection_mode,
            expected_selection_plan=(expected_plan if isinstance(expected_plan, Mapping) else None),
            expected_g1_registry_contract=(
                expected_g1_registry_contract
                if isinstance(expected_g1_registry_contract, Mapping)
                else None
            ),
            final_text=str(row.get("final_text") or ""),
            require_output_binding=index == len(traces) - 1,
        )
        if index < len(traces) - 1 and call_trace.get("fallback_used") is True:
            fallback_reasons = admissible_empty_nonterminal_fallback_reasons(
                call_trace,
                expected_selection_plan=(
                    expected_plan if isinstance(expected_plan, Mapping) else None
                ),
            )
            reasons.extend(fallback_reasons)
            if not fallback_reasons:
                call_reasons = [
                    reason
                    for reason in call_reasons
                    if reason not in _ADMISSIBLE_NONTERMINAL_FALLBACK_CORE_REASONS
                ]
        reasons.extend(call_reasons)

    agent_call_count = coerce_metric_int(trace.get("agent_llm_call_count"))
    final_call_index = coerce_metric_int(final_trace.get("agent_call_index"))
    if agent_call_count and final_call_index and final_call_index != agent_call_count:
        reasons.append("final_agent_call_ensemble_trace_missing")
    if str(final_trace.get("request_outcome") or "llm_response") != "llm_response":
        reasons.append("aggregator_call_error")
    if final_trace.get("fallback_used") is not False:
        reasons.append("aggregator_fallback_used_or_unknown")
    if str(final_trace.get("final_request_role") or "") != "aggregator":
        reasons.append("final_request_not_aggregator")

    total = final_trace.get("total_candidates")
    successful = final_trace.get("successful_proposers")
    if (
        isinstance(total, bool)
        or not isinstance(total, int)
        or total <= 0
        or isinstance(successful, bool)
        or not isinstance(successful, int)
        or successful > total
        or successful < legal_proposer_quorum(total)
    ):
        reasons.append("insufficient_proposer_quorum")

    final_request = final_trace.get("final_request")
    if not isinstance(final_request, dict):
        reasons.append("missing_final_aggregator_request")
        return list(dict.fromkeys(reasons))
    if str(final_request.get("role") or "") != "aggregator":
        reasons.append("final_request_trace_not_aggregator")
    if final_request.get("request_started") is not True:
        reasons.append("aggregator_request_not_started")
    if final_request.get("error") or final_trace.get("aggregator_error"):
        reasons.append("aggregator_request_error")

    usage = final_request.get("usage")
    output = (
        final_trace.get("assembled_output")
        if final_trace.get("output_binding_schema") == "opensquilla.ensemble-output-binding/v1"
        else final_request.get("output")
    )
    if (
        not isinstance(output, dict)
        or coerce_metric_int(output.get("chars")) <= 0
        or not str(output.get("text") or "").strip()
    ):
        reasons.append("aggregator_done_output_missing")
    elif str(row.get("final_text") or "").strip():
        output_text = str(output.get("text") or "")
        final_text = str(row.get("final_text") or "")
        output_chars = coerce_metric_int(output.get("chars"))
        final_segment = (
            final_text[-output_chars:]
            if output_chars > 0 and len(final_text) >= output_chars
            else ""
        )
        output_matches = (
            final_segment.startswith(output_text)
            if output.get("truncated") is True
            else final_segment == output_text
        )
        if not output_matches:
            reasons.append("aggregator_output_mismatch")

    provider_spec = row.get("provider_spec")
    if not isinstance(provider_spec, dict):
        reasons.append("missing_provider_spec")
    else:
        for key in ("kind", "selection_mode", "model"):
            if key in expected_spec and provider_spec.get(key) != expected_spec.get(key):
                reasons.append(f"wrong_provider_spec_{key}")

    final_selection_mode = str(
        final_trace.get("selection_strategy")
        or (
            final_trace.get("selection_plan", {}).get("strategy")
            if isinstance(final_trace.get("selection_plan"), dict)
            else ""
        )
        or ""
    )
    if expected_selection_mode and final_selection_mode != expected_selection_mode:
        reasons.append("wrong_executed_selection_mode")

    executed_plan = final_trace.get("selection_plan")
    if spec.get("kind") == "selection_mode" and not isinstance(expected_plan, dict):
        reasons.append("missing_expected_selection_plan")
    if spec.get("kind") == "selection_mode" and not isinstance(executed_plan, dict):
        reasons.append("missing_executed_selection_plan")
    if isinstance(expected_plan, dict) and isinstance(executed_plan, dict):
        expected_models_value = expected_plan.get("proposer_models")
        normalized_expected_models = (
            [model.strip() if isinstance(model, str) else "" for model in expected_models_value]
            if isinstance(expected_models_value, list)
            else []
        )
        expected_selected_p_value = expected_plan.get("selected_P")
        normalized_expected_selected_p = (
            [
                identity.strip() if isinstance(identity, str) else ""
                for identity in expected_selected_p_value
            ]
            if isinstance(expected_selected_p_value, list)
            else []
        )
        raw_aggregator_model = expected_plan.get("aggregator_model")
        expected_aggregator_model_value = (
            raw_aggregator_model.strip() if isinstance(raw_aggregator_model, str) else ""
        )
        raw_selected_a = expected_plan.get("selected_A")
        expected_selected_a_value = (
            raw_selected_a.strip() if isinstance(raw_selected_a, str) else ""
        )
        raw_sample_count = expected_plan.get("proposer_sample_count")
        expected_sample_count_value = (
            raw_sample_count
            if isinstance(raw_sample_count, int) and not isinstance(raw_sample_count, bool)
            else 0
        )
        normalized_selected_p_parts = [
            tuple(part.strip() for part in identity.partition(":"))
            for identity in normalized_expected_selected_p
        ]
        selected_a_provider, selected_a_separator, selected_a_model = tuple(
            part.strip() for part in expected_selected_a_value.partition(":")
        )
        expected_plan_valid = bool(
            isinstance(expected_models_value, list)
            and normalized_expected_models
            and all(isinstance(model, str) for model in expected_models_value)
            and all(normalized_expected_models)
            and isinstance(expected_selected_p_value, list)
            and all(isinstance(identity, str) for identity in expected_selected_p_value)
            and len(normalized_expected_selected_p) == len(normalized_expected_models)
            and expected_sample_count_value > 0
            and expected_sample_count_value == len(normalized_expected_models)
            and isinstance(raw_aggregator_model, str)
            and expected_aggregator_model_value
            and isinstance(raw_selected_a, str)
            and expected_selected_a_value
            and all(
                isinstance(identity, str)
                and separator == ":"
                and bool(provider)
                and bool(model_identity)
                and model_identity == model
                for identity, (provider, separator, model_identity), model in zip(
                    expected_selected_p_value,
                    normalized_selected_p_parts,
                    normalized_expected_models,
                    strict=True,
                )
            )
            and selected_a_separator == ":"
            and bool(selected_a_provider)
            and bool(selected_a_model)
            and selected_a_model == expected_aggregator_model_value
        )
        if not expected_plan_valid:
            reasons.append("invalid_expected_selection_plan")
        for key in (
            "strategy",
            "selection_mode",
            "profile",
            "proposer_models",
            "selected_P",
            "aggregator_model",
            "selected_A",
        ):
            if key in expected_plan and executed_plan.get(key) != expected_plan.get(key):
                reasons.append(f"executed_selection_plan_mismatch_{key}")
        expected_total = expected_sample_count_value
        if expected_total <= 0:
            expected_models = expected_plan.get("proposer_models")
            if isinstance(expected_models, list):
                expected_total = len(expected_models)
        if expected_total and total != expected_total:
            reasons.append("wrong_executed_proposer_count")
        if expected_total and (
            not isinstance(successful, int) or successful < legal_proposer_quorum(expected_total)
        ):
            reasons.append("insufficient_configured_proposer_quorum")

        expected_aggregator_model = expected_aggregator_model_value
        expected_selected_a = expected_selected_a_value
        expected_aggregator_provider = (
            expected_selected_a.partition(":")[0].strip() if ":" in expected_selected_a else ""
        )
        raw_actual_aggregator_model = usage.get("model") if isinstance(usage, Mapping) else None
        actual_aggregator_model = (
            raw_actual_aggregator_model.strip()
            if isinstance(raw_actual_aggregator_model, str)
            else ""
        )
        raw_actual_aggregator_provider = (
            usage.get("provider") if isinstance(usage, Mapping) else None
        )
        actual_aggregator_provider = (
            raw_actual_aggregator_provider.strip()
            if isinstance(raw_actual_aggregator_provider, str)
            else ""
        )
        if expected_aggregator_model and not actual_aggregator_model:
            reasons.append("missing_actual_aggregator_model")
        elif expected_aggregator_model and actual_aggregator_model != expected_aggregator_model:
            reasons.append("wrong_actual_aggregator_model")
        if expected_aggregator_provider and not actual_aggregator_provider:
            reasons.append("missing_actual_aggregator_provider")
        elif (
            expected_aggregator_provider
            and actual_aggregator_provider != expected_aggregator_provider
        ):
            reasons.append("wrong_actual_aggregator_provider")
    if group == "B2" and isinstance(expected_run_compatibility_contract, Mapping):
        experiment = expected_run_compatibility_contract.get("experiment_config")
        ensemble = experiment.get("ensemble") if isinstance(experiment, Mapping) else None
        routing = experiment.get("routing") if isinstance(experiment, Mapping) else None
        if not isinstance(ensemble, Mapping):
            reasons.append("missing_expected_b2_ensemble_contract")
        else:
            if not isinstance(executed_plan, dict):
                reasons.append("missing_executed_selection_plan")
            expected_members = ensemble.get("proposers")
            members = (
                [member for member in expected_members if isinstance(member, Mapping)]
                if isinstance(expected_members, list)
                else []
            )
            expected_models = [
                str(member.get("model") or "")
                for member in members
                for _ in range(max(1, coerce_metric_int(member.get("k"))))
            ]
            expected_selected_p = [
                f"{str(member.get('provider') or '')}:{str(member.get('model') or '')}"
                for member in members
                for _ in range(max(1, coerce_metric_int(member.get("k"))))
            ]
            expected_total = len(expected_models)
            expected_minimum = legal_proposer_quorum(expected_total)
            if expected_total and total != expected_total:
                reasons.append("wrong_b2_proposer_count")
            if expected_minimum and (
                not isinstance(successful, int) or successful < expected_minimum
            ):
                reasons.append("insufficient_b2_configured_quorum")
            if isinstance(executed_plan, dict):
                if expected_models and executed_plan.get("proposer_models") != expected_models:
                    reasons.append("wrong_b2_proposer_lineup")
                if expected_selected_p and executed_plan.get("selected_P") != expected_selected_p:
                    reasons.append("wrong_b2_proposer_provider_lineup")
                expected_aggregator = ensemble.get("aggregator")
                expected_aggregator_provider = (
                    str(expected_aggregator.get("provider") or "")
                    if isinstance(expected_aggregator, Mapping)
                    else ""
                )
                expected_aggregator_model = (
                    str(expected_aggregator.get("model") or "")
                    if isinstance(expected_aggregator, Mapping)
                    else ""
                )
                if (
                    expected_aggregator_model
                    and executed_plan.get("aggregator_model") != expected_aggregator_model
                ):
                    reasons.append("wrong_b2_aggregator_model")
                expected_selected_a = (
                    f"{expected_aggregator_provider}:{expected_aggregator_model}"
                    if expected_aggregator_provider and expected_aggregator_model
                    else ""
                )
                if expected_selected_a and executed_plan.get("selected_A") != expected_selected_a:
                    reasons.append("wrong_b2_aggregator_provider_identity")
                actual_aggregator_model = (
                    str(usage.get("model") or "") if isinstance(usage, Mapping) else ""
                )
                if expected_aggregator_model and not actual_aggregator_model:
                    reasons.append("missing_actual_aggregator_model")
                elif (
                    expected_aggregator_model
                    and actual_aggregator_model != expected_aggregator_model
                ):
                    reasons.append("wrong_b2_actual_aggregator_model")
                actual_aggregator_provider = (
                    str(usage.get("provider") or "") if isinstance(usage, Mapping) else ""
                )
                if expected_aggregator_provider and not actual_aggregator_provider:
                    reasons.append("missing_actual_aggregator_provider")
                elif (
                    expected_aggregator_provider
                    and actual_aggregator_provider != expected_aggregator_provider
                ):
                    reasons.append("wrong_b2_actual_aggregator_provider")
                expected_profile = str(ensemble.get("profile_name") or "")
                if expected_profile and executed_plan.get("profile") != expected_profile:
                    reasons.append("wrong_b2_profile")
                expected_mode = (
                    str(routing.get("selection_mode") or "") if isinstance(routing, Mapping) else ""
                )
                executed_mode = str(
                    executed_plan.get("selection_mode") or executed_plan.get("strategy") or ""
                )
                if expected_mode and executed_mode != expected_mode:
                    reasons.append("wrong_b2_selection_mode")
    return list(dict.fromkeys(reasons))


def terminal_generation_reclassification_evidence(
    row: Mapping[str, Any],
    *,
    ensemble_reasons: Sequence[str],
) -> dict[str, Any] | None:
    """Prove one legacy B2 policy marker is stale without changing attempt history."""

    if str(row.get("group") or "") != "B2" or ensemble_reasons:
        return None
    execution = row.get("execution")
    if not isinstance(execution, Mapping):
        return None
    if (
        str(row.get("error") or "") != LEGACY_TERMINAL_POLICY_ERROR
        or str(execution.get("run_error") or "") != LEGACY_TERMINAL_POLICY_ERROR
        or row.get("selected_generation_succeeded") is not False
    ):
        return None
    matching = matching_saved_generation_attempts(row)
    if len(matching) != 1:
        return None
    attempt = matching[0]
    run = attempt.get("run")
    if (
        not isinstance(run, Mapping)
        or str(run.get("error") or "") != LEGACY_TERMINAL_POLICY_ERROR
        or str(attempt.get("retry_reason") or "") != LEGACY_TERMINAL_POLICY_ERROR
    ):
        return None
    selected_marker = coerce_metric_int(execution.get("selected_generation_attempt"))
    if selected_marker > 0 and selected_marker != coerce_metric_int(attempt.get("attempt")):
        return None
    trace = row.get("ensemble_trace")
    calls, sequence_reasons = ensemble_call_trace_sequence(
        trace if isinstance(trace, Mapping) else {}
    )
    if sequence_reasons or not calls:
        return None
    intermediate_fallbacks = [
        coerce_metric_int(call.get("agent_call_index"))
        for call in calls[:-1]
        if call.get("fallback_used") is True
    ]
    if not intermediate_fallbacks:
        return None
    terminal = calls[-1]
    if (
        terminal.get("fallback_used") is not False
        or str(terminal.get("final_request_role") or "") != "aggregator"
    ):
        return None
    attempt_id = str(attempt.get("attempt_id") or "")
    if len(attempt_id) != 32 or any(char not in "0123456789abcdef" for char in attempt_id):
        return None
    return {
        "schema": GENERATION_TERMINAL_RECLASSIFICATION_SCHEMA,
        "policy": "terminal_aggregator_with_empty_intermediate_fallback/v1",
        "original_error": LEGACY_TERMINAL_POLICY_ERROR,
        "selected_attempt_id": attempt_id,
        "selected_attempt": coerce_metric_int(attempt.get("attempt")),
        "terminal_call_index": coerce_metric_int(terminal.get("agent_call_index")),
        "intermediate_fallback_call_indexes": intermediate_fallbacks,
    }


def apply_terminal_generation_reclassification(
    row: dict[str, Any],
    *,
    expected_run_compatibility_contract: Mapping[str, Any] | None,
) -> bool:
    """Normalize only top-level policy metadata after strict terminal proof."""

    ensemble_reasons = ensemble_generation_completion_reasons(
        row,
        expected_run_compatibility_contract=expected_run_compatibility_contract,
    )
    evidence = terminal_generation_reclassification_evidence(
        row,
        ensemble_reasons=ensemble_reasons,
    )
    if evidence is None:
        return False
    attempt = matching_saved_generation_attempts(row)[0]
    run = attempt["run"]
    usage = run.get("usage") if isinstance(run, Mapping) else None
    billed_cost = float(usage.get("billed_cost") or 0.0) if isinstance(usage, Mapping) else 0.0
    selected_metrics = {
        "latency_ms": coerce_metric_int(run.get("latency_ms")),
        "stream_tool_call_count": coerce_metric_int(run.get("stream_tool_call_count")),
        "server_tool_call_count": coerce_metric_int(run.get("server_tool_call_count")),
        "server_tool_use": (
            dict(run.get("server_tool_use"))
            if isinstance(run.get("server_tool_use"), Mapping)
            else {}
        ),
        "total_tool_call_count": coerce_metric_int(run.get("total_tool_call_count")),
        "trajectory_steps": coerce_metric_int(run.get("trajectory_steps")),
        "llm_request_count": coerce_metric_int(run.get("llm_request_count")),
        "usage_unknown_count": coerce_metric_int(run.get("usage_unknown_count")),
        "billed_cost_usd": billed_cost,
        "generation_attempt": coerce_metric_int(attempt.get("attempt")),
    }
    row["error"] = None
    row["selected_generation_succeeded"] = True
    row["selected_attempt_metrics"] = selected_metrics
    row["selected_attempt_billed_cost_usd"] = billed_cost
    for metric_field in (
        "latency_ms",
        "stream_tool_call_count",
        "server_tool_call_count",
        "server_tool_use",
        "total_tool_call_count",
        "trajectory_steps",
        "llm_request_count",
        "usage_unknown_count",
    ):
        row[metric_field] = selected_metrics[metric_field]
    row["tool_call_count"] = selected_metrics["stream_tool_call_count"]
    execution = dict(row.get("execution") or {})
    execution.update(
        {
            "run_error": "",
            "judge_skipped_reason": "",
            "selected_generation_attempt": selected_metrics["generation_attempt"],
            "selected_generation_latency_ms": selected_metrics["latency_ms"],
            "selected_attempt_metrics": selected_metrics,
            "generation_terminal_reclassification": evidence,
        }
    )
    for metric_field in (
        "latency_ms",
        "stream_tool_call_count",
        "server_tool_call_count",
        "server_tool_use",
        "total_tool_call_count",
        "trajectory_steps",
        "llm_request_count",
        "usage_unknown_count",
    ):
        execution[metric_field] = selected_metrics[metric_field]
    execution["tool_call_count"] = selected_metrics["stream_tool_call_count"]
    row["execution"] = execution
    completion = (
        dict(row.get("completion_status"))
        if isinstance(row.get("completion_status"), Mapping)
        else {}
    )
    completion["generation_accepted"] = True
    completion["incomplete_reasons"] = [
        reason
        for reason in completion.get("incomplete_reasons") or []
        if reason != LEGACY_TERMINAL_POLICY_ERROR
    ]
    row["completion_status"] = completion
    return True


def usage_actual_identity_evidence(usage: Any) -> dict[str, Any]:
    """Extract auditable physical model/provider evidence from saved receipts."""

    usage_dict = usage if isinstance(usage, Mapping) else {}
    breakdown = usage_dict.get("model_usage_breakdown")
    rows = (
        [
            item
            for item in breakdown
            if isinstance(item, Mapping)
            and str(item.get("role") or "").strip().casefold()
            not in MISSING_USAGE_PLACEHOLDER_ROLES
        ]
        if isinstance(breakdown, list)
        else []
    )
    models = sorted(
        {
            str(item.get("model") or "").strip()
            for item in rows
            if str(item.get("model") or "").strip()
        }
    )
    providers = sorted(
        {
            str(item.get("provider") or "").strip()
            for item in rows
            if str(item.get("provider") or "").strip()
        }
    )
    return {
        "model": models[0] if len(models) == 1 else "",
        "provider": providers[0] if len(providers) == 1 else "",
        "models": models,
        "providers": providers,
        "receipt_count": len(rows),
    }


def _set_ensemble_metadata_repair(
    record: dict[str, Any],
    field: str,
    *,
    status: str,
    source: str,
    receipt_count: int = 0,
) -> bool:
    repair = (
        dict(record.get("metadata_repair"))
        if isinstance(record.get("metadata_repair"), Mapping)
        else {}
    )
    item: dict[str, Any] = {
        "status": status,
        "source": source,
    }
    if receipt_count:
        item["receipt_count"] = receipt_count
    if repair.get(field) == item:
        return False
    repair[field] = item
    record["metadata_repair"] = repair
    return True


def _ensemble_receipt_rows(
    row: Mapping[str, Any],
    record: Mapping[str, Any],
    *,
    role: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key in ("model_usage_breakdown", "diagnostic_model_usage_breakdown"):
        value = record.get(key)
        if isinstance(value, list):
            rows.extend(dict(item) for item in value if isinstance(item, Mapping))
    usage = record.get("usage")
    if isinstance(usage, Mapping):
        breakdown = usage.get("model_usage_breakdown")
        if isinstance(breakdown, list):
            rows.extend(dict(item) for item in breakdown if isinstance(item, Mapping))
    top_usage = row.get("usage")
    top_breakdown = (
        top_usage.get("model_usage_breakdown") if isinstance(top_usage, Mapping) else None
    )
    if isinstance(top_breakdown, list):
        requested_provider = str(record.get("requested_provider") or "").strip()
        requested_model = str(record.get("requested_model") or "").strip()
        execution = record.get("execution")
        if isinstance(execution, Mapping):
            requested_provider = (
                requested_provider
                or str(
                    execution.get("requested_provider") or execution.get("provider") or ""
                ).strip()
            )
            requested_model = (
                requested_model
                or str(execution.get("requested_model") or execution.get("model") or "").strip()
            )
        label = str(record.get("label") or "").strip()
        for item in top_breakdown:
            if not isinstance(item, Mapping):
                continue
            item_role = str(item.get("role") or "").strip().casefold()
            if item_role and item_role != role:
                continue
            item_requested_provider = str(item.get("requested_provider") or "").strip()
            item_requested_model = str(item.get("requested_model") or "").strip()
            item_label = str(item.get("label") or "").strip()
            if (
                requested_provider
                and item_requested_provider
                and item_requested_provider != requested_provider
            ):
                continue
            if requested_model and item_requested_model and item_requested_model != requested_model:
                continue
            if label and item_label and item_label != label:
                continue
            rows.append(dict(item))
    return rows


def _unique_receipt_identity(rows: list[dict[str, Any]]) -> dict[str, str]:
    models = {
        str(item.get("model") or "").strip()
        for item in rows
        if str(item.get("model") or "").strip()
    }
    providers = {
        str(item.get("provider") or "").strip()
        for item in rows
        if str(item.get("provider") or "").strip()
    }
    return {
        "model": next(iter(models)) if len(models) == 1 else "",
        "provider": next(iter(providers)) if len(providers) == 1 else "",
    }


def repair_ensemble_trace_metadata(row: dict[str, Any]) -> bool:
    """Repair trace metadata without inventing response identity or stop reason."""

    root_trace = row.get("ensemble_trace")
    if not isinstance(root_trace, dict):
        return False
    calls = root_trace.get("calls")
    call_traces = (
        [item for item in calls if isinstance(item, dict)]
        if isinstance(calls, list)
        else [root_trace]
    )
    changed = False
    for call_trace in call_traces:
        candidates = call_trace.get("candidates")
        if isinstance(candidates, list):
            for candidate in candidates:
                if not isinstance(candidate, dict) or candidate.get("ok") is not True:
                    continue
                receipt_rows = _ensemble_receipt_rows(
                    row,
                    candidate,
                    role="proposer",
                )
                identity = _unique_receipt_identity(receipt_rows)
                for field in ("provider", "model"):
                    repair_field = f"actual_{field}"
                    if str(candidate.get(field) or "").strip():
                        continue
                    inferred = identity[field]
                    if inferred:
                        candidate[field] = inferred
                        changed = True
                        changed |= _set_ensemble_metadata_repair(
                            candidate,
                            repair_field,
                            status="backfilled",
                            source="unique_physical_usage_receipt",
                            receipt_count=len(receipt_rows),
                        )
                    else:
                        changed |= _set_ensemble_metadata_repair(
                            candidate,
                            repair_field,
                            status="unavailable",
                            source="no_unique_physical_usage_receipt",
                        )
                if candidate.get("usage_reported") is not True:
                    if receipt_rows:
                        candidate["usage_reported"] = True
                        changed = True
                        changed |= _set_ensemble_metadata_repair(
                            candidate,
                            "usage",
                            status="backfilled",
                            source="physical_usage_receipt",
                            receipt_count=len(receipt_rows),
                        )
                    else:
                        changed |= _set_ensemble_metadata_repair(
                            candidate,
                            "usage",
                            status="unavailable",
                            source="no_physical_usage_receipt",
                        )
                if not str(candidate.get("stop_reason") or "").strip():
                    changed |= _set_ensemble_metadata_repair(
                        candidate,
                        "stop_reason",
                        status="unavailable",
                        source="provider_stop_reason_not_recorded",
                    )

        final_request = call_trace.get("final_request")
        if not isinstance(final_request, dict):
            continue
        usage = final_request.get("usage")
        if not isinstance(usage, dict):
            usage = {}
            final_request["usage"] = usage
            changed = True
            changed |= _set_ensemble_metadata_repair(
                final_request,
                "usage",
                status="unavailable",
                source="provider_usage_not_recorded",
            )
        call_plan = call_trace.get("selection_plan")
        selected_a = call_plan.get("selected_A") if isinstance(call_plan, Mapping) else None
        requested_provider, separator, requested_model = (
            selected_a.partition(":") if isinstance(selected_a, str) else ("", "", "")
        )
        if separator == ":" and requested_provider.strip() and requested_model.strip():
            if not str(usage.get("requested_provider") or "").strip():
                usage["requested_provider"] = requested_provider.strip()
                changed = True
            if not str(usage.get("requested_model") or "").strip():
                usage["requested_model"] = requested_model.strip()
                changed = True
        receipt_record = {
            **final_request,
            "requested_provider": usage.get("requested_provider"),
            "requested_model": usage.get("requested_model"),
        }
        receipt_rows = _ensemble_receipt_rows(
            row,
            receipt_record,
            role="aggregator",
        )
        identity = _unique_receipt_identity(receipt_rows)
        for field in ("provider", "model"):
            repair_field = f"actual_{field}"
            if str(usage.get(field) or "").strip():
                continue
            inferred = identity[field]
            if inferred:
                usage[field] = inferred
                changed = True
                changed |= _set_ensemble_metadata_repair(
                    final_request,
                    repair_field,
                    status="backfilled",
                    source="unique_physical_usage_receipt",
                    receipt_count=len(receipt_rows),
                )
            else:
                changed |= _set_ensemble_metadata_repair(
                    final_request,
                    repair_field,
                    status="unavailable",
                    source="no_unique_physical_usage_receipt",
                )
        if not str(usage.get("stop_reason") or "").strip():
            changed |= _set_ensemble_metadata_repair(
                final_request,
                "stop_reason",
                status="unavailable",
                source="provider_stop_reason_not_recorded",
            )
    return changed


def backfill_usage_actual_identity(usage: Any) -> bool:
    """Fill only uniquely evidenced envelope identity; never copy declarations."""

    if not isinstance(usage, dict):
        return False
    evidence = usage_actual_identity_evidence(usage)
    changed_fields: list[str] = []
    for field_name in ("model", "provider"):
        current = usage.get(field_name)
        current_text = current.strip() if isinstance(current, str) else ""
        inferred = str(evidence.get(field_name) or "")
        if not current_text and inferred:
            usage[field_name] = inferred
            changed_fields.append(field_name)
    if not changed_fields:
        return False
    provider_usage = (
        dict(usage.get("provider_usage"))
        if isinstance(usage.get("provider_usage"), Mapping)
        else {}
    )
    provider_usage["actual_identity_backfill"] = {
        "source": "unique_model_usage_breakdown",
        "fields": changed_fields,
        "receipt_count": coerce_metric_int(evidence.get("receipt_count")),
    }
    usage["provider_usage"] = provider_usage
    return True


def backfill_saved_row_requested_identity(
    row: dict[str, Any],
    *,
    expected_run_compatibility_contract: Mapping[str, Any] | None = None,
) -> bool:
    """Repair absent requested identity from the row's frozen compatible plan."""

    usage = row.get("usage")
    if not isinstance(usage, dict):
        return False
    spec = GROUP_SPECS.get(str(row.get("group") or "")) or {}
    routing_trace = row.get("routing_trace")
    expected_model = ""
    if spec.get("kind") == "single":
        expected_model = str(spec.get("model") or "").strip()
    elif spec.get("kind") == "router_single" and isinstance(routing_trace, Mapping):
        expected_model = str(
            routing_trace.get("applied_model") or routing_trace.get("fallback_model") or ""
        ).strip()
    expected_provider = ""
    if isinstance(expected_run_compatibility_contract, Mapping):
        runtime_contract = expected_run_compatibility_contract.get("resolved_llm_runtime")
        if isinstance(runtime_contract, Mapping):
            expected_provider = str(runtime_contract.get("provider") or "").strip()
    expected_plan = (
        routing_trace.get("selection_plan")
        if isinstance(routing_trace, Mapping)
        and isinstance(routing_trace.get("selection_plan"), Mapping)
        else None
    )
    synthetic_done = DoneEvent(
        requested_model=str(usage.get("requested_model") or ""),
        requested_provider=str(usage.get("requested_provider") or ""),
        provider_usage=(
            dict(usage.get("provider_usage"))
            if isinstance(usage.get("provider_usage"), Mapping)
            else {}
        ),
        ensemble_trace=(
            row.get("ensemble_trace") if isinstance(row.get("ensemble_trace"), dict) else None
        ),
    )
    synthetic_result = RunResult(
        final_text=str(row.get("final_text") or ""),
        done=synthetic_done,
    )
    changed = backfill_result_requested_identity(
        synthetic_result,
        expected_model=expected_model,
        expected_provider=expected_provider,
        expected_selection_plan=expected_plan,
    )
    if not changed:
        return False
    usage["requested_model"] = synthetic_done.requested_model
    usage["requested_provider"] = synthetic_done.requested_provider
    usage["provider_usage"] = dict(synthetic_done.provider_usage)
    if spec.get("kind") in {"single", "router_single"}:
        breakdown = usage.get("model_usage_breakdown")
        if isinstance(breakdown, list):
            for item in breakdown:
                if not isinstance(item, dict):
                    continue
                if expected_model and not str(item.get("requested_model") or "").strip():
                    item["requested_model"] = expected_model
                if expected_provider and not str(item.get("requested_provider") or "").strip():
                    item["requested_provider"] = expected_provider
    return True


def generation_postprocessing_terminal_evidence(
    row: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Prove a paid attempt is terminal and must never be auto-sent again."""

    reason = str(row.get("error") or "")
    prefix = "generation_postprocessing_failed:"
    if not reason.startswith(prefix):
        return None
    _, _, encoded = reason.partition(prefix)
    stage, separator, exception_type = encoded.rpartition(":")
    if not separator or not stage or not exception_type:
        return None
    execution = row.get("execution")
    attempts = (
        execution.get("generation_attempts")
        if isinstance(execution, Mapping)
        else None
    )
    if not isinstance(attempts, list) or not attempts:
        return None
    attempt = attempts[-1]
    run = attempt.get("run") if isinstance(attempt, Mapping) else None
    attempt_failure = (
        attempt.get("generation_postprocessing_failure")
        if isinstance(attempt, Mapping)
        else None
    )
    run_failure = (
        run.get("generation_postprocessing_failure")
        if isinstance(run, Mapping)
        else None
    )
    if (
        not isinstance(attempt, Mapping)
        or str(attempt.get("attempt_kind") or "") != "generation"
        or str(attempt.get("retry_reason") or "") != reason
        or str(attempt.get("retry_suppressed_reason") or "") != reason
        or attempt.get("will_retry") is not False
        or not isinstance(run, Mapping)
        or str(run.get("error") or "") != reason
        or coerce_metric_int(run.get("llm_request_count")) <= 0
        or not isinstance(attempt_failure, Mapping)
        or str(attempt_failure.get("stage") or "") != stage
        or str(attempt_failure.get("exception_type") or "")
        != exception_type
        or not isinstance(run_failure, Mapping)
        or str(run_failure.get("stage") or "") != stage
        or str(run_failure.get("exception_type") or "")
        != exception_type
        or row.get("selected_generation_succeeded") is not False
    ):
        return None
    attempt_id = str(attempt.get("attempt_id") or "")
    if (
        len(attempt_id) != 32
        or any(character not in "0123456789abcdef" for character in attempt_id)
    ):
        return None
    return {
        "schema": "opensquilla.draco.generation-postprocessing-terminal/v1",
        "reason": reason,
        "stage": stage,
        "exception_type": exception_type,
        "attempt_id": attempt_id,
        "attempt": coerce_metric_int(attempt.get("attempt")),
        "llm_request_count": coerce_metric_int(
            run.get("llm_request_count")
        ),
        "automatic_generation_retry_allowed": False,
    }


def resume_row_completion_state(
    row: dict[str, Any],
    *,
    expected_prompt_sha256: str,
    expected_task_input_sha256: str,
    expected_run_compatibility_fingerprint: str,
    expected_run_compatibility_contract: Mapping[str, Any] | None = None,
    require_openrouter_non_byok: bool = False,
    judge_required: bool = True,
) -> dict[str, Any]:
    """Classify a prior row without conflating generation, Judge, and metadata."""

    generation_reasons: list[str] = []
    identity_metadata_reasons: list[str] = []
    if not verify_result_row_evidence(row):
        generation_reasons.append("invalid_result_evidence")
    row_error = str(row.get("error") or "")
    stored_non_byok = row.get("openrouter_non_byok_audit")
    fatal_policy_reasons: list[str] = []
    if row_error in _FATAL_POLICY_ERRORS:
        fatal_policy_reasons.append(row_error)
    if isinstance(stored_non_byok, Mapping) and (
        stored_non_byok.get("policy_safe_to_continue") is False
        or stored_non_byok.get("status") == "policy_violation"
        or coerce_metric_int(stored_non_byok.get("explicit_byok_request_count")) > 0
        or coerce_metric_int(stored_non_byok.get("conflict_request_count")) > 0
    ):
        fatal_policy_reasons.append("stored_openrouter_non_byok_policy_violation")
    if row_error and row_error not in _NON_GENERATION_ERRORS:
        generation_reasons.append("generation_error")
    if not str(row.get("final_text") or "").strip():
        generation_reasons.append("empty_final_text")
    if str(row.get("prompt_sha256") or "") != expected_prompt_sha256:
        generation_reasons.append("prompt_hash_mismatch")
    task_input_sha256 = str(row.get("task_input_sha256") or "")
    if not task_input_sha256:
        generation_reasons.append("missing_task_input_sha256")
    elif task_input_sha256 != expected_task_input_sha256:
        generation_reasons.append("task_input_hash_mismatch")
    run_fingerprint = str(row.get("run_compatibility_fingerprint") or "")
    if not run_fingerprint:
        generation_reasons.append("missing_run_compatibility_fingerprint")
    elif run_fingerprint != expected_run_compatibility_fingerprint:
        generation_reasons.append("run_compatibility_fingerprint_mismatch")

    ensemble_reasons = ensemble_generation_completion_reasons(
        row,
        expected_run_compatibility_contract=(expected_run_compatibility_contract),
    )
    for reason in ensemble_reasons:
        if ensemble_metadata_only_reason(reason):
            identity_metadata_reasons.append(f"{reason}_backfill_required")
        else:
            generation_reasons.append(reason)
    terminal_reclassification = terminal_generation_reclassification_evidence(
        row,
        ensemble_reasons=[
            reason for reason in ensemble_reasons if not ensemble_metadata_only_reason(reason)
        ],
    )
    if terminal_reclassification is not None:
        generation_reasons = [
            reason for reason in generation_reasons if reason != "generation_error"
        ]
    postprocessing_terminal = (
        generation_postprocessing_terminal_evidence(row)
    )
    generation_valid = not generation_reasons

    judge_reasons = judge_completion_reasons(
        row,
        judge_required=judge_required,
    )
    judge_attempt_budget_exhausted = bool(
        isinstance(row.get("judge"), Mapping)
        and row["judge"].get("judge_attempt_budget_exhausted") is True
    )
    judge_complete = generation_valid and not judge_reasons

    spec = GROUP_SPECS.get(str(row.get("group") or "")) or {}
    provider_spec = row.get("provider_spec")
    if not isinstance(provider_spec, Mapping):
        generation_reasons.append("missing_provider_spec")
    elif provider_spec.get("kind") != spec.get("kind"):
        generation_reasons.append("wrong_provider_spec_kind")

    expected_model = ""
    if spec.get("kind") == "single":
        raw_expected_model = spec.get("model")
        expected_model = raw_expected_model.strip() if isinstance(raw_expected_model, str) else ""
        configured_model = (
            provider_spec.get("model") if isinstance(provider_spec, Mapping) else None
        )
        if configured_model != expected_model:
            generation_reasons.append("wrong_fixed_model")
    elif spec.get("kind") == "router_single":
        routing_trace = row.get("routing_trace")
        if not isinstance(routing_trace, Mapping):
            generation_reasons.append("missing_single_router_trace")
        else:
            raw_applied_model = routing_trace.get("applied_model")
            raw_fallback_model = routing_trace.get("fallback_model")
            applied_model = raw_applied_model.strip() if isinstance(raw_applied_model, str) else ""
            fallback_model = (
                raw_fallback_model.strip() if isinstance(raw_fallback_model, str) else ""
            )
            expected_model = applied_model or fallback_model
            if not expected_model:
                generation_reasons.append("missing_single_router_model")

    if spec.get("kind") in {"single", "router_single"}:
        usage = row.get("usage")
        identity_evidence = usage_actual_identity_evidence(usage)
        raw_actual_model = usage.get("model") if isinstance(usage, Mapping) else None
        actual_model = raw_actual_model.strip() if isinstance(raw_actual_model, str) else ""
        raw_actual_provider = usage.get("provider") if isinstance(usage, Mapping) else None
        actual_provider = (
            raw_actual_provider.strip() if isinstance(raw_actual_provider, str) else ""
        )
        evidenced_model = str(identity_evidence.get("model") or "")
        evidenced_models = list(identity_evidence.get("models") or [])
        if (
            actual_model
            and evidenced_models
            and any(model != actual_model for model in evidenced_models)
        ):
            generation_reasons.append("actual_model_receipt_mismatch")
        if not actual_model:
            if evidenced_model:
                if expected_model and evidenced_model != expected_model:
                    generation_reasons.append("wrong_actual_model")
                else:
                    identity_metadata_reasons.append("actual_model_metadata_backfill_required")
            else:
                identity_metadata_reasons.append("actual_model_evidence_missing")
        elif expected_model and actual_model != expected_model:
            generation_reasons.append("wrong_actual_model")

        expected_provider = ""
        if isinstance(expected_run_compatibility_contract, Mapping):
            runtime_contract = expected_run_compatibility_contract.get("resolved_llm_runtime")
            raw_expected_provider = (
                runtime_contract.get("provider") if isinstance(runtime_contract, Mapping) else None
            )
            expected_provider = (
                raw_expected_provider.strip() if isinstance(raw_expected_provider, str) else ""
            )
        evidenced_provider = str(identity_evidence.get("provider") or "")
        evidenced_providers = list(identity_evidence.get("providers") or [])
        if (
            actual_provider
            and evidenced_providers
            and any(provider != actual_provider for provider in evidenced_providers)
        ):
            generation_reasons.append("actual_provider_receipt_mismatch")
        if not actual_provider:
            if evidenced_provider:
                if expected_provider and evidenced_provider != expected_provider:
                    generation_reasons.append("wrong_actual_provider")
                else:
                    identity_metadata_reasons.append("actual_provider_metadata_backfill_required")
            else:
                identity_metadata_reasons.append("actual_provider_evidence_missing")
        elif expected_provider and actual_provider != expected_provider:
            generation_reasons.append("wrong_actual_provider")

        raw_requested_model = usage.get("requested_model") if isinstance(usage, Mapping) else None
        requested_model = (
            raw_requested_model.strip() if isinstance(raw_requested_model, str) else ""
        )
        if not requested_model:
            identity_metadata_reasons.append("requested_model_metadata_backfill_required")
        elif expected_model and requested_model != expected_model:
            generation_reasons.append("wrong_requested_model")

        raw_requested_provider = (
            usage.get("requested_provider") if isinstance(usage, Mapping) else None
        )
        requested_provider = (
            raw_requested_provider.strip() if isinstance(raw_requested_provider, str) else ""
        )
        if not requested_provider:
            identity_metadata_reasons.append("requested_provider_metadata_backfill_required")
        elif expected_provider and requested_provider != expected_provider:
            generation_reasons.append("wrong_requested_provider")

    generation_valid = not generation_reasons
    if not generation_valid:
        judge_complete = False

    cost_reasons: list[str] = []
    cost_reasons.extend(identity_metadata_reasons)
    accounting = row_cost_accounting(row)
    if not bool(accounting.get("actual_llm_cost_complete")):
        cost_reasons.append("cost_metadata_incomplete")
    recomputed_non_byok: dict[str, Any] | None = None
    if require_openrouter_non_byok:
        runtime_contract = (
            expected_run_compatibility_contract.get("resolved_llm_runtime")
            if isinstance(expected_run_compatibility_contract, Mapping)
            else None
        )
        contract_provider_routing = (
            runtime_contract.get("provider_routing")
            if isinstance(runtime_contract, Mapping)
            and isinstance(runtime_contract.get("provider_routing"), Mapping)
            else None
        )
        recomputed_non_byok = openrouter_non_byok_audit(
            row,
            provider_routing=contract_provider_routing,
        )
        if recomputed_non_byok.get("policy_safe_to_continue") is False:
            fatal_policy_reasons.append("openrouter_non_byok_policy_violation")
        elif (
            recomputed_non_byok.get("pass") is not True
            or coerce_metric_int(recomputed_non_byok.get("request_count")) <= 0
            or coerce_metric_int(recomputed_non_byok.get("unverified_or_byok_request_count")) != 0
            or not isinstance(stored_non_byok, dict)
            or stored_non_byok != recomputed_non_byok
        ):
            cost_reasons.append("openrouter_non_byok_metadata_incomplete")
    stale_error = bool(
        (row_error == "judge_incomplete" and not judge_reasons)
        or (
            row_error == "cost_metadata_incomplete"
            and bool(accounting.get("actual_llm_cost_complete"))
        )
        or (
            row_error
            in {
                "openrouter_non_byok_verification_failed",
                "openrouter_non_byok_metadata_incomplete",
            }
            and (
                not require_openrouter_non_byok
                or (
                    isinstance(recomputed_non_byok, Mapping)
                    and recomputed_non_byok.get("pass") is True
                )
            )
        )
    )
    if stale_error:
        # Clear only a marker whose underlying condition has already become
        # complete.  A real Judge-only gap must remain judge_only rather than
        # being misclassified as metadata repair.
        cost_reasons.append("stale_completion_error")
    fatal_policy_reasons = list(dict.fromkeys(fatal_policy_reasons))
    cost_metadata_complete = generation_valid and not cost_reasons and not fatal_policy_reasons
    if fatal_policy_reasons:
        action = "policy_violation"
    elif not generation_valid:
        action = "regenerate"
    elif not judge_complete:
        action = "judge_only"
    elif not cost_metadata_complete:
        action = "metadata_only"
    else:
        action = "complete"
    return {
        "generation_valid": generation_valid,
        "judge_complete": judge_complete,
        "cost_metadata_complete": cost_metadata_complete,
        "generation_reasons": generation_reasons,
        "judge_reasons": judge_reasons,
        "judge_attempt_budget_exhausted": judge_attempt_budget_exhausted,
        "cost_metadata_reasons": cost_reasons,
        "fatal_policy_reasons": fatal_policy_reasons,
        "metadata_repair_attempted": (
            isinstance(row.get("execution"), Mapping)
            and row["execution"].get("metadata_repair_attempted") is True
        ),
        "generation_terminal_reclassification": terminal_reclassification,
        "generation_postprocessing_terminal": postprocessing_terminal,
        "generation_auto_retry_blocked": (
            postprocessing_terminal is not None
        ),
        "action": action,
    }


def strict_resume_row_invalid_reasons(
    row: dict[str, Any],
    *,
    expected_prompt_sha256: str,
    expected_task_input_sha256: str,
    expected_run_compatibility_fingerprint: str,
    expected_run_compatibility_contract: Mapping[str, Any] | None = None,
    require_openrouter_non_byok: bool = False,
    judge_required: bool = True,
) -> list[str]:
    """Compatibility wrapper returning all incomplete-state reasons."""

    state = resume_row_completion_state(
        row,
        expected_prompt_sha256=expected_prompt_sha256,
        expected_task_input_sha256=expected_task_input_sha256,
        expected_run_compatibility_fingerprint=(expected_run_compatibility_fingerprint),
        expected_run_compatibility_contract=expected_run_compatibility_contract,
        require_openrouter_non_byok=require_openrouter_non_byok,
        judge_required=judge_required,
    )
    return [
        *state["generation_reasons"],
        *state["judge_reasons"],
        *state["cost_metadata_reasons"],
        *state["fatal_policy_reasons"],
    ]


def load_resume_group_task_states(
    *,
    resume_paths: list[Path],
    selected_keys: set[tuple[str, str]],
    prompt_hashes: dict[str, str],
    task_input_hashes: dict[str, str],
    run_compatibility_fingerprints: dict[str, str],
    run_compatibility_contracts: Mapping[str, Mapping[str, Any]] | None = None,
    require_openrouter_non_byok: bool = False,
    judge_required: bool = True,
) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, Any]]:
    best: dict[tuple[str, str], dict[str, Any]] = {}
    declared_attempt_budget_used: dict[tuple[str, str], int] = {}
    attempt_evidence_modes: dict[tuple[str, str], str] = {}
    strict_attempt_payloads: dict[
        tuple[str, str],
        dict[str, dict[str, Any]],
    ] = {}
    strict_attempt_owners: dict[str, tuple[str, str]] = {}
    legacy_generation_attempts: dict[
        tuple[str, str],
        dict[str, int],
    ] = {}
    matching_attempts = 0
    invalid_attempts = 0
    invalid_reason_counts: dict[str, int] = {}
    actions = (
        "policy_violation",
        "regenerate",
        "judge_only",
        "metadata_only",
        "complete",
    )

    def _repairability_rank(state: Mapping[str, Any]) -> tuple[int, int, int]:
        # Exact generation cost metadata is not reconstructible by merely
        # replaying Judge. Prefer a cost-complete generation whose missing
        # Judge can converge, then one with a completed Judge, and only then a
        # metadata-incomplete row requiring external backfill/audit.
        return (
            int(bool(state.get("generation_valid"))),
            int(bool(state.get("cost_metadata_complete"))),
            int(bool(state.get("judge_complete"))),
        )

    def _generation_completed_sort_key(
        candidate: Mapping[str, Any],
    ) -> tuple[int, float, int, int]:
        row = candidate.get("row")
        raw_timestamp: Any = None
        if isinstance(row, Mapping):
            raw_timestamp = row.get("generation_completed_at")
            if not isinstance(raw_timestamp, int | float) or isinstance(
                raw_timestamp,
                bool,
            ):
                raw_timestamp = row.get("completed_at")
            if not isinstance(raw_timestamp, int | float) or isinstance(
                raw_timestamp,
                bool,
            ):
                raw_timestamp = row.get("started_at")
        timestamp = (
            float(raw_timestamp)
            if isinstance(raw_timestamp, int | float)
            and not isinstance(raw_timestamp, bool)
            and math.isfinite(float(raw_timestamp))
            else 0.0
        )
        return (
            int(timestamp > 0.0),
            timestamp,
            coerce_metric_int(candidate.get("source_index")),
            coerce_metric_int(candidate.get("source_line")),
        )

    def _selection_rank(
        candidate: Mapping[str, Any],
    ) -> tuple[int, int, int, int, int, float, int, int]:
        return (
            int(candidate.get("action") == "policy_violation"),
            *_repairability_rank(candidate),
            *_generation_completed_sort_key(candidate),
        )

    def _row_generation_attempt_count(row: Mapping[str, Any]) -> int:
        execution = row.get("execution")
        attempts = execution.get("generation_attempts") if isinstance(execution, Mapping) else None
        count = max(
            coerce_metric_int(row.get("generation_attempt_count")),
            len(attempts) if isinstance(attempts, list) else 0,
        )
        if (
            count <= 0
            and str(row.get("final_text") or "").strip()
            and coerce_metric_int(row.get("llm_request_count")) > 0
        ):
            count = 1
        return count

    def _legacy_generation_fingerprint(row: Mapping[str, Any]) -> str:
        execution = row.get("execution")
        attempts = (
            execution.get("generation_attempts")
            if isinstance(execution, Mapping)
            and isinstance(execution.get("generation_attempts"), list)
            else []
        )
        return canonical_json_sha256(
            {
                "group": row.get("group"),
                "task_id": row.get("task_id"),
                "prompt_sha256": row.get("prompt_sha256"),
                "started_at": row.get("started_at"),
                "final_text_sha256": (
                    row.get("final_text_sha256") or text_sha256(str(row.get("final_text") or ""))
                ),
                "generation_attempts": attempts,
            }
        )

    def _attempt_evidence_location(path: Path, line_number: int) -> str:
        return f"{path}:{line_number}"

    def _strict_attempt_payload_is_monotonic_enrichment(
        prior: Any,
        current: Any,
        *,
        path: tuple[str, ...] = (),
    ) -> bool:
        """Allow only additive usage metadata on a repeated physical attempt."""

        usage_metadata_path = len(path) >= 2 and path[:2] == ("run", "usage")

        def _cost_source_trust_rank(value: Any) -> int:
            source = str(value or "").strip().casefold()
            if source in {"", "none", "unavailable"}:
                return 0
            if source in {
                "opensquilla_estimate",
                "opensquilla_static_estimate",
                "static_estimate",
            }:
                return 1
            if source in {"mixed", "openrouter_usage", "provider_billed"}:
                return 2
            return -1

        def _has_exact_cost_evidence(value: Mapping[str, Any]) -> bool:
            if exact_provider_usage_cost(value) is not None:
                return True
            if str(value.get("cost_source") or "").strip().casefold() == "mixed":
                exact_projection = dict(value)
                exact_projection["cost_source"] = "provider_billed"
                return exact_provider_usage_cost(exact_projection) is not None
            return False

        if isinstance(prior, Mapping):
            if not isinstance(current, Mapping):
                return False
            prior_keys = set(prior)
            current_keys = set(current)
            if not prior_keys <= current_keys:
                return False
            added_keys = current_keys - prior_keys
            if added_keys and not (
                usage_metadata_path or (path == ("run",) and added_keys <= {"usage"})
            ):
                return False
            if usage_metadata_path:
                prior_source = prior.get("cost_source")
                current_source = current.get("cost_source")
                prior_rank = _cost_source_trust_rank(prior_source)
                current_rank = _cost_source_trust_rank(current_source)
                if current_source != prior_source and (
                    current_rank <= prior_rank
                    or current_rank < 0
                    or (current_rank == 2 and not _has_exact_cost_evidence(current))
                ):
                    return False
            return all(
                _strict_attempt_payload_is_monotonic_enrichment(
                    prior[key],
                    current[key],
                    path=(*path, str(key)),
                )
                for key in prior_keys
            )
        if isinstance(prior, list):
            if not isinstance(current, list):
                return False
            if len(prior) != len(current):
                if not (usage_metadata_path and not prior and current):
                    return False
                return True
            return all(
                _strict_attempt_payload_is_monotonic_enrichment(
                    prior_item,
                    current_item,
                    path=(*path, str(index)),
                )
                for index, (prior_item, current_item) in enumerate(zip(prior, current, strict=True))
            )
        if type(prior) is type(current) and prior == current:
            return True
        if not usage_metadata_path:
            return False
        if prior is None:
            return current is not None
        if (
            isinstance(prior, str)
            and isinstance(current, str)
            and not prior.strip()
            and bool(current.strip())
        ):
            return True
        field_name = path[-1] if path else ""
        if field_name in {"cost_source", "costSource"}:
            prior_rank = _cost_source_trust_rank(prior)
            current_rank = _cost_source_trust_rank(current)
            return current_rank > prior_rank >= 0
        if field_name in {
            "billed_cost",
            "billed_cost_usd",
            "billedCostUsd",
            "cost_usd",
            "costUsd",
            "estimated_cost_usd",
            "estimatedCostUsd",
            "provider_reported_cost",
        }:
            return (
                isinstance(prior, int | float)
                and not isinstance(prior, bool)
                and float(prior) == 0.0
                and isinstance(current, int | float)
                and not isinstance(current, bool)
                and math.isfinite(float(current))
                and float(current) >= 0.0
            )
        return False

    def _strict_g1_terminal_attempt_reason(
        attempt: Mapping[str, Any],
    ) -> str:
        run = attempt.get("run")
        run_map = run if isinstance(run, Mapping) else {}
        trace_events = run_map.get("trace_events")
        routing_trace = run_map.get("routing_trace")
        if (
            attempt.get("attempt_kind") == "generation_pre_call_guard"
            or any(
                isinstance(event, Mapping)
                and event.get("code") == "g1_pre_call_guard_failed"
                for event in (
                    trace_events
                    if isinstance(trace_events, list)
                    else []
                )
            )
            or (
                isinstance(routing_trace, Mapping)
                and isinstance(
                    routing_trace.get("pre_call_guard"),
                    Mapping,
                )
            )
        ):
            return "g1_pre_call_guard_failed"
        if (
            attempt.get("attempt_kind")
            == "provider_build_after_paid_setup"
            and coerce_metric_int(run_map.get("llm_request_count")) > 0
        ):
            return "g1_paid_setup_attempt_lacks_frozen_lifecycle"
        return ""

    def _strict_attempt_evidence(
        row: Mapping[str, Any],
        *,
        key: tuple[str, str],
        path: Path,
        line_number: int,
    ) -> tuple[int, int]:
        location = _attempt_evidence_location(path, line_number)
        schema = row.get("generation_attempt_evidence_schema")
        if schema != GENERATION_ATTEMPT_EVIDENCE_SCHEMA:
            raise ValueError(
                f"unsupported generation attempt evidence schema at {location}: {schema!r}"
            )
        execution = row.get("execution")
        attempts = execution.get("generation_attempts") if isinstance(execution, Mapping) else None
        if not isinstance(attempts, list):
            raise ValueError(f"generation attempt evidence list is missing at {location}")
        observed_count = len(attempts)
        declared_row_count = row.get("generation_attempt_count")
        if (
            not isinstance(declared_row_count, int)
            or isinstance(declared_row_count, bool)
            or declared_row_count != observed_count
        ):
            raise ValueError(f"generation attempt row count contradicts evidence at {location}")
        actual_metrics = row.get("actual_spend_metrics")
        actual_count = (
            actual_metrics.get("generation_attempt_count")
            if isinstance(actual_metrics, Mapping)
            else None
        )
        if (
            not isinstance(actual_count, int)
            or isinstance(actual_count, bool)
            or actual_count != observed_count
        ):
            raise ValueError(
                f"actual-spend generation attempt count contradicts evidence at {location}"
            )
        budget_limit = row.get("generation_attempt_budget_limit")
        if budget_limit != GENERATION_MAX_ATTEMPTS:
            raise ValueError(f"generation attempt budget limit differs at {location}")
        declared_budget = row.get("generation_attempt_budget_used")
        if (
            not isinstance(declared_budget, int)
            or isinstance(declared_budget, bool)
            or declared_budget < 0
            or declared_budget > GENERATION_MAX_ATTEMPTS
        ):
            raise ValueError(f"generation attempt budget declaration is invalid at {location}")

        seen_in_row: set[str] = set()
        known_payloads = strict_attempt_payloads.setdefault(key, {})
        prior_declared = declared_attempt_budget_used.get(key, 0)
        new_attempt_count = 0
        for attempt in attempts:
            if not isinstance(attempt, Mapping):
                raise ValueError(f"generation attempt evidence row is invalid at {location}")
            attempt_id = attempt.get("attempt_id")
            if (
                not isinstance(attempt_id, str)
                or len(attempt_id) != 32
                or any(char not in "0123456789abcdef" for char in attempt_id)
            ):
                raise ValueError(f"generation attempt identity is invalid at {location}")
            if attempt_id in seen_in_row:
                raise ValueError(
                    f"duplicate generation attempt identity at {location}: {attempt_id}"
                )
            seen_in_row.add(attempt_id)
            prior_owner = strict_attempt_owners.get(attempt_id)
            if prior_owner is not None and prior_owner != key:
                raise ValueError(
                    "generation attempt identity is already owned by "
                    f"{prior_owner[0]}/{prior_owner[1]} at {location}: "
                    f"{attempt_id}"
                )
            attempt_kind = attempt.get("attempt_kind")
            if attempt_kind not in {
                "generation",
                "generation_pre_call_guard",
                "provider_build_after_paid_setup",
            }:
                raise ValueError(
                    f"generation attempt kind is invalid at {location}: {attempt_kind!r}"
                )
            attempt_index = attempt.get("attempt")
            if (
                not isinstance(attempt_index, int)
                or isinstance(attempt_index, bool)
                or not 1 <= attempt_index <= GENERATION_MAX_ATTEMPTS
            ):
                raise ValueError(f"generation attempt ordinal is invalid at {location}")
            attempt_started_at = attempt.get("started_at")
            attempt_completed_at = attempt.get("completed_at")
            if (
                not isinstance(attempt_started_at, int | float)
                or isinstance(attempt_started_at, bool)
                or not math.isfinite(float(attempt_started_at))
                or not isinstance(attempt_completed_at, int | float)
                or isinstance(attempt_completed_at, bool)
                or not math.isfinite(float(attempt_completed_at))
                or float(attempt_completed_at) < float(attempt_started_at)
            ):
                raise ValueError(f"generation attempt timestamps are invalid at {location}")
            run = attempt.get("run")
            if not isinstance(run, Mapping):
                raise ValueError(f"generation attempt run evidence is missing at {location}")
            if key[0] == "G1":
                terminal_reason = _strict_g1_terminal_attempt_reason(
                    attempt
                )
                if terminal_reason:
                    raise ValueError(
                        "strict G1 generation history is terminal at "
                        f"{location}: {terminal_reason}"
                    )
            if (
                attempt_kind == "provider_build_after_paid_setup"
                and coerce_metric_int(run.get("llm_request_count")) <= 0
            ):
                raise ValueError(
                    f"paid provider-build attempt lacks physical request evidence at {location}"
                )
            prior_payload = known_payloads.get(attempt_id)
            if prior_payload is None:
                new_attempt_count += 1
                expected_ordinal = prior_declared + new_attempt_count
                if attempt_index != expected_ordinal:
                    raise ValueError(
                        "generation attempt ordinal is not cumulative at "
                        f"{location}: observed={attempt_index}, "
                        f"expected={expected_ordinal}"
                    )
                strict_attempt_owners[attempt_id] = key
                known_payloads[attempt_id] = copy.deepcopy(dict(attempt))
            elif not _strict_attempt_payload_is_monotonic_enrichment(
                prior_payload,
                attempt,
            ):
                raise ValueError(
                    f"generation attempt identity has conflicting evidence at "
                    f"{location}: {attempt_id}"
                )
            else:
                known_payloads[attempt_id] = copy.deepcopy(dict(attempt))

        prior_generation_used = (
            execution.get("prior_generation_attempts_used")
            if isinstance(execution, Mapping)
            else None
        )
        if (
            not isinstance(prior_generation_used, int)
            or isinstance(prior_generation_used, bool)
            or prior_generation_used != prior_declared
        ):
            raise ValueError(f"prior generation attempt budget is invalid at {location}")
        budget_remaining = (
            execution.get("generation_attempt_budget_remaining")
            if isinstance(execution, Mapping)
            else None
        )
        if (
            not isinstance(budget_remaining, int)
            or isinstance(budget_remaining, bool)
            or budget_remaining != GENERATION_MAX_ATTEMPTS - declared_budget
        ):
            raise ValueError(f"generation attempt budget remainder is invalid at {location}")
        generation_reused = execution.get("generation_reused")
        resume_action = str(execution.get("resume_action") or "")
        if new_attempt_count == 0 and prior_declared > 0:
            if generation_reused is not True or resume_action not in {
                "judge_only",
                "metadata_only",
                "regenerate",
            }:
                raise ValueError(f"reused generation attempt evidence is invalid at {location}")
            if declared_budget != prior_generation_used:
                raise ValueError(f"reused generation attempt budget is invalid at {location}")
        elif generation_reused is True:
            raise ValueError(f"generation reuse cannot introduce physical attempts at {location}")
        return declared_budget, new_attempt_count

    for source_index, resume_path in enumerate(resume_paths):
        path = Path(resume_path)
        if not path.is_file():
            raise ValueError(f"resume JSONL does not exist: {path}")
        with path.open("r", encoding="utf-8") as resume_fh:
            for line_number, line in enumerate(resume_fh, start=1):
                if not line.strip():
                    continue
                try:
                    prior_row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"invalid resume JSONL at {path}:{line_number}: {exc}"
                    ) from exc
                if not isinstance(prior_row, dict):
                    raise ValueError(f"resume JSONL row is not an object at {path}:{line_number}")
                key = (
                    str(prior_row.get("group") or ""),
                    str(prior_row.get("task_id") or ""),
                )
                if key not in selected_keys:
                    continue
                matching_attempts += 1
                row_attempt_count = _row_generation_attempt_count(prior_row)
                attempt_schema = prior_row.get("generation_attempt_evidence_schema")
                row_mode = "strict_v1" if attempt_schema is not None else "legacy"
                prior_mode = attempt_evidence_modes.get(key)
                if prior_mode is not None and prior_mode != row_mode:
                    raise ValueError(
                        "mixed generation attempt evidence schemas for "
                        f"{key[0]}/{key[1]} at "
                        f"{_attempt_evidence_location(path, line_number)}"
                    )
                attempt_evidence_modes[key] = row_mode
                if row_mode == "strict_v1":
                    strict_source_bindings = {
                        "prompt_sha256": (
                            str(prior_row.get("prompt_sha256") or ""),
                            str(prompt_hashes.get(key[1], "") or ""),
                        ),
                        "task_input_sha256": (
                            str(prior_row.get("task_input_sha256") or ""),
                            str(task_input_hashes.get(key[1], "") or ""),
                        ),
                        "run_compatibility_fingerprint": (
                            str(
                                prior_row.get(
                                    "run_compatibility_fingerprint"
                                )
                                or ""
                            ),
                            str(
                                run_compatibility_fingerprints.get(key[0], "")
                                or ""
                            ),
                        ),
                    }
                    binding_mismatches = [
                        field_name
                        for field_name, (observed, expected) in (
                            strict_source_bindings.items()
                        )
                        if not expected or observed != expected
                    ]
                    if binding_mismatches:
                        raise ValueError(
                            "strict generation attempt source binding differs at "
                            f"{_attempt_evidence_location(path, line_number)}: "
                            + ",".join(binding_mismatches)
                        )
                    cumulative_attempt_count, new_attempt_count = _strict_attempt_evidence(
                        prior_row,
                        key=key,
                        path=path,
                        line_number=line_number,
                    )
                    prior_declared = declared_attempt_budget_used.get(key, 0)
                    expected_cumulative = prior_declared + new_attempt_count
                    if cumulative_attempt_count != expected_cumulative:
                        raise ValueError(
                            "generation attempt budget contradicts unique physical "
                            f"evidence for {key[0]}/{key[1]} at "
                            f"{_attempt_evidence_location(path, line_number)}: "
                            f"declared={cumulative_attempt_count}, "
                            f"expected={expected_cumulative}"
                        )
                    declared_attempt_budget_used[key] = cumulative_attempt_count
                else:
                    raw_cumulative_attempt_count = prior_row.get("generation_attempt_budget_used")
                    has_cumulative_declaration = raw_cumulative_attempt_count is not None
                    if has_cumulative_declaration and (
                        not isinstance(raw_cumulative_attempt_count, int)
                        or isinstance(raw_cumulative_attempt_count, bool)
                        or raw_cumulative_attempt_count < 0
                    ):
                        raise ValueError(
                            "legacy generation attempt budget declaration is invalid "
                            f"for {key[0]}/{key[1]} at "
                            f"{_attempt_evidence_location(path, line_number)}"
                        )
                    cumulative_attempt_count = coerce_metric_int(raw_cumulative_attempt_count)
                    if cumulative_attempt_count > GENERATION_MAX_ATTEMPTS:
                        raise ValueError(
                            "generation attempt budget exceeds the formal limit "
                            f"for {key[0]}/{key[1]} at "
                            f"{_attempt_evidence_location(path, line_number)}"
                        )
                    prior_declared = declared_attempt_budget_used.get(key, 0)
                    if has_cumulative_declaration:
                        if cumulative_attempt_count < prior_declared:
                            raise ValueError(
                                "generation attempt budget is non-monotonic for "
                                f"{key[0]}/{key[1]} at "
                                f"{_attempt_evidence_location(path, line_number)}"
                            )
                        declared_delta = cumulative_attempt_count - prior_declared
                        if (
                            cumulative_attempt_count < row_attempt_count
                            or declared_delta > row_attempt_count
                        ):
                            raise ValueError(
                                "legacy generation attempt declaration contradicts "
                                f"observable evidence for {key[0]}/{key[1]} at "
                                f"{_attempt_evidence_location(path, line_number)}"
                            )
                        declared_attempt_budget_used[key] = cumulative_attempt_count
                    elif row_attempt_count > 0:
                        fingerprint = _legacy_generation_fingerprint(prior_row)
                        attempts_by_fingerprint = legacy_generation_attempts.setdefault(
                            key,
                            {},
                        )
                        attempts_by_fingerprint[fingerprint] = max(
                            row_attempt_count,
                            attempts_by_fingerprint.get(fingerprint, 0),
                        )
                state = resume_row_completion_state(
                    prior_row,
                    expected_prompt_sha256=prompt_hashes.get(key[1], ""),
                    expected_task_input_sha256=task_input_hashes.get(key[1], ""),
                    expected_run_compatibility_fingerprint=(
                        run_compatibility_fingerprints.get(key[0], "")
                    ),
                    expected_run_compatibility_contract=(
                        run_compatibility_contracts.get(key[0])
                        if run_compatibility_contracts is not None
                        else None
                    ),
                    require_openrouter_non_byok=require_openrouter_non_byok,
                    judge_required=judge_required,
                )
                if state["action"] != "complete":
                    invalid_attempts += 1
                for reason in (
                    *state["generation_reasons"],
                    *state["judge_reasons"],
                    *state["cost_metadata_reasons"],
                    *state["fatal_policy_reasons"],
                ):
                    invalid_reason_counts[reason] = invalid_reason_counts.get(reason, 0) + 1
                candidate = {
                    **state,
                    "row": prior_row,
                    "source_path": str(path),
                    "source_index": source_index,
                    "source_line": line_number,
                }
                current = best.get(key)
                if current is None or _selection_rank(candidate) > _selection_rank(current):
                    best[key] = candidate
    for key, state in best.items():
        declared = declared_attempt_budget_used.get(key, 0)
        legacy = sum(legacy_generation_attempts.get(key, {}).values())
        prior_used = max(declared, legacy)
        if prior_used > GENERATION_MAX_ATTEMPTS:
            raise ValueError(
                "observable generation attempts exceed the formal limit for "
                f"{key[0]}/{key[1]}: {prior_used}"
            )
        state["prior_generation_attempts_used"] = prior_used
        state["generation_attempt_evidence_mode"] = attempt_evidence_modes.get(
            key,
            "legacy",
        )
        state["observed_unique_generation_attempt_count"] = (
            len(strict_attempt_payloads.get(key, {}))
            if attempt_evidence_modes.get(key) == "strict_v1"
            else legacy
        )
        current_contract = (
            run_compatibility_contracts.get(key[0])
            if run_compatibility_contracts is not None
            else None
        )
        if (
            attempt_evidence_modes.get(key) == "strict_v1"
            and g1_cross_wave_lifecycle_requires_reconstruction(
                group=key[0],
                prior_attempts_used=prior_used,
                state=state,
                current_run_compatibility_contract=current_contract,
            )
        ):
            if not isinstance(current_contract, Mapping):
                raise ValueError(
                    "current G1 run compatibility contract is unavailable for "
                    f"{key[0]}/{key[1]}"
                )
            state["g1_frozen_resume_lifecycle"] = (
                reconstruct_g1_cross_wave_frozen_lifecycle(
                    attempts=list(strict_attempt_payloads.get(key, {}).values()),
                    current_run_compatibility_contract=current_contract,
                )
            )
    action_counts = {
        action: sum(1 for state in best.values() if state["action"] == action) for action in actions
    }
    return best, {
        "resume_source_count": len(resume_paths),
        "matching_attempt_count": matching_attempts,
        "best_pair_count": len(best),
        "resume_action_counts": action_counts,
        "strict_valid_pair_count": action_counts["complete"],
        "strict_invalid_attempt_count": invalid_attempts,
        "strict_invalid_reason_counts": dict(sorted(invalid_reason_counts.items())),
    }


def load_strict_completed_group_task_keys(
    *,
    resume_paths: list[Path],
    selected_keys: set[tuple[str, str]],
    prompt_hashes: dict[str, str],
    task_input_hashes: dict[str, str],
    run_compatibility_fingerprints: dict[str, str],
    run_compatibility_contracts: Mapping[str, Mapping[str, Any]] | None = None,
    require_openrouter_non_byok: bool = False,
    judge_required: bool = True,
) -> tuple[set[tuple[str, str]], dict[str, Any]]:
    states, audit = load_resume_group_task_states(
        resume_paths=resume_paths,
        selected_keys=selected_keys,
        prompt_hashes=prompt_hashes,
        task_input_hashes=task_input_hashes,
        run_compatibility_fingerprints=run_compatibility_fingerprints,
        run_compatibility_contracts=run_compatibility_contracts,
        require_openrouter_non_byok=require_openrouter_non_byok,
        judge_required=judge_required,
    )
    return {key for key, state in states.items() if state["action"] == "complete"}, audit


async def amain(args: argparse.Namespace) -> int:
    args._source_provenance = source_provenance()
    if getattr(args, "require_clean_source", False) and (
        args._source_provenance.get("git_dirty") is not False
    ):
        raise ValueError(
            "--require-clean-source requires a clean, readable Git worktree before launch"
        )
    repair_only_prerequisites = validate_repair_only_source_drift_prerequisites(args)
    groups = parse_groups(args.groups)
    alignment = apply_b2_g12_argument_alignment(args, groups)
    bundle = getattr(args, "_draco_experiment_config_bundle", None)
    experiment_config = bundle.config if isinstance(bundle, DracoExperimentConfigBundle) else None
    if experiment_config is not None:
        all_tasks = load_tasks(args.input, max_tasks=0)
        input_validation = validate_reference_input(
            args.input,
            task_ids=[str(task["id"]) for task in all_tasks],
            config=experiment_config.benchmark_input,
        )
        args._draco_input_validation = input_validation
        if alignment is not None:
            alignment["input_validation"] = input_validation
        tasks = all_tasks[: args.max_tasks] if args.max_tasks > 0 else all_tasks
    else:
        tasks = load_tasks(args.input, max_tasks=args.max_tasks)
    if alignment is not None:
        effective = alignment["effective_args"]
        print(
            "DRACO shared experiment profile applied: "
            f"concurrency={effective['concurrency']}, timeout={effective['timeout']}, "
            f"web={effective['local_web_search_provider']}, "
            f"judge={effective['judge_model']}",
            file=sys.stderr,
            flush=True,
        )
    args.runner_mode = str(
        getattr(args, "runner_mode", DEFAULT_DRACO_RUNNER_MODE) or DEFAULT_DRACO_RUNNER_MODE
    ).strip()
    if args.runner_mode not in SUPPORTED_RUNNER_MODES:
        raise ValueError(f"unknown runner mode: {args.runner_mode}")
    validate_runner_mode_for_groups(args.runner_mode, groups)
    agent_finalization_policy = normalize_agent_runner_args(args)
    args.generation_max_attempts = bounded_generation_attempts(
        getattr(args, "generation_max_attempts", GENERATION_MAX_ATTEMPTS)
    )
    args.generation_retry_backoff = bounded_generation_retry_backoff(
        getattr(
            args,
            "generation_retry_backoff",
            DEFAULT_GENERATION_RETRY_BACKOFF_SECONDS,
        )
    )
    tool_policy = benchmark_tool_policy(args)
    smoke_only = bool(getattr(args, "local_web_tools_smoke_only", False))
    if smoke_only and bool(getattr(args, "dry_run", False)):
        raise ValueError("--local-web-tools-smoke-only cannot be combined with --dry-run")
    if smoke_only and tool_policy.get("tool_mode") != TOOL_MODE_LOCAL_WEB_TOOLS:
        raise ValueError("--local-web-tools-smoke-only requires --tool-mode=local_web_tools")
    validate_tool_mode_for_runner(
        args.runner_mode,
        str(tool_policy.get("tool_mode") or ""),
        smoke_only=smoke_only,
    )
    generation_policy = generation_thinking_policy(args)
    config = GatewayConfig.load(args.config)
    # Process-local credentials take precedence over any inline value in the
    # reference config. Only an irreversible key fingerprint enters the run
    # compatibility contract.
    api_key_env = str(getattr(config.llm, "api_key_env", "") or "").strip()
    process_api_key = os.environ.get(api_key_env or "OPENROUTER_API_KEY", "").strip()
    if process_api_key:
        config.llm.api_key = process_api_key
    args._formal_runtime_freeze = enforce_formal_draco_runtime_config(
        config,
        experiment_config,
        groups,
    )
    if getattr(args, "require_openrouter_non_byok", False) and not getattr(args, "dry_run", False):
        args._strict_openrouter_non_byok_environment = (
            validate_strict_openrouter_non_byok_environment(config)
        )
    if "G1" in groups:
        if experiment_config is None:
            raise ValueError("G1 requires an experiment config")
        args._g1_registry_contract = validate_g1_registry_contract(
            experiment_config,
            config,
        )
        if alignment is not None:
            alignment["g1_registry_contract"] = dict(args._g1_registry_contract)
    expected_compatibility_manifest = getattr(
        args,
        "expected_compatibility_manifest",
        None,
    )
    selected_keys = load_selected_group_task_keys(
        tasks=tasks,
        groups=groups,
        only_keys_path=getattr(args, "only_group_task_keys", None),
    )
    prompt_hashes = {str(task["id"]): text_sha256(str(task.get("prompt") or "")) for task in tasks}
    task_input_hashes = {str(task["id"]): canonical_json_sha256(task) for task in tasks}
    if getattr(args, "only_group_task_keys", None) is not None:
        print(
            f"selection: restricted run to {len(selected_keys)} group/task pairs",
            file=sys.stderr,
            flush=True,
        )
    sandbox_runtime = configure_benchmark_sandbox_runtime(config, tool_policy)
    fetch_runtime = configure_local_web_fetch_runtime(tool_policy)
    search_runtime = configure_local_web_search_runtime(
        config,
        tool_policy,
        dry_run=bool(getattr(args, "dry_run", False)),
    )
    if search_runtime or sandbox_runtime or fetch_runtime:
        local_web_tools = dict(tool_policy.get("local_web_tools") or {})
        if search_runtime:
            local_web_tools["search_runtime"] = search_runtime
        if sandbox_runtime:
            local_web_tools["sandbox_runtime"] = sandbox_runtime
        if fetch_runtime:
            local_web_tools["fetch_runtime"] = fetch_runtime
        tool_policy = {**tool_policy, "local_web_tools": local_web_tools}
    stable_group_tool_policies = benchmark_tool_policies_for_groups(
        tool_policy,
        groups,
        args=args,
    )
    current_run_compatibility = build_run_compatibility(
        args=args,
        config=config,
        groups=groups,
        group_tool_policies=stable_group_tool_policies,
        generation_policy=generation_policy,
    )
    if repair_only_prerequisites is not None:
        (
            args._run_compatibility,
            args._repair_compatibility_audit,
        ) = validate_repair_only_source_drift_compatibility(
            path=expected_compatibility_manifest,
            actual=current_run_compatibility,
            groups=groups,
        )
        args._repair_compatibility_audit["preconditions"] = repair_only_prerequisites
    else:
        args._run_compatibility = current_run_compatibility
    if expected_compatibility_manifest is not None and repair_only_prerequisites is None:
        validate_expected_run_compatibility(
            path=expected_compatibility_manifest,
            actual=args._run_compatibility,
            groups=groups,
        )
    judge_required = bool(str(getattr(args, "judge_model", "") or "").strip())
    resume_states, resume_audit = load_resume_group_task_states(
        resume_paths=list(getattr(args, "resume_from_jsonl", []) or []),
        selected_keys=selected_keys,
        prompt_hashes=prompt_hashes,
        task_input_hashes=task_input_hashes,
        run_compatibility_fingerprints=args._run_compatibility["fingerprints"],
        run_compatibility_contracts=args._run_compatibility["contracts"],
        require_openrouter_non_byok=bool(getattr(args, "require_openrouter_non_byok", False)),
        judge_required=judge_required,
    )
    completed_keys = {key for key, state in resume_states.items() if state["action"] == "complete"}
    scheduled_keys = selected_keys - completed_keys
    fatal_policy_keys = {
        key
        for key in scheduled_keys
        if key in resume_states and resume_states[key]["action"] == "policy_violation"
    }
    regenerate_keys = {
        key
        for key in scheduled_keys
        if key not in resume_states or resume_states[key]["action"] == "regenerate"
    }
    generation_budget_exhausted_keys = {
        key
        for key in regenerate_keys
        if key in resume_states
        and coerce_metric_int(resume_states[key].get("prior_generation_attempts_used"))
        >= GENERATION_MAX_ATTEMPTS
    }
    generation_auto_retry_blocked_keys = {
        key
        for key in regenerate_keys
        if key in resume_states
        and resume_states[key].get("generation_auto_retry_blocked") is True
    }
    model_regenerate_keys = (
        regenerate_keys
        - generation_budget_exhausted_keys
        - generation_auto_retry_blocked_keys
    )
    judge_only_keys = {
        key
        for key in scheduled_keys
        if key in resume_states and resume_states[key]["action"] == "judge_only"
    }
    metadata_only_keys = {
        key
        for key in scheduled_keys
        if key in resume_states and resume_states[key]["action"] == "metadata_only"
    }
    args._resume_selection = {
        "selected_pair_count": len(selected_keys),
        "scheduled_pair_count": len(scheduled_keys),
        "regenerate_pair_count": len(regenerate_keys),
        "model_regenerate_pair_count": len(model_regenerate_keys),
        "generation_budget_exhausted_pair_count": len(generation_budget_exhausted_keys),
        "generation_auto_retry_blocked_pair_count": len(
            generation_auto_retry_blocked_keys
        ),
        "judge_only_pair_count": len(judge_only_keys),
        "metadata_only_pair_count": len(metadata_only_keys),
        "policy_violation_pair_count": len(fatal_policy_keys),
        "scheduled_pairs": [
            {
                "group": group,
                "task_id": str(task["id"]),
                "action": (
                    resume_states[(group, str(task["id"]))]["action"]
                    if (group, str(task["id"])) in resume_states
                    else "regenerate"
                ),
            }
            for task in tasks
            for group in groups
            if (group, str(task["id"])) in scheduled_keys
        ],
        **resume_audit,
    }
    if repair_only_prerequisites is not None:
        repair_action_audit = repair_only_resume_classification_audit(
            selected_keys=selected_keys,
            resume_states=resume_states,
        )
        args._repair_compatibility_audit["classification_gate"] = repair_action_audit
        if repair_action_audit["regenerate_pair_count"]:
            args._repair_compatibility_audit["status"] = "rejected_regeneration_required"
            preview = ", ".join(
                f"{item['group']}/{item['task_id']} ({item['reason']})"
                for item in repair_action_audit["regenerate_pairs"][:10]
            )
            raise ValueError(
                "--repair-only-source-drift refuses every generation path; "
                f"resume classification requires regeneration for: {preview}"
            )
        args._repair_compatibility_audit["status"] = "repair_actions_validated"
    if fatal_policy_keys:
        output_dir = args.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        failure_stamp = time.strftime("%Y%m%d-%H%M%S")
        failure_manifest = output_dir / (
            f"draco_run_{failure_stamp}.policy-violation.manifest.json"
        )
        manifest_tool_policy = {
            **tool_policy,
            "group_tool_policies": stable_group_tool_policies,
        }
        fatal_pairs = [
            {
                "group": group,
                "task_id": task_id,
                "reasons": list(resume_states[(group, task_id)].get("fatal_policy_reasons") or []),
            }
            for group, task_id in sorted(fatal_policy_keys)
        ]
        write_manifest(
            failure_manifest,
            args=args,
            stamp=failure_stamp,
            status="cost_audit_failed",
            started_at=time.time(),
            finished_at=time.time(),
            tasks=tasks,
            groups=groups,
            rows_written=0,
            artifacts={"manifest_json": str(failure_manifest)},
            tool_policy=manifest_tool_policy,
            command=command_payload(args),
            failure={
                "stage": "resume_source_openrouter_non_byok_policy_violation",
                "failure_count": len(fatal_pairs),
                "failures": fatal_pairs,
                "model_or_judge_started": False,
            },
        )
        print(
            "resume: explicit OpenRouter BYOK/provider evidence conflict found "
            "in prior campaign data; no preflight, provider, or Judge call was started",
            file=sys.stderr,
            flush=True,
        )
        return 2
    if completed_keys:
        print(
            f"resume: skipped {len(completed_keys)} strictly valid group/task pairs; "
            f"scheduled {len(scheduled_keys)} remaining pairs",
            file=sys.stderr,
            flush=True,
        )
    if not scheduled_keys and not smoke_only:
        print(
            "resume: no pending group/task pairs; no preflight, provider, Judge, or "
            "output artifacts were created",
            file=sys.stderr,
            flush=True,
        )
        return 0
    preflight_started_at = time.time()
    web_preflight: dict[str, Any] = {}
    if model_regenerate_keys or smoke_only:
        try:
            web_preflight = await run_local_web_tools_preflight(
                tool_policy,
                dry_run=bool(getattr(args, "dry_run", False)),
            )
        except Exception as exc:
            if not smoke_only:
                output_dir = args.output_dir
                output_dir.mkdir(parents=True, exist_ok=True)
                failure_stamp = time.strftime("%Y%m%d-%H%M%S")
                failure_manifest = output_dir / (
                    f"draco_run_{failure_stamp}.preflight-failed.manifest.json"
                )
                write_manifest(
                    failure_manifest,
                    args=args,
                    stamp=failure_stamp,
                    status="preflight_failed",
                    started_at=preflight_started_at,
                    finished_at=time.time(),
                    tasks=tasks,
                    groups=groups,
                    artifacts={"manifest_json": str(failure_manifest)},
                    tool_policy=tool_policy,
                    command=command_payload(args),
                    failure={
                        "stage": "local_web_tools_preflight",
                        "error_class": type(exc).__name__,
                        "error": str(exc)[:1000],
                        "model_or_judge_started": False,
                    },
                )
            raise
    elif tool_policy.get("tool_mode") == TOOL_MODE_LOCAL_WEB_TOOLS:
        web_preflight = {
            "status": "skipped_not_required",
            "reason": "resume_wave_has_no_model_regeneration",
            "model_regenerate_pair_count": 0,
            "preflight_calls": {"web_search": 0, "web_fetch": 0},
            "cost_attribution": "no_generation_preflight_not_required",
        }
    if web_preflight:
        local_web_tools = dict(tool_policy.get("local_web_tools") or {})
        local_web_tools["preflight"] = web_preflight
        tool_policy = {**tool_policy, "local_web_tools": local_web_tools}
    if smoke_only:
        print(
            json.dumps({"local_web_tools_preflight": web_preflight}, ensure_ascii=False),
            flush=True,
        )
        return 0
    inherited = inherited_provider_config(config)
    group_tool_policies = benchmark_tool_policies_for_groups(
        tool_policy,
        groups,
        args=args,
    )
    manifest_tool_policy = {
        **tool_policy,
        "group_tool_policies": group_tool_policies,
    }
    if (
        tool_policy.get("tools_enabled")
        and tool_policy.get("tool_mode") == TOOL_MODE_OPENROUTER_SERVER_TOOLS
        and inherited.provider != "openrouter"
        and not getattr(args, "dry_run", False)
    ):
        raise ValueError(
            "--tool-mode=openrouter_server_tools requires an OpenRouter runtime provider"
        )
    if (
        any(policy.get("openrouter_fusion_enabled") for policy in group_tool_policies.values())
        and inherited.provider != "openrouter"
        and not getattr(args, "dry_run", False)
    ):
        raise ValueError(
            "OpenRouter Fusion experiment groups require an OpenRouter runtime provider"
        )
    judge_provider = None
    if args.judge_model:
        judge_provider = build_single_provider(
            inherited=inherited,
            group="judge",
            model=args.judge_model,
            dry_run=args.dry_run,
        )
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_started_at = time.time()
    jsonl_path = output_dir / f"draco_ensemble_{stamp}.jsonl"
    trace_path = output_dir / f"draco_run_{stamp}.trace.jsonl"
    manifest_path = output_dir / f"draco_run_{stamp}.manifest.json"
    command_path = output_dir / f"draco_run_{stamp}.command.txt"
    summary_json_path = jsonl_path.with_suffix(".summary.json")
    semaphore = asyncio.Semaphore(max(1, args.concurrency))
    judge_semaphore = asyncio.Semaphore(max(1, int(getattr(args, "judge_concurrency", 1) or 1)))
    rows: list[dict[str, Any]] = []
    artifacts = {
        "results_jsonl": str(jsonl_path),
        "trace_jsonl": str(trace_path),
        "manifest_json": str(manifest_path),
        "command_txt": str(command_path),
        "summary_json": str(summary_json_path),
        "summary_markdown": str(jsonl_path.with_suffix(".md")),
    }
    artifacts.update(write_experiment_config_artifacts(output_dir, args=args, stamp=stamp))
    command = write_command_file(command_path, args=args, stamp=stamp)
    write_manifest(
        manifest_path,
        args=args,
        stamp=stamp,
        status="running",
        started_at=run_started_at,
        tasks=tasks,
        groups=groups,
        artifacts=artifacts,
        tool_policy=manifest_tool_policy,
        command=command,
    )

    def _generation_budget_exhausted_row(
        task: dict[str, Any],
        group: str,
        state: Mapping[str, Any],
    ) -> dict[str, Any]:
        row = json.loads(json.dumps(state["row"], ensure_ascii=False))
        prior_used = coerce_metric_int(state.get("prior_generation_attempts_used"))
        if not isinstance(row.get("generation_completed_at"), int | float):
            legacy_completed_at = row.get("completed_at")
            if isinstance(legacy_completed_at, int | float):
                row["generation_completed_at"] = float(legacy_completed_at)
        row["generation_attempt_budget_limit"] = GENERATION_MAX_ATTEMPTS
        row["generation_attempt_budget_used"] = prior_used
        row["error"] = "generation_attempt_budget_exhausted"
        row["completed_at"] = time.time()
        execution = dict(row.get("execution") or {})
        execution.update(
            {
                "resume_action": "regenerate",
                "generation_reused": True,
                "generation_model_started": False,
                "prior_generation_attempts_used": prior_used,
                "generation_attempt_budget_remaining": 0,
            }
        )
        row["execution"] = execution
        reasons = list(
            dict.fromkeys(
                [
                    *(state.get("generation_reasons") or []),
                    "generation_attempt_budget_exhausted",
                ]
            )
        )
        row["resume_completion"] = {
            "action": "regenerate",
            "generation_reused": True,
            "metadata_repaired": False,
            "judge_reran": False,
            "judge_complete": False,
            "cost_metadata_complete": False,
            "post_repair_action": "regenerate",
            "status": "incomplete",
            "incomplete_reasons": reasons,
        }
        row["completion_status"] = {
            "generation_accepted": False,
            "cost_metadata_complete": False,
            "judge_complete": False,
            "status": "incomplete",
            "incomplete_reasons": reasons,
        }
        return row

    def _g1_lifecycle_reconstruction_required_row(
        task: dict[str, Any],
        group: str,
        state: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Preserve evidence and refuse a fresh analyzer/plan after paid G1."""

        del task, group
        row = json.loads(json.dumps(state["row"], ensure_ascii=False))
        prior_used = coerce_metric_int(state.get("prior_generation_attempts_used"))
        row["generation_attempt_budget_limit"] = GENERATION_MAX_ATTEMPTS
        row["generation_attempt_budget_used"] = prior_used
        row["error"] = "g1_cross_wave_lifecycle_reconstruction_required"
        row["completed_at"] = time.time()
        execution = dict(row.get("execution") or {})
        execution.update(
            {
                "resume_action": "regenerate",
                "generation_reused": True,
                "generation_model_started": False,
                "prior_generation_attempts_used": prior_used,
                "generation_attempt_budget_remaining": max(
                    0,
                    GENERATION_MAX_ATTEMPTS - prior_used,
                ),
                "g1_cross_wave_resume_blocked": True,
                "g1_cross_wave_resume_block_reason": (
                    "frozen_task_analysis_and_retry_plan_reconstruction_required"
                ),
            }
        )
        row["execution"] = execution
        reasons = list(
            dict.fromkeys(
                [
                    *state.get("generation_reasons", []),
                    "g1_cross_wave_lifecycle_reconstruction_required",
                ]
            )
        )
        row["resume_completion"] = {
            "action": "regenerate",
            "generation_reused": True,
            "metadata_repaired": False,
            "judge_reran": False,
            "judge_complete": False,
            "cost_metadata_complete": False,
            "post_repair_action": "regenerate",
            "status": "incomplete",
            "incomplete_reasons": reasons,
        }
        row["completion_status"] = {
            "generation_accepted": False,
            "cost_metadata_complete": False,
            "judge_complete": False,
            "status": "incomplete",
            "incomplete_reasons": reasons,
        }
        return row

    def _generation_postprocessing_terminal_row(
        task: dict[str, Any],
        group: str,
        state: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Preserve one paid terminal attempt without another model call."""

        del task, group
        row = json.loads(json.dumps(state["row"], ensure_ascii=False))
        prior_used = coerce_metric_int(
            state.get("prior_generation_attempts_used")
        )
        evidence = state.get("generation_postprocessing_terminal")
        reason = str(row.get("error") or "")
        row["generation_attempt_budget_limit"] = (
            GENERATION_MAX_ATTEMPTS
        )
        row["generation_attempt_budget_used"] = prior_used
        row["completed_at"] = time.time()
        execution = dict(row.get("execution") or {})
        execution.update(
            {
                "resume_action": "regenerate",
                "generation_reused": True,
                "generation_model_started": False,
                "prior_generation_attempts_used": prior_used,
                "generation_attempt_budget_remaining": max(
                    0,
                    GENERATION_MAX_ATTEMPTS - prior_used,
                ),
                "generation_auto_retry_blocked": True,
                "generation_postprocessing_terminal": (
                    dict(evidence)
                    if isinstance(evidence, Mapping)
                    else None
                ),
            }
        )
        row["execution"] = execution
        reasons = list(
            dict.fromkeys(
                [
                    *state.get("generation_reasons", []),
                    reason,
                    "generation_auto_retry_blocked",
                ]
            )
        )
        row["resume_completion"] = {
            "action": "regenerate",
            "generation_reused": True,
            "metadata_repaired": False,
            "judge_reran": False,
            "judge_complete": False,
            "cost_metadata_complete": False,
            "post_repair_action": "regenerate",
            "status": "incomplete",
            "incomplete_reasons": reasons,
        }
        row["completion_status"] = {
            "generation_accepted": False,
            "cost_metadata_complete": False,
            "judge_complete": False,
            "status": "incomplete",
            "incomplete_reasons": reasons,
        }
        return row

    async def _guarded(task: dict[str, Any], group: str) -> dict[str, Any]:
        group_tool_policy = group_tool_policies[group]
        group_tools = benchmark_tools_for_policy(group_tool_policy)
        key = (group, str(task["id"]))
        state = resume_states.get(key)
        prior_used = (
            coerce_metric_int(state.get("prior_generation_attempts_used"))
            if isinstance(state, Mapping)
            else 0
        )
        if (
            isinstance(state, Mapping)
            and state.get("generation_auto_retry_blocked") is True
        ):
            return _generation_postprocessing_terminal_row(
                task,
                group,
                state,
            )
        remaining_attempts = remaining_generation_attempts(
            prior_used,
            getattr(
                args,
                "generation_max_attempts",
                GENERATION_MAX_ATTEMPTS,
            ),
        )
        if remaining_attempts <= 0 and isinstance(state, Mapping):
            return _generation_budget_exhausted_row(task, group, state)
        current_contract = args._run_compatibility["contracts"].get(group)
        requires_frozen_g1_lifecycle = (
            g1_cross_wave_lifecycle_requires_reconstruction(
                group=group,
                prior_attempts_used=prior_used,
                state=state,
                current_run_compatibility_contract=current_contract,
            )
        )
        frozen_g1_lifecycle = (
            state.get("g1_frozen_resume_lifecycle")
            if requires_frozen_g1_lifecycle
            and isinstance(state, Mapping)
            else None
        )
        if requires_frozen_g1_lifecycle and not isinstance(
            frozen_g1_lifecycle,
            Mapping,
        ):
            return _g1_lifecycle_reconstruction_required_row(
                task,
                group,
                state,
            )
        async with semaphore:
            row = await run_one(
                task=task,
                group=group,
                config=config,
                inherited=inherited,
                dry_run=args.dry_run,
                judge_provider=judge_provider,
                judge_candidates=args.judge_candidates,
                judge_repeats=args.judge_repeats,
                judge_concurrency=getattr(args, "judge_concurrency", 1),
                judge_max_attempts=getattr(args, "judge_max_attempts", JUDGE_MAX_ATTEMPTS),
                judge_semaphore=judge_semaphore,
                timeout=args.timeout,
                ensemble_proposer_timeout=getattr(args, "ensemble_proposer_timeout", None),
                ensemble_aggregator_timeout=getattr(args, "ensemble_aggregator_timeout", None),
                ensemble_proposer_early_stop_success_count=getattr(
                    args,
                    "ensemble_proposer_early_stop_success_count",
                    None,
                ),
                ensemble_proposer_early_stop_after=getattr(
                    args,
                    "ensemble_proposer_early_stop_after",
                    None,
                ),
                expand_ensemble_timeouts_to_task_timeout=getattr(
                    args,
                    "expand_ensemble_timeouts_to_task_timeout",
                    False,
                ),
                tool_policy=group_tool_policy,
                generation_policy=generation_policy,
                experiment_config=(experiment_config if group in {"B2", "G1"} else None),
                runner_mode=args.runner_mode,
                output_dir=output_dir,
                agent_max_iterations=args.agent_max_iterations,
                agent_finalization_policy=agent_finalization_policy,
                generation_max_attempts=getattr(
                    args, "generation_max_attempts", GENERATION_MAX_ATTEMPTS
                )
                if state is None
                else remaining_attempts,
                generation_attempt_offset=prior_used,
                generation_retry_backoff=getattr(
                    args,
                    "generation_retry_backoff",
                    DEFAULT_GENERATION_RETRY_BACKOFF_SECONDS,
                ),
                tools=group_tools,
                run_compatibility_fingerprint=args._run_compatibility["fingerprints"][group],
                g1_registry_contract=(
                    getattr(args, "_g1_registry_contract", None) if group == "G1" else None
                ),
                frozen_g1_lifecycle=frozen_g1_lifecycle,
                require_openrouter_non_byok=bool(
                    getattr(args, "require_openrouter_non_byok", False)
                    and not getattr(args, "dry_run", False)
                ),
            )
        current_attempts = coerce_metric_int(row.get("generation_attempt_count"))
        budget_used = prior_used + current_attempts
        row["generation_attempt_budget_limit"] = GENERATION_MAX_ATTEMPTS
        row["generation_attempt_budget_used"] = budget_used
        execution = dict(row.get("execution") or {})
        execution.update(
            {
                "prior_generation_attempts_used": prior_used,
                "generation_attempt_budget_remaining": max(
                    0,
                    GENERATION_MAX_ATTEMPTS - budget_used,
                ),
            }
        )
        row["execution"] = execution
        return row

    async def _repair_prior_row(
        task: dict[str, Any],
        group: str,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Repair Judge/metadata while preserving the accepted generation verbatim."""

        def _usage_generation_contract(value: Any) -> Any:
            return generation_usage_identity_contract(value)

        async with semaphore:
            row = json.loads(json.dumps(state["row"], ensure_ascii=False))
            action = str(state["action"])
            prior_execution = (
                row.get("execution") if isinstance(row.get("execution"), Mapping) else {}
            )
            expected_contract = args._run_compatibility["contracts"].get(group)
            terminal_reclassified = apply_terminal_generation_reclassification(
                row,
                expected_run_compatibility_contract=expected_contract,
            )
            g1_lifecycle_routing_repaired = False
            if group == "G1":
                recovered_routing, recovery_evidence, recovery_reasons = (
                    g1_provider_lifecycle_routing_evidence(
                        row,
                        contract=expected_contract,
                    )
                )
                if (
                    not recovery_reasons
                    and recovery_evidence
                    and recovered_routing
                    and not row.get("routing_trace")
                ):
                    row["routing_trace"] = recovered_routing
                    execution = dict(row.get("execution") or {})
                    execution["routing_trace"] = recovered_routing
                    execution["g1_provider_lifecycle_routing_recovery"] = recovery_evidence
                    provider_error = str(execution.get("provider_error") or "")
                    if (
                        provider_error.startswith("TypeError:")
                        and "ProviderBillingReceipt" in provider_error
                    ):
                        execution["provider_error_before_routing_recovery"] = provider_error
                        execution["provider_error"] = ""
                    row["execution"] = execution
                    g1_lifecycle_routing_repaired = True
            metadata_repair_already_attempted = (
                prior_execution.get("metadata_repair_attempted") is True
            )
            metadata_only_repair_started_at = (
                time.time()
                if action == "metadata_only" and not metadata_repair_already_attempted
                else None
            )
            should_apply_metadata_repair = (
                action != "metadata_only" or not metadata_repair_already_attempted
            )
            requested_identity_repaired = bool(
                should_apply_metadata_repair
                and backfill_saved_row_requested_identity(
                    row,
                    expected_run_compatibility_contract=(
                        args._run_compatibility["contracts"].get(group)
                    ),
                )
            )
            ensemble_metadata_repaired = bool(
                should_apply_metadata_repair and repair_ensemble_trace_metadata(row)
            )
            actual_identity_repaired = bool(
                should_apply_metadata_repair and backfill_usage_actual_identity(row.get("usage"))
            )
            identity_repaired = bool(
                actual_identity_repaired
                or requested_identity_repaired
                or ensemble_metadata_repaired
            )
            prior_final_text = row.get("final_text")
            prior_usage_contract = _usage_generation_contract(row.get("usage"))
            cost_metadata_repaired = bool(
                should_apply_metadata_repair and repair_row_cost_metadata_with_estimates(row)
            )
            metadata_repaired = bool(identity_repaired or cost_metadata_repaired)
            metadata_repaired = bool(
                metadata_repaired or terminal_reclassified or g1_lifecycle_routing_repaired
            )
            preexisting_non_byok_ready = True
            if getattr(
                args,
                "require_openrouter_non_byok",
                False,
            ) and not getattr(args, "dry_run", False):
                # A new Judge call cannot repair historical BYOK/unverified
                # spend because prior attempts remain part of actual spend.
                # Refuse to add more cost unless every existing generation and
                # Judge receipt already satisfies the strict policy.
                preexisting_non_byok_ready = (
                    openrouter_non_byok_audit(
                        row,
                        provider_routing=_openrouter_audit_provider_routing(
                            inherited.provider_routing,
                            (
                                getattr(args, "_g1_registry_contract", None)
                                if row.get("group") == "G1"
                                else None
                            ),
                        ),
                    ).get("policy_safe_to_continue")
                    is True
                )
            needs_judge = bool(
                judge_completion_reasons(
                    row,
                    judge_required=judge_required,
                )
            )
            judge_repair_requested = bool(
                needs_judge and preexisting_non_byok_ready and action == "judge_only"
            )
            judge_reran = False
            judge_attempt_budget_exhausted = False
            if judge_repair_requested:
                judge = await judge_text(
                    judge_provider=judge_provider,
                    task=task,
                    answer=str(prior_final_text or ""),
                    dry_run=bool(args.dry_run and judge_provider is not None),
                    judge_repeats=args.judge_repeats,
                    judge_concurrency=getattr(args, "judge_concurrency", 1),
                    judge_max_attempts=getattr(
                        args,
                        "judge_max_attempts",
                        JUDGE_MAX_ATTEMPTS,
                    ),
                    judge_semaphore=judge_semaphore,
                    prior_judge=(
                        row.get("judge") if isinstance(row.get("judge"), Mapping) else None
                    ),
                )
                judge_reran = bool(
                    isinstance(judge, Mapping)
                    and coerce_metric_int(judge.get("judge_new_attempt_count")) > 0
                )
                judge_attempt_budget_exhausted = bool(
                    isinstance(judge, Mapping)
                    and judge.get("judge_attempt_budget_exhausted") is True
                )
                row["judge"] = judge
                row["quality_total"] = quality_total(judge)
                candidate_totals = [
                    total
                    for candidate_judge in (
                        row.get("candidate_judges")
                        if isinstance(row.get("candidate_judges"), list)
                        else []
                    )
                    if (total := quality_total(candidate_judge)) is not None
                ]
                row["fusion_delta"] = (
                    row["quality_total"] - max(candidate_totals)
                    if row["quality_total"] is not None and candidate_totals
                    else None
                )
                metadata_repaired |= repair_row_cost_metadata_with_estimates(row)
            non_byok_audit: dict[str, Any] | None = None
            if getattr(args, "require_openrouter_non_byok", False):
                prior_audit = row.get("openrouter_non_byok_audit")
                non_byok_audit = openrouter_non_byok_audit(
                    row,
                    provider_routing=_openrouter_audit_provider_routing(
                        inherited.provider_routing,
                        (
                            getattr(args, "_g1_registry_contract", None)
                            if row.get("group") == "G1"
                            else None
                        ),
                    ),
                )
                row["openrouter_non_byok_audit"] = non_byok_audit
                metadata_repaired |= prior_audit != non_byok_audit
            non_byok_ready = non_byok_audit is None or non_byok_audit.get("pass") is True
            non_byok_policy_safe = (
                non_byok_audit is None or non_byok_audit.get("policy_safe_to_continue") is True
            )
            execution = dict(row.get("execution") or {})
            execution.update(
                {
                    "resume_action": action,
                    "generation_reused": True,
                    "metadata_repaired": metadata_repaired,
                    "generation_terminal_reclassified": terminal_reclassified,
                    "g1_lifecycle_routing_repaired": (g1_lifecycle_routing_repaired),
                    "judge_repair_requested": judge_repair_requested,
                    "judge_reran": judge_reran,
                    "judge_attempt_budget_exhausted": (judge_attempt_budget_exhausted),
                    "resume_source_path": state.get("source_path"),
                    "resume_source_line": state.get("source_line"),
                    "prior_generation_attempts_used": coerce_metric_int(
                        state.get("prior_generation_attempts_used")
                    ),
                    "generation_attempt_budget_remaining": max(
                        0,
                        GENERATION_MAX_ATTEMPTS
                        - coerce_metric_int(state.get("prior_generation_attempts_used")),
                    ),
                }
            )
            if action == "metadata_only":
                execution["metadata_repair_attempted"] = True
                if metadata_only_repair_started_at is not None:
                    execution["metadata_repair_attempted_at"] = metadata_only_repair_started_at
                    execution["metadata_repair_summary"] = {
                        "status": "applied",
                        "generation_called": False,
                        "judge_called": False,
                        "requested_identity_repaired": requested_identity_repaired,
                        "actual_identity_repaired": actual_identity_repaired,
                        "ensemble_metadata_repaired": ensemble_metadata_repaired,
                        "cost_metadata_repaired": cost_metadata_repaired,
                        "changed": metadata_repaired,
                    }
                else:
                    execution.setdefault(
                        "metadata_repair_summary",
                        {
                            "status": "already_attempted",
                            "generation_called": False,
                            "judge_called": False,
                            "changed": False,
                        },
                    )
            else:
                execution.setdefault("metadata_repair_attempted", False)
            row["execution"] = execution
            row["generation_attempt_budget_limit"] = GENERATION_MAX_ATTEMPTS
            row["generation_attempt_budget_used"] = coerce_metric_int(
                state.get("prior_generation_attempts_used")
            )
            if not isinstance(row.get("generation_completed_at"), int | float):
                legacy_completed_at = row.get("completed_at")
                if isinstance(legacy_completed_at, int | float):
                    row["generation_completed_at"] = float(legacy_completed_at)
            row["completed_at"] = time.time()
            row["cost_accounting"] = public_cost_accounting(row_cost_accounting(row))
            judge_complete = not judge_completion_reasons(
                row,
                judge_required=judge_required,
            )
            cost_metadata_complete = (
                bool(row["cost_accounting"].get("actual_llm_cost_complete")) and non_byok_ready
            )
            if not cost_metadata_complete:
                if not non_byok_ready:
                    row["error"] = (
                        "openrouter_non_byok_metadata_incomplete"
                        if non_byok_policy_safe
                        else "openrouter_non_byok_policy_violation"
                    )
                else:
                    row["error"] = "cost_metadata_incomplete"
            elif not judge_complete:
                row["error"] = (
                    JUDGE_ATTEMPT_BUDGET_EXHAUSTED_ERROR
                    if judge_attempt_budget_exhausted
                    else "judge_incomplete"
                )
            elif str(row.get("error") or "") in _REPAIRABLE_COMPLETION_ERRORS:
                row["error"] = None
            # Re-seal and re-run the same strict classifier used when loading a
            # resume source.  This prevents a metadata-only repair from
            # claiming completion while still carrying an unrepairable
            # identity or stale state that would fail again on the next run.
            row = seal_result_row(row)
            task_id = str(task["id"])
            post_state = resume_row_completion_state(
                row,
                expected_prompt_sha256=prompt_hashes.get(task_id, ""),
                expected_task_input_sha256=task_input_hashes.get(task_id, ""),
                expected_run_compatibility_fingerprint=(
                    args._run_compatibility["fingerprints"].get(group, "")
                ),
                expected_run_compatibility_contract=(
                    args._run_compatibility["contracts"].get(group)
                ),
                require_openrouter_non_byok=bool(
                    getattr(args, "require_openrouter_non_byok", False)
                ),
                judge_required=judge_required,
            )
            judge_complete = bool(post_state["judge_complete"])
            cost_metadata_complete = bool(post_state["cost_metadata_complete"])
            generation_accepted = bool(post_state["generation_valid"])
            incomplete_reasons = list(
                dict.fromkeys(
                    [
                        *post_state["generation_reasons"],
                        *post_state["judge_reasons"],
                        *post_state["cost_metadata_reasons"],
                    ]
                )
            )
            completion_complete = post_state["action"] == "complete"
            row["resume_completion"] = {
                "action": action,
                "generation_reused": True,
                "identity_repaired": identity_repaired,
                "metadata_repaired": metadata_repaired,
                "judge_reran": judge_reran,
                "judge_complete": judge_complete,
                "cost_metadata_complete": cost_metadata_complete,
                "post_repair_action": post_state["action"],
                "status": "complete" if completion_complete else "incomplete",
                "incomplete_reasons": incomplete_reasons,
            }
            row["completion_status"] = {
                "generation_accepted": generation_accepted,
                "cost_metadata_complete": cost_metadata_complete,
                "cost_metadata_scope": "actual_llm_spend",
                "all_provider_cost_complete": bool(
                    row["cost_accounting"].get("actual_spend_cost_complete")
                ),
                "actual_external_tool_cost_complete": bool(
                    row["cost_accounting"].get("actual_external_cost_complete")
                ),
                "judge_complete": judge_complete,
                "status": "complete" if completion_complete else "incomplete",
                "incomplete_reasons": list(incomplete_reasons),
            }
            if (
                row.get("final_text") != prior_final_text
                or _usage_generation_contract(row.get("usage")) != prior_usage_contract
            ):
                raise RuntimeError("Judge/metadata repair mutated accepted generation data")
            return row

    expected_result_keys = set(scheduled_keys)
    pending = [
        asyncio.create_task(
            _guarded(task, group)
            if (group, str(task["id"])) in regenerate_keys
            else _repair_prior_row(
                task,
                group,
                resume_states[(group, str(task["id"]))],
            )
        )
        for task in tasks
        for group in groups
        if (group, str(task["id"])) in scheduled_keys
    ]
    cost_audit_failure: dict[str, Any] | None = None
    cost_audit_failures: list[dict[str, Any]] = []
    repair_failures: list[dict[str, Any]] = []
    with (
        jsonl_path.open("w", encoding="utf-8") as fh,
        trace_path.open("w", encoding="utf-8") as trace_fh,
    ):
        for row_index, coro in enumerate(asyncio.as_completed(pending), start=1):
            row = await coro
            row["row_index"] = row_index
            resume_completion = row.get("resume_completion")
            if (
                isinstance(resume_completion, dict)
                and resume_completion.get("status") == "incomplete"
            ):
                repair_failures.append(
                    {
                        "stage": "resume_repair",
                        "group": row.get("group"),
                        "task_id": row.get("task_id"),
                        "action": resume_completion.get("action"),
                        "reasons": list(resume_completion.get("incomplete_reasons") or []),
                        "model_or_judge_started": bool(resume_completion.get("judge_reran")),
                    }
                )
            if getattr(
                args,
                "require_openrouter_non_byok",
                False,
            ) and not getattr(args, "dry_run", False):
                audit = openrouter_non_byok_audit(
                    row,
                    provider_routing=_openrouter_audit_provider_routing(
                        inherited.provider_routing,
                        (
                            getattr(args, "_g1_registry_contract", None)
                            if row.get("group") == "G1"
                            else None
                        ),
                    ),
                )
                row["openrouter_non_byok_audit"] = audit
                if not audit["pass"]:
                    if audit.get("policy_safe_to_continue") is not True:
                        prior_error = str(row.get("error") or "")
                        if prior_error and prior_error != "openrouter_non_byok_policy_violation":
                            execution = dict(row.get("execution") or {})
                            execution["prior_error_before_non_byok_policy_violation"] = prior_error
                            row["execution"] = execution
                        row["error"] = "openrouter_non_byok_policy_violation"
                    elif not row.get("error"):
                        row["error"] = "openrouter_non_byok_metadata_incomplete"
                    if audit.get("policy_safe_to_continue") is not True:
                        failure = {
                            "stage": "openrouter_non_byok_policy_violation",
                            "group": row.get("group"),
                            "task_id": row.get("task_id"),
                            "audit": audit,
                            "model_or_judge_started": True,
                        }
                        cost_audit_failures.append(failure)
                        if cost_audit_failure is None:
                            cost_audit_failure = failure
            row = seal_result_row(row)
            trace_value = trace_row(row)
            result_line = json.dumps(row, ensure_ascii=False, allow_nan=False)
            trace_line = json.dumps(trace_value, ensure_ascii=False, allow_nan=False)
            rows.append(row)
            fh.write(result_line + "\n")
            fh.flush()
            trace_fh.write(trace_line + "\n")
            trace_fh.flush()
            print(f"{row['group']} {row['task_id']} error={bool(row['error'])}", flush=True)
            if cost_audit_failure is not None:
                break
    if cost_audit_failure is not None:
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    result_failures: list[dict[str, Any]] = []
    for row in rows:
        if (
            cost_audit_failure is not None
            and str(row.get("error") or "") == "openrouter_non_byok_policy_violation"
        ):
            continue
        completion = row.get("completion_status")
        completion_complete = bool(
            isinstance(completion, Mapping) and completion.get("status") == "complete"
        )
        if not row.get("error") and completion_complete:
            continue
        reasons = (
            list(completion.get("incomplete_reasons") or [])
            if isinstance(completion, Mapping)
            else ["missing_completion_status"]
        )
        if row.get("error"):
            reasons.append(str(row["error"]))
        result_failures.append(
            {
                "stage": "result_completion",
                "group": row.get("group"),
                "task_id": row.get("task_id"),
                "reasons": list(dict.fromkeys(reason for reason in reasons if reason)),
                "model_or_judge_started": True,
            }
        )
    if cost_audit_failure is None:
        coverage = result_key_coverage(rows, expected_keys=expected_result_keys)
        if not coverage["pass"]:
            coverage_reasons: list[str] = []
            if coverage["missing_keys"]:
                coverage_reasons.append("missing_result_rows")
            if coverage["unexpected_keys"]:
                coverage_reasons.append("unexpected_result_rows")
            if coverage["duplicate_keys"]:
                coverage_reasons.append("duplicate_result_rows")
            result_failures.append(
                {
                    "stage": "result_coverage",
                    **coverage,
                    "reasons": coverage_reasons,
                    "model_or_judge_started": bool(rows),
                }
            )
    manifest_failure = cost_audit_failure
    if len(cost_audit_failures) > 1:
        manifest_failure = {
            "stage": "openrouter_non_byok_audit",
            "failure_count": len(cost_audit_failures),
            "failures": cost_audit_failures,
            "model_or_judge_started": True,
        }
    if repair_failures:
        repair_failure: dict[str, Any] = {
            "stage": "resume_repair",
            "failure_count": len(repair_failures),
            "failures": repair_failures,
            "model_or_judge_started": any(
                bool(item.get("model_or_judge_started")) for item in repair_failures
            ),
        }
        if manifest_failure is None:
            manifest_failure = repair_failure
        else:
            manifest_failure = {
                "stage": "multiple_failures",
                "failures": [manifest_failure, repair_failure],
                "model_or_judge_started": True,
            }
    if result_failures:
        result_failure: dict[str, Any] = {
            "stage": "result_completion",
            "failure_count": len(result_failures),
            "failures": result_failures,
            "model_or_judge_started": any(
                bool(item.get("model_or_judge_started")) for item in result_failures
            ),
        }
        if manifest_failure is None:
            manifest_failure = result_failure
        else:
            manifest_failure = {
                "stage": "multiple_failures",
                "failures": [manifest_failure, result_failure],
                "model_or_judge_started": True,
            }
    run_status = "complete"
    if cost_audit_failure is not None:
        run_status = "cost_audit_failed"
    elif result_failures or repair_failures:
        flattened_reasons = {
            str(reason)
            for failure in [*result_failures, *repair_failures]
            for reason in failure.get("reasons", [])
        }
        run_status = (
            "metadata_incomplete"
            if flattened_reasons
            and flattened_reasons
            <= {
                "cost_metadata_incomplete",
                "openrouter_non_byok_verification_failed",
                "openrouter_non_byok_metadata_incomplete",
            }
            else "judge_incomplete"
            if flattened_reasons and flattened_reasons <= {"judge_incomplete"}
            else "resume_repair_incomplete"
        )
    summary = summarize(rows)
    summary_path = jsonl_path.with_suffix(".md")
    summary_path.write_text(
        render_markdown(
            summary,
            jsonl_path,
            tool_policy=manifest_tool_policy,
            generation_policy=generation_policy,
            runner_mode=args.runner_mode,
            agent_max_iterations=args.agent_max_iterations,
            agent_finalization_policy=agent_finalization_policy,
        ),
        encoding="utf-8",
    )
    summary_json_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_manifest(
        manifest_path,
        args=args,
        stamp=stamp,
        status=run_status,
        started_at=run_started_at,
        finished_at=time.time(),
        tasks=tasks,
        groups=groups,
        rows_written=len(rows),
        artifacts=artifacts,
        summary=summary,
        tool_policy=manifest_tool_policy,
        command=command,
        failure=manifest_failure,
    )
    print(f"wrote {jsonl_path}")
    print(f"wrote {trace_path}")
    print(f"wrote {manifest_path}")
    print(f"wrote {command_path}")
    print(f"wrote {summary_json_path}")
    print(f"wrote {summary_path}")
    for key, path in artifacts.items():
        if key.startswith("experiment_config_"):
            print(f"wrote {path}")
    return 2 if manifest_failure is not None else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="DRACO JSONL input.")
    parser.add_argument(
        "--resume-from-jsonl",
        type=Path,
        action="append",
        default=[],
        help=(
            "Skip group/task pairs already present in this JSONL; repeatable. "
            "New results are always written to a separate timestamped JSONL."
        ),
    )
    parser.add_argument(
        "--expected-compatibility-manifest",
        type=Path,
        default=None,
        help=(
            "Fail before preflight or model calls unless the current per-group run "
            "fingerprints match this manifest."
        ),
    )
    parser.add_argument(
        "--only-group-task-keys",
        type=Path,
        default=None,
        help=(
            "Run only group/task pairs listed as JSONL objects containing group and "
            "task_id. Every listed pair must be selected by --groups and --input."
        ),
    )
    parser.add_argument("--config", type=Path, default=None, help="OpenSquilla TOML config.")
    parser.add_argument(
        "--experiment-config",
        type=Path,
        default=(
            Path(os.environ["OPENSQUILLA_DRACO_EXPERIMENT_CONFIG"])
            if os.environ.get("OPENSQUILLA_DRACO_EXPERIMENT_CONFIG")
            else None
        ),
        help=(
            "Base B2 experiment JSON. Defaults to the bundled G12-derived "
            "quality-first profile; "
            "OPENSQUILLA_DRACO_EXPERIMENT_CONFIG can inject another base file."
        ),
    )
    parser.add_argument(
        "--experiment-config-override",
        type=Path,
        action="append",
        default=[],
        help="JSON overlay applied after the base experiment config; repeatable.",
    )
    parser.add_argument(
        "--experiment-config-set",
        action="append",
        default=[],
        metavar="DOTTED.PATH=JSON_VALUE",
        help=(
            "Inline experiment-config override applied last; list indexes are supported "
            "and the option is repeatable."
        ),
    )
    parser.add_argument("--output-dir", type=Path, default=Path("reports/draco"))
    parser.add_argument(
        "--groups",
        required=True,
        help="Comma-separated experiment groups to run, for example B0,B1,G3,G8.",
    )
    parser.add_argument("--max-tasks", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument(
        "--ensemble-proposer-timeout",
        type=float,
        default=None,
        help=(
            "Per proposer request timeout in seconds for ensemble profile groups. "
            "By default the profile/default proposer timeout is used and is not "
            "expanded to match --timeout."
        ),
    )
    parser.add_argument(
        "--ensemble-aggregator-timeout",
        type=float,
        default=None,
        help=(
            "Per aggregator/scorer request timeout in seconds for ensemble profile "
            "groups. By default the profile/default aggregator timeout is used and "
            "is not expanded to match --timeout."
        ),
    )
    parser.add_argument(
        "--ensemble-proposer-early-stop-success-count",
        type=int,
        default=None,
        help=(
            "For ensemble profile groups, stop waiting for remaining proposers once "
            "this many successful candidate responses are available. Omit to use "
            "the profile default; pass 0 to disable."
        ),
    )
    parser.add_argument(
        "--ensemble-proposer-early-stop-after",
        type=float,
        default=None,
        help=(
            "Minimum seconds to wait before applying proposer early-stop. Omit to "
            "use the profile default; pass 0 to stop as soon as the success quorum "
            "is reached."
        ),
    )
    parser.add_argument(
        "--expand-ensemble-timeouts-to-task-timeout",
        action="store_true",
        help=(
            "Legacy behavior: distribute spare --timeout budget into per-member "
            "ensemble proposer/aggregator timeouts. Leave off for lower tail latency."
        ),
    )
    parser.add_argument(
        "--runner-mode",
        choices=SUPPORTED_RUNNER_MODES,
        default=DEFAULT_DRACO_RUNNER_MODE,
        help=(
            "Generation runner. agent_loop runs the full Agent tool loop; "
            "provider runs one provider.chat call for provider-level ablations."
        ),
    )
    parser.add_argument(
        "--agent-max-iterations",
        type=int,
        default=DEFAULT_AGENT_MAX_ITERATIONS,
        help=(
            "Maximum Agent LLM/tool-loop iterations when --runner-mode=agent_loop; "
            "0 means unlimited."
        ),
    )
    parser.add_argument(
        "--deadline-wrapup-margin-seconds",
        type=int,
        default=DEFAULT_DEADLINE_WRAPUP_MARGIN_SECONDS,
        help=(
            "Reserve this many seconds before the Agent deadline for a finalization "
            "pass; 0 preserves the legacy hard-deadline behavior."
        ),
    )
    parser.add_argument(
        "--deadline-wrapup-disable-tools",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_DEADLINE_WRAPUP_DISABLE_TOOLS,
        help="Disable tools during the deadline-triggered finalization pass.",
    )
    parser.add_argument(
        "--deadline-thinking-off-margin-seconds",
        type=int,
        default=DEFAULT_DEADLINE_THINKING_OFF_MARGIN_SECONDS,
        help=(
            "Disable model thinking this many seconds before the Agent deadline; "
            "0 disables the transition."
        ),
    )
    parser.add_argument(
        "--max-iterations-includes-finalization",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_MAX_ITERATIONS_INCLUDES_FINALIZATION,
        help="Count the finalization pass against --agent-max-iterations.",
    )
    parser.add_argument(
        "--retrieval-loop-finalization-threshold",
        type=int,
        default=DEFAULT_RETRIEVAL_LOOP_FINALIZATION_THRESHOLD,
        help=(
            "Trigger finalization after this many consecutive retrieval-only Agent "
            "iterations; 0 disables retrieval-loop finalization."
        ),
    )
    parser.add_argument(
        "--finalization-aggregator-only",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_FINALIZATION_AGGREGATOR_ONLY,
        help="Use only the ensemble aggregator during Agent finalization.",
    )
    parser.add_argument(
        "--finalization-disable-thinking",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_FINALIZATION_DISABLE_THINKING,
        help="Disable model thinking during Agent finalization.",
    )
    parser.add_argument(
        "--require-clean-source",
        action="store_true",
        help="Refuse to start unless the benchmark source worktree is clean and identifiable.",
    )
    parser.add_argument(
        "--repair-only-source-drift",
        action="store_true",
        help=(
            "Permit a clean repair commit to reuse an expected manifest only when "
            "source_identity is the sole compatibility difference. Requires "
            "--require-clean-source, --expected-compatibility-manifest, at least one "
            "--resume-from-jsonl, and --only-group-task-keys; any generation action "
            "then fails before provider or Judge construction."
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--local-web-tools-smoke-only",
        action="store_true",
        help=(
            "Configure local benchmark web tools, run a real search/fetch preflight, "
            "then exit before any model or judge calls."
        ),
    )
    parser.add_argument("--judge-model", default="")
    parser.add_argument("--judge-repeats", type=int, default=3)
    parser.add_argument("--judge-concurrency", type=int, default=1)
    parser.add_argument(
        "--judge-max-attempts",
        type=int,
        default=JUDGE_MAX_ATTEMPTS,
        help=(
            "Cumulative physical attempts for each Judge criterion/repeat unit "
            "across campaign resume waves; capped at 3."
        ),
    )
    parser.add_argument("--judge-candidates", action="store_true")
    parser.add_argument(
        "--generation-max-attempts",
        type=int,
        default=GENERATION_MAX_ATTEMPTS,
        help=(
            "Cumulative answer-generation attempts per group/task across campaign "
            "resume waves; capped at 3."
        ),
    )
    parser.add_argument(
        "--generation-max-tokens",
        type=int,
        default=DEFAULT_GENERATION_MAX_TOKENS_OVERRIDE,
        help=(
            "Override max completion tokens for generation and ensemble members; "
            "0 preserves the existing 16384-token/default profile behavior."
        ),
    )
    parser.add_argument(
        "--generation-retry-backoff",
        type=float,
        default=DEFAULT_GENERATION_RETRY_BACKOFF_SECONDS,
        help="Initial seconds to wait before retrying answer generation; doubles each retry.",
    )
    parser.add_argument(
        "--tool-mode",
        choices=SUPPORTED_TOOL_MODES,
        default=TOOL_MODE_PROVIDER_ONLY,
        help=(
            "Benchmark tool mode. provider_only attaches no external tools; "
            "local_web_tools attaches executable web_search and web_fetch tools "
            "for the Agent loop; openrouter_server_tools attaches OpenRouter "
            "server tools for provider-level runs."
        ),
    )
    parser.add_argument(
        "--contamination-blocked-domains",
        default=",".join(DEFAULT_CONTAMINATION_BLOCKED_DOMAINS),
        help=(
            "Comma-separated benchmark leakage domains to exclude from web search "
            "and block from web fetch when research tools are wired."
        ),
    )
    parser.add_argument(
        "--local-web-search-provider",
        choices=SUPPORTED_LOCAL_WEB_SEARCH_PROVIDERS,
        default=DEFAULT_LOCAL_WEB_SEARCH_PROVIDER,
        help=("Provider for executable local web_search when --tool-mode=local_web_tools."),
    )
    parser.add_argument(
        "--local-web-search-api-key-env",
        default="",
        help=(
            "Environment variable that contains the local web_search provider API key. "
            "Use BRAVE_SEARCH_API_KEY for --local-web-search-provider=brave. "
            "The key value is never written to benchmark command/manifest files."
        ),
    )
    parser.add_argument(
        "--allow-firecrawl-web-fetch",
        action="store_true",
        help=(
            "Allow web_fetch to escalate short/failed local extraction to paid Firecrawl. "
            "Disabled by default so benchmark tool spend is reproducible."
        ),
    )
    parser.add_argument(
        "--require-openrouter-non-byok",
        action="store_true",
        help=(
            "Fail the experiment unless every OpenRouter LLM usage record proves "
            "is_byok=false. Missing evidence fails closed."
        ),
    )
    parser.add_argument(
        "--continue-after-cost-audit-failure",
        action="store_true",
        help=(
            "Deprecated compatibility flag. Explicit OpenRouter BYOK/provider "
            "policy violations always stop the run immediately."
        ),
    )
    parser.add_argument(
        "--openrouter-web-search-engine",
        default=DEFAULT_OPENROUTER_WEB_SEARCH_ENGINE,
        help="OpenRouter web_search engine used when --tool-mode=openrouter_server_tools.",
    )
    parser.add_argument(
        "--openrouter-web-search-max-results",
        type=int,
        default=DEFAULT_OPENROUTER_WEB_SEARCH_MAX_RESULTS,
        help="Maximum results per OpenRouter web_search call.",
    )
    parser.add_argument(
        "--openrouter-web-search-max-total-results",
        type=int,
        default=DEFAULT_OPENROUTER_WEB_SEARCH_MAX_TOTAL_RESULTS,
        help="Maximum total results across OpenRouter web_search calls.",
    )
    parser.add_argument(
        "--openrouter-web-search-context-size",
        choices=("low", "medium", "high"),
        default=DEFAULT_OPENROUTER_WEB_SEARCH_CONTEXT_SIZE,
        help="Search context size for OpenRouter web_search.",
    )
    parser.add_argument(
        "--openrouter-web-fetch-engine",
        default=DEFAULT_OPENROUTER_WEB_FETCH_ENGINE,
        help="OpenRouter web_fetch engine used when --tool-mode=openrouter_server_tools.",
    )
    parser.add_argument(
        "--openrouter-web-fetch-max-uses",
        type=int,
        default=DEFAULT_OPENROUTER_WEB_FETCH_MAX_USES,
        help="Maximum OpenRouter web_fetch uses per request.",
    )
    parser.add_argument(
        "--openrouter-web-fetch-max-content-tokens",
        type=int,
        default=DEFAULT_OPENROUTER_WEB_FETCH_MAX_CONTENT_TOKENS,
        help="Maximum content tokens returned by OpenRouter web_fetch.",
    )
    parser.add_argument(
        "--openrouter-fusion-analysis-models",
        default=",".join(DEFAULT_OPENROUTER_FUSION_ANALYSIS_MODELS),
        help=(
            "Comma-separated OpenRouter Fusion panel models. Used by B8; "
            "OpenRouter documents 1 to 8 models."
        ),
    )
    parser.add_argument(
        "--openrouter-fusion-model",
        default=DEFAULT_OPENROUTER_FUSION_MODEL,
        help="OpenRouter Fusion judge model. Used by B8.",
    )
    parser.add_argument(
        "--openrouter-fusion-max-tool-calls",
        type=int,
        default=DEFAULT_OPENROUTER_FUSION_MAX_TOOL_CALLS,
        help=(
            "Maximum OpenRouter web_search/web_fetch steps for each Fusion "
            "panel model and judge call; range 1 to 16."
        ),
    )
    parser.add_argument(
        "--openrouter-fusion-max-completion-tokens",
        type=int,
        default=DEFAULT_OPENROUTER_FUSION_MAX_COMPLETION_TOKENS,
        help="Maximum completion tokens for each inner Fusion panel/judge call.",
    )
    parser.add_argument(
        "--openrouter-fusion-reasoning-effort",
        default=DEFAULT_OPENROUTER_FUSION_REASONING_EFFORT,
        help="Reasoning effort forwarded to OpenRouter Fusion panel and judge calls.",
    )
    parser.add_argument(
        "--openrouter-fusion-temperature",
        type=float,
        default=DEFAULT_OPENROUTER_FUSION_TEMPERATURE,
        help="Temperature forwarded to OpenRouter Fusion panel calls.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    args.command_argv = [sys.executable, *sys.argv]
    return asyncio.run(amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
