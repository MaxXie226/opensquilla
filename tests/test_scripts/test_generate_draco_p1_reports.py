from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

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
        "comparison_warnings": [],
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
                "scope": "hit_gate_matched_tasks",
                "comparison_role": "primary",
                "pair_count": 10,
                "expected_task_count": 10,
                "pairing_complete_for_scope": True,
                "mean_delta_quality": 0.1,
                "bootstrap_ci95": [-1.0, 1.0],
                "wins": 5,
                "ties": 1,
                "losses": 4,
            }
        ],
        plan={
            "run_id": "p1-test",
            "semantic_contract": module.SEMANTIC_CONTRACT,
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
    assert "Primary hit-gated slice" in markdown
    assert "scope=`hit_gate_matched_tasks`" in markdown
    assert "n=10/10" in markdown
    assert "pair_complete=`true`" in markdown


def test_root_report_keeps_account_and_theoretical_cost_separate() -> None:
    report = {
        "run_id": "p1-test",
        "semantic_contract": module.SEMANTIC_CONTRACT,
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


def _hit_decision(task_ids: list[str], *, decision: str = "eligible") -> dict:
    return {
        "decision": decision,
        "gate": {
            "metric": "generation_retry_count",
            "op": "gt",
            "threshold": 0,
            "minimum_tasks": 1,
        },
        "matched_task_ids": task_ids,
        "matched_task_count": len(task_ids),
    }


def test_matched_task_ids_require_eligible_nonempty_exact_gate() -> None:
    arm = SimpleNamespace(
        arm_id="P1-35-E1",
        hit_gate=SimpleNamespace(
            metric="generation_retry_count", op="gt", threshold=0, minimum_tasks=1
        ),
    )
    decisions = {arm.arm_id: _hit_decision(["task-1", "task-3"])}
    assert module.matched_task_ids_for_arm(
        arm, decisions, ["task-1", "task-2", "task-3"]
    ) == ["task-1", "task-3"]

    decisions[arm.arm_id] = _hit_decision([], decision="no_hit")
    with pytest.raises(module.ReportError, match="no eligible"):
        module.matched_task_ids_for_arm(
            arm, decisions, ["task-1", "task-2", "task-3"]
        )

    decisions[arm.arm_id] = _hit_decision(["task-1"])
    decisions[arm.arm_id]["gate"]["threshold"] = 1
    with pytest.raises(module.ReportError, match="contract differs"):
        module.matched_task_ids_for_arm(
            arm, decisions, ["task-1", "task-2", "task-3"]
        )


def test_scoped_comparison_declares_scope_n_and_relative_completeness() -> None:
    observed: dict = {}

    class Common:
        @staticmethod
        def paired(
            control: dict,
            candidate: dict,
            *,
            scope: str,
            allowed_task_ids: set[str],
        ) -> dict:
            observed.update(
                {
                    "control": control["spec"]["arm_id"],
                    "candidate": candidate["spec"]["arm_id"],
                    "scope": scope,
                    "allowed": allowed_task_ids,
                }
            )
            task_ids = sorted(allowed_task_ids)
            return {
                "control_arm_id": control["spec"]["arm_id"],
                "variant_arm_id": candidate["spec"]["arm_id"],
                "scope": scope,
                "pair_count": len(task_ids),
                "complete_task_id_pairing": len(task_ids) == 10,
                "task_rows": [{"task_id": task_id} for task_id in task_ids],
            }

    comparison = module.build_scoped_comparison(
        Common(),
        {"analyzer_mode": "frozen_replay", "rows": []},
        {"analyzer_mode": "frozen_replay", "rows": []},
        control_arm_id="common-E0-R1",
        variant_arm_id="P1-35-E1",
        scope="hit_gate_matched_tasks",
        comparison_role="primary",
        expected_task_ids=["task-1", "task-3"],
    )
    assert observed["allowed"] == {"task-1", "task-3"}
    assert comparison["comparison_role"] == "primary"
    assert comparison["pair_count"] == comparison["expected_task_count"] == 2
    assert comparison["complete_ten_task_id_pairing"] is False
    assert comparison["complete_task_id_pairing"] is False
    assert comparison["pairing_complete_for_scope"] is True

    full = module.build_scoped_comparison(
        Common(),
        {"analyzer_mode": "frozen_replay", "rows": []},
        {"analyzer_mode": "frozen_replay", "rows": []},
        control_arm_id="common-E0-R1",
        variant_arm_id="P1-35-E1",
        scope="all_tasks_secondary_diagnostic",
        comparison_role="secondary",
        expected_task_ids=[f"task-{index}" for index in range(10)],
    )
    assert full["complete_task_id_pairing"] is True
    assert full["pairing_complete_for_scope"] is True


def test_v1_plan_is_rejected_before_its_frozen_modules_can_self_validate(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "legacy-snapshot"
    scripts = snapshot / "scripts/experiments"
    scripts.mkdir(parents=True)
    controller_path = scripts / "run_draco_p1_tuning_campaign.py"
    common_path = scripts / "generate_draco_p0_p05_reports.py"
    controller_path.write_text("LEGACY_ACCEPTS_V1 = True\n", encoding="utf-8")
    common_path.write_text("LEGACY_REPORTER = True\n", encoding="utf-8")
    plan = {
        "schema": "opensquilla.draco-p1-campaign-plan/v1",
        "paths": {"snapshot": str(snapshot)},
        "freeze": {
            "sources": {
                "controller_raw_sha256": module.file_sha256(controller_path),
                "common_reporter_raw_sha256": module.file_sha256(common_path),
            }
        },
    }
    legacy_controller, _ = module.load_frozen_modules(plan)
    assert legacy_controller.LEGACY_ACCEPTS_V1 is True
    plan_path = tmp_path / "legacy-plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    args = SimpleNamespace(
        plan=plan_path,
        status=None,
        output_root=None,
        allow_nonterminal=False,
    )
    with pytest.raises(module.ReportError, match="campaign plan schema differs"):
        module.generate(args)


def test_terminal_report_is_incomplete_when_derived_evidence_is_missing() -> None:
    loaded = {
        "P1-35-E1": {
            "state": "succeeded",
            "formal_evidence_valid": True,
            "decision_evidence_valid": False,
        }
    }
    assert module.terminal_report_complete(
        "completed",
        loaded,
        ["derived plan unavailable"],
        terminal_arm_states={"succeeded"},
    ) is False


def test_no_hit_skip_requires_exact_status_decision() -> None:
    decision = {"decision": "no_hit", "matched_task_ids": []}
    arm = SimpleNamespace(
        arm_id="P1-35-E1", experiment_id="P1-35", hit_gate=object()
    )
    loaded = {
        arm.arm_id: {
            "state": "no_hit_skipped",
            "decision_evidence_valid": None,
            "decision_evidence_reasons": [],
        }
    }

    class Controller:
        @staticmethod
        def authenticate_hit_decision(_plan: dict, _arm: object, raw: object) -> list[str]:
            assert raw == decision
            return []

    status = {"arms": {arm.arm_id: {"hit_gate_evidence": decision}}}
    assert module.authenticate_terminal_decisions(
        {"paths": {"run_root": "/tmp/run"}},
        status,
        [arm],
        loaded,
        {"hit_decisions": {arm.arm_id: decision}},
        controller=Controller(),
    ) == []
    assert loaded[arm.arm_id]["decision_evidence_valid"] is True

    loaded[arm.arm_id]["decision_evidence_reasons"] = []
    status["arms"][arm.arm_id]["hit_gate_evidence"] = {"decision": "tampered"}
    reasons = module.authenticate_terminal_decisions(
        {"paths": {"run_root": "/tmp/run"}},
        status,
        [arm],
        loaded,
        {"hit_decisions": {arm.arm_id: decision}},
        controller=Controller(),
    )
    assert reasons and loaded[arm.arm_id]["decision_evidence_valid"] is False


def test_progression_skip_requires_authenticated_receipt_and_status_hash() -> None:
    decision = {"decision": "eligible", "matched_task_ids": ["task-1"]}
    p1_35_decision = {"decision": "eligible", "matched_task_ids": ["task-1"]}
    receipt = {
        "decision": "skip_p1_15_sufficient",
        "receipt_sha256": "a" * 64,
    }
    arm = SimpleNamespace(
        arm_id="P1-15-E1", experiment_id="P1-15", hit_gate=object()
    )
    loaded = {
        arm.arm_id: {
            "state": "progression_skipped",
            "decision_evidence_valid": None,
            "decision_evidence_reasons": [],
        }
    }

    class Controller:
        @staticmethod
        def authenticate_hit_decision(_plan: dict, _arm: object, raw: object) -> list[str]:
            assert raw == decision
            return ["task-1"]

        @staticmethod
        def load_progression_receipt(
            _plan: dict,
            _path: Path,
            *,
            hit_decision: dict,
            hit_receipt_sha256: str,
        ) -> dict:
            assert hit_decision == p1_35_decision
            assert hit_receipt_sha256 == "b" * 64
            return receipt

    status = {
        "arms": {
            arm.arm_id: {
                "hit_gate_evidence": decision,
                "progression_receipt_sha256": "a" * 64,
            }
        }
    }
    reasons = module.authenticate_terminal_decisions(
        {"paths": {"run_root": "/tmp/run"}},
        status,
        [arm],
        loaded,
        {
            "hit_decisions": {
                arm.arm_id: decision,
                "P1-35-E1": p1_35_decision,
            },
            "hit_receipt_sha256": "b" * 64,
        },
        controller=Controller(),
    )
    assert reasons == []
    assert loaded[arm.arm_id]["decision_evidence_valid"] is True

    status["arms"][arm.arm_id]["progression_receipt_sha256"] = "c" * 64
    loaded[arm.arm_id]["decision_evidence_reasons"] = []
    reasons = module.authenticate_terminal_decisions(
        {"paths": {"run_root": "/tmp/run"}},
        status,
        [arm],
        loaded,
        {
            "hit_decisions": {
                arm.arm_id: decision,
                "P1-35-E1": p1_35_decision,
            },
            "hit_receipt_sha256": "b" * 64,
        },
        controller=Controller(),
    )
    assert reasons and loaded[arm.arm_id]["decision_evidence_valid"] is False
