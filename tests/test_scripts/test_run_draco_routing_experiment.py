from __future__ import annotations

import ast
import asyncio
import importlib.util
import inspect
import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from opensquilla.engine.types import DoneEvent as AgentDoneEvent
from opensquilla.engine.types import ThinkingLevel
from opensquilla.gateway.config import GatewayConfig
from opensquilla.provider.ensemble import (
    _member_chat_config,
    build_ensemble_provider_from_config,
)
from opensquilla.provider.selector import ProviderConfig
from opensquilla.provider.types import (
    ChatConfig,
    DoneEvent,
    ErrorEvent,
    ProviderBillingReceipt,
)
from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.integration import configure_runtime, reset_runtime
from opensquilla.sandbox.run_context import PublicNetworkGrant, RunContext
from opensquilla.sandbox.run_mode import RunMode
from opensquilla.tool_boundary import ToolCall
from opensquilla.tools.types import current_tool_context

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "run_draco_routing_experiment.py"
RESUME_SCRIPT_PATH = SCRIPT_PATH.with_name("run_draco_routing_experiment_resume.py")
ROOT = SCRIPT_PATH.parent.parent


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "run_draco_routing_experiment_under_test",
        SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


runner = _load_runner()


def _openrouter_exact_evidence(
    cost: float,
    response_id: str,
    *,
    requested_model: str = "deepseek/deepseek-v4-pro",
    serving_provider: str = "DeepSeek",
    serving_model: str = "deepseek/deepseek-v4-pro-20260423",
) -> dict[str, object]:
    return {
        "is_byok": False,
        "provider_reported_cost": cost,
        "response_ids": [response_id],
        "router_metadata": {
            "requested": requested_model,
            "is_byok": False,
            "endpoints": {
                "available": [
                    {
                        "provider": serving_provider,
                        "model": serving_model,
                        "selected": True,
                    }
                ]
            },
            "attempts": [
                {
                    "provider": serving_provider,
                    "model": serving_model,
                    "status": 200,
                }
            ],
        },
    }


def _complete_legacy_judge(
    response_id: str,
    *,
    score: float = 4.0,
) -> dict[str, object]:
    cost = 0.002
    return {
        "mode": "legacy_dimension_score",
        "judge_model": "judge-model",
        "scores": {
            "accuracy": score,
            "completeness": score,
            "objectivity": score,
            "citation": score,
        },
        "score_status": "complete",
        "judge_error_count": 0,
        "total": score / 5.0 * 100.0,
        "judge_attempt_count": 1,
        "judge_new_attempt_count": 1,
        "judge_attempts": [
            {
                "attempt": 1,
                "run": {
                    "llm_request_count": 1,
                    "usage": {
                        "provider": "openrouter",
                        "model": "judge-model",
                        "input_tokens": 5,
                        "output_tokens": 2,
                        "billed_cost": cost,
                        "cost_source": "provider_billed",
                        "provider_usage": _openrouter_exact_evidence(
                            cost,
                            response_id,
                        ),
                    },
                },
            }
        ],
    }


def _valid_ensemble_trace(
    *,
    selection_mode: str,
    llm_request_count: int,
    total_candidates: int = 4,
    successful_proposers: int = 3,
    final_text: str = "answer",
) -> dict[str, object]:
    candidates = []
    for index in range(total_candidates):
        ok = index < successful_proposers
        text = f"candidate-{index}" if ok else ""
        candidates.append(
            {
                "index": index,
                "sample_index": 0,
                "provider": "openrouter" if ok else "",
                "requested_provider": "openrouter",
                "model": f"model-{index}" if ok else "",
                "requested_model": f"model-{index}",
                "ok": ok,
                "request_started": True,
                "physical_request_count": 1,
                "usage_reported": ok,
                "stop_reason": "stop" if ok else "",
                "content": {
                    "text": text,
                    "chars": len(text),
                    "truncated": False,
                },
                **(
                    {}
                    if ok
                    else {
                        "error": "candidate failed",
                        "error_code": "test_failure",
                    }
                ),
            }
        )
    return {
        "llm_request_count": llm_request_count,
        "selection_strategy": selection_mode,
        "fallback_used": False,
        "final_request_role": "aggregator",
        "total_candidates": total_candidates,
        "successful_proposers": successful_proposers,
        "candidates": candidates,
        "final_request": {
            "role": "aggregator",
            "request_started": True,
            "usage": {
                "provider": "openrouter",
                "model": "aggregator",
                "requested_provider": "openrouter",
                "requested_model": "aggregator",
                "stop_reason": "stop",
            },
            "output": {
                "text": final_text,
                "chars": len(final_text),
                "truncated": False,
            },
        },
    }


def _load_resume_runner():
    spec = importlib.util.spec_from_file_location(
        "run_draco_routing_experiment_resume_under_test",
        RESUME_SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


resume_runner = _load_resume_runner()


@pytest.fixture
def configured_tool_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Exercise web-tool behavior through a granted Standard sandbox context."""

    configure_runtime(
        SandboxSettings(),
        approval_queue=runner._BenchmarkApprovalQueue(),
        workspace=tmp_path,
    )
    original_builder = runner.build_benchmark_tool_context

    def _standard_context(**kwargs):
        context = original_builder(**kwargs)
        context.run_mode = RunMode.STANDARD.value
        context.sandbox_run_context = RunContext(
            run_mode=RunMode.STANDARD,
            workspace=str(tmp_path),
            public_network=(PublicNetworkGrant(scope="chat", source="test"),),
            source="test_standard_network_grant",
        )
        return context

    monkeypatch.setattr(runner, "build_benchmark_tool_context", _standard_context)
    try:
        yield
    finally:
        reset_runtime()


@pytest.mark.asyncio
async def test_judge_concurrency_is_capped_across_concurrent_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = 0
    peak = 0

    async def fake_judge_criterion(*, criterion, repeat_index, **_kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {
            **criterion,
            "repeat_index": repeat_index,
            "verdict": "MET",
            "met": True,
            "rationale": "ok",
        }

    monkeypatch.setattr(runner, "judge_criterion", fake_judge_criterion)
    task = {
        "id": "task-1",
        "prompt": "prompt",
        "rubric": {
            "id": "rubric-1",
            "sections": [
                {
                    "id": "section-1",
                    "title": "Section",
                    "criteria": [
                        {"id": f"criterion-{index}", "weight": 1, "requirement": "x"}
                        for index in range(3)
                    ],
                }
            ],
        },
    }
    shared = asyncio.Semaphore(2)
    await asyncio.gather(
        *[
            runner.judge_text(
                judge_provider=object(),
                task=task,
                answer="answer",
                dry_run=False,
                judge_repeats=1,
                judge_concurrency=6,
                judge_semaphore=shared,
            )
            for _ in range(2)
        ]
    )

    assert peak == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module",
    [runner, _load_resume_runner()],
    ids=["main", "resume"],
)
async def test_legacy_judge_uses_the_experiment_wide_semaphore(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = 0
    peak = 0

    async def fake_collect_run(*_args, **_kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return module.RunResult(
            final_text=json.dumps(
                {
                    "scores": {
                        "accuracy": 5,
                        "completeness": 5,
                        "objectivity": 5,
                        "citation": 5,
                    },
                    "total": 20,
                    "rationale": "ok",
                }
            ),
            done=None,
        )

    monkeypatch.setattr(module, "collect_run", fake_collect_run)
    shared = asyncio.Semaphore(2)
    task = {"id": "task-1", "prompt": "prompt", "rubric": "legacy rubric"}
    await asyncio.gather(
        *[
            module.judge_text(
                judge_provider=object(),
                task=task,
                answer="answer",
                dry_run=False,
                judge_concurrency=8,
                judge_semaphore=shared,
            )
            for _ in range(5)
        ]
    )

    assert peak == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module",
    [runner, resume_runner],
    ids=["main", "resume"],
)
async def test_judge_attempt_budget_is_cumulative_per_unit_across_waves(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    physical_calls = 0

    async def fake_collect_run(*_args, **_kwargs):
        nonlocal physical_calls
        physical_calls += 1
        return module.RunResult(
            final_text="not-json",
            done=None,
            trace_events=[
                {
                    "kind": "error",
                    "code": "provider_stream_close_failed",
                }
            ],
        )

    monkeypatch.setattr(module, "collect_run", fake_collect_run)
    provider = type("JudgeProvider", (), {"model": "judge-model"})()
    task = {
        "id": "task-1",
        "prompt": "prompt",
        "rubric": {
            "id": "rubric-1",
            "sections": [
                {
                    "id": "section-1",
                    "title": "Section",
                    "criteria": [
                        {
                            "id": "criterion-1",
                            "weight": 1,
                            "requirement": "x",
                        }
                    ],
                }
            ],
        },
    }
    prior = None
    for expected_wave in range(1, 4):
        prior = await module.judge_text(
            judge_provider=provider,
            task=task,
            answer="answer",
            dry_run=False,
            judge_repeats=2,
            judge_concurrency=2,
            judge_max_attempts=3,
            prior_judge=prior,
        )
        assert prior is not None
        assert prior["judge_new_attempt_count"] == 2
        assert physical_calls == expected_wave * 2
        for judgment in prior["criterion_judgments"]:
            assert judgment["judge_attempt_count"] == expected_wave
            assert judgment["prior_judge_attempts_used"] == expected_wave - 1
            assert [attempt["attempt"] for attempt in judgment["judge_attempts"]] == (
                list(range(1, expected_wave + 1))
            )

    attempt_ids = [
        attempt["attempt_id"]
        for judgment in prior["criterion_judgments"]
        for attempt in judgment["judge_attempts"]
    ]
    assert len(attempt_ids) == 6
    assert len(set(attempt_ids)) == 6
    assert prior["judge_attempt_budget_exhausted"] is True
    assert prior["judge_attempt_budget_exhausted_count"] == 2

    closed = await module.judge_text(
        judge_provider=provider,
        task=task,
        answer="answer",
        dry_run=False,
        judge_repeats=2,
        judge_concurrency=2,
        judge_max_attempts=3,
        prior_judge=prior,
    )

    assert closed is not None
    assert physical_calls == 6
    assert closed["judge_new_attempt_count"] == 0
    assert closed["judge_attempt_budget_exhausted"] is True
    assert all(
        judgment["error"] == module.JUDGE_ATTEMPT_BUDGET_EXHAUSTED_ERROR
        and judgment["judge_attempt_count"] == 3
        and judgment["judge_attempt_budget_remaining"] == 0
        for judgment in closed["criterion_judgments"]
    )


def _experiment_config():
    return runner.load_draco_experiment_config(runner.DEFAULT_B2_EXPERIMENT_CONFIG_PATH).config


def test_reasoning_tokens_are_not_double_counted_as_total_tokens() -> None:
    usage = {
        "input_tokens": 50,
        "output_tokens": 100,
        "reasoning_tokens": 80,
    }

    assert runner._usage_token_count(usage) == 150
    assert _load_resume_runner()._usage_token_count(usage) == 150


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
def test_task_analyzer_provider_preserves_routing_and_disables_replay(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    sentinel = object()

    class _Selector:
        def __init__(self, config) -> None:
            captured["primary"] = config.primary

        def resolve(self):
            return sentinel

    monkeypatch.setattr(module, "ModelSelector", _Selector)
    routed = ProviderConfig(
        provider="openrouter",
        model="original",
        api_key="fake",
        base_url="https://openrouter.example/api/v1",
        provider_routing={"order": ["OpenAI"], "allow_fallbacks": False},
        replay_provider_state=True,
    )

    resolved = module.build_task_analyzer_provider(
        routed,
        provider_id="openrouter",
        model_id="openai/gpt-analyzer",
    )
    primary = captured["primary"]

    assert resolved is sentinel
    assert primary.provider_routing == routed.provider_routing
    assert primary.replay_provider_state is False
    assert primary.model == "openai/gpt-analyzer"


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
def test_task_analyzer_usage_separates_actual_from_requested_identity(module) -> None:
    row = module.task_analyzer_usage_row(
        {
            "attempt": 1,
            "physical_attempt_id": "1" * 32,
            "provider": "physical-provider",
            "model": "physical-model",
            "input_tokens": 3,
            "output_tokens": 1,
        },
        provider_id="requested-provider",
        model_id="requested-model",
        source="llm",
        fallback_reason="",
    )
    unknown = module.task_analyzer_usage_row(
        {},
        provider_id="requested-provider",
        model_id="requested-model",
        source="fallback",
        fallback_reason="missing receipt",
    )
    legacy_aggregate = module.task_analyzer_usage_rows(
        {
            "provider": "physical-provider",
            "model": "physical-model",
            "attempt_count": 3,
            "input_tokens": 9,
            "output_tokens": 3,
            "billed_cost": 0.3,
            "cost_source": "provider_billed",
            "provider_usage": {"response_ids": ["r1", "r2", "r3"]},
        },
        provider_id="requested-provider",
        model_id="requested-model",
        source="llm",
        fallback_reason="",
    )

    assert row["provider"] == "physical-provider"
    assert row["model"] == "physical-model"
    assert row["requested_provider"] == "requested-provider"
    assert row["requested_model"] == "requested-model"
    assert row["request_count"] == 1
    assert row["physical_attempt_id"] == "1" * 32
    assert unknown["provider"] == ""
    assert unknown["model"] == ""
    assert unknown["requested_provider"] == "requested-provider"
    assert unknown["requested_model"] == "requested-model"
    assert unknown["request_count"] == 1
    assert unknown["role"] == "unknown_request"
    assert len(legacy_aggregate) == 3
    assert all(item["role"] == "unknown_request" for item in legacy_aggregate)
    assert all(item["request_count"] == 1 for item in legacy_aggregate)
    assert len({item["physical_attempt_id"] for item in legacy_aggregate}) == 3
    assert sum(item["billed_cost"] for item in legacy_aggregate) == 0.0
    aggregate_evidence = legacy_aggregate[0]["provider_usage"]["unallocated_aggregate_usage"]
    assert aggregate_evidence["response_ids"] == ["r1", "r2", "r3"]
    assert aggregate_evidence["billed_cost"] == pytest.approx(0.3)


@pytest.mark.parametrize("loaded_runner", [runner, resume_runner])
def test_task_analyzer_retry_usage_expands_to_distinct_physical_units(
    loaded_runner,
) -> None:
    usage = {
        "attempt_count": 2,
        "physical_attempts": [
            {
                "attempt": 1,
                "physical_attempt_id": "1" * 32,
                "provider": "openrouter",
                "model": "anthropic/claude-opus-4.8",
                "requested_provider": "openrouter",
                "requested_model": "anthropic/claude-opus-4.8",
                "input_tokens": 11,
                "output_tokens": 2,
                "billed_cost": 0.01,
                "cost_source": "provider_billed",
                "provider_usage": {"response_ids": ["analyzer-1"]},
            },
            {
                "attempt": 2,
                "physical_attempt_id": "2" * 32,
                "provider": "openrouter",
                "model": "anthropic/claude-opus-4.8",
                "requested_provider": "openrouter",
                "requested_model": "anthropic/claude-opus-4.8",
                "input_tokens": 12,
                "output_tokens": 3,
                "billed_cost": 0.02,
                "cost_source": "provider_billed",
                "provider_usage": {"response_ids": ["analyzer-2"]},
            },
        ],
    }
    rows = loaded_runner.task_analyzer_usage_rows(
        usage,
        provider_id="openrouter",
        model_id="anthropic/claude-opus-4.8",
        source="llm_provider",
        fallback_reason="",
    )

    assert len(rows) == 2
    assert [row["request_count"] for row in rows] == [1, 1]
    assert [row["attempt"] for row in rows] == [1, 2]
    assert [row["physical_attempt_id"] for row in rows] == [
        "1" * 32,
        "2" * 32,
    ]
    assert [row["provider_usage"]["response_ids"][0] for row in rows] == [
        "analyzer-1",
        "analyzer-2",
    ]
    assert loaded_runner.usage_rows_request_count(rows) == 2


def _openrouter_config() -> tuple[GatewayConfig, ProviderConfig]:
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "fake",
            "base_url": "https://openrouter.example/api/v1",
        },
        llm_ensemble={
            "enabled": True,
            "selection_mode": "static_openrouter_b5",
        },
    )
    inherited = ProviderConfig(
        provider="openrouter",
        model="deepseek/deepseek-v4-pro",
        api_key="fake",
        base_url="https://openrouter.example/api/v1",
        provider_routing={
            "deepseek/deepseek-v4-pro": "deepseek",
        },
    )
    return config, inherited


def test_b2_argument_alignment_reproduces_g12_run_envelope() -> None:
    args = runner.build_parser().parse_args(
        [
            "--input",
            "tasks.jsonl",
            "--groups",
            "B2",
            "--concurrency",
            "8",
            "--timeout",
            "180",
            "--runner-mode",
            "provider",
            "--tool-mode",
            "provider_only",
            "--local-web-search-provider",
            "duckduckgo",
        ]
    )

    record = runner.apply_b2_g12_argument_alignment(args, ["B2"])

    assert record is not None
    assert record["requested_args"]["concurrency"] == 8
    assert record["requested_args"]["timeout"] == 180.0
    assert args.concurrency == 2
    assert args.timeout == 3600.0
    assert args.ensemble_proposer_timeout == pytest.approx(907.5)
    assert args.ensemble_aggregator_timeout == pytest.approx(2662.5)
    assert args.runner_mode == "agent_loop"
    assert args.agent_max_iterations == 12
    assert args.deadline_wrapup_margin_seconds == 600
    assert args.deadline_wrapup_disable_tools is True
    assert args.deadline_thinking_off_margin_seconds == 600
    assert args.max_iterations_includes_finalization is True
    assert args.retrieval_loop_finalization_threshold == 3
    assert args.finalization_aggregator_only is True
    assert args.finalization_disable_thinking is True
    assert args.generation_max_tokens == 16_384
    assert args.generation_max_attempts == 3
    assert args.tool_mode == "local_web_tools"
    assert args.local_web_search_provider == "brave"
    assert args.local_web_search_api_key_env == "BRAVE_SEARCH_API_KEY"
    assert args.judge_model == "google/gemini-3.1-pro-preview"
    assert args.judge_repeats == 3
    assert args.judge_concurrency == 6
    assert args.judge_max_attempts == 3


def test_generation_config_preserves_provider_native_max_level() -> None:
    policy = runner.generation_thinking_policy()

    config = runner.generation_chat_config(
        policy,
        model="moonshotai/kimi-k2.7-code",
    )

    assert config.thinking is True
    assert config.thinking_level == "max"
    assert config.thinking_budget_tokens == 50_000


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
@pytest.mark.parametrize(
    ("model", "expected_level"),
    [
        ("anthropic/claude-opus-4.8", "max"),
        ("openai/gpt-5.5", "xhigh"),
    ],
)
def test_generation_config_enables_openrouter_baseline_reasoning(
    module,
    model: str,
    expected_level: str,
) -> None:
    config = module.generation_chat_config(
        module.generation_thinking_policy(),
        model=model,
    )

    assert config.thinking is True
    assert config.thinking_level == expected_level
    assert config.model_capabilities is not None
    assert config.model_capabilities.supports_reasoning is True
    assert config.model_capabilities.reasoning_format == "openrouter"


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_router_single_selected_model_receives_reasoning_capabilities(module) -> None:
    config = module.generation_chat_config(
        module.generation_thinking_policy(),
        model=None,
    )
    assert config.model_capabilities is None

    resolved = module.with_openrouter_model_capabilities(config, "openai/gpt-5.5")

    assert resolved.model_capabilities is not None
    assert resolved.model_capabilities.supports_reasoning is True
    assert resolved.model_capabilities.reasoning_format == "openrouter"


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_agent_loop_preserves_provider_native_max_thinking(module) -> None:
    chat_config = module.generation_chat_config(
        module.generation_thinking_policy(),
        model="anthropic/claude-opus-4.8",
    )

    agent_config = module.agent_config_from_chat_config(
        chat_config,
        timeout=3600,
        model_id="anthropic/claude-opus-4.8",
        max_iterations=12,
    )

    assert agent_config.thinking is ThinkingLevel.MAX
    assert agent_config.resolve_thinking("prompt") == (True, 50_000)


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_agent_finalization_cli_defaults_off_and_can_be_enabled(module) -> None:
    defaults = module.build_parser().parse_args(["--input", "tasks.jsonl", "--groups", "B1"])
    assert module.agent_finalization_policy_from_args(defaults) == (
        module.DEFAULT_AGENT_FINALIZATION_POLICY
    )

    enabled = module.build_parser().parse_args(
        [
            "--input",
            "tasks.jsonl",
            "--groups",
            "B1",
            "--deadline-wrapup-margin-seconds",
            "600",
            "--deadline-wrapup-disable-tools",
            "--deadline-thinking-off-margin-seconds",
            "600",
            "--max-iterations-includes-finalization",
            "--retrieval-loop-finalization-threshold",
            "3",
            "--finalization-aggregator-only",
            "--finalization-disable-thinking",
        ]
    )

    assert module.agent_finalization_policy_from_args(enabled) == {
        "deadline_wrapup_margin_seconds": 600,
        "deadline_wrapup_disable_tools": True,
        "deadline_thinking_off_margin_seconds": 600,
        "max_iterations_includes_finalization": True,
        "retrieval_loop_finalization_threshold": 3,
        "finalization_aggregator_only": True,
        "finalization_disable_thinking": True,
    }


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_agent_runner_normalization_preserves_explicit_zero_iterations(module) -> None:
    args = module.build_parser().parse_args(
        [
            "--input",
            "tasks.jsonl",
            "--groups",
            "B1",
            "--agent-max-iterations",
            "0",
        ]
    )

    policy = module.normalize_agent_runner_args(args)

    assert args.agent_max_iterations == 0
    assert policy == module.DEFAULT_AGENT_FINALIZATION_POLICY


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_agent_config_threads_finalization_policy(module) -> None:
    policy = {
        "deadline_wrapup_margin_seconds": 600,
        "deadline_wrapup_disable_tools": True,
        "deadline_thinking_off_margin_seconds": 600,
        "max_iterations_includes_finalization": True,
        "retrieval_loop_finalization_threshold": 3,
        "finalization_aggregator_only": True,
        "finalization_disable_thinking": True,
    }

    agent_config = module.agent_config_from_chat_config(
        None,
        timeout=3600,
        model_id="test/model",
        max_iterations=0,
        finalization_policy=policy,
    )

    assert agent_config.max_iterations == 0
    for field_name, expected in policy.items():
        assert getattr(agent_config, field_name) == expected
    assert agent_config.metadata["agent_finalization_policy"] == policy


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
async def test_agent_hard_timeout_is_preserved_without_generation_retry(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    async def fake_collect_agent_run(*_args, **kwargs):
        calls.append(kwargs)
        return module.RunResult(
            final_text="partial progress",
            done=None,
            error="TimeoutError: agent run timed out after 3600s",
            trace_events=[{"kind": "timeout", "timeout_s": 3600}],
        )

    monkeypatch.setattr(module, "collect_agent_run", fake_collect_agent_run)
    policy = {
        **module.DEFAULT_AGENT_FINALIZATION_POLICY,
        "deadline_wrapup_margin_seconds": 600,
    }

    result, attempts, selected_attempt = await module.collect_generation_with_retries(
        object(),
        "prompt",
        timeout=3600,
        runner_mode=module.RUNNER_MODE_AGENT_LOOP,
        tool_policy={},
        finalization_policy=policy,
        max_attempts=3,
    )

    assert len(calls) == 1
    assert calls[0]["finalization_policy"] == policy
    assert result.error == "TimeoutError: agent run timed out after 3600s"
    assert result.final_text == "partial progress"
    assert selected_attempt == 0
    assert len(attempts) == 1
    assert len(attempts[0]["attempt_id"]) == 32
    assert set(attempts[0]["attempt_id"]) <= set("0123456789abcdef")
    assert attempts[0]["attempt_kind"] == "generation"
    assert attempts[0]["started_at"] <= attempts[0]["completed_at"]
    assert attempts[0]["will_retry"] is False
    assert attempts[0]["retry_suppressed_reason"] == "agent_hard_timeout"


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
async def test_generation_attempt_offset_uses_cumulative_ordinal(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_collect_run(*_args, **_kwargs):
        return module.RunResult(
            final_text="accepted",
            done=DoneEvent(
                provider="openrouter",
                model="model-a",
                requested_provider="openrouter",
                requested_model="model-a",
                stop_reason="stop",
            ),
        )

    monkeypatch.setattr(module, "collect_run", fake_collect_run)

    result, attempts, selected_attempt = await module.collect_generation_with_retries(
        object(),
        "prompt",
        timeout=30,
        max_attempts=3,
        attempt_offset=2,
        expected_model="model-a",
        expected_provider="openrouter",
    )

    assert result.final_text == "accepted"
    assert len(attempts) == 1
    assert attempts[0]["attempt"] == 3
    assert selected_attempt == 3


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_single_generation_identity_is_checked_before_judge(module) -> None:
    valid = module.RunResult(
        final_text="answer",
        done=DoneEvent(
            provider="openrouter",
            model="model-a",
            requested_provider="openrouter",
            requested_model="model-a",
            stop_reason="stop",
        ),
    )
    assert (
        module.generation_retry_reason(
            valid,
            expected_model="model-a",
            expected_provider="openrouter",
        )
        == ""
    )

    wrong_provider = module.RunResult(
        final_text="answer",
        done=DoneEvent(
            provider="unexpected",
            model="model-a",
            requested_provider="openrouter",
            requested_model="model-a",
            stop_reason="stop",
        ),
    )
    assert (
        module.generation_retry_reason(
            wrong_provider,
            expected_model="model-a",
            expected_provider="openrouter",
        )
        == "wrong_actual_provider"
    )

    inferred = module.RunResult(
        final_text="answer",
        done=DoneEvent(
            provider="",
            model="",
            requested_provider="openrouter",
            requested_model="model-a",
            stop_reason="stop",
            model_usage_breakdown=[
                {
                    "provider": "openrouter",
                    "model": "model-a",
                    "input_tokens": 1,
                    "output_tokens": 1,
                }
            ],
        ),
    )
    assert (
        module.generation_retry_reason(
            inferred,
            expected_model="model-a",
            expected_provider="openrouter",
        )
        == ""
    )


@pytest.mark.asyncio
async def test_run_one_records_and_forwards_agent_finalization_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, inherited = _openrouter_config()
    captured: dict[str, object] = {}
    policy = {
        "deadline_wrapup_margin_seconds": 600,
        "deadline_wrapup_disable_tools": True,
        "deadline_thinking_off_margin_seconds": 600,
        "max_iterations_includes_finalization": True,
        "retrieval_loop_finalization_threshold": 3,
        "finalization_aggregator_only": True,
        "finalization_disable_thinking": True,
    }
    result = runner.RunResult(
        final_text="answer",
        done=DoneEvent(model="test/model", stop_reason="stop"),
    )

    async def fake_build_experiment_provider(**_kwargs):
        return runner.ProviderBuildResult(provider=object(), prompt="prompt")

    async def fake_collect_generation_with_retries(*_args, **kwargs):
        captured.update(kwargs)
        return result, [], 1

    async def fake_judge_text(**_kwargs):
        return None

    monkeypatch.setattr(
        runner,
        "build_experiment_provider",
        fake_build_experiment_provider,
    )
    monkeypatch.setattr(
        runner,
        "collect_generation_with_retries",
        fake_collect_generation_with_retries,
    )
    monkeypatch.setattr(runner, "judge_text", fake_judge_text)

    row = await runner.run_one(
        task={"id": "task-1", "prompt": "prompt"},
        group="B3",
        config=config,
        inherited=inherited,
        dry_run=False,
        judge_provider=None,
        judge_candidates=False,
        judge_repeats=1,
        judge_concurrency=1,
        judge_max_attempts=1,
        judge_semaphore=None,
        timeout=3600,
        ensemble_proposer_timeout=None,
        ensemble_aggregator_timeout=None,
        ensemble_proposer_early_stop_success_count=None,
        ensemble_proposer_early_stop_after=None,
        expand_ensemble_timeouts_to_task_timeout=False,
        tool_policy={"tools_enabled": False, "tool_mode": "provider_only"},
        generation_policy={},
        runner_mode=runner.RUNNER_MODE_AGENT_LOOP,
        agent_finalization_policy=policy,
    )

    assert captured["finalization_policy"] == policy
    assert row["agent_finalization_policy"] == policy
    assert row["execution"]["agent_finalization_policy"] == policy


@pytest.mark.asyncio
async def test_run_one_separates_selected_attempt_metrics_from_actual_spend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, inherited = _openrouter_config()

    def attempt_result(
        *,
        final_text: str,
        billed_cost: float,
        response_id: str,
        latency_ms: int,
        error: str = "",
    ) -> runner.RunResult:
        rows = [
            {
                "provider": "openrouter",
                "model": f"model-{index}",
                "input_tokens": 10,
                "output_tokens": 2,
                "billed_cost": billed_cost / 2,
                "cost_source": "provider_billed",
                "provider_usage": _openrouter_exact_evidence(
                    billed_cost / 2,
                    f"{response_id}-{index}",
                ),
            }
            for index in range(2)
        ]
        return runner.RunResult(
            final_text=final_text,
            done=DoneEvent(
                input_tokens=20,
                output_tokens=4,
                billed_cost=billed_cost,
                cost_source="provider_billed",
                model="aggregator",
                model_usage_breakdown=rows,
                ensemble_trace=_valid_ensemble_trace(
                    selection_mode="router_tree_baseline",
                    llm_request_count=2,
                    final_text=final_text,
                ),
            ),
            error=error,
            latency_ms=latency_ms,
        )

    failed = attempt_result(
        final_text="",
        billed_cost=1.0,
        response_id="failed",
        latency_ms=10,
        error="empty_generation_output",
    )
    selected = attempt_result(
        final_text="accepted answer",
        billed_cost=0.2,
        response_id="selected",
        latency_ms=20,
    )
    attempts = [
        {
            "attempt": 1,
            "retryable": True,
            "retry_reason": "empty_generation_output",
            "will_retry": True,
            "run": runner.run_result_summary(failed),
        },
        {
            "attempt": 2,
            "retryable": False,
            "retry_reason": "",
            "will_retry": False,
            "run": runner.run_result_summary(selected),
        },
    ]

    async def fake_build_experiment_provider(**_kwargs):
        return runner.ProviderBuildResult(provider=object(), prompt="prompt")

    async def fake_collect_generation_with_retries(*_args, **_kwargs):
        return selected, attempts, 2

    monkeypatch.setattr(
        runner,
        "build_experiment_provider",
        fake_build_experiment_provider,
    )
    monkeypatch.setattr(
        runner,
        "collect_generation_with_retries",
        fake_collect_generation_with_retries,
    )

    row = await runner.run_one(
        task={"id": "task-1", "prompt": "prompt"},
        group="B3",
        config=config,
        inherited=inherited,
        dry_run=False,
        judge_provider=None,
        judge_candidates=False,
        judge_repeats=1,
        judge_concurrency=1,
        judge_max_attempts=1,
        judge_semaphore=None,
        timeout=60,
        ensemble_proposer_timeout=None,
        ensemble_aggregator_timeout=None,
        ensemble_proposer_early_stop_success_count=None,
        ensemble_proposer_early_stop_after=None,
        expand_ensemble_timeouts_to_task_timeout=False,
        tool_policy={"tools_enabled": False, "tool_mode": "provider_only"},
        generation_policy={},
    )

    assert row["final_text"] == "accepted answer"
    assert row["generation_attempt_count"] == 2
    assert row["latency_ms"] == 20
    assert row["llm_request_count"] == 2
    assert row["selected_attempt_billed_cost_usd"] == pytest.approx(0.2)
    assert row["selected_attempt_metrics"]["generation_attempt"] == 2
    assert row["actual_spend_metrics"]["latency_ms"] == 30
    assert row["actual_spend_metrics"]["llm_request_count"] == 4
    assert row["actual_spend_billed_cost_usd"] == pytest.approx(1.2)
    assert row["cost_accounting"]["generation"]["recorded_cost_usd"] == (pytest.approx(0.2))
    assert row["cost_accounting"]["actual_generation_spend"]["recorded_cost_usd"] == pytest.approx(
        1.2
    )


@pytest.mark.asyncio
async def test_run_one_keeps_failed_generation_spend_out_of_selected_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, inherited = _openrouter_config()
    usage_row = {
        "provider": "openrouter",
        "model": "failed-model",
        "input_tokens": 10,
        "output_tokens": 2,
        "billed_cost": 0.5,
        "cost_source": "provider_billed",
        "provider_usage": _openrouter_exact_evidence(0.5, "failed-generation"),
    }
    failed = runner.RunResult(
        final_text="partial answer",
        done=DoneEvent(
            model="failed-model",
            billed_cost=0.5,
            cost_source="provider_billed",
            model_usage_breakdown=[usage_row],
            ensemble_trace={"llm_request_count": 1},
        ),
        error="TimeoutError: generation failed",
        latency_ms=25,
    )
    attempts = [
        {
            "attempt": 1,
            "retryable": True,
            "retry_reason": failed.error,
            "retry_suppressed_reason": "agent_hard_timeout",
            "will_retry": False,
            "run": runner.run_result_summary(failed),
        }
    ]

    async def fake_build_experiment_provider(**_kwargs):
        return runner.ProviderBuildResult(provider=object(), prompt="prompt")

    async def fake_collect_generation_with_retries(*_args, **_kwargs):
        return failed, attempts, 0

    monkeypatch.setattr(
        runner,
        "build_experiment_provider",
        fake_build_experiment_provider,
    )
    monkeypatch.setattr(
        runner,
        "collect_generation_with_retries",
        fake_collect_generation_with_retries,
    )

    row = await runner.run_one(
        task={"id": "task-1", "prompt": "prompt"},
        group="B3",
        config=config,
        inherited=inherited,
        dry_run=False,
        judge_provider=None,
        judge_candidates=False,
        judge_repeats=1,
        judge_concurrency=1,
        judge_max_attempts=1,
        judge_semaphore=None,
        timeout=60,
        ensemble_proposer_timeout=None,
        ensemble_aggregator_timeout=None,
        ensemble_proposer_early_stop_success_count=None,
        ensemble_proposer_early_stop_after=None,
        expand_ensemble_timeouts_to_task_timeout=False,
        tool_policy={"tools_enabled": False, "tool_mode": "provider_only"},
        generation_policy={},
    )

    assert row["selected_generation_succeeded"] is False
    assert row["selected_attempt_metrics"]["generation_attempt"] == 0
    assert row["selected_attempt_metrics"]["llm_request_count"] == 0
    assert row["selected_attempt_billed_cost_usd"] == 0.0
    assert row["actual_spend_metrics"]["llm_request_count"] == 1
    assert row["actual_spend_billed_cost_usd"] == pytest.approx(0.5)
    assert row["cost_accounting"]["generation"]["recorded_cost_usd"] == 0.0
    assert row["cost_accounting"]["actual_generation_spend"]["recorded_cost_usd"] == pytest.approx(
        0.5
    )


def _ensemble_member(module, model: str, *, thinking: str = "low"):
    return module.EnsembleMemberConfig(
        provider_config=ProviderConfig(
            provider="openrouter",
            model=model,
            provider_routing={model: "verified-upstream"},
        ),
        label=model.rsplit("/", 1)[-1],
        temperature=0.7,
        max_tokens=1024,
        thinking=thinking,
    )


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_run_wide_generation_policy_overrides_realized_ensemble_members(module) -> None:
    provider = module.EnsembleProvider(
        profile_name="router_dynamic/c3",
        proposers=[
            _ensemble_member(module, "anthropic/claude-opus-4.8"),
            _ensemble_member(module, "qwen/qwen3.7-max"),
            _ensemble_member(module, "x-ai/grok-4.5"),
        ],
        aggregator=_ensemble_member(module, "anthropic/claude-sonnet-5"),
    )
    policy = {
        **module.generation_thinking_policy(),
        "temperature": 0.0,
        "max_tokens": 16_384,
        "max_tokens_overridden": True,
        "thinking_budget_tokens": 50_000,
    }

    aligned = module.apply_generation_policy_to_ensemble_provider(provider, policy)

    assert [member.thinking for member in aligned.proposers] == [
        "max",
        "xhigh",
        "xhigh",
    ]
    assert aligned.aggregator.thinking == "xhigh"
    assert all(member.temperature == 0.0 for member in aligned.proposers)
    assert aligned.aggregator.temperature == 0.0
    assert all(member.max_tokens == 16_384 for member in aligned.proposers)
    assert aligned.aggregator.max_tokens == 16_384
    assert aligned.selection_plan["generation_policy_applied"] is True
    assert {
        row["model"]: row["thinking"] for row in aligned.selection_plan["member_generation"]
    } == {
        "anthropic/claude-opus-4.8": "max",
        "qwen/qwen3.7-max": "xhigh",
        "x-ai/grok-4.5": "xhigh",
        "anthropic/claude-sonnet-5": "xhigh",
    }


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_default_model_specific_budget_is_resolved_per_ensemble_member(module) -> None:
    provider = module.EnsembleProvider(
        profile_name="router_dynamic/c3",
        proposers=[_ensemble_member(module, "anthropic/claude-opus-4.8")],
        aggregator=_ensemble_member(module, "x-ai/grok-4.5"),
    )
    policy = module.generation_thinking_policy()
    assert policy["thinking_budget_tokens"] == "model-specific"

    aligned = module.apply_generation_policy_to_ensemble_provider(provider, policy)

    assert {
        row["model"]: row["thinking_budget_tokens"]
        for row in aligned.selection_plan["member_generation"]
    } == {
        "anthropic/claude-opus-4.8": 50_000,
        "x-ai/grok-4.5": 50_000,
    }


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_strict_ensemble_validation_rejects_unproved_reasoning_member(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "kwaipilot/kat-coder-pro-v2.5"
    provider = module.EnsembleProvider(
        profile_name="router_dynamic/c3",
        proposers=[_ensemble_member(module, model, thinking="xhigh")],
        aggregator=_ensemble_member(module, "x-ai/grok-4.5", thinking="xhigh"),
    )
    monkeypatch.setenv("OPENSQUILLA_PROVIDER_ROUTING_STRICT", "1")
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_REQUIRE_PARAMETERS", "1")

    with pytest.raises(ValueError, match="cannot prove support"):
        module.validate_strict_openrouter_ensemble_members(
            provider,
            module.generation_thinking_policy(),
        )


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_strict_ensemble_validation_requires_upstream_pin(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "x-ai/grok-4.5"
    member = _ensemble_member(module, model, thinking="xhigh")
    member.provider_config.provider_routing.clear()
    provider = module.EnsembleProvider(
        profile_name="router_dynamic/c3",
        proposers=[member],
        aggregator=_ensemble_member(module, "anthropic/claude-sonnet-5"),
    )
    monkeypatch.setenv("OPENSQUILLA_PROVIDER_ROUTING_STRICT", "1")
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_REQUIRE_PARAMETERS", "1")

    with pytest.raises(ValueError, match="no strict upstream provider pin"):
        module.validate_strict_openrouter_ensemble_members(
            provider,
            module.generation_thinking_policy(),
        )


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_strict_ensemble_validation_checks_fallback_and_enabled_boolean(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "deepseek/deepseek-v4-pro"
    provider = module.EnsembleProvider(
        profile_name="static_openrouter_b5",
        proposers=[_ensemble_member(module, model, thinking="xhigh")],
        aggregator=_ensemble_member(module, "z-ai/glm-5.2", thinking="xhigh"),
        fallback_model=model,
    )
    fallback_config = ProviderConfig(
        provider="openrouter",
        model=model,
        provider_routing={},
    )
    monkeypatch.setenv("OPENSQUILLA_PROVIDER_ROUTING_STRICT", "enabled")
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_REQUIRE_PARAMETERS", "enabled")

    with pytest.raises(ValueError, match="no strict upstream provider pin"):
        module.validate_strict_openrouter_ensemble_members(
            provider,
            module.generation_thinking_policy(),
            fallback_config=fallback_config,
        )


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_ensemble_fallback_model_receives_openrouter_reasoning_capabilities(module) -> None:
    policy = module.generation_thinking_policy()
    config = module.generation_chat_config(policy, model=None)

    resolved = module.with_openrouter_model_capabilities(
        config,
        "deepseek/deepseek-v4-pro",
    )

    assert resolved.model_capabilities is not None
    assert resolved.model_capabilities.supports_reasoning is True
    assert resolved.model_capabilities.reasoning_format == "openrouter"


def test_non_b2_groups_do_not_apply_g12_argument_alignment() -> None:
    args = runner.build_parser().parse_args(
        ["--input", "tasks.jsonl", "--groups", "B1", "--concurrency", "8"]
    )

    record = runner.apply_b2_g12_argument_alignment(args, ["B1"])

    assert record is None
    assert args.concurrency == 8
    assert not hasattr(args, "_benchmark_alignments")


def test_local_brave_runtime_allows_missing_key_only_for_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    configure_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "opensquilla.tools.builtin.web.configure_search",
        lambda **kwargs: configure_calls.append(kwargs),
    )
    config = GatewayConfig(search_api_key="")
    args = runner.build_parser().parse_args(
        [
            "--input",
            "tasks.jsonl",
            "--groups",
            "B2",
            "--tool-mode",
            "local_web_tools",
            "--local-web-search-provider",
            "brave",
            "--local-web-search-api-key-env",
            "BRAVE_SEARCH_API_KEY",
        ]
    )
    policy = runner.benchmark_tool_policy(args)

    runtime = runner.configure_local_web_search_runtime(config, policy, dry_run=True)

    assert runtime["credential_status"] == "missing_allowed_dry_run"
    assert runtime["runtime_configured"] is False
    assert runtime["api_key_configured"] is False
    assert configure_calls == []
    with pytest.raises(ValueError, match="requires an API key"):
        runner.configure_local_web_search_runtime(config, policy, dry_run=False)


def test_explicit_brave_environment_key_overrides_stale_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "fresh-env-key")
    configure_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "opensquilla.tools.builtin.web.configure_search",
        lambda **kwargs: configure_calls.append(kwargs),
    )
    config = GatewayConfig(search_api_key="stale-config-key")
    args = runner.build_parser().parse_args(
        [
            "--input",
            "tasks.jsonl",
            "--groups",
            "B1",
            "--tool-mode",
            "local_web_tools",
            "--local-web-search-provider",
            "brave",
            "--local-web-search-api-key-env",
            "BRAVE_SEARCH_API_KEY",
        ]
    )

    runtime = runner.configure_local_web_search_runtime(
        config,
        runner.benchmark_tool_policy(args),
    )

    assert runtime["api_key_source"] == "env:BRAVE_SEARCH_API_KEY"
    assert configure_calls[0]["api_key"] == "fresh-env-key"


def _local_web_tool_policy():
    args = runner.build_parser().parse_args(
        [
            "--input",
            "tasks.jsonl",
            "--groups",
            "B1",
            "--tool-mode",
            "local_web_tools",
        ]
    )
    return runner.benchmark_tool_policy(args)


def test_benchmark_tool_context_does_not_force_full_host_access() -> None:
    policy = _local_web_tool_policy()

    context = runner.build_benchmark_tool_context(
        task_id="task-1",
        group="B1",
        tool_policy=policy,
    )

    assert context.run_mode is None
    assert context.sandbox_run_context is None
    assert context.allowed_tools == {"web_search", "web_fetch"}


@pytest.mark.asyncio
async def test_local_web_preflight_does_not_enable_full_host_access(
    monkeypatch: pytest.MonkeyPatch,
    configured_tool_runtime,
) -> None:
    calls = {"search": 0, "fetch": 0}

    async def _fake_search(query: str, max_results: int, *, exclude_domains, provider: str):
        calls["search"] += 1
        assert "OpenAI official website" in query
        assert max_results == 1
        assert "github.com" in exclude_domains
        assert provider == "duckduckgo"
        return {
            "query": query,
            "results": [
                {
                    "title": "OpenAI",
                    "url": "https://openai.com/",
                    "snippet": "Official site",
                }
            ],
        }

    async def _fake_fetch(
        url: str,
        *,
        extract_mode: str,
        max_chars: int | None,
        extractor: str,
    ):
        from opensquilla.tools.run_mode import full_host_access_active

        calls["fetch"] += 1
        assert full_host_access_active() is False
        assert url == "https://example.com/"
        assert extract_mode == "text"
        assert max_chars == 1_000
        assert extractor == "auto"
        return {
            "url": url,
            "final_url": url,
            "status": 200,
            "text": "<external-content>Example Domain</external-content>",
        }

    monkeypatch.setattr(
        "opensquilla.tools.builtin.web.run_web_search_payload",
        _fake_search,
    )
    monkeypatch.setattr(
        "opensquilla.tools.builtin.web_fetch.run_web_fetch_payload",
        _fake_fetch,
    )

    result = await runner.run_local_web_tools_preflight(_local_web_tool_policy())

    assert result["status"] == "passed"
    assert "run_mode" not in result
    assert result["web_search_result_count"] == 1
    assert result["web_fetch_http_status"] == 200
    assert result["attempts_used"] == 1
    assert result["preflight_calls"] == {"web_search": 1, "web_fetch": 1}
    assert calls == {"search": 1, "fetch": 1}


@pytest.mark.asyncio
async def test_local_web_fetch_keeps_draco_contamination_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetch_called = False

    async def _fake_fetch(*args, **kwargs):
        nonlocal fetch_called
        fetch_called = True
        return {"status": 200, "text": "should not be fetched"}

    monkeypatch.setattr(
        "opensquilla.tools.builtin.web_fetch.run_web_fetch_payload",
        _fake_fetch,
    )
    policy = _local_web_tool_policy()
    registry = runner.build_local_web_tool_registry(policy)
    context = runner.build_benchmark_tool_context(
        task_id="task-1",
        group="B1",
        tool_policy=policy,
    )
    handler = runner.build_tool_handler(registry, context)

    result = await handler(
        ToolCall(
            tool_use_id="blocked-fetch",
            tool_name="web_fetch",
            arguments={"url": "https://github.com/openai/example"},
        )
    )

    payload = json.loads(result.content)
    assert fetch_called is False
    assert payload["error_class"] == "BlockedDomain"
    assert payload["blocked_domain"] == "github.com"


@pytest.mark.asyncio
async def test_local_web_preflight_fails_closed_on_denial_payload(
    monkeypatch: pytest.MonkeyPatch,
    configured_tool_runtime,
) -> None:
    async def _fake_search(query: str, max_results: int, *, exclude_domains, provider: str):
        assert provider == "duckduckgo"
        return {
            "query": query,
            "results": [{"title": "OpenAI", "url": "https://openai.com/"}],
        }

    async def _denied_fetch(*args, **kwargs):
        return {
            "status": "error",
            "reason": "policy_denied",
            "error": "network denied",
        }

    monkeypatch.setattr(
        "opensquilla.tools.builtin.web.run_web_search_payload",
        _fake_search,
    )
    monkeypatch.setattr(
        "opensquilla.tools.builtin.web_fetch.run_web_fetch_payload",
        _denied_fetch,
    )

    with pytest.raises(RuntimeError, match="web_fetch preflight failed"):
        await runner.run_local_web_tools_preflight(
            _local_web_tool_policy(),
            max_attempts=1,
        )


@pytest.mark.asyncio
async def test_benchmark_tools_respect_ambient_standard_context(
    monkeypatch: pytest.MonkeyPatch,
    configured_tool_runtime,
) -> None:
    observed: list[tuple[bool, str | None]] = []

    async def _fake_fetch(
        url: str,
        *,
        extract_mode: str,
        max_chars: int | None,
        extractor: str,
    ):
        from opensquilla.tools.run_mode import full_host_access_active

        active = current_tool_context.get()
        observed.append((full_host_access_active(), active.task_id if active else None))
        return {"url": url, "final_url": url, "status": 200, "text": "ok"}

    monkeypatch.setattr(
        "opensquilla.tools.builtin.web_fetch.run_web_fetch_payload",
        _fake_fetch,
    )
    policy = _local_web_tool_policy()
    benchmark_context = runner.build_benchmark_tool_context(
        task_id="benchmark-task",
        group="B1",
        tool_policy=policy,
    )
    ambient = runner.ToolContext(
        run_mode="standard",
        sandbox_run_context=benchmark_context.sandbox_run_context,
        task_id="outer-task",
    )
    handler = runner.build_tool_handler(
        runner.build_local_web_tool_registry(policy),
        benchmark_context,
    )

    token = current_tool_context.set(ambient)
    try:
        result = await handler(
            ToolCall(
                tool_use_id="ambient-override",
                tool_name="web_fetch",
                arguments={"url": "https://example.com/"},
            )
        )
        assert result.is_error is False
        assert current_tool_context.get() is ambient
    finally:
        current_tool_context.reset(token)
    assert observed == [(False, "outer-task")]


@pytest.mark.asyncio
async def test_local_web_search_filters_results_sources_and_internal_fetches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def _fake_search(query: str, max_results: int, *, exclude_domains, provider: str):
        captured.update(
            query=query,
            max_results=max_results,
            exclude_domains=list(exclude_domains),
            provider=provider,
        )
        return {
            "query": query,
            "results": [
                {"title": "blocked", "url": "https://github.com/example"},
                {"title": "allowed", "url": "https://example.com/allowed"},
            ],
            "sources": [
                {"url": "https://huggingface.co/datasets/example", "text": "blocked"},
                {"url": "https://example.com/source", "text": "allowed"},
            ],
        }

    monkeypatch.setattr(
        "opensquilla.tools.builtin.web.run_web_search_payload",
        _fake_search,
    )
    policy = _local_web_tool_policy()
    policy["local_web_tools"]["web_search"]["provider"] = "brave"
    context = runner.build_benchmark_tool_context(
        task_id="search-task",
        group="B1",
        tool_policy=policy,
    )
    handler = runner.build_tool_handler(
        runner.build_local_web_tool_registry(policy),
        context,
    )
    result = await handler(
        ToolCall(
            tool_use_id="search-contamination",
            tool_name="web_search",
            arguments={"query": "test"},
        )
    )

    payload = json.loads(result.content)
    assert captured["query"] == "test"
    assert captured["max_results"] == runner.local_web_search_max_results(policy)
    assert "github.com" in captured["exclude_domains"]
    assert captured["provider"] == "brave"
    assert [item["url"] for item in payload["results"]] == ["https://example.com/allowed"]
    assert [item["url"] for item in payload["sources"]] == ["https://example.com/source"]
    assert payload["blocked_result_count"] == 1
    assert payload["blocked_source_count"] == 1


@pytest.mark.asyncio
async def test_local_web_fetch_discards_blocked_redirect_content(
    monkeypatch: pytest.MonkeyPatch,
    configured_tool_runtime,
) -> None:
    async def _redirected_fetch(*args, **kwargs):
        return {
            "url": "https://example.com/start",
            "final_url": "https://github.com/private/answer",
            "status": 200,
            "text": "must not reach the model",
        }

    monkeypatch.setattr(
        "opensquilla.tools.builtin.web_fetch.run_web_fetch_payload",
        _redirected_fetch,
    )
    policy = _local_web_tool_policy()
    context = runner.build_benchmark_tool_context(
        task_id="redirect-task",
        group="B1",
        tool_policy=policy,
    )
    handler = runner.build_tool_handler(
        runner.build_local_web_tool_registry(policy),
        context,
    )
    result = await handler(
        ToolCall(
            tool_use_id="redirect-contamination",
            tool_name="web_fetch",
            arguments={"url": "https://example.com/start"},
        )
    )

    payload = json.loads(result.content)
    assert payload["error_class"] == "BlockedDomain"
    assert payload["blocked_domain"] == "github.com"
    assert "must not reach the model" not in result.content


@pytest.mark.asyncio
async def test_local_web_preflight_retries_and_records_all_setup_calls(
    monkeypatch: pytest.MonkeyPatch,
    configured_tool_runtime,
) -> None:
    fetch_attempt = 0

    async def _fake_search(query: str, max_results: int, *, exclude_domains, provider: str):
        assert provider == "duckduckgo"
        return {"query": query, "results": [{"url": "https://openai.com/"}]}

    async def _flaky_fetch(*args, **kwargs):
        nonlocal fetch_attempt
        fetch_attempt += 1
        if fetch_attempt == 1:
            return {"status": 503, "text": "temporarily unavailable"}
        return {
            "url": "https://example.com/",
            "final_url": "https://example.com/",
            "status": 200,
            "text": "Example Domain",
        }

    monkeypatch.setattr("opensquilla.tools.builtin.web.run_web_search_payload", _fake_search)
    monkeypatch.setattr(
        "opensquilla.tools.builtin.web_fetch.run_web_fetch_payload",
        _flaky_fetch,
    )

    result = await runner.run_local_web_tools_preflight(
        _local_web_tool_policy(),
        retry_backoff_seconds=0,
    )

    assert result["attempts_used"] == 2
    assert result["preflight_calls"] == {"web_search": 2, "web_fetch": 2}


@pytest.mark.asyncio
async def test_local_web_preflight_times_out_closed(
    monkeypatch: pytest.MonkeyPatch,
    configured_tool_runtime,
) -> None:
    async def _fake_search(query: str, max_results: int, *, exclude_domains, provider: str):
        assert provider == "duckduckgo"
        return {"query": query, "results": [{"url": "https://openai.com/"}]}

    async def _hanging_fetch(*args, **kwargs):
        await asyncio.sleep(1)
        return {"status": 200, "text": "too late"}

    monkeypatch.setattr("opensquilla.tools.builtin.web.run_web_search_payload", _fake_search)
    monkeypatch.setattr(
        "opensquilla.tools.builtin.web_fetch.run_web_fetch_payload",
        _hanging_fetch,
    )

    with pytest.raises(RuntimeError, match="timed out"):
        await runner.run_local_web_tools_preflight(
            _local_web_tool_policy(),
            max_attempts=1,
            call_timeout_seconds=0.01,
        )


def test_runner_tool_mode_combinations_fail_closed() -> None:
    with pytest.raises(ValueError, match="requires --runner-mode=agent_loop"):
        runner.validate_tool_mode_for_runner("provider", "local_web_tools")
    with pytest.raises(ValueError, match="requires --runner-mode=provider"):
        runner.validate_tool_mode_for_runner("agent_loop", "openrouter_server_tools")
    runner.validate_tool_mode_for_runner(
        "provider",
        "local_web_tools",
        smoke_only=True,
    )


def test_select_tasks_by_ids_preserves_reference_order_and_rejects_bad_ids() -> None:
    tasks = [
        {"id": "task-a", "prompt": "a"},
        {"id": "task-b", "prompt": "b"},
        {"id": "task-c", "prompt": "c"},
    ]

    selected = runner.select_tasks_by_ids(tasks, ["task-c", "task-a"])

    assert [task["id"] for task in selected] == ["task-a", "task-c"]
    with pytest.raises(ValueError, match="duplicate --task-ids"):
        runner.select_tasks_by_ids(tasks, ["task-a", "task-a"])
    with pytest.raises(ValueError, match="unknown --task-ids"):
        runner.select_tasks_by_ids(tasks, ["task-missing"])


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
def test_task_and_group_identity_inputs_fail_closed(
    module,
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "tasks.jsonl"
    input_path.write_text(
        "\n".join(
            [
                json.dumps({"id": "task-a", "prompt": "first"}),
                json.dumps({"task_id": " task-a ", "problem": "duplicate"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate task id"):
        module.load_tasks(input_path)
    with pytest.raises(ValueError, match="at least one experiment group"):
        module.parse_groups(",")
    with pytest.raises(ValueError, match=r"duplicate group\(s\): B2"):
        module.parse_groups("B2,b2")


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
def test_result_key_coverage_requires_exactly_one_row_per_key(module) -> None:
    expected = {("B2", "task-a"), ("G1", "task-a")}
    complete = module.result_key_coverage(
        [
            {"group": "b2", "task_id": " task-a "},
            {"group": "G1", "task_id": "task-a"},
        ],
        expected_keys=expected,
    )
    assert complete["pass"] is True

    invalid = module.result_key_coverage(
        [
            {"group": "B2", "task_id": "task-a"},
            {"group": "B2", "task_id": "task-a"},
            {"group": "B4", "task_id": "task-a"},
        ],
        expected_keys=expected,
    )
    assert invalid["pass"] is False
    assert invalid["missing_keys"] == [["G1", "task-a"]]
    assert invalid["unexpected_keys"] == [["B4", "task-a"]]
    assert invalid["duplicate_keys"] == [{"key": ["B2", "task-a"], "count": 2}]


def test_recovery_cli_arguments_are_manifested_and_reconstructed() -> None:
    args = runner.build_parser().parse_args(
        [
            "--input",
            "tasks.jsonl",
            "--groups",
            "B0,B1",
            "--task-ids",
            "task-a",
            "--task-ids",
            "task-c",
            "--agent-max-iterations",
            "0",
            "--deadline-wrapup-margin-seconds",
            "600",
            "--deadline-wrapup-disable-tools",
            "--deadline-thinking-off-margin-seconds",
            "600",
            "--max-iterations-includes-finalization",
            "--retrieval-loop-finalization-threshold",
            "3",
            "--finalization-aggregator-only",
            "--finalization-disable-thinking",
            "--continue-after-cost-audit-failure",
        ]
    )

    manifest = runner.manifest_args(args)
    reconstructed = runner.reconstructed_cli_args(args)
    assert manifest["task_ids"] == ["task-a", "task-c"]
    assert manifest["continue_after_cost_audit_failure"] is True
    assert manifest["agent_max_iterations"] == 0
    assert manifest["deadline_wrapup_margin_seconds"] == 600
    assert manifest["deadline_wrapup_disable_tools"] is True
    assert manifest["deadline_thinking_off_margin_seconds"] == 600
    assert manifest["max_iterations_includes_finalization"] is True
    assert manifest["retrieval_loop_finalization_threshold"] == 3
    assert manifest["finalization_aggregator_only"] is True
    assert manifest["finalization_disable_thinking"] is True
    assert reconstructed.count("--task-ids") == 2
    assert reconstructed[reconstructed.index("--agent-max-iterations") + 1] == "0"
    assert "--deadline-wrapup-disable-tools" in reconstructed
    assert "--max-iterations-includes-finalization" in reconstructed
    assert "--finalization-aggregator-only" in reconstructed
    assert "--finalization-disable-thinking" in reconstructed
    assert "--continue-after-cost-audit-failure" in reconstructed


@pytest.mark.asyncio
async def test_preflight_failure_writes_audit_manifest_before_any_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = tmp_path / "tasks.jsonl"
    input_path.write_text(
        json.dumps({"id": "task-1", "prompt": "test prompt"}) + "\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    args = runner.build_parser().parse_args(
        [
            "--input",
            str(input_path),
            "--output-dir",
            str(output_dir),
            "--groups",
            "B1",
            "--runner-mode",
            "agent_loop",
            "--tool-mode",
            "local_web_tools",
            "--local-web-search-provider",
            "duckduckgo",
        ]
    )

    monkeypatch.setattr(runner.GatewayConfig, "load", lambda _path: GatewayConfig())
    monkeypatch.setattr(
        runner,
        "configure_local_web_search_runtime",
        lambda *_args, **_kwargs: {"provider": "duckduckgo"},
    )

    async def _failed_preflight(*_args, **_kwargs):
        raise RuntimeError("synthetic preflight failure")

    monkeypatch.setattr(runner, "run_local_web_tools_preflight", _failed_preflight)

    with pytest.raises(RuntimeError, match="synthetic preflight failure"):
        await runner.amain(args)

    manifests = list(output_dir.glob("*.preflight-failed.manifest.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["status"] == "preflight_failed"
    assert manifest["failure"]["stage"] == "local_web_tools_preflight"
    assert manifest["failure"]["model_or_judge_started"] is False
    assert manifest["rows_written"] == 0
    assert not list(output_dir.glob("draco_ensemble_*.jsonl"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("continue_after_failure", "expected_rows"),
    [(False, 2), (True, 2)],
    ids=["default", "deprecated-continue-flag"],
)
async def test_strict_non_byok_dry_run_skips_receipt_audit_but_keeps_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    continue_after_failure: bool,
    expected_rows: int,
) -> None:
    input_path = tmp_path / "tasks.jsonl"
    input_path.write_text(
        "\n".join(
            json.dumps({"id": task_id, "prompt": f"prompt {task_id}"})
            for task_id in ("task-a", "task-b")
        )
        + "\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    argv = [
        "--input",
        str(input_path),
        "--output-dir",
        str(output_dir),
        "--groups",
        "B0",
        "--dry-run",
        "--require-openrouter-non-byok",
        "--concurrency",
        "1",
    ]
    if continue_after_failure:
        argv.append("--continue-after-cost-audit-failure")
    args = runner.build_parser().parse_args(argv)
    monkeypatch.setattr(runner.GatewayConfig, "load", lambda _path: GatewayConfig())

    status = await runner.amain(args)

    assert status == 0
    result_paths = list(output_dir.glob("draco_ensemble_*.jsonl"))
    manifest_paths = list(output_dir.glob("draco_run_*.manifest.json"))
    assert len(result_paths) == 1
    assert len(manifest_paths) == 1
    rows = [
        json.loads(line)
        for line in result_paths[0].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    manifest = json.loads(manifest_paths[0].read_text(encoding="utf-8"))
    assert len(rows) == expected_rows
    assert manifest["rows_written"] == expected_rows
    assert manifest["status"] == "complete"
    assert (
        manifest["run_compatibility"]["contracts"]["B0"]["cost_policy"][
            "require_openrouter_non_byok"
        ]
        is True
    )
    assert all(not row["error"] for row in rows)
    assert all("openrouter_non_byok_audit" not in row for row in rows)


def test_local_web_fetch_runtime_disables_hidden_firecrawl_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIRECRAWL_API_KEY", "secret-not-serialized")
    policy = _local_web_tool_policy()

    runtime = runner.configure_local_web_fetch_runtime(policy)

    assert runtime["firecrawl_allowed"] is False
    assert runtime["firecrawl_api_key_active"] is False
    assert runtime["firecrawl_disabled_for_reproducibility"] is True
    assert "FIRECRAWL_API_KEY" not in runner.os.environ


def test_cost_accounting_includes_judge_and_never_prices_unknown_as_zero() -> None:
    generation_usage = {
        "model_usage_breakdown": [
            {
                "provider": "openrouter",
                "model": "known",
                "input_tokens": 100,
                "output_tokens": 20,
                "billed_cost": 0.10,
                "cost_source": "provider_billed",
                "provider_usage": _openrouter_exact_evidence(0.10, "gen-known"),
            },
            {
                "model": "unknown",
                "input_tokens": 200,
                "output_tokens": 40,
                "billed_cost": 0.0,
                "cost_source": "none",
            },
        ]
    }
    judge_attempts = [
        {
            "attempt": 1,
            "run": {
                "llm_request_count": 1,
                "usage": {
                    "provider": "openrouter",
                    "model": "judge-model",
                    "input_tokens": 50,
                    "output_tokens": 5,
                    "billed_cost": 0.03,
                    "cost_source": "provider_billed",
                    "provider_usage": _openrouter_exact_evidence(0.03, "judge-1"),
                },
            },
        },
        {
            "attempt": 2,
            "run": {
                "llm_request_count": 1,
                "usage": {
                    "provider": "openrouter",
                    "model": "judge-model",
                    "input_tokens": 60,
                    "output_tokens": 6,
                    "billed_cost": 0.04,
                    "cost_source": "provider_billed",
                    "provider_usage": _openrouter_exact_evidence(0.04, "judge-2"),
                },
            },
        },
    ]
    row = {
        "tool_policy": {"tool_mode": "provider_only"},
        "llm_request_count": 2,
        # Compatibility metrics are scoped to the accepted generation attempt.
        "usage": generation_usage,
        "execution": {
            "generation_attempts": [
                {
                    "attempt": 1,
                    "run": {"llm_request_count": 2, "usage": generation_usage},
                }
            ]
        },
        "judge": {
            "criterion_judgments": [
                {
                    "judge_attempts": judge_attempts,
                    # This is a duplicate of the final attempt and must not be counted.
                    "judge_run": judge_attempts[-1]["run"],
                }
            ]
        },
        "candidate_judges": [],
    }

    accounting = runner.row_cost_accounting(row)

    assert runner.usage_unknown_count_from_usage_payload(generation_usage) == 1
    assert accounting["generation"]["request_count"] == 2
    assert accounting["generation"]["unknown_request_count"] == 1
    assert accounting["generation"]["unknown_tokens"] == 240
    assert accounting["generation"]["recorded_cost_usd"] == pytest.approx(0.10)
    assert accounting["actual_generation_spend"]["recorded_cost_usd"] == (pytest.approx(0.10))
    assert accounting["judge"]["request_count"] == 2
    assert accounting["judge"]["recorded_cost_usd"] == pytest.approx(0.07)
    assert accounting["llm_total"]["recorded_cost_usd"] == pytest.approx(0.17)
    assert accounting["result_cost_complete"] is False


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_unpriced_brave_cost_is_separate_from_llm_completion(module) -> None:
    usage = {
        "provider": "openrouter",
        "model": "model-a",
        "requested_provider": "openrouter",
        "requested_model": "model-a",
        "input_tokens": 5,
        "output_tokens": 1,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
        "provider_usage": _openrouter_exact_evidence(
            0.01,
            "brave-separate-cost",
        ),
    }
    prompt_hash = module.text_sha256("same prompt")
    row = {
        "group": "B1",
        "provider_spec": dict(module.GROUP_SPECS["B1"]),
        "routing_trace": {
            "applied_model": "model-a",
            "fallback_model": "model-a",
        },
        "task_id": "task-1",
        "prompt_sha256": prompt_hash,
        "task_input_sha256": "sha256:task-input",
        "run_compatibility_fingerprint": "sha256:run-contract",
        "error": None,
        "final_text": "accepted answer",
        "llm_request_count": 1,
        "total_tool_call_count": 1,
        "actual_spend_metrics": {
            "total_tool_call_count": 1,
            "llm_request_count": 1,
        },
        "tool_policy": {
            "tool_mode": "local_web_tools",
            "local_web_tools": {
                "web_search": {"provider": "brave"},
                "web_fetch": {"allow_firecrawl": False},
            },
        },
        "usage": usage,
        "quality_total": 80.0,
        "judge": _complete_legacy_judge("brave-separate-cost-judge"),
    }

    accounting = module.row_cost_accounting(row)

    assert accounting["actual_llm_cost_complete"] is True
    assert accounting["actual_llm_cost_exact"] is True
    assert accounting["actual_external_tools"]["cost_complete"] is False
    assert accounting["actual_external_tools"]["cost_precision"] == "unknown"
    assert accounting["actual_external_tools"]["estimated_cost_usd"] is None
    assert accounting["actual_external_tools"]["recorded_cost_usd_is_lower_bound"] is True
    assert accounting["actual_external_tools"]["separate_from_task_completion"] is True
    assert accounting["actual_spend_cost_complete"] is False
    assert accounting["actual_spend_recorded_total_cost_is_lower_bound"] is True

    if module is resume_runner:
        state = module.resume_row_completion_state(
            module.seal_result_row(row),
            expected_prompt_sha256=prompt_hash,
            expected_task_input_sha256="sha256:task-input",
            expected_run_compatibility_fingerprint="sha256:run-contract",
        )
        assert state["generation_valid"] is True
        assert state["cost_metadata_complete"] is True
        assert state["action"] == "complete"


def test_resume_runner_does_not_force_full_host_access() -> None:
    resume_runner = _load_resume_runner()
    args = resume_runner.build_parser().parse_args(
        [
            "--input",
            "tasks.jsonl",
            "--groups",
            "B1",
            "--tool-mode",
            "local_web_tools",
        ]
    )
    policy = resume_runner.benchmark_tool_policy(args)

    context = resume_runner.build_benchmark_tool_context(
        task_id="task-1",
        group="B1",
        tool_policy=policy,
    )

    assert context.run_mode is None
    assert context.sandbox_run_context is None


def test_agent_llm_error_without_usage_is_counted_as_unknown_cost() -> None:
    breakdown = runner.aggregate_agent_model_usage(
        [
            {
                "kind": "llm_error",
                "payload": {"iteration": 2, "attempt": 1, "error": "timeout"},
            }
        ]
    )

    assert len(breakdown) == 1
    assert breakdown[0]["role"] == "agent_llm_request_unknown"
    accounting = runner.usage_cost_accounting(
        {"model_usage_breakdown": breakdown},
        expected_requests=1,
        scope="generation",
    )
    assert accounting["unknown_request_count"] == 1
    assert accounting["cost_exact"] is False
    assert runner.usage_unknown_count_from_usage_payload({"model_usage_breakdown": breakdown}) == 1


def test_agent_llm_error_without_usage_honors_missing_request_count() -> None:
    breakdown = runner.aggregate_agent_model_usage(
        [
            {
                "kind": "llm_error",
                "payload": {"iteration": 2, "attempt": 1, "usage_missing_count": 2},
            }
        ]
    )

    assert len(breakdown) == 2
    assert all(row["cost_source"] == "none" for row in breakdown)


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
def test_agent_llm_usage_without_breakdown_preserves_native_receipt(module) -> None:
    receipt = ProviderBillingReceipt(
        currency="USD",
        status="confirmed",
        amount_nanos=10_000_000,
        usd_equivalent_nanos=10_000_000,
        fx_native_per_usd_nanos=1_000_000_000,
    )
    breakdown = module.aggregate_agent_model_usage(
        [
            {
                "kind": "llm_response",
                "payload": {
                    "iteration": 1,
                    "attempt": 1,
                    "usage_missing_count": 1,
                    "usage": {
                        "provider": "openrouter",
                        "model": "model-a",
                        "input_tokens": 3,
                        "output_tokens": 1,
                        "billed_cost": 0.01,
                        "cost_source": "provider_billed",
                        "billing_receipt": receipt,
                    },
                },
            }
        ]
    )

    assert len(breakdown) == 2
    assert breakdown[0]["billing_receipt"] is receipt
    assert breakdown[1]["role"] == "agent_llm_request_unknown"


@pytest.mark.parametrize(
    ("physical_request_count", "expected_unknown"),
    [(0, 0), (2, 2)],
)
def test_run_result_summary_honors_explicit_physical_request_count(
    physical_request_count: int,
    expected_unknown: int,
) -> None:
    result = runner.RunResult(
        final_text="",
        done=None,
        error="provider_error",
        trace_events=[
            {
                "kind": "error",
                "code": "provider_error",
                "request_started": physical_request_count > 0,
                "physical_request_count": physical_request_count,
            }
        ],
    )

    summary = runner.run_result_summary(result)

    assert summary["llm_request_count"] == physical_request_count
    assert summary["usage_unknown_count"] == expected_unknown


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
def test_no_done_paid_setup_unknown_usage_is_counted(module) -> None:
    physical_attempt_id = "a" * 32
    unknown_analyzer = {
        "role": "unknown_request",
        "label": "task_analyzer",
        "provider": "",
        "model": "",
        "requested_provider": "openrouter",
        "requested_model": "anthropic/claude-opus-4.8",
        "input_tokens": 0,
        "output_tokens": 0,
        "billed_cost": 0.0,
        "cost_source": "none",
        "attempt": 1,
        "physical_attempt_id": physical_attempt_id,
        "provider_usage": {
            "usage_unknown": True,
            "physical_attempt_id": physical_attempt_id,
        },
    }
    result = module.RunResult(
        final_text="",
        done=None,
        error="analyzer_cleanup_failed",
        setup_usage=[unknown_analyzer],
        trace_events=[
            {
                "kind": "error",
                "code": "provider_build_failed_after_setup",
                "request_started": False,
                "physical_request_count": 0,
            }
        ],
    )

    summary = module.run_result_summary(result)

    assert summary["llm_request_count"] == 1
    assert summary["usage_unknown_count"] == 1
    assert summary["usage"]["model_usage_breakdown"] == [unknown_analyzer]


def test_error_diagnostic_reconciles_skewed_trace_and_disjoint_receipt() -> None:
    proposer_row = {
        "role": "proposer",
        "provider": "openrouter",
        "model": "p1",
        "input_tokens": 5,
        "output_tokens": 1,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
    }
    aggregator_row = {
        "role": "aggregator",
        "provider": "openrouter",
        "model": "agg",
        "input_tokens": 6,
        "output_tokens": 1,
        "billed_cost": 0.02,
        "cost_source": "provider_billed",
    }
    event = ErrorEvent(
        message="failed after two requests",
        model_usage_breakdown=[proposer_row],
        diagnostic_done=DoneEvent(
            input_tokens=6,
            output_tokens=1,
            billed_cost=0.02,
            cost_source="provider_billed",
            provider="openrouter",
            model="agg",
            model_usage_breakdown=[aggregator_row],
        ),
        ensemble_trace={
            "llm_request_count": 1,
            "physical_request_count": 1,
        },
        request_started=True,
        physical_request_count=2,
    )

    done = runner.diagnostic_done_from_error_event(event)

    assert done is not None
    assert done.model_usage_breakdown == [proposer_row, aggregator_row]
    assert done.ensemble_trace["llm_request_count"] == 2
    assert done.ensemble_trace["physical_request_count"] == 2
    assert done.usage_missing_count == 0
    assert done.billed_cost == pytest.approx(0.03)


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_error_diagnostic_preserves_identical_receipt_multiplicity(module) -> None:
    receipt = {
        "role": "proposer",
        "provider": "openrouter",
        "model": "same-model",
        "input_tokens": 5,
        "output_tokens": 1,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
    }
    event = ErrorEvent(
        message="two identical physical receipts",
        model_usage_breakdown=[receipt],
        diagnostic_done=DoneEvent(
            input_tokens=10,
            output_tokens=2,
            billed_cost=0.02,
            cost_source="provider_billed",
            provider="openrouter",
            model="same-model",
            model_usage_breakdown=[receipt, receipt],
        ),
        request_started=True,
        physical_request_count=2,
    )

    done = module.diagnostic_done_from_error_event(event)

    assert done is not None
    assert len(done.model_usage_breakdown) == 2
    assert done.billed_cost == pytest.approx(0.02)
    assert done.ensemble_trace["physical_request_count"] == 2
    assert done.usage_missing_count == 0


def test_error_diagnostic_infers_receipt_plus_missing_without_trace() -> None:
    event = ErrorEvent(
        message="one known and one unknown",
        model_usage_breakdown=[
            {
                "role": "proposer",
                "provider": "openrouter",
                "model": "p1",
                "input_tokens": 5,
                "output_tokens": 1,
                "billed_cost": 0.01,
                "cost_source": "provider_billed",
            }
        ],
        usage_missing_count=1,
    )

    done = runner.diagnostic_done_from_error_event(event)

    assert done is not None
    assert done.ensemble_trace["llm_request_count"] == 2
    assert done.usage_missing_count == 1


def test_error_diagnostic_explicit_zero_wins_over_stale_missing_marker() -> None:
    event = ErrorEvent(
        message="local preflight",
        request_started=False,
        physical_request_count=0,
        usage_missing_count=1,
    )

    assert runner.diagnostic_done_from_error_event(event) is None


def test_cost_accounting_does_not_reinflate_explicit_zero_request_attempts() -> None:
    no_request_run = {
        "llm_request_count": 0,
        "usage_unknown_count": 0,
        "usage": {},
        "trace_events": [
            {
                "kind": "error",
                "code": "provider_request_budget_exhausted",
                "request_started": False,
                "physical_request_count": 0,
            }
        ],
    }
    row = {
        "execution": {
            "generation_attempts": [
                {"attempt": 1, "run": no_request_run},
            ]
        }
    }
    judge = {
        "judge_attempt_count": 1,
        "judge_attempts": [
            {"attempt": 1, "run": no_request_run},
        ],
    }

    generation = runner.actual_generation_spend_accounting(row)
    judge_accounting = runner.judge_cost_accounting(judge, scope="judge")

    assert generation["request_count"] == 0
    assert generation["unknown_request_count"] == 0
    assert judge_accounting["request_count"] == 0
    assert judge_accounting["unknown_request_count"] == 0


@pytest.mark.asyncio
async def test_collect_run_preserves_partial_receipts_from_terminal_error() -> None:
    receipt = {
        "role": "proposer",
        "provider": "openrouter",
        "model": "model-a",
        "requested_provider": "openrouter",
        "requested_model": "model-a",
        "input_tokens": 10,
        "output_tokens": 2,
        "billed_cost": 0.05,
        "cost_source": "provider_billed",
        "provider_usage": _openrouter_exact_evidence(0.05, "partial-error"),
    }

    class PartialFailureProvider:
        def chat(self, *_args, **_kwargs):
            async def events():
                yield ErrorEvent(
                    message="ensemble failed",
                    code="ensemble_all_failed",
                    model_usage_breakdown=[receipt],
                    usage_missing_count=1,
                    ensemble_trace={
                        "llm_request_count": 2,
                        "physical_request_count": 2,
                        "usage_missing_count": 1,
                    },
                )

            return events()

    result = await runner.collect_run(
        PartialFailureProvider(),
        "prompt",
        timeout=1.0,
    )
    summary = runner.run_result_summary(result)
    accounting = runner.usage_cost_accounting(
        summary["usage"],
        expected_requests=summary["llm_request_count"],
        scope="failed_generation",
    )

    assert result.error == "ensemble failed"
    assert result.done is not None
    assert result.done.stop_reason == "error"
    assert result.done.model_usage_breakdown == [receipt]
    assert summary["llm_request_count"] == 2
    assert summary["usage_unknown_count"] == 1
    assert accounting["request_count"] == 2
    assert accounting["exact_request_count"] == 1
    assert accounting["unknown_request_count"] == 1
    assert accounting["recorded_cost_usd"] == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_collect_run_preserves_zero_request_ensemble_preflight() -> None:
    class PreflightFailureProvider:
        def chat(self, *_args, **_kwargs):
            async def events():
                yield ErrorEvent(
                    message="no request started",
                    code="ensemble_call_in_progress",
                    request_started=False,
                    physical_request_count=0,
                    ensemble_trace={
                        "llm_request_count": 0,
                        "physical_request_count": 0,
                        "usage_missing_count": 0,
                    },
                )

            return events()

    result = await runner.collect_run(
        PreflightFailureProvider(),
        "prompt",
        timeout=1.0,
    )
    summary = runner.run_result_summary(result)

    assert result.done is None
    assert result.error == "no request started"
    assert summary["llm_request_count"] == 0
    assert summary["usage_unknown_count"] == 0


@pytest.mark.asyncio
async def test_collect_run_keeps_outer_receipts_when_error_has_diagnostic_done() -> None:
    proposer_row = {
        "role": "proposer",
        "provider": "openrouter",
        "model": "proposer-model",
        "input_tokens": 8,
        "output_tokens": 2,
        "billed_cost": 0.02,
        "cost_source": "provider_billed",
        "billing_receipt": ProviderBillingReceipt(
            currency="USD",
            status="confirmed",
            amount_nanos=20_000_000,
            usd_equivalent_nanos=20_000_000,
            fx_native_per_usd_nanos=1_000_000_000,
        ),
    }
    aggregator_row = {
        "role": "aggregator",
        "provider": "openrouter",
        "model": "aggregator-model",
        "input_tokens": 12,
        "output_tokens": 1,
        "billed_cost": 0.03,
        "cost_source": "provider_billed",
        "billing_receipt": ProviderBillingReceipt(
            currency="USD",
            status="confirmed",
            amount_nanos=30_000_000,
            usd_equivalent_nanos=30_000_000,
            fx_native_per_usd_nanos=1_000_000_000,
        ),
    }
    diagnostic = DoneEvent(
        input_tokens=12,
        output_tokens=1,
        billed_cost=0.03,
        cost_source="provider_billed",
        provider="openrouter",
        model="aggregator-model",
        model_usage_breakdown=[aggregator_row],
    )

    class PartialDiagnosticFailureProvider:
        def chat(self, *_args, **_kwargs):
            async def events():
                yield ErrorEvent(
                    message="aggregator metadata invalid",
                    code="aggregator_metadata_invalid",
                    model_usage_breakdown=[proposer_row, aggregator_row],
                    diagnostic_done=diagnostic,
                    ensemble_trace={
                        "llm_request_count": 2,
                        "physical_request_count": 2,
                        "usage_missing_count": 0,
                    },
                )

            return events()

    result = await runner.collect_run(
        PartialDiagnosticFailureProvider(),
        "prompt",
        timeout=1.0,
    )
    summary = runner.run_result_summary(result)

    assert result.done is not diagnostic
    assert result.done is not None
    assert result.done.model_usage_breakdown == [proposer_row, aggregator_row]
    assert result.done.billed_cost == pytest.approx(0.05)
    assert summary["llm_request_count"] == 2
    assert summary["usage_unknown_count"] == 0


@pytest.mark.parametrize(
    "placeholder_role",
    sorted(runner.MISSING_USAGE_PLACEHOLDER_ROLES),
)
def test_agent_usage_does_not_double_count_missing_usage_placeholder(
    placeholder_role: str,
) -> None:
    breakdown = runner.aggregate_agent_model_usage(
        [
            {
                "kind": "llm_response",
                "payload": {
                    "iteration": 1,
                    "attempt": 1,
                    "usage_missing_count": 1,
                    "usage": {
                        "model_usage_breakdown": [
                            {
                                "role": placeholder_role,
                                "provider": "openrouter",
                                "model": "model-a",
                                "input_tokens": 0,
                                "output_tokens": 0,
                                "billed_cost": 0.0,
                                "cost_source": "none",
                            }
                        ]
                    },
                },
            }
        ]
    )

    assert len(breakdown) == 1
    assert breakdown[0]["role"] == placeholder_role
    assert runner.usage_unknown_count_from_usage_payload({"model_usage_breakdown": breakdown}) == 1


def test_agent_usage_does_not_treat_generic_unpriced_row_as_missing_placeholder() -> None:
    breakdown = runner.aggregate_agent_model_usage(
        [
            {
                "kind": "llm_error",
                "payload": {
                    "iteration": 1,
                    "attempt": 1,
                    "usage_missing_count": 1,
                    "usage": {
                        "model_usage_breakdown": [
                            {
                                "role": "proposer",
                                "provider": "openrouter",
                                "model": "model-a",
                                "input_tokens": 3,
                                "output_tokens": 1,
                                "billed_cost": 0.0,
                                "cost_source": "none",
                            }
                        ]
                    },
                },
            }
        ]
    )

    assert len(breakdown) == 2
    assert [row["role"] for row in breakdown] == [
        "proposer",
        "agent_llm_request_unknown",
    ]


def test_agent_retry_partial_error_preserves_missing_physical_request() -> None:
    first_attempt_rows = [
        {
            "provider": "openrouter",
            "model": f"proposer-{index}",
            "input_tokens": 10,
            "output_tokens": 2,
            "billed_cost": 0.01,
            "cost_source": "provider_billed",
            "provider_usage": _openrouter_exact_evidence(0.01, f"failed-attempt-proposer-{index}"),
        }
        for index in range(4)
    ]
    retry_rows = [
        {
            "provider": "openrouter",
            "model": f"retry-model-{index}",
            "input_tokens": 10,
            "output_tokens": 2,
            "billed_cost": 0.02,
            "cost_source": "provider_billed",
            "provider_usage": _openrouter_exact_evidence(0.02, f"successful-retry-{index}"),
        }
        for index in range(5)
    ]

    breakdown = runner.aggregate_agent_model_usage(
        [
            {
                "kind": "llm_error",
                "payload": {
                    "iteration": 1,
                    "attempt": 1,
                    "usage_missing_count": 1,
                    "usage": {"model_usage_breakdown": first_attempt_rows},
                },
            },
            {
                "kind": "llm_response",
                "payload": {
                    "iteration": 1,
                    "attempt": 2,
                    "usage": {"model_usage_breakdown": retry_rows},
                },
            },
        ]
    )

    assert len(breakdown) == 10
    assert sum(row["cost_source"] == "provider_billed" for row in breakdown) == 9
    unknown = [row for row in breakdown if row["cost_source"] == "none"]
    assert len(unknown) == 1
    assert unknown[0]["role"] == "agent_llm_request_unknown"
    accounting = runner.usage_cost_accounting(
        {"model_usage_breakdown": breakdown},
        expected_requests=9,
        scope="generation",
    )
    assert accounting["request_count"] == 10
    assert accounting["unknown_request_count"] == 1
    assert accounting["cost_exact"] is False


def test_estimated_and_mixed_usage_record_their_actual_cost_fields() -> None:
    accounting = runner.usage_cost_accounting(
        {
            "model_usage_breakdown": [
                {
                    "provider": "openrouter",
                    "model": "estimated-model",
                    "input_tokens": 3,
                    "output_tokens": 1,
                    "billed_cost": 0.0,
                    "estimated_cost_usd": 0.40,
                    "cost_usd": 0.40,
                    "cost_source": "opensquilla_estimate",
                },
                {
                    "provider": "openrouter",
                    "model": "mixed-model",
                    "input_tokens": 4,
                    "output_tokens": 2,
                    "billed_cost": 0.10,
                    "billed_cost_usd": 0.10,
                    "estimated_cost_usd": 0.20,
                    "cost_usd": 0.30,
                    "cost_source": "mixed",
                },
            ]
        },
        expected_requests=2,
        scope="generation",
    )

    assert accounting["estimated_request_count"] == 1
    assert accounting["mixed_request_count"] == 1
    assert accounting["recorded_cost_usd"] == pytest.approx(0.70)


def test_agent_model_usage_preserves_physical_provider_evidence() -> None:
    provider_usage = _openrouter_exact_evidence(0.01, "generation-1")
    breakdown = runner.aggregate_agent_model_usage(
        [
            {
                "kind": "llm_response",
                "payload": {
                    "iteration": 1,
                    "call_attempt": 1,
                    "usage": {
                        "provider": "openrouter",
                        "model": "model-a",
                        "input_tokens": 3,
                        "output_tokens": 1,
                        "billed_cost": 0.01,
                        "cost_source": "provider_billed",
                        "provider_usage": provider_usage,
                    },
                },
            }
        ]
    )

    assert breakdown[0]["provider"] == "openrouter"
    assert breakdown[0]["provider_usage"] == provider_usage
    assert (
        runner.usage_cost_accounting(
            {"model_usage_breakdown": breakdown},
            expected_requests=1,
            scope="generation",
        )["cost_exact"]
        is True
    )


def test_failed_agent_attempt_synthesizes_diagnostic_usage_from_recorder() -> None:
    recorder = runner.BenchmarkTurnCallRecorder()
    recorder.write(
        "llm_error",
        {
            "iteration": 1,
            "call_attempt": 1,
            "usage": {
                "provider": "openrouter",
                "model": "model-a",
                "input_tokens": 12,
                "output_tokens": 3,
                "billed_cost": 0.25,
                "cost_source": "provider_billed",
                "provider_usage": _openrouter_exact_evidence(0.25, "failed-1"),
            },
            "error": {"code": "timeout", "message": "timed out"},
        },
    )

    done = runner.provider_done_from_agent_done(
        None,
        recorder=recorder,
        fallback_model="model-a",
    )

    assert done is not None
    assert done.stop_reason == "error"
    assert done.input_tokens == 12
    assert done.output_tokens == 3
    assert done.billed_cost == pytest.approx(0.25)
    assert done.provider_usage["diagnostic_usage_only"] is True
    assert done.model_usage_breakdown[0]["provider_usage"]["response_ids"] == ["failed-1"]
    assert (
        runner.usage_cost_accounting(
            runner.done_payload(done),
            expected_requests=1,
            scope="generation",
        )["cost_exact"]
        is True
    )


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_all_untraced_agent_calls_preserve_unknown_physical_requests(module) -> None:
    recorder = module.BenchmarkTurnCallRecorder()
    recorder.write(
        "llm_abandoned",
        {
            "call_id": "call-1",
            "iteration": 1,
            "attempt": 1,
            "usage_missing_count": 1,
        },
    )
    recorder.write(
        "llm_error",
        {
            "call_id": "call-2",
            "iteration": 2,
            "attempt": 1,
            "usage_missing_count": 2,
        },
    )

    trace = module.aggregate_agent_ensemble_trace(recorder.records)
    done = module.provider_done_from_agent_done(
        None,
        recorder=recorder,
        fallback_model="fallback-model",
    )

    assert trace["agent_llm_call_count"] == 2
    assert trace["untraced_agent_llm_call_count"] == 2
    assert trace["llm_request_count"] == 3
    assert [call["request_outcome"] for call in trace["calls"]] == [
        "llm_abandoned",
        "llm_error",
    ]
    assert all(call["trace_missing"] is True for call in trace["calls"])
    assert done is not None
    assert done.stop_reason == "error"
    assert done.model == ""
    assert done.requested_model == "fallback-model"
    assert len(done.model_usage_breakdown) == 3
    assert module.usage_unknown_count_from_usage_payload(module.done_payload(done)) == 3


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
@pytest.mark.parametrize(
    "placeholder_role",
    sorted(runner.MISSING_USAGE_PLACEHOLDER_ROLES),
)
def test_missing_usage_placeholders_never_supply_actual_identity(
    module,
    placeholder_role: str,
) -> None:
    placeholder = {
        "role": placeholder_role,
        "provider": "openrouter",
        "model": "requested-model",
        "requested_provider": "openrouter",
        "requested_model": "requested-model",
        "input_tokens": 0,
        "output_tokens": 0,
        "billed_cost": 0.0,
        "cost_source": "none",
    }
    result = module.RunResult(
        final_text="answer",
        done=DoneEvent(
            requested_provider="openrouter",
            requested_model="requested-model",
            stop_reason="stop",
            model_usage_breakdown=[placeholder],
        ),
    )

    assert (
        module.single_generation_identity_reason(
            result,
            expected_provider="openrouter",
            expected_model="requested-model",
        )
        == "actual_model_evidence_missing"
    )

    recorder = module.BenchmarkTurnCallRecorder()
    recorder.write(
        "llm_error",
        {
            "usage_missing_count": 1,
            "usage": {"model_usage_breakdown": [placeholder]},
        },
    )
    diagnostic = module.provider_done_from_agent_done(
        None,
        recorder=recorder,
        fallback_model="requested-model",
    )

    assert diagnostic is not None
    assert diagnostic.model == ""
    assert diagnostic.provider == ""
    assert diagnostic.requested_model == "requested-model"
    assert module.usage_unknown_count_from_usage_payload(module.done_payload(diagnostic)) == 1


@pytest.mark.parametrize(
    "placeholder_role",
    sorted(runner.MISSING_USAGE_PLACEHOLDER_ROLES),
)
def test_resume_actual_identity_evidence_ignores_missing_usage_placeholders(
    placeholder_role: str,
) -> None:
    evidence = resume_runner.usage_actual_identity_evidence(
        {
            "model_usage_breakdown": [
                {
                    "role": placeholder_role,
                    "provider": "openrouter",
                    "model": "requested-model",
                }
            ]
        }
    )

    assert evidence["receipt_count"] == 0
    assert evidence["provider"] == ""
    assert evidence["model"] == ""


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_diagnostic_explicit_zero_discards_stale_placeholder(module) -> None:
    placeholder = {
        "role": "abandoned_stream_request",
        "provider": "openrouter",
        "model": "requested-model",
        "cost_source": "none",
    }
    event = ErrorEvent(
        message="request was rejected before send",
        code="preflight",
        model_usage_breakdown=[placeholder],
        request_started=False,
        physical_request_count=0,
    )

    assert module.diagnostic_done_from_error_event(event) is None


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
@pytest.mark.parametrize("diagnostic_id_first", [False, True])
def test_diagnostic_receipt_matching_prioritizes_response_ids(
    module,
    diagnostic_id_first: bool,
) -> None:
    def receipt(response_id: str | None) -> dict[str, object]:
        return {
            "provider": "openrouter",
            "model": "same-model",
            "input_tokens": 5,
            "output_tokens": 1,
            "billed_cost": 0.01,
            "cost_source": "provider_billed",
            "provider_usage": ({"response_ids": [response_id]} if response_id is not None else {}),
        }

    diagnostic_rows = [receipt("response-a"), receipt(None)]
    if not diagnostic_id_first:
        diagnostic_rows.reverse()
    event = ErrorEvent(
        message="diagnostic copies arrive in an adversarial order",
        code="response_invalid",
        model_usage_breakdown=[receipt("response-a"), receipt("response-b")],
        diagnostic_done=DoneEvent(
            input_tokens=10,
            output_tokens=2,
            billed_cost=0.02,
            cost_source="provider_billed",
            model_usage_breakdown=diagnostic_rows,
        ),
        request_started=True,
        physical_request_count=2,
    )

    done = module.diagnostic_done_from_error_event(event)

    assert done is not None
    assert len(done.model_usage_breakdown) == 2
    assert done.ensemble_trace["physical_request_count"] == 2
    assert done.usage_missing_count == 0


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_diagnostic_same_id_uses_richer_authoritative_receipt(module) -> None:
    outer = {
        "provider": "",
        "model": "",
        "requested_provider": "openrouter",
        "requested_model": "requested-model",
        "input_tokens": 0,
        "output_tokens": 0,
        "billed_cost": 0.0,
        "cost_source": "none",
        "provider_usage": {"response_ids": ["response-a"]},
    }
    richer = {
        **outer,
        "provider": "openrouter",
        "model": "actual-model",
        "input_tokens": 7,
        "output_tokens": 3,
        "billed_cost": 0.25,
        "cost_source": "provider_billed",
    }
    event = ErrorEvent(
        message="outer wrapper retained partial usage",
        code="response_invalid",
        model_usage_breakdown=[outer],
        diagnostic_done=DoneEvent(
            input_tokens=7,
            output_tokens=3,
            billed_cost=0.25,
            cost_source="provider_billed",
            model_usage_breakdown=[richer],
        ),
        request_started=True,
        physical_request_count=1,
    )

    done = module.diagnostic_done_from_error_event(event)

    assert done is not None
    assert len(done.model_usage_breakdown) == 1
    assert done.model == "actual-model"
    assert done.provider == "openrouter"
    assert done.input_tokens == 7
    assert done.output_tokens == 3
    assert done.billed_cost == pytest.approx(0.25)


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_nested_diagnostic_missing_evidence_is_preserved(module) -> None:
    outer = {
        "provider": "openrouter",
        "model": "model-a",
        "input_tokens": 4,
        "output_tokens": 1,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
    }
    nested = {
        **outer,
        "model": "model-b",
    }
    event = ErrorEvent(
        message="nested composite retained one unknown request",
        code="response_invalid",
        model_usage_breakdown=[outer],
        diagnostic_done=DoneEvent(
            input_tokens=4,
            output_tokens=1,
            billed_cost=0.01,
            cost_source="provider_billed",
            model_usage_breakdown=[nested],
            usage_missing_count=1,
            ensemble_trace={
                "llm_request_count": 2,
                "physical_request_count": 2,
                "usage_missing_count": 1,
            },
        ),
        request_started=True,
    )

    done = module.diagnostic_done_from_error_event(event)

    assert done is not None
    assert len(done.model_usage_breakdown) == 2
    assert done.usage_missing_count == 1
    assert done.ensemble_trace["physical_request_count"] == 3


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_native_receipt_precedence_is_shared_by_cost_paths(module) -> None:
    confirmed = ProviderBillingReceipt(
        currency="USD",
        status="confirmed",
        amount_nanos=200_000_000,
        usd_equivalent_nanos=200_000_000,
        fx_native_per_usd_nanos=1_000_000_000,
    )
    pending = ProviderBillingReceipt(
        currency="USD",
        status="pending",
        amount_nanos=None,
        usd_equivalent_nanos=None,
        fx_native_per_usd_nanos=1_000_000_000,
    )
    base = {
        "provider": "openrouter",
        "model": "model-a",
        "input_tokens": 5,
        "output_tokens": 1,
        "billed_cost": 0.1,
        "cost_source": "provider_billed",
        "provider_usage": _openrouter_exact_evidence(0.1, "response-a"),
    }
    exact = {**base, "billing_receipt": confirmed}
    unresolved = {**base, "billing_receipt": pending}

    exact_accounting = module.usage_cost_accounting(
        {"model_usage_breakdown": [exact]},
        expected_requests=1,
        scope="generation",
    )
    pending_accounting = module.usage_cost_accounting(
        {"model_usage_breakdown": [unresolved]},
        expected_requests=1,
        scope="generation",
    )

    assert module.exact_provider_usage_cost(exact) == pytest.approx(0.2)
    assert exact_accounting["recorded_cost_usd"] == pytest.approx(0.2)
    assert exact_accounting["exact_request_count"] == 1
    assert module.exact_provider_usage_cost(unresolved) is None
    assert module.trusted_provider_billed_cost(unresolved) == 0.0
    assert pending_accounting["unknown_request_count"] == 1


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_native_receipt_requires_complete_schema_and_fails_closed(module) -> None:
    valid = {
        "currency": "USD",
        "status": "confirmed",
        "amount_nanos": 10_000_000,
        "usd_equivalent_nanos": 10_000_000,
        "fx_native_per_usd_nanos": 1_000_000_000,
        "schema_version": 1,
    }
    invalid_receipts = [
        {**valid, "currency": "US"},
        {**valid, "currency": "usd"},
        {**valid, "currency": "ＵＳＤ"},
        {**valid, "amount_nanos": None},
        {**valid, "usd_equivalent_nanos": 9_999_999},
        {**valid, "fx_native_per_usd_nanos": 0},
        {**valid, "amount_nanos": 1 << 63},
        {**valid, "usd_equivalent_nanos": 1 << 63},
        {**valid, "fx_native_per_usd_nanos": 1 << 63},
        {**valid, "schema_version": True},
        {**valid, "schema_version": 2},
        {**valid, "status": "pending"},
    ]
    base = {
        "provider": "openrouter",
        "model": "model-a",
        "input_tokens": 5,
        "output_tokens": 1,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
        "provider_usage": _openrouter_exact_evidence(0.01, "response-a"),
    }

    assert module.exact_provider_usage_cost({**base, "billing_receipt": valid}) == (
        pytest.approx(0.01)
    )
    for receipt in invalid_receipts:
        unit = {**base, "billing_receipt": receipt}
        assert module.exact_provider_usage_cost(unit) is None
        assert module.trusted_provider_billed_cost(unit) == 0.0
        audit = module.openrouter_non_byok_audit({"llm_request_count": 1, "usage": unit})
        assert audit["pass"] is False
        assert audit["unverified_or_byok_request_count"] == 1


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_non_byok_audit_is_stricter_than_ordinary_cost_accounting(module) -> None:
    native_receipt = {
        "currency": "USD",
        "status": "confirmed",
        "amount_nanos": 10_000_000,
        "usd_equivalent_nanos": 10_000_000,
        "fx_native_per_usd_nanos": 1_000_000_000,
        "schema_version": 1,
    }
    generic_confirmed = {
        "provider": "openrouter",
        "model": "model-a",
        "input_tokens": 5,
        "output_tokens": 1,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
        "billing_receipt": native_receipt,
        "provider_usage": {},
    }
    legacy_openrouter_usage = {
        "provider": "openrouter",
        "model": "model-a",
        "input_tokens": 5,
        "output_tokens": 1,
        "billed_cost": 0.01,
        "cost_source": "openrouter_usage",
        "provider_usage": {},
    }
    missing_serving_provider_metadata = {
        "provider": "openrouter",
        "model": "model-a",
        "input_tokens": 5,
        "output_tokens": 1,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
        "provider_usage": {
            "is_byok": False,
            "provider_reported_cost": 0.01,
            "response_ids": ["response-a"],
            "router_metadata": {"is_byok": False},
        },
    }

    for unit in (
        generic_confirmed,
        legacy_openrouter_usage,
        missing_serving_provider_metadata,
    ):
        assert module.exact_provider_usage_cost(unit) == pytest.approx(0.01)
        audit = module.openrouter_non_byok_audit({"llm_request_count": 1, "usage": unit})
        assert audit["pass"] is False
        assert audit["unverified_or_byok_request_count"] == 1

    fully_attested = {
        **generic_confirmed,
        "provider_usage": _openrouter_exact_evidence(0.01, "response-a"),
    }
    assert (
        module.openrouter_non_byok_audit({"llm_request_count": 1, "usage": fully_attested})["pass"]
        is True
    )


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_duplicate_response_receipt_counts_as_one_physical_request(module) -> None:
    receipt = {
        "provider": "openrouter",
        "model": "model-a",
        "input_tokens": 5,
        "output_tokens": 1,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
        "provider_usage": _openrouter_exact_evidence(0.01, "response-duplicate"),
    }
    usage = {"model_usage_breakdown": [receipt, json.loads(json.dumps(receipt))]}

    accounting = module.usage_cost_accounting(
        usage,
        expected_requests=2,
        scope="generation",
    )
    audit = module.openrouter_non_byok_audit(
        {
            "llm_request_count": 2,
            "usage": usage,
        }
    )

    assert accounting["request_count"] == 2
    assert accounting["usage_observed_request_count"] == 1
    assert accounting["duplicate_stable_receipt_count"] == 1
    assert accounting["recorded_cost_usd"] == pytest.approx(0.01)
    assert accounting["exact_request_count"] == 1
    assert accounting["unknown_request_count"] == 1
    assert accounting["cost_exact"] is False
    assert audit["pass"] is False
    assert audit["exact_request_count"] == 1
    assert audit["unverified_or_byok_request_count"] == 1

    # Matching token/cost values alone do not prove two rows are one call.
    without_stable_id = [
        {"input_tokens": 5, "output_tokens": 1, "billed_cost": 0.01},
        {"input_tokens": 5, "output_tokens": 1, "billed_cost": 0.01},
    ]
    assert len(module.deduplicate_stable_usage_receipts(without_stable_id)) == 2


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reverse"])
def test_duplicate_response_receipt_preserves_contradictory_byok_evidence(
    module,
    reverse: bool,
) -> None:
    exact = {
        "provider": "openrouter",
        "model": "model-a",
        "input_tokens": 5,
        "output_tokens": 1,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
        "provider_usage": _openrouter_exact_evidence(
            0.01,
            "response-conflicting-byok",
        ),
    }
    explicit_byok = {
        **exact,
        "provider_usage": {
            **_openrouter_exact_evidence(
                0.01,
                "response-conflicting-byok",
            ),
            "is_byok": True,
            "router_metadata": {
                **_openrouter_exact_evidence(
                    0.01,
                    "unused",
                )["router_metadata"],
                "is_byok": True,
            },
        },
    }
    units = [exact, explicit_byok]
    if reverse:
        units.reverse()

    deduplicated = module.deduplicate_stable_usage_receipts(units)
    audit = module.openrouter_non_byok_audit(
        {
            "llm_request_count": 1,
            "usage": {"model_usage_breakdown": units},
        }
    )

    assert len(deduplicated) == 1
    evidence = deduplicated[0]["provider_usage"][module.STABLE_RECEIPT_EVIDENCE_KEY]
    assert evidence["usage_is_byok_values"] == [False, True]
    assert evidence["router_is_byok_values"] == [False, True]
    assert evidence["receipt_conflict"] is True
    assert audit["pass"] is False
    assert audit["status"] == "policy_violation"
    assert audit["conflict_request_count"] == 1


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reverse"])
@pytest.mark.parametrize(
    "conflicting_field",
    ["provider", "model", "cost", "input_tokens"],
)
def test_duplicate_response_receipt_preserves_other_conflicts_order_independently(
    module,
    reverse: bool,
    conflicting_field: str,
) -> None:
    first = {
        "provider": "openrouter",
        "model": "model-a",
        "input_tokens": 5,
        "output_tokens": 1,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
        "provider_usage": _openrouter_exact_evidence(
            0.01,
            "response-conflicting-receipt",
        ),
    }
    second = json.loads(json.dumps(first))
    if conflicting_field == "provider":
        second["provider"] = "another-provider"
    elif conflicting_field == "model":
        second["model"] = "model-b"
    elif conflicting_field == "cost":
        second["billed_cost"] = 0.02
        second["provider_usage"]["provider_reported_cost"] = 0.02
    else:
        second["input_tokens"] = 6
    units = [first, second]
    if reverse:
        units.reverse()

    deduplicated = module.deduplicate_stable_usage_receipts(units)
    audit = module.openrouter_non_byok_audit(
        {
            "llm_request_count": 1,
            "usage": {"model_usage_breakdown": units},
        }
    )

    assert len(deduplicated) == 1
    evidence = deduplicated[0]["provider_usage"][module.STABLE_RECEIPT_EVIDENCE_KEY]
    assert evidence["receipt_conflict"] is True
    assert audit["pass"] is False
    assert audit["status"] == "policy_violation"
    assert audit["conflict_request_count"] == 1


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_duplicate_response_receipt_is_deduplicated_across_generation_attempts(
    module,
) -> None:
    receipt = {
        "provider": "openrouter",
        "model": "model-a",
        "input_tokens": 5,
        "output_tokens": 1,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
        "provider_usage": _openrouter_exact_evidence(
            0.01,
            "response-cross-attempt",
        ),
    }
    run = {"llm_request_count": 1, "usage": receipt}
    row = {
        "generation_attempt_count": 2,
        "execution": {
            "generation_attempts": [
                {"run": run},
                {"run": json.loads(json.dumps(run))},
            ]
        },
    }

    accounting = module.actual_generation_spend_accounting(row)

    assert accounting["request_count"] == 2
    assert accounting["usage_observed_request_count"] == 1
    assert accounting["duplicate_stable_receipt_count"] == 1
    assert accounting["recorded_cost_usd"] == pytest.approx(0.01)
    assert accounting["exact_request_count"] == 1
    assert accounting["unknown_request_count"] == 1
    assert accounting["cost_exact"] is False


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_duplicate_response_receipt_is_deduplicated_across_llm_scopes(module) -> None:
    receipt = {
        "provider": "openrouter",
        "model": "model-a",
        "input_tokens": 5,
        "output_tokens": 1,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
        "provider_usage": _openrouter_exact_evidence(
            0.01,
            "response-cross-scope",
        ),
    }
    judge = {
        "judge_model": "judge-model",
        "judge_attempt_count": 1,
        "judge_attempts": [
            {
                "run": {
                    "llm_request_count": 1,
                    "usage": json.loads(json.dumps(receipt)),
                }
            }
        ],
    }
    row = {
        "final_text": "answer",
        "llm_request_count": 1,
        "usage": receipt,
        "judge": judge,
        "candidate_judges": [json.loads(json.dumps(judge))],
    }

    accounting = module.row_cost_accounting(row)
    for total_name in ("llm_total", "actual_llm_total"):
        total = accounting[total_name]
        assert total["request_count"] == 3
        assert total["usage_observed_request_count"] == 1
        assert total["duplicate_stable_receipt_count"] == 2
        assert total["recorded_cost_usd"] == pytest.approx(0.01)
        assert total["exact_request_count"] == 1
        assert total["unknown_request_count"] == 2
        assert total["cost_exact"] is False

    audit = module.openrouter_non_byok_audit(row)
    assert audit["pass"] is False
    assert audit["request_count"] == 3
    assert audit["exact_request_count"] == 1
    assert audit["unverified_or_byok_request_count"] == 2

    public_accounting = module.public_cost_accounting(accounting)
    serialized = json.dumps(public_accounting)
    assert "_stable_usage_receipts" not in serialized
    assert "_receipt_provenance_complete" not in serialized


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_unrepresented_missing_request_increases_all_request_counts(module) -> None:
    receipt = {
        "provider": "openrouter",
        "model": "model-a",
        "input_tokens": 5,
        "output_tokens": 1,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
    }
    done = DoneEvent(
        input_tokens=5,
        output_tokens=1,
        billed_cost=0.01,
        cost_source="provider_billed",
        model_usage_breakdown=[receipt],
        usage_missing_count=1,
    )
    result = module.RunResult(final_text="answer", done=done)
    row = {"usage": module.done_payload(done)}

    assert (
        module.llm_request_count_for_run(
            spec={"kind": "single"},
            done=done,
            provider_attempted=True,
        )
        == 2
    )
    assert module.run_result_summary(result)["llm_request_count"] == 2
    assert module.row_llm_request_count(row) == 2


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_no_breakdown_receipt_plus_missing_increases_all_request_counts(module) -> None:
    done = DoneEvent(
        provider="openrouter",
        model="model-a",
        input_tokens=5,
        output_tokens=1,
        billed_cost=0.01,
        cost_source="provider_billed",
        usage_missing_count=2,
        ensemble_trace={
            "llm_request_count": 1,
            "physical_request_count": 1,
        },
    )
    result = module.RunResult(final_text="answer", done=done)
    row = {"llm_request_count": 1, "usage": module.done_payload(done)}

    assert (
        module.llm_request_count_for_run(
            spec={"kind": "single"},
            done=done,
            provider_attempted=True,
        )
        == 3
    )
    assert module.run_result_summary(result)["llm_request_count"] == 3
    assert module.row_llm_request_count(row) == 3


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_agent_done_without_recorder_breakdown_preserves_receipt_and_missing(
    module,
) -> None:
    receipt = ProviderBillingReceipt(
        currency="USD",
        status="confirmed",
        amount_nanos=10_000_000,
        usd_equivalent_nanos=10_000_000,
        fx_native_per_usd_nanos=1_000_000_000,
    )
    done = AgentDoneEvent(
        text="answer",
        input_tokens=5,
        output_tokens=1,
        cost_usd=0.01,
        billed_cost=0.01,
        cost_source="provider_billed",
        provider="openrouter",
        model="model-a",
        requested_provider="openrouter",
        requested_model="model-a",
    )
    setattr(done, "billing_receipt", receipt)
    setattr(done, "provider_usage", _openrouter_exact_evidence(0.01, "response-a"))
    setattr(done, "usage_missing_count", 2)

    provider_done = module.provider_done_from_agent_done(
        done,
        recorder=module.BenchmarkTurnCallRecorder(),
        fallback_model="model-a",
    )

    assert provider_done is not None
    assert provider_done.billing_receipt is receipt
    assert provider_done.usage_missing_count == 2
    assert len(provider_done.model_usage_breakdown) == 1
    assert provider_done.ensemble_trace["physical_request_count"] == 3
    assert module.done_payload(provider_done)["billed_cost"] == pytest.approx(0.01)


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_agent_done_rollup_does_not_duplicate_recorded_physical_requests(
    module,
) -> None:
    recorder = module.BenchmarkTurnCallRecorder()
    for index, (input_tokens, output_tokens, billed_cost) in enumerate(
        [(5, 1, 0.01), (7, 2, 0.02)],
        start=1,
    ):
        recorder.write(
            "llm_response",
            {
                "iteration": index,
                "call_attempt": 1,
                "usage": {
                    "provider": "openrouter",
                    "model": "model-a",
                    "requested_provider": "openrouter",
                    "requested_model": "model-a",
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "billed_cost": billed_cost,
                    "cost_source": "provider_billed",
                    "provider_usage": _openrouter_exact_evidence(
                        billed_cost,
                        f"response-{index}",
                    ),
                },
            },
        )

    # AgentDoneEvent is the aggregate envelope for both recorded calls. It is
    # not a third physical request and must not be appended to the breakdown.
    done = AgentDoneEvent(
        text="answer",
        input_tokens=12,
        output_tokens=3,
        cost_usd=0.03,
        billed_cost=0.03,
        cost_source="opensquilla_static_estimate",
        provider="openrouter",
        model="model-a",
        requested_provider="openrouter",
        requested_model="model-a",
    )
    provider_done = module.provider_done_from_agent_done(
        done,
        recorder=recorder,
        fallback_model="model-a",
    )

    assert provider_done is not None
    assert len(provider_done.model_usage_breakdown) == 2
    assert {item["role"] for item in provider_done.model_usage_breakdown} == {"agent_llm_call"}
    usage = module.done_payload(provider_done)
    row = {"llm_request_count": 2, "usage": usage}
    accounting = module.usage_cost_accounting(
        usage,
        expected_requests=2,
        scope="generation",
    )

    assert accounting["request_count"] == 2
    assert accounting["usage_observed_request_count"] == 2
    assert accounting["exact_request_count"] == 2
    assert accounting["estimated_request_count"] == 0
    assert accounting["recorded_cost_usd"] == pytest.approx(0.03)
    assert accounting["cost_exact"] is True
    assert module.row_llm_request_count(row) == 2
    assert module.usage_unknown_count_from_usage_payload(usage) == 0
    non_byok_audit = module.openrouter_non_byok_audit(row)
    assert non_byok_audit["pass"] is True
    assert non_byok_audit["status"] == "exact"
    assert non_byok_audit["policy_safe_to_continue"] is True
    assert non_byok_audit["request_count"] == 2
    assert non_byok_audit["exact_request_count"] == 2
    assert non_byok_audit["unverified_request_count"] == 0
    assert non_byok_audit["explicit_byok_request_count"] == 0
    assert non_byok_audit["conflict_request_count"] == 0
    assert non_byok_audit["unverified_or_byok_request_count"] == 0


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_agent_done_breakdown_rollups_do_not_duplicate_ensemble_receipts(
    module,
) -> None:
    recorder = module.BenchmarkTurnCallRecorder()
    physical_rows = []
    for index, (input_tokens, output_tokens, billed_cost) in enumerate(
        [(5, 1, 0.01), (7, 2, 0.02)],
        start=1,
    ):
        physical = {
            "role": "proposer",
            "label": "proposer_1",
            "provider": "openrouter",
            "model": "model-a",
            "requested_provider": "openrouter",
            "requested_model": "model-a",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "billed_cost": billed_cost,
            "cost_source": "provider_billed",
            "provider_usage": _openrouter_exact_evidence(
                billed_cost,
                f"ensemble-response-{index}",
            ),
        }
        physical_rows.append(physical)
        recorder.write(
            "llm_response",
            {
                "iteration": index,
                "call_attempt": 1,
                "usage": {"model_usage_breakdown": [physical]},
            },
        )

    done = AgentDoneEvent(
        text="answer",
        input_tokens=12,
        output_tokens=3,
        cost_usd=0.03,
        billed_cost=0.03,
        cost_source="opensquilla_static_estimate",
        provider="openrouter",
        model="model-a",
        requested_provider="openrouter",
        requested_model="model-a",
    )
    setattr(
        done,
        "model_usage_breakdown",
        [
            {
                "role": "proposer",
                "label": "proposer_1",
                "request_count": 2,
                "provider": "openrouter",
                "model": "model-a",
                "requested_provider": "openrouter",
                "requested_model": "model-a",
                "input_tokens": 12,
                "output_tokens": 3,
                "billed_cost": 0.03,
                "cost_source": "opensquilla_static_estimate",
            }
        ],
    )

    provider_done = module.provider_done_from_agent_done(
        done,
        recorder=recorder,
        fallback_model="model-a",
    )

    assert provider_done is not None
    assert len(provider_done.model_usage_breakdown) == 2
    assert all(
        item.get("agent_call_index") in {1, 2} for item in provider_done.model_usage_breakdown
    )
    assert provider_done.provider_usage["agent_done_summary_rows_ignored"] == 1
    usage = module.done_payload(provider_done)
    accounting = module.usage_cost_accounting(
        usage,
        expected_requests=2,
        scope="generation",
    )
    assert accounting["recorded_cost_usd"] == pytest.approx(0.03)
    assert accounting["total_tokens"] == 15
    assert accounting["cost_exact"] is True
    assert (
        module.openrouter_non_byok_audit({"llm_request_count": 2, "usage": usage})["pass"] is True
    )


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
@pytest.mark.parametrize(
    ("evidence_kind", "expected_classification", "expected_field"),
    [
        ("explicit_byok", "explicit_byok", None),
        ("is_byok_conflict", "conflict", "is_byok"),
        ("provider_conflict", "conflict", "provider"),
        ("model_conflict", "conflict", "model"),
        ("receipt_conflict", "conflict", "cost_usd_nanos"),
    ],
)
def test_ignored_agent_done_rollup_preserves_independent_policy_evidence(
    module,
    evidence_kind: str,
    expected_classification: str,
    expected_field: str | None,
) -> None:
    recorder = module.BenchmarkTurnCallRecorder()
    for index, cost in enumerate((0.01, 0.02), start=1):
        recorder.write(
            "llm_response",
            {
                "iteration": index,
                "call_attempt": 1,
                "usage": {
                    "provider": "openrouter",
                    "model": "model-a",
                    "input_tokens": index + 2,
                    "output_tokens": 1,
                    "billed_cost": cost,
                    "cost_source": "provider_billed",
                    "provider_usage": _openrouter_exact_evidence(
                        cost,
                        f"physical-{index}",
                    ),
                },
            },
        )
    summary_provider_usage: dict[str, object] = {}
    summary_provider = "openrouter"
    summary_model = "model-a"
    if evidence_kind == "explicit_byok":
        summary_provider_usage = {
            "is_byok": True,
            "router_metadata": {"is_byok": True},
        }
    elif evidence_kind == "is_byok_conflict":
        summary_provider_usage = {
            "is_byok": False,
            "router_metadata": {"is_byok": True},
        }
    elif evidence_kind == "provider_conflict":
        summary_provider = "unexpected-provider"
    elif evidence_kind == "model_conflict":
        summary_model = "unexpected-model"
    else:
        summary_provider_usage = {
            module.STABLE_RECEIPT_EVIDENCE_KEY: {
                "conflict_fields": ["cost_usd_nanos"],
                "receipt_conflict": True,
            }
        }
    done = AgentDoneEvent(
        text="answer",
        input_tokens=7,
        output_tokens=2,
        cost_usd=0.03,
        billed_cost=0.03,
        cost_source="opensquilla_static_estimate",
        provider="openrouter",
        model="model-a",
    )
    setattr(
        done,
        "model_usage_breakdown",
        [
            {
                "request_count": 2,
                "provider": summary_provider,
                "model": summary_model,
                "input_tokens": 7,
                "output_tokens": 2,
                "billed_cost": 0.03,
                "cost_source": "opensquilla_static_estimate",
                "provider_usage": summary_provider_usage,
            }
        ],
    )

    provider_done = module.provider_done_from_agent_done(
        done,
        recorder=recorder,
        fallback_model="model-a",
    )

    assert provider_done is not None
    assert len(provider_done.model_usage_breakdown) == 2
    usage = module.done_payload(provider_done)
    accounting = module.usage_cost_accounting(
        usage,
        expected_requests=2,
        scope="generation",
    )
    assert accounting["request_count"] == 2
    assert accounting["recorded_cost_usd"] == pytest.approx(0.03)
    assert accounting["total_tokens"] == 9
    evidence = provider_done.provider_usage[module.IGNORED_AGENT_DONE_POLICY_EVIDENCE_KEY]
    assert len(evidence) == 1
    assert evidence[0]["classification"] == expected_classification
    if expected_field is not None:
        assert expected_field in evidence[0]["conflict_fields"]
    audit = module.openrouter_non_byok_audit({"llm_request_count": 2, "usage": usage})
    assert audit["status"] == "policy_violation"
    assert audit["policy_safe_to_continue"] is False
    assert audit["request_count"] == 2
    assert audit["exact_request_count"] == 2
    assert audit["explicit_byok_request_count"] == 0
    assert audit["conflict_request_count"] == 0
    assert audit["independent_policy_evidence_count"] == 1


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_ignored_agent_done_rollup_accepts_frozen_serving_model_alias(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        module,
        "_formal_openrouter_model_aliases",
        lambda: {
            "vendor/model-base": frozenset(
                {
                    "vendor/model-base",
                    "vendor/model-base-20260725",
                }
            )
        },
    )

    evidence = module.ignored_agent_done_summary_policy_evidence(
        {
            "request_count": 2,
            "provider": "openrouter",
            "model": "vendor/model-base",
        },
        physical_rows=[
            {
                "provider": "openrouter",
                "model": "vendor/model-base-20260725",
            }
        ],
    )

    assert evidence is None


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_agent_done_unmatched_atomic_receipt_can_recover_missing_ledger_row(
    module,
) -> None:
    recorder = module.BenchmarkTurnCallRecorder()
    recorder.write(
        "llm_response",
        {
            "iteration": 1,
            "call_attempt": 1,
            "usage": {
                "provider": "openrouter",
                "model": "model-a",
                "input_tokens": 5,
                "output_tokens": 1,
                "billed_cost": 0.01,
                "cost_source": "provider_billed",
                "provider_usage": _openrouter_exact_evidence(
                    0.01,
                    "recorded-response",
                ),
            },
        },
    )
    recovered = {
        "role": "agent_done",
        "request_count": 1,
        "provider": "openrouter",
        "model": "model-a",
        "requested_provider": "openrouter",
        "requested_model": "model-a",
        "input_tokens": 7,
        "output_tokens": 2,
        "billed_cost": 0.02,
        "cost_source": "provider_billed",
        "provider_usage": _openrouter_exact_evidence(
            0.02,
            "recovered-response",
        ),
    }
    done = AgentDoneEvent(
        text="answer",
        input_tokens=12,
        output_tokens=3,
        cost_usd=0.03,
        billed_cost=0.03,
        cost_source="provider_billed",
        provider="openrouter",
        model="model-a",
    )
    setattr(done, "model_usage_breakdown", [recovered])

    provider_done = module.provider_done_from_agent_done(
        done,
        recorder=recorder,
        fallback_model="model-a",
    )

    assert provider_done is not None
    assert len(provider_done.model_usage_breakdown) == 2
    assert {
        response_id
        for row in provider_done.model_usage_breakdown
        for response_id in module.usage_row_response_ids(row)
    } == {"recorded-response", "recovered-response"}
    accounting = module.usage_cost_accounting(
        module.done_payload(provider_done),
        expected_requests=2,
        scope="generation",
    )
    assert accounting["recorded_cost_usd"] == pytest.approx(0.03)
    assert accounting["cost_exact"] is True


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_agent_done_envelope_does_not_create_request_after_explicit_zero(
    module,
) -> None:
    recorder = module.BenchmarkTurnCallRecorder()
    recorder.write(
        "llm_error",
        {
            "iteration": 1,
            "call_attempt": 1,
            "request_started": False,
            "physical_request_count": 0,
            "usage_missing_count": 0,
        },
    )
    done = AgentDoneEvent(
        text="answer",
        input_tokens=5,
        output_tokens=1,
        cost_usd=0.01,
        billed_cost=0.01,
        cost_source="opensquilla_static_estimate",
        provider="openrouter",
        model="model-a",
    )

    provider_done = module.provider_done_from_agent_done(
        done,
        recorder=recorder,
        fallback_model="model-a",
    )

    assert provider_done is not None
    assert provider_done.model_usage_breakdown == []
    assert provider_done.ensemble_trace["physical_request_count"] == 0
    assert provider_done.ensemble_trace["llm_request_count"] == 0


def test_main_and_resume_share_identical_critical_runtime_functions() -> None:
    resume_runner = _load_resume_runner()
    critical = (
        "validate_tool_mode_for_runner",
        "configure_benchmark_sandbox_runtime",
        "configure_local_web_fetch_runtime",
        "filter_blocked_search_results",
        "build_local_web_tool_registry",
        "build_benchmark_tool_context",
        "run_local_web_tools_preflight",
        "build_task_analyzer_provider",
        "result_key_coverage",
        "llm_request_count_for_run",
        "run_result_summary",
        "row_llm_request_count",
        "_finite_nonnegative_number",
        "_openrouter_router_provider_metadata_is_complete",
        "_normalize_openrouter_provider_identity",
        "_formal_openrouter_model_aliases",
        "_formal_openrouter_models_equivalent",
        "_openrouter_router_provider_metadata_pin_state",
        "_openrouter_provider_billed_cost_is_exact",
        "_openrouter_non_byok_receipt_is_exact",
        "_first_usage_cost",
        "_coerce_provider_billing_receipt",
        "_billing_receipt_state",
        "exact_provider_usage_cost",
        "trusted_provider_billed_cost",
        "_mixed_usage_cost",
        "build_stable_receipt_evidence",
        "ignored_agent_done_summary_policy_evidence",
        "merge_usage_receipt_provenance",
        "deduplicate_stable_usage_receipts",
        "usage_cost_accounting",
        "merge_cost_accounting",
        "public_cost_accounting",
        "summarized_run_expected_request_count",
        "diagnostic_done_from_error_event",
        "aggregate_agent_model_usage",
        "aggregate_agent_ensemble_trace",
        "ensemble_call_trace_sequence",
        "admissible_empty_nonterminal_fallback_reasons",
        "agent_call_output_sequence_reasons",
        "ensemble_generation_retry_reason",
        "provider_done_from_agent_done",
        "backfill_result_requested_identity",
        "collect_generation_with_retries",
        "external_tool_cost_accounting",
        "row_cost_accounting",
        "_usage_units_for_openrouter_non_byok_audit",
        "_actual_llm_usage_units_for_openrouter_non_byok_audit",
        "_independent_openrouter_policy_evidence",
        "classify_openrouter_non_byok_unit",
        "normalized_agent_finalization_policy",
        "legal_proposer_quorum",
        "validate_g1_registry_contract",
        "g1_registry_contract_reasons",
        "apply_b2_g12_argument_alignment",
        "enforce_draco_legal_proposer_quorum",
        "enforce_formal_draco_runtime_config",
        "canonical_json_sha256",
        "gateway_execution_contract",
        "validate_strict_openrouter_non_byok_environment",
        "resolved_llm_runtime_contract",
        "build_run_compatibility",
        "openrouter_non_byok_audit",
        "judge_text",
        "run_one",
        "trace_row",
        "render_markdown",
    )

    for name in critical:
        assert inspect.getsource(getattr(runner, name)) == inspect.getsource(
            getattr(resume_runner, name)
        )
    assert (
        runner._ADMISSIBLE_NONTERMINAL_FALLBACK_CORE_REASONS
        == resume_runner._ADMISSIBLE_NONTERMINAL_FALLBACK_CORE_REASONS
    )


def test_all_serialized_cost_accounting_assignments_strip_private_provenance() -> None:
    for path in (SCRIPT_PATH, RESUME_SCRIPT_PATH):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assignments = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == "row"
                and isinstance(target.slice, ast.Constant)
                and target.slice.value == "cost_accounting"
                for target in node.targets
            )
        ]
        assert assignments
        for assignment in assignments:
            assert isinstance(assignment.value, ast.Call)
            assert isinstance(assignment.value.func, ast.Name)
            assert assignment.value.func.id == "public_cost_accounting"


def test_resume_only_skips_strict_valid_matching_prompt(tmp_path: Path) -> None:
    resume_runner = _load_resume_runner()
    prompt_hash = resume_runner.text_sha256("same prompt")
    valid = resume_runner.seal_result_row(
        {
            "group": "B1",
            "provider_spec": dict(resume_runner.GROUP_SPECS["B1"]),
            "routing_trace": {
                "applied_model": "model-a",
                "fallback_model": "model-a",
            },
            "task_id": "task-1",
            "prompt_sha256": prompt_hash,
            "task_input_sha256": "sha256:task-input",
            "run_compatibility_fingerprint": "sha256:run-contract",
            "error": None,
            "final_text": "answer",
            "quality_total": 80.0,
            "judge": _complete_legacy_judge("resume-valid-judge"),
            "ensemble_trace": {},
            "llm_request_count": 1,
            "usage": {
                "provider": "openrouter",
                "model": "model-a",
                "requested_provider": "openrouter",
                "requested_model": "model-a",
                "input_tokens": 3,
                "output_tokens": 1,
                "billed_cost": 0.01,
                "cost_source": "provider_billed",
                "provider_usage": _openrouter_exact_evidence(0.01, "resume-valid"),
            },
        }
    )
    failed = resume_runner.seal_result_row(
        {**valid, "task_id": "task-2", "error": "provider failure"}
    )
    wrong_prompt = resume_runner.seal_result_row(
        {**valid, "task_id": "task-3", "prompt_sha256": "wrong"}
    )
    path = tmp_path / "prior.jsonl"
    path.write_text(
        "\n".join(json.dumps(row) for row in (valid, failed, wrong_prompt)) + "\n",
        encoding="utf-8",
    )
    selected = {("B1", "task-1"), ("B1", "task-2"), ("B1", "task-3")}

    completed, audit = resume_runner.load_strict_completed_group_task_keys(
        resume_paths=[path],
        selected_keys=selected,
        prompt_hashes={task_id: prompt_hash for _, task_id in selected},
        task_input_hashes={task_id: "sha256:task-input" for _, task_id in selected},
        run_compatibility_fingerprints={"B1": "sha256:run-contract"},
    )

    assert completed == {("B1", "task-1")}
    assert audit["matching_attempt_count"] == 3
    assert audit["strict_valid_pair_count"] == 1
    assert audit["strict_invalid_attempt_count"] == 2


def test_resume_rejects_legacy_or_incompatible_contract_rows(tmp_path: Path) -> None:
    resume_runner = _load_resume_runner()
    prompt_hash = resume_runner.text_sha256("same prompt")
    base = {
        "group": "B1",
        "provider_spec": dict(resume_runner.GROUP_SPECS["B1"]),
        "routing_trace": {
            "applied_model": "model-a",
            "fallback_model": "model-a",
        },
        "prompt_sha256": prompt_hash,
        "task_input_sha256": "sha256:task-input",
        "run_compatibility_fingerprint": "sha256:old-contract",
        "error": None,
        "final_text": "answer",
        "quality_total": 80.0,
        "judge": _complete_legacy_judge("resume-contract-judge"),
        "ensemble_trace": {},
        "usage": {
            "provider": "openrouter",
            "model": "model-a",
            "requested_provider": "openrouter",
            "requested_model": "model-a",
            "input_tokens": 3,
            "output_tokens": 1,
            "cost_source": "none",
        },
    }
    legacy = {
        key: value
        for key, value in {**base, "task_id": "task-1"}.items()
        if key not in {"task_input_sha256", "run_compatibility_fingerprint"}
    }
    mismatched = resume_runner.seal_result_row({**base, "task_id": "task-2"})
    path = tmp_path / "prior.jsonl"
    path.write_text(
        "\n".join(json.dumps(row) for row in (legacy, mismatched)) + "\n",
        encoding="utf-8",
    )

    completed, audit = resume_runner.load_strict_completed_group_task_keys(
        resume_paths=[path],
        selected_keys={("B1", "task-1"), ("B1", "task-2")},
        prompt_hashes={"task-1": prompt_hash, "task-2": prompt_hash},
        task_input_hashes={"task-1": "sha256:task-input", "task-2": "sha256:task-input"},
        run_compatibility_fingerprints={"B1": "sha256:new-contract"},
    )

    assert completed == set()
    assert audit["strict_invalid_reason_counts"] == {
        "cost_metadata_incomplete": 2,
        "invalid_result_evidence": 1,
        "missing_run_compatibility_fingerprint": 1,
        "missing_task_input_sha256": 1,
        "run_compatibility_fingerprint_mismatch": 1,
    }


def test_resume_requires_recomputed_non_byok_cost_evidence() -> None:
    resume_runner = _load_resume_runner()
    prompt_hash = resume_runner.text_sha256("same prompt")
    base = {
        "group": "B1",
        "provider_spec": dict(resume_runner.GROUP_SPECS["B1"]),
        "routing_trace": {
            "applied_model": "model-a",
            "fallback_model": "model-a",
        },
        "task_id": "task-1",
        "prompt_sha256": prompt_hash,
        "task_input_sha256": "sha256:task-input",
        "run_compatibility_fingerprint": "sha256:run-contract",
        "error": None,
        "final_text": "answer",
        "quality_total": 80.0,
        "judge": _complete_legacy_judge("resume-non-byok-judge"),
        "ensemble_trace": {},
    }
    invalid = resume_runner.seal_result_row(base)
    invalid_reasons = resume_runner.strict_resume_row_invalid_reasons(
        invalid,
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
        require_openrouter_non_byok=True,
    )
    assert "openrouter_non_byok_metadata_incomplete" in invalid_reasons

    exact = {
        **base,
        "llm_request_count": 1,
        "usage": {
            "provider": "openrouter",
            "model": "model-a",
            "requested_provider": "openrouter",
            "requested_model": "model-a",
            "input_tokens": 3,
            "output_tokens": 1,
            "billed_cost": 0.01,
            "cost_source": "provider_billed",
            "provider_usage": _openrouter_exact_evidence(0.01, "resume-1"),
        },
    }
    exact["openrouter_non_byok_audit"] = resume_runner.openrouter_non_byok_audit(exact)
    sealed_exact = resume_runner.seal_result_row(exact)
    exact_reasons = resume_runner.strict_resume_row_invalid_reasons(
        sealed_exact,
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
        require_openrouter_non_byok=True,
    )
    assert exact_reasons == []


def test_resume_policy_violation_overrides_regeneration_and_is_never_repairable() -> None:
    prompt_hash = resume_runner.text_sha256("same prompt")
    explicit_byok_usage = {
        "provider": "openrouter",
        "model": "model-a",
        "requested_provider": "openrouter",
        "requested_model": "model-a",
        "input_tokens": 3,
        "output_tokens": 1,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
        "provider_usage": {
            "is_byok": True,
            "provider_reported_cost": 0.01,
            "response_ids": ["resume-explicit-byok"],
            "router_metadata": {
                **_openrouter_exact_evidence(
                    0.01,
                    "unused",
                )["router_metadata"],
                "is_byok": True,
            },
        },
    }
    row = {
        "group": "B1",
        "provider_spec": dict(resume_runner.GROUP_SPECS["B1"]),
        "routing_trace": {
            "applied_model": "model-a",
            "fallback_model": "model-a",
        },
        "task_id": "task-1",
        "prompt_sha256": prompt_hash,
        "task_input_sha256": "sha256:task-input",
        "run_compatibility_fingerprint": "sha256:run-contract",
        "error": "openrouter_non_byok_metadata_incomplete",
        "final_text": "",
        "llm_request_count": 1,
        "usage": explicit_byok_usage,
    }
    row["openrouter_non_byok_audit"] = resume_runner.openrouter_non_byok_audit(row)
    state = resume_runner.resume_row_completion_state(
        resume_runner.seal_result_row(row),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
        require_openrouter_non_byok=True,
    )

    assert "empty_final_text" in state["generation_reasons"]
    assert state["action"] == "policy_violation"
    assert "openrouter_non_byok_policy_violation" in state["fatal_policy_reasons"]

    stored_marker = {
        **row,
        "error": "openrouter_non_byok_policy_violation",
        "final_text": "accepted answer",
    }
    stored_state = resume_runner.resume_row_completion_state(
        resume_runner.seal_result_row(stored_marker),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
        require_openrouter_non_byok=False,
        judge_required=False,
    )
    assert stored_state["action"] == "policy_violation"
    assert stored_state["cost_metadata_complete"] is False


@pytest.mark.asyncio
async def test_resume_source_policy_violation_stops_before_any_new_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = {"id": "task-a", "prompt": "same prompt"}
    input_path = tmp_path / "tasks.jsonl"
    input_path.write_text(json.dumps(task) + "\n", encoding="utf-8")
    output_dir = tmp_path / "output"
    model = str(resume_runner.GROUP_SPECS["B0"]["model"])
    base = {
        "group": "B0",
        "provider_spec": dict(resume_runner.GROUP_SPECS["B0"]),
        "routing_trace": {
            "applied_model": model,
            "fallback_model": model,
        },
        "task_id": "task-a",
        "prompt_sha256": resume_runner.text_sha256(task["prompt"]),
        "task_input_sha256": resume_runner.canonical_json_sha256(task),
        "run_compatibility_fingerprint": "sha256:run-contract",
        "final_text": "accepted answer",
        "llm_request_count": 1,
        "generation_attempt_budget_used": 1,
        "usage": {
            "provider": "openrouter",
            "model": model,
            "requested_provider": "openrouter",
            "requested_model": model,
            "input_tokens": 3,
            "output_tokens": 1,
            "billed_cost": 0.01,
            "cost_source": "provider_billed",
        },
        "quality_total": 80.0,
        "judge": _complete_legacy_judge("resume-policy-judge"),
    }
    explicit = json.loads(json.dumps(base))
    explicit["usage"]["provider_usage"] = {
        "is_byok": True,
        "provider_reported_cost": 0.01,
        "response_ids": ["resume-policy-generation"],
        "router_metadata": {
            **_openrouter_exact_evidence(
                0.01,
                "unused",
            )["router_metadata"],
            "is_byok": True,
        },
    }
    explicit["error"] = "openrouter_non_byok_policy_violation"
    explicit["openrouter_non_byok_audit"] = resume_runner.openrouter_non_byok_audit(explicit)
    exact = json.loads(json.dumps(base))
    exact["usage"]["provider_usage"] = _openrouter_exact_evidence(
        0.01,
        "resume-later-exact-generation",
    )
    exact["error"] = None
    exact["openrouter_non_byok_audit"] = resume_runner.openrouter_non_byok_audit(exact)
    resume_path = tmp_path / "prior.jsonl"
    resume_path.write_text(
        "\n".join(json.dumps(resume_runner.seal_result_row(row)) for row in (explicit, exact))
        + "\n",
        encoding="utf-8",
    )
    args = resume_runner.build_parser().parse_args(
        [
            "--input",
            str(input_path),
            "--output-dir",
            str(output_dir),
            "--groups",
            "B0",
            "--tool-mode",
            "local_web_tools",
            "--resume-from-jsonl",
            str(resume_path),
            "--judge-model",
            "judge-model",
            "--dry-run",
            "--require-openrouter-non-byok",
            "--continue-after-cost-audit-failure",
        ]
    )
    monkeypatch.setattr(
        resume_runner.GatewayConfig,
        "load",
        lambda _path: GatewayConfig(),
    )
    monkeypatch.setattr(
        resume_runner,
        "build_run_compatibility",
        lambda **_kwargs: {
            "contracts": {
                "B0": {
                    "resolved_llm_runtime": {
                        "provider": "openrouter",
                    }
                }
            },
            "fingerprints": {"B0": "sha256:run-contract"},
        },
    )
    call_counts = {"preflight": 0, "provider": 0, "generation": 0, "judge": 0}

    async def forbidden_preflight(*_args, **_kwargs):
        call_counts["preflight"] += 1
        raise AssertionError("web preflight must not start after a policy violation")

    def forbidden_provider(*_args, **_kwargs):
        call_counts["provider"] += 1
        raise AssertionError("provider construction must not start")

    async def forbidden_generation(*_args, **_kwargs):
        call_counts["generation"] += 1
        raise AssertionError("generation must not start")

    async def forbidden_judge(*_args, **_kwargs):
        call_counts["judge"] += 1
        raise AssertionError("Judge must not start")

    monkeypatch.setattr(
        resume_runner,
        "run_local_web_tools_preflight",
        forbidden_preflight,
    )
    monkeypatch.setattr(resume_runner, "build_single_provider", forbidden_provider)
    monkeypatch.setattr(resume_runner, "run_one", forbidden_generation)
    monkeypatch.setattr(resume_runner, "judge_text", forbidden_judge)

    status = await resume_runner.amain(args)

    assert status == 2
    assert call_counts == {
        "preflight": 0,
        "provider": 0,
        "generation": 0,
        "judge": 0,
    }
    assert not list(output_dir.glob("draco_ensemble_*.jsonl"))
    manifests = list(output_dir.glob("*.policy-violation.manifest.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["status"] == "cost_audit_failed"
    assert manifest["failure"]["stage"] == "resume_source_openrouter_non_byok_policy_violation"
    assert manifest["failure"]["model_or_judge_started"] is False


@pytest.mark.asyncio
async def test_resume_missing_non_byok_receipt_reruns_only_missing_judge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = {"id": "task-a", "prompt": "same prompt"}
    input_path = tmp_path / "tasks.jsonl"
    input_path.write_text(json.dumps(task) + "\n", encoding="utf-8")
    output_dir = tmp_path / "output"
    model = str(resume_runner.GROUP_SPECS["B0"]["model"])
    prior = {
        "group": "B0",
        "provider_spec": dict(resume_runner.GROUP_SPECS["B0"]),
        "routing_trace": {
            "applied_model": model,
            "fallback_model": model,
        },
        "task_id": "task-a",
        "prompt_sha256": resume_runner.text_sha256(task["prompt"]),
        "task_input_sha256": resume_runner.canonical_json_sha256(task),
        "run_compatibility_fingerprint": "sha256:run-contract",
        "error": "openrouter_non_byok_metadata_incomplete",
        "final_text": "accepted generation",
        "latency_ms": 1,
        "llm_request_count": 2,
        "generation_attempt_count": 1,
        "generation_attempt_budget_used": 1,
        "usage": {
            "provider": "openrouter",
            "model": model,
            "requested_provider": "openrouter",
            "requested_model": model,
            "input_tokens": 3,
            "output_tokens": 1,
            "billed_cost": 0.01,
            "cost_source": "provider_billed",
            "provider_usage": _openrouter_exact_evidence(
                0.01,
                "resume-generation-exact",
            ),
        },
        "judge": None,
        "quality_total": None,
    }
    prior["openrouter_non_byok_audit"] = resume_runner.openrouter_non_byok_audit(prior)
    prior = resume_runner.seal_result_row(prior)
    resume_path = tmp_path / "prior.jsonl"
    resume_path.write_text(json.dumps(prior) + "\n", encoding="utf-8")
    args = resume_runner.build_parser().parse_args(
        [
            "--input",
            str(input_path),
            "--output-dir",
            str(output_dir),
            "--groups",
            "B0",
            "--tool-mode",
            "local_web_tools",
            "--resume-from-jsonl",
            str(resume_path),
            "--judge-model",
            "judge-model",
            "--dry-run",
            "--require-openrouter-non-byok",
        ]
    )
    monkeypatch.setattr(
        resume_runner.GatewayConfig,
        "load",
        lambda _path: GatewayConfig(),
    )
    monkeypatch.setattr(
        resume_runner,
        "build_run_compatibility",
        lambda **_kwargs: {
            "contracts": {
                "B0": {
                    "resolved_llm_runtime": {
                        "provider": "openrouter",
                    }
                }
            },
            "fingerprints": {"B0": "sha256:run-contract"},
        },
    )
    call_counts = {"provider": 0, "generation": 0, "judge": 0}

    def fake_provider(*_args, **_kwargs):
        call_counts["provider"] += 1
        return object()

    async def forbidden_generation(*_args, **_kwargs):
        call_counts["generation"] += 1
        raise AssertionError("accepted generation must not rerun")

    async def fake_judge(*_args, **_kwargs):
        call_counts["judge"] += 1
        return _complete_legacy_judge("resume-new-judge")

    monkeypatch.setattr(resume_runner, "build_single_provider", fake_provider)
    monkeypatch.setattr(resume_runner, "run_one", forbidden_generation)
    monkeypatch.setattr(resume_runner, "judge_text", fake_judge)

    status = await resume_runner.amain(args)

    assert status == 2
    assert call_counts == {"provider": 1, "generation": 0, "judge": 1}
    result_paths = list(output_dir.glob("draco_ensemble_*.jsonl"))
    assert len(result_paths) == 1
    rows = [
        json.loads(line)
        for line in result_paths[0].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    repaired = rows[0]
    assert repaired["final_text"] == prior["final_text"]
    assert repaired["usage"]["input_tokens"] == prior["usage"]["input_tokens"]
    assert repaired["usage"]["output_tokens"] == prior["usage"]["output_tokens"]
    assert repaired["execution"]["generation_reused"] is True
    assert repaired["execution"]["judge_reran"] is True
    assert repaired["execution"]["prior_generation_attempts_used"] == 1
    assert repaired["generation_attempt_budget_used"] == 1
    assert repaired["judge"]["score_status"] == "complete"
    assert repaired["openrouter_non_byok_audit"]["status"] == ("metadata_incomplete")
    assert repaired["error"] == "openrouter_non_byok_metadata_incomplete"
    manifests = list(output_dir.glob("draco_run_*.manifest.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    preflight = manifest["tool_policy"]["local_web_tools"]["preflight"]
    assert preflight["status"] == "skipped_not_required"
    assert preflight["model_regenerate_pair_count"] == 0
    assert preflight["preflight_calls"] == {"web_search": 0, "web_fetch": 0}
    assert manifest["resume_selection"]["model_regenerate_pair_count"] == 0


@pytest.mark.asyncio
async def test_resume_metadata_only_repairs_once_without_generation_or_judge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = {"id": "task-a", "prompt": "same prompt"}
    input_path = tmp_path / "tasks.jsonl"
    input_path.write_text(json.dumps(task) + "\n", encoding="utf-8")
    output_dir = tmp_path / "output"
    model = str(resume_runner.GROUP_SPECS["B0"]["model"])
    prior = resume_runner.seal_result_row(
        {
            "group": "B0",
            "provider_spec": dict(resume_runner.GROUP_SPECS["B0"]),
            "routing_trace": {
                "applied_model": model,
                "fallback_model": model,
            },
            "task_id": "task-a",
            "prompt_sha256": resume_runner.text_sha256(task["prompt"]),
            "task_input_sha256": resume_runner.canonical_json_sha256(task),
            "run_compatibility_fingerprint": "sha256:run-contract",
            "error": "cost_metadata_incomplete",
            "final_text": "accepted generation",
            "latency_ms": 1,
            "llm_request_count": 1,
            "generation_attempt_count": 1,
            "generation_attempt_budget_used": 1,
            "usage": {
                "provider": "openrouter",
                "model": model,
                "requested_provider": "openrouter",
                "requested_model": model,
                "input_tokens": 3,
                "output_tokens": 1,
                "billed_cost": 0.0,
                "cost_source": "none",
            },
            "judge": _complete_legacy_judge("resume-existing-judge"),
            "quality_total": 80.0,
        }
    )
    resume_path = tmp_path / "prior.jsonl"
    resume_path.write_text(json.dumps(prior) + "\n", encoding="utf-8")
    args = resume_runner.build_parser().parse_args(
        [
            "--input",
            str(input_path),
            "--output-dir",
            str(output_dir),
            "--groups",
            "B0",
            "--tool-mode",
            "local_web_tools",
            "--resume-from-jsonl",
            str(resume_path),
            "--judge-model",
            "judge-model",
            "--dry-run",
        ]
    )
    monkeypatch.setattr(
        resume_runner.GatewayConfig,
        "load",
        lambda _path: GatewayConfig(),
    )
    monkeypatch.setattr(
        resume_runner,
        "build_run_compatibility",
        lambda **_kwargs: {
            "contracts": {
                "B0": {
                    "resolved_llm_runtime": {
                        "provider": "openrouter",
                    }
                }
            },
            "fingerprints": {"B0": "sha256:run-contract"},
        },
    )
    call_counts = {"generation": 0, "judge": 0, "metadata": 0}

    async def forbidden_generation(*_args, **_kwargs):
        call_counts["generation"] += 1
        raise AssertionError("metadata-only repair must not regenerate")

    async def forbidden_judge(*_args, **_kwargs):
        call_counts["judge"] += 1
        raise AssertionError("metadata-only repair must not call Judge")

    original_cost_repair = resume_runner.repair_row_cost_metadata_with_estimates

    def counted_cost_repair(row):
        call_counts["metadata"] += 1
        return original_cost_repair(row)

    monkeypatch.setattr(resume_runner, "run_one", forbidden_generation)
    monkeypatch.setattr(resume_runner, "judge_text", forbidden_judge)
    monkeypatch.setattr(
        resume_runner,
        "repair_row_cost_metadata_with_estimates",
        counted_cost_repair,
    )

    status = await resume_runner.amain(args)

    assert status in {0, 2}
    assert call_counts == {"generation": 0, "judge": 0, "metadata": 1}
    result_paths = list(output_dir.glob("draco_ensemble_*.jsonl"))
    assert len(result_paths) == 1
    repaired = json.loads(
        next(
            line
            for line in result_paths[0].read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    )
    execution = repaired["execution"]
    assert execution["generation_reused"] is True
    assert execution["judge_reran"] is False
    assert execution["metadata_repair_attempted"] is True
    assert isinstance(execution["metadata_repair_attempted_at"], float)
    assert execution["metadata_repair_summary"]["generation_called"] is False
    assert execution["metadata_repair_summary"]["judge_called"] is False
    manifests = list(output_dir.glob("draco_run_*.manifest.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    preflight = manifest["tool_policy"]["local_web_tools"]["preflight"]
    assert preflight["status"] == "skipped_not_required"
    assert preflight["preflight_calls"] == {"web_search": 0, "web_fetch": 0}
    assert manifest["resume_selection"]["model_regenerate_pair_count"] == 0


def test_resume_classifies_generation_judge_and_metadata_independently() -> None:
    resume_runner = _load_resume_runner()
    prompt_hash = resume_runner.text_sha256("same prompt")
    base = {
        "group": "B1",
        "provider_spec": dict(resume_runner.GROUP_SPECS["B1"]),
        "routing_trace": {
            "applied_model": "model-a",
            "fallback_model": "model-a",
        },
        "task_id": "task-1",
        "prompt_sha256": prompt_hash,
        "task_input_sha256": "sha256:task-input",
        "run_compatibility_fingerprint": "sha256:run-contract",
        "error": None,
        "final_text": "accepted answer",
        "llm_request_count": 1,
        "usage": {
            "provider": "openrouter",
            "model": "model-a",
            "requested_provider": "openrouter",
            "requested_model": "model-a",
            "input_tokens": 3,
            "output_tokens": 1,
            "billed_cost": 0.01,
            "cost_source": "provider_billed",
            "provider_usage": _openrouter_exact_evidence(
                0.01,
                "resume-classification",
            ),
        },
    }

    judge_only = resume_runner.resume_row_completion_state(
        resume_runner.seal_result_row(base),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
    )
    assert judge_only["generation_valid"] is True
    assert judge_only["action"] == "judge_only"

    judge_disabled = resume_runner.resume_row_completion_state(
        resume_runner.seal_result_row(base),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
        judge_required=False,
    )
    assert judge_disabled["judge_complete"] is True
    assert judge_disabled["action"] == "complete"

    legacy_row = {
        **base,
        "quality_total": 80.0,
        "judge": _complete_legacy_judge("resume-legacy-judge"),
    }
    legacy = resume_runner.resume_row_completion_state(
        resume_runner.seal_result_row(legacy_row),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
    )
    assert legacy["judge_complete"] is True
    assert legacy["action"] == "complete"

    metadata_only_row = {
        **base,
        "llm_request_count": 1,
        "usage": {
            "provider": "openrouter",
            "model": "model-a",
            "requested_provider": "openrouter",
            "requested_model": "model-a",
            "input_tokens": 3,
            "output_tokens": 1,
            "cost_source": "none",
        },
        "quality_total": 80.0,
        "judge": _complete_legacy_judge("resume-duplicate-judge"),
    }
    metadata_only = resume_runner.resume_row_completion_state(
        resume_runner.seal_result_row(metadata_only_row),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
    )
    assert metadata_only["generation_valid"] is True
    assert metadata_only["judge_complete"] is True
    assert metadata_only["cost_metadata_complete"] is False
    assert metadata_only["metadata_repair_attempted"] is False
    assert metadata_only["action"] == "metadata_only"

    already_attempted_row = json.loads(json.dumps(metadata_only_row))
    already_attempted_row["execution"] = {
        "metadata_repair_attempted": True,
        "metadata_repair_attempted_at": 123.0,
        "metadata_repair_summary": {
            "status": "applied",
            "generation_called": False,
            "judge_called": False,
            "changed": False,
        },
    }
    already_attempted = resume_runner.resume_row_completion_state(
        resume_runner.seal_result_row(already_attempted_row),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
    )
    assert already_attempted["action"] == "metadata_only"
    assert already_attempted["metadata_repair_attempted"] is True

    regenerate = resume_runner.resume_row_completion_state(
        resume_runner.seal_result_row({**base, "final_text": ""}),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
    )
    assert regenerate["action"] == "regenerate"


def test_resume_only_treats_resolved_error_markers_as_stale() -> None:
    prompt_hash = resume_runner.text_sha256("same prompt")
    base = {
        "group": "B1",
        "provider_spec": dict(resume_runner.GROUP_SPECS["B1"]),
        "routing_trace": {
            "applied_model": "model-a",
            "fallback_model": "model-a",
        },
        "task_id": "task-1",
        "prompt_sha256": prompt_hash,
        "task_input_sha256": "sha256:task-input",
        "run_compatibility_fingerprint": "sha256:run-contract",
        "final_text": "accepted answer",
        "llm_request_count": 1,
        "usage": {
            "provider": "openrouter",
            "model": "model-a",
            "requested_provider": "openrouter",
            "requested_model": "model-a",
            "input_tokens": 3,
            "output_tokens": 1,
            "billed_cost": 0.01,
            "cost_source": "provider_billed",
            "provider_usage": _openrouter_exact_evidence(
                0.01,
                "stale-error-generation",
            ),
        },
    }

    real_judge_gap = resume_runner.resume_row_completion_state(
        resume_runner.seal_result_row(
            {
                **base,
                "error": "judge_incomplete",
                "judge": None,
            }
        ),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
    )
    assert real_judge_gap["action"] == "judge_only"
    assert "stale_completion_error" not in real_judge_gap["cost_metadata_reasons"]

    stale_judge_marker = resume_runner.resume_row_completion_state(
        resume_runner.seal_result_row(
            {
                **base,
                "error": "judge_incomplete",
                "judge": _complete_legacy_judge("stale-error-judge"),
                "quality_total": 80.0,
            }
        ),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
    )
    assert stale_judge_marker["action"] == "metadata_only"
    assert "stale_completion_error" in stale_judge_marker["cost_metadata_reasons"]


def test_resume_backfills_actual_identity_only_from_unique_receipts() -> None:
    prompt_hash = resume_runner.text_sha256("same prompt")
    generation_receipt = {
        "provider": "openrouter",
        "model": "model-a",
        "input_tokens": 3,
        "output_tokens": 1,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
        "provider_usage": _openrouter_exact_evidence(
            0.01,
            "identity-backfill",
        ),
    }
    row = {
        "group": "B1",
        "provider_spec": dict(resume_runner.GROUP_SPECS["B1"]),
        "routing_trace": {
            "applied_model": "model-a",
            "fallback_model": "model-a",
        },
        "task_id": "task-1",
        "prompt_sha256": prompt_hash,
        "task_input_sha256": "sha256:task-input",
        "run_compatibility_fingerprint": "sha256:run-contract",
        "error": None,
        "final_text": "accepted answer",
        "llm_request_count": 1,
        "usage": {
            "requested_provider": "openrouter",
            "requested_model": "model-a",
            "model_usage_breakdown": [generation_receipt],
            "input_tokens": 3,
            "output_tokens": 1,
            "billed_cost": 0.01,
            "cost_source": "provider_billed",
        },
        "judge": _complete_legacy_judge("identity-backfill-judge"),
        "quality_total": 80.0,
    }
    before = resume_runner.resume_row_completion_state(
        resume_runner.seal_result_row(row),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
    )
    assert before["generation_valid"] is True
    assert before["action"] == "metadata_only"
    assert "actual_provider_metadata_backfill_required" in (before["cost_metadata_reasons"])
    assert "actual_model_metadata_backfill_required" in before["cost_metadata_reasons"]

    assert resume_runner.backfill_usage_actual_identity(row["usage"]) is True
    after = resume_runner.resume_row_completion_state(
        resume_runner.seal_result_row(row),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
    )
    assert after["action"] == "complete"
    assert row["usage"]["provider"] == "openrouter"
    assert row["usage"]["model"] == "model-a"

    no_receipt = json.loads(json.dumps(row))
    no_receipt["usage"].pop("provider", None)
    no_receipt["usage"].pop("model", None)
    no_receipt["usage"]["model_usage_breakdown"] = []
    no_receipt_state = resume_runner.resume_row_completion_state(
        resume_runner.seal_result_row(no_receipt),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
    )
    assert no_receipt_state["generation_valid"] is True
    assert no_receipt_state["action"] == "metadata_only"
    assert "actual_model_evidence_missing" in no_receipt_state["cost_metadata_reasons"]
    assert "actual_provider_evidence_missing" in no_receipt_state["cost_metadata_reasons"]


def test_resume_backfills_missing_requested_identity_but_rejects_mismatch() -> None:
    prompt_hash = resume_runner.text_sha256("same prompt")
    contract = {"resolved_llm_runtime": {"provider": "openrouter"}}
    row = {
        "group": "B1",
        "provider_spec": dict(resume_runner.GROUP_SPECS["B1"]),
        "routing_trace": {
            "applied_model": "model-a",
            "fallback_model": "model-a",
        },
        "task_id": "task-1",
        "prompt_sha256": prompt_hash,
        "task_input_sha256": "sha256:task-input",
        "run_compatibility_fingerprint": "sha256:run-contract",
        "error": None,
        "final_text": "accepted answer",
        "llm_request_count": 1,
        "usage": {
            "provider": "openrouter",
            "model": "model-a",
            "input_tokens": 3,
            "output_tokens": 1,
            "billed_cost": 0.01,
            "cost_source": "provider_billed",
            "provider_usage": _openrouter_exact_evidence(
                0.01,
                "requested-identity-backfill",
            ),
        },
        "judge": _complete_legacy_judge("requested-identity-judge"),
        "quality_total": 80.0,
    }

    before = resume_runner.resume_row_completion_state(
        resume_runner.seal_result_row(row),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
        expected_run_compatibility_contract=contract,
    )

    assert before["generation_valid"] is True
    assert before["action"] == "metadata_only"
    assert "requested_model_metadata_backfill_required" in before["cost_metadata_reasons"]
    assert "requested_provider_metadata_backfill_required" in before["cost_metadata_reasons"]
    assert (
        resume_runner.backfill_saved_row_requested_identity(
            row,
            expected_run_compatibility_contract=contract,
        )
        is True
    )
    after = resume_runner.resume_row_completion_state(
        resume_runner.seal_result_row(row),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
        expected_run_compatibility_contract=contract,
    )
    assert after["action"] == "complete"
    assert row["usage"]["requested_model"] == "model-a"
    assert row["usage"]["requested_provider"] == "openrouter"

    tampered = json.loads(json.dumps(row))
    tampered["usage"]["requested_model"] = "model-b"
    mismatch = resume_runner.resume_row_completion_state(
        resume_runner.seal_result_row(tampered),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
        expected_run_compatibility_contract=contract,
    )
    assert mismatch["action"] == "regenerate"
    assert "wrong_requested_model" in mismatch["generation_reasons"]


def test_resume_prefers_cost_complete_duplicate_that_can_converge(
    tmp_path: Path,
) -> None:
    resume_runner = _load_resume_runner()
    prompt_hash = resume_runner.text_sha256("same prompt")
    base = {
        "group": "B1",
        "provider_spec": dict(resume_runner.GROUP_SPECS["B1"]),
        "routing_trace": {
            "applied_model": "model-a",
            "fallback_model": "model-a",
        },
        "task_id": "task-1",
        "prompt_sha256": prompt_hash,
        "task_input_sha256": "sha256:task-input",
        "run_compatibility_fingerprint": "sha256:run-contract",
        "error": None,
        "final_text": "accepted answer",
        "llm_request_count": 1,
    }
    cost_complete_missing_judge = resume_runner.seal_result_row(
        {
            **base,
            "source_marker": "repairable",
            "usage": {
                "provider": "openrouter",
                "model": "model-a",
                "requested_provider": "openrouter",
                "requested_model": "model-a",
                "input_tokens": 3,
                "output_tokens": 1,
                "billed_cost": 0.01,
                "cost_source": "provider_billed",
                "provider_usage": _openrouter_exact_evidence(
                    0.01,
                    "repairable-duplicate",
                ),
            },
        }
    )
    judge_complete_missing_cost = resume_runner.seal_result_row(
        {
            **base,
            "source_marker": "unrepairable",
            "usage": {
                "provider": "openrouter",
                "model": "model-a",
                "requested_provider": "openrouter",
                "requested_model": "model-a",
                "input_tokens": 3,
                "output_tokens": 1,
                "cost_source": "none",
            },
            "quality_total": 80.0,
            "judge": _complete_legacy_judge("unrepairable-judge"),
        }
    )
    path = tmp_path / "duplicates.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(row)
            for row in (
                cost_complete_missing_judge,
                judge_complete_missing_cost,
            )
        )
        + "\n",
        encoding="utf-8",
    )

    states, audit = resume_runner.load_resume_group_task_states(
        resume_paths=[path],
        selected_keys={("B1", "task-1")},
        prompt_hashes={"task-1": prompt_hash},
        task_input_hashes={"task-1": "sha256:task-input"},
        run_compatibility_fingerprints={"B1": "sha256:run-contract"},
    )

    state = states[("B1", "task-1")]
    assert state["action"] == "judge_only"
    assert state["row"]["source_marker"] == "repairable"
    assert audit["strict_invalid_attempt_count"] == 2


@pytest.mark.parametrize("reverse_order", [False, True])
def test_resume_prefers_latest_generation_within_same_completion_rank(
    tmp_path: Path,
    reverse_order: bool,
) -> None:
    resume_runner = _load_resume_runner()
    prompt_hash = resume_runner.text_sha256("same prompt")
    base = {
        "group": "B1",
        "provider_spec": dict(resume_runner.GROUP_SPECS["B1"]),
        "routing_trace": {
            "applied_model": "model-a",
            "fallback_model": "model-a",
        },
        "task_id": "task-1",
        "prompt_sha256": prompt_hash,
        "task_input_sha256": "sha256:task-input",
        "run_compatibility_fingerprint": "sha256:run-contract",
        "error": None,
        "final_text": "accepted answer",
        "llm_request_count": 1,
        "generation_attempt_budget_used": 1,
        "usage": {
            "provider": "openrouter",
            "model": "model-a",
            "requested_provider": "openrouter",
            "requested_model": "model-a",
            "input_tokens": 3,
            "output_tokens": 1,
            "billed_cost": 0.01,
            "cost_source": "provider_billed",
            "provider_usage": _openrouter_exact_evidence(
                0.01,
                "latest-generation",
            ),
        },
        "quality_total": 80.0,
        "judge": _complete_legacy_judge("latest-generation-judge"),
    }
    older = resume_runner.seal_result_row(
        {
            **base,
            "source_marker": "older",
            "generation_completed_at": 100.0,
            "completed_at": 500.0,
        }
    )
    newer = resume_runner.seal_result_row(
        {
            **base,
            "source_marker": "newer",
            "generation_completed_at": 200.0,
            "completed_at": 300.0,
        }
    )
    rows = [older, newer]
    if reverse_order:
        rows.reverse()
    path = tmp_path / "latest-duplicates.jsonl"
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    states, _ = resume_runner.load_resume_group_task_states(
        resume_paths=[path],
        selected_keys={("B1", "task-1")},
        prompt_hashes={"task-1": prompt_hash},
        task_input_hashes={"task-1": "sha256:task-input"},
        run_compatibility_fingerprints={"B1": "sha256:run-contract"},
    )

    state = states[("B1", "task-1")]
    assert state["action"] == "complete"
    assert state["row"]["source_marker"] == "newer"


def test_resume_generation_attempt_budget_is_cumulative() -> None:
    resume_runner = _load_resume_runner()

    assert resume_runner.remaining_generation_attempts(0, 3) == 3
    assert resume_runner.remaining_generation_attempts(1, 3) == 2
    assert resume_runner.remaining_generation_attempts(2, 3) == 1
    assert resume_runner.remaining_generation_attempts(3, 3) == 0
    assert resume_runner.remaining_generation_attempts(8, 3) == 0


def test_resume_generation_attempt_budget_is_cumulative_across_jsonl_waves(
    tmp_path: Path,
) -> None:
    prompt_hash = resume_runner.text_sha256("same prompt")
    base = {
        "group": "B1",
        "provider_spec": dict(resume_runner.GROUP_SPECS["B1"]),
        "routing_trace": {
            "applied_model": "model-a",
            "fallback_model": "model-a",
        },
        "task_id": "task-1",
        "prompt_sha256": prompt_hash,
        "task_input_sha256": "sha256:task-input",
        "run_compatibility_fingerprint": "sha256:run-contract",
        "error": "provider_error",
        "final_text": "",
        "llm_request_count": 1,
        "generation_attempt_count": 1,
        "usage": {},
    }
    paths: list[Path] = []
    for wave, cumulative_budget in enumerate((1, 2, 3), start=1):
        row = resume_runner.seal_result_row(
            {
                **base,
                "generation_attempt_budget_used": cumulative_budget,
                "generation_completed_at": float(wave),
            }
        )
        path = tmp_path / f"wave-{wave}.jsonl"
        path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        paths.append(path)

    states_after_two, _ = resume_runner.load_resume_group_task_states(
        resume_paths=paths[:2],
        selected_keys={("B1", "task-1")},
        prompt_hashes={"task-1": prompt_hash},
        task_input_hashes={"task-1": "sha256:task-input"},
        run_compatibility_fingerprints={"B1": "sha256:run-contract"},
    )
    state_after_two = states_after_two[("B1", "task-1")]
    assert state_after_two["action"] == "regenerate"
    assert state_after_two["prior_generation_attempts_used"] == 2
    assert (
        resume_runner.remaining_generation_attempts(
            state_after_two["prior_generation_attempts_used"],
            3,
        )
        == 1
    )

    states_after_three, _ = resume_runner.load_resume_group_task_states(
        resume_paths=paths,
        selected_keys={("B1", "task-1")},
        prompt_hashes={"task-1": prompt_hash},
        task_input_hashes={"task-1": "sha256:task-input"},
        run_compatibility_fingerprints={"B1": "sha256:run-contract"},
    )
    state_after_three = states_after_three[("B1", "task-1")]
    assert state_after_three["action"] == "regenerate"
    assert state_after_three["prior_generation_attempts_used"] == 3
    assert (
        resume_runner.remaining_generation_attempts(
            state_after_three["prior_generation_attempts_used"],
            3,
        )
        == 0
    )


def _strict_attempt_resume_row(
    *,
    attempt_id: str,
    cumulative_budget: int,
    generation_completed_at: float,
) -> dict[str, object]:
    prompt_hash = resume_runner.text_sha256("same prompt")
    attempt = {
        "attempt_id": attempt_id,
        "attempt_kind": "generation",
        "attempt": cumulative_budget,
        "started_at": generation_completed_at - 0.5,
        "completed_at": generation_completed_at,
        "retryable": True,
        "retry_reason": "provider_error",
        "retry_suppressed_reason": "",
        "will_retry": False,
        "retry_backoff_s": 0.0,
        "run": {
            "llm_request_count": 1,
            "usage_unknown_count": 1,
            "error": "provider_error",
            "usage": {},
        },
    }
    return {
        "group": "B1",
        "provider_spec": dict(resume_runner.GROUP_SPECS["B1"]),
        "routing_trace": {
            "applied_model": "model-a",
            "fallback_model": "model-a",
        },
        "task_id": "task-1",
        "prompt_sha256": prompt_hash,
        "task_input_sha256": "sha256:task-input",
        "run_compatibility_fingerprint": "sha256:run-contract",
        "error": "provider_error",
        "final_text": "",
        "llm_request_count": 1,
        "generation_attempt_count": 1,
        "generation_attempt_evidence_schema": (resume_runner.GENERATION_ATTEMPT_EVIDENCE_SCHEMA),
        "generation_attempt_budget_limit": 3,
        "generation_attempt_budget_used": cumulative_budget,
        "generation_completed_at": generation_completed_at,
        "actual_spend_metrics": {"generation_attempt_count": 1},
        "execution": {
            "generation_attempts": [attempt],
            "prior_generation_attempts_used": cumulative_budget - 1,
            "generation_attempt_budget_remaining": 3 - cumulative_budget,
        },
        "usage": {},
    }


def test_resume_strict_attempt_evidence_reconstructs_cumulative_budget(
    tmp_path: Path,
) -> None:
    paths: list[Path] = []
    for wave in range(1, 4):
        row = resume_runner.seal_result_row(
            _strict_attempt_resume_row(
                attempt_id=f"{wave:032x}",
                cumulative_budget=wave,
                generation_completed_at=float(wave),
            )
        )
        path = tmp_path / f"strict-wave-{wave}.jsonl"
        path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        paths.append(path)

    states, _ = resume_runner.load_resume_group_task_states(
        resume_paths=paths,
        selected_keys={("B1", "task-1")},
        prompt_hashes={"task-1": resume_runner.text_sha256("same prompt")},
        task_input_hashes={"task-1": "sha256:task-input"},
        run_compatibility_fingerprints={"B1": "sha256:run-contract"},
    )

    state = states[("B1", "task-1")]
    assert state["prior_generation_attempts_used"] == 3
    assert state["observed_unique_generation_attempt_count"] == 3
    assert state["generation_attempt_evidence_mode"] == "strict_v1"


def test_resume_strict_attempt_evidence_accepts_monotonic_repair(
    tmp_path: Path,
) -> None:
    attempt_id = "a" * 32
    first = _strict_attempt_resume_row(
        attempt_id=attempt_id,
        cumulative_budget=1,
        generation_completed_at=1.0,
    )
    first_attempt = first["execution"]["generation_attempts"][0]
    first_attempt["run"]["usage"] = {
        "provider": "openrouter",
        "model": "model-a",
        "input_tokens": 10,
        "output_tokens": 2,
        "cost_source": "none",
    }
    repaired = json.loads(json.dumps(first))
    repaired["generation_completed_at"] = 2.0
    repaired["execution"]["resume_action"] = "metadata_only"
    repaired["execution"]["generation_reused"] = True
    repaired["execution"]["prior_generation_attempts_used"] = 1
    repaired["execution"]["generation_attempt_budget_remaining"] = 2
    repaired_usage = repaired["execution"]["generation_attempts"][0]["run"]["usage"]
    repaired_usage.update(
        {
            "cost_source": "opensquilla_static_estimate",
            "estimated_cost_usd": 0.002,
            "cost_usd": 0.002,
            "provider_usage": {
                "cost_repair": "token_price_estimate",
                "price_source": "frozen",
            },
        }
    )

    paths: list[Path] = []
    for wave, row in enumerate((first, repaired), start=1):
        path = tmp_path / f"repair-wave-{wave}.jsonl"
        path.write_text(
            json.dumps(resume_runner.seal_result_row(row)) + "\n",
            encoding="utf-8",
        )
        paths.append(path)

    states, _ = resume_runner.load_resume_group_task_states(
        resume_paths=paths,
        selected_keys={("B1", "task-1")},
        prompt_hashes={"task-1": resume_runner.text_sha256("same prompt")},
        task_input_hashes={"task-1": "sha256:task-input"},
        run_compatibility_fingerprints={"B1": "sha256:run-contract"},
    )

    state = states[("B1", "task-1")]
    assert state["prior_generation_attempts_used"] == 1
    assert state["observed_unique_generation_attempt_count"] == 1
    assert state["row"]["execution"]["generation_attempts"][0]["attempt_id"] == attempt_id


def test_resume_strict_attempt_evidence_allows_cost_confidence_upgrade_only(
    tmp_path: Path,
) -> None:
    attempt_id = "c" * 32
    initial = _strict_attempt_resume_row(
        attempt_id=attempt_id,
        cumulative_budget=1,
        generation_completed_at=1.0,
    )
    initial_usage = initial["execution"]["generation_attempts"][0]["run"]["usage"]
    initial_usage.update(
        {
            "provider": "openrouter",
            "model": "model-a",
            "input_tokens": 10,
            "output_tokens": 2,
            "cost_source": "none",
        }
    )
    estimate = json.loads(json.dumps(initial))
    estimate["generation_completed_at"] = 2.0
    estimate["execution"].update(
        {
            "resume_action": "metadata_only",
            "generation_reused": True,
            "prior_generation_attempts_used": 1,
            "generation_attempt_budget_remaining": 2,
        }
    )
    estimate_usage = estimate["execution"]["generation_attempts"][0]["run"]["usage"]
    estimate_usage.update(
        {
            "cost_source": "opensquilla_static_estimate",
            "estimated_cost_usd": 0.002,
            "cost_usd": 0.002,
            "provider_usage": {
                "cost_repair": "token_price_estimate",
                "price_source": "frozen",
            },
        }
    )
    exact = json.loads(json.dumps(estimate))
    exact["generation_completed_at"] = 3.0
    exact_usage = exact["execution"]["generation_attempts"][0]["run"]["usage"]
    exact_usage.update(
        {
            "cost_source": "provider_billed",
            "billed_cost": 0.0021,
            "provider_usage": {
                **exact_usage["provider_usage"],
                **_openrouter_exact_evidence(
                    0.0021,
                    "strict-attempt-cost-upgrade",
                ),
            },
        }
    )

    accepted_paths: list[Path] = []
    for wave, row in enumerate((initial, estimate, exact), start=1):
        path = tmp_path / f"cost-upgrade-wave-{wave}.jsonl"
        path.write_text(
            json.dumps(resume_runner.seal_result_row(row)) + "\n",
            encoding="utf-8",
        )
        accepted_paths.append(path)

    states, _ = resume_runner.load_resume_group_task_states(
        resume_paths=accepted_paths,
        selected_keys={("B1", "task-1")},
        prompt_hashes={"task-1": resume_runner.text_sha256("same prompt")},
        task_input_hashes={"task-1": "sha256:task-input"},
        run_compatibility_fingerprints={"B1": "sha256:run-contract"},
    )
    retained_usage = states[("B1", "task-1")]["row"]["execution"]["generation_attempts"][0]["run"][
        "usage"
    ]
    assert retained_usage["estimated_cost_usd"] == pytest.approx(0.002)
    assert retained_usage["cost_usd"] == pytest.approx(0.002)
    assert retained_usage["billed_cost"] == pytest.approx(0.0021)
    assert retained_usage["cost_source"] == "provider_billed"

    conflicting_exact = json.loads(json.dumps(exact))
    conflicting_exact["generation_completed_at"] = 4.0
    conflicting_usage = conflicting_exact["execution"]["generation_attempts"][0]["run"]["usage"]
    conflicting_usage["billed_cost"] = 0.003
    conflicting_usage["provider_usage"]["provider_reported_cost"] = 0.003
    conflict_path = tmp_path / "cost-upgrade-wave-4.jsonl"
    conflict_path.write_text(
        json.dumps(resume_runner.seal_result_row(conflicting_exact)) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="conflicting evidence"):
        resume_runner.load_resume_group_task_states(
            resume_paths=[*accepted_paths, conflict_path],
            selected_keys={("B1", "task-1")},
            prompt_hashes={"task-1": resume_runner.text_sha256("same prompt")},
            task_input_hashes={"task-1": "sha256:task-input"},
            run_compatibility_fingerprints={"B1": "sha256:run-contract"},
        )


def test_resume_strict_attempt_evidence_rejects_cross_task_owner(
    tmp_path: Path,
) -> None:
    attempt_id = "b" * 32
    first = _strict_attempt_resume_row(
        attempt_id=attempt_id,
        cumulative_budget=1,
        generation_completed_at=1.0,
    )
    second = _strict_attempt_resume_row(
        attempt_id=attempt_id,
        cumulative_budget=1,
        generation_completed_at=2.0,
    )
    second["task_id"] = "task-2"
    second["prompt_sha256"] = resume_runner.text_sha256("other prompt")
    second["task_input_sha256"] = "sha256:other-task-input"

    paths: list[Path] = []
    for wave, row in enumerate((first, second), start=1):
        path = tmp_path / f"owner-wave-{wave}.jsonl"
        path.write_text(
            json.dumps(resume_runner.seal_result_row(row)) + "\n",
            encoding="utf-8",
        )
        paths.append(path)

    with pytest.raises(ValueError, match="already owned"):
        resume_runner.load_resume_group_task_states(
            resume_paths=paths,
            selected_keys={("B1", "task-1"), ("B1", "task-2")},
            prompt_hashes={
                "task-1": resume_runner.text_sha256("same prompt"),
                "task-2": resume_runner.text_sha256("other prompt"),
            },
            task_input_hashes={
                "task-1": "sha256:task-input",
                "task-2": "sha256:other-task-input",
            },
            run_compatibility_fingerprints={"B1": "sha256:run-contract"},
        )


@pytest.mark.parametrize(
    ("mutation", "error_match"),
    [
        ("non_monotonic", "contradicts unique physical evidence"),
        ("over_limit", "budget declaration is invalid"),
        ("conflicting_identity", "conflicting evidence"),
        ("ordinal_reset", "ordinal is not cumulative"),
        ("mixed_schema", "mixed generation attempt evidence schemas"),
        ("actual_count", "actual-spend generation attempt count contradicts"),
    ],
)
def test_resume_strict_attempt_evidence_fails_closed_on_contradiction(
    tmp_path: Path,
    mutation: str,
    error_match: str,
) -> None:
    first = _strict_attempt_resume_row(
        attempt_id="1" * 32,
        cumulative_budget=1,
        generation_completed_at=1.0,
    )
    second = _strict_attempt_resume_row(
        attempt_id="2" * 32,
        cumulative_budget=2,
        generation_completed_at=2.0,
    )
    if mutation == "non_monotonic":
        second["generation_attempt_budget_used"] = 0
        second["execution"]["generation_attempt_budget_remaining"] = 3
    elif mutation == "over_limit":
        second["generation_attempt_budget_used"] = 4
    elif mutation == "conflicting_identity":
        second["execution"]["generation_attempts"][0]["attempt_id"] = "1" * 32
    elif mutation == "ordinal_reset":
        second["execution"]["generation_attempts"][0]["attempt"] = 1
    elif mutation == "mixed_schema":
        second.pop("generation_attempt_evidence_schema")
    else:
        second["actual_spend_metrics"]["generation_attempt_count"] = 2

    paths: list[Path] = []
    for wave, row in enumerate((first, second), start=1):
        path = tmp_path / f"contradictory-wave-{wave}.jsonl"
        path.write_text(
            json.dumps(resume_runner.seal_result_row(row)) + "\n",
            encoding="utf-8",
        )
        paths.append(path)

    with pytest.raises(ValueError, match=error_match):
        resume_runner.load_resume_group_task_states(
            resume_paths=paths,
            selected_keys={("B1", "task-1")},
            prompt_hashes={"task-1": resume_runner.text_sha256("same prompt")},
            task_input_hashes={"task-1": "sha256:task-input"},
            run_compatibility_fingerprints={"B1": "sha256:run-contract"},
        )


def test_resume_invalid_attempt_count_does_not_mix_rows_and_pairs(
    tmp_path: Path,
) -> None:
    resume_runner = _load_resume_runner()
    prompt_hash = resume_runner.text_sha256("same prompt")
    base = {
        "group": "B1",
        "provider_spec": dict(resume_runner.GROUP_SPECS["B1"]),
        "routing_trace": {
            "applied_model": "model-a",
            "fallback_model": "model-a",
        },
        "task_id": "task-1",
        "prompt_sha256": prompt_hash,
        "task_input_sha256": "sha256:task-input",
        "run_compatibility_fingerprint": "sha256:run-contract",
        "error": None,
        "final_text": "accepted answer",
        "llm_request_count": 1,
        "usage": {
            "provider": "openrouter",
            "model": "model-a",
            "requested_provider": "openrouter",
            "requested_model": "model-a",
            "input_tokens": 3,
            "output_tokens": 1,
            "billed_cost": 0.01,
            "cost_source": "provider_billed",
            "provider_usage": _openrouter_exact_evidence(
                0.01,
                "complete-duplicate",
            ),
        },
        "quality_total": 80.0,
        "judge": _complete_legacy_judge("complete-duplicate-judge"),
    }
    rows = [resume_runner.seal_result_row({**base, "copy": copy_index}) for copy_index in (1, 2)]
    path = tmp_path / "complete-duplicates.jsonl"
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    _, audit = resume_runner.load_resume_group_task_states(
        resume_paths=[path],
        selected_keys={("B1", "task-1")},
        prompt_hashes={"task-1": prompt_hash},
        task_input_hashes={"task-1": "sha256:task-input"},
        run_compatibility_fingerprints={"B1": "sha256:run-contract"},
    )

    assert audit["matching_attempt_count"] == 2
    assert audit["strict_valid_pair_count"] == 1
    assert audit["strict_invalid_attempt_count"] == 0


@pytest.mark.parametrize("group", ["B2", "G1"])
def test_resume_requires_ensemble_completion_proof_for_routing_groups(
    group: str,
) -> None:
    resume_runner = _load_resume_runner()
    prompt_hash = resume_runner.text_sha256("same prompt")
    row = resume_runner.seal_result_row(
        {
            "group": group,
            "task_id": "task-1",
            "prompt_sha256": prompt_hash,
            "task_input_sha256": "sha256:task-input",
            "run_compatibility_fingerprint": "sha256:run-contract",
            "error": None,
            "final_text": "answer without an ensemble proof",
            "quality_total": 80.0,
            "judge": _complete_legacy_judge("missing-ensemble-judge"),
        }
    )

    state = resume_runner.resume_row_completion_state(
        row,
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
    )

    assert state["action"] == "regenerate"
    assert "missing_ensemble_trace" in state["generation_reasons"]


@pytest.mark.parametrize("group", ["B0", "B1", "B4"])
def test_resume_does_not_apply_ensemble_proof_to_single_agent_loop_calls(
    group: str,
) -> None:
    prompt_hash = resume_runner.text_sha256("same prompt")
    spec = dict(resume_runner.GROUP_SPECS[group])
    expected_model = str(spec.get("model") or "routed-model")
    row = {
        "group": group,
        "provider_spec": spec,
        "routing_trace": {
            "applied_model": expected_model,
            "fallback_model": expected_model,
        },
        "task_id": "task-1",
        "prompt_sha256": prompt_hash,
        "task_input_sha256": "sha256:task-input",
        "run_compatibility_fingerprint": "sha256:run-contract",
        "error": "openrouter_non_byok_verification_failed",
        "final_text": "accepted answer",
        "llm_request_count": 1,
        "usage": {
            "provider": "openrouter",
            "model": expected_model,
            "requested_provider": "openrouter",
            "requested_model": expected_model,
            "input_tokens": 3,
            "output_tokens": 1,
            "billed_cost": 0.01,
            "cost_source": "provider_billed",
            "provider_usage": _openrouter_exact_evidence(
                0.01,
                f"single-agent-loop-{group}",
            ),
        },
        "ensemble_trace": {
            "mode": "agent_loop",
            "agent_llm_call_count": 1,
            "llm_request_count": 1,
            "physical_request_count": 1,
            "calls": [
                {
                    "agent_call_index": 1,
                    "request_outcome": "llm_response",
                    "trace_missing": True,
                }
            ],
        },
        "quality_total": 80.0,
        "judge": _complete_legacy_judge(f"single-agent-loop-judge-{group}"),
        "openrouter_non_byok_audit": {
            "pass": False,
            "request_count": 2,
            "exact_request_count": 1,
            "unverified_or_byok_request_count": 1,
        },
    }

    assert resume_runner.ensemble_generation_completion_reasons(row) == []
    state = resume_runner.resume_row_completion_state(
        resume_runner.seal_result_row(row),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
    )

    assert state["generation_valid"] is True
    assert state["generation_reasons"] == []
    assert state["action"] == "metadata_only"


def test_resume_binds_final_aggregator_output_to_agent_text_tail() -> None:
    resume_runner = _load_resume_runner()
    final_answer = "authoritative final answer"
    selection_plan = {
        "strategy": "router_dynamic",
        "selection_mode": "router_dynamic",
        "profile": "router_dynamic/c3",
        "proposer_models": ["p1", "p2", "p3", "p4"],
        "proposer_sample_count": 4,
        "selected_P": [
            "openrouter:p1",
            "openrouter:p2",
            "openrouter:p3",
            "openrouter:p4",
        ],
        "aggregator_model": "agg",
        "selected_A": "openrouter:agg",
    }
    final_trace = {
        "agent_call_index": 1,
        "request_outcome": "llm_response",
        "selection_strategy": "router_dynamic",
        "selection_plan": selection_plan,
        "successful_proposers": 3,
        "total_candidates": 4,
        "fallback_used": False,
        "candidates": [
            {
                "provider": "openrouter",
                "requested_provider": "openrouter",
                "model": model,
                "requested_model": model,
                "ok": index < 3,
                "request_started": True,
                "physical_request_count": 1,
                "usage_reported": index < 3,
                "stop_reason": "stop" if index < 3 else "",
                "content": {
                    "text": f"candidate-{index}" if index < 3 else "",
                    "chars": len(f"candidate-{index}") if index < 3 else 0,
                    "truncated": False,
                },
                **({} if index < 3 else {"error": "failed", "error_code": "test_failure"}),
            }
            for index, model in enumerate(selection_plan["proposer_models"])
        ],
        "final_request_role": "aggregator",
        "final_request": {
            "role": "aggregator",
            "request_started": True,
            "execution": {
                "provider": "openrouter",
                "requested_provider": "openrouter",
                "requested_model": "agg",
            },
            "usage": {
                "stop_reason": "stop",
                "provider": "openrouter",
                "model": "agg",
                "requested_provider": "openrouter",
                "requested_model": "agg",
            },
            "output": {
                "text": final_answer,
                "chars": len(final_answer),
                "truncated": False,
            },
        },
    }
    row = {
        "group": "G1",
        "provider_spec": dict(resume_runner.GROUP_SPECS["G1"]),
        "routing_trace": {"selection_plan": selection_plan},
        "final_text": f"earlier tool-loop narration\n{final_answer}",
        "ensemble_trace": {"calls": [final_trace], "agent_llm_call_count": 1},
    }

    reasons = resume_runner.ensemble_generation_completion_reasons(row)

    assert "aggregator_output_mismatch" not in reasons
    assert reasons == []

    row["ensemble_trace"]["calls"][0]["final_request"]["output"] = {
        "text": final_answer[:8],
        "chars": len(final_answer),
        "truncated": True,
    }
    assert resume_runner.ensemble_generation_completion_reasons(row) == []


def test_resume_verifies_b2_runtime_lineup_against_current_contract() -> None:
    resume_runner = _load_resume_runner()
    experiment = json.loads(
        resume_runner.DEFAULT_B2_EXPERIMENT_CONFIG_PATH.read_text(encoding="utf-8")
    )
    ensemble = experiment["ensemble"]
    expected_models = [member["model"] for member in ensemble["proposers"]]
    selection_plan = {
        "strategy": experiment["routing"]["selection_mode"],
        "selection_mode": experiment["routing"]["selection_mode"],
        "profile": ensemble["profile_name"],
        "proposer_models": expected_models,
        "proposer_sample_count": len(expected_models),
        "selected_P": [
            f"{member['provider']}:{member['model']}" for member in ensemble["proposers"]
        ],
        "aggregator_model": ensemble["aggregator"]["model"],
        "selected_A": (f"{ensemble['aggregator']['provider']}:{ensemble['aggregator']['model']}"),
    }
    final_answer = "answer"
    final_trace = {
        "agent_call_index": 1,
        "request_outcome": "llm_response",
        "selection_strategy": experiment["routing"]["selection_mode"],
        "selection_plan": selection_plan,
        "successful_proposers": ensemble["min_successful_proposers"],
        "total_candidates": len(expected_models),
        "fallback_used": False,
        "candidates": [
            {
                "provider": member["provider"],
                "requested_provider": member["provider"],
                "model": member["model"],
                "requested_model": member["model"],
                "ok": index < ensemble["min_successful_proposers"],
                "request_started": True,
                "physical_request_count": 1,
                "usage_reported": index < ensemble["min_successful_proposers"],
                "stop_reason": ("stop" if index < ensemble["min_successful_proposers"] else ""),
                "content": {
                    "text": (
                        f"candidate-{index}" if index < ensemble["min_successful_proposers"] else ""
                    ),
                    "chars": (
                        len(f"candidate-{index}")
                        if index < ensemble["min_successful_proposers"]
                        else 0
                    ),
                    "truncated": False,
                },
                **(
                    {}
                    if index < ensemble["min_successful_proposers"]
                    else {"error": "failed", "error_code": "test_failure"}
                ),
            }
            for index, member in enumerate(ensemble["proposers"])
        ],
        "final_request_role": "aggregator",
        "final_request": {
            "role": "aggregator",
            "request_started": True,
            "execution": {
                "provider": ensemble["aggregator"]["provider"],
                "requested_provider": ensemble["aggregator"]["provider"],
                "requested_model": ensemble["aggregator"]["model"],
            },
            "usage": {
                "stop_reason": "stop",
                "provider": ensemble["aggregator"]["provider"],
                "model": ensemble["aggregator"]["model"],
                "requested_provider": ensemble["aggregator"]["provider"],
                "requested_model": ensemble["aggregator"]["model"],
            },
            "output": {
                "text": final_answer,
                "chars": len(final_answer),
                "truncated": False,
            },
        },
    }
    row = {
        "group": "B2",
        "provider_spec": dict(resume_runner.GROUP_SPECS["B2"]),
        "routing_trace": {"selection_plan": selection_plan},
        "final_text": final_answer,
        "ensemble_trace": {"calls": [final_trace], "agent_llm_call_count": 1},
    }
    contract = {"experiment_config": experiment}

    assert (
        resume_runner.ensemble_generation_completion_reasons(
            row,
            expected_run_compatibility_contract=contract,
        )
        == []
    )

    metadata_missing = json.loads(json.dumps(row))
    successful_candidate = metadata_missing["ensemble_trace"]["calls"][0]["candidates"][0]
    successful_candidate.pop("provider")
    successful_candidate.pop("model")
    successful_candidate["usage_reported"] = False
    successful_candidate["stop_reason"] = ""
    final_usage = metadata_missing["ensemble_trace"]["calls"][0]["final_request"]["usage"]
    final_usage.pop("provider")
    final_usage.pop("model")
    final_usage.pop("stop_reason")
    metadata_reasons = resume_runner.ensemble_generation_completion_reasons(
        metadata_missing,
        expected_run_compatibility_contract=contract,
    )
    assert "missing_actual_proposer_identity" in metadata_reasons
    assert "missing_actual_aggregator_model" in metadata_reasons
    assert "missing_actual_aggregator_provider" in metadata_reasons
    assert "missing_proposer_usage_metadata" in metadata_reasons
    assert "missing_proposer_stop_reason" in metadata_reasons
    assert "missing_aggregator_stop_reason" in metadata_reasons
    assert "invalid_successful_proposer_evidence" not in metadata_reasons
    assert "successful_proposer_count_mismatch" not in metadata_reasons
    assert "insufficient_actual_proposer_quorum" not in metadata_reasons
    assert not any(reason.startswith("wrong_actual_") for reason in metadata_reasons)
    assert not any(reason.startswith("wrong_b2_actual_") for reason in metadata_reasons)

    metadata_result = resume_runner.RunResult(
        final_text=final_answer,
        done=resume_runner.DoneEvent(
            ensemble_trace=metadata_missing["ensemble_trace"],
        ),
    )
    assert (
        resume_runner.ensemble_generation_retry_reason(
            metadata_result,
            expected_selection_mode=experiment["routing"]["selection_mode"],
            expected_selection_plan=selection_plan,
        )
        == ""
    )

    prompt_hash = resume_runner.text_sha256("same prompt")
    metadata_missing.update(
        {
            "task_id": "task-1",
            "prompt_sha256": prompt_hash,
            "task_input_sha256": "sha256:task-input",
            "run_compatibility_fingerprint": "sha256:run-contract",
            "error": None,
        }
    )
    metadata_state = resume_runner.resume_row_completion_state(
        resume_runner.seal_result_row(metadata_missing),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
        expected_run_compatibility_contract=contract,
        judge_required=False,
    )
    assert metadata_state["generation_valid"] is True, metadata_state
    assert metadata_state["action"] == "metadata_only"
    assert (
        "missing_proposer_usage_metadata_backfill_required"
        in metadata_state["cost_metadata_reasons"]
    )

    repairable = json.loads(json.dumps(metadata_missing))
    generation_receipts = []
    for index, member in enumerate(ensemble["proposers"][:3]):
        generation_receipts.append(
            {
                "role": "proposer",
                "provider": member["provider"],
                "model": member["model"],
                "requested_provider": member["provider"],
                "requested_model": member["model"],
                "input_tokens": 3,
                "output_tokens": 1,
                "billed_cost": 0.01,
                "cost_source": "provider_billed",
                "provider_usage": _openrouter_exact_evidence(
                    0.01,
                    f"metadata-repair-proposer-{index}",
                ),
            }
        )
    generation_receipts.append(
        {
            "role": "aggregator",
            "provider": ensemble["aggregator"]["provider"],
            "model": ensemble["aggregator"]["model"],
            "requested_provider": ensemble["aggregator"]["provider"],
            "requested_model": ensemble["aggregator"]["model"],
            "input_tokens": 5,
            "output_tokens": 2,
            "billed_cost": 0.02,
            "cost_source": "provider_billed",
            "provider_usage": _openrouter_exact_evidence(
                0.02,
                "metadata-repair-aggregator",
            ),
        }
    )
    repairable["llm_request_count"] = len(generation_receipts)
    repairable["usage"] = {
        "input_tokens": 14,
        "output_tokens": 5,
        "billed_cost": 0.05,
        "cost_source": "provider_billed",
        "model_usage_breakdown": generation_receipts,
    }
    original_final_text = repairable["final_text"]
    original_attempts = json.loads(
        json.dumps((repairable.get("execution") or {}).get("generation_attempts"))
    )
    assert resume_runner.repair_ensemble_trace_metadata(repairable) is True
    repaired_candidate = repairable["ensemble_trace"]["calls"][0]["candidates"][0]
    repaired_final_request = repairable["ensemble_trace"]["calls"][0]["final_request"]
    assert repaired_candidate["provider"] == ensemble["proposers"][0]["provider"]
    assert repaired_candidate["model"] == ensemble["proposers"][0]["model"]
    assert repaired_candidate["stop_reason"] == ""
    assert repaired_candidate["metadata_repair"]["stop_reason"]["status"] == ("unavailable")
    assert repaired_final_request["usage"]["provider"] == (ensemble["aggregator"]["provider"])
    assert repaired_final_request["usage"]["model"] == ensemble["aggregator"]["model"]
    assert repaired_final_request["metadata_repair"]["stop_reason"]["status"] == ("unavailable")
    repaired_state = resume_runner.resume_row_completion_state(
        resume_runner.seal_result_row(repairable),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
        expected_run_compatibility_contract=contract,
        judge_required=False,
    )
    assert repaired_state["action"] == "complete", repaired_state
    assert repairable["final_text"] == original_final_text
    assert (repairable.get("execution") or {}).get("generation_attempts") == (original_attempts)

    aggregator_usage_missing = json.loads(json.dumps(row))
    aggregator_usage_missing["ensemble_trace"]["calls"][0]["final_request"].pop("usage")
    aggregator_metadata_reasons = resume_runner.ensemble_generation_completion_reasons(
        aggregator_usage_missing,
        expected_run_compatibility_contract=contract,
    )
    assert "missing_aggregator_usage_metadata" in aggregator_metadata_reasons
    assert "missing_aggregator_stop_reason" in aggregator_metadata_reasons
    assert "aggregator_request_incomplete" not in aggregator_metadata_reasons

    missing_candidates = json.loads(json.dumps(row))
    missing_candidates["ensemble_trace"]["calls"][0].pop("candidates")
    missing_candidate_reasons = resume_runner.ensemble_generation_completion_reasons(
        missing_candidates,
        expected_run_compatibility_contract=contract,
    )
    assert "missing_actual_proposer_candidates" in missing_candidate_reasons
    assert (
        resume_runner.ensemble_metadata_only_reason("missing_actual_proposer_candidates") is False
    )

    wrong = json.loads(json.dumps(row))
    wrong_plan = wrong["ensemble_trace"]["calls"][0]["selection_plan"]
    wrong_plan["proposer_models"] = expected_models[:3]
    wrong["routing_trace"]["selection_plan"]["proposer_models"] = expected_models[:3]
    wrong["ensemble_trace"]["calls"][0]["total_candidates"] = 3
    wrong["ensemble_trace"]["calls"][0]["successful_proposers"] = 2
    reasons = resume_runner.ensemble_generation_completion_reasons(
        wrong,
        expected_run_compatibility_contract=contract,
    )

    assert "wrong_b2_proposer_count" in reasons
    assert "insufficient_b2_configured_quorum" in reasons
    assert "wrong_b2_proposer_lineup" in reasons

    missing_plan = json.loads(json.dumps(row))
    missing_plan["ensemble_trace"]["calls"][0].pop("selection_plan")
    missing_plan_reasons = resume_runner.ensemble_generation_completion_reasons(
        missing_plan,
        expected_run_compatibility_contract=contract,
    )
    assert "missing_executed_selection_plan" in missing_plan_reasons

    wrong_actual = json.loads(json.dumps(row))
    wrong_actual["ensemble_trace"]["calls"][0]["final_request"]["usage"]["model"] = (
        "wrong/aggregator"
    )
    wrong_actual_reasons = resume_runner.ensemble_generation_completion_reasons(
        wrong_actual,
        expected_run_compatibility_contract=contract,
    )
    assert "wrong_b2_actual_aggregator_model" in wrong_actual_reasons

    wrong_success = json.loads(json.dumps(row))
    wrong_success["ensemble_trace"]["calls"][0]["candidates"][0]["ok"] = False
    wrong_success_reasons = resume_runner.ensemble_generation_completion_reasons(
        wrong_success,
        expected_run_compatibility_contract=contract,
    )
    assert "successful_proposer_count_mismatch" in wrong_success_reasons
    assert "insufficient_actual_proposer_quorum" in wrong_success_reasons

    request_not_started = json.loads(json.dumps(row))
    request_not_started["ensemble_trace"]["calls"][0]["candidates"][0]["request_started"] = False
    request_not_started_reasons = resume_runner.ensemble_generation_completion_reasons(
        request_not_started,
        expected_run_compatibility_contract=contract,
    )
    assert "invalid_successful_proposer_evidence" in request_not_started_reasons
    assert "successful_proposer_count_mismatch" in request_not_started_reasons

    # The fourth failed proposer is legal under the 3/4 quorum and does not
    # need actual response identity; its requested slot is still audited.
    assert (
        resume_runner.ensemble_generation_completion_reasons(
            row,
            expected_run_compatibility_contract=contract,
        )
        == []
    )

    forged_success = json.loads(json.dumps(row))
    forged_candidate = forged_success["ensemble_trace"]["calls"][0]["candidates"][3]
    forged_candidate.update(
        {
            "ok": True,
            "error": "",
            "stop_reason": "stop",
            "content": {
                "text": "forged",
                "chars": len("forged"),
                "truncated": False,
            },
        }
    )
    forged_success["ensemble_trace"]["calls"][0]["successful_proposers"] = 4
    forged_reasons = resume_runner.ensemble_generation_completion_reasons(
        forged_success,
        expected_run_compatibility_contract=contract,
    )
    assert "missing_proposer_usage_metadata" in forged_reasons
    assert "invalid_successful_proposer_evidence" not in forged_reasons
    assert "successful_proposer_count_mismatch" not in forged_reasons

    failed_receipt_wrong_identity = json.loads(json.dumps(row))
    failed_with_receipt = failed_receipt_wrong_identity["ensemble_trace"]["calls"][0]["candidates"][
        3
    ]
    failed_with_receipt.update(
        {
            "provider": "wrong-provider",
            "model": failed_with_receipt["requested_model"],
            "diagnostic_model_usage_breakdown": [
                {
                    "provider": "wrong-provider",
                    "model": failed_with_receipt["requested_model"],
                }
            ],
        }
    )
    failed_receipt_reasons = resume_runner.ensemble_generation_completion_reasons(
        failed_receipt_wrong_identity,
        expected_run_compatibility_contract=contract,
    )
    assert "wrong_actual_proposer_identity" in failed_receipt_reasons

    two_calls = json.loads(json.dumps(row))
    terminal_call = json.loads(json.dumps(two_calls["ensemble_trace"]["calls"][0]))
    terminal_call["agent_call_index"] = 2
    first_call = json.loads(json.dumps(terminal_call))
    first_call["agent_call_index"] = 1
    intermediate_text = "intermediate answer segment\n"
    first_call["final_request"]["output"] = {
        "text": intermediate_text,
        "chars": len(intermediate_text),
        "truncated": False,
    }
    two_calls["final_text"] = intermediate_text + row["final_text"]
    two_calls["ensemble_trace"]["calls"] = [
        first_call,
        terminal_call,
    ]
    two_calls["ensemble_trace"]["agent_llm_call_count"] = 2
    assert (
        resume_runner.ensemble_generation_completion_reasons(
            two_calls,
            expected_run_compatibility_contract=contract,
        )
        == []
    )

    tampered_intermediate = json.loads(json.dumps(two_calls))
    tampered_intermediate["ensemble_trace"]["calls"][0]["final_request"]["output"][
        "text"
    ] = "x" * len(intermediate_text)
    assert "wrong_agent_call_output_binding" in (
        resume_runner.ensemble_generation_completion_reasons(
            tampered_intermediate,
            expected_run_compatibility_contract=contract,
        )
    )

    dropped_early = json.loads(json.dumps(two_calls))
    dropped_early["ensemble_trace"]["calls"] = [dropped_early["ensemble_trace"]["calls"][1]]
    dropped_reasons = resume_runner.ensemble_generation_completion_reasons(
        dropped_early,
        expected_run_compatibility_contract=contract,
    )
    assert "wrong_agent_llm_call_count" in dropped_reasons
    assert "invalid_agent_call_index_sequence" in dropped_reasons

    malformed_call = json.loads(json.dumps(row))
    malformed_call["ensemble_trace"]["calls"].insert(0, "not-a-call")
    malformed_reasons = resume_runner.ensemble_generation_completion_reasons(
        malformed_call,
        expected_run_compatibility_contract=contract,
    )
    assert "invalid_ensemble_call_trace" in malformed_reasons


def _terminal_policy_call(
    *,
    index: int,
    plan: dict[str, object],
    successful: int,
    fallback: bool,
    output: str,
) -> dict[str, object]:
    proposer_models = list(plan["proposer_models"])
    candidates = []
    for candidate_index, model in enumerate(proposer_models):
        ok = candidate_index < successful
        text = f"candidate-{candidate_index}" if ok else ""
        candidates.append(
            {
                "provider": "openrouter",
                "requested_provider": "openrouter",
                "model": model,
                "requested_model": model,
                "ok": ok,
                "request_started": True,
                "physical_request_count": 1,
                "usage_reported": ok,
                "stop_reason": "stop" if ok else "",
                "error": "" if ok else "test failure",
                "content": {
                    "text": text,
                    "chars": len(text),
                    "truncated": False,
                },
            }
        )
    role = "fallback_single" if fallback else "aggregator"
    model = proposer_models[0] if fallback else str(plan["aggregator_model"])
    return {
        "agent_call_index": index,
        "request_outcome": "llm_response",
        "selection_strategy": plan["selection_mode"],
        "selection_plan": deepcopy(plan),
        "successful_proposers": successful,
        "total_candidates": len(proposer_models),
        "fallback_used": fallback,
        "fallback_reason": "sub-quorum" if fallback else "",
        "candidates": candidates,
        "final_request_role": role,
        "final_request": {
            "role": role,
            "request_started": True,
            "error": "",
            "execution": {
                "role": role,
                "requested_provider": "openrouter",
                "provider": "openrouter",
                "actual_provider": "openrouter",
                "requested_model": model,
                "model": model,
                "actual_model": model,
            },
            "usage": {
                "stop_reason": "stop",
                "provider": "openrouter",
                "model": model,
                "requested_provider": "openrouter",
                "requested_model": model,
            },
            "output": {
                "text": output,
                "chars": len(output),
                "truncated": False,
            },
        },
    }


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
def test_terminal_policy_allows_only_empty_nonterminal_fallback(module) -> None:
    plan = {
        "strategy": "static_openrouter_b5",
        "selection_mode": "static_openrouter_b5",
        "profile": "test",
        "proposer_models": ["p1", "p2", "p3", "p4"],
        "proposer_sample_count": 4,
        "selected_P": [
            "openrouter:p1",
            "openrouter:p2",
            "openrouter:p3",
            "openrouter:p4",
        ],
        "aggregator_model": "agg",
        "selected_A": "openrouter:agg",
    }
    fallback = _terminal_policy_call(
        index=1,
        plan=plan,
        successful=2,
        fallback=True,
        output="",
    )
    terminal = _terminal_policy_call(
        index=2,
        plan=plan,
        successful=4,
        fallback=False,
        output="final answer",
    )

    def retry_reason(calls: list[dict[str, object]], final_text: str) -> str:
        return module.ensemble_generation_retry_reason(
            module.RunResult(
                final_text=final_text,
                done=module.DoneEvent(
                    ensemble_trace={
                        "mode": "agent_loop",
                        "agent_llm_call_count": len(calls),
                        "untraced_agent_llm_call_count": 0,
                        "calls": calls,
                    }
                ),
            ),
            expected_selection_mode="static_openrouter_b5",
            expected_selection_plan=plan,
        )

    assert retry_reason([deepcopy(fallback), deepcopy(terminal)], "final answer") == ""

    visible = deepcopy(fallback)
    visible["final_request"]["output"] = {
        "text": "unsafe text",
        "chars": len("unsafe text"),
        "truncated": False,
    }
    assert (
        retry_reason([visible, deepcopy(terminal)], "unsafe textfinal answer")
        == "intermediate_fallback_visible_output"
    )

    wrong_model = deepcopy(fallback)
    wrong_model["final_request"]["usage"]["model"] = "outside/model"
    wrong_model["final_request"]["usage"]["requested_model"] = "outside/model"
    assert (
        retry_reason([wrong_model, deepcopy(terminal)], "final answer")
        == "wrong_intermediate_fallback_model"
    )

    conflicting_execution = deepcopy(fallback)
    conflicting_execution["final_request"]["execution"]["actual_model"] = (
        "outside/model"
    )
    assert (
        retry_reason([conflicting_execution, deepcopy(terminal)], "final answer")
        == "wrong_intermediate_fallback_model"
    )

    boolean_chars = deepcopy(fallback)
    boolean_chars["final_request"]["output"]["chars"] = False
    assert module.admissible_empty_nonterminal_fallback_reasons(
        boolean_chars,
        expected_selection_plan=plan,
    ) == ["intermediate_fallback_visible_output"]

    assert retry_reason([deepcopy(fallback)], "") == (
        "aggregator_fallback_used_or_unknown"
    )


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
def test_agent_ensemble_root_summary_uses_terminal_call(module) -> None:
    plan = {
        "selection_mode": "static_openrouter_b5",
        "proposer_models": ["p1", "p2", "p3", "p4"],
        "selected_P": [
            "openrouter:p1",
            "openrouter:p2",
            "openrouter:p3",
            "openrouter:p4",
        ],
        "aggregator_model": "agg",
        "selected_A": "openrouter:agg",
    }
    recorder = module.BenchmarkTurnCallRecorder()
    for call in (
        _terminal_policy_call(
            index=1,
            plan=plan,
            successful=2,
            fallback=True,
            output="",
        ),
        _terminal_policy_call(
            index=2,
            plan=plan,
            successful=4,
            fallback=False,
            output="final answer",
        ),
    ):
        recorder.write(
            "llm_response",
            {
                "iteration": call["agent_call_index"],
                "attempt": 1,
                "ensemble_trace": call,
            },
        )

    trace = module.aggregate_agent_ensemble_trace(recorder.records)

    assert trace["fallback_used"] is False
    assert trace["final_request_role"] == "aggregator"
    assert trace["successful_proposers"] == 4
    assert trace["total_candidates"] == 4
    assert trace["terminal_call_index"] == 2
    assert trace["any_intermediate_fallback"] is True
    assert trace["intermediate_fallback_call_indexes"] == [1]


def test_resume_reclassifies_one_legacy_terminal_attempt_without_hiding_spend() -> None:
    module = _load_resume_runner()
    plan = {
        "strategy": "static_openrouter_b5",
        "selection_mode": "static_openrouter_b5",
        "profile": "test",
        "proposer_models": ["p1", "p2", "p3", "p4"],
        "proposer_sample_count": 4,
        "selected_P": [
            "openrouter:p1",
            "openrouter:p2",
            "openrouter:p3",
            "openrouter:p4",
        ],
        "aggregator_model": "agg",
        "selected_A": "openrouter:agg",
    }
    calls = [
        _terminal_policy_call(
            index=1,
            plan=plan,
            successful=2,
            fallback=True,
            output="",
        ),
        _terminal_policy_call(
            index=2,
            plan=plan,
            successful=4,
            fallback=False,
            output="final answer",
        ),
    ]

    def usage(response_id: str, cost: float) -> dict[str, object]:
        return {
            "provider": "openrouter",
            "model": "agg",
            "requested_provider": "openrouter",
            "requested_model": "agg",
            "input_tokens": 10,
            "output_tokens": 5,
            "billed_cost": cost,
            "cost_source": "provider_billed",
            "provider_usage": _openrouter_exact_evidence(cost, response_id),
        }

    selected_usage = usage("selected-generation", 0.1)
    attempts = []
    for index, (answer, attempt_usage, error) in enumerate(
        (
            (
                "final answer",
                selected_usage,
                module.LEGACY_TERMINAL_POLICY_ERROR,
            ),
            ("later answer 2", usage("extra-generation-2", 0.2), "other failure"),
            ("later answer 3", usage("extra-generation-3", 0.3), "other failure"),
        ),
        start=1,
    ):
        attempts.append(
            {
                "attempt_id": f"{index:032x}",
                "attempt_kind": "generation",
                "attempt": index,
                "started_at": float(index),
                "completed_at": float(index + 1),
                "retry_reason": error,
                "run": {
                    "error": error,
                    "final_text_sha256": module.text_sha256(answer),
                    "latency_ms": 100 * index,
                    "stream_tool_call_count": index,
                    "server_tool_call_count": 0,
                    "server_tool_use": {},
                    "total_tool_call_count": index,
                    "trajectory_steps": index + 1,
                    "llm_request_count": 1,
                    "usage_unknown_count": 0,
                    "usage": attempt_usage,
                },
            }
        )
    row = {
        "group": "B2",
        "provider_spec": dict(module.GROUP_SPECS["B2"]),
        "routing_trace": {"selection_plan": plan},
        "final_text": "final answer",
        "final_text_sha256": module.text_sha256("final answer"),
        "final_text_chars": len("final answer"),
        "error": module.LEGACY_TERMINAL_POLICY_ERROR,
        "selected_generation_succeeded": False,
        "usage": selected_usage,
        "ensemble_trace": {
            "mode": "agent_loop",
            "agent_llm_call_count": 2,
            "untraced_agent_llm_call_count": 0,
            "calls": calls,
        },
        "execution": {
            "run_error": module.LEGACY_TERMINAL_POLICY_ERROR,
            "selected_generation_attempt": 0,
            "generation_attempts": attempts,
        },
        "completion_status": {
            "generation_accepted": False,
            "incomplete_reasons": [module.LEGACY_TERMINAL_POLICY_ERROR],
        },
        "generation_attempt_count": 3,
        "actual_spend_metrics": {"generation_attempt_count": 3},
    }

    assert module.apply_terminal_generation_reclassification(
        row,
        expected_run_compatibility_contract=None,
    )
    assert row["selected_generation_succeeded"] is True
    assert row["execution"]["selected_generation_attempt"] == 1
    assert row["execution"]["generation_attempts"][0]["run"]["error"] == (
        module.LEGACY_TERMINAL_POLICY_ERROR
    )
    assert row["selected_attempt_billed_cost_usd"] == pytest.approx(0.1)
    accounting = module.row_cost_accounting(row)
    assert accounting["selected_generation_attempt"]["recorded_cost_usd"] == (
        pytest.approx(0.1)
    )
    assert accounting["actual_generation_spend"]["recorded_cost_usd"] == (
        pytest.approx(0.6)
    )
    prompt_hash = module.text_sha256("same prompt")
    row.update(
        {
            "task_id": "task-1",
            "prompt_sha256": prompt_hash,
            "task_input_sha256": "sha256:task-input",
            "run_compatibility_fingerprint": "sha256:run-contract",
        }
    )
    state = module.resume_row_completion_state(
        module.seal_result_row(row),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
    )
    assert state["generation_valid"] is True, state
    assert state["action"] == "judge_only"


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
def test_g1_only_uses_the_same_shared_experiment_profile_as_b2_g1(module) -> None:
    def aligned(groups: str):
        args = module.build_parser().parse_args(
            [
                "--input",
                "tasks.jsonl",
                "--groups",
                groups,
                "--concurrency",
                "9",
                "--agent-max-iterations",
                "1",
            ]
        )
        record = module.apply_b2_g12_argument_alignment(
            args,
            module.parse_groups(groups),
        )
        return args, record

    g1_args, g1_record = aligned("G1")
    joint_args, joint_record = aligned("B2,G1")

    assert g1_record is not None
    assert joint_record is not None
    assert g1_record["effective_args"] == joint_record["effective_args"]
    assert g1_args.agent_max_iterations == joint_args.agent_max_iterations == 12
    assert g1_args.concurrency == joint_args.concurrency == 2
    assert "global_experiment_profile" in g1_args._benchmark_alignments
    assert "B2" not in g1_args._benchmark_alignments
    assert g1_args._effective_argument_sources["agent_max_iterations"] == ("experiment_config")


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
def test_g1_registry_contract_is_manifested_and_fingerprinted(
    module,
    tmp_path: Path,
) -> None:
    args = module.build_parser().parse_args(["--input", "tasks.jsonl", "--groups", "G1"])
    module.apply_b2_g12_argument_alignment(args, ["G1"])
    experiment = args._draco_experiment_config_bundle.config
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "fake",
        },
        llm_ensemble={
            "ranking_user_profile_generation_enabled": True,
            "ranking_user_profile_enabled": True,
        },
        sandbox={"sandbox": True, "security_grading": True},
    )
    args._formal_runtime_freeze = module.enforce_formal_draco_runtime_config(
        config,
        experiment,
        ["G1"],
    )
    assert config.llm_ensemble.ranking_user_profile_generation_enabled is False
    assert config.llm_ensemble.ranking_user_profile_enabled is False
    assert config.sandbox.sandbox is False
    assert config.sandbox.security_grading is False
    args._g1_registry_contract = module.validate_g1_registry_contract(
        experiment,
        config,
    )
    args._source_provenance = {
        "git_head": "a" * 40,
        "source_tree_sha256": "b" * 64,
    }
    policy = module.benchmark_tool_policy(args)
    compatibility = module.build_run_compatibility(
        args=args,
        config=config,
        groups=["G1"],
        group_tool_policies=module.benchmark_tool_policies_for_groups(
            policy,
            ["G1"],
            args=args,
        ),
        generation_policy=module.generation_thinking_policy(args),
    )
    args._run_compatibility = compatibility
    contract = compatibility["contracts"]["G1"]
    assert contract["global_experiment_profile"]["g1_routing"] == (
        experiment.g1_routing.model_dump(mode="json")
    )
    assert contract["g1_registry_contract"]["expected_candidate_count"] == 20
    assert contract["g1_registry_contract"]["runtime_pins_match"] is True
    assert contract["formal_runtime_freeze"] == {
        "source": "experiment_config",
        "sandbox_enabled": False,
        "sandbox_security_grading_enabled": False,
        "g1_user_profile_generation_enabled": False,
        "g1_user_profile_enabled": False,
    }
    assert (
        contract["gateway_execution"]["llm_ensemble"]["ranking_user_profile_generation_enabled"]
        is False
    )
    assert contract["gateway_execution"]["llm_ensemble"]["ranking_user_profile_enabled"] is False
    assert contract["gateway_execution"]["sandbox"]["sandbox"] is False
    assert contract["gateway_execution"]["sandbox"]["security_grading"] is False

    manifest_path = tmp_path / f"{module.__name__}.manifest.json"
    module.write_manifest(
        manifest_path,
        args=args,
        stamp="test",
        status="running",
        started_at=1.0,
        tasks=[{"id": "task-1"}],
        groups=["G1"],
        artifacts={},
        tool_policy=policy,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["g1_registry_contract"]["profile_id"] == (
        "draco_g1_formal_openrouter_20_20260725"
    )
    assert manifest["g1_registry_contract"]["expected_candidate_count"] == 20
    assert manifest["formal_runtime_freeze"] == contract["formal_runtime_freeze"]


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
async def test_g1_dry_build_records_exact_candidate_allowlist(module) -> None:
    experiment = module.load_draco_experiment_config(
        module.DEFAULT_B2_EXPERIMENT_CONFIG_PATH
    ).config
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "fake",
        }
    )
    result = await module.build_experiment_provider(
        config=config,
        inherited=ProviderConfig(
            provider="openrouter",
            model="deepseek/deepseek-v4-pro",
            api_key="fake",
        ),
        group="G1",
        prompt="dry prompt",
        dry_run=True,
        enable_proposer_tools=False,
        ensemble_proposer_timeout=None,
        ensemble_aggregator_timeout=None,
        experiment_config=experiment,
        generation_policy={},
    )

    plan = result.routing_trace["selection_plan"]
    assert plan["candidate_pool_size"] == 20
    assert len(plan["candidate_pool"]) == 20
    assert plan["candidate_allowlist"]["candidate_count"] == 20
    assert plan["user_profile_enabled"] is False
    assert len(plan["candidate_allowlist"]["expected_identities"]) == 20
    assert plan["candidate_allowlist"]["filtered_registry_snapshot_version"].startswith(
        "curated-openrouter-step2-2026-07-24.2+"
    )
    assert len(plan["selected_P"]) == 2
    assert plan["N_min"] == 1
    assert plan["N_max"] == 2
    assert plan["proposer_count"] == 2
    assert plan["ranking_config_hash"] == (experiment.g1_routing.expected_ranking_config_sha256)
    assert plan["selected_A"] in plan["candidate_allowlist"]["expected_identities"]
    assert set(plan["selected_P"]) <= set(plan["candidate_allowlist"]["expected_identities"])


def test_g1_dry_cli_main_exits_zero_with_frozen_registry_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = tmp_path / "tasks.jsonl"
    input_path.write_text(
        json.dumps({"id": "task-1", "prompt": "dry G1 prompt"}) + "\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    experiment_path = ROOT / "configs" / "benchmarks" / "draco_b2_g12.json"
    experiment = runner.load_draco_experiment_config(experiment_path).config
    assert experiment.g1_routing is not None
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "fake",
            "provider_routing": experiment.g1_routing.expected_routes,
        }
    )
    monkeypatch.setattr(runner.GatewayConfig, "load", lambda _path: config)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT_PATH),
            "--input",
            str(input_path),
            "--output-dir",
            str(output_dir),
            "--experiment-config",
            str(experiment_path),
            "--groups",
            "G1",
            "--max-tasks",
            "1",
            "--dry-run",
            "--require-openrouter-non-byok",
            "--experiment-config-set",
            "benchmark_input.enforce_reference_input=false",
            "--experiment-config-set",
            "runner.concurrency=1",
            "--experiment-config-set",
            "judge.concurrency=1",
            "--experiment-config-set",
            "generation.max_attempts=1",
        ],
    )

    assert runner.main() == 0

    manifests = list(output_dir.glob("draco_run_*.manifest.json"))
    result_paths = list(output_dir.glob("draco_ensemble_*.jsonl"))
    assert len(manifests) == 1
    assert len(result_paths) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    row = json.loads(result_paths[0].read_text(encoding="utf-8").splitlines()[0])
    plan = row["ensemble_trace"]["calls"][0]["selection_plan"]
    assert manifest["status"] == "complete"
    assert (
        manifest["run_compatibility"]["contracts"]["G1"]["cost_policy"][
            "require_openrouter_non_byok"
        ]
        is True
    )
    assert "openrouter_non_byok_audit" not in row
    assert len(plan["candidate_pool"]) == 20
    assert (
        plan["registry_snapshot_version"]
        == (plan["candidate_allowlist"]["filtered_registry_snapshot_version"])
    )
    registry_hash = plan["registry_snapshot_hash"]
    assert len(registry_hash) == 64
    assert all(char in "0123456789abcdef" for char in registry_hash)


def test_g1_registry_contract_rejects_runtime_pin_drift() -> None:
    experiment = _experiment_config()
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "fake",
            "provider_routing": {"x-ai/grok-4.5": "wrong-provider"},
        }
    )

    with pytest.raises(ValueError, match="provider pin"):
        runner.validate_g1_registry_contract(experiment, config)


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
def test_g1_registry_contract_rejects_allowlist_trace_drift(module) -> None:
    experiment = _experiment_config()
    contract = experiment.g1_routing.model_dump(mode="json")
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "fake",
            "provider_routing": experiment.g1_routing.expected_routes,
        },
        llm_ensemble={"enabled": True, "selection_mode": "router_dynamic"},
    )
    inherited = ProviderConfig(
        provider="openrouter",
        model="deepseek/deepseek-v4-pro",
        api_key="fake",
        provider_routing=experiment.g1_routing.expected_routes,
    )
    provider = build_ensemble_provider_from_config(
        config=config,
        inherited_provider_config=inherited,
        fallback_provider=None,
        turn_metadata={"routed_tier": "c1", "routing_confidence": 0.9},
        ranking_inputs={"registry_allowlist": experiment.g1_routing.model_dump(mode="json")},
    )
    expected_identities = sorted(f"openrouter:{model}" for model in contract["expected_routes"])
    plan = json.loads(json.dumps(provider.selection_plan))
    trace = {"selection_plan": plan}

    assert module.g1_registry_contract_reasons(trace, contract) == []

    missing_allowlist = json.loads(json.dumps(trace))
    missing_allowlist["selection_plan"].pop("candidate_allowlist")
    assert "missing_g1_candidate_allowlist" in module.g1_registry_contract_reasons(
        missing_allowlist,
        contract,
    )

    wrong_version = json.loads(json.dumps(trace))
    wrong_version["selection_plan"]["candidate_allowlist"]["filtered_registry_snapshot_version"] = (
        "wrong-version"
    )
    assert (
        "wrong_g1_candidate_allowlist_filtered_registry_snapshot_version"
        in module.g1_registry_contract_reasons(wrong_version, contract)
    )

    wrong_count = json.loads(json.dumps(trace))
    wrong_count["selection_plan"]["candidate_pool_size"] = 19
    wrong_count["selection_plan"]["candidate_allowlist"]["candidate_count"] = 19
    wrong_count_reasons = module.g1_registry_contract_reasons(
        wrong_count,
        contract,
    )
    assert "wrong_g1_candidate_pool_size" in wrong_count_reasons
    assert "wrong_g1_candidate_allowlist_candidate_count" in wrong_count_reasons

    wrong_pool = json.loads(json.dumps(trace))
    wrong_pool["selection_plan"]["candidate_pool"][0]["identity"] = "openrouter:evil/model"
    assert "wrong_g1_candidate_pool" in module.g1_registry_contract_reasons(
        wrong_pool,
        contract,
    )

    wrong_registry_version = json.loads(json.dumps(trace))
    wrong_registry_version["selection_plan"]["registry_snapshot_version"] = "wrong-version"
    assert "wrong_g1_registry_snapshot_version" in module.g1_registry_contract_reasons(
        wrong_registry_version, contract
    )

    invalid_registry_hash = json.loads(json.dumps(trace))
    invalid_registry_hash["selection_plan"]["registry_snapshot_hash"] = "not-a-hash"
    assert "invalid_g1_registry_snapshot_hash" in module.g1_registry_contract_reasons(
        invalid_registry_hash, contract
    )

    escaped_route = json.loads(json.dumps(trace))
    escaped_route["selection_plan"]["selected_P"][0] = "openrouter:evil/model"
    assert "wrong_g1_selected_proposers" in module.g1_registry_contract_reasons(
        escaped_route,
        contract,
    )

    wrong_user_profile = json.loads(json.dumps(trace))
    wrong_user_profile["selection_plan"]["user_profile_enabled"] = True
    assert "wrong_g1_user_profile_enabled" in module.g1_registry_contract_reasons(
        wrong_user_profile,
        contract,
    )

    escaped_count = json.loads(json.dumps(trace))
    escaped_count["selection_plan"]["selected_P"] = expected_identities
    escaped_count["selection_plan"]["proposer_count"] = len(expected_identities)
    escaped_count["selection_plan"]["proposer_sample_count"] = len(expected_identities)
    escaped_count["selection_plan"]["N_max"] = len(expected_identities)
    escaped_reasons = module.g1_registry_contract_reasons(
        escaped_count,
        contract,
    )
    assert "wrong_g1_proposer_bounds" in escaped_reasons
    assert "wrong_g1_selected_proposer_count" in escaped_reasons

    ranking_drift = json.loads(json.dumps(trace))
    ranking_drift["selection_plan"]["ranking_parameters"]["proposer_count"]["high_risk"]["max"] = 20
    assert "wrong_g1_ranking_config_hash" in module.g1_registry_contract_reasons(
        ranking_drift, contract
    )

    ranking_trace_drift = json.loads(json.dumps(trace))
    ranking_trace_drift["selection_plan"]["ranking_config_hash"] = "0" * 64
    assert "wrong_g1_ranking_config_trace" in module.g1_registry_contract_reasons(
        ranking_trace_drift, contract
    )

    duplicate_proposer = json.loads(json.dumps(trace))
    duplicate_proposer["selection_plan"]["selected_P"][1] = duplicate_proposer["selection_plan"][
        "selected_P"
    ][0]
    assert "wrong_g1_selected_proposers" in module.g1_registry_contract_reasons(
        duplicate_proposer, contract
    )


def test_resume_reuses_g1_plan_and_analyzer_within_one_provider_lifecycle() -> None:
    module = _load_resume_runner()
    from opensquilla.provider.ranking_router import (
        TASK_ANALYZER_MODEL_ID,
        TASK_ANALYZER_PROVIDER_ID,
    )

    experiment = _experiment_config()
    contract = experiment.g1_routing.model_dump(mode="json")
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "fake",
            "provider_routing": experiment.g1_routing.expected_routes,
        },
        llm_ensemble={"enabled": True, "selection_mode": "router_dynamic"},
    )
    inherited = ProviderConfig(
        provider="openrouter",
        model="deepseek/deepseek-v4-pro",
        api_key="fake",
        provider_routing=experiment.g1_routing.expected_routes,
    )
    provider = build_ensemble_provider_from_config(
        config=config,
        inherited_provider_config=inherited,
        fallback_provider=None,
        turn_metadata={"routed_tier": "c1", "routing_confidence": 0.9},
        ranking_inputs={
            "registry_allowlist": experiment.g1_routing.model_dump(mode="json")
        },
    )
    plan = deepcopy(provider.selection_plan)
    final_text = "answer"
    selected_usage = {
        "provider": "openrouter",
        "model": plan["aggregator_model"],
        "requested_provider": "openrouter",
        "requested_model": plan["aggregator_model"],
        "input_tokens": 3,
        "output_tokens": 1,
    }
    analyzer = {
        "role": "task_analyzer",
        "provider": TASK_ANALYZER_PROVIDER_ID,
        "model": TASK_ANALYZER_MODEL_ID,
        "requested_provider": TASK_ANALYZER_PROVIDER_ID,
        "requested_model": TASK_ANALYZER_MODEL_ID,
        "input_tokens": 3,
        "output_tokens": 1,
    }
    row = {
        "group": "G1",
        "provider_spec": dict(module.GROUP_SPECS["G1"]),
        "routing_trace": {},
        "final_text": final_text,
        "final_text_sha256": module.text_sha256(final_text),
        "usage": selected_usage,
        "ensemble_trace": {
            "mode": "agent_loop",
            "agent_llm_call_count": 1,
            "untraced_agent_llm_call_count": 0,
            "calls": [
                _terminal_policy_call(
                    index=1,
                    plan=plan,
                    successful=len(plan["proposer_models"]),
                    fallback=False,
                    output=final_text,
                )
            ],
        },
        "execution": {
            "selected_generation_attempt": 2,
            "generation_attempts": [
                {
                    "attempt_id": "1" * 32,
                    "attempt": 1,
                    "run": {
                        "error": "retry",
                        "final_text_sha256": module.text_sha256("earlier"),
                        "llm_request_count": 1,
                        "routing_trace": {
                            "kind": "selection_mode",
                            "selection_mode": "router_dynamic",
                            "selection_plan": deepcopy(plan),
                        },
                        "usage": {"model_usage_breakdown": [analyzer]},
                    },
                },
                {
                    "attempt_id": "2" * 32,
                    "attempt": 2,
                    "run": {
                        "error": "",
                        "final_text_sha256": module.text_sha256(final_text),
                        "llm_request_count": 1,
                        "routing_trace": {},
                        "usage": deepcopy(selected_usage),
                    },
                },
            ],
        },
    }
    compatibility = {"g1_registry_contract": contract}

    assert (
        module.ensemble_generation_completion_reasons(
            row,
            expected_run_compatibility_contract=compatibility,
        )
        == []
    )

    conflict = deepcopy(row)
    conflict["ensemble_trace"]["calls"][0]["selection_plan"]["decision_id"] = (
        "conflicting-decision"
    )
    conflict_reasons = module.ensemble_generation_completion_reasons(
        conflict,
        expected_run_compatibility_contract=compatibility,
    )
    assert "g1_lifecycle_plan_differs_from_physical_plan" in conflict_reasons

    missing_analyzer = deepcopy(row)
    missing_analyzer["execution"]["generation_attempts"][0]["run"]["usage"] = {
        "model_usage_breakdown": []
    }
    assert (
        "missing_g1_task_analyzer_request"
        in module.ensemble_generation_completion_reasons(
            missing_analyzer,
            expected_run_compatibility_contract=compatibility,
        )
    )

    repeated_analyzer = deepcopy(row)
    repeated_analyzer["execution"]["generation_attempts"][1]["run"]["usage"][
        "model_usage_breakdown"
    ] = [deepcopy(analyzer)]
    assert (
        "repeated_g1_task_analyzer_request"
        in module.ensemble_generation_completion_reasons(
            repeated_analyzer,
            expected_run_compatibility_contract=compatibility,
        )
    )

    prompt_hash = module.text_sha256("same prompt")
    row.update(
        {
            "task_id": "task-1",
            "prompt_sha256": prompt_hash,
            "task_input_sha256": "sha256:task-input",
            "run_compatibility_fingerprint": "sha256:run-contract",
            "quality_total": 80.0,
            "judge": _complete_legacy_judge("g1-lifecycle-judge"),
        }
    )
    state = module.resume_row_completion_state(
        module.seal_result_row(row),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
        expected_run_compatibility_contract=compatibility,
    )
    assert state["generation_valid"] is True, state
    assert state["action"] == "metadata_only"


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
def test_g1_runtime_dynamic_plan_satisfies_frozen_ranking_contract(module) -> None:
    experiment = _experiment_config()
    assert experiment.g1_routing is not None
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "fake",
            "provider_routing": experiment.g1_routing.expected_routes,
        },
        llm_ensemble={"enabled": True, "selection_mode": "router_dynamic"},
    )
    inherited = ProviderConfig(
        provider="openrouter",
        model="deepseek/deepseek-v4-pro",
        api_key="fake",
        provider_routing=experiment.g1_routing.expected_routes,
    )

    provider = build_ensemble_provider_from_config(
        config=config,
        inherited_provider_config=inherited,
        fallback_provider=None,
        turn_metadata={"routed_tier": "c1", "routing_confidence": 0.9},
        ranking_inputs={"registry_allowlist": experiment.g1_routing.model_dump(mode="json")},
    )

    assert (
        module.g1_registry_contract_reasons(
            {"selection_plan": provider.selection_plan},
            experiment.g1_routing.model_dump(mode="json"),
        )
        == []
    )


def _compatibility_for(
    module,
    *,
    concurrency: int = 1,
    judge_repeats: int = 3,
    api_key: str = "benchmark-key-secret",
    extra_args: list[str] | None = None,
):
    args = module.build_parser().parse_args(
        [
            "--input",
            "tasks.jsonl",
            "--groups",
            "B1",
            "--concurrency",
            str(concurrency),
            "--judge-model",
            "google/gemini-3.1-pro-preview",
            "--judge-repeats",
            str(judge_repeats),
            *(extra_args or []),
        ]
    )
    args._source_provenance = {
        "git_head": "a" * 40,
        "source_tree_sha256": "b" * 64,
    }
    policy = module.benchmark_tool_policy(args)
    generation = module.generation_thinking_policy(args)
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": api_key,
        }
    )
    return module.build_run_compatibility(
        args=args,
        config=config,
        groups=["B1"],
        group_tool_policies=module.benchmark_tool_policies_for_groups(
            policy,
            ["B1"],
            args=args,
        ),
        generation_policy=generation,
    )


def test_run_compatibility_is_shared_by_main_and_resume_and_excludes_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENSQUILLA_PROVIDER_ROUTING_STRICT", raising=False)
    resume_runner = _load_resume_runner()
    main_contract = _compatibility_for(runner, concurrency=1)
    resume_contract = _compatibility_for(resume_runner, concurrency=5)

    assert main_contract["fingerprints"] == resume_contract["fingerprints"]
    assert "benchmark-key-secret" not in json.dumps(main_contract)
    changed_key = _compatibility_for(runner, api_key="different-benchmark-key")
    assert changed_key["fingerprints"]["B1"] != main_contract["fingerprints"]["B1"]
    changed_judge = _compatibility_for(runner, judge_repeats=2)
    assert changed_judge["fingerprints"]["B1"] != main_contract["fingerprints"]["B1"]
    changed_finalization = _compatibility_for(
        runner,
        extra_args=[
            "--deadline-wrapup-margin-seconds",
            "600",
            "--deadline-wrapup-disable-tools",
        ],
    )
    assert changed_finalization["fingerprints"]["B1"] != main_contract["fingerprints"]["B1"]
    assert (
        changed_finalization["contracts"]["B1"]["runner"]["finalization_policy"][
            "deadline_wrapup_margin_seconds"
        ]
        == 600
    )
    monkeypatch.setenv("OPENSQUILLA_PROVIDER_ROUTING_STRICT", "1")
    strict = _compatibility_for(runner)
    assert strict["fingerprints"]["B1"] != main_contract["fingerprints"]["B1"]


def _b2_compatibility_for(module, *, runner_concurrency: int, judge_concurrency: int):
    args = module.build_parser().parse_args(
        [
            "--input",
            "tasks.jsonl",
            "--groups",
            "B2",
            "--experiment-config",
            str(ROOT / "configs" / "benchmarks" / "draco_b2_g12.json"),
            "--experiment-config-set",
            f"runner.concurrency={runner_concurrency}",
            "--experiment-config-set",
            f"judge.concurrency={judge_concurrency}",
        ]
    )
    module.apply_b2_g12_argument_alignment(args, ["B2"])
    args._source_provenance = {
        "git_head": "a" * 40,
        "source_tree_sha256": "b" * 64,
    }
    policy = module.benchmark_tool_policy(args)
    config = GatewayConfig(llm={"provider": "openrouter", "model": "deepseek/deepseek-v4-pro"})
    return module.build_run_compatibility(
        args=args,
        config=config,
        groups=["B2"],
        group_tool_policies=module.benchmark_tool_policies_for_groups(
            policy,
            ["B2"],
            args=args,
        ),
        generation_policy=module.generation_thinking_policy(args),
    )


def test_b2_compatibility_excludes_effective_scheduling_concurrency() -> None:
    canary = _b2_compatibility_for(
        runner,
        runner_concurrency=1,
        judge_concurrency=1,
    )
    full = _b2_compatibility_for(
        runner,
        runner_concurrency=5,
        judge_concurrency=6,
    )

    assert canary["fingerprints"]["B2"] == full["fingerprints"]["B2"]
    experiment = canary["contracts"]["B2"]["experiment_config"]
    assert "concurrency" not in experiment["runner"]
    assert "concurrency" not in experiment["judge"]
    provider_routing = canary["contracts"]["B2"]["resolved_llm_runtime"]["provider_routing"]
    assert provider_routing["deepseek/deepseek-v4-pro"] == "deepseek"
    assert provider_routing["google/gemini-3.1-pro-preview"] == "google-ai-studio"
    assert provider_routing["moonshotai/kimi-k2.7-code"] == "moonshotai"
    assert provider_routing["qwen/qwen3.7-max"] == "alibaba"
    assert provider_routing["z-ai/glm-5.2"] == "z-ai"


def test_resume_expected_manifest_rejects_incompatible_contract(tmp_path: Path) -> None:
    resume_runner = _load_resume_runner()
    actual = _compatibility_for(resume_runner)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"run_compatibility": {"fingerprints": {"B1": "sha256:different"}}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="incompatible"):
        resume_runner.validate_expected_run_compatibility(
            path=manifest,
            actual=actual,
            groups=["B1"],
        )


def _repair_source_drift_compatibility(
    module,
    *,
    git_head: str,
    source_tree_sha256: str,
    contract_marker: str = "same",
) -> dict[str, object]:
    contract = {
        "schema": module.RUN_COMPATIBILITY_SCHEMA,
        "benchmark": "DRACO",
        "group": "B0",
        "source_identity": {
            "git_head": git_head,
            "source_tree_sha256": source_tree_sha256,
        },
        "contract_marker": contract_marker,
        "resolved_llm_runtime": {"provider": "openrouter"},
    }
    return {
        "schema": module.RUN_COMPATIBILITY_SCHEMA,
        "contracts": {"B0": contract},
        "fingerprints": {"B0": module.canonical_json_sha256(contract)},
    }


def test_repair_only_source_drift_inherits_expected_contract_and_actions(
    tmp_path: Path,
) -> None:
    module = _load_resume_runner()
    expected = _repair_source_drift_compatibility(
        module,
        git_head="a" * 40,
        source_tree_sha256="b" * 64,
    )
    actual = _repair_source_drift_compatibility(
        module,
        git_head="c" * 40,
        source_tree_sha256="d" * 64,
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"run_compatibility": expected}),
        encoding="utf-8",
    )

    inherited, audit = (
        module.validate_repair_only_source_drift_compatibility(
            path=manifest,
            actual=actual,
            groups=["B0"],
        )
    )

    assert inherited == expected
    assert audit["groups"]["B0"]["source_identity_changed"] is True
    assert audit["groups"]["B0"]["non_source_contract_match"] is True
    action_audit = module.repair_only_resume_classification_audit(
        selected_keys={("B2", "task-1"), ("G1", "task-1")},
        resume_states={
            ("B2", "task-1"): {"action": "judge_only"},
            ("G1", "task-1"): {"action": "metadata_only"},
        },
    )
    assert action_audit["status"] == "repair_actions_validated"
    assert action_audit["action_counts"] == {
        "judge_only": 1,
        "metadata_only": 1,
    }
    assert action_audit["regenerate_pair_count"] == 0
    assert action_audit["generation_allowed"] is False


@pytest.mark.parametrize(
    ("mutate_expected", "match"),
    [
        (
            lambda expected: expected["contracts"]["B0"].__setitem__(
                "contract_marker",
                "different",
            ),
            "non_source_contract_mismatch",
        ),
        (
            lambda expected: expected["fingerprints"].__setitem__(
                "B0",
                "sha256:not-canonical",
            ),
            "expected_fingerprint_not_canonical",
        ),
    ],
    ids=["non-source-contract", "non-canonical-fingerprint"],
)
def test_repair_only_source_drift_rejects_every_other_difference(
    tmp_path: Path,
    mutate_expected,
    match: str,
) -> None:
    module = _load_resume_runner()
    expected = _repair_source_drift_compatibility(
        module,
        git_head="a" * 40,
        source_tree_sha256="b" * 64,
    )
    actual = _repair_source_drift_compatibility(
        module,
        git_head="c" * 40,
        source_tree_sha256="d" * 64,
    )
    mutate_expected(expected)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"run_compatibility": expected}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=match):
        module.validate_repair_only_source_drift_compatibility(
            path=manifest,
            actual=actual,
            groups=["B0"],
        )


def test_repair_only_source_drift_requires_all_safeguards(tmp_path: Path) -> None:
    module = _load_resume_runner()
    input_path = tmp_path / "tasks.jsonl"
    input_path.write_text("", encoding="utf-8")
    args = module.build_parser().parse_args(
        [
            "--input",
            str(input_path),
            "--groups",
            "B0",
            "--repair-only-source-drift",
        ]
    )

    with pytest.raises(ValueError, match="all repair-only safeguards") as exc:
        module.validate_repair_only_source_drift_prerequisites(args)

    message = str(exc.value)
    assert "--require-clean-source" in message
    assert "--expected-compatibility-manifest" in message
    assert "--resume-from-jsonl" in message
    assert "--only-group-task-keys" in message


def test_repair_only_classification_rejects_regenerate_and_budget_exhaustion() -> None:
    module = _load_resume_runner()
    audit = module.repair_only_resume_classification_audit(
        selected_keys={("B0", "missing"), ("B1", "budget")},
        resume_states={
            ("B1", "budget"): {
                "action": "regenerate",
                "prior_generation_attempts_used": module.GENERATION_MAX_ATTEMPTS,
                "generation_reasons": ["empty_final_text"],
            }
        },
    )

    assert audit["status"] == "rejected_regeneration_required"
    assert audit["regenerate_pair_count"] == 2
    assert {
        item["reason"] for item in audit["regenerate_pairs"]
    } == {"missing_resume_state", "generation_budget_exhausted"}


@pytest.mark.asyncio
async def test_repair_only_regenerate_fails_before_provider_or_judge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_resume_runner()
    task = {"id": "task-1", "prompt": "prompt"}
    input_path = tmp_path / "tasks.jsonl"
    input_path.write_text(json.dumps(task) + "\n", encoding="utf-8")
    resume_path = tmp_path / "resume.jsonl"
    resume_path.write_text("", encoding="utf-8")
    only_keys = tmp_path / "only.jsonl"
    only_keys.write_text(
        json.dumps({"group": "B0", "task_id": "task-1"}) + "\n",
        encoding="utf-8",
    )
    expected = _repair_source_drift_compatibility(
        module,
        git_head="a" * 40,
        source_tree_sha256="b" * 64,
    )
    actual = _repair_source_drift_compatibility(
        module,
        git_head="c" * 40,
        source_tree_sha256="d" * 64,
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"run_compatibility": expected}),
        encoding="utf-8",
    )
    args = module.build_parser().parse_args(
        [
            "--input",
            str(input_path),
            "--output-dir",
            str(tmp_path / "output"),
            "--groups",
            "B0",
            "--resume-from-jsonl",
            str(resume_path),
            "--only-group-task-keys",
            str(only_keys),
            "--expected-compatibility-manifest",
            str(manifest),
            "--require-clean-source",
            "--repair-only-source-drift",
            "--judge-model",
            "judge-model",
        ]
    )
    monkeypatch.setattr(
        module,
        "source_provenance",
        lambda: {
            "git_head": "c" * 40,
            "source_tree_sha256": "d" * 64,
            "git_dirty": False,
        },
    )
    monkeypatch.setattr(
        module.GatewayConfig,
        "load",
        lambda _path: GatewayConfig(),
    )
    monkeypatch.setattr(
        module,
        "build_run_compatibility",
        lambda **_kwargs: actual,
    )
    calls = {"preflight": 0, "provider": 0, "generation": 0, "judge": 0}

    async def forbidden_preflight(*_args, **_kwargs):
        calls["preflight"] += 1
        raise AssertionError("preflight must not start")

    def forbidden_provider(*_args, **_kwargs):
        calls["provider"] += 1
        raise AssertionError("provider must not be built")

    async def forbidden_generation(*_args, **_kwargs):
        calls["generation"] += 1
        raise AssertionError("generation must not start")

    async def forbidden_judge(*_args, **_kwargs):
        calls["judge"] += 1
        raise AssertionError("Judge must not start")

    monkeypatch.setattr(
        module,
        "run_local_web_tools_preflight",
        forbidden_preflight,
    )
    monkeypatch.setattr(module, "build_single_provider", forbidden_provider)
    monkeypatch.setattr(module, "run_one", forbidden_generation)
    monkeypatch.setattr(module, "judge_text", forbidden_judge)

    with pytest.raises(
        ValueError,
        match="refuses every generation path",
    ):
        await module.amain(args)

    assert calls == {
        "preflight": 0,
        "provider": 0,
        "generation": 0,
        "judge": 0,
    }
    assert args._run_compatibility == expected
    assert args._repair_compatibility_audit["status"] == (
        "rejected_regeneration_required"
    )
    assert not (tmp_path / "output").exists()


def test_repair_manifest_keeps_current_source_and_inherited_compatibility(
    tmp_path: Path,
) -> None:
    module = _load_resume_runner()
    input_path = tmp_path / "tasks.jsonl"
    input_path.write_text("", encoding="utf-8")
    args = module.build_parser().parse_args(
        [
            "--input",
            str(input_path),
            "--groups",
            "B0",
            "--repair-only-source-drift",
        ]
    )
    inherited = _repair_source_drift_compatibility(
        module,
        git_head="a" * 40,
        source_tree_sha256="b" * 64,
    )
    args._source_provenance = {
        "git_head": "c" * 40,
        "source_tree_sha256": "d" * 64,
        "git_dirty": False,
    }
    args._run_compatibility = inherited
    args._repair_compatibility_audit = {
        "schema": module.REPAIR_ONLY_SOURCE_DRIFT_SCHEMA,
        "status": "repair_actions_validated",
    }
    manifest = tmp_path / "repair.manifest.json"

    module.write_manifest(
        manifest,
        args=args,
        stamp="repair-test",
        status="running",
        started_at=1.0,
        tasks=[],
        groups=["B0"],
        artifacts={},
        tool_policy={"tool_mode": "provider_only"},
    )

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["source_provenance"]["git_head"] == "c" * 40
    assert payload["run_compatibility"] == inherited
    assert payload["repair_compatibility_audit"]["status"] == (
        "repair_actions_validated"
    )


def test_task_input_hash_covers_rubric_not_only_prompt() -> None:
    first = {"id": "task-1", "prompt": "same", "rubric": {"criteria": ["a"]}}
    second = {"id": "task-1", "prompt": "same", "rubric": {"criteria": ["b"]}}

    assert runner.text_sha256(first["prompt"]) == runner.text_sha256(second["prompt"])
    assert runner.canonical_json_sha256(first) != runner.canonical_json_sha256(second)


def test_openrouter_non_byok_audit_fails_closed() -> None:
    exact = {
        "llm_request_count": 2,
        "usage": {
            "model_usage_breakdown": [
                {
                    "provider": "openrouter",
                    "model": "model-a",
                    "input_tokens": 3,
                    "output_tokens": 1,
                    "billed_cost": 0.01,
                    "cost_source": "provider_billed",
                    "provider_usage": _openrouter_exact_evidence(0.01, "exact-1"),
                },
                {
                    "provider": "openrouter",
                    "model": "model-b",
                    "input_tokens": 4,
                    "output_tokens": 2,
                    "billed_cost": 0.02,
                    "cost_source": "provider_billed",
                    "provider_usage": _openrouter_exact_evidence(0.02, "exact-2"),
                },
            ]
        },
        # A forged/stale summary must not bypass physical receipt validation.
        "cost_accounting": {
            "llm_total": {
                "cost_exact": True,
                "request_count": 2,
                "exact_request_count": 2,
            }
        },
    }
    unverified = {
        **exact,
        "usage": {
            "model_usage_breakdown": [
                exact["usage"]["model_usage_breakdown"][0],
                {
                    **exact["usage"]["model_usage_breakdown"][1],
                    "provider_usage": {
                        "is_byok": True,
                        "provider_reported_cost": 0.02,
                        "response_ids": ["byok-2"],
                        "router_metadata": {"is_byok": True},
                    },
                },
            ]
        },
        "cost_accounting": {
            "llm_total": {
                "cost_exact": True,
                "request_count": 2,
                "exact_request_count": 2,
            }
        },
    }

    assert runner.openrouter_non_byok_audit(exact)["pass"] is True
    audit = runner.openrouter_non_byok_audit(unverified)
    assert audit["pass"] is False
    assert audit["status"] == "policy_violation"
    assert audit["policy_safe_to_continue"] is False
    assert audit["explicit_byok_request_count"] == 1
    assert audit["conflict_request_count"] == 0
    assert audit["unverified_request_count"] == 0
    assert audit["unverified_or_byok_request_count"] == 1


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
@pytest.mark.parametrize(
    ("group", "model", "provider_slug", "provider_name", "serving_model"),
    [
        (
            "B0",
            "anthropic/claude-opus-4.8",
            "anthropic",
            "Anthropic",
            "anthropic/claude-4.8-opus-20260528",
        ),
        (
            "B1",
            "deepseek/deepseek-v4-flash",
            "deepseek",
            "DeepSeek",
            "deepseek/deepseek-v4-flash-20260423",
        ),
        (
            "B2",
            "qwen/qwen3.7-max",
            "alibaba",
            "Alibaba",
            "qwen/qwen3.7-max-20260520",
        ),
        (
            "B4",
            "openai/gpt-5.5",
            "openai",
            "OpenAI",
            "openai/gpt-5.5-20260423",
        ),
        (
            "G1",
            "x-ai/grok-4.5",
            "xai",
            "xAI",
            "x-ai/grok-4.5-20260708",
        ),
    ],
)
def test_openrouter_non_byok_receipt_requires_formal_serving_route_pin(
    module,
    group: str,
    model: str,
    provider_slug: str,
    provider_name: str,
    serving_model: str,
) -> None:
    del group
    routing = {model: provider_slug}
    unit = {
        "provider": "openrouter",
        "model": serving_model,
        "requested_model": model,
        "input_tokens": 3,
        "output_tokens": 1,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
        "provider_usage": _openrouter_exact_evidence(
            0.01,
            f"route-{provider_slug}",
            requested_model=model,
            serving_provider=provider_name,
            serving_model=serving_model,
        ),
    }
    row = {"llm_request_count": 1, "usage": unit}

    exact = module.openrouter_non_byok_audit(
        row,
        provider_routing=routing,
    )
    assert exact["pass"] is True
    assert exact["status"] == "exact"

    selected_only = json.loads(json.dumps(row))
    selected_only["usage"]["provider_usage"]["router_metadata"].pop("attempts")
    selected_only_audit = module.openrouter_non_byok_audit(
        selected_only,
        provider_routing=routing,
    )
    assert selected_only_audit["pass"] is True

    unexpected = json.loads(json.dumps(row))
    metadata = unexpected["usage"]["provider_usage"]["router_metadata"]
    metadata["attempts"][0]["provider"] = "UnexpectedProvider"
    metadata["endpoints"]["available"][0]["provider"] = "UnexpectedProvider"
    rejected = module.openrouter_non_byok_audit(
        unexpected,
        provider_routing=routing,
    )
    assert rejected["pass"] is False
    assert rejected["status"] == "policy_violation"
    assert rejected["conflict_request_count"] == 1


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_strict_openrouter_non_byok_environment_contract(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "OPENROUTER_BASE_URL",
        "OPENSQUILLA_LLM_BASE_URL",
        "OPENSQUILLA_LLM_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    for name in (
        "OPENSQUILLA_PROVIDER_ROUTING_STRICT",
        "OPENSQUILLA_PROVIDER_STREAM_ERROR_FRAMES",
        "OPENSQUILLA_OPENROUTER_METADATA_REQUIRED",
        "OPENSQUILLA_OPENROUTER_REQUIRE_PARAMETERS",
        "OPENSQUILLA_OPENROUTER_DISABLE_RESPONSE_CACHE",
        "DRACO_OPENROUTER_KEY_EXCLUSIVE",
    ):
        monkeypatch.setenv(name, "1")
    monkeypatch.setenv("OPENSQUILLA_TRUST_ENV", "0")
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "test-openrouter-key",
        }
    )

    contract = module.validate_strict_openrouter_non_byok_environment(config)

    assert contract["validated"] is True
    assert contract["key_exclusive"] is True
    assert contract["trust_env"] is False
    monkeypatch.setenv("HTTPS_PROXY", "http://unexpected.invalid")
    with pytest.raises(ValueError, match="HTTPS_PROXY"):
        module.validate_strict_openrouter_non_byok_environment(config)


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
async def test_require_non_byok_rejects_unsafe_environment_before_any_call(
    module,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_path = tmp_path / "tasks.jsonl"
    task_path.write_text(
        json.dumps({"id": "task-1", "prompt": "prompt"}) + "\n",
        encoding="utf-8",
    )
    args = module.build_parser().parse_args(
        [
            "--input",
            str(task_path),
            "--output-dir",
            str(tmp_path / "output"),
            "--groups",
            "B0",
            "--require-openrouter-non-byok",
        ]
    )
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "anthropic/claude-opus-4.8",
            "api_key": "test-openrouter-key",
        }
    )
    monkeypatch.setattr(module.GatewayConfig, "load", lambda _path: config)
    monkeypatch.delenv("OPENSQUILLA_PROVIDER_ROUTING_STRICT", raising=False)
    calls = {"preflight": 0, "provider": 0, "judge": 0}

    async def forbidden_preflight(*_args, **_kwargs):
        calls["preflight"] += 1
        raise AssertionError("preflight must not start")

    def forbidden_provider(*_args, **_kwargs):
        calls["provider"] += 1
        raise AssertionError("provider must not be built")

    async def forbidden_judge(*_args, **_kwargs):
        calls["judge"] += 1
        raise AssertionError("Judge must not start")

    monkeypatch.setattr(module, "run_local_web_tools_preflight", forbidden_preflight)
    monkeypatch.setattr(module, "build_single_provider", forbidden_provider)
    monkeypatch.setattr(module, "judge_text", forbidden_judge)

    with pytest.raises(
        ValueError,
        match="strict OpenRouter non-BYOK environment validation failed",
    ):
        await module.amain(args)
    assert calls == {"preflight": 0, "provider": 0, "judge": 0}


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_openrouter_non_byok_audit_rejects_zero_request_vacuous_pass(module) -> None:
    audit = module.openrouter_non_byok_audit({"llm_request_count": 0, "usage": {}})

    assert audit["pass"] is False
    assert audit["status"] == "metadata_incomplete"
    assert audit["policy_safe_to_continue"] is True
    assert audit["request_count"] == 0
    assert audit["evidence_unit_count"] == 0


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
@pytest.mark.parametrize("extra_kind", ["explicit_byok", "conflict"])
def test_openrouter_non_byok_audit_does_not_hide_extra_policy_evidence(
    module,
    extra_kind: str,
) -> None:
    exact = {
        "provider": "openrouter",
        "model": "model-a",
        "input_tokens": 3,
        "output_tokens": 1,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
        "provider_usage": _openrouter_exact_evidence(0.01, "exact-1"),
    }
    extra = {
        **exact,
        "provider_usage": {
            "is_byok": True,
            "provider_reported_cost": 0.01,
            "response_ids": [f"{extra_kind}-1"],
            "router_metadata": {
                **_openrouter_exact_evidence(0.01, "unused")["router_metadata"],
                "is_byok": extra_kind == "explicit_byok",
            },
        },
    }
    audit = module.openrouter_non_byok_audit(
        {
            "llm_request_count": 1,
            "usage": {"model_usage_breakdown": [exact, extra]},
        }
    )

    assert audit["pass"] is False
    assert audit["status"] == "policy_violation"
    assert audit["policy_safe_to_continue"] is False
    assert audit["request_count"] == 2
    assert audit["evidence_unit_count"] == 2
    assert audit["evidence_overflow_count"] == 0
    if extra_kind == "explicit_byok":
        assert audit["explicit_byok_request_count"] == 1
        assert audit["conflict_request_count"] == 0
    else:
        assert audit["explicit_byok_request_count"] == 0
        assert audit["conflict_request_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "strict",
        "expected_requests",
        "explicit_byok",
        "expected_judge_calls",
        "expected_error",
    ),
    [
        (True, 5, False, 1, "openrouter_non_byok_metadata_incomplete"),
        (True, 4, False, 1, None),
        (True, 4, True, 0, "openrouter_non_byok_policy_violation"),
        (False, 5, False, 1, "cost_metadata_incomplete"),
    ],
    ids=[
        "strict-missing-receipt",
        "strict-all-exact",
        "strict-explicit-byok",
        "non-strict-unchanged",
    ],
)
async def test_generation_non_byok_gate_runs_before_judge(
    monkeypatch: pytest.MonkeyPatch,
    strict: bool,
    expected_requests: int,
    explicit_byok: bool,
    expected_judge_calls: int,
    expected_error: str | None,
) -> None:
    config, inherited = _openrouter_config()
    rows = [
        {
            "provider": "openrouter",
            "model": f"model-{index}",
            "input_tokens": 3,
            "output_tokens": 1,
            "billed_cost": 0.01,
            "cost_source": "provider_billed",
            "provider_usage": _openrouter_exact_evidence(
                0.01,
                f"generation-{index}",
            ),
        }
        for index in range(4)
    ]
    if explicit_byok:
        rows[-1]["provider_usage"] = {
            "is_byok": True,
            "provider_reported_cost": 0.01,
            "response_ids": ["generation-explicit-byok"],
            "router_metadata": {"is_byok": True},
        }
    done = DoneEvent(
        input_tokens=sum(int(row["input_tokens"]) for row in rows),
        output_tokens=sum(int(row["output_tokens"]) for row in rows),
        billed_cost=sum(float(row["billed_cost"]) for row in rows),
        cost_source="provider_billed",
        model="model-final",
        provider="openrouter",
        model_usage_breakdown=rows,
        ensemble_trace=_valid_ensemble_trace(
            selection_mode="router_tree_baseline",
            llm_request_count=expected_requests,
        ),
    )
    result = runner.RunResult(final_text="answer", done=done)
    attempts = [
        {
            "attempt": 1,
            "retryable": False,
            "retry_reason": "",
            "will_retry": False,
            "retry_backoff_s": 0.0,
            "run": runner.run_result_summary(result),
        }
    ]

    async def fake_build_experiment_provider(**_kwargs):
        return runner.ProviderBuildResult(provider=object(), prompt="prompt")

    async def fake_collect_generation_with_retries(*_args, **_kwargs):
        return result, attempts, 1

    judge_calls = 0

    async def fake_judge_text(**_kwargs):
        nonlocal judge_calls
        judge_calls += 1
        return _complete_legacy_judge("generation-gate-judge")

    monkeypatch.setattr(runner, "build_experiment_provider", fake_build_experiment_provider)
    monkeypatch.setattr(
        runner,
        "collect_generation_with_retries",
        fake_collect_generation_with_retries,
    )
    monkeypatch.setattr(runner, "judge_text", fake_judge_text)

    row = await runner.run_one(
        task={"id": "task-1", "prompt": "prompt"},
        group="B3",
        config=config,
        inherited=inherited,
        dry_run=False,
        judge_provider=object(),
        judge_candidates=False,
        judge_repeats=1,
        judge_concurrency=1,
        judge_max_attempts=1,
        judge_semaphore=None,
        timeout=10.0,
        ensemble_proposer_timeout=None,
        ensemble_aggregator_timeout=None,
        ensemble_proposer_early_stop_success_count=None,
        ensemble_proposer_early_stop_after=None,
        expand_ensemble_timeouts_to_task_timeout=False,
        tool_policy={"tools_enabled": False, "tool_mode": "provider_only"},
        generation_policy={},
        require_openrouter_non_byok=strict,
    )

    assert judge_calls == expected_judge_calls
    assert row["error"] == expected_error
    assert (row.get("judge") is not None) is bool(expected_judge_calls)
    if strict:
        assert row["openrouter_non_byok_audit"]["pass"] is (not expected_error)
        assert row["openrouter_non_byok_audit"]["policy_safe_to_continue"] is (not explicit_byok)
    else:
        assert "openrouter_non_byok_audit" not in row


@pytest.mark.asyncio
async def test_provider_build_failure_preserves_already_billed_setup_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, inherited = _openrouter_config()
    setup_usage = {
        "provider": "openrouter",
        "model": "anthropic/claude-opus-4.8",
        "input_tokens": 12,
        "output_tokens": 3,
        "billed_cost": 0.25,
        "cost_source": "provider_billed",
        "provider_usage": _openrouter_exact_evidence(0.25, "setup-receipt-1"),
    }

    async def fake_build_experiment_provider(**_kwargs):
        raise runner.ProviderBuildError(
            RuntimeError("strict dynamic selection failed"),
            setup_latency_ms=123,
            setup_usage=[setup_usage],
            routing_trace={"task_analyzer": {"model": setup_usage["model"]}},
        )

    judge_calls = 0

    async def fake_judge_text(**_kwargs):
        nonlocal judge_calls
        judge_calls += 1
        return {"total": 10.0}

    monkeypatch.setattr(runner, "build_experiment_provider", fake_build_experiment_provider)
    monkeypatch.setattr(runner, "judge_text", fake_judge_text)

    row = await runner.run_one(
        task={"id": "task-1", "prompt": "prompt"},
        group="G1",
        config=config,
        inherited=inherited,
        dry_run=False,
        judge_provider=object(),
        judge_candidates=False,
        judge_repeats=1,
        judge_concurrency=1,
        judge_max_attempts=1,
        judge_semaphore=None,
        timeout=10.0,
        ensemble_proposer_timeout=None,
        ensemble_aggregator_timeout=None,
        ensemble_proposer_early_stop_success_count=None,
        ensemble_proposer_early_stop_after=None,
        expand_ensemble_timeouts_to_task_timeout=False,
        tool_policy={"tools_enabled": False, "tool_mode": "provider_only"},
        generation_policy={},
        require_openrouter_non_byok=True,
    )

    assert row["error"] == "RuntimeError: strict dynamic selection failed"
    assert row["selected_generation_succeeded"] is False
    assert row["llm_request_count"] == 0
    assert row["actual_spend_metrics"]["llm_request_count"] == 1
    assert row["generation_attempt_count"] == 1
    assert row["generation_attempt_budget_used"] == 1
    assert row["generation_attempt_evidence_schema"] == (runner.GENERATION_ATTEMPT_EVIDENCE_SCHEMA)
    assert row["selected_attempt_billed_cost_usd"] == 0.0
    assert row["actual_spend_billed_cost_usd"] == pytest.approx(0.25)
    assert row["execution"]["routing_setup_latency_ms"] == 123
    failed_attempt = row["execution"]["generation_attempts"][0]
    assert failed_attempt["attempt_kind"] == "provider_build_after_paid_setup"
    assert len(failed_attempt["attempt_id"]) == 32
    assert failed_attempt["run"]["llm_request_count"] == 1
    assert failed_attempt["run"]["usage"]["model_usage_breakdown"] == [setup_usage]
    assert row["usage"]["model_usage_breakdown"] == [setup_usage]
    assert runner.openrouter_non_byok_audit(row)["pass"] is True
    assert judge_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
async def test_run_one_freezes_dataclass_routing_receipt(
    monkeypatch: pytest.MonkeyPatch,
    module,
) -> None:
    config, inherited = _openrouter_config()
    receipt = ProviderBillingReceipt(
        currency="USD",
        status="confirmed",
        amount_nanos=10_000_000,
        usd_equivalent_nanos=10_000_000,
        fx_native_per_usd_nanos=1_000_000_000,
    )
    model = str(module.GROUP_SPECS["B0"]["model"])
    usage_row = {
        "provider": "openrouter",
        "model": model,
        "requested_provider": "openrouter",
        "requested_model": model,
        "input_tokens": 3,
        "output_tokens": 1,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
        "provider_usage": _openrouter_exact_evidence(
            0.01,
            f"dataclass-routing-{module.__name__}",
        ),
    }
    done = DoneEvent(
        input_tokens=3,
        output_tokens=1,
        billed_cost=0.01,
        cost_source="provider_billed",
        provider="openrouter",
        model=model,
        requested_provider="openrouter",
        requested_model=model,
        model_usage_breakdown=[usage_row],
    )
    result = module.RunResult(final_text="answer", done=done)
    attempts = [
        {
            "attempt_id": "1" * 32,
            "attempt_kind": "generation",
            "attempt": 1,
            "started_at": 1.0,
            "completed_at": 2.0,
            "retryable": False,
            "retry_reason": "",
            "will_retry": False,
            "retry_backoff_s": 0.0,
            "run": module.run_result_summary(result),
        }
    ]

    async def fake_build_experiment_provider(**_kwargs):
        return module.ProviderBuildResult(
            provider=object(),
            prompt="prompt",
            routing_trace={
                "kind": "single",
                "model": model,
                "billing_receipt": receipt,
            },
        )

    async def fake_collect_generation_with_retries(*_args, **_kwargs):
        return result, attempts, 1

    async def fake_judge_text(**_kwargs):
        return _complete_legacy_judge(
            f"dataclass-routing-judge-{module.__name__}"
        )

    monkeypatch.setattr(
        module,
        "build_experiment_provider",
        fake_build_experiment_provider,
    )
    monkeypatch.setattr(
        module,
        "collect_generation_with_retries",
        fake_collect_generation_with_retries,
    )
    monkeypatch.setattr(module, "judge_text", fake_judge_text)

    row = await module.run_one(
        task={"id": "task-1", "prompt": "prompt"},
        group="B0",
        config=config,
        inherited=inherited,
        dry_run=False,
        judge_provider=object(),
        judge_candidates=False,
        judge_repeats=1,
        judge_concurrency=1,
        judge_max_attempts=1,
        judge_semaphore=None,
        timeout=10.0,
        ensemble_proposer_timeout=None,
        ensemble_aggregator_timeout=None,
        ensemble_proposer_early_stop_success_count=None,
        ensemble_proposer_early_stop_after=None,
        expand_ensemble_timeouts_to_task_timeout=False,
        tool_policy={"tools_enabled": False, "tool_mode": "provider_only"},
        generation_policy={},
    )

    assert row["selected_generation_succeeded"] is True
    assert row["execution"]["provider_error"] == ""
    assert row["routing_trace"]["billing_receipt"]["status"] == "confirmed"
    assert isinstance(row["routing_trace"]["billing_receipt"], dict)


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
def test_openrouter_exact_receipt_requires_billed_and_reported_cost_match(module) -> None:
    usage = {
        "provider": "openrouter",
        "model": "model-a",
        "input_tokens": 3,
        "output_tokens": 1,
        "billed_cost": 0.0,
        "cost_source": "provider_billed",
        "provider_usage": _openrouter_exact_evidence(0.25, "mismatch-1"),
    }

    accounting = module.usage_cost_accounting(
        usage,
        expected_requests=1,
        scope="generation",
    )

    assert accounting["exact_request_count"] == 0
    assert accounting["unknown_request_count"] == 1
    assert accounting["cost_exact"] is False


def test_done_payload_keeps_provider_for_single_exact_receipt() -> None:
    done = runner.DoneEvent(
        provider="openrouter",
        model="model-a",
        requested_provider="openrouter",
        requested_model="requested-model-a",
        input_tokens=3,
        output_tokens=1,
        billed_cost=0.25,
        cost_source="provider_billed",
        provider_usage=_openrouter_exact_evidence(0.25, "single-1"),
    )

    payload = runner.done_payload(done)

    assert payload["provider"] == "openrouter"
    assert payload["model"] == "model-a"
    assert payload["requested_provider"] == "openrouter"
    assert payload["requested_model"] == "requested-model-a"
    assert (
        runner.usage_cost_accounting(
            payload,
            expected_requests=1,
            scope="generation",
        )["cost_exact"]
        is True
    )


def test_b2_provider_alignment_pins_effective_member_configuration() -> None:
    config, inherited = _openrouter_config()
    provider = build_ensemble_provider_from_config(
        config=config,
        inherited_provider_config=inherited,
        fallback_provider=None,
    )
    assert provider.min_successful_proposers == 3
    assert provider.quorum_grace_seconds == 10.0

    provider = runner.align_b2_provider_to_g12(provider, _experiment_config())

    assert provider.profile_name == "g12_k2_replace_gemini"
    assert [member.provider_config.model for member in provider.proposers] == [
        "deepseek/deepseek-v4-pro",
        "z-ai/glm-5.2",
        "moonshotai/kimi-k2.7-code",
        "qwen/qwen3.7-max",
    ]
    assert provider.aggregator.provider_config.model == "z-ai/glm-5.2"
    assert provider.min_successful_proposers == 3
    assert provider.proposer_timeout_seconds == pytest.approx(907.5)
    assert provider.aggregator_timeout_seconds == pytest.approx(2662.5)
    assert provider.quorum_grace_seconds == 0.0
    assert provider.candidate_max_chars == 24_000
    assert provider.shuffle_candidates is False
    assert provider.record_candidates is True
    assert provider.proposer_tools is False
    assert provider.aggregator_tools is True

    members = [*provider.proposers, provider.aggregator]
    assert all(member.max_tokens == 16_384 for member in members)
    assert all(member.temperature == 0.0 for member in members)
    assert all(member.k == 1 for member in members)
    assert [member.thinking for member in provider.proposers] == [
        "xhigh",
        "xhigh",
        "max",
        "xhigh",
    ]
    base = ChatConfig(max_tokens=999, temperature=0.9, thinking=False)
    effective = [_member_chat_config(base, member) for member in members]
    assert all(config.max_tokens == 16_384 for config in effective)
    assert all(config.temperature == 0.0 for config in effective)
    assert all(config.thinking is True for config in effective)
    assert [config.thinking_level for config in effective] == [
        "xhigh",
        "xhigh",
        "max",
        "xhigh",
        "xhigh",
    ]

    plan = provider.selection_plan
    assert plan["benchmark_alignment"]["id"] == "opensquilla_g12_20260630"
    assert plan["pre_alignment"]["min_successful_proposers"] == 3
    assert plan["pre_alignment"]["selection_plan"]["profile"] == "static_openrouter_b5"
    assert plan["proposer_models"] == [
        "deepseek/deepseek-v4-pro",
        "z-ai/glm-5.2",
        "moonshotai/kimi-k2.7-code",
        "qwen/qwen3.7-max",
    ]
    assert plan["aggregator_model"] == "z-ai/glm-5.2"
    assert plan["proposer_count"] == 4
    assert plan["proposer_sample_count"] == 4
    assert plan["selected_P"] == [
        "openrouter:deepseek/deepseek-v4-pro",
        "openrouter:z-ai/glm-5.2",
        "openrouter:moonshotai/kimi-k2.7-code",
        "openrouter:qwen/qwen3.7-max",
    ]
    assert plan["selected_A"] == "openrouter:z-ai/glm-5.2"
    assert plan["wait_for_all_proposers"] is True
    assert plan["member_generation"][2]["model"] == "moonshotai/kimi-k2.7-code"
    assert plan["member_generation"][2]["max_tokens"] == 16_384
    assert plan["member_generation"][2]["thinking"] == "max"
    assert plan["proposer_tools"] is False
    assert plan["aggregator_tools"] is True

    provider = runner.enforce_draco_legal_proposer_quorum(provider)
    assert provider.min_successful_proposers == 3
    assert provider.selection_plan["effective_min_successful_proposers"] == 3
    assert provider.selection_plan["legal_min_successful_proposers"] == 3


def test_b2_provider_alignment_rebuilds_trace_after_lineup_override() -> None:
    config, inherited = _openrouter_config()
    provider = build_ensemble_provider_from_config(
        config=config,
        inherited_provider_config=inherited,
        fallback_provider=None,
    )
    experiment = runner.load_draco_experiment_config(
        runner.DEFAULT_B2_EXPERIMENT_CONFIG_PATH,
        inline_sets=[
            'ensemble.proposers.0.model="openai/gpt-5.5-pro"',
            'ensemble.proposers.0.thinking="xhigh"',
            'ensemble.aggregator.model="deepseek/deepseek-v4-pro"',
            'ensemble.aggregator.thinking="xhigh"',
        ],
    ).config

    provider = runner.align_b2_provider_to_g12(provider, experiment)

    assert provider.proposers[0].provider_config.model == "openai/gpt-5.5-pro"
    assert provider.aggregator.provider_config.model == "deepseek/deepseek-v4-pro"
    assert provider.selection_plan["proposer_models"][0] == "openai/gpt-5.5-pro"
    assert provider.selection_plan["aggregator_model"] == "deepseek/deepseek-v4-pro"
    assert provider.selection_plan["selected_P"][0] == "openrouter:openai/gpt-5.5-pro"
    assert provider.selection_plan["selected_A"] == ("openrouter:deepseek/deepseek-v4-pro")
    assert (
        provider.selection_plan["pre_alignment"]["selection_plan"]["aggregator_model"]
        == "z-ai/glm-5.2"
    )


@pytest.mark.asyncio
async def test_b2_build_skips_single_model_router_and_aligns_provider(monkeypatch) -> None:
    config, inherited = _openrouter_config()

    async def _unexpected_router(*_args, **_kwargs):
        raise AssertionError("fixed B2 must not run SquillaRouter")

    monkeypatch.setattr(runner, "run_pipeline", _unexpected_router)
    result = await runner.build_experiment_provider(
        config=config,
        inherited=inherited,
        group="B2",
        prompt="test prompt",
        dry_run=False,
        enable_proposer_tools=True,
        ensemble_proposer_timeout=1.0,
        ensemble_aggregator_timeout=2.0,
        experiment_config=_experiment_config(),
    )

    assert result.routing_trace["routing_applied"] is False
    assert result.routing_trace["routing_source"] == "fixed_g12_alignment"
    assert result.routing_trace["selection_plan"]["wait_for_all_proposers"] is True
    assert result.provider.proposer_tools is False
    assert result.provider.aggregator_tools is True
    assert result.provider.proposer_timeout_seconds == pytest.approx(907.5)
    assert result.provider.aggregator_timeout_seconds == pytest.approx(2662.5)


@pytest.mark.asyncio
async def test_b2_dry_build_records_canonical_selection_plan() -> None:
    config, inherited = _openrouter_config()

    result = await runner.build_experiment_provider(
        config=config,
        inherited=inherited,
        group="B2",
        prompt="test prompt",
        dry_run=True,
        enable_proposer_tools=True,
        ensemble_proposer_timeout=1.0,
        ensemble_aggregator_timeout=2.0,
        experiment_config=_experiment_config(),
    )

    plan = result.routing_trace["selection_plan"]
    assert plan["proposer_models"] == [
        "deepseek/deepseek-v4-pro",
        "z-ai/glm-5.2",
        "moonshotai/kimi-k2.7-code",
        "qwen/qwen3.7-max",
    ]
    assert plan["aggregator_model"] == "z-ai/glm-5.2"
    assert plan["selected_P"][2] == "openrouter:moonshotai/kimi-k2.7-code"
    assert plan["selected_A"] == "openrouter:z-ai/glm-5.2"
    assert plan["proposer_count"] == 4
    assert plan["proposer_sample_count"] == 4
    assert plan["member_generation"][-1]["role"] == "aggregator"
    assert plan["member_generation"][-1]["thinking"] == "xhigh"


def test_manifest_records_effective_and_requested_b2_alignment(tmp_path: Path) -> None:
    args = runner.build_parser().parse_args(
        ["--input", "tasks.jsonl", "--groups", "B2", "--concurrency", "8"]
    )
    runner.apply_b2_g12_argument_alignment(args, ["B2"])
    path = tmp_path / "manifest.json"

    runner.write_manifest(
        path,
        args=args,
        stamp="test",
        status="running",
        started_at=1.0,
        tasks=[{"id": "task-1"}],
        groups=["B2"],
        artifacts={},
        tool_policy={"tool_mode": "local_web_tools", "tools_enabled": True},
    )

    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert manifest["runner"] == "scripts/run_draco_routing_experiment.py"
    assert manifest["args"]["concurrency"] == 2
    assert manifest["agent_finalization_policy"] == {
        "deadline_wrapup_margin_seconds": 600,
        "deadline_wrapup_disable_tools": True,
        "deadline_thinking_off_margin_seconds": 600,
        "max_iterations_includes_finalization": True,
        "retrieval_loop_finalization_threshold": 3,
        "finalization_aggregator_only": True,
        "finalization_disable_thinking": True,
    }
    alignment = manifest["benchmark_alignments"]["B2"]
    assert alignment["requested_args"]["concurrency"] == 8
    assert alignment["effective_args"]["concurrency"] == 2
    assert alignment["effective_config"]["ensemble"]["wait_for_all_proposers"] is True
    assert alignment["reference"]["source_commit"] == ("153e5ff267950b0e285efcdb180cea8724c0471d")
    assert len(manifest["source_provenance"]["runner_sha256"]) == 64
    assert "git_head" in manifest["source_provenance"]


def test_markdown_records_agent_finalization_policy(tmp_path: Path) -> None:
    policy = {
        **runner.DEFAULT_AGENT_FINALIZATION_POLICY,
        "deadline_wrapup_margin_seconds": 600,
        "finalization_disable_thinking": True,
    }

    markdown = runner.render_markdown(
        {"groups": {}},
        tmp_path / "draco_ensemble_test.jsonl",
        agent_finalization_policy=policy,
    )

    assert "Agent finalization policy:" in markdown
    assert '"deadline_wrapup_margin_seconds": 600' in markdown
    assert '"finalization_disable_thinking": true' in markdown
    assert "Avg Selected LLM $" in markdown
    assert "Avg Actual LLM $" in markdown
    assert "selected columns contain only the accepted generation attempt" in markdown
    assert "actual-spend columns contain every generation attempt" in markdown


def test_manifest_reuses_source_provenance_frozen_at_process_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = runner.build_parser().parse_args(["--input", "tasks.jsonl", "--groups", "B1"])
    args._source_provenance = {
        "runner_path": "/frozen/runner.py",
        "runner_sha256": "a" * 64,
        "git_head": "b" * 40,
        "git_dirty": False,
        "source_tree_sha256": "c" * 64,
    }
    monkeypatch.setattr(
        runner,
        "source_provenance",
        lambda: {"runner_sha256": "changed-after-start"},
    )
    path = tmp_path / "manifest.json"

    runner.write_manifest(
        path,
        args=args,
        stamp="test",
        status="complete",
        started_at=1.0,
        tasks=[{"id": "task-1"}],
        groups=["B1"],
        artifacts={},
    )

    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert manifest["source_provenance"] == args._source_provenance


def test_source_provenance_detects_tracked_changes_against_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            stdout = "a" * 40 + "\n"
        elif command[:3] == ["git", "diff", "--binary"]:
            stdout = "tracked diff\n"
        else:
            stdout = ""
        return runner.subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    provenance = runner.source_provenance()

    diff_command = next(command for command in commands if command[1] == "diff")
    assert diff_command.count("HEAD") == 1
    assert provenance["git_tracked_dirty"] is True
    assert provenance["git_dirty"] is True


def test_experiment_config_artifacts_save_source_effective_and_resolution(
    tmp_path: Path,
) -> None:
    override_path = tmp_path / "override.json"
    override_path.write_text('{"judge":{"repeats":2}}\n', encoding="utf-8")
    args = runner.build_parser().parse_args(
        [
            "--input",
            "tasks.jsonl",
            "--groups",
            "B2",
            "--experiment-config-override",
            str(override_path),
            "--experiment-config-set",
            "runner.concurrency=4",
        ]
    )
    runner.apply_b2_g12_argument_alignment(args, ["B2"])
    args._draco_input_validation = {"status": "matched"}

    artifacts = runner.write_experiment_config_artifacts(
        tmp_path,
        args=args,
        stamp="test",
    )

    assert set(artifacts) == {
        "experiment_config_base_json",
        "experiment_config_effective_json",
        "experiment_config_override_01_json",
        "experiment_config_inline_overrides_json",
        "experiment_config_resolution_json",
    }
    effective = json.loads(
        Path(artifacts["experiment_config_effective_json"]).read_text(encoding="utf-8")
    )
    inline = json.loads(
        Path(artifacts["experiment_config_inline_overrides_json"]).read_text(encoding="utf-8")
    )
    resolution = json.loads(
        Path(artifacts["experiment_config_resolution_json"]).read_text(encoding="utf-8")
    )
    assert effective["runner"]["concurrency"] == 4
    assert effective["judge"]["repeats"] == 2
    copied_override = json.loads(
        Path(artifacts["experiment_config_override_01_json"]).read_text(encoding="utf-8")
    )
    assert copied_override == {"judge": {"repeats": 2}}
    assert inline == [{"path": "runner.concurrency", "value": 4}]
    assert resolution["input_validation"]["status"] == "matched"
    assert resolution["artifact_keys"] == sorted(artifacts)
