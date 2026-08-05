from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts/experiments/run_draco_p1_tuning_campaign.py"
)
SPEC = importlib.util.spec_from_file_location("run_draco_p1_tuning_campaign", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def _source_contract() -> dict:
    return {
        "schema": "opensquilla.draco-preexisting-analyzer-source/v1",
        "enabled": True,
        "source_plan_path": "/tmp/p0-plan.json",
        "source_plan_raw_sha256": "7" * 64,
        "source_plan_canonical_sha256": "8" * 64,
        "source_snapshot_path": "/tmp/p0-snapshot",
        "source_snapshot_commit": "9" * 40,
        "source_snapshot_tree": "a" * 40,
        "source_output_dir": "/tmp/p0-source",
        "source_manifest_sha256": "b" * 64,
        "source_results_sha256": "c" * 64,
        "source_trace_sha256": "d" * 64,
    }


def _plan() -> dict:
    contracts = module.expected_variant_contracts()
    experiments = []
    for group in sorted(module.SUPPORTED_GROUPS):
        variants = []
        for arm_id, contract in contracts.items():
            if contract["group"] != group:
                continue
            variants.append(
                {
                    "id": contract["variant"],
                    "override": contract["override"],
                    "hit_gate": contract["hit_gate"],
                    "analyzer_mode": contract["analyzer_mode"],
                    "priority": contract["priority"],
                    **(
                        {"control_arm_id": contract["control_arm_id"]}
                        if contract["control_arm_id"] is not None
                        else {}
                    ),
                }
            )
        experiments.append(
            {
                "id": group,
                "directory_name": group,
                "title": group,
                "variants": variants,
            }
        )
    live_ids = sorted(
        arm_id
        for arm_id, contract in contracts.items()
        if contract["analyzer_mode"] == "live"
    )
    replay_ids = sorted(
        arm_id
        for arm_id, contract in contracts.items()
        if contract["analyzer_mode"] == "frozen_replay"
        and arm_id not in {"P1-35-E1", "P1-15-E1", "P1-15-E2", "P1-21-E0", "P1-21-E1"}
    )
    order = [
        module.SOURCE_ARM_ID,
        *live_ids,
        "common-E0-R1",
        "P1-35-E1",
        "P1-15-E1",
        "P1-15-E2",
        "P1-21-E0",
        "P1-21-E1",
        *replay_ids,
        "common-E0-R2",
        "common-E0-R3",
    ]
    anchors = {
        arm_id: (
            arm_id
            if arm_id in {module.SOURCE_ARM_ID, *module.REPLAY_CONTROL_IDS, "P1-21-E0"}
            else module.SOURCE_ARM_ID
            if arm_id in live_ids
            else "P1-21-E0"
            if arm_id == "P1-21-E1"
            else "common-E0-R1"
        )
        for arm_id in order
    }
    control_overrides = {
        arm_id: anchor
        for arm_id, anchor in anchors.items()
        if arm_id not in {module.SOURCE_ARM_ID, *module.REPLAY_CONTROL_IDS, "P1-21-E0"}
    }
    return {
        "schema": module.PLAN_SCHEMA,
        "run_id": "p1-test-20260805",
        "source_plan": {
            "commit": "1" * 40,
            "blob_sha": "2" * 40,
            "raw_sha256": "3" * 64,
        },
        "freeze": {
            "snapshot_commit": "4" * 40,
            "snapshot_tree": "5" * 40,
            "ranking_thinking_assignment_enabled": False,
            "inputs": {},
            "sources": {},
            "model_registry": {},
            "ranking_config": {},
        },
        "paths": {
            "run_root": "/tmp/p1-run",
            "snapshot": "/tmp/p1-snapshot",
            "report_root": "/tmp/p1-reports",
            "reference_repo": "/tmp/reference",
            "python": "/usr/bin/python3",
            "reporter": "/tmp/p1-snapshot/scripts/experiments/generate_draco_p1_reports.py",
            "experiment_config_relative": "configs/benchmarks/draco_b2_g12.json",
            "launcher_relative": "scripts/experiments/run_draco_mini_b0_b1_b2_b4_g1_campaign.sh",
        },
        "benchmark": {
            "name": "DRACO mini",
            "input_sha256": "6" * 64,
            "task_count": 10,
            "task_ids": [f"task-{index}" for index in range(10)],
            "groups": ["G1"],
        },
        "execution": {
            "serial_arms": True,
            "task_concurrency": 6,
            "judge_concurrency": 6,
            "generation_max_attempts": 3,
            "continue_after_arm_failure": True,
            "global_openrouter_lock": "/tmp/p1.lock",
            "openrouter_secret_file": "/tmp/openrouter.env",
            "openrouter_credential_scope": "dedicated_key",
            "schedule": {
                "mode": "hit_gated_serial",
                "strict_task_interleaving": False,
                "design_label": "anchored_serial_not_task_interleaved",
                "arm_order": order,
                "anchor_by_arm_id": anchors,
            },
        },
        "comparison_controls": {
            "default_control_arm_id": "common-E0-R1",
            "arm_control_overrides": control_overrides,
        },
        "common_e0": [
            {
                "arm_id": module.SOURCE_ARM_ID,
                "variant": "E0-live",
                "analyzer_mode": "live",
                "override": {},
                "priority": 0,
            },
            *[
                {
                    "arm_id": arm_id,
                    "variant": "E0-replay",
                    "analyzer_mode": "frozen_replay",
                    "override": {},
                    "priority": 1,
                }
                for arm_id in module.REPLAY_CONTROL_IDS
            ],
        ],
        "experiments": experiments,
        "excluded": [
            {
                "id": group,
                "kind": "missing_feature",
                "reason": "feature unavailable",
            }
            for group in sorted(module.MISSING_FEATURE_GROUPS)
        ]
        + [
            {
                "id": group,
                "kind": "deterministic_no_hit",
                "reason": "frozen mini has no hit",
            }
            for group in sorted(module.DETERMINISTIC_NO_HIT_GROUPS)
        ],
        "progression": {
            "schema": "opensquilla.draco-p1-progression/v1",
            "first_arm_id": "P1-35-E1",
            "conditional_arm_ids": ["P1-15-E1", "P1-15-E2"],
            "control_arm_id": "common-E0-R1",
            "skip_p1_15_if_cost_reduction_fraction_at_least": 0.1,
            "minimum_mean_delta_quality": 0.0,
        },
        "runtime_contract": {
            "frozen_replay": {
                "schema": "opensquilla.draco.frozen-task-analysis/v2",
                "mode_path": ["g1_routing", "task_analysis_execution", "mode"],
                "mode_value": "frozen_replay",
                "payload_path": ["g1_routing", "task_analysis_execution"],
                "artifact_projection_key": "replay_payload",
            },
            "analyzer_source": {
                "schema": "opensquilla.draco-analyzer-source-policy/v1",
                "allow_deterministic_router_fallback": True,
            },
            "preexisting_source": _source_contract(),
        },
        "reporting": {
            "mini_is_diagnostic_only": True,
            "automatic_winner_promotion": False,
            "independent_safety_gate_available": False,
            "pairing_key": "task_id",
            "bootstrap_repetitions": 20_000,
        },
    }


def test_authoritative_matrix_validates_and_expands() -> None:
    plan = _plan()
    arms = module.validate_plan(plan, allow_placeholders=False)
    assert len(arms) == 43
    assert sum(arm.experiment_id != "common-E0" for arm in arms) == 39
    assert {
        arm.experiment_id for arm in arms if arm.experiment_id != "common-E0"
    } == module.SUPPORTED_GROUPS
    arm_ids = [arm.arm_id for arm in arms]
    assert arm_ids.index("P1-35-E1") < arm_ids.index("P1-15-E1")
    assert module.screening_design_contract(plan) == {
        "design_label": "anchored_serial_not_task_interleaved",
        "strict_task_interleaving": False,
        "task_interleaving_contract_satisfied": False,
        "mini_diagnostic_screening_only": True,
        "automatic_winner_promotion": False,
        "winner_or_combination_requires": "strict_task_interleaved_confirmatory",
    }


def test_missing_or_tampered_matrix_is_rejected() -> None:
    plan = _plan()
    plan["experiments"] = [row for row in plan["experiments"] if row["id"] != "P1-36"]
    with pytest.raises(module.ControllerError, match="inventory"):
        module.validate_plan(plan, allow_placeholders=False)
    plan = _plan()
    target = next(row for row in plan["experiments"] if row["id"] == "P1-35")
    target["variants"][0]["override"]["runner"]["retrieval_loop_finalization_threshold"] = 2
    with pytest.raises(module.ControllerError, match="parameter contract"):
        module.validate_plan(plan, allow_placeholders=False)
    plan = _plan()
    plan["execution"]["schedule"]["strict_task_interleaving"] = True
    with pytest.raises(module.ControllerError, match="anchored serial screening design"):
        module.validate_plan(plan, allow_placeholders=False)


def test_relative_secret_path_is_rejected() -> None:
    plan = _plan()
    plan["execution"]["openrouter_secret_file"] = "openrouter.env"
    with pytest.raises(module.ControllerError, match="secret-file"):
        module.validate_plan(plan, allow_placeholders=False)


def test_source_output_and_identity_reuse_authenticated_preexisting_e0(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan()
    source_arm = next(
        arm
        for arm in module.validate_plan(plan, allow_placeholders=False)
        if arm.arm_id == module.SOURCE_ARM_ID
    )
    expected = {"portable": "old-p0-publication"}

    class Common:
        @staticmethod
        def preexisting_source_identity(_plan: dict):
            return expected, {"receipt": "ok"}

    monkeypatch.setattr(module, "common_controller", lambda _snapshot=None: Common())
    assert module.output_dir(plan, source_arm) == Path("/tmp/p0-source")
    assert module.arm_completion_identity(
        plan,
        source_arm,
        snapshot=Path("/tmp/p1-snapshot"),
        snapshot_identity={"commit": "4" * 40, "tree": "5" * 40},
        override={},
    ) == expected


def test_arm_identity_projects_launcher_runner_concurrency_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan = _plan()
    plan["paths"]["snapshot"] = str(tmp_path)
    plan["paths"]["reference_repo"] = str(tmp_path / "reference")
    plan["paths"]["report_root"] = str(tmp_path / "reports")
    runner_dir = tmp_path / "scripts"
    runner_dir.mkdir()
    for name in (
        "run_draco_routing_experiment.py",
        "run_draco_routing_experiment_resume.py",
    ):
        (runner_dir / name).write_text("# frozen\n", encoding="utf-8")
    arm = next(
        arm
        for arm in module.validate_plan(plan, allow_placeholders=False)
        if arm.arm_id == "P1-18-E1"
    )

    class Ensemble:
        candidate_order_seed = None
        shuffle_candidates = False

    class Generation:
        max_attempts = 1

    class Config:
        ensemble = Ensemble()
        generation = Generation()

        @staticmethod
        def model_dump(mode: str) -> dict:
            assert mode == "json"
            return {
                "runner": {"concurrency": 2, "timeout_seconds": 100},
                "judge": {"concurrency": 6},
                "generation": {"max_attempts": 1},
                "ensemble": {
                    "candidate_order_seed": None,
                    "shuffle_candidates": False,
                },
                "unrelated": "retained",
            }

    class Common:
        @staticmethod
        def load_effective_experiment_config(*_args, **_kwargs):
            return Config()

    monkeypatch.setattr(module, "common_controller", lambda _snapshot=None: Common())
    identity = module.arm_completion_identity(
        plan,
        arm,
        snapshot=tmp_path,
        snapshot_identity={"commit": "4" * 40, "tree": "5" * 40},
        override=arm.override,
    )
    expected_config = Config.model_dump("json")
    expected_config["runner"]["concurrency"] = 6
    assert identity["effective_config_sha256"] == module.canonical_sha256(
        expected_config
    )
    assert identity["generation_max_attempts"] == 1


def test_launch_binds_declared_secret_file_without_logging_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = _plan()
    plan["paths"]["snapshot"] = str(tmp_path)
    plan["paths"]["report_root"] = str(tmp_path / "reports")
    plan["paths"]["reference_repo"] = str(tmp_path / "reference")
    plan["paths"]["python"] = "/usr/bin/python3"
    plan["execution"]["openrouter_secret_file"] = str(tmp_path / "secret.key")
    launcher = tmp_path / plan["paths"]["launcher_relative"]
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    arm = next(
        arm
        for arm in module.validate_plan(plan, allow_placeholders=False)
        if arm.arm_id == "P1-18-E1"
    )
    observed: dict = {}

    class Completed:
        returncode = 0

    def run(command: list[str], *, env: dict, check: bool) -> Completed:
        observed.update({"command": command, "env": env, "check": check})
        return Completed()

    monkeypatch.setattr(module.subprocess, "run", run)
    assert module.launch_arm(plan, arm, snapshot=tmp_path, override=arm.override) == 0
    assert observed["env"]["OPENSQUILLA_OPENROUTER_SECRET_FILE"] == str(
        tmp_path / "secret.key"
    )
    assert "OPENROUTER_API_KEY" not in observed["env"]
    output = capsys.readouterr().out
    assert "secret.key" not in output


def _row(task_id: str) -> dict:
    call = {
        "agent_call_duration_ms": 1_000_000,
        "candidates": [
            {
                "elapsed_ms": 350_000,
                "content": {"chars": 24_000, "truncated": True},
                "text": "x",
                "usable_for_aggregation": True,
            },
            {
                "elapsed_ms": 10_000,
                "content": {"chars": 1_000, "truncated": False},
                "text": "x",
                "usable_for_aggregation": True,
            },
        ],
        "aggregator_recovery": {"attempts": [{"stop_reason": "tool_calls"}]},
        "soft_deadline_triggered": True,
        "final_request": {
            "soft_deadline_replacement": True,
            "execution": {"effective_thinking": True},
        },
    }
    final = {
        **call,
        "aggregator_recovery": {"attempts": [{"stop_reason": "stop"}]},
    }
    return {
        "task_id": task_id,
        "generation_attempt_count": 2,
        "total_elapsed_ms": 10_300_000,
        "routing_trace": {
            "selection_plan": {
                "selected_P": ["a", "b"],
                "task_profile_pre_escalation": {
                    "constraints": {"cost": "low", "latency": "interactive"}
                },
                "task_analyzer": {
                    "source": "llm_provider",
                    "usage": {
                        "physical_attempts": [
                            {"usage_unknown": True},
                            {"usage_unknown": False},
                        ]
                    },
                },
            }
        },
        "ensemble_trace": {
            "calls": [call, call, call, final],
            "agent_iterations": 5,
            "usable_proposers": 1,
            "execution_quorum_required": 2,
            "fallback_used": True,
        },
        "run_trace": {
            "events": [
                {"tool_name": "web_search", "phase": "tool", "kind": "tool_use_start"},
                {"tool_name": "web_fetch", "phase": "tool", "kind": "tool_use_start"},
                {"message": "deadline_wrapup"},
            ]
        },
    }


def test_source_metrics_cover_conditional_slices() -> None:
    metrics = module.derive_source_task_metrics([_row("task-1")])["task-1"]
    assert metrics["analyzer_retry_or_fallback"] is True
    assert metrics["max_proposer_elapsed_ms"] == 350_000
    assert metrics["estimated_aggregator_elapsed_ms"] == 650_000
    assert metrics["generation_retry_count"] == 1
    assert metrics["web_search_calls"] == 1
    assert metrics["web_fetch_calls"] == 1
    assert metrics["candidate_at_current_cap"] is True
    assert metrics["deadline_wrapup_observed"] is True
    assert metrics["max_consecutive_retrieval_iterations"] == 3
    assert metrics["soft_deadline_finalizer_with_thinking"] is True
    assert metrics["constrained_at_two_proposers"] is True


def test_normal_final_answer_does_not_unlock_p1_36() -> None:
    row = _row("task-1")
    for call in row["ensemble_trace"]["calls"]:
        call.pop("soft_deadline_triggered", None)
        call["final_request"].pop("soft_deadline_replacement", None)
    metrics = module.derive_source_task_metrics([row])["task-1"]
    assert metrics["soft_deadline_finalizer_with_thinking"] is False


def test_quorum_tail_uses_usable_proposer_completion() -> None:
    row = _row("task-1")
    row["ensemble_trace"]["calls"] = [
        {
            "execution_quorum_required": 2,
            "candidates": [
                {"elapsed_ms": 5_000, "usable_for_aggregation": False},
                {"elapsed_ms": 10_000, "usable_for_aggregation": True},
                {"elapsed_ms": 20_000, "usable_for_aggregation": True},
                {"elapsed_ms": 30_000, "usable_for_aggregation": True},
            ],
        }
    ]
    metrics = module.derive_source_task_metrics([row])["task-1"]
    assert metrics["quorum_tail_ms"] == 10_000


def test_hit_receipt_is_task_bound_and_self_hashed(tmp_path: Path) -> None:
    plan = _plan()
    source = tmp_path / "source"
    source.mkdir()
    for name in ("manifest.json", "results.jsonl", "trace.jsonl"):
        (source / name).write_text(name, encoding="utf-8")
    arms = module.validate_plan(plan, allow_placeholders=False)
    receipt = module.derive_hit_receipt(plan, arms, rows=[_row("task-1")], source_dir=source)
    unsigned = dict(receipt)
    embedded = unsigned.pop("receipt_sha256")
    assert embedded == module.canonical_sha256(unsigned)
    assert receipt["decisions"]["P1-35-E1"]["decision"] == "eligible"
    assert receipt["decisions"]["P1-34-E1"]["decision"] == "no_hit"


def test_prepare_derived_consumes_controller_owned_source_package(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan = _plan()
    run_root = tmp_path / "run"
    source = tmp_path / "source"
    package = run_root / "preexisting-source-package"
    source.mkdir()
    package.mkdir(parents=True)
    plan["paths"]["run_root"] = str(run_root)
    plan["runtime_contract"]["preexisting_source"]["source_output_dir"] = str(source)
    for name in ("manifest.json", "results.jsonl", "trace.jsonl"):
        (package / name).write_text(name + "\n", encoding="utf-8")
    arms = module.validate_plan(plan, allow_placeholders=False)
    observed: dict = {}
    source_import = {
        "source_output_dir": str(source),
        "package_dir": str(package),
        "receipt_sha256": "e" * 64,
    }

    class Common:
        @staticmethod
        def materialize_preexisting_source(_plan: dict) -> dict:
            return source_import

        @staticmethod
        def extract_analyzer_artifact(**kwargs: object) -> dict:
            observed.update(kwargs)
            artifact = {"schema": "artifact", "artifact_sha256": "f" * 64}
            module.atomic_write_json(Path(str(kwargs["destination"])), artifact)
            return artifact

    monkeypatch.setattr(module, "common_controller", lambda _snapshot=None: Common())
    monkeypatch.setattr(module, "_result_rows", lambda _source, _snapshot: [_row("task-1")])
    derived, _ = module.prepare_derived(
        plan,
        arms,
        snapshot=tmp_path / "snapshot",
        snapshot_identity={"commit": "4" * 40, "tree": "5" * 40},
    )
    assert observed["source_dir"] == package
    assert observed["replay_schema"] == "opensquilla.draco.frozen-task-analysis/v2"
    assert observed["source_import_evidence"] == source_import
    assert derived["preexisting_source_import_receipt_sha256"] == "e" * 64
    assert derived["screening_design"] == module.screening_design_contract(plan)

    status = module.initialize_status(
        plan,
        arms,
        {"commit": "4" * 40, "tree": "5" * 40},
    )
    assert status["screening_design"] == module.screening_design_contract(plan)
    module.atomic_write_json(run_root / "status.json", status)
    assert module.load_or_initialize_status(
        plan,
        arms,
        {"commit": "4" * 40, "tree": "5" * 40},
    )["screening_design"] == module.screening_design_contract(plan)
    status["screening_design"]["task_interleaving_contract_satisfied"] = True
    module.atomic_write_json(run_root / "status.json", status)
    with pytest.raises(module.ControllerError, match="another campaign"):
        module.load_or_initialize_status(
            plan,
            arms,
            {"commit": "4" * 40, "tree": "5" * 40},
        )


def test_p1_15_progression_skips_only_with_complete_sufficient_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan = _plan()
    control_path = Path("/tmp/control")
    variant_path = Path("/tmp/variant")

    def rows(path: Path, _snapshot: Path) -> list[dict]:
        cost = 10.0 if path == control_path else 8.0
        return [
            {
                "task_id": f"task-{index}",
                "quality_total": 60.0,
                "selected_attempt_billed_cost_usd": cost,
            }
            for index in range(10)
        ]

    monkeypatch.setattr(module, "_result_rows", rows)
    monkeypatch.setattr(
        module, "_progression_cost_runtime", lambda _plan, _snapshot: (None, {})
    )
    receipt = module.p1_15_progression_decision(
        plan,
        snapshot=Path("/tmp/snapshot"),
        control_dir=control_path,
        p1_35_dir=variant_path,
    )
    assert receipt["decision"] == "skip_p1_15_sufficient"
    assert receipt["cost_reduction_fraction"] == pytest.approx(0.2)
    receipt_path = tmp_path / "progression.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    assert module.load_progression_receipt(plan, receipt_path) == receipt
    tampered = dict(receipt)
    tampered["cost_reduction_fraction"] = 0.9
    receipt_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(module.ControllerError, match="identity differs"):
        module.load_progression_receipt(plan, receipt_path)

    def missing_cost(path: Path, _snapshot: Path) -> list[dict]:
        result = rows(path, _snapshot)
        result[0].pop("selected_attempt_billed_cost_usd")
        return result

    monkeypatch.setattr(module, "_result_rows", missing_cost)
    receipt = module.p1_15_progression_decision(
        plan,
        snapshot=Path("/tmp/snapshot"),
        control_dir=control_path,
        p1_35_dir=variant_path,
    )
    assert receipt["decision"] == "run_p1_15_insufficient_or_uncertain"


def test_progression_cost_uses_cache_aware_complete_estimate_fallback() -> None:
    class Reporter:
        @staticmethod
        def selected_generation_cost(_row: dict, _prices: dict) -> dict:
            return {
                "usd": 1.25,
                "complete": True,
                "estimated_cache_aware_requests": 2,
            }

    assert module._selected_cost(
        {"selected_generation_succeeded": True},
        reporter=Reporter(),
        prices={"model": object()},
    ) == pytest.approx(1.25)
    assert module._selected_cost(
        {},
        reporter=type(
            "IncompleteReporter",
            (),
            {
                "selected_generation_cost": staticmethod(
                    lambda _row, _prices: {"usd": 1.25, "complete": False}
                )
            },
        )(),
        prices={},
    ) is None


def test_p1_35_no_hit_keeps_p1_15_eligible_as_uncertain() -> None:
    plan = _plan()
    receipt = module.p1_15_uncertain_progression_decision(
        plan, reason="p1_35_source_slice_no_hit"
    )
    assert receipt["decision"] == "run_p1_15_insufficient_or_uncertain"
    assert receipt["selected_generation_cost_evidence_complete"] is False
    assert receipt["uncertainty_reason"] == "p1_35_source_slice_no_hit"
    unsigned = dict(receipt)
    embedded = unsigned.pop("receipt_sha256")
    assert embedded == module.canonical_sha256(unsigned)


def test_validate_and_expand_commands_are_read_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_plan()), encoding="utf-8")
    assert module.main(["validate-plan", str(plan_path)]) == 0
    assert json.loads(capsys.readouterr().out)["arm_count"] == 43
    assert module.main(["expand-plan", str(plan_path)]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 43
    assert list(tmp_path.iterdir()) == [plan_path]
