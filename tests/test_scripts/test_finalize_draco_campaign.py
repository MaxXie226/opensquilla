from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from opensquilla.eval.draco_artifact_integrity import (
    trace_row_from_result as canonical_trace_row_from_result,
)
from opensquilla.eval.draco_experiment_config import load_draco_experiment_config

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "experiments" / "finalize_draco_campaign.py"
TEST_G1_RUNTIME_REGISTRY_HASH = "d" * 64


def _load():
    spec = importlib.util.spec_from_file_location("finalize_draco_campaign_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def module():
    return _load()


def test_formal_model_thinking_levels_match_campaign_config(module) -> None:
    config_path = ROOT / "configs" / "benchmarks" / "draco_b2_g12.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))

    assert module.FORMAL_MODEL_THINKING_LEVELS == config["generation"]["model_thinking_levels"]
    assert module.FORMAL_MODEL_THINKING_LEVELS["moonshotai/kimi-k2.7-code"] == "high"
    assert module.FORMAL_MODEL_THINKING_LEVELS["qwen/qwen3.7-max"] == "high"
    resolved = load_draco_experiment_config(config_path).config.g1_routing
    assert resolved is not None
    if getattr(resolved, "candidate_scope", "exact_routes") == "registry_all":
        assert set(module.formal_registry_all_routes().values()) == {"auto"}
    else:
        assert resolved.expected_routes is not None
        assert set(resolved.expected_routes) <= set(module.FORMAL_MODEL_THINKING_LEVELS)


def test_finalizer_version_covers_canonical_usage_evidence(module) -> None:
    assert module.FINALIZER_VERSION == 5


def test_text_sha256_matches_runner_wire_format(module) -> None:
    assert module.text_sha256("value") == hashlib.sha256(b"value").hexdigest()


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
def test_load_jsonl_rows_preserves_unicode_line_separators(
    module,
    tmp_path: Path,
    separator: str,
) -> None:
    path = tmp_path / "rows.jsonl"
    value = {"content": f"before{separator}after"}
    path.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)

    assert module.load_jsonl_rows(
        path,
        owner_only=True,
        source_label="test JSONL",
    ) == [(1, value)]


def test_run_expected_request_count_does_not_double_count_placeholder(module) -> None:
    run = {
        "llm_request_count": 2,
        "usage": {
            "usage_missing_count": 1,
            "model_usage_breakdown": [
                {"role": "proposer"},
                {"role": "agent_llm_request_unknown"},
            ],
        },
    }

    assert module.run_expected_request_count(run) == 2


@pytest.mark.parametrize("ordinal", range(5))
def test_counter_only_failed_judge_request_is_canonical_and_route_bound(
    module,
    tmp_path: Path,
    ordinal: int,
) -> None:
    """The five observed 503 shapes each become one strict unknown receipt."""

    attempt_id = f"{0x5100 + ordinal:032x}"
    run = {
        "error": (
            "OpenRouter chat request failed (HTTP 503): Cloudflare Worker exceeded resource limits"
        ),
        "llm_request_count": 1,
        "usage_unknown_count": 1,
        "usage": {},
        "trace_events": [
            {
                "kind": "error",
                "code": "503",
                "request_started": None,
                "physical_request_count": None,
            }
        ],
    }
    wave1 = module.SourceRecord(
        path=tmp_path / "wave-1.jsonl",
        source_index=0,
        line=ordinal + 1,
        row={"group": "B1", "task_id": f"task-{ordinal}"},
    )
    wave2 = module.SourceRecord(
        path=tmp_path / "wave-2.jsonl",
        source_index=1,
        line=ordinal + 1,
        row={"group": "B1", "task_id": f"task-{ordinal}"},
    )

    _, canonical = module.validate_and_select_monotonic_run_version(
        [(wave1, run), (wave2, deepcopy(run))],
        label=f"Judge attempt {attempt_id}",
        identity_seed=f"judge-attempt:{attempt_id}",
        requested_provider="openrouter",
        requested_model=module.JUDGE_MODEL,
        role="unknown_request",
    )
    units = module.canonical_run_usage_units(
        canonical,
        identity_seed=f"judge-attempt:{attempt_id}",
    )

    assert module.run_expected_request_count(canonical) == 1
    assert len(units) == 1
    assert module._is_unknown_judge_placeholder(units[0]) is True
    assert units[0]["provider"] == ""
    assert units[0]["model"] == ""
    assert units[0]["requested_provider"] == "openrouter"
    assert units[0]["requested_model"] == module.JUDGE_MODEL
    ledger_entries = {}
    module._record_run(
        ledger_entries,
        {},
        run=canonical,
        scope="judge",
        base_identity=f"judge:B1:task-{ordinal}:judge:criterion/x/0/{attempt_id}",
        reference={"group": "B1", "task_id": f"task-{ordinal}"},
        occurrence_counter=module.Counter(),
    )
    assert len(ledger_entries) == 1
    assert (
        next(iter(ledger_entries.values())).units[0]["usage_evidence_id"]
        == units[0]["usage_evidence_id"]
    )
    assert (
        module.usage_route_reasons(
            canonical["usage"],
            allowed_models={module.JUDGE_MODEL},
            provider_pins={module.JUDGE_MODEL: module.FORMAL_UPSTREAM_PINS[module.JUDGE_MODEL]},
            allow_unknown_judge_attempts=True,
        )
        == []
    )
    assert (
        module.proof_only_usage_evidence_reasons(
            {
                "judge": {
                    "criterion_judgments": [
                        {
                            "id": "criterion",
                            "repeat_index": 0,
                            "judge_attempts": [
                                {
                                    "attempt_id": attempt_id,
                                    "attempt": 1,
                                    "run": deepcopy(run),
                                }
                            ],
                        }
                    ]
                }
            }
        )
        == []
    )


def test_unknown_judge_placeholder_cannot_claim_success_or_actual_identity(
    module,
    tmp_path: Path,
) -> None:
    attempt_id = "5" * 32
    record = module.SourceRecord(
        path=tmp_path / "wave.jsonl",
        source_index=0,
        line=1,
        row={"group": "B1", "task_id": "task"},
    )
    _, canonical = module.validate_and_select_monotonic_run_version(
        [
            (
                record,
                {
                    "error": "HTTP 503",
                    "llm_request_count": 1,
                    "usage_unknown_count": 1,
                    "usage": {},
                },
            )
        ],
        label=f"Judge attempt {attempt_id}",
        identity_seed=f"judge-attempt:{attempt_id}",
        requested_provider="openrouter",
        requested_model=module.JUDGE_MODEL,
        role="unknown_request",
    )
    tampered = deepcopy(canonical["usage"])
    tampered["model_usage_breakdown"][0]["provider"] = "openrouter"

    assert set(
        module.usage_route_reasons(
            tampered,
            allowed_models={module.JUDGE_MODEL},
            provider_pins={module.JUDGE_MODEL: module.FORMAL_UPSTREAM_PINS[module.JUDGE_MODEL]},
            allow_unknown_judge_attempts=True,
        )
    ) == {
        "invalid_unknown_judge_usage_placeholder",
        "missing_generation_usage_route_evidence",
    }
    tampered_run = deepcopy(canonical)
    tampered_run["usage"] = tampered
    proof_reasons = module.proof_only_usage_evidence_reasons(
        {
            "judge": {
                "criterion_judgments": [
                    {
                        "id": "criterion",
                        "repeat_index": 0,
                        "judge_attempts": [
                            {
                                "attempt_id": attempt_id,
                                "attempt": 1,
                                "run": tampered_run,
                            }
                        ],
                    }
                ]
            }
        }
    )
    assert any("invalid_unknown_judge_usage_placeholder" in reason for reason in proof_reasons)


def test_selected_endpoint_receipt_binds_serving_alias_and_provider(module) -> None:
    unit = {
        "role": "agent_llm_call",
        "provider": "openrouter",
        "model": module.B0_MODEL,
        "requested_provider": "openrouter",
        "requested_model": module.B0_MODEL,
        "billed_cost": 0.1,
        "provider_usage": {
            "is_byok": False,
            "provider_reported_cost": 0.1,
            "response_ids": ["response-1"],
            "router_metadata": {
                "requested": module.B0_MODEL,
                "is_byok": False,
                "endpoints": {
                    "available": [
                        {
                            "provider": "Anthropic",
                            "model": "anthropic/claude-4.8-opus-20260528",
                            "selected": True,
                        }
                    ]
                },
            },
        },
    }

    assert (
        module.usage_route_reasons(
            {
                "model_usage_breakdown": [
                    unit,
                    {"role": "agent_llm_request_unknown"},
                ]
            },
            allowed_models={module.B0_MODEL},
            provider_pins={module.B0_MODEL: "anthropic"},
        )
        == []
    )
    assert module.unit_exact_non_byok(unit) is True
    assert module._formal_openrouter_models_equivalent(
        module.B0_MODEL,
        "anthropic/claude-4.8-opus-20260528",
    )
    assert module._formal_openrouter_models_equivalent(
        "anthropic/claude-sonnet-5",
        "anthropic/claude-sonnet-5-20260630",
    )

    bad_placeholder = {
        "role": "agent_llm_request_unknown",
        "provider_usage": {
            "requested_provider": "direct",
            "requested_model": module.B4_MODEL,
            "router_metadata": {"requested": module.B4_MODEL},
        },
    }
    reasons = module.usage_route_reasons(
        {"model_usage_breakdown": [unit, bad_placeholder]},
        allowed_models={module.B0_MODEL},
        provider_pins={module.B0_MODEL: "anthropic"},
    )
    assert "wrong_generation_provider_route" in reasons
    assert "wrong_generation_model_route" in reasons

    unit["provider_usage"]["router_metadata"]["endpoints"]["available"].append(
        {
            "provider": "OpenAI",
            "model": module.B4_MODEL,
            "selected": True,
        }
    )
    reasons = module.usage_route_reasons(
        {"model_usage_breakdown": [unit]},
        allowed_models={module.B0_MODEL},
        provider_pins={module.B0_MODEL: "anthropic"},
    )
    assert "conflicting_successful_router_receipt" in reasons
    assert module.unit_exact_non_byok(unit) is False


@pytest.mark.parametrize(
    ("requested_model", "serving_model", "upstream_provider"),
    [
        (
            "x-ai/grok-4.5",
            "x-ai/grok-4.5-20260708",
            "xai",
        ),
        (
            "anthropic/claude-sonnet-5",
            "anthropic/claude-sonnet-5-20260630",
            "anthropic",
        ),
    ],
)
def test_frozen_g1_serving_aliases_bind_router_receipts(
    module,
    requested_model: str,
    serving_model: str,
    upstream_provider: str,
) -> None:
    unit = {
        "role": "proposer",
        "provider": "openrouter",
        "model": requested_model,
        "requested_provider": "openrouter",
        "requested_model": requested_model,
        "provider_usage": {
            "router_metadata": {
                "requested": requested_model,
                "attempts": [
                    {
                        "provider": upstream_provider,
                        "model": serving_model,
                        "status": 200,
                    }
                ],
            },
        },
    }

    assert (
        module.usage_route_reasons(
            {"model_usage_breakdown": [unit]},
            allowed_models={requested_model},
            provider_pins={requested_model: upstream_provider},
        )
        == []
    )
    assert module._formal_openrouter_models_equivalent(
        requested_model,
        serving_model,
    )

    outside_snapshot = deepcopy(unit)
    outside_model = f"{serving_model}-outside-snapshot"
    outside_snapshot["provider_usage"]["router_metadata"]["attempts"][0]["model"] = outside_model
    assert not module._formal_openrouter_models_equivalent(
        requested_model,
        outside_model,
    )
    assert "router_receipt_model_not_bound_to_formal_route" in (
        module.usage_route_reasons(
            {"model_usage_breakdown": [outside_snapshot]},
            allowed_models={requested_model},
            provider_pins={requested_model: upstream_provider},
        )
    )


def test_registry_all_contract_resolves_the_complete_packaged_pool(module) -> None:
    routes = module.formal_registry_all_routes()

    assert len(routes) == module.FORMAL_G1_REGISTRY_ALL_CANDIDATE_COUNT == 80
    assert set(routes.values()) == {"auto"}
    assert all(model == model.strip().lower() and "/" in model for model in routes)


def test_auto_candidate_provider_accepts_any_successful_upstream_but_role_pin_remains(
    module,
) -> None:
    requested_model = "x-ai/grok-4.5"
    unit = {
        "role": "proposer",
        "provider": "openrouter",
        "model": requested_model,
        "requested_provider": "openrouter",
        "requested_model": requested_model,
        "provider_usage": {
            "requested_provider": "openrouter",
            "requested_model": requested_model,
            "router_metadata": {
                "requested_provider": "openrouter",
                "requested": requested_model,
                "attempts": [
                    {
                        "provider": "some-operational-upstream",
                        "model": requested_model,
                        "status": 200,
                    }
                ],
            },
        },
    }

    assert (
        module.usage_route_reasons(
            {"model_usage_breakdown": [unit]},
            allowed_models={requested_model},
            provider_pins={requested_model: "auto"},
        )
        == []
    )

    analyzer = deepcopy(unit)
    analyzer.update(
        {
            "role": "task_analyzer",
            "model": module.B0_MODEL,
            "requested_model": module.B0_MODEL,
        }
    )
    analyzer["provider_usage"]["requested_model"] = module.B0_MODEL
    analyzer["provider_usage"]["router_metadata"]["requested"] = module.B0_MODEL
    analyzer["provider_usage"]["router_metadata"]["attempts"][0]["model"] = module.B0_MODEL
    reasons = module.usage_route_reasons(
        {"model_usage_breakdown": [analyzer]},
        allowed_models={requested_model},
        provider_pins={module.B0_MODEL: "auto"},
        role_model_pins={"task_analyzer": module.B0_MODEL},
        role_provider_pins={
            "task_analyzer": module.FORMAL_UPSTREAM_PINS[module.B0_MODEL],
        },
    )
    assert "router_receipt_provider_not_bound_to_formal_route" in reasons


def test_registry_all_auto_preserves_concrete_runtime_provider_pins(module) -> None:
    pinned_model = "x-ai/grok-4.5"
    unpinned_model = "meta-llama/llama-4-scout"
    contract = {
        "resolved_llm_runtime": {
            "provider_routing": {
                pinned_model.upper(): "xAI",
            }
        },
        "g1_registry_contract": {
            "candidate_scope": "registry_all",
            "expected_routes": {
                pinned_model: "auto",
                unpinned_model: "auto",
            },
        },
    }

    assert module.contract_provider_pins(contract) == {
        pinned_model: "xai",
        unpinned_model: "auto",
    }

    unit = {
        "role": "proposer",
        "provider": "openrouter",
        "model": pinned_model,
        "requested_provider": "openrouter",
        "requested_model": pinned_model,
        "provider_usage": {
            "requested_provider": "openrouter",
            "requested_model": pinned_model,
            "router_metadata": {
                "requested_provider": "openrouter",
                "requested": pinned_model,
                "attempts": [
                    {
                        "provider": "different-upstream",
                        "model": pinned_model,
                        "status": 200,
                    }
                ],
            },
        },
    }
    reasons = module.usage_route_reasons(
        {"model_usage_breakdown": [unit]},
        allowed_models={pinned_model},
        provider_pins=module.contract_provider_pins(contract),
    )
    assert "router_receipt_provider_not_bound_to_formal_route" in reasons


def test_registry_all_plan_requires_all_registry_models_policy_and_pool(module) -> None:
    routes = module.formal_registry_all_routes()
    routes_hash = module.canonical_sha256(routes)
    profile_id = "test-registry-all"
    source_version = "curated-openrouter-step2-2026-07-24.3"
    filtered_version = f"{source_version}+{profile_id}+{routes_hash[:12]}"
    identities = sorted(f"openrouter:{model}" for model in routes)
    contract = {
        "profile_id": profile_id,
        "selection_mode": "router_dynamic",
        "candidate_scope": "registry_all",
        "policy": "all_registry_models",
        "user_profile_enabled": False,
        "source_registry_snapshot_version": source_version,
        "expected_routes": routes,
        "expected_routes_sha256": routes_hash,
        "expected_candidate_count": len(routes),
        "expected_source_registry_snapshot_sha256": (
            module.FORMAL_G1_SOURCE_REGISTRY_SNAPSHOT_SHA256
        ),
        "expected_ranking_config_schema_version": (module.FORMAL_G1_RANKING_CONFIG_SCHEMA_VERSION),
        "expected_ranking_config_version": module.FORMAL_G1_RANKING_CONFIG_VERSION,
        "expected_ranking_config_sha256": module.FORMAL_G1_RANKING_CONFIG_SHA256,
        "expected_proposer_count_max": module.FORMAL_G1_PROPOSER_COUNT_MAX,
    }
    plan = {
        "candidate_allowlist": {
            "policy": "all_registry_models",
            "profile_id": profile_id,
            "source_registry_snapshot_version": source_version,
            "expected_source_registry_snapshot_sha256": (
                module.FORMAL_G1_SOURCE_REGISTRY_SNAPSHOT_SHA256
            ),
            "filtered_registry_snapshot_version": filtered_version,
            "expected_routes_sha256": routes_hash,
            "expected_candidate_count": len(routes),
            "candidate_count": len(routes),
            "expected_identities": identities,
        },
        "candidate_pool_size": len(routes),
        "candidate_pool": [{"identity": identity} for identity in identities],
        "registry_snapshot_version": filtered_version,
        "registry_snapshot_hash": "d" * 64,
    }

    reasons, _, _ = module.g1_registry_plan_reasons(plan, contract=contract)
    assert "invalid_g1_registry_contract" not in reasons
    assert "wrong_g1_candidate_pool" not in reasons
    assert not any(reason.startswith("wrong_g1_candidate_allowlist") for reason in reasons)

    plan["candidate_allowlist"]["policy"] = "exact_openrouter_routes"
    reasons, _, _ = module.g1_registry_plan_reasons(plan, contract=contract)
    assert "wrong_g1_candidate_allowlist_policy" in reasons


def _owner_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    path.chmod(0o600)


def _receipt(
    response_id: str,
    model: str,
    *,
    cost: float = 0.1,
    exact: bool = True,
    is_byok: bool = False,
) -> dict[str, object]:
    upstream_provider = (
        "deepseek"
        if model.startswith("deepseek/")
        else "z-ai"
        if model.startswith("z-ai/")
        else "alibaba"
        if model.startswith("qwen/")
        else "moonshotai"
        if model.startswith("moonshotai/")
        else "anthropic"
        if model.startswith("anthropic/")
        else "openai"
        if model.startswith("openai/")
        else "google-ai-studio"
        if model.startswith("google/")
        else "upstream"
    )
    provider_usage: dict[str, object] = {
        "response_ids": [response_id],
        "provider_reported_cost": cost,
        "router_metadata": {
            "requested": model,
            "attempts": [
                {
                    "provider": upstream_provider,
                    "model": model,
                    "status": 200,
                }
            ],
        },
    }
    if exact:
        provider_usage.update(
            {
                "is_byok": is_byok,
            }
        )
        provider_usage["router_metadata"]["is_byok"] = is_byok
    return {
        "provider": "openrouter",
        "model": model,
        "requested_provider": "openrouter",
        "requested_model": model,
        "input_tokens": 10,
        "output_tokens": 5,
        "billed_cost": cost,
        "cost_source": "provider_billed",
        "provider_usage": provider_usage,
    }


def _ensemble_trace(
    proposers: list[str],
    aggregator: str,
    *,
    final_text: str,
    selection_mode: str,
    successful: int | None = None,
) -> dict[str, object]:
    def output_binding(text: str) -> dict[str, object]:
        return {
            "text": text,
            "chars": len(text),
            "truncated": False,
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }

    candidates = []
    for model in proposers:
        text = f"candidate from {model}"
        candidates.append(
            {
                "ok": True,
                "request_started": True,
                "physical_request_count": 1,
                "error": "",
                "provider": "openrouter",
                "model": model,
                "requested_provider": "openrouter",
                "requested_model": model,
                "content": {"text": text, "chars": len(text), "truncated": False},
            }
        )
    aggregator_identity = f"openrouter:{aggregator}"
    physical = {
        "agent_call_index": 1,
        "output_binding_schema": "opensquilla.ensemble-output-binding/v1",
        "assembled_output": output_binding(final_text),
        "output_components": [
            {
                "attempt": 1,
                "kind": "primary",
                "fallback_index": 0,
                "requested_provider": "openrouter",
                "requested_model": aggregator,
                "assembled_start": 0,
                "assembled_end": len(final_text),
                "physical_output": output_binding(final_text),
                "assembled_contribution": output_binding(final_text),
                "assembled_prefix_sha256": hashlib.sha256(final_text.encode("utf-8")).hexdigest(),
            }
        ],
        "request_outcome": "llm_response",
        "total_candidates": len(proposers),
        "successful_proposers": (len(proposers) if successful is None else successful),
        "fallback_used": False,
        "fallback_reason": "",
        "executed_A": aggregator_identity,
        "run_outcome": "success",
        "delivery_outcome": "complete",
        "final_request_role": "aggregator",
        "selection_plan": {
            "strategy": selection_mode,
            "selection_mode": selection_mode,
            "proposer_models": proposers,
            "aggregator_model": aggregator,
            "aggregator_candidates": [aggregator_identity],
        },
        "candidates": candidates,
        "aggregator_recovery": {
            "schema": "opensquilla.ensemble-aggregator-recovery/v1",
            "mode": "experiment",
            "candidate_count": 1,
            "candidate_ids": [aggregator_identity],
            "max_tokens_cap": 65_536,
            "visible_answer_reserve_tokens": 8_192,
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
                    "requested_provider": "openrouter",
                    "requested_model": aggregator,
                    "actual_provider": "openrouter",
                    "actual_model": aggregator,
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
        "final_request": {
            "role": "aggregator",
            "request_started": True,
            "error": "",
            "execution": {
                "actual_provider": "openrouter",
                "actual_model": aggregator,
                "requested_provider": "openrouter",
                "requested_model": aggregator,
            },
            "usage": {
                "provider": "openrouter",
                "model": aggregator,
                "requested_provider": "openrouter",
                "requested_model": aggregator,
            },
            "output": {
                "text": final_text,
                "chars": len(final_text),
                "truncated": False,
                "sha256": hashlib.sha256(final_text.encode("utf-8")).hexdigest(),
            },
        },
        "llm_request_count": len(proposers) + 1,
        "physical_request_count": len(proposers) + 1,
    }
    return {
        "mode": "agent_loop",
        "agent_llm_call_count": 1,
        "untraced_agent_llm_call_count": 0,
        "calls": [physical],
    }


def _nonterminal_fallback_call(
    terminal_call: dict[str, object],
    *,
    successful: int,
    output: str = "",
) -> dict[str, object]:
    call = deepcopy(terminal_call)
    call["agent_call_index"] = 1
    call["successful_proposers"] = successful
    call["fallback_used"] = True
    call["final_request_role"] = "fallback_single"
    call.pop("aggregator_recovery", None)
    call.pop("executed_A", None)
    call.pop("run_outcome", None)
    call.pop("delivery_outcome", None)
    candidates = call["candidates"]
    assert isinstance(candidates, list)
    for index, candidate in enumerate(candidates):
        assert isinstance(candidate, dict)
        if index < successful:
            continue
        candidate.update(
            {
                "ok": False,
                "error": "test proposer failure",
                "content": {"text": "", "chars": 0, "truncated": False},
            }
        )
    plan = call["selection_plan"]
    assert isinstance(plan, dict)
    fallback_model = str(plan["proposer_models"][0])
    final_request = call["final_request"]
    assert isinstance(final_request, dict)
    final_request.update(
        {
            "role": "fallback_single",
            "execution": {
                "role": "fallback_single",
                "provider": "openrouter",
                "actual_provider": "openrouter",
                "model": fallback_model,
                "actual_model": fallback_model,
                "requested_provider": "openrouter",
                "requested_model": fallback_model,
            },
            "usage": {
                "provider": "openrouter",
                "model": fallback_model,
                "requested_provider": "openrouter",
                "requested_model": fallback_model,
                "stop_reason": "stop",
            },
            "output": {
                "text": output,
                "chars": len(output),
                "truncated": False,
                "sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
            },
        }
    )
    call["assembled_output"] = deepcopy(final_request["output"])
    call["output_components"] = []
    return call


def _ranked_aggregator_fallback_call(
    plan: dict[str, object],
    *,
    final_text: str,
    fallback_index: int,
    trigger: str = "reasoning_only_length",
) -> dict[str, object]:
    proposers = [str(value) for value in plan["proposer_models"]]
    primary_model = str(plan["aggregator_model"])
    call = _ensemble_trace(
        proposers,
        primary_model,
        final_text=final_text,
        selection_mode="router_dynamic",
    )["calls"][0]
    assert isinstance(call, dict)
    call["selection_plan"] = deepcopy(plan)
    chain = [str(value) for value in plan["aggregator_candidates"]]
    selected_identity = chain[fallback_index]
    selected_provider, _, selected_model = selected_identity.partition(":")
    primary_provider, _, primary_model = chain[0].partition(":")
    attempts = [
        {
            "attempt": 1,
            "kind": "primary",
            "fallback_index": 0,
            "trigger": trigger,
            "request_started": True,
            "physical_attempt_index": 1,
            "physical_request_count": 1,
            "visible_output_emitted": False,
            "stream_closed": True,
            "outcome": "abandoned",
            "requested_provider": primary_provider,
            "requested_model": primary_model,
        }
    ]
    if fallback_index == 2:
        skipped_provider, _, skipped_model = chain[1].partition(":")
        attempts.append(
            {
                "attempt": 2,
                "kind": "model_fallback",
                "fallback_index": 1,
                "trigger": trigger,
                "request_started": False,
                "physical_attempt_index": None,
                "physical_request_count": 0,
                "outcome": "member_unavailable",
                "requested_provider": skipped_provider,
                "requested_model": skipped_model,
            }
        )
    selected_attempt = len(attempts) + 1
    attempts.append(
        {
            "attempt": selected_attempt,
            "kind": "model_fallback",
            "fallback_index": fallback_index,
            "trigger": trigger,
            "request_started": True,
            "physical_attempt_index": 2,
            "physical_request_count": 1,
            "visible_output_emitted": True,
            "stream_closed": True,
            "outcome": "succeeded",
            "requested_provider": selected_provider,
            "requested_model": selected_model,
            "actual_provider": selected_provider,
            "actual_model": selected_model,
        }
    )
    call.update(
        {
            "fallback_used": True,
            "fallback_reason": trigger,
            "executed_A": selected_identity,
            "run_outcome": "aggregator_recovered",
            "delivery_outcome": "complete",
            "aggregator_recovery": {
                "schema": "opensquilla.ensemble-aggregator-recovery/v1",
                "mode": "experiment",
                "candidate_count": len(chain),
                "candidate_ids": chain,
                "max_tokens_cap": 65_536,
                "visible_answer_reserve_tokens": 8_192,
                "attempts": attempts,
                "proposer_reused": True,
                "success": True,
                "degraded": False,
                "selected_attempt": selected_attempt,
                "selected_kind": "model_fallback",
                "fallback_index": fallback_index,
                "fallback_reason": trigger,
                "executed_A": selected_identity,
                "continuation_count": 0,
                "same_model_recovery_count": 0,
            },
            "llm_request_count": len(proposers) + 2,
            "physical_request_count": len(proposers) + 2,
        }
    )
    final_request = call["final_request"]
    assert isinstance(final_request, dict)
    final_request.update(
        {
            "role": "aggregator",
            "execution": {
                "provider": selected_provider,
                "model": selected_model,
                "actual_provider": selected_provider,
                "actual_model": selected_model,
                "requested_provider": selected_provider,
                "requested_model": selected_model,
            },
            "usage": {
                "provider": selected_provider,
                "model": selected_model,
                "requested_provider": selected_provider,
                "requested_model": selected_model,
            },
        }
    )
    output = final_request["output"]
    assert isinstance(output, dict)
    call["output_components"] = [
        {
            "attempt": selected_attempt,
            "kind": "model_fallback",
            "fallback_index": fallback_index,
            "requested_provider": selected_provider,
            "requested_model": selected_model,
            "assembled_start": 0,
            "assembled_end": len(final_text),
            "physical_output": deepcopy(output),
            "assembled_contribution": deepcopy(call["assembled_output"]),
            "assembled_prefix_sha256": hashlib.sha256(final_text.encode("utf-8")).hexdigest(),
        }
    ]
    return call


def _contract(module, group: str, key_hash: str) -> dict[str, object]:
    fixed = {
        "B0": "anthropic/claude-opus-4.8",
        "B4": "openai/gpt-5.5",
    }
    blocked_domains = list(module.FORMAL_BLOCKED_DOMAINS)
    finalization_policy = dict(module.FORMAL_AGENT_FINALIZATION_POLICY)
    model_thinking_levels = dict(module.FORMAL_MODEL_THINKING_LEVELS)
    contract: dict[str, object] = {
        "schema": "opensquilla.draco.run-compatibility/v1",
        "benchmark": "DRACO",
        "group": group,
        "group_spec": (
            {"kind": "single", "model": fixed[group]}
            if group in fixed
            else {"kind": "router_single"}
            if group == "B1"
            else {
                "kind": "selection_mode",
                "selection_mode": ("static_openrouter_b5" if group == "B2" else "router_dynamic"),
            }
        ),
        "runner": {
            "mode": "agent_loop",
            "agent_max_iterations": 20,
            "finalization_policy": finalization_policy,
        },
        "tools": {
            "tool_mode": "local_web_tools",
            "tools_enabled": True,
            "tool_names": ["web_search", "web_fetch"],
            "local_web_tools": {
                "web_search": {
                    "excluded_domains": blocked_domains,
                    "max_results": 5,
                    "provider": "brave",
                    "api_key_env": "BRAVE_SEARCH_API_KEY",
                },
                "web_fetch": {
                    "blocked_domains": blocked_domains,
                    "max_content_tokens": 50_000,
                    "max_content_chars": 200_000,
                    "allow_firecrawl": False,
                },
            },
            "contamination_blocked_domains": blocked_domains,
            "contamination_controls": {
                "status": "enforced_by_local_web_tools",
                "web_search_field": "excluded_domains_query_and_result_filter",
                "web_fetch_field": "blocked_domains",
            },
        },
        "generation": {
            "policy": {
                "generation_thinking": "model_max",
                "temperature": 0.0,
                "thinking_enabled": True,
                "thinking_level": "model-specific",
                "default_thinking_level": "xhigh",
                "thinking_budget_tokens": 50_000,
                "max_thinking_budget_tokens": 50_000,
                "max_tokens": 16_384,
                "max_tokens_overridden": True,
                "model_thinking_levels": model_thinking_levels,
                "require_highest_thinking": True,
                "applies_to": "single baselines and ensemble members",
            },
            "max_attempts": 3,
            "retry_backoff_seconds": 2.0,
        },
        "judge": {
            "model": "google/gemini-3.1-pro-preview",
            "repeats": 3,
            "max_attempts": 3,
            "judge_candidates": False,
        },
        "timeouts": {
            "task_seconds": 10_800.0,
            "proposer_seconds": 907.5,
            "aggregator_seconds": 2662.5,
            "proposer_early_stop_success_count": 0,
            "proposer_early_stop_after_seconds": 0.0,
            "expand_to_task_timeout": False,
        },
        "resolved_llm_runtime": {
            "provider": "openrouter",
            "api_key_sha256": f"sha256:{key_hash}",
            "base_url": "https://openrouter.ai/api/v1",
            "base_url_from_env": False,
            "proxy": "",
            "provider_routing": dict(module.FORMAL_UPSTREAM_PINS),
            "provider_routing_strict": True,
            "stream_error_frames": True,
            "router_metadata_required": True,
            "require_parameters": True,
            "response_cache_disabled": True,
            "key_exclusive": True,
            "cache_namespace_enabled": False,
            "cache_namespace_required": False,
            "cache_namespace_sha256": "",
            "trust_env": False,
            "ambient_proxies": {},
        },
        "cost_policy": {"require_openrouter_non_byok": True},
        "global_experiment_profile": {
            "schema_version": 1,
            "profile_id": "opensquilla_b2_quality_first_v2",
            "benchmark_input": {
                "sha256": module.FROZEN_DRACO_MINI_SHA256,
                "task_count": module.FROZEN_DRACO_MINI_TASK_COUNT,
                "enforce_reference_input": True,
            },
            "timeouts": {
                "task_seconds": 10_800.0,
                "proposer_seconds": 907.5,
                "aggregator_seconds": 2662.5,
                "task_margin_seconds": 30.0,
            },
            "runner": {
                "mode": "agent_loop",
                "agent_max_iterations": 20,
                **finalization_policy,
            },
            "generation": {
                "thinking_enabled": True,
                "thinking_budget_tokens": 50_000,
                "default_thinking_level": "xhigh",
                "model_thinking_levels": model_thinking_levels,
                "require_highest_thinking": True,
                "temperature": 0.0,
                "max_tokens": 16_384,
                "max_attempts": 3,
                "retry_backoff_seconds": 2.0,
            },
            "tools": {
                "mode": "local_web_tools",
                "sandbox_enabled": False,
                "contamination_blocked_domains": blocked_domains,
                "web_search": {
                    "provider": "brave",
                    "api_key_env": "BRAVE_SEARCH_API_KEY",
                    "max_results": 5,
                },
                "web_fetch": {"max_content_tokens": 50_000},
            },
            "judge": {
                "model": "google/gemini-3.1-pro-preview",
                "repeats": 3,
                "max_attempts": 3,
                "judge_candidates": False,
            },
        },
        "formal_runtime_freeze": {
            "source": "experiment_config",
            "sandbox_enabled": False,
            "sandbox_security_grading_enabled": False,
            **module.FORMAL_AGGREGATOR_RECOVERY_POLICY,
            "g1_user_profile_generation_enabled": False,
            "g1_user_profile_enabled": False,
        },
        "gateway_execution": {
            "llm_ensemble": dict(module.FORMAL_AGGREGATOR_RECOVERY_POLICY),
        },
        "dry_run": False,
    }
    if group == "B1":
        contract["gateway_execution"]["squilla_router"] = {
            "tiers": {
                "c0": {
                    "provider": "openrouter",
                    "model": "deepseek/deepseek-v4-flash",
                },
                "c1": {
                    "provider": "openrouter",
                    "model": "deepseek/deepseek-v4-pro",
                },
                "c2": {"provider": "openrouter", "model": "z-ai/glm-5.2"},
                "c3": {
                    "provider": "openrouter",
                    "model": "anthropic/claude-opus-4.8",
                },
                "image_model": {
                    "provider": "openrouter",
                    "model": "moonshotai/kimi-k2.6",
                },
            }
        }
    if group == "G1":
        routes = {
            "deepseek/deepseek-v4-pro": "deepseek",
            "z-ai/glm-5.2": "z-ai",
            "qwen/qwen3.7-max": "alibaba",
        }
        contract["g1_registry_contract"] = {
            "profile_id": "test-g1",
            "selection_mode": "router_dynamic",
            "user_profile_enabled": False,
            "source_registry_snapshot_version": "test-registry-v1",
            "expected_routes": routes,
            "expected_routes_sha256": module.canonical_sha256(routes),
            "expected_candidate_count": len(routes),
            "expected_source_registry_snapshot_sha256": (
                module.FORMAL_G1_SOURCE_REGISTRY_SNAPSHOT_SHA256
            ),
            "expected_ranking_config_schema_version": (
                module.FORMAL_G1_RANKING_CONFIG_SCHEMA_VERSION
            ),
            "expected_ranking_config_version": (module.FORMAL_G1_RANKING_CONFIG_VERSION),
            "expected_ranking_config_sha256": (module.FORMAL_G1_RANKING_CONFIG_SHA256),
            "expected_proposer_count_max": (module.FORMAL_G1_PROPOSER_COUNT_MAX),
        }
    return contract


def _test_ranking_config(module) -> dict[str, object]:
    from opensquilla.provider.ranking_router import ranking_config_snapshot

    config = deepcopy(ranking_config_snapshot())
    # Make this tiny three-model fixture select all three proposers while still
    # exercising the complete production ranking configuration.
    config["proposer_count"]["by_tier"]["3"] = {"min": 3, "max": 3}
    return config


def _g1_registry_row(
    model_id: str,
    *,
    vendor: str,
    capability: float,
    aggregator_fit: float,
) -> dict[str, object]:
    return {
        "source": "test_registry",
        "runtime": {"thinking": "off"},
        "registry_facts": {
            "model_id": model_id,
            "version": "test-v1",
            "provider": "openrouter",
            "vendor": vendor,
            "family": model_id,
            "is_open_source": False,
            "is_chinese_model": True,
            "supports_reasoning": True,
            "supports_tools": True,
            "status": "enabled",
            "roles": ["proposer", "aggregator"],
            "context_window": 256_000,
            "effective_context_bucket": "extra_long",
            "modalities": ["text"],
            "tools": [],
            "price": {
                "input_per_million": 1.0,
                "output_per_million": 1.0,
            },
            "latency_p50_ms": 1_000,
            "latency_p95_ms": 2_000,
            "quota": "available",
            "rate_limit": "available",
            "health": "healthy",
            "credential_available": True,
        },
        "static_profile": {
            "capability_dist_prior": {
                "reasoning": capability,
                "code_generation": capability,
                "format_following": capability,
            },
            "domain_dist_prior": {
                "software_engineering": capability,
                "general": capability,
            },
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


def _g1_plan(module, proposers: list[str], aggregator: str) -> dict[str, object]:
    from opensquilla.provider.ranking_router import (
        TaskAnalysisResult,
        build_request_context,
        fallback_task_profile,
        rank_models,
    )

    routes = {
        "deepseek/deepseek-v4-pro": "deepseek",
        "z-ai/glm-5.2": "z-ai",
        "qwen/qwen3.7-max": "alibaba",
    }
    routes_hash = module.canonical_sha256(routes)
    identities = [f"openrouter:{model}" for model in routes]
    version = f"test-registry-v1+test-g1+{routes_hash[:12]}"
    ranking_config = _test_ranking_config(module)
    request_context = build_request_context(
        message="research this",
        turn_metadata={},
        attachments=[],
        candidate_output_tokens=2_000,
        aggregator_output_tokens=2_000,
        ranking_config=ranking_config,
    )
    task_profile = fallback_task_profile(
        routed_tier="c2",
        request_context=request_context,
        ranking_config=ranking_config,
    )
    preference_order = {model: len(proposers) - index for index, model in enumerate(proposers)}
    registry_snapshot = {
        "schema_version": "test-registry",
        "snapshot_version": version,
        "models": [
            _g1_registry_row(
                model,
                vendor=vendor,
                capability=0.82 + 0.04 * preference_order.get(model, 0),
                aggregator_fit=0.99 if model == aggregator else 0.70,
            )
            for model, vendor in routes.items()
        ],
    }
    decision = rank_models(
        task_analysis=TaskAnalysisResult(
            profile=task_profile,
            source="test",
            schema_valid=True,
            confidence=0.9,
            provider_id="openrouter",
            model_id=module.B0_MODEL,
        ),
        user_profile=None,
        request_context=request_context,
        registry_snapshot=registry_snapshot,
        routed_tier="c2",
        routing_confidence=0.9,
        ranking_config=ranking_config,
        decision_id="test-g1-decision",
    )
    plan = deepcopy(decision.trace)
    selected_proposers = [str(identity).partition(":")[2] for identity in plan["selected_P"]]
    selected_aggregator = str(plan["selected_A"]).partition(":")[2]
    plan.update(
        {
            "selection_mode": "router_dynamic",
            "proposer_models": selected_proposers,
            "proposer_sample_count": len(selected_proposers),
            "aggregator_model": selected_aggregator,
            **module.FORMAL_AGGREGATOR_RECOVERY_POLICY,
            "candidate_allowlist": {
                "policy": "exact_openrouter_routes",
                "profile_id": "test-g1",
                "source_registry_snapshot_version": "test-registry-v1",
                "expected_source_registry_snapshot_sha256": (
                    module.FORMAL_G1_SOURCE_REGISTRY_SNAPSHOT_SHA256
                ),
                "filtered_registry_snapshot_version": version,
                "expected_routes_sha256": routes_hash,
                "expected_candidate_count": 3,
                "candidate_count": 3,
                "expected_identities": identities,
            },
        }
    )
    return plan


def _row(
    module,
    *,
    group: str,
    task: dict[str, object],
    fingerprint: str,
    response_prefix: str,
    quality: float = 85.0,
    exact: bool = True,
) -> dict[str, object]:
    model = {
        "B0": "anthropic/claude-opus-4.8",
        "B1": "deepseek/deepseek-v4-pro",
        "B2": "z-ai/glm-5.2",
        "B4": "openai/gpt-5.5",
        "G1": "z-ai/glm-5.2",
    }[group]
    generation_receipt = _receipt(
        f"{response_prefix}-generation",
        model,
        exact=exact,
    )
    generation_units = [generation_receipt]
    if group == "G1":
        analyzer_receipt = _receipt(
            f"{response_prefix}-task-analyzer",
            module.B0_MODEL,
            exact=exact,
        )
        analyzer_receipt["role"] = "task_analyzer"
        generation_units.insert(0, analyzer_receipt)
    generation_request_count = len(generation_units)
    generation_cost = 0.1 * generation_request_count
    final_text = f"answer for {group}"
    routing_trace: dict[str, object]
    ensemble_trace: dict[str, object]
    if group in {"B0", "B4"}:
        routing_trace = {"kind": "single", "selection": "fixed", "model": model}
        ensemble_trace = {
            "mode": "agent_loop",
            "agent_iterations": 1,
        }
    elif group == "B1":
        routing_trace = {
            "kind": "router_single",
            "routing_applied": True,
            "routed_model": model,
            "applied_model": model,
        }
        ensemble_trace = {"mode": "agent_loop", "agent_iterations": 1}
    elif group == "B2":
        routing_trace = {
            "kind": "selection_mode",
            "selection_mode": "static_openrouter_b5",
            "selection_plan": {
                "proposer_models": list(module.B2_PROPOSERS),
                "aggregator_model": module.B2_AGGREGATOR,
                "aggregator_candidates": [f"openrouter:{module.B2_AGGREGATOR}"],
            },
        }
        ensemble_trace = _ensemble_trace(
            list(module.B2_PROPOSERS),
            module.B2_AGGREGATOR,
            final_text=final_text,
            selection_mode="static_openrouter_b5",
        )
    else:
        proposers = [
            "deepseek/deepseek-v4-pro",
            "qwen/qwen3.7-max",
            "z-ai/glm-5.2",
        ]
        plan = _g1_plan(module, proposers, model)
        routing_trace = {
            "kind": "selection_mode",
            "selection_mode": "router_dynamic",
            "selection_plan": plan,
        }
        ensemble_trace = _ensemble_trace(
            proposers,
            model,
            final_text=final_text,
            selection_mode="router_dynamic",
        )
        call = ensemble_trace["calls"][0]
        call["selection_plan"] = deepcopy(plan)
        recovery = call["aggregator_recovery"]
        recovery["candidate_ids"] = list(plan["aggregator_candidates"])
        recovery["candidate_count"] = len(plan["aggregator_candidates"])
    rubric = task["rubric"]
    criteria = rubric["sections"][0]["criteria"]
    judgments = []
    for repeat in range(module.JUDGE_REPEATS):
        for criterion_index, criterion in enumerate(criteria):
            judge_attempt_number = (
                1000 + module.GROUPS.index(group) * 100 + repeat * 10 + criterion_index
            )
            met = criterion_index == 0 or quality >= 100
            judge_receipt = _receipt(
                f"{response_prefix}-judge-{repeat}-{criterion_index}",
                module.JUDGE_MODEL,
                exact=exact,
            )
            met_verdict = "MET" if met else "UNMET"
            judge_run = {
                "error": "",
                "llm_request_count": 1,
                "usage": {
                    "model_usage_breakdown": [judge_receipt],
                },
            }
            judgments.append(
                {
                    **criterion,
                    "section_id": rubric["sections"][0]["id"],
                    "section_title": rubric["sections"][0]["title"],
                    "repeat_index": repeat,
                    "verdict": met_verdict,
                    "met": met,
                    "error": "",
                    "judge_run": deepcopy(judge_run),
                    "judge_attempt_evidence_schema": (module.JUDGE_ATTEMPT_EVIDENCE_SCHEMA),
                    "judge_attempt_budget_scope": (module.JUDGE_ATTEMPT_BUDGET_SCOPE),
                    "judge_attempt_budget_limit": 3,
                    "prior_judge_attempts_used": 0,
                    "judge_attempt_count": 1,
                    "judge_attempt_budget_used": 1,
                    "judge_attempt_budget_remaining": 2,
                    "judge_new_attempt_count": 1,
                    "judge_attempt_budget_exhausted": False,
                    "judge_attempts": [
                        {
                            "attempt_id": f"{judge_attempt_number:032x}",
                            "attempt": 1,
                            "verdict": met_verdict,
                            "met": met,
                            "retry_suppressed_reason": "",
                            "run": judge_run,
                        }
                    ],
                }
            )
    row = {
        "task_id": task["id"],
        "group": group,
        "domain": "test",
        "prompt": task["prompt"],
        "prompt_sha256": module.text_sha256(str(task["prompt"])),
        "task_input_sha256": module.canonical_sha256(task, prefix=True),
        "run_compatibility_fingerprint": fingerprint,
        "provider_spec": {"kind": "single", "model": model},
        "routing_trace": routing_trace,
        "runner_mode": "agent_loop",
        "tools_enabled": True,
        "tool_policy": {
            "tool_mode": "local_web_tools",
            "local_web_tools": {
                "web_search": {"provider": "brave"},
                "web_fetch": {"allow_firecrawl": False},
            },
        },
        "generation_policy": {"max_attempts": 3},
        "generation_config": {"max_tokens": 16384, "temperature": 0},
        "started_at": 1_000.0,
        "generation_completed_at": 1_005.0,
        "completed_at": 1_010.0,
        "llm_request_count": generation_request_count,
        "selected_generation_succeeded": True,
        "generation_attempt_count": 1,
        "generation_attempt_evidence_schema": (module.GENERATION_ATTEMPT_EVIDENCE_SCHEMA),
        "generation_attempt_budget_limit": 3,
        "generation_attempt_budget_used": 1,
        "generation_attempt_total_billed_cost": generation_cost,
        "actual_spend_metrics": {
            "generation_attempt_count": 1,
            "total_tool_call_count": 0,
        },
        "error": (None if exact else "openrouter_non_byok_metadata_incomplete"),
        "final_text": final_text,
        "final_text_chars": len(final_text),
        "final_text_sha256": module.text_sha256(final_text),
        "usage": {
            "model_usage_breakdown": generation_units,
            "billed_cost": generation_cost,
        },
        "execution": {
            "run_error": "",
            "prior_generation_attempts_used": 0,
            "generation_attempts": [
                {
                    "attempt_id": (f"{module.GROUPS.index(group) + 1:032x}"),
                    "attempt_kind": "generation",
                    "started_at": 1_000.0,
                    "completed_at": 1_005.0,
                    "attempt": 1,
                    "retry_reason": "",
                    "run": {
                        "error": "",
                        "final_text_sha256": module.text_sha256(final_text),
                        "llm_request_count": generation_request_count,
                        "usage": {
                            "model_usage_breakdown": generation_units,
                        },
                    },
                }
            ],
        },
        "ensemble_trace": ensemble_trace,
        "judge": {
            "mode": "draco_criterion_judgments",
            "rubric_id": rubric["id"],
            "judge_model": module.JUDGE_MODEL,
            "judge_repeats": module.JUDGE_REPEATS,
            "rubric_criteria_count": len(criteria),
            "score_status": "complete",
            "judge_error_count": 0,
            "normalized_score": quality,
            "pass_rate": 50.0,
            "valid_pass_rate": 50.0,
            "criteria_count": len(judgments),
            "valid_criteria_count": len(judgments),
            "invalid_criteria_count": 0,
            "criterion_judgments": judgments,
            "judge_attempt_evidence_schema": (module.JUDGE_ATTEMPT_EVIDENCE_SCHEMA),
            "judge_attempt_budget_scope": module.JUDGE_ATTEMPT_BUDGET_SCOPE,
            "judge_attempt_budget_limit_per_unit": 3,
            "judge_attempt_count": len(judgments),
            "judge_new_attempt_count": len(judgments),
            "judge_attempt_budget_exhausted_count": 0,
            "judge_attempt_budget_exhausted": False,
        },
        "quality_total": quality,
        "completion_status": {
            "generation_accepted": True,
            "judge_complete": True,
            "cost_metadata_complete": exact,
            "status": "complete" if exact else "incomplete",
        },
        "cost_accounting": {
            "selected_generation_attempt": {
                "recorded_cost_usd": generation_cost,
                "request_count": generation_request_count,
                "cost_complete": exact,
                "cost_exact": exact,
            },
            "actual_llm_cost_complete": exact,
            "actual_spend_cost_complete": exact,
        },
        "openrouter_non_byok_audit": {
            "pass": exact,
            "status": "exact" if exact else "metadata_incomplete",
            "policy_safe_to_continue": True,
        },
    }
    return module.seal_result_row(row)


def test_ensemble_gate_allows_only_empty_nonterminal_fallback(module) -> None:
    final_text = "final answer"
    trace = _ensemble_trace(
        list(module.B2_PROPOSERS),
        module.B2_AGGREGATOR,
        final_text=final_text,
        selection_mode="static_openrouter_b5",
    )
    terminal = deepcopy(trace["calls"][0])
    terminal["agent_call_index"] = 2
    fallback = _nonterminal_fallback_call(terminal, successful=2)
    row = {
        "final_text": final_text,
        "routing_trace": {
            "selection_plan": deepcopy(terminal["selection_plan"]),
        },
        "ensemble_trace": {
            "mode": "agent_loop",
            "agent_llm_call_count": 2,
            "untraced_agent_llm_call_count": 0,
            "calls": [fallback, terminal],
        },
    }

    assert (
        module.ensemble_gate(
            row,
            expected_proposers=module.B2_PROPOSERS,
            expected_aggregator=module.B2_AGGREGATOR,
        )
        == []
    )

    visible = deepcopy(row)
    visible["ensemble_trace"]["calls"][0]["final_request"]["output"] = {
        "text": "unsafe text",
        "chars": len("unsafe text"),
        "truncated": False,
    }
    assert "intermediate_fallback_visible_output" in module.ensemble_gate(
        visible,
        expected_proposers=module.B2_PROPOSERS,
        expected_aggregator=module.B2_AGGREGATOR,
    )

    wrong_model = deepcopy(row)
    fallback_request = wrong_model["ensemble_trace"]["calls"][0]["final_request"]
    fallback_request["usage"]["model"] = "outside/model"
    fallback_request["usage"]["requested_model"] = "outside/model"
    assert "wrong_intermediate_fallback_model" in module.ensemble_gate(
        wrong_model,
        expected_proposers=module.B2_PROPOSERS,
        expected_aggregator=module.B2_AGGREGATOR,
    )

    conflicting_execution = deepcopy(row)
    conflicting_execution["ensemble_trace"]["calls"][0]["final_request"]["execution"][
        "actual_model"
    ] = "outside/model"
    assert "wrong_intermediate_fallback_model" in module.ensemble_gate(
        conflicting_execution,
        expected_proposers=module.B2_PROPOSERS,
        expected_aggregator=module.B2_AGGREGATOR,
    )

    boolean_chars = deepcopy(row)
    boolean_chars["ensemble_trace"]["calls"][0]["final_request"]["output"]["chars"] = False
    assert "intermediate_fallback_visible_output" in (
        module.admissible_empty_nonterminal_fallback_reasons(
            boolean_chars["ensemble_trace"]["calls"][0],
            expected_proposers=module.B2_PROPOSERS,
        )
    )

    terminal_fallback = deepcopy(row)
    terminal_fallback["ensemble_trace"].update(
        {
            "agent_llm_call_count": 1,
            "calls": [deepcopy(fallback)],
        }
    )
    assert "missing_aggregator_recovery_evidence" in module.ensemble_gate(
        terminal_fallback,
        expected_proposers=module.B2_PROPOSERS,
        expected_aggregator=module.B2_AGGREGATOR,
    )

    prefix = "preface "
    visible_intermediate = deepcopy(row)
    visible_intermediate["final_text"] = prefix + final_text
    first = deepcopy(terminal)
    first["agent_call_index"] = 1
    first["final_request"]["output"] = {
        "text": prefix,
        "chars": len(prefix),
        "truncated": False,
        "sha256": module.text_sha256(prefix),
    }
    first["assembled_output"] = deepcopy(first["final_request"]["output"])
    first["output_components"][0].update(
        {
            "assembled_end": len(prefix),
            "physical_output": deepcopy(first["final_request"]["output"]),
            "assembled_contribution": deepcopy(first["final_request"]["output"]),
            "assembled_prefix_sha256": module.text_sha256(prefix),
        }
    )
    visible_intermediate["ensemble_trace"]["calls"] = [first, terminal]
    assert (
        module.ensemble_gate(
            visible_intermediate,
            expected_proposers=module.B2_PROPOSERS,
            expected_aggregator=module.B2_AGGREGATOR,
        )
        == []
    )
    visible_intermediate["ensemble_trace"]["calls"][0]["assembled_output"]["text"] = "tampered"
    assert "wrong_agent_call_output_binding" in module.ensemble_gate(
        visible_intermediate,
        expected_proposers=module.B2_PROPOSERS,
        expected_aggregator=module.B2_AGGREGATOR,
    )


@pytest.mark.parametrize("fallback_index", [1, 2])
def test_g1_ensemble_gate_accepts_only_frozen_ranked_aggregator_fallbacks(
    module,
    monkeypatch: pytest.MonkeyPatch,
    fallback_index: int,
) -> None:
    monkeypatch.setattr(
        module,
        "FORMAL_G1_RANKING_CONFIG_SHA256",
        module.canonical_sha256(_test_ranking_config(module)),
    )
    plan = _g1_plan(
        module,
        [
            "deepseek/deepseek-v4-pro",
            "qwen/qwen3.7-max",
            "z-ai/glm-5.2",
        ],
        "z-ai/glm-5.2",
    )
    final_text = "ranked fallback answer"
    call = _ranked_aggregator_fallback_call(
        plan,
        final_text=final_text,
        fallback_index=fallback_index,
    )
    row = {
        "final_text": final_text,
        "routing_trace": {"selection_plan": deepcopy(plan)},
        "ensemble_trace": {
            "mode": "agent_loop",
            "agent_llm_call_count": 1,
            "untraced_agent_llm_call_count": 0,
            "calls": [call],
        },
    }
    contract = _contract(module, "G1", "a" * 64)["g1_registry_contract"]

    reasons, _, primary_model = module.g1_registry_plan_reasons(plan, contract=contract)
    assert reasons == []
    assert primary_model == str(plan["selected_A"]).partition(":")[2]
    assert call["selection_plan"]["selected_A"] == plan["selected_A"]
    assert call["selection_plan"]["aggregator_model"] == plan["aggregator_model"]
    assert (
        module.ensemble_gate(
            row,
            expected_proposers=plan["proposer_models"],
            expected_aggregator=primary_model,
            allowed_models={
                "deepseek/deepseek-v4-pro",
                "z-ai/glm-5.2",
                "qwen/qwen3.7-max",
            },
        )
        == []
    )


@pytest.mark.parametrize(
    ("continuation_count", "expected_reason"),
    [(2, None), (0, "aggregator_continuation_fallback_count_mismatch")],
)
def test_formal_continuation_fallback_contract(
    module,
    continuation_count: int,
    expected_reason: str | None,
) -> None:
    plan = _g1_plan(
        module,
        [
            "deepseek/deepseek-v4-pro",
            "qwen/qwen3.7-max",
            "z-ai/glm-5.2",
        ],
        "z-ai/glm-5.2",
    )
    final_text = "assembled continuation fallback answer"
    call = _ranked_aggregator_fallback_call(
        plan,
        final_text=final_text,
        fallback_index=1,
        trigger="visible_length_continuations_exhausted",
    )
    recovery = call["aggregator_recovery"]
    recovery["selected_kind"] = "continuation_fallback"
    recovery["continuation_count"] = continuation_count
    recovery["attempts"][0]["visible_output_emitted"] = True
    recovery["attempts"][-1]["kind"] = "continuation_fallback"
    prefix = "assembled "
    remainder = final_text[len(prefix) :]

    def binding(text: str) -> dict[str, object]:
        return {
            "text": text,
            "chars": len(text),
            "truncated": False,
            "sha256": module.text_sha256(text),
        }

    final_request = call["final_request"]
    final_request["output"] = binding(remainder)
    selected_attempt = recovery["selected_attempt"]
    selected_fallback_index = recovery["fallback_index"]
    selected_identity = recovery["candidate_ids"][selected_fallback_index]
    selected_provider, _, selected_model = selected_identity.partition(":")
    call["output_components"] = [
        {
            "attempt": 1,
            "kind": "primary",
            "fallback_index": 0,
            "requested_provider": "openrouter",
            "requested_model": str(plan["aggregator_model"]),
            "assembled_start": 0,
            "assembled_end": len(prefix),
            "physical_output": binding(prefix),
            "assembled_contribution": binding(prefix),
            "assembled_prefix_sha256": module.text_sha256(prefix),
        },
        {
            "attempt": selected_attempt,
            "kind": "continuation_fallback",
            "fallback_index": selected_fallback_index,
            "requested_provider": selected_provider,
            "requested_model": selected_model,
            "assembled_start": len(prefix),
            "assembled_end": len(final_text),
            "physical_output": binding(remainder),
            "assembled_contribution": binding(remainder),
            "assembled_prefix_sha256": module.text_sha256(final_text),
        },
    ]
    row = {
        "final_text": final_text,
        "routing_trace": {"selection_plan": deepcopy(plan)},
        "ensemble_trace": {
            "mode": "agent_loop",
            "agent_llm_call_count": 1,
            "untraced_agent_llm_call_count": 0,
            "calls": [call],
        },
    }

    reasons = module.ensemble_gate(
        row,
        expected_proposers=plan["proposer_models"],
        expected_aggregator=str(plan["aggregator_model"]),
    )
    if expected_reason is None:
        assert reasons == []
    else:
        assert expected_reason in reasons


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("missing_schema", "missing_aggregator_output_binding_schema"),
        ("missing_components", "missing_aggregator_output_components"),
        ("component_gap", "noncontiguous_aggregator_output_components"),
        (
            "wrong_selected_physical",
            "wrong_selected_aggregator_physical_output_binding",
        ),
        (
            "wrong_prefix_hash",
            "wrong_aggregator_output_component_prefix_hash",
        ),
    ],
)
def test_formal_output_binding_fails_closed(
    module,
    mutation: str,
    expected_reason: str,
) -> None:
    final_text = "auditable assembled answer"
    trace = _ensemble_trace(
        list(module.B2_PROPOSERS),
        module.B2_AGGREGATOR,
        final_text=final_text,
        selection_mode="static_openrouter_b5",
    )
    call = trace["calls"][0]
    if mutation == "missing_schema":
        call.pop("output_binding_schema")
    elif mutation == "missing_components":
        call["output_components"] = []
    elif mutation == "component_gap":
        call["output_components"][0]["assembled_start"] = 1
    elif mutation == "wrong_selected_physical":
        call["output_components"][0]["physical_output"]["sha256"] = "f" * 64
    else:
        call["output_components"][0]["assembled_prefix_sha256"] = "f" * 64
    row = {
        "final_text": final_text,
        "routing_trace": {
            "selection_plan": deepcopy(call["selection_plan"]),
        },
        "ensemble_trace": trace,
    }

    reasons = module.ensemble_gate(
        row,
        expected_proposers=module.B2_PROPOSERS,
        expected_aggregator=module.B2_AGGREGATOR,
    )
    assert expected_reason in reasons


def test_formal_recovery_request_conservation_allows_nested_physical_usage(module) -> None:
    plan = _g1_plan(
        module,
        [
            "deepseek/deepseek-v4-pro",
            "qwen/qwen3.7-max",
            "z-ai/glm-5.2",
        ],
        "z-ai/glm-5.2",
    )
    final_text = "nested provider answer"
    call = _ranked_aggregator_fallback_call(
        plan,
        final_text=final_text,
        fallback_index=1,
    )
    attempts = call["aggregator_recovery"]["attempts"]
    attempts[0]["physical_request_count"] = 2
    attempts[1]["physical_attempt_index"] = 3
    call["llm_request_count"] += 1
    call["physical_request_count"] += 1
    row = {
        "final_text": final_text,
        "routing_trace": {"selection_plan": deepcopy(plan)},
        "ensemble_trace": {
            "mode": "agent_loop",
            "agent_llm_call_count": 1,
            "untraced_agent_llm_call_count": 0,
            "calls": [call],
        },
    }

    assert (
        module.ensemble_gate(
            row,
            expected_proposers=plan["proposer_models"],
            expected_aggregator=str(plan["aggregator_model"]),
        )
        == []
    )


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("unranked_execution", "aggregator_executed_identity_mismatch"),
        ("final_request_identity", "wrong_final_aggregator_usage_requested_identity"),
        ("judge_trigger", "nonstructural_aggregator_fallback_trigger"),
        ("candidate_trace_chain", "aggregator_recovery_candidates_mismatch"),
        ("skip_top2", "aggregator_fallback_skipped_ranked_candidate"),
        ("fallback_single", "final_request_not_aggregator"),
    ],
)
def test_g1_ensemble_gate_rejects_forged_or_semantic_aggregator_fallback(
    module,
    mutation: str,
    expected_reason: str,
) -> None:
    plan = _g1_plan(
        module,
        [
            "deepseek/deepseek-v4-pro",
            "qwen/qwen3.7-max",
            "z-ai/glm-5.2",
        ],
        "z-ai/glm-5.2",
    )
    final_text = "ranked fallback answer"
    call = _ranked_aggregator_fallback_call(
        plan,
        final_text=final_text,
        fallback_index=2 if mutation == "skip_top2" else 1,
    )
    if mutation == "unranked_execution":
        call["executed_A"] = "openrouter:outside/unranked"
    elif mutation == "final_request_identity":
        call["final_request"]["usage"]["requested_model"] = "outside/unranked"
    elif mutation == "judge_trigger":
        call["fallback_reason"] = "judge_low_score"
        call["aggregator_recovery"]["fallback_reason"] = "judge_low_score"
        for attempt in call["aggregator_recovery"]["attempts"]:
            attempt["trigger"] = "judge_low_score"
    elif mutation == "candidate_trace_chain":
        call["aggregator_recovery"]["candidate_ids"] = list(
            reversed(call["aggregator_recovery"]["candidate_ids"])
        )
    elif mutation == "skip_top2":
        call["aggregator_recovery"]["attempts"] = [
            attempt
            for attempt in call["aggregator_recovery"]["attempts"]
            if attempt["fallback_index"] != 1
        ]
    else:
        call["final_request_role"] = "fallback_single"
        call["final_request"]["role"] = "fallback_single"
    row = {
        "final_text": final_text,
        "routing_trace": {"selection_plan": deepcopy(plan)},
        "ensemble_trace": {
            "mode": "agent_loop",
            "agent_llm_call_count": 1,
            "untraced_agent_llm_call_count": 0,
            "calls": [call],
        },
    }

    reasons = module.ensemble_gate(
        row,
        expected_proposers=plan["proposer_models"],
        expected_aggregator=str(plan["aggregator_model"]),
    )
    assert expected_reason in reasons


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("missing_all_candidates", "missing_aggregator_candidate_chain"),
        ("missing_recovery", "missing_aggregator_recovery_evidence"),
        ("partial_salvage", "invalid_aggregator_recovery_selected_kind"),
        ("degraded", "degraded_aggregator_recovery_not_formal"),
        ("partial_delivery", "aggregator_delivery_not_complete"),
        ("wrong_run_outcome", "aggregator_run_outcome_selected_kind_mismatch"),
        ("duplicate_attempt", "invalid_aggregator_recovery_attempt_sequence"),
        ("selected_unstarted", "ambiguous_aggregator_recovery_selected_attempt"),
        ("proposer_attempt_kind", "invalid_aggregator_recovery_attempt_kind"),
        ("missing_physical_index", "invalid_aggregator_physical_attempt_index"),
        ("request_count_mismatch", "ensemble_request_count_mismatch"),
        ("hidden_proposer_request", "ensemble_physical_request_count_undercounted"),
    ],
)
def test_formal_ensemble_recovery_evidence_fails_closed(
    module,
    mutation: str,
    expected_reason: str,
) -> None:
    final_text = "formal answer"
    trace = _ensemble_trace(
        list(module.B2_PROPOSERS),
        module.B2_AGGREGATOR,
        final_text=final_text,
        selection_mode="static_openrouter_b5",
    )
    call = trace["calls"][0]
    recovery = call["aggregator_recovery"]
    selected_attempt = recovery["attempts"][0]
    if mutation == "missing_all_candidates":
        call["selection_plan"].pop("aggregator_candidates")
        recovery.pop("candidate_ids")
    elif mutation == "missing_recovery":
        call.pop("aggregator_recovery")
    elif mutation == "partial_salvage":
        recovery["selected_kind"] = "partial_salvage"
        recovery["degraded"] = True
        call["delivery_outcome"] = "partial_usable"
        call["run_outcome"] = "length_capped_usable"
    elif mutation == "degraded":
        recovery["degraded"] = True
    elif mutation == "partial_delivery":
        call["delivery_outcome"] = "partial_usable"
    elif mutation == "wrong_run_outcome":
        call["run_outcome"] = "aggregator_recovered"
    elif mutation == "duplicate_attempt":
        recovery["attempts"].append(deepcopy(selected_attempt))
    elif mutation == "selected_unstarted":
        selected_attempt["request_started"] = False
        selected_attempt["physical_attempt_index"] = None
    elif mutation == "proposer_attempt_kind":
        selected_attempt["kind"] = "proposer"
    elif mutation == "missing_physical_index":
        selected_attempt["physical_attempt_index"] = None
    elif mutation == "request_count_mismatch":
        call["physical_request_count"] -= 1
    else:
        call["candidates"][0]["physical_request_count"] += 1
    row = {
        "final_text": final_text,
        "routing_trace": {"selection_plan": deepcopy(call["selection_plan"])},
        "ensemble_trace": trace,
    }

    reasons = module.ensemble_gate(
        row,
        expected_proposers=module.B2_PROPOSERS,
        expected_aggregator=module.B2_AGGREGATOR,
    )

    assert expected_reason in reasons


def test_static_aggregator_chain_can_be_bound_by_execution_receipt(module) -> None:
    final_text = "formal static answer"
    trace = _ensemble_trace(
        list(module.B2_PROPOSERS),
        module.B2_AGGREGATOR,
        final_text=final_text,
        selection_mode="static_openrouter_b5",
    )
    call = trace["calls"][0]
    call["selection_plan"].pop("aggregator_candidates")
    row = {
        "final_text": final_text,
        "routing_trace": {"selection_plan": deepcopy(call["selection_plan"])},
        "ensemble_trace": trace,
    }

    assert (
        module.ensemble_gate(
            row,
            expected_proposers=module.B2_PROPOSERS,
            expected_aggregator=module.B2_AGGREGATOR,
        )
        == []
    )


def test_agent_call_sequence_hashes_full_segment_when_trace_text_is_clipped(
    module,
) -> None:
    first = "short"
    second = "long-" + ("x" * 9_000)
    calls = [
        {
            "output_binding_schema": module.ENSEMBLE_OUTPUT_BINDING_SCHEMA,
            "assembled_output": {
                "text": first,
                "chars": len(first),
                "truncated": False,
                "sha256": module.text_sha256(first),
            },
        },
        {
            "output_binding_schema": module.ENSEMBLE_OUTPUT_BINDING_SCHEMA,
            "assembled_output": {
                "text": second[:8_000],
                "chars": len(second),
                "truncated": True,
                "sha256": module.text_sha256(second),
            },
        },
    ]

    assert (
        module.agent_call_output_sequence_reasons(
            calls,
            final_text=first + second,
        )
        == []
    )
    calls[1]["assembled_output"]["sha256"] = module.text_sha256(second + "tampered")
    assert module.agent_call_output_sequence_reasons(
        calls,
        final_text=first + second,
    ) == ["wrong_agent_call_output_hash"]


def test_g1_candidate_chain_must_equal_unique_frozen_aggregator_top3(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        module,
        "FORMAL_G1_RANKING_CONFIG_SHA256",
        module.canonical_sha256(_test_ranking_config(module)),
    )
    plan = _g1_plan(
        module,
        [
            "deepseek/deepseek-v4-pro",
            "qwen/qwen3.7-max",
            "z-ai/glm-5.2",
        ],
        "z-ai/glm-5.2",
    )
    contract = _contract(module, "G1", "a" * 64)["g1_registry_contract"]
    assert module.g1_registry_plan_reasons(plan, contract=contract)[0] == []

    reordered = deepcopy(plan)
    reordered["aggregator_candidates"][1:] = reversed(reordered["aggregator_candidates"][1:])
    reasons, _, _ = module.g1_registry_plan_reasons(reordered, contract=contract)
    assert "wrong_g1_aggregator_candidate_chain" in reasons

    duplicated = deepcopy(plan)
    duplicated["aggregator_candidates"][2] = duplicated["aggregator_candidates"][1]
    reasons, _, _ = module.g1_registry_plan_reasons(duplicated, contract=contract)
    assert "wrong_g1_aggregator_candidate_chain" in reasons

    missing = deepcopy(plan)
    missing.pop("aggregator_candidates")
    for field in module.FORMAL_AGGREGATOR_RECOVERY_POLICY:
        missing.pop(field)
    reasons, _, _ = module.g1_registry_plan_reasons(missing, contract=contract)
    assert "missing_g1_aggregator_recovery_policy" in reasons


def test_g1_route_gate_reuses_one_provider_lifecycle_plan_and_analyzer(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.provider import ranking_router

    monkeypatch.setattr(
        module,
        "FORMAL_G1_RANKING_CONFIG_SHA256",
        module.canonical_sha256(_test_ranking_config(module)),
    )
    monkeypatch.setattr(
        ranking_router,
        "ranking_trace_replay_reasons",
        lambda _plan: [],
    )
    task = {
        "id": "task-1",
        "prompt": "research this",
        "rubric": {
            "id": "rubric-1",
            "sections": [
                {
                    "id": "quality",
                    "title": "Quality",
                    "criteria": [{"id": "core", "weight": 100, "requirement": "core"}],
                }
            ],
        },
    }
    contract = _contract(module, "G1", "a" * 64)
    row = _row(
        module,
        group="G1",
        task=task,
        fingerprint="fingerprint",
        response_prefix="g1-lifecycle",
    )
    routing = deepcopy(row["routing_trace"])
    plan = deepcopy(routing["selection_plan"])
    original_attempt = row["execution"]["generation_attempts"][0]
    original_units = row["usage"]["model_usage_breakdown"]
    analyzer = deepcopy(original_units[0])
    generation = deepcopy(original_units[1])
    selected_attempt_id = "f" * 32
    row["routing_trace"] = {}
    row["usage"] = {
        "model_usage_breakdown": [generation],
        "billed_cost": generation["billed_cost"],
    }
    row["llm_request_count"] = 1
    row["execution"].update(
        {
            "selected_generation_attempt": 2,
            "generation_attempts": [
                {
                    **deepcopy(original_attempt),
                    "attempt_id": "e" * 32,
                    "attempt": 1,
                    "run": {
                        "error": "retry",
                        "final_text_sha256": module.text_sha256("earlier answer"),
                        "llm_request_count": 1,
                        "routing_trace": routing,
                        "usage": {"model_usage_breakdown": [analyzer]},
                    },
                },
                {
                    "attempt_id": selected_attempt_id,
                    "attempt": 2,
                    "run": {
                        "error": "",
                        "final_text_sha256": row["final_text_sha256"],
                        "llm_request_count": 1,
                        "routing_trace": {},
                        "usage": {"model_usage_breakdown": [generation]},
                    },
                },
            ],
        }
    )
    row["ensemble_trace"]["calls"][0]["selection_plan"] = deepcopy(plan)

    assert module.route_reasons(row, group="G1", contract=contract) == []

    conflicting = deepcopy(row)
    conflicting["ensemble_trace"]["calls"][0]["selection_plan"]["decision_id"] = (
        "conflicting-decision"
    )
    assert "g1_lifecycle_plan_differs_from_physical_plan" in module.route_reasons(
        conflicting,
        group="G1",
        contract=contract,
    )

    missing_analyzer = deepcopy(row)
    missing_analyzer["execution"]["generation_attempts"][0]["run"]["usage"] = {
        "model_usage_breakdown": []
    }
    assert "missing_g1_task_analyzer_request" in module.route_reasons(
        missing_analyzer,
        group="G1",
        contract=contract,
    )


def test_legacy_b2_attempt_error_requires_terminal_reclassification_proof(
    module,
    tmp_path: Path,
) -> None:
    task = {
        "id": "task-1",
        "prompt": "research this",
        "rubric": {
            "id": "rubric-1",
            "sections": [
                {
                    "id": "quality",
                    "title": "Quality",
                    "criteria": [{"id": "core", "weight": 100, "requirement": "core"}],
                }
            ],
        },
    }
    row = _row(
        module,
        group="B2",
        task=task,
        fingerprint="fingerprint",
        response_prefix="b2-reclassified",
    )
    terminal = deepcopy(row["ensemble_trace"]["calls"][0])
    terminal["agent_call_index"] = 2
    fallback = _nonterminal_fallback_call(terminal, successful=2)
    row["ensemble_trace"].update(
        {
            "agent_llm_call_count": 2,
            "calls": [fallback, terminal],
        }
    )
    attempt = row["execution"]["generation_attempts"][0]
    attempt["run"]["error"] = module.LEGACY_TERMINAL_POLICY_ERROR
    attempt["retry_reason"] = module.LEGACY_TERMINAL_POLICY_ERROR
    row["error"] = None
    row["selected_generation_succeeded"] = True
    row["execution"]["run_error"] = ""
    row["execution"]["selected_generation_attempt"] = 1
    row["execution"]["generation_terminal_reclassification"] = {
        "schema": module.GENERATION_TERMINAL_RECLASSIFICATION_SCHEMA,
        "policy": "terminal_aggregator_with_empty_intermediate_fallback/v1",
        "original_error": module.LEGACY_TERMINAL_POLICY_ERROR,
        "selected_attempt_id": attempt["attempt_id"],
        "selected_attempt": attempt["attempt"],
        "terminal_call_index": 2,
        "intermediate_fallback_call_indexes": [1],
    }
    record = module.SourceRecord(
        path=tmp_path / "wave.jsonl",
        source_index=0,
        line=1,
        row=row,
    )

    assert module.bind_selected_generation_attempts([record], [record]) == {
        "B2/task-1": attempt["attempt_id"]
    }

    invalid = deepcopy(row)
    invalid["execution"]["generation_terminal_reclassification"]["schema"] = "wrong"
    invalid_record = module.SourceRecord(
        path=tmp_path / "wave.jsonl",
        source_index=0,
        line=1,
        row=invalid,
    )
    with pytest.raises(
        module.FinalizationError,
        match="not bound to exactly one successful physical generation attempt",
    ):
        module.bind_selected_generation_attempts([invalid_record], [invalid_record])


def _campaign(
    module,
    tmp_path: Path,
    *,
    exact: bool = True,
    with_repair: bool = True,
) -> tuple[argparse.Namespace, list[dict[str, object]], int]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    task = {
        "id": "task-1",
        "prompt": "research this",
        "rubric": {
            "id": "rubric-1",
            "sections": [
                {
                    "id": "quality",
                    "title": "Quality",
                    "criteria": [
                        {"id": "core", "weight": 85, "requirement": "core"},
                        {"id": "extra", "weight": 15, "requirement": "extra"},
                    ],
                }
            ],
        },
    }
    input_path = tmp_path / "mini.jsonl"
    input_path.write_text(json.dumps(task) + "\n")
    input_path.chmod(0o600)
    module.FROZEN_DRACO_MINI_TASK_COUNT = 1
    module.FROZEN_DRACO_MINI_SHA256 = module.file_sha256(input_path)
    module.FORMAL_G1_RANKING_CONFIG_SHA256 = module.canonical_sha256(_test_ranking_config(module))
    key_hash = "a" * 64
    contracts = {group: _contract(module, group, key_hash) for group in module.GROUPS}
    fingerprints = {
        group: module.canonical_sha256(contract, prefix=True)
        for group, contract in contracts.items()
    }
    rows = [
        _row(
            module,
            group=group,
            task=task,
            fingerprint=fingerprints[group],
            response_prefix=group.lower(),
            exact=exact,
        )
        for group in module.GROUPS
    ]
    wave1 = tmp_path / "wave1.jsonl"
    wave1.write_text("".join(json.dumps(row) + "\n" for row in rows))
    wave1.chmod(0o600)
    result_paths = [wave1]
    manifest_paths = []

    def write_source_manifest(
        path: Path, result_path: Path, source_rows: list[dict[str, object]]
    ) -> None:
        repair_shard = bool(source_rows) and all(
            isinstance(row.get("execution"), dict)
            and row["execution"].get("generation_reused") is True
            for row in source_rows
        )
        _owner_json(
            path,
            {
                "benchmark": "DRACO",
                "status": "complete",
                "started_at": 1_000.0,
                "finished_at": 1_030.0,
                "groups": list(module.GROUPS),
                "task_ids": [task["id"]],
                "rows_written": len(source_rows),
                "artifacts": {"results_jsonl": str(result_path.resolve())},
                "command": {
                    "parsed_args": {
                        "groups": "B0,B1,B2,B4,G1",
                        "max_tasks": module.FROZEN_DRACO_MINI_TASK_COUNT,
                        "concurrency": 5,
                        "judge_concurrency": 6,
                        "require_clean_source": True,
                        "dry_run": False,
                    }
                },
                "tool_policy": {
                    "local_web_tools": {
                        "preflight": {
                            "status": ("skipped_not_required" if repair_shard else "passed"),
                            "preflight_calls": {
                                "web_search": 0 if repair_shard else 1,
                                "web_fetch": 0 if repair_shard else 1,
                            },
                        },
                    },
                },
                **(
                    {
                        "resume_selection": {
                            "model_regenerate_pair_count": 0,
                        }
                    }
                    if repair_shard
                    else {}
                ),
                "run_compatibility": {
                    "fingerprints": fingerprints,
                    "contracts": contracts,
                },
            },
        )

    wave1_manifest = tmp_path / "wave1.manifest.json"
    write_source_manifest(wave1_manifest, wave1, rows)
    manifest_paths.append(wave1_manifest)
    if with_repair:
        repaired = []
        for row in rows:
            value = deepcopy(row)
            value["completed_at"] = 1_020.0
            value["execution"]["prior_generation_attempts_used"] = 1
            value["execution"]["resume_action"] = "metadata_only"
            value["execution"]["generation_reused"] = True
            value["execution"]["metadata_repaired"] = True
            value["execution"]["judge_reran"] = False
            value["resume_completion"] = {
                "action": "metadata_only",
                "generation_reused": True,
                "metadata_repaired": True,
                "judge_reran": False,
                "post_repair_action": "complete",
                "status": "complete",
                "incomplete_reasons": [],
            }
            for judgment in value["judge"]["criterion_judgments"]:
                judgment["prior_judge_attempts_used"] = 1
                judgment["judge_new_attempt_count"] = 0
            value["judge"]["judge_new_attempt_count"] = 0
            repaired.append(module.seal_result_row(value))
        wave2 = tmp_path / "wave2.jsonl"
        wave2.write_text("".join(json.dumps(row) + "\n" for row in repaired))
        wave2.chmod(0o600)
        result_paths.append(wave2)
        wave2_manifest = tmp_path / "wave2.manifest.json"
        write_source_manifest(wave2_manifest, wave2, repaired)
        manifest_paths.append(wave2_manifest)

    runtime_path = tmp_path / "runtime.json"
    environment = {"python": {"version": "test"}, "packages": []}
    runtime_sha = module.canonical_sha256(environment)
    _owner_json(
        runtime_path,
        {
            "schema": module.RUNTIME_SCHEMA,
            "captured_at": "1970-01-01T00:00:00+00:00",
            "environment_sha256": runtime_sha,
            "environment": environment,
        },
    )
    before = tmp_path / "before.json"
    after = tmp_path / "after.json"
    _owner_json(
        before,
        {
            "captured_at": "1970-01-01T00:16:30+00:00",
            "api_key_sha256": key_hash,
            "benchmark_environment_key_verified": True,
            "usage": "10",
            "byok_usage": "0",
            "is_free_tier": False,
        },
    )
    _owner_json(
        after,
        {
            "captured_at": "1970-01-01T00:20:30+00:00",
            "api_key_sha256": key_hash,
            "benchmark_environment_key_verified": True,
            "usage": "13.6",
            "byok_usage": "0",
            "is_free_tier": False,
        },
    )
    lock_path = tmp_path / "account.lock"
    lock_path.write_text("")
    lock_path.chmod(0o600)
    lock_fd = os.open(lock_path, os.O_RDWR)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    reconciliation = tmp_path / "reconciliation.json"
    stable_offsets = (0, 60, 120, 180, 195, 210)
    stable = [
        {
            "captured_at": (f"1970-01-01T00:{17 + offset // 60:02d}:{offset % 60:02d}+00:00"),
            "usage": "13.6",
            "byok_usage": "0",
        }
        for offset in stable_offsets
    ]
    _owner_json(
        reconciliation,
        {
            "schema": module.RECONCILIATION_SCHEMA,
            "created_at": "1970-01-01T00:20:30+00:00",
            "settlement_status": "stable",
            "api_key_sha256": key_hash,
            "usage_before_usd": "10",
            "usage_after_usd": "13.6",
            "usage_delta_usd": "3.6",
            "byok_usage_before_usd": "0",
            "byok_usage_after_usd": "0",
            "byok_usage_delta_usd": "0",
            "is_free_tier": False,
            "stable_poll_count": 6,
            "required_stable_poll_count": 6,
            "poll_observation_count": 6,
            "stable_tail_start_index": 0,
            "poll_interval_seconds": 15,
            "minimum_settlement_seconds": 180,
            "minimum_stable_tail_seconds": 75,
            "observation_span_seconds": 210.0,
            "stable_tail_span_seconds": 210.0,
            "stable_observations": stable,
            "runtime_environment_sha256": runtime_sha,
            "runtime_environment_file_sha256": module.file_sha256(runtime_path),
            "lock_file": str(lock_path.resolve()),
            "lock_inode": lock_path.stat().st_ino,
        },
    )
    args = argparse.Namespace(
        input=input_path,
        result=result_paths,
        manifest=manifest_paths,
        account_before=before,
        account_after=after,
        account_reconciliation=reconciliation,
        runtime_environment=runtime_path,
        lock_file=lock_path,
        lock_fd=lock_fd,
        output_dir=tmp_path / "final",
        groups="B0,B1,B2,B4,G1",
        max_generation_attempts=3,
    )
    return args, rows, lock_fd


def _prior_account_window(
    module,
    args: argparse.Namespace,
    tmp_path: Path,
    *,
    before_usage: str = "5.401561244",
    suffix: str = "prior-aborted",
) -> Path:
    window = tmp_path / "archive" / "account" / suffix
    window.mkdir(parents=True)
    runtime = window / "runtime-environment.json"
    runtime.write_bytes(args.runtime_environment.read_bytes())
    runtime.chmod(0o600)
    key_hash = json.loads(args.account_before.read_text())["api_key_sha256"]
    before = window / "openrouter-account-before.json"
    after = window / "openrouter-account-after.json"
    _owner_json(
        before,
        {
            "captured_at": "1970-01-01T00:10:00+00:00",
            "api_key_sha256": key_hash,
            "benchmark_environment_key_verified": True,
            "usage": before_usage,
            "byok_usage": "0",
            "is_free_tier": False,
        },
    )
    _owner_json(
        after,
        {
            "captured_at": "1970-01-01T00:15:00+00:00",
            "api_key_sha256": key_hash,
            "benchmark_environment_key_verified": True,
            "usage": "10",
            "byok_usage": "0",
            "is_free_tier": False,
        },
    )
    runtime_payload = json.loads(runtime.read_text())
    usage_delta = str(module.Decimal("10") - module.Decimal(before_usage))
    observations = [
        {
            "captured_at": value,
            "usage": "10",
            "byok_usage": "0",
        }
        for value in (
            "1970-01-01T00:11:00+00:00",
            "1970-01-01T00:12:00+00:00",
            "1970-01-01T00:13:00+00:00",
            "1970-01-01T00:14:00+00:00",
            "1970-01-01T00:14:45+00:00",
            "1970-01-01T00:15:00+00:00",
        )
    ]
    _owner_json(
        window / "openrouter-account-reconciliation.json",
        {
            "schema": module.RECONCILIATION_SCHEMA,
            "created_at": "1970-01-01T00:15:00+00:00",
            "settlement_status": "stable",
            "api_key_sha256": key_hash,
            "usage_before_usd": before_usage,
            "usage_after_usd": "10",
            "usage_delta_usd": usage_delta,
            "byok_usage_before_usd": "0",
            "byok_usage_after_usd": "0",
            "byok_usage_delta_usd": "0",
            "is_free_tier": False,
            "stable_poll_count": 6,
            "required_stable_poll_count": 6,
            "poll_observation_count": 6,
            "stable_tail_start_index": 0,
            "poll_interval_seconds": 15,
            "minimum_settlement_seconds": 180,
            "minimum_stable_tail_seconds": 75,
            "observation_span_seconds": 240.0,
            "stable_tail_span_seconds": 240.0,
            "stable_observations": observations,
            "runtime_environment_sha256": runtime_payload["environment_sha256"],
            "runtime_environment_file_sha256": module.file_sha256(runtime),
        },
    )
    return window


def _bind_prior_campaign_window(
    module,
    args: argparse.Namespace,
    tmp_path: Path,
) -> Path:
    prior = _prior_account_window(
        module,
        args,
        tmp_path,
        before_usage="6.4",
        suffix="prior-campaign",
    )
    prior_reconciliation_path = prior / "openrouter-account-reconciliation.json"
    prior_reconciliation = json.loads(prior_reconciliation_path.read_text())
    prior_reconciliation.update(
        {
            "lock_file": str(args.lock_file.resolve()),
            "lock_inode": args.lock_file.stat().st_ino,
        }
    )
    _owner_json(prior_reconciliation_path, prior_reconciliation)

    for source_index, result_path in enumerate(args.result):
        rows = [json.loads(line) for line in result_path.read_text().splitlines()]
        for row_index, row in enumerate(rows):
            # Both waves retain the immutable timing of the generation that
            # physically ran in wave 1.  Only the repair row completion belongs
            # to the current window.
            row["started_at"] = 660.0
            row["generation_completed_at"] = 665.0
            if source_index == 0:
                row["completed_at"] = 870.0
            execution = row["execution"]
            assert isinstance(execution, dict)
            attempts = execution["generation_attempts"]
            assert isinstance(attempts, list)
            for attempt in attempts:
                assert isinstance(attempt, dict)
                attempt["started_at"] = 660.0
                attempt["completed_at"] = 665.0
            rows[row_index] = module.seal_result_row(row)
        result_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        result_path.chmod(0o600)

    wave1_manifest = json.loads(args.manifest[0].read_text())
    wave1_manifest.update(
        {
            "started_at": 600.0,
            "finished_at": 900.0,
        }
    )
    _owner_json(args.manifest[0], wave1_manifest)

    account_after = json.loads(args.account_after.read_text())
    account_after["usage"] = "10"
    _owner_json(args.account_after, account_after)
    current_reconciliation = json.loads(args.account_reconciliation.read_text())
    current_reconciliation.update(
        {
            "usage_before_usd": "10",
            "usage_after_usd": "10",
            "usage_delta_usd": "0",
        }
    )
    for observation in current_reconciliation["stable_observations"]:
        observation["usage"] = "10"
    _owner_json(args.account_reconciliation, current_reconciliation)
    args.campaign_account_window_dir = [prior]
    return prior


def _prior_campaign_window_reconciliation(
    proof: dict[str, object],
) -> dict[str, dict[str, object]]:
    cost_scope = proof["cost_scope"]
    assert isinstance(cost_scope, dict)
    reconciliations = cost_scope["ledger_window_reconciliation"]
    assert isinstance(reconciliations, list)
    return {
        str(value["account_window_kind"]): value
        for value in reconciliations
        if isinstance(value, dict)
    }


def test_prior_campaign_window_reconciles_physical_first_sources(
    module,
    tmp_path: Path,
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path)
    prior = _bind_prior_campaign_window(module, args, tmp_path)
    try:
        manifest = module.run_finalization(args)
    finally:
        os.close(lock_fd)

    proof = json.loads((args.output_dir / "openrouter-non-byok-campaign-proof.json").read_text())
    assert [window["kind"] for window in proof["account_windows"]] == [
        "prior_campaign",
        "current",
    ]
    assert proof["result_row_account_window_scope"] == "campaign_windows"
    assert proof["account"]["campaign_usage_delta_usd"] == "3.6"
    assert proof["account"]["campaign_window_count"] == 2
    scope = proof["cost_scope"]
    assert scope["campaign_bound_account_window_total_usd"] == "3.6"
    assert scope["campaign_attributable_exact"] is True
    assert scope["campaign_attributable_cost_usd"] == "3.6"

    coverage = proof["window"]["source_window_coverage"]
    assert [
        (
            value["source_index"],
            value["account_window_kind"],
            value["physical_first_generation_attempt_count"],
        )
        for value in coverage
    ] == [
        (0, "prior_campaign", 5),
        (1, "current", 0),
    ]
    assert coverage[0]["account_window_path"] == str(prior.resolve())
    assert coverage[1]["account_window_path"] == str(tmp_path.resolve())

    reconciliations = _prior_campaign_window_reconciliation(proof)
    assert reconciliations["prior_campaign"] == {
        "account_window_path": str(prior.resolve()),
        "account_window_kind": "prior_campaign",
        "source_indexes": [0],
        "physical_request_count": 36,
        "ledger_recorded_cost_usd": "3.600000000",
        "ledger_exact_cost_usd": "3.600000000",
        "unknown_cost_request_count": 0,
        "non_exact_cost_request_count": 0,
        "account_usage_delta_usd": "3.6",
        "reconciliation_gap_usd": "0E-9",
        "reconciliation_status": "exact",
    }
    assert reconciliations["current"] == {
        "account_window_path": str(tmp_path.resolve()),
        "account_window_kind": "current",
        "source_indexes": [1],
        "physical_request_count": 0,
        "ledger_recorded_cost_usd": "0",
        "ledger_exact_cost_usd": "0",
        "unknown_cost_request_count": 0,
        "non_exact_cost_request_count": 0,
        "account_usage_delta_usd": "0",
        "reconciliation_gap_usd": "0",
        "reconciliation_status": "exact",
    }

    ledger = [
        json.loads(line)
        for line in (args.output_dir / "actual-spend-ledger.jsonl").read_text().splitlines()
    ]
    assert len(ledger) == 36
    assert {
        (
            row["physical_source"]["source_index"],
            row["physical_source"]["path"],
            tuple(row["receipt_source_indexes"]),
        )
        for row in ledger
    } == {
        (0, str(args.result[0]), (1,)),
    }

    report = (args.output_dir / "EXPERIMENT_RESULTS.md").read_text()
    assert "Generation disposition 成本：selected=$0.600000000" in report
    assert "账本已记录成本：$3.600000000" in report
    assert "Campaign-bound OpenRouter account delta：$3.6" in report
    assert "prior_campaign=36 requests/$3.6/exact" in report
    assert "current=0 requests/$0/exact" in report
    assert manifest["cost_attribution"]["campaign_bound_account_window_total_usd"] == "3.6"


@pytest.mark.parametrize(
    ("outside_evidence", "error_pattern"),
    [
        (
            "physical_attempt",
            "physical-first generation attempt is outside its source manifest",
        ),
        (
            "row_completion",
            "source result row completion is outside its manifest execution window",
        ),
    ],
)
def test_prior_campaign_rejects_source_timing_outside_manifest(
    module,
    tmp_path: Path,
    outside_evidence: str,
    error_pattern: str,
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path)
    _bind_prior_campaign_window(module, args, tmp_path)
    result_paths = args.result if outside_evidence == "physical_attempt" else args.result[:1]
    for result_path in result_paths:
        rows = [json.loads(line) for line in result_path.read_text().splitlines()]
        if outside_evidence == "physical_attempt":
            rows[0]["execution"]["generation_attempts"][0]["started_at"] = 599.0
        else:
            rows[0]["completed_at"] = 901.0
        rows[0] = module.seal_result_row(rows[0])
        result_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        result_path.chmod(0o600)
    try:
        with pytest.raises(module.FinalizationError, match=error_pattern):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)
    assert not args.output_dir.exists()


@pytest.mark.parametrize("binding", ["lock", "runtime"])
def test_prior_campaign_requires_same_runtime_and_lock(
    module,
    tmp_path: Path,
    binding: str,
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path)
    prior = _bind_prior_campaign_window(module, args, tmp_path)
    reconciliation_path = prior / "openrouter-account-reconciliation.json"
    reconciliation = json.loads(reconciliation_path.read_text())
    if binding == "lock":
        reconciliation["lock_inode"] = args.lock_file.stat().st_ino + 1
    else:
        runtime_path = prior / "runtime-environment.json"
        runtime = json.loads(runtime_path.read_text())
        runtime["environment"]["python"]["version"] = "different"
        runtime["environment_sha256"] = module.canonical_sha256(runtime["environment"])
        _owner_json(runtime_path, runtime)
        reconciliation["runtime_environment_sha256"] = runtime["environment_sha256"]
        reconciliation["runtime_environment_file_sha256"] = module.file_sha256(runtime_path)
    _owner_json(reconciliation_path, reconciliation)
    try:
        with pytest.raises(
            module.FinalizationError,
            match="not bound to the same runtime/lock",
        ):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)
    assert not args.output_dir.exists()


def test_full_offline_finalization_is_atomic_and_preserves_contracts(
    module, tmp_path: Path
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path)
    try:
        manifest = module.run_finalization(args)
    finally:
        os.close(lock_fd)
    assert manifest["status"] == "complete"
    assert manifest["result_count"] == 5
    output = args.output_dir
    assert sorted(path.name for path in output.iterdir()) == [
        "EXPERIMENT_RESULTS.md",
        "actual-spend-ledger.jsonl",
        "audit.json",
        "manifest.json",
        "openrouter-non-byok-campaign-proof.json",
        "results.jsonl",
        "trace.jsonl",
    ]
    final_rows = [json.loads(line) for line in (output / "results.jsonl").read_text().splitlines()]
    assert [row["group"] for row in final_rows] == list(module.GROUPS)
    assert all(module.verify_result_row_evidence(row) for row in final_rows)
    traces = [json.loads(line) for line in (output / "trace.jsonl").read_text().splitlines()]
    assert traces == [canonical_trace_row_from_result(row) for row in final_rows]
    selected_source_rows = [json.loads(line) for line in args.result[-1].read_text().splitlines()]
    by_group = {row["group"]: row for row in selected_source_rows}
    for final in final_rows:
        original = by_group[final["group"]]
        assert final["final_text"] == original["final_text"]
        assert final["usage"] == original["usage"]
        assert final["execution"] == original["execution"]
        assert final["cost_accounting"] == original["cost_accounting"]
        assert final["quality_total"] == 85.0
        assert final["openrouter_non_byok_resolution"]["campaign_proof_pass"] is True
    ledger = [
        json.loads(line) for line in (output / "actual-spend-ledger.jsonl").read_text().splitlines()
    ]
    # Six physical generation requests (G1 includes its analyzer) plus thirty
    # (2 criteria x 3 repeats)
    # physical Judge requests.
    # The repair wave repeats both but must not double-count them.
    assert len(ledger) == 36
    generation_rows = [row for row in ledger if "generation" in row["scopes"]]
    assert {row["generation_disposition"] for row in generation_rows} == {"selected"}
    audit = json.loads((output / "audit.json").read_text())
    assert audit["pass"] is True
    assert audit["result_count"] == 5
    assert (
        audit["selected_generation_cost"]["attribution_precision"] == "campaign-attributable-exact"
    )
    assert audit["external_tool_cost"]["task_generation_tool_call_count"] == 0
    assert audit["external_tool_cost"]["live_preflight_tool_call_count"] == 2
    assert audit["judge_attempt_evidence"]["unique_physical_judge_attempt_count"] == 30
    assert audit["selected_generation_cost_reconciliation"]["pair_count"] == 5
    report = (output / "EXPERIMENT_RESULTS.md").read_text()
    assert "Brave" in report
    assert "单任务超时 10800 秒，最多 12 轮；任务并发 5" in report
    assert "temperature=0，max_tokens=16384" in report
    assert "`google/gemini-3.1-pro-preview`，3 repeats，Judge 并发 6" in report
    assert "最多 3 次 attempt" in report
    assert "`research.perplexity.ai`" in report
    assert "只允许 non-BYOK" in report
    assert "response cache 关闭" in report
    assert "Rubric 分项平均" in report
    assert "修复动作明细" in report
    proof = json.loads((output / "openrouter-non-byok-campaign-proof.json").read_text())
    assert proof["cost_scope"]["campaign_attributable_exact"] is True
    assert proof["cost_scope"]["campaign_attributable_cost_usd"] == "3.6"
    assert proof["cost_scope"]["attribution_precision"] == "campaign-attributable-exact"
    assert [window["kind"] for window in proof["account_windows"]] == ["current"]
    assert proof["account_window_total_usd"] == "3.6"
    assert proof["unallocated_aborted_window_usd"] == "0"


def test_metadata_only_wave_may_replay_identical_judge_snapshot_declarations(
    module,
    tmp_path: Path,
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path)
    rows = [json.loads(line) for line in args.result[1].read_text().splitlines()]
    judge = rows[0]["judge"]
    declared_new = 0
    for judgment in judge["criterion_judgments"]:
        attempts = judgment["judge_attempts"]
        judgment["prior_judge_attempts_used"] = 0
        judgment["judge_new_attempt_count"] = len(attempts)
        declared_new += len(attempts)
    judge["judge_new_attempt_count"] = declared_new
    rows[0] = module.seal_result_row(rows[0])
    args.result[1].write_text("".join(json.dumps(row) + "\n" for row in rows))
    try:
        manifest = module.run_finalization(args)
    finally:
        os.close(lock_fd)

    assert manifest["status"] == "complete"


def test_prior_aborted_account_window_is_preserved_without_fake_ledger_rows(
    module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opensquilla.provider import ranking_router

    monkeypatch.setattr(
        ranking_router,
        "ranking_trace_replay_reasons",
        lambda _plan: [],
        raising=False,
    )
    args, _, lock_fd = _campaign(module, tmp_path)
    prior = _prior_account_window(module, args, tmp_path)
    args.prior_account_window_dir = [prior]
    try:
        manifest = module.run_finalization(args)
    finally:
        os.close(lock_fd)
    proof = json.loads((args.output_dir / "openrouter-non-byok-campaign-proof.json").read_text())
    scope = proof["cost_scope"]
    assert [window["kind"] for window in scope["account_windows"]] == [
        "prior_aborted",
        "current",
    ]
    assert scope["account_window_total_usd"] == "8.198438756"
    assert scope["unallocated_aborted_window_usd"] == "4.598438756"
    assert (
        scope["attribution_precision"] == "multi-window-counter-exact-campaign-attribution-unproven"
    )
    assert scope["campaign_attributable_exact"] is False
    assert scope["campaign_attributable_cost_usd"] is None
    assert proof["result_row_account_window_scope"] == "current_window_only"
    ledger = [
        json.loads(line)
        for line in (args.output_dir / "actual-spend-ledger.jsonl").read_text().splitlines()
    ]
    assert len(ledger) == 36
    assert manifest["cost_attribution"]["account_windows"] == scope["account_windows"]
    assert manifest["cost_attribution"]["account_window_total_usd"] == "8.198438756"
    audit = json.loads((args.output_dir / "audit.json").read_text())
    assert audit["selected_generation_cost"]["unallocated_aborted_window_usd"] == ("4.598438756")
    report = (args.output_dir / "EXPERIMENT_RESULTS.md").read_text()
    assert "multi-window-counter-exact-campaign-attribution-unproven" in report
    assert "4.598438756" in report
    assert "当前正式窗口的物理回执与账户 counter 已精确对账" in report
    assert "当前正式窗口存在 non-exact/unknown 物理请求" not in report
    assert proof["account_windows"][0]["admission_basis"] == (
        "operator_supplied_unallocated_window"
    )


def test_current_reconciliation_runtime_file_hash_mismatch_is_rejected(
    module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opensquilla.provider import ranking_router

    monkeypatch.setattr(
        ranking_router,
        "ranking_trace_replay_reasons",
        lambda _plan: [],
        raising=False,
    )
    args, _, lock_fd = _campaign(module, tmp_path)
    reconciliation = json.loads(args.account_reconciliation.read_text())
    reconciliation["runtime_environment_file_sha256"] = "b" * 64
    _owner_json(args.account_reconciliation, reconciliation)
    try:
        with pytest.raises(module.FinalizationError, match="runtime file hash differs"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)


def test_prior_account_window_overlap_is_rejected(
    module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opensquilla.provider import ranking_router

    monkeypatch.setattr(
        ranking_router,
        "ranking_trace_replay_reasons",
        lambda _plan: [],
        raising=False,
    )
    args, _, lock_fd = _campaign(module, tmp_path)
    prior = _prior_account_window(module, args, tmp_path)
    after = json.loads((prior / "openrouter-account-after.json").read_text())
    after["captured_at"] = "1970-01-01T00:17:00+00:00"
    _owner_json(prior / "openrouter-account-after.json", after)
    reconciliation = json.loads((prior / "openrouter-account-reconciliation.json").read_text())
    reconciliation["stable_observations"][-1]["captured_at"] = "1970-01-01T00:17:00+00:00"
    reconciliation["observation_span_seconds"] = 360.0
    reconciliation["stable_tail_span_seconds"] = 360.0
    _owner_json(prior / "openrouter-account-reconciliation.json", reconciliation)
    args.prior_account_window_dir = [prior]
    try:
        with pytest.raises(module.FinalizationError, match="overlap"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)


def test_prior_account_before_must_precede_first_stable_observation(
    module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opensquilla.provider import ranking_router

    monkeypatch.setattr(
        ranking_router,
        "ranking_trace_replay_reasons",
        lambda _plan: [],
        raising=False,
    )
    args, _, lock_fd = _campaign(module, tmp_path)
    prior = _prior_account_window(module, args, tmp_path)
    before = json.loads((prior / "openrouter-account-before.json").read_text())
    before["captured_at"] = "1970-01-01T00:11:30+00:00"
    _owner_json(prior / "openrouter-account-before.json", before)
    args.prior_account_window_dir = [prior]
    try:
        with pytest.raises(module.FinalizationError, match="precede settlement observations"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)


def test_prior_windows_require_monotonic_cumulative_byok_counter(
    module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opensquilla.provider import ranking_router

    monkeypatch.setattr(
        ranking_router,
        "ranking_trace_replay_reasons",
        lambda _plan: [],
        raising=False,
    )
    args, _, lock_fd = _campaign(module, tmp_path)
    for path in (args.account_before, args.account_after):
        payload = json.loads(path.read_text())
        payload["byok_usage"] = "2"
        _owner_json(path, payload)
    current_reconciliation = json.loads(args.account_reconciliation.read_text())
    for field in (
        "byok_usage_before_usd",
        "byok_usage_after_usd",
    ):
        current_reconciliation[field] = "2"
    for observation in current_reconciliation["stable_observations"]:
        observation["byok_usage"] = "2"
    _owner_json(args.account_reconciliation, current_reconciliation)

    earlier = _prior_account_window(
        module,
        args,
        tmp_path,
        before_usage="1",
        suffix="prior-earlier",
    )
    earlier_before = json.loads((earlier / "openrouter-account-before.json").read_text())
    earlier_before.update(
        captured_at="1970-01-01T00:03:00+00:00",
        byok_usage="2",
    )
    _owner_json(earlier / "openrouter-account-before.json", earlier_before)
    earlier_after = json.loads((earlier / "openrouter-account-after.json").read_text())
    earlier_after.update(
        captured_at="1970-01-01T00:09:00+00:00",
        usage="5",
        byok_usage="2",
    )
    _owner_json(earlier / "openrouter-account-after.json", earlier_after)
    earlier_reconciliation = json.loads(
        (earlier / "openrouter-account-reconciliation.json").read_text()
    )
    earlier_reconciliation.update(
        usage_before_usd="1",
        usage_after_usd="5",
        usage_delta_usd="4",
        byok_usage_before_usd="2",
        byok_usage_after_usd="2",
        byok_usage_delta_usd="0",
    )
    earlier_times = (
        "1970-01-01T00:05:00+00:00",
        "1970-01-01T00:06:00+00:00",
        "1970-01-01T00:07:00+00:00",
        "1970-01-01T00:08:00+00:00",
        "1970-01-01T00:08:45+00:00",
        "1970-01-01T00:09:00+00:00",
    )
    for observation, captured_at in zip(
        earlier_reconciliation["stable_observations"],
        earlier_times,
        strict=True,
    ):
        observation.update(captured_at=captured_at, usage="5", byok_usage="2")
    _owner_json(
        earlier / "openrouter-account-reconciliation.json",
        earlier_reconciliation,
    )

    later = _prior_account_window(
        module,
        args,
        tmp_path,
        before_usage="6",
        suffix="prior-later",
    )
    for name in (
        "openrouter-account-before.json",
        "openrouter-account-after.json",
    ):
        payload = json.loads((later / name).read_text())
        payload["byok_usage"] = "1"
        _owner_json(later / name, payload)
    later_reconciliation = json.loads(
        (later / "openrouter-account-reconciliation.json").read_text()
    )
    later_reconciliation.update(
        byok_usage_before_usd="1",
        byok_usage_after_usd="1",
        byok_usage_delta_usd="0",
    )
    for observation in later_reconciliation["stable_observations"]:
        observation["byok_usage"] = "1"
    _owner_json(later / "openrouter-account-reconciliation.json", later_reconciliation)
    args.prior_account_window_dir = [earlier, later]
    try:
        with pytest.raises(module.FinalizationError, match="BYOK usage decreased"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)


def test_prior_account_window_delta_is_not_hard_coded(
    module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opensquilla.provider import ranking_router

    monkeypatch.setattr(
        ranking_router,
        "ranking_trace_replay_reasons",
        lambda _plan: [],
        raising=False,
    )
    args, _, lock_fd = _campaign(module, tmp_path)
    args.prior_account_window_dir = [
        _prior_account_window(module, args, tmp_path, before_usage="6")
    ]
    try:
        module.run_finalization(args)
    finally:
        os.close(lock_fd)
    proof = json.loads((args.output_dir / "openrouter-non-byok-campaign-proof.json").read_text())
    assert proof["unallocated_aborted_window_usd"] == "4"
    assert proof["account_window_total_usd"] == "7.6"
    assert proof["cost_scope"]["campaign_attributable_exact"] is False


def test_unverified_receipt_is_resolved_only_by_campaign_proof(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, exact=False)
    try:
        module.run_finalization(args)
    finally:
        os.close(lock_fd)
    proof = json.loads((args.output_dir / "openrouter-non-byok-campaign-proof.json").read_text())
    assert proof["pass"] is True
    assert proof["local_physical_request_evidence"]["unverified_request_count"] == 36
    assert (
        proof["local_physical_request_evidence"]["campaign_covered_unverified_request_count"] == 36
    )
    assert proof["cost_scope"]["campaign_attributable_exact"] is False
    assert proof["cost_scope"]["campaign_attributable_cost_usd"] is None
    assert (
        proof["cost_scope"]["attribution_precision"]
        == "account_window_only_external-use-not-provable"
    )
    assert proof["window"]["exclusive_lock_scope"] == ("local_host_filesystem_only")
    assert proof["window"]["cross_host_exclusivity_proven"] is False
    report = (args.output_dir / "EXPERIMENT_RESULTS.md").read_text()
    assert "account_window_only_external-use-not-provable" in report


def test_unknown_failed_analyzer_attempt_is_preserved_without_rerun(
    module,
    tmp_path: Path,
) -> None:
    args, _, lock_fd = _campaign(
        module,
        tmp_path,
        with_repair=False,
    )
    path = args.result[0]
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    g1 = next(row for row in rows if row["group"] == "G1")
    physical_attempt_id = "a" * 32
    unknown_analyzer = {
        "role": "unknown_request",
        "label": "task_analyzer",
        "provider": "",
        "model": "",
        "requested_provider": "openrouter",
        "requested_model": module.B0_MODEL,
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "cached_tokens": 0,
        "cache_write_tokens": 0,
        "billed_cost": 0.0,
        "cost_source": "none",
        "attempt": 1,
        "physical_attempt_id": physical_attempt_id,
        "provider_usage": {
            "usage_unknown": True,
            "physical_attempt_id": physical_attempt_id,
        },
    }
    top_units = g1["usage"]["model_usage_breakdown"]
    run = g1["execution"]["generation_attempts"][0]["run"]
    run_units = run["usage"]["model_usage_breakdown"]
    top_units.insert(0, deepcopy(unknown_analyzer))
    run_units.insert(0, deepcopy(unknown_analyzer))
    g1["llm_request_count"] = 3
    run["llm_request_count"] = 3
    g1["error"] = "openrouter_non_byok_metadata_incomplete"
    g1["completion_status"].update(
        {
            "cost_metadata_complete": False,
            "status": "incomplete",
        }
    )
    g1["cost_accounting"]["selected_generation_attempt"].update(
        {
            "request_count": 3,
            "cost_complete": False,
            "cost_exact": False,
        }
    )
    g1["cost_accounting"]["actual_llm_cost_complete"] = False
    g1["cost_accounting"]["actual_spend_cost_complete"] = False
    g1["openrouter_non_byok_audit"].update(
        {
            "pass": False,
            "status": "metadata_incomplete",
        }
    )
    rows[rows.index(g1)] = module.seal_result_row(g1)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    path.chmod(0o600)
    try:
        manifest = module.run_finalization(args)
    finally:
        os.close(lock_fd)

    assert manifest["status"] == "complete"
    ledger = [
        json.loads(line)
        for line in (args.output_dir / "actual-spend-ledger.jsonl").read_text().splitlines()
    ]
    assert len(ledger) == 37
    unknown = [
        row
        for row in ledger
        if row["cost_precision"] == "unknown"
        and {"group": "G1", "task_id": "task-1"} in row["group_task_pairs"]
    ]
    assert len(unknown) == 1
    proof = json.loads((args.output_dir / "openrouter-non-byok-campaign-proof.json").read_text())
    assert proof["cost_scope"]["campaign_attributable_exact"] is False
    assert proof["cost_scope"]["campaign_attributable_cost_usd"] is None
    assert (
        proof["cost_scope"]["attribution_precision"]
        == "account_window_only_external-use-not-provable"
    )


def test_five_counter_only_failed_judge_attempts_finalize_without_rerun(
    module,
    tmp_path: Path,
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    path = args.result[0]
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    row = rows[0]
    judgments = row["judge"]["criterion_judgments"]

    for index, judgment in enumerate(judgments[:5]):
        successful = judgment["judge_attempts"][0]
        successful["attempt"] = 2
        failed = {
            "attempt_id": f"{0x5030 + index:032x}",
            "attempt": 1,
            "verdict": "",
            "met": None,
            "retry_suppressed_reason": "",
            "run": {
                "error": (
                    "OpenRouter chat request failed (HTTP 503): "
                    "Cloudflare Worker exceeded resource limits"
                ),
                "llm_request_count": 1,
                "usage_unknown_count": 1,
                "usage": {},
                "trace_events": [
                    {
                        "kind": "error",
                        "code": "503",
                        "request_started": None,
                        "physical_request_count": None,
                    }
                ],
            },
        }
        judgment["judge_attempts"] = [failed, successful]
        judgment.update(
            {
                "judge_attempt_count": 2,
                "judge_attempt_budget_used": 2,
                "judge_attempt_budget_remaining": 1,
                "judge_new_attempt_count": 2,
            }
        )

    row["judge"]["judge_attempt_count"] += 5
    row["judge"]["judge_new_attempt_count"] += 5
    row["error"] = "openrouter_non_byok_metadata_incomplete"
    row["completion_status"].update(
        {
            "cost_metadata_complete": False,
            "status": "incomplete",
        }
    )
    row["cost_accounting"]["actual_llm_cost_complete"] = False
    row["cost_accounting"]["actual_spend_cost_complete"] = False
    row["openrouter_non_byok_audit"].update(
        {
            "pass": False,
            "status": "metadata_incomplete",
        }
    )
    rows[0] = module.seal_result_row(row)
    path.write_text("".join(json.dumps(item) + "\n" for item in rows))
    path.chmod(0o600)

    try:
        manifest = module.run_finalization(args)
    finally:
        os.close(lock_fd)

    assert manifest["status"] == "complete"
    ledger = [
        json.loads(line)
        for line in (args.output_dir / "actual-spend-ledger.jsonl").read_text().splitlines()
    ]
    unknown_judge = [
        item
        for item in ledger
        if item["cost_precision"] == "unknown"
        and item["scopes"] == ["judge"]
        and item["group_task_pairs"] == [{"group": row["group"], "task_id": row["task_id"]}]
    ]
    assert len(ledger) == 41
    assert len(unknown_judge) == 5
    assert all(item["recorded_cost_usd"] is None for item in unknown_judge)


def test_cleanup_failure_setup_attempt_survives_later_success_end_to_end(
    module,
    tmp_path: Path,
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=True)
    wave1 = [json.loads(line) for line in args.result[0].read_text().splitlines()]
    wave2 = [json.loads(line) for line in args.result[1].read_text().splitlines()]
    first = next(row for row in wave1 if row["group"] == "G1")
    second = next(row for row in wave2 if row["group"] == "G1")
    physical_attempt_id = "b" * 32
    unknown_analyzer = {
        "role": "unknown_request",
        "label": "task_analyzer",
        "provider": "",
        "model": "",
        "requested_provider": "openrouter",
        "requested_model": module.B0_MODEL,
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "cached_tokens": 0,
        "cache_write_tokens": 0,
        "billed_cost": 0.0,
        "cost_source": "none",
        "attempt": 1,
        "physical_attempt_id": physical_attempt_id,
        "provider_usage": {
            "usage_unknown": True,
            "physical_attempt_id": physical_attempt_id,
        },
    }
    failed_run = {
        "error": "TaskAnalyzerStreamCleanupError",
        "final_text_sha256": module.text_sha256(""),
        "llm_request_count": 1,
        "usage_unknown_count": 1,
        "usage": {"model_usage_breakdown": [deepcopy(unknown_analyzer)]},
        "trace_events": [
            {
                "kind": "error",
                "code": "provider_build_failed_after_setup",
                "request_started": False,
                "physical_request_count": 0,
            }
        ],
    }
    failed_attempt = {
        "attempt_id": "f" * 32,
        "attempt_kind": "provider_build_after_paid_setup",
        "attempt": 1,
        "started_at": 1_000.0,
        "completed_at": 1_002.0,
        "retryable": True,
        "retry_reason": "TaskAnalyzerStreamCleanupError",
        "retry_suppressed_reason": "",
        "will_retry": False,
        "retry_backoff_s": 0.0,
        "run": failed_run,
    }
    first.update(
        {
            "final_text": "",
            "final_text_chars": 0,
            "final_text_sha256": module.text_sha256(""),
            "selected_generation_succeeded": False,
            "generation_attempt_count": 1,
            "generation_attempt_budget_used": 1,
            "generation_attempt_total_billed_cost": 0.0,
            "llm_request_count": 1,
            "error": "TaskAnalyzerStreamCleanupError",
            "usage": {
                "model_usage_breakdown": [deepcopy(unknown_analyzer)],
                "billed_cost": 0.0,
            },
        }
    )
    first["actual_spend_metrics"]["generation_attempt_count"] = 1
    first["execution"]["generation_attempts"] = [failed_attempt]
    first["execution"]["run_error"] = "TaskAnalyzerStreamCleanupError"
    first["completion_status"].update({"generation_accepted": False, "status": "incomplete"})

    success = second["execution"]["generation_attempts"][0]
    success["attempt"] = 2
    second["execution"]["generation_attempts"] = [success]
    second["execution"]["prior_generation_attempts_used"] = 1
    second["execution"]["generation_reused"] = False
    second["execution"]["resume_action"] = "model_regenerate"
    second["generation_attempt_count"] = 1
    second["generation_attempt_budget_used"] = 2
    second["actual_spend_metrics"]["generation_attempt_count"] = 1
    wave1[wave1.index(first)] = module.seal_result_row(first)
    wave2[wave2.index(second)] = module.seal_result_row(second)
    args.result[0].write_text("".join(json.dumps(row) + "\n" for row in wave1))
    args.result[1].write_text("".join(json.dumps(row) + "\n" for row in wave2))
    wave2_manifest = json.loads(args.manifest[1].read_text())
    wave2_manifest["tool_policy"]["local_web_tools"]["preflight"].update(
        {
            "status": "passed",
            "preflight_calls": {"web_search": 1, "web_fetch": 1},
        }
    )
    wave2_manifest["resume_selection"]["model_regenerate_pair_count"] = 1
    _owner_json(args.manifest[1], wave2_manifest)
    try:
        manifest = module.run_finalization(args)
    finally:
        os.close(lock_fd)

    assert manifest["status"] == "complete"
    ledger = [
        json.loads(line)
        for line in (args.output_dir / "actual-spend-ledger.jsonl").read_text().splitlines()
    ]
    setup_only = [
        row
        for row in ledger
        if row["cost_precision"] == "unknown"
        and row["roles"] == ["unknown_request"]
        and {"group": "G1", "task_id": "task-1"} in row["group_task_pairs"]
    ]
    assert len(setup_only) == 1
    assert any(
        reference.get("attempt_kind") == "provider_build_after_paid_setup"
        for reference in setup_only[0]["source_references"]
    )


def test_explicit_byok_is_fatal_even_when_account_delta_is_zero(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    path = args.result[0]
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    row = rows[0]
    units = row["execution"]["generation_attempts"][0]["run"]["usage"]["model_usage_breakdown"]
    units[0]["provider_usage"]["is_byok"] = True
    units[0]["provider_usage"]["router_metadata"]["is_byok"] = True
    row["usage"]["model_usage_breakdown"][0]["provider_usage"]["is_byok"] = True
    row["usage"]["model_usage_breakdown"][0]["provider_usage"]["router_metadata"]["is_byok"] = True
    rows[0] = module.seal_result_row(row)
    path.write_text("".join(json.dumps(value) + "\n" for value in rows))
    path.chmod(0o600)
    try:
        with pytest.raises(module.FinalizationError, match="explicit BYOK"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)
    assert not args.output_dir.exists()


def test_generation_attempt_budget_over_three_is_fatal(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    path = args.result[0]
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    first_attempt = rows[0]["execution"]["generation_attempts"][0]
    attempts = []
    for index in range(1, 5):
        attempt = deepcopy(first_attempt)
        attempt["attempt"] = index
        attempt["attempt_id"] = f"{100 + index:032x}"
        attempts.append(attempt)
    rows[0]["execution"]["generation_attempts"] = attempts
    rows[0]["generation_attempt_count"] = 4
    rows[0]["actual_spend_metrics"]["generation_attempt_count"] = 4
    rows[0]["generation_attempt_budget_used"] = 4
    rows[0] = module.seal_result_row(rows[0])
    path.write_text("".join(json.dumps(value) + "\n" for value in rows))
    path.chmod(0o600)
    try:
        with pytest.raises(module.FinalizationError, match="ordinal is invalid|exceeds 3"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)
    assert not args.output_dir.exists()


def test_b2_illegal_quorum_leaves_no_partial_output(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    path = args.result[0]
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    b2 = next(index for index, row in enumerate(rows) if row["group"] == "B2")
    rows[b2]["ensemble_trace"]["calls"][0]["successful_proposers"] = 2
    rows[b2] = module.seal_result_row(rows[b2])
    path.write_text("".join(json.dumps(value) + "\n" for value in rows))
    path.chmod(0o600)
    try:
        with pytest.raises(module.FinalizationError, match="no valid generation"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)
    assert not args.output_dir.exists()


def _b2_physical_call(
    module,
    *,
    proven_successes: int,
    declared_successes: int | None = None,
) -> dict[str, object]:
    trace = _ensemble_trace(
        list(module.B2_PROPOSERS),
        module.B2_AGGREGATOR,
        final_text="bound final answer",
        selection_mode="fixed",
        successful=(proven_successes if declared_successes is None else declared_successes),
    )
    call = trace["calls"][0]
    assert isinstance(call, dict)
    candidates = call["candidates"]
    assert isinstance(candidates, list)
    for candidate in candidates[proven_successes:]:
        assert isinstance(candidate, dict)
        candidate.update(
            {
                "ok": False,
                "error": "test proposer failure",
                "provider": "",
                "model": "",
                "content": {"text": "", "chars": 0, "truncated": False},
            }
        )
    return call


def _b2_physical_call_reasons(module, call: dict[str, object]) -> list[str]:
    return module.ensemble_physical_call_reasons(
        call,
        expected_proposers=module.B2_PROPOSERS,
        expected_aggregator=module.B2_AGGREGATOR,
        final_text="bound final answer",
        require_output_binding=True,
    )


def test_b2_physical_call_accepts_failed_candidate_without_actual_route(
    module,
) -> None:
    call = _b2_physical_call(module, proven_successes=3)

    assert _b2_physical_call_reasons(module, call) == []


def test_b2_physical_call_rejects_wrong_failed_candidate_requested_route(
    module,
) -> None:
    call = _b2_physical_call(module, proven_successes=3)
    candidate = call["candidates"][-1]
    assert isinstance(candidate, dict)
    candidate["requested_provider"] = "direct"
    candidate["requested_model"] = "vendor/wrong-model"

    reasons = _b2_physical_call_reasons(module, call)
    assert "wrong_actual_proposer_provider" in reasons
    assert "wrong_actual_proposer_model" in reasons


def test_b2_physical_call_rejects_failed_candidate_conflicting_actual_route(
    module,
) -> None:
    call = _b2_physical_call(module, proven_successes=3)
    candidate = call["candidates"][-1]
    assert isinstance(candidate, dict)
    candidate["provider"] = "direct"
    candidate["model"] = "vendor/wrong-model"

    reasons = _b2_physical_call_reasons(module, call)
    assert "wrong_actual_proposer_provider" in reasons
    assert "wrong_actual_proposer_model" in reasons


def test_b2_physical_call_rejects_successful_candidate_without_actual_route(
    module,
) -> None:
    call = _b2_physical_call(module, proven_successes=3)
    candidate = call["candidates"][0]
    assert isinstance(candidate, dict)
    candidate["provider"] = ""
    candidate["model"] = ""

    reasons = _b2_physical_call_reasons(module, call)
    assert "wrong_actual_proposer_provider" in reasons
    assert "wrong_actual_proposer_model" in reasons


def test_b2_physical_call_rejects_declared_success_count_above_proven_count(
    module,
) -> None:
    call = _b2_physical_call(
        module,
        proven_successes=3,
        declared_successes=4,
    )

    assert "successful_proposer_count_mismatch" in _b2_physical_call_reasons(
        module,
        call,
    )


def test_b2_physical_call_rejects_two_of_four_below_quorum(module) -> None:
    call = _b2_physical_call(module, proven_successes=2)

    reasons = _b2_physical_call_reasons(module, call)
    assert "proposer_quorum_not_met" in reasons
    assert "insufficient_actual_proposer_quorum" in reasons


def test_nonzero_decimal_byok_delta_is_fatal(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path)
    after = json.loads(args.account_after.read_text())
    after["byok_usage"] = "0.000000001"
    _owner_json(args.account_after, after)
    reconciliation = json.loads(args.account_reconciliation.read_text())
    reconciliation["byok_usage_after_usd"] = "0.000000001"
    reconciliation["byok_usage_delta_usd"] = "0.000000001"
    for observation in reconciliation["stable_observations"]:
        observation["byok_usage"] = "0.000000001"
    _owner_json(args.account_reconciliation, reconciliation)
    try:
        with pytest.raises(module.FinalizationError, match="BYOK delta"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)


def test_unexplained_account_to_exact_ledger_delta_is_fatal(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path)
    after = json.loads(args.account_after.read_text())
    after["usage"] = "14"
    _owner_json(args.account_after, after)
    reconciliation = json.loads(args.account_reconciliation.read_text())
    reconciliation["usage_after_usd"] = "14"
    reconciliation["usage_delta_usd"] = "4"
    for observation in reconciliation["stable_observations"]:
        observation["usage"] = "14"
    _owner_json(args.account_reconciliation, reconciliation)
    try:
        with pytest.raises(module.FinalizationError, match="unexplained OpenRouter"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)
    assert not args.output_dir.exists()


def test_three_identical_stable_polls_and_preheld_lock_are_mandatory(
    module, tmp_path: Path
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path)
    reconciliation = json.loads(args.account_reconciliation.read_text())
    reconciliation["stable_poll_count"] = 2
    _owner_json(args.account_reconciliation, reconciliation)
    try:
        with pytest.raises(module.FinalizationError, match="stable_poll_count"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)

    other = tmp_path / "other"
    args2, _, lock_fd2 = _campaign(module, other)
    fcntl.flock(lock_fd2, fcntl.LOCK_UN)
    try:
        with pytest.raises(module.FinalizationError, match="not already held"):
            module.run_finalization(args2)
    finally:
        os.close(lock_fd2)


def test_source_hash_recheck_detects_mutation(module, tmp_path: Path) -> None:
    source = tmp_path / "rows.jsonl"
    source.write_text("{}\n")
    source.chmod(0o600)
    snapshots = {str(source.resolve()): module.file_sha256(source)}
    source.write_text('{"changed":true}\n')
    with pytest.raises(module.FinalizationError, match="changed during finalization"):
        module.verify_source_snapshots(snapshots)


def test_attempt_id_payload_conflict_and_legacy_mix_are_fatal(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path)
    try:
        wave2 = args.result[-1]
        rows = [json.loads(line) for line in wave2.read_text().splitlines()]
        rows[0]["execution"]["generation_attempts"][0]["run"]["usage"]["model_usage_breakdown"][0][
            "input_tokens"
        ] = 999
        rows[0] = module.seal_result_row(rows[0])
        wave2.write_text("".join(json.dumps(row) + "\n" for row in rows))
        wave2.chmod(0o600)
        with pytest.raises(module.FinalizationError, match="conflicting payloads"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)

    legacy_dir = tmp_path / "legacy"
    args2, _, lock_fd2 = _campaign(module, legacy_dir, with_repair=False)
    try:
        path = args2.result[0]
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows[0].pop("generation_attempt_evidence_schema")
        rows[0] = module.seal_result_row(rows[0])
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        path.chmod(0o600)
        with pytest.raises(module.FinalizationError, match="legacy or missing"):
            module.run_finalization(args2)
    finally:
        os.close(lock_fd2)


def test_attempt_budget_must_be_monotonic_across_repair_wave(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path)
    wave2 = args.result[-1]
    rows = [json.loads(line) for line in wave2.read_text().splitlines()]
    rows[0]["execution"]["prior_generation_attempts_used"] = 0
    rows[0] = module.seal_result_row(rows[0])
    wave2.write_text("".join(json.dumps(row) + "\n" for row in rows))
    wave2.chmod(0o600)
    try:
        with pytest.raises(module.FinalizationError, match="non-monotonic"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)


def test_actual_spend_ledger_keeps_failed_replaced_attempt_once(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    try:
        path = args.result[0]
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        first = rows[0]
        success = first["execution"]["generation_attempts"][0]
        failed = deepcopy(success)
        failed["attempt"] = 1
        failed["attempt_id"] = "f" * 32
        failed["run"]["error"] = "provider_timeout"
        failed["run"]["usage"]["model_usage_breakdown"][0]["provider_usage"]["response_ids"] = [
            "b0-failed-generation"
        ]
        success["attempt"] = 2
        first["execution"]["generation_attempts"] = [failed, success]
        first["generation_attempt_count"] = 2
        first["actual_spend_metrics"]["generation_attempt_count"] = 2
        first["generation_attempt_budget_used"] = 2
        rows[0] = module.seal_result_row(first)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        path.chmod(0o600)
        records, _ = module.read_source_rows([path])
        attempt_audit = module.validate_generation_attempt_evidence(records, max_attempts=3)
        ledger, summary = module.build_actual_spend_ledger(records)
        assert attempt_audit["B0/task-1"]["unique_attempt_count"] == 2
        assert summary["distinct_generation_attempt_count"] == 6
        assert len(ledger) == 37
        ids = {item for row in ledger for item in row["response_id_sha256"]}
        assert len(ids) == 37
    finally:
        os.close(lock_fd)


@pytest.mark.parametrize(
    "collision",
    ["generation_generation", "generation_judge", "judge_judge"],
)
def test_actual_spend_ledger_rejects_response_id_reuse_across_physical_units(
    module,
    tmp_path: Path,
    collision: str,
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    try:
        path = args.result[0]
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        shared = "shared-provider-response"
        generation_a = rows[0]["execution"]["generation_attempts"][0]["run"]["usage"][
            "model_usage_breakdown"
        ][0]
        judge_a = rows[0]["judge"]["criterion_judgments"][0]["judge_attempts"][0]["run"]["usage"][
            "model_usage_breakdown"
        ][0]
        if collision == "generation_generation":
            generation_b = rows[1]["execution"]["generation_attempts"][0]["run"]["usage"][
                "model_usage_breakdown"
            ][0]
            units = [generation_a, generation_b]
        elif collision == "generation_judge":
            units = [generation_a, judge_a]
        else:
            judge_b = rows[0]["judge"]["criterion_judgments"][1]["judge_attempts"][0]["run"][
                "usage"
            ]["model_usage_breakdown"][0]
            units = [judge_a, judge_b]
        for unit in units:
            unit["provider_usage"]["response_ids"] = [shared]
        rows = [module.seal_result_row(row) for row in rows]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        path.chmod(0o600)
        records, _ = module.read_source_rows([path])
        with pytest.raises(module.FinalizationError, match="response_id is reused"):
            module.build_actual_spend_ledger(records)
    finally:
        os.close(lock_fd)


@pytest.mark.parametrize(
    ("estimated_cost", "expected_cost", "expected_precision"),
    [(None, None, "unknown"), (0.42, "0.42", "estimated")],
)
def test_none_cost_source_does_not_turn_billed_default_into_recorded_zero(
    module,
    tmp_path: Path,
    estimated_cost: float | None,
    expected_cost: str | None,
    expected_precision: str,
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    try:
        path = args.result[0]
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        unit = rows[0]["execution"]["generation_attempts"][0]["run"]["usage"][
            "model_usage_breakdown"
        ][0]
        unit["provider_usage"].pop("provider_reported_cost")
        unit["cost_source"] = "none"
        unit["billed_cost"] = 0.0
        if estimated_cost is not None:
            unit["estimated_cost_usd"] = estimated_cost
        rows[0] = module.seal_result_row(rows[0])
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        path.chmod(0o600)
        records, _ = module.read_source_rows([path])
        ledger, _ = module.build_actual_spend_ledger(records)
        generation = next(
            row
            for row in ledger
            if "generation" in row["scopes"]
            and {"group": "B0", "task_id": "task-1"} in row["group_task_pairs"]
        )
        assert generation["provider"] == "openrouter"
        assert generation["model"] == module.B0_MODEL
        assert generation["input_tokens"] == 10
        assert generation["recorded_cost_usd"] == expected_cost
        assert generation["cost_precision"] == expected_precision
    finally:
        os.close(lock_fd)


def test_g1_plan_must_be_bound_to_frozen_registry(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    path = args.result[0]
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    index = next(index for index, row in enumerate(rows) if row["group"] == "G1")
    forged = {
        "selection_mode": "router_dynamic",
        "proposer_models": ["z-ai/glm-5.2"],
        "proposer_sample_count": 1,
        "aggregator_model": "z-ai/glm-5.2",
    }
    rows[index]["routing_trace"]["selection_plan"] = forged
    call = rows[index]["ensemble_trace"]["calls"][0]
    call["selection_plan"] = deepcopy(forged)
    call["total_candidates"] = 1
    call["successful_proposers"] = 1
    call["candidates"] = call["candidates"][:1]
    rows[index] = module.seal_result_row(rows[index])
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    path.chmod(0o600)
    try:
        with pytest.raises(module.FinalizationError, match="no valid generation"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)


def test_g1_source_and_runtime_registry_hashes_have_distinct_semantics(
    module,
) -> None:
    module.FORMAL_G1_RANKING_CONFIG_SHA256 = module.canonical_sha256(_test_ranking_config(module))
    contract = _contract(module, "G1", "1" * 64)["g1_registry_contract"]
    plan = _g1_plan(
        module,
        [
            "deepseek/deepseek-v4-pro",
            "z-ai/glm-5.2",
            "qwen/qwen3.7-max",
        ],
        "z-ai/glm-5.2",
    )
    assert plan["registry_snapshot_hash"] != contract["expected_source_registry_snapshot_sha256"]
    reasons, _, _ = module.g1_registry_plan_reasons(
        plan,
        contract=contract,
    )
    assert reasons == []

    wrong_source = deepcopy(plan)
    wrong_source["candidate_allowlist"]["expected_source_registry_snapshot_sha256"] = "e" * 64
    reasons, _, _ = module.g1_registry_plan_reasons(
        wrong_source,
        contract=contract,
    )
    assert ("wrong_g1_candidate_allowlist_expected_source_registry_snapshot_sha256") in reasons

    invalid_runtime = deepcopy(plan)
    invalid_runtime["registry_snapshot_hash"] = "not-a-sha256"
    reasons, _, _ = module.g1_registry_plan_reasons(
        invalid_runtime,
        contract=contract,
    )
    assert "wrong_g1_registry_snapshot_hash" in reasons


@pytest.mark.parametrize("selection_field", ["selected_P", "selected_A"])
def test_g1_frozen_replay_rejects_valid_pool_selection_swap(
    module,
    selection_field: str,
) -> None:
    module.FORMAL_G1_RANKING_CONFIG_SHA256 = module.canonical_sha256(_test_ranking_config(module))
    contract = _contract(module, "G1", "1" * 64)["g1_registry_contract"]
    plan = _g1_plan(
        module,
        [
            "deepseek/deepseek-v4-pro",
            "qwen/qwen3.7-max",
            "z-ai/glm-5.2",
        ],
        "z-ai/glm-5.2",
    )
    forged = deepcopy(plan)
    if selection_field == "selected_P":
        forged["selected_P"] = list(reversed(forged["selected_P"]))
        forged["proposer_models"] = [
            identity.partition(":")[2] for identity in forged["selected_P"]
        ]
        for index, step in enumerate(forged["selection_steps"]):
            step["selected"] = forged["selected_P"][index]
    else:
        replacement = next(
            row["identity"]
            for row in forged["candidate_pool"]
            if row["identity"] != forged["selected_A"]
        )
        forged["selected_A"] = replacement
        forged["aggregator_model"] = replacement.partition(":")[2]

    reasons, _, _ = module.g1_registry_plan_reasons(
        forged,
        contract=contract,
    )
    assert f"g1_frozen_ranker_replay_mismatch_{selection_field}" in reasons


def test_g1_runtime_registry_hash_must_match_every_physical_plan(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    path = args.result[0]
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    index = next(index for index, row in enumerate(rows) if row["group"] == "G1")
    rows[index]["ensemble_trace"]["calls"][0]["selection_plan"]["registry_snapshot_hash"] = "e" * 64
    rows[index] = module.seal_result_row(rows[index])
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    path.chmod(0o600)
    try:
        with pytest.raises(module.FinalizationError, match="no valid generation"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)


def test_same_attempt_metadata_repair_replaces_estimate_with_exact_receipt(
    module, tmp_path: Path
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, exact=False)
    try:
        wave1_rows = [json.loads(line) for line in args.result[0].read_text().splitlines()]
        wave2_rows = [json.loads(line) for line in args.result[1].read_text().splitlines()]
        old_unit = wave1_rows[0]["execution"]["generation_attempts"][0]["run"]["usage"][
            "model_usage_breakdown"
        ][0]
        old_unit["provider_usage"] = {}
        old_unit["billed_cost"] = 0.2
        old_unit["cost_source"] = "opensquilla_static_estimate"
        new_unit = _receipt(
            "b0-repaired-exact",
            "anthropic/claude-opus-4.8",
            cost=0.1,
            exact=True,
        )
        wave2_rows[0]["execution"]["generation_attempts"][0]["run"]["usage"][
            "model_usage_breakdown"
        ][0] = new_unit
        wave1_rows[0] = module.seal_result_row(wave1_rows[0])
        wave2_rows[0] = module.seal_result_row(wave2_rows[0])
        args.result[0].write_text("".join(json.dumps(row) + "\n" for row in wave1_rows))
        args.result[1].write_text("".join(json.dumps(row) + "\n" for row in wave2_rows))
        records, _ = module.read_source_rows(args.result)
        module.validate_generation_attempt_evidence(records, max_attempts=3)
        selected = [record for record in records if record.source_index == 1]
        ledger, _ = module.build_actual_spend_ledger(
            records,
            selected=selected,
        )
        b0_generation = next(
            row
            for row in ledger
            if "generation" in row["scopes"]
            and {"group": "B0", "task_id": "task-1"} in row["group_task_pairs"]
        )
        assert b0_generation["recorded_cost_usd"] == "0.100000000"
        assert b0_generation["cost_precision"] == "exact"
        assert b0_generation["generation_disposition"] == "selected"
        assert len(b0_generation["response_id_sha256"]) == 1
    finally:
        os.close(lock_fd)


def test_non_exact_estimates_may_exceed_exact_account_delta(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(
        module,
        tmp_path,
        exact=False,
        with_repair=False,
    )
    path = args.result[0]
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    for row in rows:
        generation_units = row["execution"]["generation_attempts"][0]["run"]["usage"][
            "model_usage_breakdown"
        ]
        judge_units = [
            attempt["run"]["usage"]["model_usage_breakdown"][0]
            for judgment in row["judge"]["criterion_judgments"]
            for attempt in judgment["judge_attempts"]
        ]
        for unit in [*generation_units, *judge_units]:
            unit["provider_usage"].pop("provider_reported_cost", None)
            unit["billed_cost"] = 0.2
            unit["cost_source"] = "opensquilla_static_estimate"
        selected_cost = 0.2 * len(generation_units)
        row["cost_accounting"]["selected_generation_attempt"]["recorded_cost_usd"] = selected_cost
        row["cost_accounting"]["selected_generation_attempt"]["cost_exact"] = False
        row["cost_accounting"]["selected_generation_attempt"]["cost_complete"] = True
        row["generation_attempt_total_billed_cost"] = selected_cost
    rows = [module.seal_result_row(row) for row in rows]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    path.chmod(0o600)
    try:
        manifest = module.run_finalization(args)
    finally:
        os.close(lock_fd)
    assert manifest["status"] == "complete"
    proof = json.loads((args.output_dir / "openrouter-non-byok-campaign-proof.json").read_text())
    assert proof["account"]["usage_delta_usd"] == "3.6"
    assert proof["cost_scope"]["ledger_recorded_cost_usd"] == "7.2"
    assert proof["cost_scope"]["reconciliation_status"] == "account_exact_per_request_incomplete"
    assert proof["cost_scope"]["campaign_attributable_exact"] is False
    assert proof["cost_scope"]["campaign_attributable_cost_usd"] is None
    assert (
        proof["cost_scope"]["attribution_precision"]
        == "account_window_only_external-use-not-provable"
    )


def test_frozen_draco_input_hash_and_count_are_fail_closed(module, tmp_path: Path) -> None:
    assert module.FROZEN_DRACO_MINI_TASK_COUNT == 10
    assert (
        module.FROZEN_DRACO_MINI_SHA256
        == "1eb4e618c8df8e7f68bded3d2b6f77a541744aa1072eb338835b776183188a8d"
    )
    path = tmp_path / "wrong.jsonl"
    path.write_text('{"id":"one","prompt":"one"}\n')
    path.chmod(0o600)
    tasks = module.read_tasks(path)
    with pytest.raises(module.FinalizationError, match="SHA256 differs"):
        module.validate_frozen_draco_input(path, tasks)
    module.FROZEN_DRACO_MINI_SHA256 = module.file_sha256(path)
    with pytest.raises(module.FinalizationError, match="exactly 10"):
        module.validate_frozen_draco_input(path, tasks)


def test_live_manifest_preflight_is_mandatory(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    manifest = json.loads(args.manifest[0].read_text())
    manifest.pop("tool_policy")
    _owner_json(args.manifest[0], manifest)
    try:
        with pytest.raises(module.FinalizationError, match="Web preflight"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)


def test_skipped_preflight_requires_a_true_no_generation_repair(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path)
    manifest = json.loads(args.manifest[1].read_text())
    manifest["resume_selection"]["model_regenerate_pair_count"] = 1
    _owner_json(args.manifest[1], manifest)
    try:
        with pytest.raises(module.FinalizationError, match="no-generation repair shard"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)


def _set_nested_value(target: dict[str, object], path: tuple[str, ...], value: object) -> None:
    current = target
    for field_name in path[:-1]:
        child = current[field_name]
        assert isinstance(child, dict)
        current = child
    current[path[-1]] = value


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("runner", "mode"), "provider"),
        (("runner", "agent_max_iterations"), 13),
        (
            ("runner", "finalization_policy", "retrieval_loop_finalization_threshold"),
            1,
        ),
        (("timeouts", "task_seconds"), 10_799),
        (("generation", "policy", "temperature"), 0.1),
        (("generation", "policy", "max_tokens"), 4_096),
        (("tools", "local_web_tools", "web_search", "provider"), "duckduckgo"),
        (("judge", "repeats"), 1),
        (("cost_policy", "require_openrouter_non_byok"), False),
        (("resolved_llm_runtime", "response_cache_disabled"), False),
        (("global_experiment_profile", "generation", "max_tokens"), 4_096),
        (("formal_runtime_freeze", "sandbox_enabled"), True),
        (("formal_runtime_freeze", "aggregator_recovery_mode"), "serving"),
        (("formal_runtime_freeze", "aggregator_recovery_top_k"), 2),
        (("formal_runtime_freeze", "aggregator_max_tokens_cap"), 16_384),
        (
            ("formal_runtime_freeze", "aggregator_visible_answer_reserve_tokens"),
            4_096,
        ),
    ],
)
def test_formal_execution_contract_tampering_fails_closed(
    module,
    path: tuple[str, ...],
    value: object,
) -> None:
    key_hash = "a" * 64
    contracts = {group: _contract(module, group, key_hash) for group in module.GROUPS}
    b2 = contracts["B2"]
    assert isinstance(b2, dict)
    _set_nested_value(b2, path, value)

    with pytest.raises(module.FinalizationError) as error:
        module.validate_formal_campaign_contracts(contracts)

    assert f"B2 formal execution contract.{'.'.join(path)}" in str(error.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("aggregator_recovery_mode", "serving"),
        ("aggregator_recovery_top_k", 2),
        ("aggregator_max_tokens_cap", 16_384),
        ("aggregator_visible_answer_reserve_tokens", 4_096),
    ],
)
def test_formal_gateway_aggregator_recovery_tampering_fails_closed(
    module,
    field: str,
    value: object,
) -> None:
    contracts = {group: _contract(module, group, "a" * 64) for group in module.GROUPS}
    contracts["G1"]["gateway_execution"]["llm_ensemble"][field] = value

    with pytest.raises(module.FinalizationError) as error:
        module.validate_formal_campaign_contracts(contracts)

    assert f"G1 gateway llm_ensemble.{field}" in str(error.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("concurrency", 4),
        ("judge_concurrency", 5),
    ],
)
def test_manifest_scheduling_tampering_fails_closed(
    module,
    tmp_path: Path,
    field: str,
    value: int,
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    manifest = json.loads(args.manifest[0].read_text())
    manifest["command"]["parsed_args"][field] = value
    _owner_json(args.manifest[0], manifest)
    try:
        with pytest.raises(module.FinalizationError) as error:
            module.run_finalization(args)
    finally:
        os.close(lock_fd)

    assert f"command.parsed_args.{field} differs from the formal value" in str(error.value)


def test_manifest_scheduling_accepts_explicit_task_concurrency(
    module,
    tmp_path: Path,
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    manifest = json.loads(args.manifest[0].read_text())
    manifest["command"]["parsed_args"]["concurrency"] = 7
    _owner_json(args.manifest[0], manifest)
    args.expected_task_concurrency = 7
    try:
        payload = module.run_finalization(args)
    finally:
        os.close(lock_fd)

    assert payload["source_manifests"][0]["execution_scheduling"] == {
        "task_concurrency": 7,
        "judge_concurrency": 6,
    }


def test_finalizer_rejects_nonpositive_expected_task_concurrency(
    module,
    tmp_path: Path,
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    args.expected_task_concurrency = 0
    try:
        with pytest.raises(
            module.FinalizationError,
            match="expected task concurrency must be a positive integer",
        ):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)


def test_b1_frozen_tier_map_cannot_drift(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    manifest = json.loads(args.manifest[0].read_text())
    contract = manifest["run_compatibility"]["contracts"]["B1"]
    contract["gateway_execution"]["squilla_router"]["tiers"]["c0"]["model"] = "evil/model"
    manifest["run_compatibility"]["fingerprints"]["B1"] = module.canonical_sha256(
        contract, prefix=True
    )
    _owner_json(args.manifest[0], manifest)
    try:
        with pytest.raises(module.FinalizationError, match="B1 c0"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)


def test_router_receipt_model_must_match_formal_route(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    path = args.result[0]
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    receipt = rows[0]["execution"]["generation_attempts"][0]["run"]["usage"][
        "model_usage_breakdown"
    ][0]
    receipt["provider_usage"]["router_metadata"]["attempts"][0]["model"] = "evil/model"
    rows[0] = module.seal_result_row(rows[0])
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    path.chmod(0o600)
    try:
        with pytest.raises(
            module.FinalizationError,
            match="physical generation route",
        ):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)


def test_b2_router_receipt_provider_must_match_frozen_pin(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    rows = [json.loads(line) for line in args.result[0].read_text().splitlines()]
    b2 = next(row for row in rows if row["group"] == "B2")
    receipt = b2["execution"]["generation_attempts"][0]["run"]["usage"]["model_usage_breakdown"][0]
    receipt["provider_usage"]["router_metadata"]["attempts"][0]["provider"] = "evil-upstream"
    rows[rows.index(b2)] = module.seal_result_row(b2)
    args.result[0].write_text("".join(json.dumps(row) + "\n" for row in rows))
    try:
        with pytest.raises(module.FinalizationError, match="physical generation route"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("required_stable_poll_count", 5),
        ("poll_interval_seconds", 14),
        ("minimum_settlement_seconds", 179),
        ("minimum_stable_tail_seconds", 74),
    ],
)
def test_formal_account_settlement_thresholds_fail_closed(
    module, tmp_path: Path, field: str, value: int
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path)
    reconciliation = json.loads(args.account_reconciliation.read_text())
    reconciliation[field] = value
    _owner_json(args.account_reconciliation, reconciliation)
    try:
        with pytest.raises(module.FinalizationError, match="formal value"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)


def test_account_observation_spans_and_monotonicity_are_recomputed(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path / "span")
    reconciliation = json.loads(args.account_reconciliation.read_text())
    reconciliation["observation_span_seconds"] = 999
    _owner_json(args.account_reconciliation, reconciliation)
    try:
        with pytest.raises(module.FinalizationError, match="was not recomputed"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)

    args2, _, lock_fd2 = _campaign(module, tmp_path / "monotonic")
    reconciliation2 = json.loads(args2.account_reconciliation.read_text())
    reconciliation2["stable_observations"][1]["usage"] = "13.5"
    _owner_json(args2.account_reconciliation, reconciliation2)
    try:
        with pytest.raises(module.FinalizationError, match="not monotonic"):
            module.run_finalization(args2)
    finally:
        os.close(lock_fd2)


def test_missing_router_receipt_is_fatal_but_monotonic_backfill_is_allowed(
    module, tmp_path: Path
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path / "fatal", with_repair=False)
    rows = [json.loads(line) for line in args.result[0].read_text().splitlines()]
    rows[0]["execution"]["generation_attempts"][0]["run"]["usage"]["model_usage_breakdown"][0][
        "provider_usage"
    ].pop("router_metadata")
    rows[0] = module.seal_result_row(rows[0])
    args.result[0].write_text("".join(json.dumps(row) + "\n" for row in rows))
    try:
        with pytest.raises(module.FinalizationError, match="physical generation route"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)

    args2, _, lock_fd2 = _campaign(module, tmp_path / "backfill")
    first_rows = [json.loads(line) for line in args2.result[0].read_text().splitlines()]
    first_rows[0]["execution"]["generation_attempts"][0]["run"]["usage"]["model_usage_breakdown"][
        0
    ]["provider_usage"].pop("router_metadata")
    first_rows[0] = module.seal_result_row(first_rows[0])
    args2.result[0].write_text("".join(json.dumps(row) + "\n" for row in first_rows))
    try:
        assert module.run_finalization(args2)["status"] == "complete"
    finally:
        os.close(lock_fd2)


def test_generation_receipt_conflict_across_repair_is_fatal(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path)
    rows = [json.loads(line) for line in args.result[1].read_text().splitlines()]
    unit = rows[0]["execution"]["generation_attempts"][0]["run"]["usage"]["model_usage_breakdown"][
        0
    ]
    unit["provider_usage"]["response_ids"] = ["conflicting-response"]
    unit["provider_usage"]["provider_reported_cost"] = 0.2
    unit["billed_cost"] = 0.2
    rows[0] = module.seal_result_row(rows[0])
    args.result[1].write_text("".join(json.dumps(row) + "\n" for row in rows))
    try:
        with pytest.raises(module.FinalizationError, match="receipt repair conflicts"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)


def test_judge_receipt_conflict_and_missing_route_are_fatal(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path / "conflict")
    rows = [json.loads(line) for line in args.result[1].read_text().splitlines()]
    unit = rows[0]["judge"]["criterion_judgments"][0]["judge_attempts"][0]["run"]["usage"][
        "model_usage_breakdown"
    ][0]
    unit["provider_usage"]["response_ids"] = ["conflicting-judge-response"]
    rows[0] = module.seal_result_row(rows[0])
    args.result[1].write_text("".join(json.dumps(row) + "\n" for row in rows))
    try:
        with pytest.raises(module.FinalizationError, match="receipt repair conflicts"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)

    args2, _, lock_fd2 = _campaign(module, tmp_path / "missing", with_repair=False)
    rows2 = [json.loads(line) for line in args2.result[0].read_text().splitlines()]
    judge_unit = rows2[0]["judge"]["criterion_judgments"][0]["judge_attempts"][0]["run"]["usage"][
        "model_usage_breakdown"
    ][0]
    judge_unit["provider_usage"].pop("router_metadata")
    rows2[0] = module.seal_result_row(rows2[0])
    args2.result[0].write_text("".join(json.dumps(row) + "\n" for row in rows2))
    try:
        with pytest.raises(module.FinalizationError, match="Judge attempt"):
            module.run_finalization(args2)
    finally:
        os.close(lock_fd2)


def test_selected_answer_must_bind_to_unique_successful_attempt(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    rows = [json.loads(line) for line in args.result[0].read_text().splitlines()]
    rows[0]["execution"]["generation_attempts"][0]["run"]["final_text_sha256"] = module.text_sha256(
        "different answer"
    )
    rows[0] = module.seal_result_row(rows[0])
    args.result[0].write_text("".join(json.dumps(row) + "\n" for row in rows))
    try:
        with pytest.raises(module.FinalizationError, match="not bound to exactly one"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)


def test_judge_result_must_bind_to_final_successful_attempt(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    rows = [json.loads(line) for line in args.result[0].read_text().splitlines()]
    judgment = rows[0]["judge"]["criterion_judgments"][0]
    judgment["met"] = False
    judgment["verdict"] = "UNMET"
    rows[0] = module.seal_result_row(rows[0])
    args.result[0].write_text("".join(json.dumps(row) + "\n" for row in rows))
    try:
        with pytest.raises(module.FinalizationError, match="not bound to its final"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)


def test_successful_judge_unit_cannot_spend_again_in_repair_wave(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path)
    rows = [json.loads(line) for line in args.result[1].read_text().splitlines()]
    judgment = rows[0]["judge"]["criterion_judgments"][0]
    extra = deepcopy(judgment["judge_attempts"][0])
    extra["attempt_id"] = "e" * 32
    extra["attempt"] = 2
    judgment["judge_attempts"].append(extra)
    judgment["judge_attempt_count"] = 2
    judgment["judge_attempt_budget_used"] = 2
    judgment["judge_attempt_budget_remaining"] = 1
    judgment["judge_new_attempt_count"] = 1
    rows[0]["judge"]["judge_attempt_count"] += 1
    rows[0]["judge"]["judge_new_attempt_count"] = 1
    rows[0] = module.seal_result_row(rows[0])
    args.result[1].write_text("".join(json.dumps(row) + "\n" for row in rows))
    try:
        with pytest.raises(module.FinalizationError, match="terminal state"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)


def test_pass_rate_uses_negative_criterion_runner_semantics(module) -> None:
    row = {
        "judge": {
            "criterion_judgments": [
                {"weight": 10, "met": True},
                {"weight": -5, "met": False},
            ]
        }
    }
    assert module.row_pass_rate(row) == 1


def test_b2_rejects_analyzer_and_g1_requires_it(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path / "b2", with_repair=False)
    rows = [json.loads(line) for line in args.result[0].read_text().splitlines()]
    b2 = next(row for row in rows if row["group"] == "B2")
    analyzer = _receipt("evil-b2-analyzer", module.B0_MODEL)
    analyzer["role"] = "task_analyzer"
    attempt_run = b2["execution"]["generation_attempts"][0]["run"]
    attempt_run["usage"]["model_usage_breakdown"].append(analyzer)
    attempt_run["llm_request_count"] = 2
    b2_index = rows.index(b2)
    rows[b2_index] = module.seal_result_row(b2)
    args.result[0].write_text("".join(json.dumps(row) + "\n" for row in rows))
    try:
        with pytest.raises(module.FinalizationError, match="physical generation route"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)

    args2, _, lock_fd2 = _campaign(module, tmp_path / "g1", with_repair=False)
    rows2 = [json.loads(line) for line in args2.result[0].read_text().splitlines()]
    g1 = next(row for row in rows2 if row["group"] == "G1")
    attempt_run2 = g1["execution"]["generation_attempts"][0]["run"]
    attempt_run2["usage"]["model_usage_breakdown"] = attempt_run2["usage"]["model_usage_breakdown"][
        1:
    ]
    attempt_run2["llm_request_count"] = 1
    g1_index = rows2.index(g1)
    rows2[g1_index] = module.seal_result_row(g1)
    args2.result[0].write_text("".join(json.dumps(row) + "\n" for row in rows2))
    try:
        with pytest.raises(module.FinalizationError, match="physical generation route"):
            module.run_finalization(args2)
    finally:
        os.close(lock_fd2)


def test_g1_proposer_count_above_frozen_ranking_bound_is_rejected(module) -> None:
    ranking = _test_ranking_config(module)
    module.FORMAL_G1_RANKING_CONFIG_SHA256 = module.canonical_sha256(ranking)
    routes = {f"vendor/model-{index}": "vendor" for index in range(6)}
    routes_hash = module.canonical_sha256(routes)
    contract = {
        "profile_id": "test-g1",
        "selection_mode": "router_dynamic",
        "user_profile_enabled": False,
        "source_registry_snapshot_version": "test-registry-v1",
        "expected_routes": routes,
        "expected_routes_sha256": routes_hash,
        "expected_candidate_count": 6,
        "expected_source_registry_snapshot_sha256": (
            module.FORMAL_G1_SOURCE_REGISTRY_SNAPSHOT_SHA256
        ),
        "expected_ranking_config_schema_version": (module.FORMAL_G1_RANKING_CONFIG_SCHEMA_VERSION),
        "expected_ranking_config_version": (module.FORMAL_G1_RANKING_CONFIG_VERSION),
        "expected_ranking_config_sha256": (module.FORMAL_G1_RANKING_CONFIG_SHA256),
        "expected_proposer_count_max": 5,
    }
    identities = [f"openrouter:{model}" for model in routes]
    version = f"test-registry-v1+test-g1+{routes_hash[:12]}"
    plan = {
        "candidate_allowlist": {
            "policy": "exact_openrouter_routes",
            "profile_id": "test-g1",
            "source_registry_snapshot_version": "test-registry-v1",
            "expected_source_registry_snapshot_sha256": (
                module.FORMAL_G1_SOURCE_REGISTRY_SNAPSHOT_SHA256
            ),
            "filtered_registry_snapshot_version": version,
            "expected_routes_sha256": routes_hash,
            "expected_candidate_count": 6,
            "candidate_count": 6,
            "expected_identities": identities,
        },
        "candidate_pool_size": 6,
        "candidate_pool": [{"identity": value} for value in identities],
        "registry_snapshot_version": version,
        "registry_snapshot_hash": TEST_G1_RUNTIME_REGISTRY_HASH,
        "ranking_config_schema_version": (module.FORMAL_G1_RANKING_CONFIG_SCHEMA_VERSION),
        "ranking_config_version": module.FORMAL_G1_RANKING_CONFIG_VERSION,
        "ranking_config_hash": module.FORMAL_G1_RANKING_CONFIG_SHA256,
        "ranking_parameters": ranking,
        "task_profile": {
            "tier_dist": {"4": 1.0},
            "constraints": {
                "cost": "medium",
                "latency": "normal",
                "risk": "low",
            },
        },
        "N_min": 3,
        "N_max": 5,
        "bound_reasons": ["tier_4"],
        "selected_P": identities,
        "selected_A": identities[0],
        "proposer_models": list(routes),
        "proposer_sample_count": 6,
        "proposer_count": 6,
        "aggregator_model": next(iter(routes)),
        "selection_steps": [
            {"step": index, "selected": identity}
            for index, identity in enumerate(identities, start=1)
        ],
    }
    reasons, _, _ = module.g1_registry_plan_reasons(plan, contract=contract)
    assert "g1_selected_proposer_count_outside_bounds" in reasons


def test_selected_generation_cost_is_reconciled_to_physical_ledger(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    rows = [json.loads(line) for line in args.result[0].read_text().splitlines()]
    rows[0]["cost_accounting"]["selected_generation_attempt"]["recorded_cost_usd"] = 9.9
    rows[0] = module.seal_result_row(rows[0])
    args.result[0].write_text("".join(json.dumps(row) + "\n" for row in rows))
    try:
        with pytest.raises(module.FinalizationError, match="cost conflicts with ledger"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)


def _g1_retrospective_fixture(module, tmp_path: Path):
    task = {
        "id": "g1-retrospective-task",
        "prompt": "research this",
        "rubric": {
            "id": "rubric-1",
            "sections": [
                {
                    "id": "quality",
                    "title": "Quality",
                    "criteria": [
                        {"id": "core", "weight": 85, "requirement": "core"},
                        {"id": "extra", "weight": 15, "requirement": "extra"},
                    ],
                }
            ],
        },
    }
    module.FORMAL_G1_RANKING_CONFIG_SHA256 = module.canonical_sha256(_test_ranking_config(module))
    contract = _contract(module, "G1", "a" * 64)
    fingerprint = module.canonical_sha256(contract, prefix=True)
    pre = deepcopy(
        _row(
            module,
            group="G1",
            task=task,
            fingerprint=fingerprint,
            response_prefix="g1-retrospective",
        )
    )
    restored_routing = deepcopy(pre["routing_trace"])
    original_attempt = pre["execution"]["generation_attempts"][0]
    analyzer = deepcopy(pre["usage"]["model_usage_breakdown"][0])
    selected_unit = deepcopy(pre["usage"]["model_usage_breakdown"][1])
    selected_attempt_id = "2" * 32
    post_attempt_id = "3" * 32
    attempt_1 = {
        **deepcopy(original_attempt),
        "attempt_id": "1" * 32,
        "attempt": 1,
        "retry_reason": "provider_lifecycle_setup_retry",
        "will_retry": True,
        "run": {
            "error": "provider_lifecycle_setup_retry",
            "final_text_sha256": module.text_sha256("discarded setup answer"),
            "llm_request_count": 1,
            "routing_trace": deepcopy(restored_routing),
            "usage": {"model_usage_breakdown": [analyzer]},
        },
    }
    attempt_2 = {
        **deepcopy(original_attempt),
        "attempt_id": selected_attempt_id,
        "attempt": 2,
        "started_at": 1_006.0,
        "completed_at": 1_007.0,
        "retry_reason": "",
        "will_retry": False,
        "run": {
            "error": "",
            "final_text_sha256": pre["final_text_sha256"],
            "llm_request_count": 1,
            "routing_trace": {},
            "usage": {"model_usage_breakdown": [selected_unit]},
        },
    }
    pre.update(
        {
            "routing_trace": {},
            "llm_request_count": 1,
            "usage": {
                "model_usage_breakdown": [selected_unit],
                "billed_cost": 0.1,
            },
            "generation_attempt_count": 2,
            "generation_attempt_budget_used": 2,
            "generation_attempt_total_billed_cost": 0.2,
            "generation_max_attempts": 3,
        }
    )
    pre["actual_spend_metrics"]["generation_attempt_count"] = 2
    pre["cost_accounting"]["selected_generation_attempt"].update(
        {
            "recorded_cost_usd": 0.1,
            "request_count": 1,
        }
    )
    pre["execution"].update(
        {
            "prior_generation_attempts_used": 0,
            "selected_generation_attempt": 2,
            "generation_max_attempts": 3,
            "generation_attempts": [attempt_1, attempt_2],
        }
    )
    recovered_routing, recovery_evidence, recovery_reasons = module.effective_g1_lifecycle_routing(
        pre, contract=contract
    )
    assert recovery_reasons == []
    assert recovered_routing == restored_routing
    assert (
        module.generation_reasons(
            module.SourceRecord(tmp_path / "pre.jsonl", 0, 1, pre),
            task=task,
            expected_fingerprint=fingerprint,
            contract=contract,
        )
        == []
    )

    post = deepcopy(pre)
    post_attempt = {
        "attempt_id": post_attempt_id,
        "attempt_kind": "generation",
        "attempt": 3,
        "started_at": 1_020.0,
        "completed_at": 1_021.0,
        "retry_reason": module.LEGACY_TERMINAL_POLICY_ERROR,
        "will_retry": False,
        "run": {
            "error": module.LEGACY_TERMINAL_POLICY_ERROR,
            "final_text_sha256": module.text_sha256("failed attempt three"),
            "llm_request_count": 1,
            "routing_trace": {},
            "usage": {
                "model_usage_breakdown": [
                    _receipt(
                        "g1-retrospective-attempt-3",
                        "z-ai/glm-5.2",
                        cost=0.3,
                    )
                ]
            },
        },
    }
    post.update(
        {
            "selected_generation_succeeded": False,
            "error": module.LEGACY_TERMINAL_POLICY_ERROR,
            "generation_attempt_count": 1,
            "generation_attempt_budget_used": 3,
            "generation_attempt_total_billed_cost": 0.3,
            "completed_at": 1_022.0,
        }
    )
    post["completion_status"]["generation_accepted"] = False
    post["actual_spend_metrics"]["generation_attempt_count"] = 1
    post["execution"].update(
        {
            "run_error": module.LEGACY_TERMINAL_POLICY_ERROR,
            "prior_generation_attempts_used": 2,
            "selected_generation_attempt": 3,
            "generation_attempts": [post_attempt],
        }
    )

    repair = deepcopy(pre)
    repair["routing_trace"] = recovered_routing
    repair["completed_at"] = 1_030.0
    repair["generation_attempt_budget_used"] = 3
    repair["execution"].update(
        {
            "prior_generation_attempts_used": 3,
            "resume_action": "metadata_only",
            "generation_reused": True,
            "metadata_repair_attempted": True,
            "metadata_repaired": True,
            "judge_reran": False,
            "g1_provider_lifecycle_routing_recovery": recovery_evidence,
        }
    )
    repair["resume_completion"] = {
        "action": "metadata_only",
        "generation_reused": True,
        "metadata_repaired": True,
        "judge_reran": False,
        "post_repair_action": "complete",
        "status": "complete",
        "incomplete_reasons": [],
    }
    records = [
        module.SourceRecord(tmp_path / "pre.jsonl", 0, 1, module.seal_result_row(pre)),
        module.SourceRecord(tmp_path / "post.jsonl", 1, 1, module.seal_result_row(post)),
        module.SourceRecord(tmp_path / "repair.jsonl", 2, 1, module.seal_result_row(repair)),
    ]
    manifest_sources = [
        {
            "resume_schedule_contract_verified": False,
            "resume_scheduled_pairs": [],
        },
        {
            "resume_schedule_contract_verified": True,
            "resume_scheduled_pairs": [
                {
                    "group": "G1",
                    "task_id": task["id"],
                    "action": "regenerate",
                }
            ],
        },
        {
            "resume_schedule_contract_verified": True,
            "resume_scheduled_pairs": [
                {
                    "group": "G1",
                    "task_id": task["id"],
                    "action": "metadata_only",
                }
            ],
        },
    ]
    return (
        task,
        contract,
        fingerprint,
        records,
        manifest_sources,
        selected_attempt_id,
        post_attempt_id,
        restored_routing,
    )


def _select_g1_retrospective(
    module,
    task,
    contract,
    fingerprint,
    records,
    manifest_sources,
):
    return module.select_results(
        records,
        tasks=[task],
        groups=["G1"],
        fingerprints={"G1": fingerprint},
        contracts={"G1": contract},
        max_attempts=3,
        manifest_sources=manifest_sources,
    )


def test_g1_retrospective_selects_attempt_two_and_keeps_attempt_three_spend(
    module,
    tmp_path: Path,
) -> None:
    (
        task,
        contract,
        fingerprint,
        records,
        manifest_sources,
        selected_attempt_id,
        post_attempt_id,
        _,
    ) = _g1_retrospective_fixture(module, tmp_path)

    selected, pair_audit = _select_g1_retrospective(
        module,
        task,
        contract,
        fingerprint,
        records,
        manifest_sources,
    )
    assert selected == [records[2]]
    recovery = pair_audit[f"G1/{task['id']}"]["retrospective_reclassification_recovery"]
    assert recovery["selected_attempt"] == 2
    assert recovery["selected_attempt_id"] == selected_attempt_id
    assert recovery["invalid_post_accept_attempt_ids"] == [post_attempt_id]

    bindings = module.bind_selected_generation_attempts(records, selected)
    assert bindings == {f"G1/{task['id']}": selected_attempt_id}
    ledger, _ = module.build_actual_spend_ledger(
        records,
        selected=selected,
        selected_attempt_bindings=bindings,
    )
    by_attempt = {
        str(reference["attempt_id"]): row
        for row in ledger
        for reference in row["source_references"]
        if reference.get("phase") == "generation"
    }
    assert by_attempt[selected_attempt_id]["generation_disposition"] == "selected"
    assert by_attempt[selected_attempt_id]["physical_source"]["source_index"] == 0
    assert by_attempt[selected_attempt_id]["receipt_source_indexes"] == [2]
    assert by_attempt[post_attempt_id]["generation_disposition"] == "failed"
    assert by_attempt[post_attempt_id]["physical_source"]["source_index"] == 1
    assert by_attempt[post_attempt_id]["receipt_source_indexes"] == [1]
    assert by_attempt[post_attempt_id]["recorded_cost_usd"] == "0.300000000"


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_manifest_action",
        "wrong_post_error",
        "post_will_retry",
        "extra_post_attempt",
        "pre_top_routing_present",
    ],
)
def test_g1_retrospective_exception_fails_closed(
    module,
    tmp_path: Path,
    mutation: str,
) -> None:
    (
        task,
        contract,
        fingerprint,
        records,
        manifest_sources,
        _,
        _,
        restored_routing,
    ) = _g1_retrospective_fixture(module, tmp_path)
    if mutation == "wrong_manifest_action":
        manifest_sources[1]["resume_scheduled_pairs"][0]["action"] = "metadata_only"
    elif mutation == "wrong_post_error":
        records[1].row["error"] = "different_error"
    elif mutation == "post_will_retry":
        records[1].row["execution"]["generation_attempts"][0]["will_retry"] = True
    elif mutation == "extra_post_attempt":
        extra = deepcopy(records[1].row["execution"]["generation_attempts"][0])
        extra["attempt_id"] = "4" * 32
        records[1].row["execution"]["generation_attempts"].append(extra)
    else:
        records[0].row["routing_trace"] = deepcopy(restored_routing)

    with pytest.raises(module.FinalizationError):
        _select_g1_retrospective(
            module,
            task,
            contract,
            fingerprint,
            records,
            manifest_sources,
        )


def _install_complete_metadata_schedule(module, args: argparse.Namespace) -> dict:
    manifest = json.loads(args.manifest[1].read_text())
    rows = [json.loads(line) for line in args.result[1].read_text().splitlines()]
    scheduled_pairs = [
        {
            "group": str(row["group"]),
            "task_id": str(row["task_id"]),
            "action": "metadata_only",
        }
        for row in rows
    ]
    pair_count = len(scheduled_pairs)
    manifest["resume_selection"] = {
        "selected_pair_count": pair_count,
        "scheduled_pair_count": pair_count,
        "regenerate_pair_count": 0,
        "model_regenerate_pair_count": 0,
        "judge_only_pair_count": 0,
        "metadata_only_pair_count": pair_count,
        "policy_violation_pair_count": 0,
        "scheduled_pairs": scheduled_pairs,
        "resume_action_counts": {
            "policy_violation": 0,
            "regenerate": 0,
            "judge_only": 0,
            "metadata_only": pair_count,
        },
    }
    _owner_json(args.manifest[1], manifest)
    return manifest


@pytest.mark.parametrize("tamper", ["counter", "result_pair_set"])
def test_manifest_resume_schedule_contract_rejects_counter_or_set_tamper(
    module,
    tmp_path: Path,
    tamper: str,
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path)
    try:
        manifest = _install_complete_metadata_schedule(module, args)
        resume = manifest["resume_selection"]
        if tamper == "counter":
            resume["scheduled_pair_count"] -= 1
        else:
            resume["scheduled_pairs"].pop()
            resume["selected_pair_count"] -= 1
            resume["scheduled_pair_count"] -= 1
            resume["metadata_only_pair_count"] -= 1
            resume["resume_action_counts"]["metadata_only"] -= 1
        _owner_json(args.manifest[1], manifest)

        with pytest.raises(
            module.FinalizationError,
            match="resume schedule counters differ from its result shard",
        ):
            module.load_manifest_contracts(
                args.manifest,
                result_paths=args.result,
                groups=module.GROUPS,
            )
    finally:
        os.close(lock_fd)
