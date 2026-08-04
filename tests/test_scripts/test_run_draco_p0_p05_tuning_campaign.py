from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

CONTROLLER_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "experiments"
    / "run_draco_p0_p05_tuning_campaign.py"
)
if not CONTROLLER_PATH.exists():
    CONTROLLER_PATH = Path(__file__).with_name("controller.py")
SPEC = importlib.util.spec_from_file_location("draco_p0_p05_controller", CONTROLLER_PATH)
assert SPEC is not None and SPEC.loader is not None
controller = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = controller
SPEC.loader.exec_module(controller)

PLAN_TEMPLATE = (
    Path(__file__).resolve().parents[2]
    / "configs"
    / "benchmarks"
    / "draco_p0_p05_campaign_plan.template.json"
)
if not PLAN_TEMPLATE.exists():
    PLAN_TEMPLATE = Path(__file__).with_name("campaign-plan.template.json")


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def sealed_result(task_id: str) -> dict[str, object]:
    row: dict[str, object] = {
        "task_id": task_id,
        "group": "G1",
        "result_evidence_schema": "opensquilla.draco.result-evidence/v1",
    }
    row["result_evidence_sha256"] = "sha256:" + controller.canonical_sha256(
        {
            "schema": row["result_evidence_schema"],
            "result": copy.deepcopy(row),
        }
    )
    return row


class ControllerTests(unittest.TestCase):
    def test_matrix_freezes_66_arms_controls_modes_and_schedule(self) -> None:
        plan = controller.load_json(PLAN_TEMPLATE)
        arms = controller.validate_plan(plan, allow_placeholders=True)
        self.assertEqual(len(arms), 66)
        self.assertEqual(
            len({arm.experiment_id for arm in arms if arm.experiment_id != "common-E0"}),
            31,
        )
        self.assertEqual(sum(arm.analyzer_mode == "frozen_replay" for arm in arms), 61)
        self.assertEqual(sum(arm.analyzer_mode == "live" for arm in arms), 5)
        self.assertEqual(
            [arm.arm_id for arm in arms],
            plan["execution"]["schedule"]["arm_order"],
        )
        r1_index = plan["execution"]["schedule"]["arm_order"].index("common-E0-R1")
        self.assertEqual(
            plan["execution"]["schedule"]["arm_order"][r1_index : r1_index + 3],
            ["common-E0-R1", "P0-20-E3", "P0-20-E2"],
        )
        by_id = {arm.arm_id: arm for arm in arms}
        for arm in arms:
            if arm.experiment_id == "common-E0":
                continue
            control = by_id[arm.control_arm_id]
            self.assertEqual(arm.analyzer_mode, control.analyzer_mode)

    def test_plan_rejects_schedule_control_or_mode_drift(self) -> None:
        template = controller.load_json(PLAN_TEMPLATE)
        mutations = []
        wrong_order = copy.deepcopy(template)
        wrong_order["execution"]["schedule"]["arm_order"][6:8] = reversed(
            wrong_order["execution"]["schedule"]["arm_order"][6:8]
        )
        mutations.append(wrong_order)
        wrong_anchor = copy.deepcopy(template)
        wrong_anchor["execution"]["schedule"]["anchor_by_arm_id"]["P0-12-E1"] = "common-E0-R2"
        mutations.append(wrong_anchor)
        wrong_control = copy.deepcopy(template)
        wrong_control["comparison_controls"]["arm_control_overrides"]["P0-12-E1"] = "common-E0-R2"
        mutations.append(wrong_control)
        strict_interleaving = copy.deepcopy(template)
        strict_interleaving["execution"]["schedule"]["strict_task_interleaving"] = True
        mutations.append(strict_interleaving)
        wrong_mode = copy.deepcopy(template)
        wrong_mode["common_e0"][1]["analyzer_mode"] = "live"
        mutations.append(wrong_mode)
        for plan in mutations:
            with self.subTest(plan=plan):
                with self.assertRaises(controller.ControllerError):
                    controller.validate_plan(plan, allow_placeholders=True)

    def test_anchor_gate_blocks_source_and_replay_tranches_after_anchor_failure(self) -> None:
        plan = controller.load_json(PLAN_TEMPLATE)
        arms = controller.validate_plan(plan, allow_placeholders=True)
        by_id = {arm.arm_id: arm for arm in arms}
        status = controller.initialize_status(
            plan,
            arms,
            plan_sha256="plan",
            snapshot_identity={"commit": "commit", "tree": "tree"},
        )
        cases = (
            ("P0-03-E1", "common-E0-source"),
            ("P0-20-E3", "common-E0-R1"),
        )
        for arm_id, anchor_id in cases:
            status["arms"][anchor_id]["state"] = "failed"
            with self.subTest(arm_id=arm_id):
                allowed, failure = controller.schedule_anchor_launch_gate(
                    plan,
                    by_id[arm_id],
                    status=status,
                    authenticated_anchor_ids=set(),
                )
                self.assertFalse(allowed)
                self.assertEqual(failure["reason"], "anchor_not_succeeded")
                self.assertEqual(failure["anchor_arm_id"], anchor_id)
                self.assertEqual(failure["anchor_state"], "failed")

    def test_anchor_gate_allows_restart_anchor_authenticated_earlier_in_schedule(self) -> None:
        plan = controller.load_json(PLAN_TEMPLATE)
        arms = controller.validate_plan(plan, allow_placeholders=True)
        by_id = {arm.arm_id: arm for arm in arms}
        status = controller.initialize_status(
            plan,
            arms,
            plan_sha256="plan",
            snapshot_identity={"commit": "commit", "tree": "tree"},
        )
        status["arms"]["common-E0-R1"]["state"] = "succeeded"
        allowed, evidence = controller.schedule_anchor_launch_gate(
            plan,
            by_id["P0-20-E3"],
            status=status,
            authenticated_anchor_ids={"common-E0-R1"},
        )
        self.assertTrue(allowed)
        self.assertTrue(evidence["anchor_authenticated"])

    def test_anchor_gate_rejects_forged_succeeded_status_without_artifacts(self) -> None:
        plan = controller.load_json(PLAN_TEMPLATE)
        arms = controller.validate_plan(plan, allow_placeholders=True)
        by_id = {arm.arm_id: arm for arm in arms}
        status = controller.initialize_status(
            plan,
            arms,
            plan_sha256="plan",
            snapshot_identity={"commit": "commit", "tree": "tree"},
        )
        status["arms"]["common-E0-source"]["state"] = "succeeded"
        allowed, failure = controller.schedule_anchor_launch_gate(
            plan,
            by_id["P0-03-E1"],
            status=status,
            authenticated_anchor_ids=set(),
        )
        self.assertFalse(allowed)
        self.assertEqual(failure["reason"], "anchor_not_succeeded")
        self.assertEqual(failure["anchor_state"], "succeeded")
        self.assertFalse(failure["anchor_authenticated"])

    def test_replicate_overrides_deep_merge_and_seed_contract(self) -> None:
        plan = controller.load_json(PLAN_TEMPLATE)
        arms = controller.validate_plan(plan, allow_placeholders=True)
        by_id = {arm.arm_id: arm for arm in arms}
        for replicate, seed in enumerate((0, 1, 4), start=1):
            override = by_id[f"P0.5-36-E1-R{replicate}"].override
            self.assertIs(override["ensemble"]["shuffle_candidates"], True)
            self.assertEqual(override["ensemble"]["candidate_order_seed"], seed)

        merge_plan = copy.deepcopy(plan)
        temperature_variant = next(
            experiment for experiment in merge_plan["experiments"] if experiment["id"] == "P0.5-11"
        )["variants"][0]
        temperature_variant["replicate_overrides"] = [
            {"generation": {"max_tokens": value}} for value in (1, 2, 3)
        ]
        merged = {
            arm.arm_id: arm
            for arm in controller.expand_arms(merge_plan)
            if arm.experiment_id == "P0.5-11"
        }
        for replicate in range(1, 4):
            generation = merged[f"P0.5-11-E1-R{replicate}"].override["generation"]
            self.assertEqual(generation["temperature"], 0.2)
            self.assertEqual(generation["max_tokens"], replicate)

    def test_replicate_overrides_and_shuffle_seeds_fail_closed(self) -> None:
        template = controller.load_json(PLAN_TEMPLATE)
        shuffle_variant = next(
            experiment for experiment in template["experiments"] if experiment["id"] == "P0.5-36"
        )["variants"][0]
        invalid: list[dict[str, object]] = []
        for value in (True, -1, 1 << 64):
            plan = copy.deepcopy(template)
            variant = next(
                experiment for experiment in plan["experiments"] if experiment["id"] == "P0.5-36"
            )["variants"][0]
            variant["replicate_overrides"][0]["ensemble"]["candidate_order_seed"] = value
            invalid.append(plan)
        duplicate = copy.deepcopy(template)
        variant = next(
            experiment for experiment in duplicate["experiments"] if experiment["id"] == "P0.5-36"
        )["variants"][0]
        variant["replicate_overrides"][0]["ensemble"]["candidate_order_seed"] = 1
        invalid.append(duplicate)
        shuffle_off = copy.deepcopy(template)
        variant = next(
            experiment for experiment in shuffle_off["experiments"] if experiment["id"] == "P0.5-36"
        )["variants"][0]
        variant["override"]["ensemble"]["shuffle_candidates"] = False
        invalid.append(shuffle_off)
        for plan in invalid:
            with self.subTest(plan=plan):
                with self.assertRaises(controller.ControllerError):
                    controller.validate_plan(plan, allow_placeholders=True)

        for malformed in (
            shuffle_variant["replicate_overrides"][:2],
            [*shuffle_variant["replicate_overrides"], {}],
            "not-a-list",
            [shuffle_variant["replicate_overrides"][0], 1, {}],
        ):
            plan = copy.deepcopy(template)
            variant = next(
                experiment for experiment in plan["experiments"] if experiment["id"] == "P0.5-36"
            )["variants"][0]
            variant["replicate_overrides"] = copy.deepcopy(malformed)
            with self.subTest(malformed=malformed):
                with self.assertRaises(controller.ControllerError):
                    controller.expand_arms(plan)
        single = copy.deepcopy(template)
        variant = next(
            experiment for experiment in single["experiments"] if experiment["id"] == "P0.5-36"
        )["variants"][0]
        variant["replicates"] = 1
        variant["replicate_overrides"] = [variant["replicate_overrides"][0]]
        with self.assertRaises(controller.ControllerError):
            controller.expand_arms(single)

    def test_plan_rejects_judge_or_generation_budget_drift(self) -> None:
        template = controller.load_json(PLAN_TEMPLATE)
        for key, value in (("judge_concurrency", 5), ("generation_max_attempts", 2)):
            with self.subTest(key=key):
                plan = copy.deepcopy(template)
                plan["execution"][key] = value
                with self.assertRaises(controller.ControllerError):
                    controller.validate_plan(plan, allow_placeholders=True)

    def test_authenticated_analyzer_extract_uses_terminal_physical_attempt(self) -> None:
        task_ids = [f"task-{index}" for index in range(10)]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            results_path = root / "results.jsonl"
            trace_path = root / "trace.jsonl"
            result_rows = [sealed_result(task_id) for task_id in task_ids]
            results_path.write_text(
                "".join(json.dumps(row, sort_keys=True) + "\n" for row in result_rows),
                encoding="utf-8",
            )
            trace_rows = []
            for index, (task_id, result) in enumerate(zip(task_ids, result_rows)):
                profile = {"index": index, "constraints": {"risk": "medium"}}
                attempts = [
                    {
                        "attempt": 1,
                        "physical_attempt_id": f"{index + 1:032x}",
                        "requested_provider": "openrouter",
                        "requested_model": "anthropic/claude-opus-4.8",
                        "usage_unknown": True,
                        "output_tokens": 0,
                        "provider_usage": {
                            "usage_unknown": True,
                            "physical_attempt_id": f"{index + 1:032x}",
                        },
                    },
                    {
                        "attempt": 2,
                        "physical_attempt_id": f"{index + 101:032x}",
                        "requested_provider": "openrouter",
                        "requested_model": "anthropic/claude-opus-4.8",
                        "provider": "openrouter",
                        "model": "anthropic/claude-opus-4.8",
                        "output_tokens": 100 + index,
                        "provider_usage": {
                            "completion_tokens": 100 + index,
                            "physical_attempt_id": f"{index + 101:032x}",
                        },
                    },
                ]
                analyzer = {
                    "source": "llm_provider",
                    "schema_valid": True,
                    "confidence": 0.8,
                    "analyzer_version": "opus-4.8-json-v3",
                    "provider": "openrouter",
                    "model": "anthropic/claude-opus-4.8",
                    "fallback_reason": "",
                    "normalization_warnings": [f"warning-{index}"],
                    # Deliberately not the terminal-attempt value.
                    "usage": {
                        "attempt_count": 2,
                        "output_tokens": 9999,
                        "physical_attempts": attempts,
                    },
                }
                selection = {
                    "task_profile_pre_escalation": profile,
                    "task_analyzer": analyzer,
                    "ranking_parameters": {
                        "task_analyzer": {
                            "provider": "openrouter",
                            "model": "anthropic/claude-opus-4.8",
                        }
                    },
                }
                trace_rows.append(
                    {
                        "task_id": task_id,
                        "group": "G1",
                        "error": None,
                        "task_input_sha256": "sha256:" + f"{index + 201:064x}",
                        "prompt_sha256": f"{index + 301:064x}",
                        "result_evidence_sha256": result["result_evidence_sha256"],
                        "routing_trace": {"selection_plan": selection},
                    }
                )
            trace_path.write_text(
                "".join(json.dumps(row, sort_keys=True) + "\n" for row in trace_rows),
                encoding="utf-8",
            )
            audit = {"execution_pass": True, "warnings": ["audit warning retained"]}
            audit["audit_sha256"] = "sha256:" + controller.canonical_sha256(audit)
            proof = {"execution_pass": True, "pass": False, "warnings": ["policy warning"]}
            proof["proof_sha256"] = "sha256:" + controller.canonical_sha256(proof)
            write_json(root / "audit.json", audit)
            write_json(root / "openrouter-non-byok-campaign-proof.json", proof)
            manifest = {
                "status": "complete",
                "audit_sha256": audit["audit_sha256"],
                "openrouter_non_byok_campaign_proof_sha256": proof["proof_sha256"],
                "artifacts": {
                    name: {
                        "path": name,
                        "size_bytes": path.stat().st_size,
                        "sha256": controller.file_sha256(path),
                    }
                    for name, path in (
                        ("results.jsonl", results_path),
                        ("trace.jsonl", trace_path),
                        ("audit.json", root / "audit.json"),
                        (
                            "openrouter-non-byok-campaign-proof.json",
                            root / "openrouter-non-byok-campaign-proof.json",
                        ),
                    )
                },
            }
            manifest["manifest_sha256"] = "sha256:" + controller.canonical_sha256(manifest)
            write_json(root / "manifest.json", manifest)
            source_arm = controller.Arm(
                arm_id="common-E0-source",
                experiment_id="common-E0",
                directory_name="common",
                variant="E0",
                replicate=1,
                analyzer_mode="live",
                override={},
                dynamic=None,
                wire_gate=None,
                output_name="source",
                control_arm_id=None,
            )
            artifact = controller.extract_analyzer_artifact(
                source_arm=source_arm,
                source_dir=root,
                destination=root / "artifact.json",
                expected_task_ids=set(task_ids),
                snapshot_identity={"commit": "c", "tree": "t"},
                plan_sha256="p",
            )
            observed = sorted(
                row["final_successful_physical_attempt_output_tokens"]
                for row in artifact["profiles"].values()
            )
            self.assertEqual(observed, list(range(100, 110)))
            replay = artifact["replay_payload"]["entries"]["task-0"]
            self.assertEqual(replay["task_analyzer"]["normalization_warnings"], ["warning-0"])
            self.assertIn("task_profile_pre_escalation", replay)
            receipt = controller.derive_analyzer_p99_receipt(
                artifact,
                destination=root / "p99.json",
                plan_sha256="p",
            )
            self.assertEqual(receipt["ordered_output_tokens"], list(range(100, 110)))

    def test_result_and_document_hashes_fail_closed(self) -> None:
        row = sealed_result("task")
        controller.verify_result_row_evidence(row)
        row["group"] = "B0"
        with self.assertRaises(controller.ControllerError):
            controller.verify_result_row_evidence(row)
        document = {"status": "complete"}
        document["manifest_sha256"] = "sha256:" + controller.canonical_sha256(document)
        controller.verify_document_self_hash(document, field="manifest_sha256", label="manifest")
        document["status"] = "failed"
        with self.assertRaises(controller.ControllerError):
            controller.verify_document_self_hash(
                document, field="manifest_sha256", label="manifest"
            )

    def test_behavior_compare_is_task_exact(self) -> None:
        baseline = {"a": {"selected_P": ["p"], "selected_A": "a"}}
        same = controller.compare_behavior_projections(baseline, copy.deepcopy(baseline))
        self.assertEqual(same["changed_task_count"], 0)
        changed = controller.compare_behavior_projections(
            baseline, {"a": {"selected_P": ["q"], "selected_A": "a"}}
        )
        self.assertEqual(changed["changed_task_count"], 1)

    def test_candidate_order_seed_is_request_visible_behavior(self) -> None:
        baseline = controller._candidate_order_seed_projection(
            SimpleNamespace(candidate_order_seed=0, shuffle_candidates=True),
            {
                "configured_candidate_order_seed": 0,
                "effective_candidate_order_seed": 0,
            },
            task_id="task",
        )
        candidate = controller._candidate_order_seed_projection(
            SimpleNamespace(candidate_order_seed=1, shuffle_candidates=True),
            {
                "configured_candidate_order_seed": 1,
                "effective_candidate_order_seed": 1,
            },
            task_id="task",
        )
        comparison = controller.compare_behavior_projections(
            {"task": baseline},
            {"task": candidate},
        )
        self.assertEqual(comparison["changed_task_count"], 1)
        with self.assertRaises(controller.ControllerError):
            controller._candidate_order_seed_projection(
                SimpleNamespace(candidate_order_seed=4, shuffle_candidates=True),
                {
                    "configured_candidate_order_seed": 4,
                    "effective_candidate_order_seed": 0,
                },
                task_id="task",
            )

    def test_candidate_order_seed_execution_evidence_gate(self) -> None:
        expected = {
            "required": True,
            "configured_candidate_order_seed": 0,
            "effective_candidate_order_seed": 0,
        }
        aggregate_call = {
            "final_request_role": "aggregator",
            "total_candidates": 3,
            "selected_candidate_count": 3,
            "shuffle_candidates": True,
            "configured_candidate_order_seed": 0,
            "candidate_order_seed": 0,
            "candidate_order_seed_source": "configured",
            "candidate_display_order": [0, 2, 1],
            "selection_plan": {
                "configured_candidate_order_seed": 0,
                "effective_candidate_order_seed": 0,
            },
            "candidates": [
                {"index": index, "selected_for_aggregation": True}
                for index in range(3)
            ],
        }
        rows = [
            {
                "task_id": "task",
                "ensemble_trace": {"mode": "agent_loop", "calls": [aggregate_call]},
            }
        ]
        matched = controller.candidate_order_seed_execution_evidence(
            rows,
            expected=expected,
        )
        self.assertTrue(matched["pass"])
        self.assertEqual(matched["status"], "matched")
        self.assertEqual(matched["aggregation_call_count"], 1)

        wrong_seed = copy.deepcopy(rows)
        wrong_seed[0]["ensemble_trace"]["calls"][0]["candidate_order_seed"] = 1
        mismatched = controller.candidate_order_seed_execution_evidence(
            wrong_seed,
            expected=expected,
        )
        self.assertFalse(mismatched["pass"])
        self.assertIn(
            "candidate_order_seed",
            mismatched["failures"][0]["fields"],
        )

        pre_aggregation = copy.deepcopy(rows)
        pre_aggregation[0]["ensemble_trace"]["calls"] = [
            {
                "final_request_role": "fallback_single",
                "total_candidates": 3,
                "fallback_used": True,
            }
        ]
        not_applicable = controller.candidate_order_seed_execution_evidence(
            pre_aggregation,
            expected=expected,
        )
        self.assertTrue(not_applicable["pass"])
        self.assertEqual(not_applicable["status"], "not_applicable")
        self.assertEqual(not_applicable["aggregation_call_count"], 0)

    def test_selection_projection_rejects_duplicate_or_mismatched_proposers(self) -> None:
        base = {
            "selected_P": ["openrouter:model-a", "openrouter:model-b"],
            "N_min": 2,
            "N_max": 3,
            "proposer_count": 2,
        }
        invalid = []
        duplicate = copy.deepcopy(base)
        duplicate["selected_P"][1] = "OPENROUTER:MODEL-A"
        invalid.append(duplicate)
        mismatched = copy.deepcopy(base)
        mismatched["proposer_count"] = 3
        invalid.append(mismatched)
        outside_range = copy.deepcopy(base)
        outside_range["N_min"] = 3
        outside_range["N_max"] = 4
        invalid.append(outside_range)
        for selection in invalid:
            with self.subTest(selection=selection):
                with self.assertRaises(controller.ControllerError):
                    controller.request_visible_selection_projection(
                        snapshot=Path("/not-used"),
                        config=None,
                        selections={"task": selection},
                        max_tokens_cap_explicit=False,
                    )

    def test_noop_requires_all_ten_byte_identical_and_uncertainty_runs(self) -> None:
        baseline = {f"task-{index}": {"value": index} for index in range(10)}
        same = controller.compare_behavior_projections(
            baseline,
            copy.deepcopy(baseline),
            expected_task_count=10,
        )
        self.assertTrue(same["all_tasks_byte_identical"])
        self.assertEqual(
            controller.offline_effect_decision(
                comparisons={"actual": same},
                arm_ids=["P0.5-10-E1"],
                budget_gated=True,
                production_budget_projection_complete=True,
                projection_uncertain=False,
            ),
            "deleted_no_live_run",
        )
        self.assertEqual(
            controller.offline_effect_decision(
                comparisons={},
                arm_ids=["P0.5-10-E1"],
                budget_gated=True,
                production_budget_projection_complete=False,
                projection_uncertain=True,
            ),
            "run_conservative_projection_uncertain",
        )
        self.assertEqual(
            controller.offline_effect_decision(
                comparisons={"actual": same},
                arm_ids=["common-E0-R1"],
                budget_gated=False,
                production_budget_projection_complete=True,
                projection_uncertain=False,
            ),
            "run_required_replicate",
        )

    def test_plan_freezes_57_unique_replay_overlays_and_registry_identities(self) -> None:
        plan = controller.load_json(PLAN_TEMPLATE)
        arms = controller.validate_plan(plan, allow_placeholders=True)
        replay_overlays = {
            controller.canonical_sha256(arm.override)
            for arm in arms
            if arm.analyzer_mode == "frozen_replay"
        }
        self.assertEqual(len(replay_overlays), 57)
        self.assertEqual(controller.EXPECTED_OFFLINE_UNIQUE_REPLAY_OVERLAYS, 57)
        controller.validate_offline_unique_replay_overlay_count(
            {str(index): {} for index in range(57)}
        )
        with self.assertRaises(controller.ControllerError):
            controller.validate_offline_unique_replay_overlay_count(
                {str(index): {} for index in range(56)}
            )
        registry = plan["freeze"]["model_registry"]
        self.assertEqual(registry["full_model_count"], 79)
        self.assertEqual(registry["formal_model_count"], 79)

    def test_status_freezes_schedule_hash_ordinals_and_anchors(self) -> None:
        plan = controller.load_json(PLAN_TEMPLATE)
        arms = controller.validate_plan(plan, allow_placeholders=True)
        status = controller.initialize_status(
            plan,
            arms,
            plan_sha256=controller.canonical_sha256(plan),
            snapshot_identity={"commit": "c", "tree": "t"},
        )
        schedule = plan["execution"]["schedule"]
        self.assertEqual(status["schedule_sha256"], controller.canonical_sha256(schedule))
        self.assertIs(status["strict_task_interleaving"], False)
        for ordinal, arm in enumerate(arms, start=1):
            state = status["arms"][arm.arm_id]
            self.assertEqual(state["schedule_ordinal"], ordinal)
            self.assertEqual(
                state["anchor_arm_id"],
                schedule["anchor_by_arm_id"][arm.arm_id],
            )

    def test_main_dry_replay_blanks_network_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            snapshot = root / "snapshot"
            (snapshot / "scripts").mkdir(parents=True)
            runner = snapshot / "scripts/run_draco_routing_experiment.py"
            runner.write_text("# offline fixture\n", encoding="utf-8")
            reference = root / "reference"
            (reference / "data/draco").mkdir(parents=True)
            (reference / ".local-state").mkdir(parents=True)
            plan = {
                "paths": {
                    "run_root": str(root / "run"),
                    "reference_repo": str(reference),
                    "python": "python3",
                    "experiment_config_relative": "config.json",
                }
            }
            captured: dict[str, object] = {}

            def fake_run(command: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
                captured["command"] = command
                captured["env"] = kwargs["env"]
                return subprocess.CompletedProcess(command, 0, "", "")

            fake_trace = root / "trace.jsonl"
            fake_manifest = root / "manifest.json"
            fake_trace.write_text("{}\n", encoding="utf-8")
            fake_manifest.write_text("{}\n", encoding="utf-8")
            plans = {f"task-{index}": {} for index in range(10)}
            with (
                mock.patch.object(controller, "validate_runtime_freeze"),
                mock.patch.object(controller.subprocess, "run", side_effect=fake_run),
                mock.patch.object(
                    controller,
                    "_dry_run_output_artifacts",
                    return_value=(fake_trace, fake_manifest),
                ),
                mock.patch.object(
                    controller,
                    "_source_selection_plans",
                    return_value=plans,
                ),
            ):
                observed, evidence = controller.run_main_dry_replay(
                    plan,
                    snapshot=snapshot,
                    snapshot_identity={"commit": "c", "tree": "t", "status": ""},
                    overlay={},
                    expected_task_ids=set(plans),
                    label="fixture",
                )
            self.assertEqual(set(observed), set(plans))
            command = captured["command"]
            self.assertIn("--dry-run", command)
            env = captured["env"]
            self.assertEqual(env["BRAVE_SEARCH_API_KEY"], "")
            self.assertEqual(env["FIRECRAWL_API_KEY"], "")
            self.assertTrue(env["OPENROUTER_API_KEY"].startswith("sk-or-v1-"))
            self.assertIn("zero Analyzer/provider/Judge calls", evidence["network_contract"])

    def test_publication_identity_accepts_bound_main_then_resume_wave(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            directory = root / "arm"
            snapshot = root / "snapshot"
            main_runner = snapshot / "scripts/run_draco_routing_experiment.py"
            resume_runner = snapshot / "scripts/run_draco_routing_experiment_resume.py"
            benchmark = root / "reference/data/draco/mini.jsonl"
            reference_config = root / "reference/.local-state/config.toml"
            effective = {
                "groups": {"G1": {"enabled": True}},
                "judge": {"concurrency": 6},
                "generation": {"max_attempts": 3},
            }
            task_ids = [f"task-{index}" for index in range(10)]
            expected = {
                "arm_id": "P0.5-11-E1-R1",
                "output_name": "fixture",
                "run_id": "run",
                "output_dir": str(directory),
                "snapshot": str(snapshot),
                "snapshot_commit": "commit",
                "runner_identities": {
                    str(main_runner.resolve()): "main-sha",
                    str(resume_runner.resolve()): "resume-sha",
                },
                "benchmark_path": str(benchmark),
                "reference_config_path": str(reference_config),
                "benchmark_sha256": "benchmark-sha",
                "task_ids": task_ids,
                "task_concurrency": 6,
                "judge_concurrency": 6,
                "generation_max_attempts": 3,
                "override_sha256": "override-sha",
                "effective_config_sha256": controller.canonical_sha256(effective),
            }
            bindings: list[dict[str, object]] = []
            for index, runner in enumerate((main_runner, resume_runner)):
                wave = directory / f"wave-{index + 1}"
                wave.mkdir(parents=True)
                effective_path = wave / "experiment-config.effective.json"
                result_path = wave / "results.jsonl"
                source_path = wave / "manifest.json"
                write_json(effective_path, effective)
                result_path.write_text("{}\n", encoding="utf-8")
                source = {
                    "args": {
                        "input": str(benchmark),
                        "config": str(reference_config),
                        "groups": "G1",
                        "max_tasks": 10,
                        "concurrency": 6,
                        "judge_concurrency": 6,
                        "generation_max_attempts": 3,
                        "dry_run": False,
                        "require_openrouter_non_byok": True,
                        "require_clean_source": True,
                        "output_dir": str(wave),
                    },
                    "command": {"cwd": str(snapshot)},
                    "source_provenance": {
                        "git_head": "commit",
                        "git_dirty": False,
                        "git_tracked_dirty": False,
                        "runner_path": str(runner),
                        "runner_sha256": ("main-sha" if index == 0 else "resume-sha"),
                    },
                    "benchmark_input_validation": {
                        "actual_sha256": "benchmark-sha",
                        "actual_task_count": 10,
                        "task_ids_match": True,
                        "status": "matched",
                    },
                    "artifacts": {"experiment_config_effective_json": str(effective_path)},
                }
                write_json(source_path, source)
                binding: dict[str, object] = {
                    "path": str(source_path),
                    "sha256": controller.file_sha256(source_path),
                    "result_path": str(result_path),
                    "result_sha256": controller.file_sha256(result_path),
                    "resume_schedule_contract_verified": index == 1,
                    "resume_scheduled_pairs": [],
                }
                if index == 1:
                    binding["resume_scheduled_pairs"] = [
                        {
                            "group": "G1",
                            "task_id": task_ids[0],
                            "action": "judge_only",
                        }
                    ]
                bindings.append(binding)
            evidence = controller.verify_arm_publication_identity(
                directory,
                {"source_manifests": bindings},
                expected=expected,
            )
            self.assertEqual(
                [row["runner_kind"] for row in evidence["source_manifests"]],
                ["main", "resume"],
            )
            source = controller.load_json(Path(str(bindings[1]["path"])))
            source["args"]["generation_max_attempts"] = 2
            write_json(Path(str(bindings[1]["path"])), source)
            bindings[1]["sha256"] = controller.file_sha256(Path(str(bindings[1]["path"])))
            with self.assertRaises(controller.ControllerError):
                controller.verify_arm_publication_identity(
                    directory,
                    {"source_manifests": bindings},
                    expected=expected,
                )
            source["args"]["generation_max_attempts"] = 3
            write_json(Path(str(bindings[1]["path"])), source)
            bindings[1]["sha256"] = controller.file_sha256(Path(str(bindings[1]["path"])))
            bindings[1]["resume_scheduled_pairs"] = [
                {"group": "G2", "task_id": task_ids[0], "action": "judge_only"}
            ]
            with self.assertRaises(controller.ControllerError):
                controller.verify_arm_publication_identity(
                    directory,
                    {"source_manifests": bindings},
                    expected=expected,
                )

    def test_terminal_status_is_immutable_and_report_failure_downgrades(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            status = {
                "schema": controller.STATUS_SCHEMA,
                "run_id": "run",
                "campaign_plan_sha256": "plan-sha",
                "phase": "succeeded",
                "arms": {
                    "arm": {"state": "succeeded"},
                    "deleted": {"state": "no_op_deleted"},
                },
                "no_op_experiments": {"P0.5-07": {"state": "no_op_deleted"}},
                "reporting": {"mutable": True},
                "terminal_status_input": {"mutable": True},
            }
            self.assertEqual(controller.campaign_terminal_phase(status), "succeeded")
            self.assertEqual(
                controller.campaign_terminal_phase(
                    status,
                    reporting_complete=False,
                ),
                "completed_with_failures",
            )
            descriptor = controller.publish_terminal_status_input(
                {"paths": {"run_root": str(root)}},
                status,
            )
            frozen = controller.load_json(Path(descriptor["path"]))
            controller.verify_bare_document_self_hash(
                frozen,
                field="terminal_status_input_sha256",
                label="terminal status input",
            )
            self.assertNotIn("reporting", frozen)
            self.assertNotIn("terminal_status_input", frozen)
            self.assertEqual(
                descriptor["semantic_sha256"],
                frozen["terminal_status_input_sha256"],
            )
            self.assertEqual(
                descriptor["file_sha256"],
                controller.file_sha256(Path(descriptor["path"])),
            )


if __name__ == "__main__":
    unittest.main()
