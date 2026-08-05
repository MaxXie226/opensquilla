from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts/experiments/generate_draco_p1_reports.py"
)
SPEC = importlib.util.spec_from_file_location("generate_draco_p1_reports", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def _metrics() -> dict:
    return {
        "row_count": 10,
        "done_count": 10,
        "avg_quality_total": 61.25,
        "avg_pass_rate": 0.63,
        "judge_error_count": 0,
        "avg_selected_generation_cost_usd": 1.2,
        "selected_generation_cost_counted_usd": 12.0,
        "selected_generation_cost_exact_task_count": 8,
        "avg_input_tokens": 100.0,
        "avg_output_tokens": 20.0,
        "avg_reasoning_tokens": 5.0,
        "avg_cached_tokens": 40.0,
        "avg_visible_tokens": 15.0,
        "avg_total_tokens": 120.0,
        "avg_tool_calls": 2.0,
        "tool_task_rate": 0.7,
        "avg_trajectory_steps": 3.0,
        "avg_llm_requests": 4.0,
        "latency_p50_ms": 500.0,
        "latency_p95_ms": 900.0,
        "n_distribution": {"3": 10},
        "fallback_task_count": 0,
        "outer_retry_count": 0,
        "partial_proposer_task_count": 0,
        "degraded_task_count": 0,
        "assembly_truncated_task_count": 0,
        "selected_generation_cost_actual_request_count": 20,
        "selected_generation_cost_estimated_request_count": 2,
        "selected_generation_cost_ignored_request_count": 1,
    }


def _arm() -> dict:
    return {
        "arm_id": "P1-35-E1",
        "experiment_id": "P1-35",
        "state": "succeeded",
        "formal_evidence_valid": True,
        "formal_evidence_reasons": [],
        "metrics": _metrics(),
        "statuses": {"execution": "pass", "policy": "warning", "audit": "warning"},
        "account": {
            "account_delta_usd": 13.0,
            "byok_delta_usd": 0.0,
            "reconciliation_stable": True,
        },
    }


def test_metric_row_uses_required_draco_columns() -> None:
    rendered = module.metric_row(_arm())
    assert rendered.startswith("| P1-35-E1 | 10 | 10 | 61.25 | 63.00% | 0 |")
    assert "| 8/10 |" in rendered
    assert module.TABLE_HEADER.splitlines()[0].count("|") == rendered.count("|")


def test_group_markdown_discloses_cost_and_safety_boundaries() -> None:
    screening_design = {
        "design_label": "anchored_serial_not_task_interleaved",
        "strict_task_interleaving": False,
        "task_interleaving_contract_satisfied": False,
        "mini_diagnostic_screening_only": True,
        "automatic_winner_promotion": False,
        "winner_or_combination_requires": "strict_task_interleaved_confirmatory",
    }
    markdown = module.build_group_markdown(
        "P1-35",
        [_arm()],
        [
            {
                "variant_arm_id": "P1-35-E1",
                "control_arm_id": "common-E0-R1",
                "pair_count": 10,
                "mean_delta_quality": 0.1,
                "bootstrap_ci95": [-1.0, 1.0],
                "wins": 5,
                "ties": 1,
                "losses": 4,
            }
        ],
        plan={
            "run_id": "p1-test",
            "freeze": {"snapshot_commit": "a" * 40, "snapshot_tree": "b" * 40},
        },
        hit_decisions={
            "P1-35-E1": {"decision": "eligible", "matched_task_count": 3}
        },
        screening_design=screening_design,
    )
    assert "DRACO mini 未提供独立 SafetyGate" in markdown
    assert "cache-aware" in markdown
    assert "Judge、失败/被替换 retry 不计入" in markdown
    assert "W/T/L=5/1/4" in markdown
    assert "diagnostic/screening only" in markdown
    assert "strict_task_interleaving=false" in markdown
    assert "task_interleaving_contract_satisfied=false" in markdown
    assert "automatic_winner_promotion=false" in markdown
    assert "strict task-interleaved confirmatory evaluation" in markdown


def test_root_report_keeps_account_and_theoretical_cost_separate() -> None:
    report = {
        "run_id": "p1-test",
        "phase": "completed",
        "formal_valid_arm_count": 1,
        "arm_count": 1,
        "arms": [_arm()],
        "screening_design": {
            "design_label": "anchored_serial_not_task_interleaved",
            "strict_task_interleaving": False,
            "task_interleaving_contract_satisfied": False,
            "mini_diagnostic_screening_only": True,
            "automatic_winner_promotion": False,
            "winner_or_combination_requires": "strict_task_interleaved_confirmatory",
        },
        "excluded": [
            {"id": "P1-06", "kind": "missing_feature", "reason": "unavailable"}
        ],
        "groups": [
            {
                "experiment_id": "P1-35",
                "arm_count": 1,
                "succeeded_count": 1,
                "skipped_count": 0,
                "failed_count": 0,
            }
        ],
    }
    markdown = module.build_root_markdown(report)
    assert "账户实际支出只取 reconciliation delta" in markdown
    assert "不与理论估算相加" in markdown
    assert "10 题 mini 是 diagnostic/screening only" in markdown
    assert "strict_task_interleaving=false" in markdown
    assert "task_interleaving_contract_satisfied=false" in markdown
    assert "automatic_winner_promotion=false" in markdown
    assert "strict task-interleaved confirmatory evaluation" in markdown


def test_natural_sort_splits_group_numbers() -> None:
    assert module.re_split("P1-4") == ["P", "1", "-", "4"]
    assert module.re_split("P1-35") == ["P", "1", "-", "35"]
