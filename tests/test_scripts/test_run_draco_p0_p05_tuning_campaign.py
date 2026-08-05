from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from opensquilla.provider.ranking_router import (
    build_request_context,
    fallback_task_profile,
    ranking_config_resolution,
)

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
    def _confirmatory_fixture(
        self,
        *,
        run_root: Path | None = None,
    ) -> tuple[dict[str, object], object, object, dict[str, object]]:
        plan = controller.load_json(PLAN_TEMPLATE)
        if run_root is not None:
            plan["paths"]["run_root"] = str(run_root)
        arms = controller.validate_plan(plan, allow_placeholders=True)
        by_id = {arm.arm_id: arm for arm in arms}
        control = by_id["common-E0-R1"]
        candidate = by_id["P0-12-E1"]
        task_ids = plan["benchmark"]["task_ids"]
        replay = {
            "schema": "opensquilla.draco.frozen-task-analysis/v2",
            "mode": "frozen_replay",
            "entries": {task_id: {"task_id": task_id} for task_id in task_ids},
        }
        artifact = {
            "artifact_sha256": "a" * 64,
            "replay_payload": replay,
        }
        replay_overlay = {"g1_routing": {"task_analysis_execution": replay}}
        control_override = controller.deep_merge(control.override, replay_overlay)
        candidate_override = controller.deep_merge(candidate.override, replay_overlay)
        schedule = controller.build_confirmatory_schedule_payload(
            plan,
            control_arm=control,
            candidate_arm=candidate,
            control_override=control_override,
            candidate_override=candidate_override,
            analyzer_artifact=artifact,
            seed="fixture-seed",
        )
        return plan, control, candidate, schedule

    def _write_confirmatory_publication(
        self,
        root: Path,
        schedule: dict[str, object],
    ) -> None:
        (root / "archive").mkdir(parents=True)
        controller.atomic_write_json(
            root / "archive" / "confirmatory-schedule.json",
            schedule,
        )
        role_manifest_hashes = {}
        cohort_sha = "sha256:" + "c" * 64
        for role in ("control", "candidate"):
            role_root = root / role
            role_root.mkdir(parents=True)
            for name in (
                "results.jsonl",
                "trace.jsonl",
                "audit.json",
                "openrouter-non-byok-campaign-proof.json",
            ):
                (role_root / name).write_text("{}\n", encoding="utf-8")
            companion = "candidate" if role == "control" else "control"
            manifest = {
                "status": "complete",
                "execution_pass": True,
                "groups": ["G1"],
                "task_count": 10,
                "result_count": 10,
                "task_ids": schedule["benchmark"]["task_ids"],
                "account_window_cohort": {
                    "cohort_id": schedule["cohort_id"],
                    "role": role,
                    "companion_role": companion,
                    "cohort_sha256": cohort_sha,
                },
            }
            manifest["manifest_sha256"] = "sha256:" + controller.canonical_sha256(
                manifest
            )
            controller.atomic_write_json(role_root / "manifest.json", manifest)
            role_manifest_hashes[role] = controller.file_sha256(
                role_root / "manifest.json"
            )
        pair_manifest = {
            "schema": controller.CONFIRMATORY_COHORT_MANIFEST_SCHEMA,
            "status": "complete",
            "cohort_id": schedule["cohort_id"],
            "schedule_sha256": schedule["schedule_sha256"],
            "roles": {
                role: {
                    "path": f"{role}/manifest.json",
                    "sha256": role_manifest_hashes[role],
                }
                for role in ("control", "candidate")
            },
            "account_delta_report_scope": "paired_cohort_once",
            "screening_is_diagnostic_only": True,
        }
        pair_manifest["manifest_sha256"] = controller.canonical_sha256(pair_manifest)
        controller.atomic_write_json(root / "cohort-manifest.json", pair_manifest)

    def test_confirmatory_schedule_is_deterministic_balanced_and_globally_bounded(self) -> None:
        plan, _, _, schedule = self._confirmatory_fixture()
        controller.validate_confirmatory_schedule(plan, schedule)
        self.assertEqual(schedule["order_balance"], {"AB": 5, "BA": 5})
        self.assertEqual(
            [len(tranche["task_ids"]) for tranche in schedule["tranches"]],
            [6, 4],
        )
        self.assertTrue(
            all(
                phase["max_inflight_tasks"] <= 6
                for tranche in schedule["tranches"]
                for phase in tranche["phases"]
            )
        )
        self.assertEqual(schedule["frozen_analyzer"]["entry_count"], 10)
        self.assertEqual(
            schedule["roles"]["control"]["override"]["g1_routing"][
                "task_analysis_execution"
            ],
            schedule["roles"]["candidate"]["override"]["g1_routing"][
                "task_analysis_execution"
            ],
        )
        _, _, _, repeated = self._confirmatory_fixture()
        self.assertEqual(schedule, repeated)

    def test_confirmatory_schedule_rejects_unbalanced_or_unpaired_repair_drift(self) -> None:
        plan, _, _, schedule = self._confirmatory_fixture()
        mutations = []
        unbalanced = copy.deepcopy(schedule)
        unbalanced["task_schedule"][0]["order"] = "BA"
        unbalanced["order_balance"] = {"AB": 4, "BA": 6}
        mutations.append(unbalanced)
        concurrency = copy.deepcopy(schedule)
        concurrency["tranches"][0]["phases"][0]["max_inflight_tasks"] = 7
        mutations.append(concurrency)
        retry = copy.deepcopy(schedule)
        retry["execution_contract"]["generation_leg_failure_policy"] = "retry_unpaired"
        mutations.append(retry)
        replay = copy.deepcopy(schedule)
        replay["roles"]["candidate"]["override"]["g1_routing"][
            "task_analysis_execution"
        ] = {"mode": "frozen_replay", "entries": {}}
        mutations.append(replay)
        role_binding = copy.deepcopy(schedule)
        role_binding["roles"]["candidate"]["control_arm_id"] = "common-E0-R2"
        mutations.append(role_binding)
        deterministic_assignment = copy.deepcopy(schedule)
        current_first_role = deterministic_assignment["task_schedule"][0]["first_role"]
        deterministic_assignment["task_schedule"][0]["first_role"] = (
            "candidate" if current_first_role == "control" else "control"
        )
        mutations.append(deterministic_assignment)
        account_window = copy.deepcopy(schedule)
        account_window["account_window_contract"]["single_account_before_after"] = False
        mutations.append(account_window)
        for mutation in mutations:
            mutation.pop("schedule_sha256")
            mutation["schedule_sha256"] = controller.canonical_sha256(mutation)
            with self.subTest(mutation=mutation):
                with self.assertRaises(controller.ControllerError):
                    controller.validate_confirmatory_schedule(plan, mutation)

    def test_live_analyzer_confirmatory_schedule_does_not_embed_replay(self) -> None:
        plan = controller.load_json(PLAN_TEMPLATE)
        by_id = {
            arm.arm_id: arm
            for arm in controller.validate_plan(plan, allow_placeholders=True)
        }
        candidate = by_id["P0-03-E1"]
        control = by_id[candidate.control_arm_id]
        schedule = controller.build_confirmatory_schedule_payload(
            plan,
            control_arm=control,
            candidate_arm=candidate,
            control_override=control.override,
            candidate_override=candidate.override,
            analyzer_artifact=None,
            seed="live-analyzer-seed",
        )
        controller.validate_confirmatory_schedule(plan, schedule)
        self.assertIsNone(schedule["frozen_analyzer"])

    def test_confirmatory_publication_registration_is_receipt_driven_and_idempotent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temporary = Path(raw)
            plan, _, _, schedule = self._confirmatory_fixture(
                run_root=temporary / "run",
            )
            cohort_root = temporary / "cohort"
            self._write_confirmatory_publication(cohort_root, schedule)
            first = controller.register_confirmatory_report_input(
                plan,
                schedule,
                cohort_root,
            )
            index_path = Path(first["index_path"])
            first_raw = index_path.read_bytes()
            second = controller.register_confirmatory_report_input(
                plan,
                schedule,
                cohort_root,
            )
            self.assertEqual(first, second)
            self.assertEqual(first_raw, index_path.read_bytes())
            index = controller.load_json(index_path)
            controller._validate_confirmatory_report_input_index(plan, index)
            self.assertEqual(len(index["entries"]), 1)
            self.assertEqual(
                index["entries"][0]["account_window_cohort_sha256"],
                "sha256:" + "c" * 64,
            )

    def test_confirmatory_publication_missing_one_role_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temporary = Path(raw)
            plan, _, _, schedule = self._confirmatory_fixture(
                run_root=temporary / "run",
            )
            cohort_root = temporary / "cohort"
            self._write_confirmatory_publication(cohort_root, schedule)
            (cohort_root / "candidate" / "manifest.json").unlink()
            with self.assertRaises(controller.ControllerError):
                controller.register_confirmatory_report_input(
                    plan,
                    schedule,
                    cohort_root,
                )
            partial = controller.register_partial_confirmatory_report_input(
                plan,
                schedule,
                cohort_root,
                reason="candidate_missing",
                launcher_returncode=2,
            )
            self.assertEqual(partial["status"], "partial")
            self.assertEqual(
                partial["roles"]["candidate"]["publication_status"],
                "missing",
            )
            self.assertEqual(
                partial["roles"]["control"]["publication_status"],
                "present_unverified",
            )
            self.assertEqual(
                controller.registered_confirmatory_report_input(
                    plan,
                    schedule["cohort_id"],
                ),
                {
                    key: value
                    for key, value in partial.items()
                    if key not in {"index_path", "index_sha256"}
                },
            )

    def test_isolated_snapshot_helper_does_not_write_bytecode(self) -> None:
        program = """
import json
import sys
from pathlib import Path

snapshot = Path(sys.argv[1])
sys.path.insert(0, str(snapshot))
import snapshot_probe

print(json.dumps({"value": snapshot_probe.VALUE}))
"""
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Path(tmp)
            (snapshot / "snapshot_probe.py").write_text(
                "VALUE = 'snapshot-imported'\n",
                encoding="utf-8",
            )

            result = controller._isolated_snapshot_json(
                snapshot,
                program=program,
                payload={},
                label="bytecode isolation probe",
            )

            self.assertEqual(result, {"value": "snapshot-imported"})
            self.assertEqual(list(snapshot.rglob("__pycache__")), [])
            self.assertEqual(list(snapshot.rglob("*.pyc")), [])

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

    def test_replay_overlay_and_runtime_support_bind_declared_schema(self) -> None:
        plan = controller.load_json(PLAN_TEMPLATE)
        artifact = {
            "replay_payload": {
                "schema": controller.FROZEN_TASK_ANALYSIS_SCHEMA_V2,
                "entries": {},
            }
        }
        with self.assertRaises(controller.ControllerError):
            controller.make_replay_overlay(plan, artifact)
        artifact["replay_payload"]["schema"] = controller.FROZEN_TASK_ANALYSIS_SCHEMA_V1
        overlay = controller.make_replay_overlay(plan, artifact)
        self.assertEqual(
            overlay["g1_routing"]["task_analysis_execution"]["schema"],
            controller.FROZEN_TASK_ANALYSIS_SCHEMA_V1,
        )
        controller.validate_frozen_replay_runtime_support(
            plan,
            {"frozen_task_analysis_schemas": {controller.FROZEN_TASK_ANALYSIS_SCHEMA_V1}},
        )
        with self.assertRaises(controller.ControllerError):
            controller.validate_frozen_replay_runtime_support(
                plan,
                {"frozen_task_analysis_schemas": {controller.FROZEN_TASK_ANALYSIS_SCHEMA_V2}},
            )

    def test_analyzer_fallback_and_preexisting_source_require_explicit_contracts(self) -> None:
        template = controller.load_json(PLAN_TEMPLATE)
        self.assertFalse(
            controller.analyzer_source_policy(template)["allow_deterministic_router_fallback"]
        )
        self.assertIsNone(controller.preexisting_source_contract(template))

        opted_in = copy.deepcopy(template)
        opted_in["runtime_contract"]["analyzer_source"] = {
            "schema": controller.ANALYZER_SOURCE_POLICY_SCHEMA,
            "allow_deterministic_router_fallback": True,
        }
        with self.assertRaises(controller.ControllerError):
            controller.validate_plan(opted_in, allow_placeholders=True)
        opted_in["runtime_contract"]["frozen_replay"]["schema"] = (
            controller.FROZEN_TASK_ANALYSIS_SCHEMA_V2
        )
        opted_in["runtime_contract"]["preexisting_source"] = {
            "schema": controller.PREEXISTING_SOURCE_SCHEMA,
            "enabled": True,
            "source_plan_path": "TODO_SOURCE_PLAN_PATH",
            "source_plan_raw_sha256": "TODO_SOURCE_PLAN_RAW_SHA256",
            "source_plan_canonical_sha256": "TODO_SOURCE_PLAN_CANONICAL_SHA256",
            "source_snapshot_path": "TODO_SOURCE_SNAPSHOT_PATH",
            "source_snapshot_commit": "TODO_SOURCE_SNAPSHOT_COMMIT",
            "source_snapshot_tree": "TODO_SOURCE_SNAPSHOT_TREE",
            "source_output_dir": "TODO_SOURCE_OUTPUT_DIR",
            "source_manifest_sha256": "TODO_SOURCE_MANIFEST_SHA256",
            "source_results_sha256": "TODO_SOURCE_RESULTS_SHA256",
            "source_trace_sha256": "TODO_SOURCE_TRACE_SHA256",
        }
        controller.validate_plan(opted_in, allow_placeholders=True)
        self.assertTrue(
            controller.analyzer_source_policy(opted_in)["allow_deterministic_router_fallback"]
        )

        malformed = copy.deepcopy(opted_in)
        malformed["runtime_contract"]["preexisting_source"].pop("source_trace_sha256")
        with self.assertRaises(controller.ControllerError):
            controller.validate_plan(malformed, allow_placeholders=True)

    def test_preexisting_source_publication_receipt_is_fail_closed(self) -> None:
        plan = {
            "benchmark": {"task_ids": [f"task-{index}" for index in range(10)]},
            "execution": {"task_concurrency": 6},
        }
        expected = {"output_dir": "/bound/source"}
        base_receipt = {"source_output_dir": "/bound/source"}
        with (
            mock.patch.object(
                controller,
                "preexisting_source_identity",
                return_value=(expected, copy.deepcopy(base_receipt)),
            ),
            mock.patch.object(
                controller,
                "inspect_complete_arm",
                return_value=(True, {"status": "complete"}),
            ),
        ):
            receipt = controller.authenticate_preexisting_source(plan)
        self.assertEqual(receipt["source_output_dir"], "/bound/source")
        self.assertIn("publication_evidence_sha256", receipt)
        self.assertIn("receipt_sha256", receipt)

        with (
            mock.patch.object(
                controller,
                "preexisting_source_identity",
                return_value=(expected, copy.deepcopy(base_receipt)),
            ),
            mock.patch.object(
                controller,
                "inspect_complete_arm",
                return_value=(False, {"reason": "hash_mismatch"}),
            ),
            self.assertRaises(controller.ControllerError),
        ):
            controller.authenticate_preexisting_source(plan)

    def test_preexisting_source_is_frozen_once_and_reused_without_source_reads(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "old-reports" / "common" / "source"
            source.mkdir(parents=True)
            snapshot = root / "old-snapshot"
            module_dir = snapshot / "src" / "opensquilla" / "eval"
            module_dir.mkdir(parents=True)
            (snapshot / "src" / "opensquilla" / "__init__.py").write_text(
                "", encoding="utf-8"
            )
            (module_dir / "__init__.py").write_text("", encoding="utf-8")
            config_module_path = module_dir / "draco_experiment_config.py"
            config_module_source = (
                "class Config:\n"
                "    def model_dump(self, mode=None):\n"
                "        return {\n"
                "            'runner': {'concurrency': 2, 'timeout_seconds': 111.0},\n"
                "            'judge': {'concurrency': 6, 'model': 'judge-model'},\n"
                "            'generation': {'max_attempts': 3, 'retry_backoff_s': 2.0},\n"
                "            'ensemble': {'candidate_order_seed': None, "
                "'shuffle_candidates': False},\n"
                "            'audit_marker': 'source',\n"
                "        }\n"
                "class Loaded:\n"
                "    config = Config()\n"
                "def load_draco_experiment_config(*args, **kwargs):\n"
                "    return Loaded()\n"
            )
            config_module_path.write_text(
                config_module_source,
                encoding="utf-8",
            )
            scripts = snapshot / "scripts"
            scripts.mkdir()
            for runner_name in (
                "run_draco_routing_experiment.py",
                "run_draco_routing_experiment_resume.py",
            ):
                (scripts / runner_name).write_text("# frozen runner\n", encoding="utf-8")
            source_arm = controller.Arm(
                arm_id=controller.ANALYZER_SOURCE_ARM_ID,
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
            source_plan_payload = {
                "run_id": "old-run",
                "paths": {
                    "experiment_config_relative": "configs/draco.json",
                    "reference_repo": str(root / "reference"),
                    "report_root": str(root / "old-reports"),
                },
                "benchmark": {
                    "input_sha256": "c" * 64,
                    "task_ids": [f"task-{index}" for index in range(10)],
                },
                "execution": {
                    "task_concurrency": 6,
                    "judge_concurrency": 6,
                    "generation_max_attempts": 3,
                },
            }
            source_plan = root / "old-plan.json"
            source_plan.write_text(
                json.dumps(source_plan_payload, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            for name in (
                "manifest.json",
                "results.jsonl",
                "trace.jsonl",
                "audit.json",
                "openrouter-non-byok-campaign-proof.json",
            ):
                (source / name).write_text(name + "\n", encoding="utf-8")
            contract = {
                "schema": controller.PREEXISTING_SOURCE_SCHEMA,
                "enabled": True,
                "source_plan_path": str(source_plan),
                "source_plan_raw_sha256": controller.file_sha256(source_plan),
                "source_plan_canonical_sha256": controller.canonical_sha256(
                    source_plan_payload
                ),
                "source_snapshot_path": str(snapshot),
                "source_snapshot_commit": "a" * 40,
                "source_snapshot_tree": "b" * 40,
                "source_output_dir": str(source),
                "source_manifest_sha256": controller.file_sha256(source / "manifest.json"),
                "source_results_sha256": controller.file_sha256(source / "results.jsonl"),
                "source_trace_sha256": controller.file_sha256(source / "trace.jsonl"),
            }
            plan = {
                "runtime_contract": {"preexisting_source": contract},
                "paths": {
                    "run_root": str(root / "run"),
                    "report_root": str(root / "new-reports"),
                },
            }
            (root / "run").mkdir()
            git_state = {"commit": "a" * 40, "tree": "b" * 40, "status": ""}
            expected_identity = controller.arm_completion_identity(
                source_plan_payload,
                source_arm,
                snapshot=snapshot,
                snapshot_identity=git_state,
                override={},
                isolated_config=True,
            )
            expected_config_payload = {
                "runner": {"concurrency": 6, "timeout_seconds": 111.0},
                "judge": {"concurrency": 6, "model": "judge-model"},
                "generation": {"max_attempts": 3, "retry_backoff_s": 2.0},
                "ensemble": {
                    "candidate_order_seed": None,
                    "shuffle_candidates": False,
                },
                "audit_marker": "source",
            }
            self.assertEqual(
                expected_identity["effective_config_sha256"],
                controller.canonical_sha256(expected_config_payload),
            )
            for section_name, field_name, invalid_value in (
                ("judge", "concurrency", 5),
                ("generation", "max_attempts", 2),
            ):
                with self.subTest(runtime_field=f"{section_name}.{field_name}"):
                    invalid_config = copy.deepcopy(expected_config_payload)
                    invalid_config[section_name][field_name] = invalid_value
                    with self.assertRaises(controller.ControllerError):
                        controller.launcher_effective_config_projection(
                            source_plan_payload,
                            invalid_config,
                        )
            config_module_path.write_text(
                config_module_source.replace(
                    "'audit_marker': 'source'",
                    "'audit_marker': 'drift'",
                ),
                encoding="utf-8",
            )
            drifted_identity = controller.arm_completion_identity(
                source_plan_payload,
                source_arm,
                snapshot=snapshot,
                snapshot_identity=git_state,
                override={},
                isolated_config=True,
            )
            self.assertNotEqual(
                drifted_identity["effective_config_sha256"],
                expected_identity["effective_config_sha256"],
            )
            config_module_path.write_text(config_module_source, encoding="utf-8")
            publication_evidence = {"reason": "complete"}
            authenticated = {
                **contract,
                "expected_identity_sha256": controller.canonical_sha256(expected_identity),
                "expected_publication_identity": expected_identity,
                "publication_evidence": publication_evidence,
                "publication_evidence_sha256": controller.canonical_sha256(publication_evidence),
                "contract_sha256": controller.canonical_sha256(contract),
            }
            authenticated["receipt_sha256"] = controller.canonical_sha256(authenticated)
            archive_bytes = io.BytesIO()
            with tarfile.open(fileobj=archive_bytes, mode="w") as archive:
                for path in sorted(snapshot.rglob("*")):
                    if not path.is_file():
                        continue
                    payload = path.read_bytes()
                    member = tarfile.TarInfo(path.relative_to(snapshot).as_posix())
                    member.size = len(payload)
                    member.mode = 0o644
                    archive.addfile(member, io.BytesIO(payload))
            completed = SimpleNamespace(returncode=0, stdout=archive_bytes.getvalue())
            real_subprocess_run = subprocess.run

            def run_with_frozen_archive(command, *args, **kwargs):
                if list(command)[:2] == ["git", "archive"]:
                    return completed
                return real_subprocess_run(command, *args, **kwargs)

            with (
                mock.patch.object(
                    controller,
                    "authenticate_preexisting_source",
                    return_value=copy.deepcopy(authenticated),
                ) as authenticate,
                mock.patch.object(
                    controller,
                    "authenticate_published_arm_artifacts",
                    return_value=({}, {}, {}, {}),
                ),
                mock.patch.object(controller, "git_identity", return_value=git_state),
                mock.patch.object(
                    controller.subprocess,
                    "run",
                    side_effect=run_with_frozen_archive,
                ),
                mock.patch.object(controller, "validate_plan", return_value=[source_arm]),
            ):
                first = controller.materialize_preexisting_source(plan)
                (source / "results.jsonl").write_text("changed\n", encoding="utf-8")
                second = controller.materialize_preexisting_source(plan)
            self.assertEqual(first, second)
            self.assertEqual(authenticate.call_count, 1)
            package = Path(first["package_dir"])
            self.assertNotEqual(
                controller.file_sha256(source / "results.jsonl"),
                controller.file_sha256(package / "results.jsonl"),
            )

    def test_preexisting_source_path_rejects_symlink_before_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "target.json"
            target.write_text("{}\n", encoding="utf-8")
            alias = root / "alias.json"
            alias.symlink_to(target)
            with self.assertRaises(controller.ControllerError):
                controller.absolute_path_without_symlinks(
                    alias,
                    label="source plan",
                )

    def test_effective_config_import_isolated_between_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            observed = []
            for marker in ("old", "new"):
                snapshot = root / marker
                module_dir = snapshot / "src" / "opensquilla" / "eval"
                module_dir.mkdir(parents=True)
                (snapshot / "src" / "opensquilla" / "__init__.py").write_text("", encoding="utf-8")
                (module_dir / "__init__.py").write_text("", encoding="utf-8")
                (module_dir / "draco_experiment_config.py").write_text(
                    "class _Config:\n"
                    "    def model_dump(self, mode=None):\n"
                    f"        return {{'marker': '{marker}', 'ensemble': {{}}}}\n"
                    "class _Loaded:\n"
                    "    config = _Config()\n"
                    "def load_draco_experiment_config(*args, **kwargs):\n"
                    "    return _Loaded()\n",
                    encoding="utf-8",
                )
                base_config = snapshot / "config.json"
                base_config.write_text("{}\n", encoding="utf-8")
                observed.append(
                    controller.load_effective_experiment_config_isolated(
                        snapshot,
                        base_config,
                        {},
                    )["marker"]
                )
            self.assertEqual(observed, ["old", "new"])

    def test_preexisting_source_identity_binds_plan_snapshot_and_three_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source_dir = root / "reports" / "common" / "source-old"
            source_dir.mkdir(parents=True)
            for filename, content in (
                ("manifest.json", "{}\n"),
                ("results.jsonl", "{}\n"),
                ("trace.jsonl", "{}\n"),
            ):
                (source_dir / filename).write_text(content, encoding="utf-8")
            source_plan_path = root / "old-plan.json"
            (root / "snapshot").mkdir()
            benchmark = {
                "task_count": 10,
                "task_ids": [f"task-{index}" for index in range(10)],
                "groups": ["G1"],
            }
            source_plan = {
                "schema": controller.PLAN_SCHEMA,
                "run_id": "old",
                "benchmark": benchmark,
                "runtime_contract": {},
                "paths": {"report_root": str(root / "reports")},
                "freeze": {"snapshot_commit": "a" * 40, "snapshot_tree": "b" * 40},
            }
            write_json(source_plan_path, source_plan)
            source_arm = controller.Arm(
                arm_id=controller.ANALYZER_SOURCE_ARM_ID,
                experiment_id="common-E0",
                directory_name="common",
                variant="E0",
                replicate=1,
                analyzer_mode="live",
                override={},
                dynamic=None,
                wire_gate=None,
                output_name="source-old",
                control_arm_id=None,
            )
            contract = {
                "schema": controller.PREEXISTING_SOURCE_SCHEMA,
                "enabled": True,
                "source_plan_path": str(source_plan_path),
                "source_plan_raw_sha256": controller.file_sha256(source_plan_path),
                "source_plan_canonical_sha256": controller.canonical_sha256(source_plan),
                "source_snapshot_path": str(root / "snapshot"),
                "source_snapshot_commit": "a" * 40,
                "source_snapshot_tree": "b" * 40,
                "source_output_dir": str(source_dir),
                "source_manifest_sha256": controller.file_sha256(source_dir / "manifest.json"),
                "source_results_sha256": controller.file_sha256(source_dir / "results.jsonl"),
                "source_trace_sha256": controller.file_sha256(source_dir / "trace.jsonl"),
            }
            plan = {
                "benchmark": copy.deepcopy(benchmark),
                "runtime_contract": {"preexisting_source": contract},
            }
            expected = {"output_dir": str(source_dir), "identity": "bound"}
            with (
                mock.patch.object(controller, "validate_plan", return_value=[source_arm]),
                mock.patch.object(
                    controller,
                    "git_identity",
                    return_value={
                        "commit": "a" * 40,
                        "tree": "b" * 40,
                        "status": "",
                    },
                ),
                mock.patch.object(
                    controller,
                    "arm_completion_identity",
                    return_value=expected,
                ) as completion_identity,
            ):
                observed = controller.preexisting_source_identity(plan)
                self.assertEqual(observed[0], expected)
                self.assertEqual(
                    observed[1]["source_results_sha256"],
                    contract["source_results_sha256"],
                )
                self.assertTrue(completion_identity.call_args.kwargs["isolated_config"])
                (source_dir / "results.jsonl").write_text('{"changed":true}\n', encoding="utf-8")
                with self.assertRaises(controller.ControllerError):
                    controller.preexisting_source_identity(plan)

    def test_analyzer_ledger_rejects_ambiguous_usage_and_cross_task_ids(self) -> None:
        expected = {
            "provider": "openrouter",
            "model": "anthropic/claude-opus-4.8",
        }
        attempt = {
            "attempt": 1,
            "physical_attempt_id": "1" * 32,
            "requested_provider": "openrouter",
            "requested_model": "anthropic/claude-opus-4.8",
            "provider": "openrouter",
            "model": "anthropic/claude-opus-4.8",
            "usage_unknown": False,
            "input_tokens": 10,
            "output_tokens": 2,
            "reasoning_tokens": 0,
            "cached_tokens": 3,
            "cache_write_tokens": 0,
            "billed_cost": 0.01,
            "provider_usage": {
                "usage_unknown": False,
                "physical_attempt_id": "1" * 32,
            },
        }
        analyzer = {
            **expected,
            "normalization_warnings": [],
            "usage": {
                "attempt_count": 1,
                "physical_request_count": 1,
                "usage_unknown_count": 0,
                "input_tokens": 10,
                "output_tokens": 2,
                "reasoning_tokens": 0,
                "cached_tokens": 3,
                "cache_write_tokens": 0,
                "billed_cost": 0.01,
                "physical_attempts": [attempt],
            },
        }
        usage, _, _ = controller._validated_analyzer_attempt_ledger(
            task_id="task-a",
            analyzer=analyzer,
            expected_config=expected,
            allow_zero_attempts=False,
        )
        owners: dict[str, str] = {}
        controller.register_analyzer_attempt_owners(owners, task_id="task-a", usage=usage)
        with self.assertRaises(controller.ControllerError):
            controller.register_analyzer_attempt_owners(owners, task_id="task-b", usage=usage)

        bool_aggregate = copy.deepcopy(analyzer)
        bool_aggregate["usage"]["input_tokens"] = True
        with self.assertRaises(controller.ControllerError):
            controller._validated_analyzer_attempt_ledger(
                task_id="task-a",
                analyzer=bool_aggregate,
                expected_config=expected,
                allow_zero_attempts=False,
            )

        conflicting_id = copy.deepcopy(analyzer)
        conflicting_id["usage"]["physical_attempts"][0]["reported_physical_attempt_ids"] = [
            "2" * 32
        ]
        with self.assertRaises(controller.ControllerError):
            controller._validated_analyzer_attempt_ledger(
                task_id="task-a",
                analyzer=conflicting_id,
                expected_config=expected,
                allow_zero_attempts=False,
            )

        contradictory_unknown = copy.deepcopy(analyzer)
        unknown_attempt = contradictory_unknown["usage"]["physical_attempts"][0]
        unknown_attempt.update(
            {
                "usage_unknown": True,
                "unknown_reason": "TimeoutError",
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_tokens": 0,
                "billed_cost": 0.0,
                "provider_usage": {
                    "usage_unknown": True,
                    "unknown_reason": "TimeoutError",
                    "physical_attempt_id": "1" * 32,
                },
            }
        )
        contradictory_unknown["usage"].update(
            {
                "usage_unknown_count": 1,
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_tokens": 0,
                "billed_cost": 0.0,
            }
        )
        with self.assertRaises(controller.ControllerError):
            controller._validated_analyzer_attempt_ledger(
                task_id="task-a",
                analyzer=contradictory_unknown,
                expected_config=expected,
                allow_zero_attempts=False,
            )

    def test_authenticated_analyzer_extract_uses_terminal_physical_attempt(self) -> None:
        task_ids = [f"task-{index}" for index in range(10)]
        ranking_config = ranking_config_resolution()["effective_config"]
        request_context = build_request_context(
            message="controller fallback replay fixture",
            turn_metadata={},
            attachments=[],
            candidate_output_tokens=8192,
            aggregator_output_tokens=8192,
            ranking_config=ranking_config,
        )
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
                profile = (
                    fallback_task_profile(
                        routed_tier="c1",
                        request_context=request_context,
                        ranking_config=ranking_config,
                    )
                    if index == 0
                    else {"index": index, "constraints": {"risk": "medium"}}
                )
                attempts = [
                    {
                        "attempt": 1,
                        "physical_attempt_id": f"{index + 1:032x}",
                        "requested_provider": "openrouter",
                        "requested_model": "anthropic/claude-opus-4.8",
                        "usage_unknown": True,
                        "unknown_reason": "TimeoutError",
                        "output_tokens": 0,
                        "provider_usage": {
                            "usage_unknown": True,
                            "unknown_reason": "TimeoutError",
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
                    "usage": {
                        "attempt_count": 2,
                        "output_tokens": 100 + index,
                        "physical_attempts": attempts,
                    },
                }
                if index == 0:
                    for attempt in attempts:
                        attempt.update(
                            {
                                "provider": "",
                                "model": "",
                                "usage_unknown": True,
                                "unknown_reason": "TimeoutError",
                                "input_tokens": 0,
                                "output_tokens": 0,
                                "reasoning_tokens": 0,
                                "cached_tokens": 0,
                                "cache_write_tokens": 0,
                                "billed_cost": 0.0,
                                "provider_usage": {
                                    "usage_unknown": True,
                                    "unknown_reason": "TimeoutError",
                                    "physical_attempt_id": attempt["physical_attempt_id"],
                                },
                            }
                        )
                    analyzer.update(
                        {
                            "source": "router_fallback",
                            "schema_valid": False,
                            "fallback_reason": "TimeoutError",
                            "usage": {
                                "attempt_count": 2,
                                "output_tokens": 0,
                                "physical_attempts": attempts,
                            },
                        }
                    )
                selection = {
                    "task_profile_pre_escalation": profile,
                    "task_analyzer": analyzer,
                    "ranking_parameters": copy.deepcopy(ranking_config),
                    "request_context": copy.deepcopy(request_context),
                    "routed_tier": "c1",
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
            extract_kwargs = {
                "source_arm": source_arm,
                "source_dir": root,
                "destination": root / "artifact.json",
                "expected_task_ids": set(task_ids),
                "snapshot": Path(__file__).resolve().parents[2],
                "snapshot_identity": {"commit": "c", "tree": "t"},
                "plan_sha256": "p",
                "replay_schema": controller.FROZEN_TASK_ANALYSIS_SCHEMA_V2,
            }
            with self.assertRaises(controller.ControllerError):
                controller.extract_analyzer_artifact(
                    **extract_kwargs,
                    allow_deterministic_router_fallback=False,
                )
            v1_kwargs = {
                **extract_kwargs,
                "replay_schema": controller.FROZEN_TASK_ANALYSIS_SCHEMA_V1,
            }
            with self.assertRaises(controller.ControllerError):
                controller.extract_analyzer_artifact(
                    **v1_kwargs,
                    allow_deterministic_router_fallback=True,
                )
            artifact = controller.extract_analyzer_artifact(
                **extract_kwargs,
                allow_deterministic_router_fallback=True,
            )
            observed = sorted(
                row["final_successful_physical_attempt_output_tokens"]
                for row in artifact["profiles"].values()
                if row["origin_outcome"] == "live_success"
            )
            self.assertEqual(observed, list(range(101, 110)))
            replay = artifact["replay_payload"]["entries"]["task-0"]
            self.assertEqual(replay["task_analyzer"]["normalization_warnings"], ["warning-0"])
            self.assertIs(replay["task_analyzer"]["schema_valid"], False)
            self.assertEqual(replay["task_analyzer"]["fallback_reason"], "TimeoutError")
            self.assertEqual(replay["origin_outcome"], "deterministic_router_fallback")
            self.assertIn("task_profile_pre_escalation", replay)

            def isolated_validation(snapshot: Path, **kwargs: object) -> dict[str, object]:
                return {
                    "derived": copy.deepcopy(kwargs["profile"]),
                    "normalized": copy.deepcopy(kwargs["profile"]),
                    "schema_valid": True,
                    "analyzer_version": "opus-4.8-json-v3",
                    "module_path": str(snapshot / "ranking_router.py"),
                    "module_sha256": "a" * 64,
                }

            imported_evidence = {
                "source_snapshot_package_dir": str(root),
                "source_snapshot_commit": "old-commit",
                "source_snapshot_tree": "old-tree",
                "source_output_dir": "/original/source",
                "receipt_sha256": "r" * 64,
            }
            with mock.patch.object(
                controller,
                "validate_fallback_profile_isolated",
                side_effect=isolated_validation,
            ) as isolated:
                controller.extract_analyzer_artifact(
                    **{
                        **extract_kwargs,
                        "destination": root / "imported-artifact.json",
                    },
                    allow_deterministic_router_fallback=True,
                    source_import_evidence=imported_evidence,
                )
            self.assertEqual(isolated.call_count, 2)
            self.assertEqual(Path(isolated.call_args_list[0].args[0]), root)
            self.assertEqual(
                Path(isolated.call_args_list[1].args[0]),
                extract_kwargs["snapshot"],
            )
            receipt = controller.derive_analyzer_p99_receipt(
                artifact,
                destination=root / "p99.json",
                plan_sha256="p",
            )
            self.assertEqual(receipt["ordered_output_tokens"], list(range(101, 110)))
            self.assertEqual(receipt["eligibility"]["eligible_denominator"], 9)
            self.assertEqual(receipt["eligibility"]["excluded_denominator"], 1)
            self.assertEqual(receipt["eligibility"]["excluded_task_ids"], ["task-0"])

    def test_analyzer_p99_requires_eight_live_observations(self) -> None:
        profiles = {
            f"task-{index}": {
                "origin_outcome": (
                    "live_success" if index < 7 else "deterministic_router_fallback"
                ),
                "final_successful_physical_attempt_output_tokens": 100 + index,
            }
            for index in range(10)
        }
        artifact = {"artifact_sha256": "a" * 64, "profiles": profiles}
        with tempfile.TemporaryDirectory() as raw, self.assertRaises(controller.ControllerError):
            controller.derive_analyzer_p99_receipt(
                artifact,
                destination=Path(raw) / "p99.json",
                plan_sha256="p",
            )

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
                {"index": index, "selected_for_aggregation": True} for index in range(3)
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

    def test_aggregator_prompt_evidence_is_authoritative_with_optional_ranking_version(
        self,
    ) -> None:
        def prompt_evidence(version: str) -> dict[str, object]:
            payload: dict[str, object] = {
                "schema": controller.AGGREGATOR_PROMPT_SCHEMA,
                "version": version,
                "description": f"prompt contract for {version}",
                "additional_instructions": [],
            }
            return {**payload, "sha256": controller.canonical_sha256(payload)}

        version = "aggregator-v1-current"
        evidence = prompt_evidence(version)
        helpers = {"aggregator_prompt_version_evidence": prompt_evidence}

        without_ranking_version = {
            "aggregator_prompt": copy.deepcopy(evidence),
            "ranking_parameters": {"aggregator": {"candidate_count": 3}},
        }
        self.assertEqual(
            controller._validated_aggregator_prompt(
                without_ranking_version,
                helpers=helpers,
            ),
            evidence,
        )

        with_matching_ranking_version = copy.deepcopy(without_ranking_version)
        with_matching_ranking_version["ranking_parameters"]["aggregator"][
            "prompt_version"
        ] = version
        self.assertEqual(
            controller._validated_aggregator_prompt(
                with_matching_ranking_version,
                helpers=helpers,
            ),
            evidence,
        )

        with_conflicting_ranking_version = copy.deepcopy(without_ranking_version)
        with_conflicting_ranking_version["ranking_parameters"]["aggregator"][
            "prompt_version"
        ] = "aggregator-v2-verify-first"
        with self.assertRaisesRegex(
            controller.ControllerError,
            "differs from ranking parameters",
        ):
            controller._validated_aggregator_prompt(
                with_conflicting_ranking_version,
                helpers=helpers,
            )

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

    def test_bridge_quality_gate_recomputes_mean_median_wtl_and_floor(self) -> None:
        passing = controller._bridge_quality_projection(
            task_deltas={f"task-{index}": float(index - 4) for index in range(10)},
            label="passing",
        )
        self.assertTrue(passing["pass"])
        self.assertAlmostEqual(passing["mean_delta_quality"], 0.5)
        self.assertAlmostEqual(passing["median_delta_quality"], 0.5)
        self.assertEqual(
            (passing["wins"], passing["ties"], passing["losses"]),
            (5, 1, 4),
        )
        failing = controller._bridge_quality_projection(
            task_deltas={
                **{f"task-{index}": 2.0 for index in range(9)},
                "task-9": -10.01,
            },
            label="floor",
        )
        self.assertFalse(failing["pass"])
        self.assertFalse(failing["gates"]["no_task_delta_below_minus_10"])

    def test_bridge_replicates_are_judged_as_one_task_mean_group(self) -> None:
        members = [
            controller.Arm(
                arm_id=f"P0.5-11-E1-R{replicate}",
                experiment_id="P0.5-11",
                directory_name="P0-5-11",
                variant="E1",
                replicate=replicate,
                analyzer_mode="frozen_replay",
                override={},
                dynamic=None,
                wire_gate=None,
                output_name=f"replicate-{replicate}",
                control_arm_id=f"common-E0-R{replicate}",
            )
            for replicate in (1, 2, 3)
        ]
        controls = [
            controller.Arm(
                arm_id=f"common-E0-R{replicate}",
                experiment_id="common-E0",
                directory_name="common",
                variant="E0-replay",
                replicate=replicate,
                analyzer_mode="frozen_replay",
                override={},
                dynamic=None,
                wire_gate=None,
                output_name=f"control-{replicate}",
                control_arm_id=None,
            )
            for replicate in (1, 2, 3)
        ]
        by_id = {arm.arm_id: arm for arm in [*members, *controls]}
        individual = {
            member.arm_id: {
                "task_deltas": {f"task-{index}": value for index in range(10)},
                "pass": value >= 0,
            }
            for member, value in zip(members, (-2.0, 1.0, 4.0), strict=True)
        }
        repeated_values = {f"task-{index}": 1.0 for index in range(10)}
        report = {
            "experiments": {
                "P0.5-11": {
                    "repeated_pairing": {
                        "replicate_count": 3,
                        "task_count": 10,
                        "complete_task_id_pairing": True,
                        "mean_delta_quality": 1.0,
                        "wins": 10,
                        "ties": 0,
                        "losses": 0,
                        "per_task_mean_delta": repeated_values,
                    }
                }
            }
        }
        with (
            mock.patch.object(
                controller,
                "_bridge_arm_completion_gate",
                return_value={"pass": True},
            ),
            mock.patch.object(
                controller,
                "_bridge_individual_quality_gate",
                side_effect=lambda _report, arm, _control: individual[arm.arm_id],
            ),
        ):
            gate = controller._bridge_group_gate(
                members=members,
                by_id=by_id,
                source_plan={},
                terminal_status={},
                report=report,
            )
        self.assertTrue(gate["pass"])
        self.assertEqual(gate["quality_basis"], "replicate_task_mean_pairing")
        self.assertEqual(gate["candidate_arm_ids"], [arm.arm_id for arm in members])
        self.assertEqual(gate["quality"]["task_deltas"], repeated_values)

    def test_bridge_and_legacy_commands_fail_before_creating_run_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            legacy_plan_path = root / "legacy-plan.json"
            write_json(legacy_plan_path, {"paths": {"run_root": str(root / "legacy-run")}})
            with (
                mock.patch.object(controller, "validate_plan", return_value=[]),
                self.assertRaisesRegex(controller.ControllerError, "screening/legacy plan"),
            ):
                controller.launch_confirmatory_schedule(
                    legacy_plan_path,
                    root / "schedule.json",
                )
            self.assertFalse((root / "legacy-run").exists())

            bridge_plan_path = root / "bridge-plan.json"
            write_json(
                bridge_plan_path,
                {
                    "confirmatory_bridge": {"mode": "confirmatory_only"},
                    "paths": {"run_root": str(root / "bridge-run")},
                },
            )
            with (
                mock.patch.object(controller, "validate_plan", return_value=[]),
                self.assertRaisesRegex(controller.ControllerError, "refuses ordinary campaign"),
            ):
                controller.run_campaign(bridge_plan_path)
            self.assertFalse((root / "bridge-run").exists())

    def test_bridge_immutable_json_is_idempotent_and_tamper_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "receipt.json"
            controller._write_new_or_identical_json(path, {"bound": True})
            first = path.read_bytes()
            controller._write_new_or_identical_json(path, {"bound": True})
            self.assertEqual(first, path.read_bytes())
            with self.assertRaisesRegex(controller.ControllerError, "refusing to overwrite"):
                controller._write_new_or_identical_json(path, {"bound": False})

    def test_bridge_launcher_support_rejects_old_launcher_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            launcher = root / "old-launcher.sh"
            launcher.write_text("#!/usr/bin/env bash\necho legacy\n", encoding="utf-8")
            plan = {
                "paths": {"launcher_relative": "old-launcher.sh"},
                "freeze": {
                    "sources": {"launcher_raw_sha256": controller.file_sha256(launcher)}
                },
                "confirmatory_bridge": {
                    "launcher_contract": {
                        "raw_sha256": controller.file_sha256(launcher),
                        "flag": "--confirmatory-schedule",
                        "fd_flag": "--confirmatory-schedule-fd",
                        "schedule_schema": controller.CONFIRMATORY_SCHEDULE_SCHEMA,
                    }
                },
            }
            with self.assertRaisesRegex(controller.ControllerError, "does not support"):
                controller.validate_confirmatory_launcher_support(plan, snapshot=root)

    def test_bridge_launcher_replacement_after_authentication_fails_before_exec(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            snapshot = Path(raw)
            launcher = snapshot / "launcher.sh"
            launcher.write_text(
                "#!/usr/bin/env bash\n"
                "# --confirmatory-schedule --confirmatory-schedule-fd\n"
                "run_confirmatory_pair() { :; }\n",
                encoding="utf-8",
            )
            digest = controller.file_sha256(launcher)
            plan = {
                "paths": {"launcher_relative": "launcher.sh"},
                "freeze": {"sources": {"launcher_raw_sha256": digest}},
                "confirmatory_bridge": {
                    "launcher_contract": {
                        "raw_sha256": digest,
                        "flag": "--confirmatory-schedule",
                        "fd_flag": "--confirmatory-schedule-fd",
                        "schedule_schema": controller.CONFIRMATORY_SCHEDULE_SCHEMA,
                    }
                },
            }
            fd, evidence, payload = controller.open_authenticated_confirmatory_launcher(
                plan, snapshot=snapshot
            )
            try:
                replacement = snapshot / "replacement.sh"
                replacement.write_text(
                    "#!/usr/bin/env bash\necho attacker\n", encoding="utf-8"
                )
                os.replace(replacement, launcher)
                with self.assertRaisesRegex(
                    controller.ControllerError, "replaced after authentication"
                ):
                    controller._assert_fd_still_names_path(
                        fd, launcher, label="confirmatory launcher"
                    )
                os.lseek(fd, 0, os.SEEK_SET)
                self.assertNotIn(b"attacker", os.read(fd, 4096))
            finally:
                os.close(fd)

    def test_bridge_sealed_launcher_and_schedule_ignore_in_place_source_mutation(
        self,
    ) -> None:
        if not hasattr(os, "memfd_create"):
            self.skipTest("Linux memfd is required")
        with tempfile.TemporaryDirectory() as raw:
            snapshot = Path(raw)
            marker = snapshot / "attacker-ran"
            launcher = snapshot / "launcher.sh"
            launcher.write_text(
                "#!/usr/bin/env bash\n# --confirmatory-schedule --confirmatory-schedule-fd\n"
                "run_confirmatory_pair() { :; }\nexit 0\n",
                encoding="utf-8",
            )
            digest = controller.file_sha256(launcher)
            plan = {
                "paths": {"launcher_relative": "launcher.sh"},
                "freeze": {"sources": {"launcher_raw_sha256": digest}},
                "confirmatory_bridge": {
                    "launcher_contract": {
                        "raw_sha256": digest,
                        "flag": "--confirmatory-schedule",
                        "fd_flag": "--confirmatory-schedule-fd",
                        "schedule_schema": controller.CONFIRMATORY_SCHEDULE_SCHEMA,
                    }
                },
            }
            source_fd, evidence, launcher_payload = (
                controller.open_authenticated_confirmatory_launcher(
                    plan, snapshot=snapshot
                )
            )
            schedule_path = snapshot / "schedule.json"
            schedule_payload = b'{"safe":true}\n'
            schedule_path.write_bytes(schedule_payload)
            launcher_memfd = controller.sealed_memfd(
                launcher_payload, label="launcher-test", executable=True
            )
            schedule_memfd = controller.sealed_memfd(
                schedule_payload, label="schedule-test", executable=False
            )
            try:
                # Same-inode truncation/rewrite is detected, while the executed bytes
                # remain the already sealed safe payload.
                launcher.write_text(
                    "#!/usr/bin/env bash\ntouch " + str(marker) + "\n",
                    encoding="utf-8",
                )
                schedule_path.write_bytes(b'{"evil":true}\n')
                with self.assertRaisesRegex(
                    controller.ControllerError, "bytes changed"
                ):
                    controller._assert_authenticated_fd_bytes(
                        source_fd,
                        launcher,
                        label="confirmatory launcher",
                        expected_sha256=evidence["raw_sha256"],
                    )
                completed = subprocess.run(
                    [f"/proc/self/fd/{launcher_memfd}"],
                    pass_fds=(launcher_memfd,),
                    check=False,
                )
                self.assertEqual(completed.returncode, 0)
                self.assertFalse(marker.exists())
                self.assertEqual(
                    os.pread(schedule_memfd, len(schedule_payload), 0),
                    schedule_payload,
                )
                with self.assertRaises(OSError):
                    os.write(schedule_memfd, b"tamper")
            finally:
                for fd in (source_fd, launcher_memfd, schedule_memfd):
                    os.close(fd)

    def test_bridge_rejects_parent_symlink_and_writable_root_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            real = root / "real"
            real.mkdir()
            alias = root / "alias"
            alias.symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(controller.ControllerError, "must not traverse"):
                controller.secure_future_absolute_path(
                    alias / "new-run", label="confirmatory run root"
                )

            source_run = root / "source-run"
            source_report = root / "source-report"
            source_snapshot = root / "source-snapshot"
            current_snapshot = root / "current-snapshot"
            for path in (source_run, source_report, source_snapshot, current_snapshot):
                path.mkdir()
            source_plan = {
                "paths": {
                    "run_root": str(source_run),
                    "report_root": str(source_report),
                    "snapshot": str(source_snapshot),
                }
            }
            source_plan_path = source_run / "campaign-plan.json"
            write_json(source_plan_path, source_plan)
            source = {
                "screening_plan": {"path": str(source_plan_path)},
                "immutable_roots": {
                    "run_root": str(source_run),
                    "report_root": str(source_report),
                    "snapshot": str(source_snapshot),
                },
            }
            with self.assertRaisesRegex(controller.ControllerError, "overlaps immutable"):
                controller.validate_confirmatory_root_isolation(
                    run_root=current_snapshot / "nested-run",
                    report_root=root / "new-report",
                    snapshot=current_snapshot,
                    screening_source=source,
                )

            # Even after validation, replacing the mutable source plan cannot
            # redefine the immutable safety boundary recorded in source evidence.
            source_plan["paths"]["snapshot"] = str(root / "forged-snapshot")
            (root / "forged-snapshot").mkdir()
            write_json(source_plan_path, source_plan)
            with self.assertRaisesRegex(
                controller.ControllerError, "roots differ from immutable root contract"
            ):
                controller.validate_confirmatory_root_isolation(
                    run_root=source_snapshot / "would-escape",
                    report_root=root / "other-report",
                    snapshot=current_snapshot,
                    screening_source=source,
                )

    def test_bridge_snapshot_post_operation_tamper_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            snapshot = Path(raw)
            subprocess.run(["git", "init", "-q"], cwd=snapshot, check=True)
            tracked = snapshot / "tracked.txt"
            tracked.write_text("frozen\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=snapshot, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Codex Test",
                    "-c",
                    "user.email=codex@example.invalid",
                    "commit",
                    "-qm",
                    "freeze",
                ],
                cwd=snapshot,
                check=True,
            )
            identity = controller.git_identity(snapshot)
            controller.assert_bridge_snapshot_unchanged(snapshot, identity)
            tracked.write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(controller.ControllerError, "snapshot changed"):
                controller.assert_bridge_snapshot_unchanged(snapshot, identity)

    def test_bridge_resealed_derived_triple_without_terminal_report_binding_is_rejected(
        self,
    ) -> None:
        derived_path = Path("/immutable/run/derived-plan.json")
        descriptor = {
            "path": "/immutable/run/analyzer.json",
            "file_sha256": "a" * 64,
            "artifact_sha256": "b" * 64,
        }
        p99 = {"receipt_sha256": "c" * 64, "value": 100}
        derived = {
            "derived_plan_sha256": "d" * 64,
            "p0_5_06": p99,
            "frozen_analyzer_artifact": descriptor,
        }
        status = {
            "derived_plan": {
                "path": str(derived_path),
                "sha256": derived["derived_plan_sha256"],
            }
        }
        report = {
            "paths": {"derived_plan": str(derived_path)},
            "derived": {
                "valid": True,
                "derived_plan_sha256": derived["derived_plan_sha256"],
                "p0_5_06": p99,
                "frozen_analyzer_artifact": descriptor,
            },
        }
        controller._assert_screening_derived_evidence_closure(
            status=status,
            report=report,
            derived_path=derived_path,
            derived=derived,
            analyzer_descriptor=descriptor,
            p99=p99,
        )
        resealed = copy.deepcopy(derived)
        resealed["derived_plan_sha256"] = "e" * 64
        resealed_p99 = {"receipt_sha256": "f" * 64, "value": 101}
        with self.assertRaisesRegex(controller.ControllerError, "not jointly bound"):
            controller._assert_screening_derived_evidence_closure(
                status=status,
                report=report,
                derived_path=derived_path,
                derived=resealed,
                analyzer_descriptor={**descriptor, "artifact_sha256": "0" * 64},
                p99=resealed_p99,
            )

    def test_bridge_post_report_formal_or_group_tamper_is_rejected(self) -> None:
        hashes = {
            "manifest.json": "1" * 64,
            "results.jsonl": "2" * 64,
            "trace.jsonl": "3" * 64,
            "audit.json": "4" * 64,
            "openrouter-non-byok-campaign-proof.json": "5" * 64,
        }
        evidence = {
            "artifact_sha256": {
                "manifest": hashes["manifest.json"],
                "results": hashes["results.jsonl"],
                "trace": hashes["trace.jsonl"],
                "audit": hashes["audit.json"],
                "proof": hashes["openrouter-non-byok-campaign-proof.json"],
            }
        }
        status_arm = {"completion_evidence": copy.deepcopy(evidence)}
        report_arm = {
            "completion_evidence": copy.deepcopy(evidence),
            "controller_reinspection": {"evidence": copy.deepcopy(evidence)},
        }
        controller._assert_formal_artifact_evidence_closure(
            arm_id="P0-12-E1",
            actual=hashes,
            status_arm=status_arm,
            report_arm=report_arm,
        )
        for key in list(hashes):
            tampered = dict(hashes)
            tampered[key] = "0" * 64
            with self.assertRaisesRegex(controller.ControllerError, "formal artifact hashes"):
                controller._assert_formal_artifact_evidence_closure(
                    arm_id="P0-12-E1",
                    actual=tampered,
                    status_arm=status_arm,
                    report_arm=report_arm,
                )
        with tempfile.TemporaryDirectory() as raw:
            group_root = Path(raw)
            markdown = group_root / "EXPERIMENT_RESULTS.md"
            group_json = group_root / "EXPERIMENT_RESULTS.json"
            markdown.write_text("good\n", encoding="utf-8")
            group_json.write_text("{}\n", encoding="utf-8")
            descriptors = {
                "markdown": {
                    "path": str(markdown),
                    "sha256": controller.file_sha256(markdown),
                    "size_bytes": markdown.stat().st_size,
                },
                "json": {
                    "path": str(group_json),
                    "sha256": controller.file_sha256(group_json),
                    "size_bytes": group_json.stat().st_size,
                },
            }
            report_doc = {"group_report_artifacts": {"P0-12": descriptors}}
            controller._assert_group_report_artifact_closure(
                experiment_id="P0-12",
                group_root=group_root,
                report=report_doc,
            )
            markdown.write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(controller.ControllerError, "differs"):
                controller._assert_group_report_artifact_closure(
                    experiment_id="P0-12",
                    group_root=group_root,
                    report=report_doc,
                )

    def test_terminal_report_receipt_fails_closed_on_group_or_formal_tamper(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_root = root / "run"
            report_root = root / "reports"
            run_root.mkdir()
            report_root.mkdir()
            reporter = root / "reporter.py"
            reporter.write_text("raise SystemExit(0)\n", encoding="utf-8")
            plan_path = run_root / "campaign-plan.json"
            status_path = run_root / "terminal-status-input.json"
            write_json(plan_path, {"fixture": True})
            write_json(status_path, {"fixture": True})
            plan = {
                "paths": {
                    "run_root": str(run_root),
                    "report_root": str(report_root),
                    "reporter": str(reporter),
                    "python": sys.executable,
                },
                "freeze": {
                    "sources": {
                        "reporter_raw_sha256": controller.file_sha256(reporter)
                    }
                },
            }
            for message in (
                "published group artifact changed",
                "formal arm changed after report generation",
            ):
                with mock.patch.object(
                    controller,
                    "validate_terminal_report_closure",
                    side_effect=controller.ControllerError(message),
                ):
                    receipt, success = controller.run_terminal_report(
                        plan,
                        plan_path=plan_path,
                        terminal_status_path=status_path,
                    )
                self.assertFalse(success)
                self.assertEqual(receipt["status"], "failed")
                self.assertIsNone(receipt["report_closure_sha256"])

    def test_terminal_closure_rejects_archived_source_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "formal"
            archive = root / "archive" / "waves" / "wave-1"
            archive.mkdir(parents=True)
            source_manifest = archive / "manifest.json"
            source_result = archive / "draco_ensemble_G1.jsonl"
            source_manifest.write_text('{"status":"complete"}\n', encoding="utf-8")
            source_result.write_text('{"task_id":"1"}\n', encoding="utf-8")
            write_json(
                root / "manifest.json",
                {
                    "source_manifests": [
                        {
                            "path": str(source_manifest),
                            "sha256": controller.file_sha256(source_manifest),
                            "result_path": str(source_result),
                            "result_sha256": controller.file_sha256(source_result),
                        }
                    ]
                },
            )
            observed = controller._bridge_archived_source_hashes(
                root, label="fixture"
            )
            self.assertEqual(set(observed), {str(source_manifest), str(source_result)})
            source_result.write_text('{"tampered":true}\n', encoding="utf-8")
            with self.assertRaisesRegex(
                controller.ControllerError, "archived result 0 hash/path differs"
            ):
                controller._bridge_archived_source_hashes(root, label="fixture")

    def test_screening_cost_identity_scope_includes_non_survivor_and_rejects_reuse(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            def write_arm(arm_id: str, seed: str) -> tuple[Path, str]:
                arm_root = root / arm_id
                arm_root.mkdir()
                source_sha256 = {
                    key: hashlib.sha256(f"{seed}:{key}".encode()).hexdigest()
                    for key in (
                        "account_before",
                        "account_after",
                        "account_reconciliation",
                        "runtime_environment",
                    )
                }
                write_json(
                    arm_root / "manifest.json",
                    {
                        "cost_attribution": {
                            "account_windows": [
                                {"source_sha256": source_sha256}
                            ]
                        }
                    },
                )
                identity = controller.canonical_sha256(
                    {
                        "account_before_sha256": source_sha256["account_before"],
                        "account_after_sha256": source_sha256["account_after"],
                        "account_reconciliation_sha256": source_sha256[
                            "account_reconciliation"
                        ],
                        "runtime_environment_sha256": source_sha256[
                            "runtime_environment"
                        ],
                    }
                )
                return arm_root, identity

            arms = {}
            expected_identities = []
            for arm_id in ("control", "survivor", "non-survivor"):
                arm_root, identity = write_arm(arm_id, arm_id)
                expected_identities.append(identity)
                arms[arm_id] = {
                    "formal_evidence_valid": True,
                    "rows": [{"task_id": "1"}],
                    "output_dir": str(arm_root),
                }
            ids, identities = controller._screening_cost_identity_scope(
                {"arms": arms}
            )
            self.assertEqual(ids, ["control", "non-survivor", "survivor"])
            self.assertEqual(identities, sorted(expected_identities))

            survivor_manifest = (root / "survivor" / "manifest.json").read_bytes()
            (root / "non-survivor" / "manifest.json").write_bytes(
                survivor_manifest
            )
            with self.assertRaisesRegex(
                controller.ControllerError, "reuse one account-window identity"
            ):
                controller._screening_cost_identity_scope({"arms": arms})


if __name__ == "__main__":
    unittest.main()
